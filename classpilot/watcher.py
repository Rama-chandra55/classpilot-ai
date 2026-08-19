"""
Feature 1: Assignment Watcher

Polls Google Classroom on a fixed interval, detects newly created assignments
and changed deadlines, and emits events for downstream handling (AI
notifications in Feature 2/3). Duplicate detection is delegated to
StateStore, so restarts never cause repeat notifications.
"""

import json
import logging
from dataclasses import dataclass
from typing import Callable, List, Optional

from .classroom_client import classroom
from .state_store import StateStore

logger = logging.getLogger(__name__)


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
    """

    def __init__(self, state_store: StateStore, on_event: Callable[[AssignmentEvent], None]):
        """
        Args:
            state_store: StateStore used to dedupe across polls/restarts.
            on_event: Callback invoked once per detected AssignmentEvent.
        """
        self._state = state_store
        self._on_event = on_event

    def check_once(self) -> List[AssignmentEvent]:
        """
        Run a single poll cycle across all active courses.

        Returns:
            The list of events detected during this cycle (also dispatched
            to on_event as they're found).
        """
        events: List[AssignmentEvent] = []

        try:
            courses = classroom.list_courses(page_size=100, course_states=["ACTIVE"])
        except Exception:
            logger.error("Failed to list courses during watcher poll", exc_info=True)
            return events

        for course in courses:
            course_id = course.get("id")
            course_name = course.get("name", "Unknown course")
            if not course_id:
                continue

            try:
                assignments = classroom.list_assignments(course_id, page_size=100)
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

        logger.info("Watcher poll complete: %d event(s) detected", len(events))
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

        known = self._state.get_known_assignment(course_id, assignment_id)

        if known is None:
            # Brand new assignment we've never recorded before.
            self._state.upsert_assignment(course_id, assignment_id, title, due_date, due_time)
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
            self._state.upsert_assignment(course_id, assignment_id, title, due_date, due_time)
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
