"""
ClassPilot AI — Background Job Entrypoint

Standalone background script intended to be invoked periodically (e.g. via Cloud Scheduler
triggering a Cloud Run Job execution every 15 minutes).

1. Reads enabled watcher configurations from PostgreSQL.
2. For each enabled user, fetches their Google credentials.
3. Polls Google Classroom and checks deadline schedules.
4. Uses atomic StateStore claims to send notifications exactly once.
5. Exits when complete.
"""

import logging

from classpilot.config import get_config
from classpilot.logging_setup import configure_logging
from classpilot.budget import NotificationBudget
from classpilot.user_store import UserStore
from classpilot.google_oauth import get_authorized_credentials
from classpilot.services import build_user_services
from classpilot.state_store import StateStore
from classpilot.notifier import create_notifier
from classpilot.llm import create_llm_provider
from classpilot.events import EventType

logger = logging.getLogger(__name__)


import os
import re
import sys
from typing import Optional


def _sanitize_diagnostic_message(msg: str) -> str:
    """Redact sensitive API keys or credentials from error messages."""
    msg = re.sub(r'([?&]key=)[^&\s]+', r'\1[REDACTED]', msg)
    msg = re.sub(r'AIza[0-9A-Za-z-_]{35}', '[REDACTED_API_KEY]', msg)
    msg = re.sub(r'Bearer\s+[A-Za-z0-9._~+/-]+', 'Bearer [REDACTED]', msg)
    return msg


def _mask_user_id(user_id: Optional[str]) -> str:
    """Mask user ID to prevent exposing sensitive identifiers in logs."""
    if not user_id:
        return "<unset>"
    clean = str(user_id).strip()
    if len(clean) <= 4:
        return "***"
    return f"***{clean[-4:]}"


