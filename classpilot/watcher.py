"""
Feature 1: Assignment Watcher

Polls Google Classroom on a fixed interval, detects newly created assignments
and changed deadlines, and emits events for downstream handling (AI
notifications in Feature 2/3). Duplicate detection is delegated to
StateStore, so restarts never cause repeat notifications.

Phase 3 — user-scoped, credentials-based:
  AssignmentWatcher is now constructed with an explicit `credentials`
  object and `user_id` — there is no longer a "the" watcher; each
  instance belongs to exactly one identified user and only ever polls
  Classroom on their behalf. Classroom calls go through study_client.py
  (which builds its own per-call Google API client from `credentials`,
  never the vendor's global token.json singleton) instead of the
  vendor `classroom_suite_mcp.classroom` module directly — this also
  removes a second, duplicate normalization/pagination code path that
  used to exist here. StateStore calls now pass `user_id` explicitly, so
  a shared course+assignment (two students in the same class watching
  the same coursework) can never overwrite one user's dedup state with
  another's — see state_store.py's PRIMARY KEY widening for why that
  used to be possible.

  The legacy, explicitly-opt-in token.json standalone path (see
  services.py/main.py) constructs an AssignmentWatcher the same way,
  just with `credentials = classroom_suite_mcp.auth.get_auth().credentials`
  and `user_id = state_store.DEFAULT_USER_ID` — same interface, different
  (legacy) credential source, never a silent fallback from within this
  class itself.
"""

import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, List, Optional

from . import study_client
from .budget import NotificationBudget
from .state_store import StateStore, DEFAULT_USER_ID

from enum import Enum

logger = logging.getLogger(__name__)


