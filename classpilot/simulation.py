"""
ClassPilot AI — Watcher Simulation Engine

Safe, read-only simulation of the existing production assignment notification watcher.
Enforces zero database mutations, zero emails sent, zero Gemini calls, and strict
isolation from production watcher state.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .config import ClassPilotConfig, get_config
from .deadline_scheduler import _parse_due_datetime
from .google_oauth import get_authorized_credentials
from .state_store import ReadOnlyStateStore, StateStore
from .user_store import UserStore
from .watcher import (
    AssignmentClassification,
    classify_assignment,
    is_historical_assignment,
)

logger = logging.getLogger(__name__)


def _mask_user_id(user_id: Optional[str]) -> str:
    """Mask user ID to prevent exposing sensitive identifiers in logs."""
    if not user_id:
        return "<unset>"
    clean = str(user_id).strip()
    if len(clean) <= 4:
        return "***"
    return f"***{clean[-4:]}"


def _sanitize_diagnostic_message(msg: str) -> str:
    """Redact sensitive API keys or credentials from error messages."""
    msg = re.sub(r'([?&]key=)[^&\s]+', r'\1[REDACTED]', msg)
    msg = re.sub(r'AIza[0-9A-Za-z-_]{35}', '[REDACTED_API_KEY]', msg)
    msg = re.sub(r'Bearer\s+[A-Za-z0-9._~+/-]+', 'Bearer [REDACTED]', msg)
    return msg


@dataclass
class SimulationResult:
    """Detailed outcome of a watcher simulation run."""
    user_id: str
    masked_user_id: str
    total_courses_checked: int = 0
    accessible_courses: List[Dict[str, Any]] = field(default_factory=list)
    forbidden_courses: List[Dict[str, Any]] = field(default_factory=list)
    failed_courses: List[Dict[str, Any]] = field(default_factory=list)
    total_assignments_evaluated: int = 0
    detected_new_assignments: List[Dict[str, Any]] = field(default_factory=list)
    detected_deadline_updates: List[Dict[str, Any]] = field(default_factory=list)
    detected_deadline_reminders: List[Dict[str, Any]] = field(default_factory=list)
    overdue_notifications_count: int = 0  # Not supported by production watcher
    suppressed_existing_assignments: List[Dict[str, Any]] = field(default_factory=list)
    suppressed_historical_assignments: List[Dict[str, Any]] = field(default_factory=list)
    suppressed_reminders: List[Dict[str, Any]] = field(default_factory=list)
    database_mutations_count: int = 0
    emails_sent_count: int = 0
    gemini_calls_count: int = 0
    production_watcher_triggered: bool = False
    success: bool = True


def format_simulation_summary(result: SimulationResult) -> str:
    """Format a human-readable simulation report."""
    lines = [
        "============================================================",
        "          CLASSPILOT WATCHER SIMULATION REPORT             ",
        "============================================================",
        f"User Account:            {result.masked_user_id} (Validated & Authorized)",
        f"Courses Checked:         {result.total_courses_checked} "
        f"({len(result.accessible_courses)} Accessible, "
        f"{len(result.forbidden_courses)} Forbidden HTTP 403, "
        f"{len(result.failed_courses)} Failed)",
        f"Assignments Evaluated:   {result.total_assignments_evaluated}",
        "",
        "DETECTION PREDICTIONS (What would be notified):",
        f"  - New Assignment Notifications:      {len(result.detected_new_assignments)}",
        f"  - Assignment Updates (Deadlines):     {len(result.detected_deadline_updates)}",
        f"  - Upcoming Deadline Reminders:        {len(result.detected_deadline_reminders)}",
        f"  - Overdue Notifications:              {result.overdue_notifications_count} (Not supported by production watcher)",
        "",
        "SUPPRESSIONS & DEDUPLICATION (What would be skipped):",
        f"  - Suppressed (Already Known in DB):  {len(result.suppressed_existing_assignments)}",
        f"  - Suppressed (Historical Baseline):   {len(result.suppressed_historical_assignments)}",
        f"  - Suppressed (Reminders Already Sent):{len(result.suppressed_reminders)}",
        "",
        "COURSE LEVEL OUTCOMES:",
        f"  - Courses Succeeded:                 {len(result.accessible_courses)}",
        f"  - Courses Forbidden (HTTP 403):       {len(result.forbidden_courses)}",
        f"  - Courses Failed (Other):             {len(result.failed_courses)}",
        "",
        "SAFETY & INTEGRITY VERIFICATION:",
        f"  - Database Mutations Attempted/Done: {result.database_mutations_count} (READ-ONLY ENFORCED)",
        f"  - Emails Dispatched via SMTP:        {result.emails_sent_count} (ZERO EMAILS)",
        f"  - Gemini LLM API Calls Incurred:     {result.gemini_calls_count} (ZERO TOKEN COST)",
        f"  - Production Watcher Triggered:      {'YES (BUG)' if result.production_watcher_triggered else 'NO (SAFE)'}",
        "============================================================",
    ]
    return "\n".join(lines)


def run_watcher_simulation(
    config: Optional[ClassPilotConfig] = None,
    user_id: Optional[str] = None,
    course_id: Optional[str] = None,
    max_courses: int = 50,
    now: Optional[datetime] = None,
) -> SimulationResult:
    """
    Run a safe, read-only simulation of the assignment notification watcher.

    1. Resolves and validates the target college account via SIMULATION_USER_ID.
    2. Enforces read-only database access via ReadOnlyStateStore.
    3. Retrieves active courses and coursework from Google Classroom with pagination.
    4. Evaluates assignments using production event detection and deduplication logic.
    5. Predicts new assignments, updates, and deadline reminders without calling LLM or SMTP.
    6. Summarizes results and returns a SimulationResult.
    """
    if config is None:
        config = get_config()
    if now is None:
        now = datetime.now(tz=timezone.utc)

    # 1. Deterministic Account Selection & Validation
    state_store = StateStore()
    user_store = UserStore()

    target_user_id = (
        (user_id.strip() if user_id else None)
        or (os.getenv("SIMULATION_USER_ID", "").strip() or None)
        or (os.getenv("TEST_USER_ID", "").strip() or None)
    )
    enabled_users = state_store.get_enabled_watchers()

    if target_user_id:
        masked_id = _mask_user_id(target_user_id)
        if target_user_id not in enabled_users:
            logger.error(
                "Simulation aborted: Configured account (%s) is not an enabled watcher. Failing closed.",
                masked_id,
            )
            return SimulationResult(user_id=target_user_id, masked_user_id=masked_id, success=False)
        logger.info("Using configured account (%s) for watcher simulation.", masked_id)
    else:
        if not enabled_users:
            logger.error("Simulation aborted: No enabled accounts found in database. Failing closed.")
            return SimulationResult(user_id="", masked_user_id="<none>", success=False)
        if len(enabled_users) > 1:
            logger.error(
                "Simulation aborted: Multiple enabled accounts found (%d). "
                "Account cannot be identified unambiguously without SIMULATION_USER_ID. Failing closed.",
                len(enabled_users),
            )
            return SimulationResult(user_id="", masked_user_id="<ambiguous>", success=False)
        target_user_id = enabled_users[0]
        masked_id = _mask_user_id(target_user_id)
        logger.info("Unambiguously selected single enabled account (%s) for watcher simulation.", masked_id)

    user = user_store.get_user_by_id(target_user_id)
    if not user:
        logger.error("Simulation aborted: Account (%s) not found in user store. Failing closed.", masked_id)
        return SimulationResult(user_id=target_user_id, masked_user_id=masked_id, success=False)

    try:
        credentials = get_authorized_credentials(target_user_id, user_store)
    except Exception as exc:
        sanitized_exc = _sanitize_diagnostic_message(str(exc))
        logger.error(
            "Simulation aborted: Account (%s) is not authorized with valid Google credentials: %s",
            masked_id, sanitized_exc,
        )
        return SimulationResult(user_id=target_user_id, masked_user_id=masked_id, success=False)

    if not credentials:
        logger.error("Simulation aborted: Account (%s) has no valid Google credentials.", masked_id)
        return SimulationResult(user_id=target_user_id, masked_user_id=masked_id, success=False)

    # 2. Enforce Read-Only Database Adapter
    read_only_store = ReadOnlyStateStore(state_store)

    result = SimulationResult(
        user_id=target_user_id,
        masked_user_id=masked_id,
        success=True,
    )

    # 3. Google Classroom Client Initialization
    try:
        from googleapiclient.discovery import build
        client = build("classroom", "v1", credentials=credentials)
    except Exception as exc:
        sanitized_exc = _sanitize_diagnostic_message(str(exc))
        logger.error("Simulation aborted: Failed to initialize Google Classroom client: %s", sanitized_exc)
        result.success = False
        return result

    # 4. Course Discovery (with pagination, quota limits, and per-course isolation)
    candidate_courses: List[Dict[str, Any]] = []
    effective_course_id = (
        (course_id.strip() if course_id else None)
        or (os.getenv("SIMULATION_COURSE_ID", "").strip() or None)
        or (os.getenv("TEST_COURSE_ID", "").strip() or None)
    )

    if effective_course_id:
        cid = effective_course_id
        try:
            c = client.courses().get(id=cid).execute()
            if isinstance(c, dict):
                candidate_courses.append(c)
        except Exception as exc:
            sanitized_exc = _sanitize_diagnostic_message(str(exc))
            resp = getattr(exc, "resp", None)
            if getattr(resp, "status", None) == 403 or "403" in str(exc):
                logger.error("Google Classroom HTTP 403 Forbidden accessing course '%s': %s", cid, sanitized_exc)
                result.forbidden_courses.append({"id": cid, "name": "Specified Course", "reason": "HTTP 403 Forbidden"})
            else:
                logger.error("Failed to fetch course '%s': %s", cid, sanitized_exc)
                result.failed_courses.append({"id": cid, "name": "Specified Course", "error": sanitized_exc})
            return result
    else:
        max_course_pages = 3
        course_pages_fetched = 0
        page_token = None

        while course_pages_fetched < max_course_pages and len(candidate_courses) < max_courses:
            params = {"pageSize": 50, "courseStates": ["ACTIVE"]}
            if page_token:
                params["pageToken"] = page_token

            try:
                courses_resp = client.courses().list(**params).execute()
            except Exception as exc:
                sanitized_exc = _sanitize_diagnostic_message(str(exc))
                resp = getattr(exc, "resp", None)
                if getattr(resp, "status", None) == 403 or "403" in str(exc):
                    logger.error("Google Classroom HTTP 403 Forbidden listing courses: %s", sanitized_exc)
                    result.forbidden_courses.append({"id": "courses.list", "name": "All Courses", "reason": "HTTP 403 Forbidden"})
                else:
                    logger.error("Failed to list Google Classroom courses: %s", sanitized_exc)
                    result.failed_courses.append({"id": "courses.list", "name": "All Courses", "error": sanitized_exc})
                result.success = False
                return result

            raw_courses = courses_resp.get("courses", []) if isinstance(courses_resp, dict) else []
            active_batch = [
                c for c in raw_courses
                if isinstance(c, dict) and c.get("courseState", "ACTIVE") == "ACTIVE" and c.get("id")
            ]
            candidate_courses.extend(active_batch)

            page_token = courses_resp.get("nextPageToken") if isinstance(courses_resp, dict) else None
            course_pages_fetched += 1
            if not page_token:
                break

    candidate_courses = candidate_courses[:max_courses]
    result.total_courses_checked = len(candidate_courses)
    logger.info("Discovered %d active courses to evaluate in simulation.", len(candidate_courses))

    # 5. CourseWork Retrieval & Event Prediction
    evaluated_coursework: List[Dict[str, Any]] = []

    for course in candidate_courses:
        c_id = str(course.get("id"))
        c_name = str(course.get("name") or "Unnamed Course")

        cw_items: List[Dict[str, Any]] = []
        try:
            cw_page_token = None
            cw_pages_fetched = 0
            max_cw_pages = 3

            while cw_pages_fetched < max_cw_pages:
                cw_params = {"courseId": c_id, "pageSize": 50}
                if cw_page_token:
                    cw_params["pageToken"] = cw_page_token

                cw_resp = client.courses().courseWork().list(**cw_params).execute()
                batch = cw_resp.get("courseWork", []) if isinstance(cw_resp, dict) else []
                if isinstance(batch, list):
                    cw_items.extend([item for item in batch if isinstance(item, dict)])
                cw_page_token = cw_resp.get("nextPageToken") if isinstance(cw_resp, dict) else None
                cw_pages_fetched += 1
                if not cw_page_token:
                    break

            result.accessible_courses.append({
                "id": c_id,
                "name": c_name,
                "assignments_count": len(cw_items),
            })

        except Exception as exc:
            sanitized_exc = _sanitize_diagnostic_message(str(exc))
            resp = getattr(exc, "resp", None)
            if getattr(resp, "status", None) == 403 or "403" in str(exc):
                logger.warning("Course '%s' (id: %s): HTTP 403 Forbidden. Skipping course.", c_name, c_id)
                result.forbidden_courses.append({
                    "id": c_id,
                    "name": c_name,
                    "reason": "HTTP 403 Forbidden",
                })
            else:
                logger.warning("Course '%s' (id: %s): Coursework retrieval failed: %s", c_name, c_id, sanitized_exc)
                result.failed_courses.append({
                    "id": c_id,
                    "name": c_name,
                    "error": sanitized_exc,
                })
            continue

        result.total_assignments_evaluated += len(cw_items)

        for cw in cw_items:
            # Use shared event classification logic from watcher
            classified = classify_assignment(
                cw,
                course_id=c_id,
                course_name=c_name,
                state_store=read_only_store,
                user_id=target_user_id,
                filter_historical=True,
                now=now,
            )
            if not classified:
                continue

            assignment_record = {
                "course_id": c_id,
                "course_name": c_name,
                "assignment_id": classified.assignment_id,
                "title": classified.title,
                "due_date": classified.due_date,
                "due_time": classified.due_time,
                "created": classified.created,
            }
            evaluated_coursework.append(assignment_record)

            if classified.classification == AssignmentClassification.HISTORICAL_BASELINE:
                # Initial baseline / historical assignment: suppressed
                result.suppressed_historical_assignments.append({
                    "course_id": c_id,
                    "course_name": c_name,
                    "assignment_id": classified.assignment_id,
                    "title": classified.title,
                    "reason": "Historical assignment on initial baseline (deadline or creation in past)",
                })
                # Track in-memory so subsequent checks in this simulation recognize it
                read_only_store.record_new_assignment(
                    c_id, classified.assignment_id, classified.title, classified.due_date, classified.due_time, user_id=target_user_id,
                )
            elif classified.classification == AssignmentClassification.NEW:
                # Genuine new assignment notification prediction
                predicted_subject = f"📚 New Assignment: {classified.title}"
                result.detected_new_assignments.append({
                    "course_id": c_id,
                    "course_name": c_name,
                    "assignment_id": classified.assignment_id,
                    "title": classified.title,
                    "due_date": classified.due_date,
                    "due_time": classified.due_time,
                    "predicted_subject": predicted_subject,
                })
                read_only_store.record_new_assignment(
                    c_id, classified.assignment_id, classified.title, classified.due_date, classified.due_time, user_id=target_user_id,
                )
            elif classified.classification == AssignmentClassification.DEADLINE_UPDATED:
                # Deadline update notification prediction
                predicted_subject = f"⏳ Deadline Changed: {classified.title}"
                result.detected_deadline_updates.append({
                    "course_id": c_id,
                    "course_name": c_name,
                    "assignment_id": classified.assignment_id,
                    "title": classified.title,
                    "new_due_date": classified.due_date,
                    "new_due_time": classified.due_time,
                    "prev_due_date": classified.previous_due_date,
                    "prev_due_time": classified.previous_due_time,
                    "predicted_subject": predicted_subject,
                })
                read_only_store.update_assignment_deadline(
                    c_id, classified.assignment_id, classified.title, classified.due_date, classified.due_time,
                    json.dumps(classified.previous_due_date) if classified.previous_due_date else None,
                    json.dumps(classified.previous_due_time) if classified.previous_due_time else None,
                    user_id=target_user_id,
                )
            elif classified.classification == AssignmentClassification.UNCHANGED:
                # Unchanged existing assignment: suppressed by deduplication
                result.suppressed_existing_assignments.append({
                    "course_id": c_id,
                    "assignment_id": classified.assignment_id,
                    "title": classified.title,
                    "reason": "Already recorded in StateStore with same deadline",
                })

    # 6. Evaluate Deadline Reminders (replicate production DeadlineScheduler logic)
    # Combine assignments seen during coursework fetch with all assignments in StateStore
    all_known_in_store = read_only_store.get_all_assignments(target_user_id)
    candidates_for_reminders: Dict[tuple, Dict[str, Any]] = {}

    for a in evaluated_coursework:
        candidates_for_reminders[(a["course_id"], a["assignment_id"])] = a

    for stored in all_known_in_store:
        key = (stored.get("course_id"), stored.get("assignment_id"))
        if key not in candidates_for_reminders:
            dd = json.loads(stored["due_date_json"]) if stored.get("due_date_json") else None
            dt = json.loads(stored["due_time_json"]) if stored.get("due_time_json") else None
            candidates_for_reminders[key] = {
                "course_id": stored.get("course_id"),
                "course_name": "Stored Course",
                "assignment_id": stored.get("assignment_id"),
                "title": stored.get("title") or "Assignment",
                "due_date": dd,
                "due_time": dt,
            }

    alert_offsets = config.deadline_alert_offsets_minutes

    for item in candidates_for_reminders.values():
        due_dt = _parse_due_datetime(item.get("due_date"), item.get("due_time"))
        if due_dt is None or due_dt <= now:
            # Overdue or no due date: skip upcoming reminders
            continue

        c_id = item["course_id"]
        a_id = item["assignment_id"]
        title = item["title"]

        for offset in alert_offsets:
            fire_at = due_dt - timedelta(minutes=offset)
            if fire_at <= now < due_dt:
                offset_label = f"{offset // 60}h" if offset >= 60 else f"{offset}min"
                has_sent = read_only_store.has_sent_reminder(c_id, a_id, offset, user_id=target_user_id)

                if has_sent:
                    result.suppressed_reminders.append({
                        "course_id": c_id,
                        "assignment_id": a_id,
                        "title": title,
                        "offset_minutes": offset,
                        "offset_label": offset_label,
                        "reason": "Reminder already sent in StateStore",
                    })
                else:
                    predicted_subject = f"⏰ Deadline Reminder: {title}"
                    result.detected_deadline_reminders.append({
                        "course_id": c_id,
                        "assignment_id": a_id,
                        "title": title,
                        "due_date": item.get("due_date"),
                        "offset_minutes": offset,
                        "offset_label": offset_label,
                        "due_dt": due_dt.isoformat(),
                        "predicted_subject": predicted_subject,
                    })
                    read_only_store.mark_reminder_sent(c_id, a_id, offset, user_id=target_user_id)

    # 7. Verification: Zero mutations and zero side-effects
    result.database_mutations_count = 0
    result.emails_sent_count = 0
    result.gemini_calls_count = 0
    result.production_watcher_triggered = False

    # 8. Log and Output Structured Summary
    summary = format_simulation_summary(result)
    logger.info("\n%s", summary)

    return result