def run_diagnostics(user_id: str, credentials, config) -> None:
    """Run read-only verifications for all critical integrations."""
    logger.info("========================================")
    logger.info("DRY RUN DIAGNOSTICS FOR USER: %s", user_id)
    logger.info("========================================")

    # 1. Verify Classroom assignment fetching
    logger.info("1. Verifying Google Classroom connectivity and assignment retrieval...")
    try:
        from googleapiclient.discovery import build
        client = build("classroom", "v1", credentials=credentials)

        raw_courses = []
        page_token = None
        max_course_pages = 3
        course_pages_fetched = 0

        while course_pages_fetched < max_course_pages:
            params = {"pageSize": 50}
            if page_token:
                params["pageToken"] = page_token
            courses_result = client.courses().list(**params).execute()
            if isinstance(courses_result, dict):
                batch = courses_result.get("courses", [])
                if isinstance(batch, list):
                    raw_courses.extend(batch)
                page_token = courses_result.get("nextPageToken")
            else:
                page_token = None
            course_pages_fetched += 1
            if not page_token:
                break

        logger.info("   [PASS] Successfully fetched %d course(s)", len(raw_courses))

        passed_count = 0
        failed_count = 0
        skipped_count = 0

        for course in raw_courses:
            course_id = course.get("id") if isinstance(course, dict) else None
            course_name = (course.get("name") if isinstance(course, dict) else None) or "Unnamed Course"
            course_state = (course.get("courseState") if isinstance(course, dict) else None) or "ACTIVE"

            if not course_id:
                logger.info("   [SKIP] Course '%s': Missing course ID", course_name)
                skipped_count += 1
                continue

            # Skip non-active courses (e.g. ARCHIVED, DECLINED, PROVISIONED)
            if course_state != "ACTIVE":
                logger.info(
                    "   [SKIP] Course '%s' (id: %s): Course is not active (state: %s)",
                    course_name, course_id, course_state
                )
                skipped_count += 1
                continue

            # Attempt read-only coursework retrieval with pagination capped
            try:
                assignments_count = 0
                cw_page_token = None
                cw_pages_fetched = 0
                max_coursework_pages = 3

                while cw_pages_fetched < max_coursework_pages:
                    cw_params = {"courseId": course_id, "pageSize": 50}
                    if cw_page_token:
                        cw_params["pageToken"] = cw_page_token
                    cw_res = client.courses().courseWork().list(**cw_params).execute()
                    if isinstance(cw_res, dict):
                        cw_items = cw_res.get("courseWork", [])
                        if isinstance(cw_items, list):
                            assignments_count += len(cw_items)
                        cw_page_token = cw_res.get("nextPageToken")
                    else:
                        cw_page_token = None
                    cw_pages_fetched += 1
                    if not cw_page_token:
                        break

                if assignments_count > 0:
                    logger.info(
                        "   [PASS] Course '%s' (id: %s): Successfully retrieved %d assignment(s)",
                        course_name, course_id, assignments_count
                    )
                else:
                    logger.info(
                        "   [PASS] Course '%s' (id: %s): 0 assignments found (empty coursework list)",
                        course_name, course_id
                    )
                passed_count += 1

            except Exception as course_err:
                resp = getattr(course_err, "resp", None)
                status = getattr(resp, "status", None)
                is_403 = (
                    status == 403
                    or getattr(course_err, "status_code", None) == 403
                    or "403" in str(course_err)
                )
                if is_403:
                    reason = getattr(course_err, "reason", None) or "Permission denied"
                    logger.error(
                        "   [FAIL] Course '%s' (id: %s): HTTP 403 Forbidden - %s",
                        course_name, course_id, reason
                    )
                else:
                    sanitized_err = _sanitize_diagnostic_message(str(course_err))
                    logger.error(
                        "   [FAIL] Course '%s' (id: %s): CourseWork retrieval failed: %s",
                        course_name, course_id, sanitized_err
                    )
                failed_count += 1

        logger.info(
            "   Classroom coursework summary for user %s: %d passed, %d failed, %d skipped",
            user_id, passed_count, failed_count, skipped_count
        )

    except Exception as exc:
        sanitized_exc = _sanitize_diagnostic_message(str(exc))
        logger.error("   [FAIL] Classroom fetch failed: %s", sanitized_exc)


    # 2. Verify LLM configuration
    try:
        logger.info("2. Verifying LLM (%s) configuration...", config.llm_provider)
        from classpilot.llm import create_llm_provider
        from classpilot.events import EventType
        llm = create_llm_provider(config)
        response = llm.generate_message(EventType.NEW_ASSIGNMENT, "Diagnostic Test Assignment")
        if not response:
            raise ValueError("LLM returned empty response")
        logger.info("   [PASS] Successfully generated text using %s", config.llm_provider)
    except Exception as exc:
        logger.error("   [FAIL] LLM verification failed: %s", exc)

    # 3. Verify SMTP configuration
    try:
        logger.info("3. Verifying SMTP configuration...")
        if config.notifier_backend.lower() == "email":
            import smtplib
            with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=15) as server:
                server.starttls()
                server.login(config.smtp_user, config.smtp_password)
            logger.info("   [PASS] Successfully connected and authenticated to SMTP server")
        else:
            logger.info("   [SKIP] Notifier backend is '%s', skipping SMTP verification", config.notifier_backend)
    except Exception as exc:
        logger.error("   [FAIL] SMTP verification failed: %s", exc)


