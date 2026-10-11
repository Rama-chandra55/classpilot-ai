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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, List, Optional

from . import study_client
from .state_store import StateStore, DEFAULT_USER_ID

logger = logging.getLogger(__name__)


def is_historical_assignment(assignment: dict, now: Optional[datetime] = None) -> bool:
    """
    Check if an assignment is historical (e.g. deadline has already passed).
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
        if due_dt is not None:
            return due_dt <= now

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
        on_event: Callable[[AssignmentEvent], None],
        credentials,
        user_id: str = DEFAULT_USER_ID,
        filter_historical: bool = False,
    ):
        """
        Args:
            state_store: StateStore used to dedupe across polls/restarts.
            on_event: Callback invoked once per detected AssignmentEvent.
            credentials: google.oauth2.credentials.Credentials for the
                         user this watcher polls Classroom on behalf of.
            user_id: identifies whose dedup state to read/write in
                     state_store — must correspond to the same user as
                     `credentials`.
            filter_historical: if True, suppresses 'new' events for historical assignments
                               (e.g. past due dates) on first startup or subsequent polls.
        """
        self._state = state_store
        self._on_event = on_event
        self._credentials = credentials
        self._user_id = user_id
        self._filter_historical = filter_historical

    def check_once(self) -> List[AssignmentEvent]:
        """
        Run a single poll cycle across all active courses.

        Returns:
            The list of events detected during this cycle (also dispatched
            to on_event as they're found).
        """
        events: List[AssignmentEvent] = []

        try:
            courses = study_client.fetch_courses(self._credentials, page_size=100)
        except Exception:
            logger.error("Failed to list courses during watcher poll", exc_info=True)
            return events

        for course in courses:
            course_id = course.get("id")
            course_name = course.get("name", "Unknown course")
            if not course_id:
                continue

            try:
                assignments = study_client.fetch_assignments(self._credentials, course_id)
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
                    try:
                        self._on_event(event)
                    except Exception:
                        logger.error(
                            "on_event handler failed for assignment %s",
                            event.assignment_id, exc_info=True,
                        )

        logger.info(
            "Watcher poll complete for user_id=%s: %d event(s) detected",
            self._user_id, len(events),
        )
        return events

    def _process_assignment(
        self, course_id: str, course_name: str, assignment: dict
    ) -> Optional[AssignmentEvent]:
        assignment_id = assignment.get("id")
        title = assignment.get("title", "Untitled assignment")
        due_date = assignment.get("due_date")
        due_time = assignment.get("due_time")

        if not assignment_id:
            return None

        known = self._state.get_known_assignment(course_id, assignment_id, user_id=self._user_id)

        if known is None:
            # Check if this assignment is historical
            if self._filter_historical and is_historical_assignment(assignment):
                self._state.record_new_assignment(
                    course_id, assignment_id, title, due_date, due_time, user_id=self._user_id,
                )
                logger.info("Historical assignment baseline recorded (no notification): %s (%s)", title, course_name)
                return None

            # Brand new assignment we've never recorded before
            won = self._state.record_new_assignment(
                course_id, assignment_id, title, due_date, due_time, user_id=self._user_id,
            )
            if not won:
                # Another worker claimed it first
                return None
            logger.info("New assignment detected: %s (%s)", title, course_name)
            return AssignmentEvent(
                kind="new",
                course_id=course_id,
                course_name=course_name,
                assignment_id=assignment_id,
                title=title,
                due_date=due_date,
                due_time=due_time,
            )

        # Already known - check whether the deadline changed.
        prev_due_date = json.loads(known["due_date_json"]) if known["due_date_json"] else None
        prev_due_time = json.loads(known["due_time_json"]) if known["due_time_json"] else None

        if prev_due_date != due_date or prev_due_time != due_time:
            won = self._state.update_assignment_deadline(
                course_id, assignment_id, title, due_date, due_time, 
                old_due_date_json=known["due_date_json"], 
                old_due_time_json=known["due_time_json"], 
                user_id=self._user_id,
            )
            if not won:
                # Another worker changed the deadline first
                return None
            logger.info("Deadline change detected: %s (%s)", title, course_name)
            return AssignmentEvent(
                kind="deadline_updated",
                course_id=course_id,
                course_name=course_name,
                assignment_id=assignment_id,
                title=title,
                due_date=due_date,
                due_time=due_time,
                previous_due_date=prev_due_date,
                previous_due_time=prev_due_time,
            )

        return None