def is_historical_assignment(assignment: dict, now: Optional[datetime] = None) -> bool:
    """
    Check if an assignment is historical (deadline has already passed or creation is aged).
    Historical assignments should not trigger 'new assignment' notifications
    on first startup or during regular polling.
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)

    due_date = assignment.get("due_date") or assignment.get("dueDate")
    due_time = assignment.get("due_time") or assignment.get("dueTime")
    if isinstance(due_date, dict):
        from .deadline_scheduler import _parse_due_datetime
        due_dt = _parse_due_datetime(due_date, due_time)
        if due_dt is not None and due_dt <= now:
            return True

    created_str = assignment.get("created") or assignment.get("creationTime")
    if created_str:
        try:
            clean_str = str(created_str).replace("Z", "+00:00")
            created_dt = datetime.fromisoformat(clean_str)
            if (now - created_dt).days > 14:
                return True
        except Exception:
            pass

    return False


class AssignmentClassification(str, Enum):
    """Possible outcomes of evaluating an assignment against watcher state."""
    NEW = "new"
    DEADLINE_UPDATED = "deadline_updated"
    HISTORICAL_BASELINE = "historical_baseline"
    UNCHANGED = "unchanged"


@dataclass
class ClassifiedAssignment:
    """Detailed decision resulting from classify_assignment."""
    classification: AssignmentClassification
    course_id: str
    course_name: str
    assignment_id: str
    title: str
    due_date: Optional[dict]
    due_time: Optional[dict]
    previous_due_date: Optional[dict] = None
    previous_due_time: Optional[dict] = None
    created: Optional[str] = None


def classify_assignment(
    assignment: dict,
    course_id: str,
    course_name: str,
    state_store: StateStore,
    user_id: str = DEFAULT_USER_ID,
    filter_historical: bool = True,
    now: Optional[datetime] = None,
) -> Optional[ClassifiedAssignment]:
    """
    Classify an assignment against the current state store.
    Shared notification decision logic between production AssignmentWatcher and simulation engine.
    """
    assignment_id = assignment.get("id")
    if not assignment_id:
        return None

    title = assignment.get("title", "Untitled assignment")
    due_date = assignment.get("due_date") or assignment.get("dueDate")
    due_time = assignment.get("due_time") or assignment.get("dueTime")
    created = assignment.get("created") or assignment.get("creationTime")

    known = state_store.get_known_assignment(course_id, assignment_id, user_id=user_id)

    if known is None:
        if filter_historical and is_historical_assignment(assignment, now=now):
            return ClassifiedAssignment(
                classification=AssignmentClassification.HISTORICAL_BASELINE,
                course_id=course_id,
                course_name=course_name,
                assignment_id=assignment_id,
                title=title,
                due_date=due_date,
                due_time=due_time,
                created=created,
            )
        return ClassifiedAssignment(
            classification=AssignmentClassification.NEW,
            course_id=course_id,
            course_name=course_name,
            assignment_id=assignment_id,
            title=title,
            due_date=due_date,
            due_time=due_time,
            created=created,
        )

    # If the assignment claim is in retryable state from a failed delivery attempt, retry it!
    if known.get("status") == "retryable":
        return ClassifiedAssignment(
            classification=AssignmentClassification.NEW,
            course_id=course_id,
            course_name=course_name,
            assignment_id=assignment_id,
            title=title,
            due_date=due_date,
            due_time=due_time,
            created=created,
        )

    # Already known - check whether the deadline changed or deadline notification is retryable.
    prev_due_date = json.loads(known["due_date_json"]) if known.get("due_date_json") else None
    prev_due_time = json.loads(known["due_time_json"]) if known.get("due_time_json") else None

    if known.get("deadline_status") == "retryable" or (prev_due_date != due_date or prev_due_time != due_time):
        return ClassifiedAssignment(
            classification=AssignmentClassification.DEADLINE_UPDATED,
            course_id=course_id,
            course_name=course_name,
            assignment_id=assignment_id,
            title=title,
            due_date=due_date,
            due_time=due_time,
            previous_due_date=prev_due_date,
            previous_due_time=prev_due_time,
            created=created,
        )

    return ClassifiedAssignment(
        classification=AssignmentClassification.UNCHANGED,
        course_id=course_id,
        course_name=course_name,
        assignment_id=assignment_id,
        title=title,
        due_date=due_date,
        due_time=due_time,
        previous_due_date=prev_due_date,
        previous_due_time=prev_due_time,
        created=created,
    )


@dataclass
class AssignmentEvent:
    """Represents a detected change worth notifying about."""
    kind: str  # "new" or "deadline_updated"
    course_id: str
    course_name: str
    assignment_id: str
    title: str
    due_date: Optional[dict]
    due_time: Optional[dict]
    previous_due_date: Optional[dict] = None
    previous_due_time: Optional[dict] = None


class AssignmentWatcher:
    """
    Checks all active courses for new or updated coursework and reports
    changes via a callback, while persisting what's already been seen.

    Belongs to exactly one user: `credentials` determines whose Classroom
    gets polled, and `user_id` determines whose dedup state is read/written.
    """

    def __init__(
        self,
        state_store: StateStore,
        on_event: Callable[[AssignmentEvent], Optional[bool]],
        credentials,
        user_id: str = DEFAULT_USER_ID,
        filter_historical: bool = True,
        now: Optional[datetime] = None,
        budget: Optional[NotificationBudget] = None,
    ):
        """
        Args:
            state_store: StateStore used to dedupe across polls/restarts.
            on_event: Callback invoked once per detected AssignmentEvent.
                      Returns True if delivered, False if failed, or None.
            credentials: google.oauth2.credentials.Credentials for the
                         user this watcher polls Classroom on behalf of.
            user_id: identifies whose dedup state to read/write in
                     state_store — must correspond to the same user as
                     `credentials`.
            filter_historical: if True, suppresses 'new' events for historical assignments
                               (e.g. past due dates) on first startup or subsequent polls.
            now: reference datetime for historical checking (defaults to utcnow).
            budget: optional NotificationBudget enforcing global notification limit.
        """
        self._state = state_store
        self._on_event = on_event
        self._credentials = credentials
        self._user_id = user_id
        self._filter_historical = filter_historical
        self._now = now
        self._budget = budget

    @property
    def _study_client(self):
        """
        Resolve the active study_client module, seamlessly supporting tests
        that patch either 'classpilot.watcher.study_client' or 'classpilot.study_client'
        across module reloads.
        """
        active = sys.modules.get("classpilot.study_client")
        if active is not None and active is not study_client:
            # If the active module has been mocked or patched, prefer it
            if hasattr(getattr(active, "fetch_courses", None), "assert_called") or getattr(getattr(active, "fetch_courses", None), "_mock_return_value", None) is not None:
                return active
        return study_client

    def check_once(self) -> List[AssignmentEvent]:
        """
        Run a single poll cycle across all active courses.

        Returns:
            The list of events detected during this cycle (also dispatched
            to on_event as they're found).
        """
        events: List[AssignmentEvent] = []
        client = self._study_client

        try:
            courses = client.fetch_courses(self._credentials, page_size=100)
        except Exception:
            logger.error("Failed to list courses during watcher poll", exc_info=True)
            return events

        for course in courses:
            course_id = course.get("id")
            course_name = course.get("name", "Unknown course")
            if not course_id:
                continue

            try:
                assignments = client.fetch_assignments(self._credentials, course_id)
            except Exception:
                logger.error(
                    "Failed to list assignments for course %s (%s)",
                    course_name, course_id, exc_info=True,
                )
                continue

            for assignment in assignments:
                event = self._process_assignment(course_id, course_name, assignment)
                if event:
                    events.append(event)
                    delivered = True
                    try:
                        res = self._on_event(event)
                        if res is False:
                            delivered = False
                    except Exception:
                        logger.error(
                            "on_event handler failed for assignment %s",
                            event.assignment_id, exc_info=True,
                        )
                        delivered = False

                    if delivered:
                        if event.kind == "new":
                            if hasattr(self._state, "mark_assignment_sent"):
                                self._state.mark_assignment_sent(
                                    event.course_id, event.assignment_id, user_id=self._user_id
                                )
                        elif event.kind == "deadline_updated":
                            if hasattr(self._state, "mark_deadline_update_sent"):
                                self._state.mark_deadline_update_sent(
                                    event.course_id, event.assignment_id, user_id=self._user_id
                                )
                        if self._budget:
                            self._budget.record_success()
                        logger.info("Notification successfully delivered: '%s' (%s)", event.title, event.kind)
                    else:
                        # Delivery failed or returned False! Mark retryable (deduplication strictly preserved)
                        err_msg = "Notification delivery failed or unconfirmed"
                        if event.kind == "new":
                            if hasattr(self._state, "mark_assignment_retryable"):
                                self._state.mark_assignment_retryable(
                                    event.course_id, event.assignment_id, error=err_msg, user_id=self._user_id
                                )
                        elif event.kind == "deadline_updated":
                            if hasattr(self._state, "mark_deadline_update_retryable"):
                                self._state.mark_deadline_update_retryable(
                                    event.course_id, event.assignment_id, error=err_msg, user_id=self._user_id
                                )
                        if self._budget:
                            self._budget.record_failure()
                        logger.warning(
                            "Delivery failed for '%s' (%s) — claim marked retryable for safe recovery",
                            event.title, course_name,
                        )

        logger.info(
            "Watcher poll complete for user_id=%s: %d event(s) detected",
            self._user_id, len(events),
        )
        return events

    def _process_assignment(
        self, course_id: str, course_name: str, assignment: dict
    ) -> Optional[AssignmentEvent]:
        classified = classify_assignment(
            assignment,
            course_id=course_id,
            course_name=course_name,
            state_store=self._state,
            user_id=self._user_id,
            filter_historical=self._filter_historical,
            now=self._now,
        )
        if classified is None:
            return None

        if classified.classification == AssignmentClassification.HISTORICAL_BASELINE:
            if hasattr(self._state, "record_baseline_assignment"):
                self._state.record_baseline_assignment(
                    classified.course_id,
                    classified.assignment_id,
                    classified.title,
                    classified.due_date,
                    classified.due_time,
                    user_id=self._user_id,
                )
            elif hasattr(self._state, "record_new_assignment"):
                self._state.record_new_assignment(
                    classified.course_id,
                    classified.assignment_id,
                    classified.title,
                    classified.due_date,
                    classified.due_time,
                    user_id=self._user_id,
                )
            if self._budget:
                self._budget.record_suppressed()
            logger.info("Historical assignment baseline recorded (no notification): %s (%s)", classified.title, course_name)
            return None

        elif classified.classification == AssignmentClassification.NEW:
            if self._budget and not self._budget.can_send():
                logger.info(
                    "Skipping new assignment notification for '%s' (%s): "
                    "MAX_NOTIFICATIONS_PER_RUN limit reached. Claim deferred.",
                    classified.title, course_name,
                )
                self._budget.record_deferred()
                return None

            if hasattr(self._state, "claim_new_assignment"):
                won = self._state.claim_new_assignment(
                    classified.course_id,
                    classified.assignment_id,
                    classified.title,
                    classified.due_date,
                    classified.due_time,
                    user_id=self._user_id,
                )
            else:
                won = self._state.record_new_assignment(
                    classified.course_id,
                    classified.assignment_id,
                    classified.title,
                    classified.due_date,
                    classified.due_time,
                    user_id=self._user_id,
                )
            if not won:
                # Already claimed or unchanged
                if self._budget:
                    self._budget.record_suppressed()
                return None

            if self._budget:
                self._budget.record_attempt()

            logger.info("New assignment claimed (pending delivery): %s (%s)", classified.title, course_name)
            return AssignmentEvent(
                kind="new",
                course_id=classified.course_id,
                course_name=classified.course_name,
                assignment_id=classified.assignment_id,
                title=classified.title,
                due_date=classified.due_date,
                due_time=classified.due_time,
            )

        elif classified.classification == AssignmentClassification.DEADLINE_UPDATED:
            if self._budget and not self._budget.can_send():
                logger.info(
                    "Skipping deadline update notification for '%s' (%s): "
                    "MAX_NOTIFICATIONS_PER_RUN limit reached. Claim deferred.",
                    classified.title, course_name,
                )
                self._budget.record_deferred()
                return None

            known = self._state.get_known_assignment(classified.course_id, classified.assignment_id, user_id=self._user_id)
            old_due_date_json = known.get("due_date_json") if known else None
            old_due_time_json = known.get("due_time_json") if known else None

            if hasattr(self._state, "claim_deadline_update"):
                won = self._state.claim_deadline_update(
                    classified.course_id,
                    classified.assignment_id,
                    classified.title,
                    classified.due_date,
                    classified.due_time,
                    old_due_date_json=old_due_date_json,
                    old_due_time_json=old_due_time_json,
                    user_id=self._user_id,
                )
            else:
                won = self._state.update_assignment_deadline(
                    classified.course_id,
                    classified.assignment_id,
                    classified.title,
                    classified.due_date,
                    classified.due_time,
                    old_due_date_json=old_due_date_json,
                    old_due_time_json=old_due_time_json,
                    user_id=self._user_id,
                )
            if not won:
                if self._budget:
                    self._budget.record_suppressed()
                return None

            if self._budget:
                self._budget.record_attempt()

            logger.info("Deadline change claimed (pending delivery): %s (%s)", classified.title, course_name)
            return AssignmentEvent(
                kind="deadline_updated",
                course_id=classified.course_id,
                course_name=classified.course_name,
                assignment_id=classified.assignment_id,
                title=classified.title,
                due_date=classified.due_date,
                due_time=classified.due_time,
                previous_due_date=classified.previous_due_date,
                previous_due_time=classified.previous_due_time,
            )

        # UNCHANGED
        if self._budget:
            self._budget.record_suppressed()
        return None