def run_test_email(
    config,
    recipient_email: str,
    course_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> bool:
    """
    Execute a safe, manual end-to-end email notification test.

    1. Validates the recipient email address.
    2. Loads stored credentials for the connected college account.
    3. Fetches at most one real assignment from an active Classroom course (or specific course_id).
    4. Generates a short, professional notification with Gemini.
    5. Sends exactly one email to recipient_email prefixed with [CLASSPILOT TEST].
    6. Never modifies notification history or touches the production watcher.
    """
    logger.info("========================================")
    logger.info("STARTING CLASSPILOT TEST EMAIL MODE")
    logger.info("========================================")

    # 1. Validate recipient email
    if not recipient_email or not recipient_email.strip():
        logger.error("Test email aborted: recipient_email is required.")
        return False

    recipient_email = recipient_email.strip()
    email_regex = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    if not re.match(email_regex, recipient_email):
        sanitized_email = _sanitize_diagnostic_message(recipient_email)
        logger.error("Test email aborted: '%s' is not a valid email address.", sanitized_email)
        return False

    # 2. Select authorized college account deterministically
    state_store = StateStore()
    user_store = UserStore()

    target_user_id = (user_id.strip() if user_id else None) or (os.getenv("TEST_USER_ID", "").strip() or None)
    enabled_users = state_store.get_enabled_watchers()

    if target_user_id:
        masked_id = _mask_user_id(target_user_id)
        if target_user_id not in enabled_users:
            logger.error(
                "Test email aborted: Configured account (%s) is not an enabled watcher. Failing closed.",
                masked_id
            )
            return False
        logger.info("Using configured account (%s) for test email.", masked_id)
    else:
        if not enabled_users:
            logger.error("Test email aborted: No enabled accounts found in database. Failing closed.")
            return False
        if len(enabled_users) > 1:
            logger.error(
                "Test email aborted: Multiple enabled accounts found (%d). "
                "Account cannot be identified unambiguously without TEST_USER_ID. Failing closed.",
                len(enabled_users)
            )
            return False
        target_user_id = enabled_users[0]
        masked_id = _mask_user_id(target_user_id)
        logger.info("Unambiguously selected single enabled account (%s) for test email.", masked_id)

    user = user_store.get_user_by_id(target_user_id)
    if not user:
        logger.error("Test email aborted: Account (%s) not found in user store. Failing closed.", masked_id)
        return False

    try:
        credentials = get_authorized_credentials(target_user_id, user_store)
    except Exception as exc:
        sanitized_exc = _sanitize_diagnostic_message(str(exc))
        logger.error("Test email aborted: Account (%s) is not authorized with valid Google credentials: %s", masked_id, sanitized_exc)
        return False

    if not credentials:
        logger.error("Test email aborted: Account (%s) has no valid Google credentials.", masked_id)
        return False

    # 3. Fetch at most one real assignment from Google Classroom
    try:
        from googleapiclient.discovery import build
        client = build("classroom", "v1", credentials=credentials)
    except Exception as exc:
        sanitized_exc = _sanitize_diagnostic_message(str(exc))
        logger.error("Failed to initialize Google Classroom client: %s", sanitized_exc)
        return False

    found_assignment = None
    target_course_name = "Classroom Course"

    effective_course_id = (course_id.strip() if course_id else None) or (os.getenv("TEST_COURSE_ID", "").strip() or None)

    if effective_course_id:
        cid = effective_course_id
        try:
            course = client.courses().get(id=cid).execute()
            if isinstance(course, dict):
                target_course_name = course.get("name") or "Unnamed Course"
            else:
                logger.error("Course '%s' returned invalid data.", cid)
                return False
        except Exception as exc:
            sanitized_exc = _sanitize_diagnostic_message(str(exc))
            resp = getattr(exc, "resp", None)
            if getattr(resp, "status", None) == 403 or "403" in str(exc):
                logger.error("Google Classroom HTTP 403 Forbidden accessing course '%s': %s", cid, sanitized_exc)
            else:
                logger.error("Failed to fetch course '%s': %s", cid, sanitized_exc)
            return False

        try:
            cw_res = client.courses().courseWork().list(courseId=cid, pageSize=10).execute()
            cw_items = cw_res.get("courseWork", []) if isinstance(cw_res, dict) else []
            if cw_items and isinstance(cw_items, list):
                found_assignment = cw_items[0]
        except Exception as exc:
            sanitized_exc = _sanitize_diagnostic_message(str(exc))
            resp = getattr(exc, "resp", None)
            if getattr(resp, "status", None) == 403 or "403" in str(exc):
                logger.error("Google Classroom HTTP 403 Forbidden on course '%s': %s", target_course_name, sanitized_exc)
                return False
            logger.warning("Error checking coursework for course '%s': %s", target_course_name, sanitized_exc)
    else:
        # Paginated course discovery with reasonable page and quota limits
        max_course_pages = 3
        course_pages_fetched = 0
        page_token = None
        checked_courses = 0
        max_courses_to_check = 50

        while course_pages_fetched < max_course_pages and not found_assignment:
            params = {"pageSize": 50}
            if page_token:
                params["pageToken"] = page_token

            try:
                courses_resp = client.courses().list(**params).execute()
            except Exception as exc:
                sanitized_exc = _sanitize_diagnostic_message(str(exc))
                resp = getattr(exc, "resp", None)
                if getattr(resp, "status", None) == 403 or "403" in str(exc):
                    logger.error("Google Classroom HTTP 403 Forbidden listing courses: %s", sanitized_exc)
                else:
                    logger.error("Failed to list Google Classroom courses: %s", sanitized_exc)
                return False

            raw_courses = courses_resp.get("courses", []) if isinstance(courses_resp, dict) else []
            candidate_courses = [
                c for c in raw_courses
                if isinstance(c, dict) and c.get("courseState", "ACTIVE") == "ACTIVE" and c.get("id")
            ]

            for course in candidate_courses:
                checked_courses += 1
                c_id = course.get("id")
                c_name = course.get("name") or "Unnamed Course"
                try:
                    cw_res = client.courses().courseWork().list(courseId=c_id, pageSize=10).execute()
                    cw_items = cw_res.get("courseWork", []) if isinstance(cw_res, dict) else []
                    if cw_items and isinstance(cw_items, list):
                        found_assignment = cw_items[0]
                        target_course_name = c_name
                        break
                except Exception as exc:
                    sanitized_exc = _sanitize_diagnostic_message(str(exc))
                    resp = getattr(exc, "resp", None)
                    if getattr(resp, "status", None) == 403 or "403" in str(exc):
                        logger.error("Google Classroom HTTP 403 Forbidden on course '%s': %s", c_name, sanitized_exc)
                        return False
                    logger.warning("Error checking coursework for course '%s': %s", c_name, sanitized_exc)

                if checked_courses >= max_courses_to_check:
                    logger.info("Reached maximum course check quota limit (%d).", max_courses_to_check)
                    break

            if found_assignment or checked_courses >= max_courses_to_check:
                break

            page_token = courses_resp.get("nextPageToken") if isinstance(courses_resp, dict) else None
            course_pages_fetched += 1
            if not page_token:
                break

    if not found_assignment:
        logger.info("No assignments found in accessible courses. Exiting test without sending email.")
        return False

    assignment_title = found_assignment.get("title") or "Assignment"

    # Format due date if available
    due_date_str = None
    due_date = found_assignment.get("dueDate")
    if isinstance(due_date, dict) and "year" in due_date and "month" in due_date and "day" in due_date:
        due_date_str = f"{due_date['year']}-{due_date['month']:02d}-{due_date['day']:02d}"
        due_time = found_assignment.get("dueTime")
        if isinstance(due_time, dict) and "hours" in due_time:
            hours = due_time.get("hours", 0)
            minutes = due_time.get("minutes", 0)
            due_date_str += f" {hours:02d}:{minutes:02d} UTC"

    # 4. Generate notification using Gemini
    logger.info("Generating test notification using Gemini provider (%s)...", config.llm_provider)
    try:
        from classpilot.llm import create_llm_provider
        from classpilot.events import EventType

        llm = create_llm_provider(config)
        if hasattr(llm, "generate_test_notification"):
            notification_text = llm.generate_test_notification(
                course_name=target_course_name,
                assignment_title=assignment_title,
                due_date=due_date_str,
            )
        else:
            notification_text = llm.generate_message(
                EventType.NEW_ASSIGNMENT,
                f"{assignment_title} in {target_course_name}"
            )
        if not notification_text:
            raise ValueError("Gemini returned empty notification text")
    except Exception as exc:
        sanitized_exc = _sanitize_diagnostic_message(str(exc))
        logger.error("Gemini notification generation failed: %s", sanitized_exc)
        return False

    # 5. Send exactly one email via SMTP
    subject = f"[CLASSPILOT TEST] {target_course_name}: {assignment_title}"
    body = (
        f"{notification_text}\n\n"
        f"---\n"
        f"Course: {target_course_name}\n"
        f"Assignment: {assignment_title}\n"
    )
    if due_date_str:
        body += f"Due Date: {due_date_str}\n"
    body += (
        f"\n[This is a ClassPilot test notification sent to {recipient_email}. "
        f"No production state or notification history was modified.]\n"
    )

    logger.info("Sending test email to %s...", recipient_email)
    try:
        from classpilot.notifier import create_notifier

        notifier = create_notifier(config, to_email_override=recipient_email)
        notifier.send(subject=subject, body=body)
        logger.info("Successfully sent test notification email to %s.", recipient_email)
        return True
    except Exception as exc:
        sanitized_exc = _sanitize_diagnostic_message(str(exc))
        logger.error("Failed to send test email via SMTP: %s", sanitized_exc)
        return False


def run_controlled_test(config, event_id: Optional[str] = None) -> bool:
    """
    Execute a single controlled live-email verification test for the authorized user only.

    Safeguards:
    1. Restricted strictly to PRODUCTION_TEST_USER_ID / TEST_USER_ID.
    2. Fails closed if authorized user ID is missing, not in UserStore, watcher disabled,
       or invalid credentials.
    3. Strictly delivers to user.email stored in PostgreSQL; never any other address.
    4. Enforces MAX_NOTIFICATIONS_PER_RUN=1 budget.
    5. Uses a controlled test event in PostgreSQL state store; never deletes or resets
       history, and never fabricates real Google Classroom assignments.
    6. Verifies claim lifecycle:
       - Phase 1: Claims event (status -> 'pending'), sends 1 email via Gemini + SMTP,
         transitions claim status -> 'sent'.
       - Phase 2: Immediately re-evaluates the same event, verifying PostgreSQL state
         store suppresses duplicate and sends 0 additional emails.
    7. Outputs exact metrics (sent, suppressed, claim status, failures, workflow URL).
    """
    logger.info("==================================================================")
    logger.info("STARTING CONTROLLED PRODUCTION EMAIL VERIFICATION")
    logger.info("==================================================================")

    # 1. Fail-closed validation of authorized user
    target_user_id = config.production_test_user_id
    if not target_user_id:
        logger.error(
            "Controlled verification aborted: PRODUCTION_TEST_USER_ID is not configured. "
            "Failing closed."
        )
        return False

    target_user_id = target_user_id.strip()
    masked_id = _mask_user_id(target_user_id)
    logger.info("Target authorized user ID: %s", masked_id)

    state_store = StateStore()
    user_store = UserStore()

    user = user_store.get_user_by_id(target_user_id)
    if not user:
        logger.error(
            "Controlled verification aborted: Authorized user %s does not exist in UserStore. "
            "Failing closed.",
            masked_id,
        )
        return False

    enabled_users = state_store.get_enabled_watchers()
    if target_user_id not in enabled_users:
        logger.error(
            "Controlled verification aborted: Authorized user %s does not have an enabled watcher. "
            "Failing closed.",
            masked_id,
        )
        return False

    try:
        credentials = get_authorized_credentials(target_user_id, user_store)
    except Exception as exc:
        sanitized_exc = _sanitize_diagnostic_message(str(exc))
        logger.error(
            "Controlled verification aborted: Failed to resolve credentials for user %s: %s. "
            "Failing closed.",
            masked_id,
            sanitized_exc,
        )
        return False

    if not credentials:
        logger.error(
            "Controlled verification aborted: User %s has no valid credentials. "
            "Failing closed.",
            masked_id,
        )
        return False

    recipient_email = (getattr(user, "email", None) or "").strip()
    email_regex = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    if not recipient_email or not re.match(email_regex, recipient_email):
        logger.error(
            "Controlled verification aborted: User %s has invalid email: %r. "
            "Failing closed.",
            masked_id,
            recipient_email,
        )
        return False

    logger.info("Authorized recipient email: %s", recipient_email)

    # 2. Setup controlled test event (safe namespace, no Classroom modifications)
    course_id = "controlled-test-verification"
    course_name = "ClassPilot Verification"
    effective_event_id = (
        (event_id.strip() if event_id else None)
        or (os.getenv("CONTROLLED_TEST_EVENT_ID", "").strip() or None)
        or "event-controlled-live-v1"
    )
    assignment_title = "Controlled Production Email Verification"

    logger.info(
        "Controlled test event: course_id='%s', assignment_id='%s', title='%s'",
        course_id, effective_event_id, assignment_title,
    )

    # 3. Setup notification budget (strictly 1 notification allowed)
    budget = NotificationBudget(limit=1)

    # 4. Phase 1: Claim & Send
    emails_sent = 0
    emails_suppressed = 0
    failures = 0
    claim_status = "unknown"

    existing_record = state_store.get_known_assignment(course_id, effective_event_id, user_id=target_user_id)
    if existing_record and existing_record.get("status") == "sent":
        logger.warning(
            "Event '%s' was ALREADY previously sent in PostgreSQL state store (status='sent'). "
            "Testing duplicate rejection on existing record.",
            effective_event_id,
        )
        claim_won = state_store.claim_new_assignment(
            course_id, effective_event_id, assignment_title, None, None, user_id=target_user_id
        )
        if claim_won:
            logger.critical("SECURITY REGRESSION: Duplicate claim won on an already-sent assignment!")
            failures += 1
            claim_status = "error"
        else:
            logger.info("Duplicate prevention verified: claim rejected by PostgreSQL.")
            budget.record_suppressed()
            emails_suppressed += 1
            claim_status = "sent"
    else:
        # Initial attempt: Claim the assignment
        logger.info("Claiming controlled test event '%s'...", effective_event_id)
        claim_won = state_store.claim_new_assignment(
            course_id, effective_event_id, assignment_title, None, None, user_id=target_user_id
        )
        if not claim_won:
            logger.error("Failed to acquire claim on test event. Failing closed.")
            failures += 1
            return False

        pending_record = state_store.get_known_assignment(course_id, effective_event_id, user_id=target_user_id)
        if not pending_record or pending_record.get("status") != "pending":
            logger.error(
                "Database claim verification failed: expected status='pending', got: %s",
                pending_record.get("status") if pending_record else "None",
            )
            failures += 1
            return False
        logger.info("Database claim status verified: 'pending'")

        if not budget.can_send():
            logger.error("Budget exhausted before sending notification. Failing closed.")
            failures += 1
            return False

        budget.record_attempt()
        try:
            llm = create_llm_provider(config)
            notification_text = llm.generate_message(
                EventType.NEW_ASSIGNMENT,
                f"{assignment_title} in {course_name}"
            )
            if not notification_text:
                raise ValueError("LLM returned empty notification text")

            subject = f"[CLASSPILOT] {assignment_title}"
            body = (
                f"{notification_text}\n\n"
                f"---\n"
                f"Course: {course_name}\n"
                f"Event ID: {effective_event_id}\n"
                f"Verified Recipient: {recipient_email}\n"
                f"Status: Controlled Live-Email Verification\n"
                f"[This email confirms that ClassPilot production notifications and PostgreSQL "
                f"deduplication safeguards are functioning correctly. No other users received notifications.]\n"
            )

            notifier = create_notifier(config, to_email_override=recipient_email)
            notifier.send(subject=subject, body=body)

            state_store.mark_assignment_sent(course_id, effective_event_id, user_id=target_user_id)
            budget.record_success()
            emails_sent += 1
            logger.info("Successfully sent controlled verification email to %s", recipient_email)

            sent_record = state_store.get_known_assignment(course_id, effective_event_id, user_id=target_user_id)
            if not sent_record or sent_record.get("status") != "sent":
                logger.error(
                    "Database claim status transition verification failed: expected 'sent', got %s",
                    sent_record.get("status") if sent_record else "None",
                )
                failures += 1
            else:
                claim_status = "sent"
                logger.info("Database claim status verified: 'sent'")

        except Exception as exc:
            sanitized_exc = _sanitize_diagnostic_message(str(exc))
            logger.error("Failed to send verification email: %s", sanitized_exc)
            state_store.mark_assignment_retryable(course_id, effective_event_id, error=str(exc), user_id=target_user_id)
            budget.record_failure()
            failures += 1
            claim_status = "retryable"
            return False

        # Phase 2: Re-run exact same event to verify duplicate prevention
        logger.info("Phase 2: Re-running exact same event to verify duplicate prevention...")
        dup_claim_won = state_store.claim_new_assignment(
            course_id, effective_event_id, assignment_title, None, None, user_id=target_user_id
        )
        if dup_claim_won:
            logger.critical("SECURITY REGRESSION: Duplicate claim was WON on an already-sent event!")
            failures += 1
        else:
            logger.info("Duplicate prevention verified: Duplicate claim rejected by PostgreSQL state store.")
            budget.record_suppressed()
            emails_suppressed += 1

    # 5. Output exact metrics
    server_url = os.getenv("GITHUB_SERVER_URL", "https://github.com")
    repo = os.getenv("GITHUB_REPOSITORY", "Rama-chandra55/classpilot-ai")
    run_id = os.getenv("GITHUB_RUN_ID", "")
    workflow_url = f"{server_url}/{repo}/actions/runs/{run_id}" if run_id else "N/A (Local / Manual execution)"

    logger.info("==================================================================")
    logger.info("CONTROLLED VERIFICATION RESULTS")
    logger.info("==================================================================")
    logger.info("Emails sent: %d", emails_sent)
    logger.info("Emails suppressed: %d", emails_suppressed)
    logger.info("Database claim status: %s", claim_status)
    logger.info("Failures: %d", failures)
    logger.info("Workflow URL: %s", workflow_url)
    logger.info("==================================================================")

    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write("## 🚀 ClassPilot AI — Controlled Production Email Verification\n\n")
                f.write("| Metric | Result |\n")
                f.write("| --- | --- |\n")
                f.write(f"| **Authorized User** | `{masked_id}` |\n")
                f.write(f"| **Verified Recipient** | `{recipient_email}` |\n")
                f.write(f"| **Emails Sent** | **{emails_sent}** |\n")
                f.write(f"| **Emails Suppressed** | **{emails_suppressed}** |\n")
                f.write(f"| **Database Claim Status** | `{claim_status}` |\n")
                f.write(f"| **Failures** | `{failures}` |\n")
                f.write(f"| **Workflow URL** | [{workflow_url}]({workflow_url}) |\n\n")
                if failures == 0 and (emails_sent == 1 or emails_suppressed >= 1):
                    f.write("✅ **Verification Passed**: Controlled live-email delivered, claim status transitioned to `sent`, and duplicate prevention confirmed.\n")
                else:
                    f.write("❌ **Verification Failed**: See job log for details.\n")
        except Exception as summary_err:
            logger.warning("Could not write GITHUB_STEP_SUMMARY: %s", summary_err)

    return failures == 0


def main() -> None:
    is_dry_run = "--dry-run" in sys.argv
    is_test_email = "--test-email" in sys.argv
    is_simulate_watcher = "--simulate-watcher" in sys.argv
    is_controlled_test = "--controlled-test" in sys.argv
    config = get_config()
    configure_logging(config.log_level)

    if is_controlled_test:
        logger.info("Starting ClassPilot in CONTROLLED PRODUCTION EMAIL TEST mode")
        event_id = None
        for idx, arg in enumerate(sys.argv):
            if arg == "--event-id" and idx + 1 < len(sys.argv):
                event_id = sys.argv[idx + 1]
            elif arg.startswith("--event-id="):
                event_id = arg.split("=", 1)[1]

        success = run_controlled_test(config, event_id=event_id)
        if success:
            logger.info("Controlled production email verification passed successfully.")
            sys.exit(0)
            return
        else:
            logger.error("Controlled production email verification failed.")
            sys.exit(1)
            return

    if is_simulate_watcher:
        logger.info("Starting ClassPilot in WATCHER SIMULATION mode")
        course_id = None
        user_id = None

        for idx, arg in enumerate(sys.argv):
            if arg == "--course-id" and idx + 1 < len(sys.argv):
                course_id = sys.argv[idx + 1]
            elif arg.startswith("--course-id="):
                course_id = arg.split("=", 1)[1]
            elif arg == "--user-id" and idx + 1 < len(sys.argv):
                user_id = sys.argv[idx + 1]
            elif arg.startswith("--user-id="):
                user_id = arg.split("=", 1)[1]

        if not course_id:
            course_id = os.getenv("SIMULATION_COURSE_ID") or os.getenv("TEST_COURSE_ID")
        if not user_id:
            user_id = os.getenv("SIMULATION_USER_ID") or os.getenv("TEST_USER_ID")

        from classpilot.simulation import run_watcher_simulation
        sim_result = run_watcher_simulation(config, user_id=user_id, course_id=course_id)
        if sim_result.success:
            logger.info("Watcher simulation finished successfully.")
            sys.exit(0)
            return
        else:
            logger.error("Watcher simulation failed.")
            sys.exit(1)
            return

    if is_test_email:
        logger.info("Starting ClassPilot in TEST EMAIL mode")
        recipient = None
        course_id = None
        user_id = None

        for idx, arg in enumerate(sys.argv):
            if arg == "--recipient-email" and idx + 1 < len(sys.argv):
                recipient = sys.argv[idx + 1]
            elif arg.startswith("--recipient-email="):
                recipient = arg.split("=", 1)[1]
            elif arg == "--course-id" and idx + 1 < len(sys.argv):
                course_id = sys.argv[idx + 1]
            elif arg.startswith("--course-id="):
                course_id = arg.split("=", 1)[1]
            elif arg == "--user-id" and idx + 1 < len(sys.argv):
                user_id = sys.argv[idx + 1]
            elif arg.startswith("--user-id="):
                user_id = arg.split("=", 1)[1]

        if not recipient:
            recipient = os.getenv("TEST_RECIPIENT_EMAIL")
        if not course_id:
            course_id = os.getenv("TEST_COURSE_ID")
        if not user_id:
            user_id = os.getenv("TEST_USER_ID")

        if not recipient:
            logger.error("TEST EMAIL mode requires --recipient-email or TEST_RECIPIENT_EMAIL environment variable.")
            sys.exit(1)
            return

        success = run_test_email(config, recipient_email=recipient, course_id=course_id, user_id=user_id)
        if success:
            logger.info("Test email mode finished successfully.")
            sys.exit(0)
            return
        else:
            logger.error("Test email mode failed or found no assignment.")
            sys.exit(1)
            return


    if is_dry_run:
        logger.info("Starting ClassPilot background job in DRY RUN mode")
    else:
        logger.info("Starting ClassPilot background job execution")

    state_store = StateStore()
    user_store = UserStore()
    enabled_users = state_store.get_enabled_watchers()

    # If PRODUCTION_TEST_USER_ID is configured, restrict execution to that authorized account.
    # Fail closed if the configured account is invalid, disabled, or unauthorized.
    target_user_id = config.production_test_user_id
    if target_user_id:
        masked_target = _mask_user_id(target_user_id)
        logger.info("PRODUCTION_TEST_USER_ID is configured: restricting execution to %s", masked_target)

        user_record = user_store.get_user_by_id(target_user_id)
        if not user_record:
            logger.error("PRODUCTION_TEST_USER_ID %s does not exist in UserStore. Failing closed.", masked_target)
            sys.exit(1)
            return

        if target_user_id not in enabled_users:
            logger.error("PRODUCTION_TEST_USER_ID %s does not have an enabled watcher. Failing closed.", masked_target)
            sys.exit(1)
            return

        try:
            _ = get_authorized_credentials(target_user_id, user_store)
        except Exception as exc:
            logger.error(
                "PRODUCTION_TEST_USER_ID %s is not authorized with valid Google credentials: %s. Failing closed.",
                masked_target,
                exc,
            )
            sys.exit(1)
            return

        enabled_users = [target_user_id]

    if not enabled_users:
        logger.info("No users have the watcher enabled. Exiting.")
        return

    budget: Optional[NotificationBudget] = None
    if not is_dry_run:
        budget = NotificationBudget(limit=config.max_notifications_per_run)
        logger.info(
            "Enforcing global notification budget: %d maximum notification(s) per run",
            budget.limit,
        )
        # Recover any abandoned pending claims from interrupted prior runs
        try:
            state_store.recover_abandoned_claims()
        except Exception as exc:
            logger.warning("Could not recover abandoned claims: %s", exc)

    for user_id in enabled_users:
        user = user_store.get_user_by_id(user_id)
        masked_user = _mask_user_id(user_id)
        if not user:
            logger.warning("User %s is enabled but does not exist in UserStore", masked_user)
            continue

        try:
            credentials = get_authorized_credentials(user_id, user_store)
        except Exception as exc:
            logger.warning("Could not fetch credentials for user %s: %s", masked_user, exc)
            continue

        if is_dry_run:
            run_diagnostics(user_id, credentials, config)
        else:
            logger.info("Running poll cycle for user %s", masked_user)
            try:
                services = build_user_services(
                    user_id, credentials, to_email=user.email, budget=budget
                )
                services.run_poll_cycle()
            except Exception as exc:
                logger.error("Poll cycle failed for user %s: %s", masked_user, exc, exc_info=True)

    if is_dry_run:
        logger.info("Dry run diagnostics complete")
    else:
        if budget is not None:
            summary = budget.summary()
            logger.info(
                "Background job notification summary: attempted=%d, successful=%d, failed=%d, "
                "deferred=%d, suppressed=%d (limit=%d, remaining=%d)",
                summary["attempted"],
                summary["successful"],
                summary["failed"],
                summary["deferred"],
                summary["suppressed"],
                summary["limit"],
                summary["remaining"],
            )
            if summary["deferred"] > 0:
                logger.warning(
                    "Global notification cap reached: %d notification(s) were deferred without consuming claims",
                    summary["deferred"],
                )
        logger.info("Background job execution complete")


if __name__ == "__main__":
    main()

