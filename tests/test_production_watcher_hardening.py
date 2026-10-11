"""
Production-readiness regression tests for ClassPilot AI AssignmentWatcher,
shared event classification, and simulation alignment.

Validates:
1. First-run initialization baseline for accounts with no history
   (historical assignments suppressed from notifications and recorded in StateStore).
2. Genuinely new assignments detected initially and after initialization.
3. Repeated polling idempotency (all unchanged assignments suppressed).
4. Deadline updates detected and notified.
5. Duplicate suppression across restarts and concurrent execution.
6. Per-course error isolation (e.g. course-level errors do not abort polling).
7. Per-user isolation and atomic deduplication.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from classpilot.config import ClassPilotConfig
from classpilot.events import EventType
from classpilot.notification_service import NotificationService
from classpilot.state_store import ReadOnlyStateStore, StateStore
from classpilot.watcher import (
    AssignmentClassification,
    AssignmentEvent,
    AssignmentWatcher,
    ClassifiedAssignment,
    classify_assignment,
    is_historical_assignment,
)


class InMemoryTestStateStore(StateStore):
    """
    In-memory StateStore simulation for isolated unit testing
    without requiring a live PostgreSQL instance.
    """

    def __init__(self):
        self._known = {}
        self._sent_reminders = set()
        self._enabled_watchers = set()

    def get_watcher_enabled(self, user_id: str) -> bool:
        return user_id in self._enabled_watchers

    def set_watcher_enabled(self, user_id: str, enabled: bool) -> None:
        if enabled:
            self._enabled_watchers.add(user_id)
        else:
            self._enabled_watchers.discard(user_id)

    def get_enabled_watchers(self) -> list[str]:
        return list(self._enabled_watchers)

    def get_known_assignment(self, course_id: str, assignment_id: str, user_id: str = "default"):
        return self._known.get((user_id, course_id, assignment_id))

    def get_all_assignments(self, user_id: str):
        return [v for (u, c, a), v in self._known.items() if u == user_id]

    def record_new_assignment(
        self, course_id: str, assignment_id: str, title: str,
        due_date: dict | None, due_time: dict | None, user_id: str = "default",
    ) -> bool:
        key = (user_id, course_id, assignment_id)
        if key in self._known:
            return False
        self._known[key] = {
            "user_id": user_id,
            "course_id": course_id,
            "assignment_id": assignment_id,
            "title": title,
            "due_date_json": json.dumps(due_date) if due_date else None,
            "due_time_json": json.dumps(due_time) if due_time else None,
        }
        return True

    def update_assignment_deadline(
        self, course_id: str, assignment_id: str, title: str,
        new_due_date: dict | None, new_due_time: dict | None,
        old_due_date_json: str | None, old_due_time_json: str | None,
        user_id: str = "default",
    ) -> bool:
        key = (user_id, course_id, assignment_id)
        existing = self._known.get(key)
        if not existing:
            return False
        # Optimistic concurrency check
        if existing["due_date_json"] != old_due_date_json or existing["due_time_json"] != old_due_time_json:
            return False
        existing["title"] = title
        existing["due_date_json"] = json.dumps(new_due_date) if new_due_date else None
        existing["due_time_json"] = json.dumps(new_due_time) if new_due_time else None
        return True

    def has_sent_reminder(self, course_id: str, assignment_id: str, offset_minutes: int, user_id: str = "default") -> bool:
        return (user_id, course_id, assignment_id, offset_minutes) in self._sent_reminders

    def mark_reminder_sent(self, course_id: str, assignment_id: str, offset_minutes: int, user_id: str = "default") -> bool:
        key = (user_id, course_id, assignment_id, offset_minutes)
        if key in self._sent_reminders:
            return False
        self._sent_reminders.add(key)
        return True


class TestProductionWatcherHardening(unittest.TestCase):

    def setUp(self):
        self.store = InMemoryTestStateStore()
        self.user_id = "usr-hardening-101"
        self.now = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
        self.events_dispatched = []

    def _event_handler(self, event: AssignmentEvent):
        self.events_dispatched.append(event)

    # --------------------------------------------------------------------------
    # 1. Historical Assignment Detection Helper
    # --------------------------------------------------------------------------

    def test_is_historical_assignment_past_deadline(self):
        """Assignments with past deadlines are marked historical."""
        past_assignment = {
            "id": "a-past",
            "title": "Past Assignment",
            "dueDate": {"year": 2026, "month": 9, "day": 1},
            "dueTime": {"hours": 23, "minutes": 59},
        }
        self.assertTrue(is_historical_assignment(past_assignment, now=self.now))

    def test_is_historical_assignment_future_deadline(self):
        """Assignments with future deadlines are not historical."""
        future_assignment = {
            "id": "a-future",
            "title": "Future Assignment",
            "dueDate": {"year": 2026, "month": 10, "day": 20},
            "dueTime": {"hours": 23, "minutes": 59},
        }
        self.assertFalse(is_historical_assignment(future_assignment, now=self.now))

    def test_is_historical_assignment_aged_creation(self):
        """Assignments created > 14 days ago without deadlines are marked historical."""
        aged_assignment = {
            "id": "a-aged",
            "title": "Aged Assignment",
            "creationTime": "2026-09-01T10:00:00Z",
        }
        self.assertTrue(is_historical_assignment(aged_assignment, now=self.now))

    def test_is_historical_assignment_recent_creation(self):
        """Assignments created recently (< 14 days ago) without deadlines are not historical."""
        recent_assignment = {
            "id": "a-recent",
            "title": "Recent Assignment",
            "creationTime": "2026-10-14T10:00:00Z",
        }
        self.assertFalse(is_historical_assignment(recent_assignment, now=self.now))

    # --------------------------------------------------------------------------
    # 2. Shared Event Classification Consistency
    # --------------------------------------------------------------------------

    def test_shared_event_classification_consistency(self):
        """classify_assignment outputs match expected classification decisions."""
        # Unrecorded historical assignment
        past_cw = {
            "id": "cw-1",
            "title": "Old HW",
            "dueDate": {"year": 2026, "month": 8, "day": 1},
        }
        decision1 = classify_assignment(
            past_cw, "c1", "Calculus", self.store, user_id=self.user_id,
            filter_historical=True, now=self.now,
        )
        self.assertIsNotNone(decision1)
        self.assertEqual(decision1.classification, AssignmentClassification.HISTORICAL_BASELINE)

        # Unrecorded genuine new assignment
        future_cw = {
            "id": "cw-2",
            "title": "New Project",
            "dueDate": {"year": 2026, "month": 10, "day": 25},
        }
        decision2 = classify_assignment(
            future_cw, "c1", "Calculus", self.store, user_id=self.user_id,
            filter_historical=True, now=self.now,
        )
        self.assertIsNotNone(decision2)
        self.assertEqual(decision2.classification, AssignmentClassification.NEW)

        # Record it in store
        self.store.record_new_assignment(
            "c1", "cw-2", "New Project",
            {"year": 2026, "month": 10, "day": 25}, None,
            user_id=self.user_id,
        )

        # Unchanged assignment
        decision3 = classify_assignment(
            future_cw, "c1", "Calculus", self.store, user_id=self.user_id,
            filter_historical=True, now=self.now,
        )
        self.assertIsNotNone(decision3)
        self.assertEqual(decision3.classification, AssignmentClassification.UNCHANGED)

        # Updated deadline assignment
        updated_cw = {
            "id": "cw-2",
            "title": "New Project",
            "dueDate": {"year": 2026, "month": 10, "day": 28},
        }
        decision4 = classify_assignment(
            updated_cw, "c1", "Calculus", self.store, user_id=self.user_id,
            filter_historical=True, now=self.now,
        )
        self.assertIsNotNone(decision4)
        self.assertEqual(decision4.classification, AssignmentClassification.DEADLINE_UPDATED)

    # --------------------------------------------------------------------------
    # 3. First-run Baseline Initialization for Account with No Prior History
    # --------------------------------------------------------------------------

    @patch("classpilot.watcher.study_client.fetch_courses")
    @patch("classpilot.watcher.study_client.fetch_assignments")
    def test_first_run_initialization_baselines_historical_assignments(
        self, mock_fetch_assignments, mock_fetch_courses
    ):
        """
        On first run for an account with no history:
        - Historical assignments are baselined in StateStore without notifications.
        - Genuinely new assignments trigger notifications and are recorded.
        """
        self.assertEqual(len(self.store.get_all_assignments(self.user_id)), 0)

        mock_fetch_courses.return_value = [{"id": "c1", "name": "Computer Science"}]
        mock_fetch_assignments.return_value = [
            # 1. Historical assignment (due in September 2026)
            {
                "id": "cw-past",
                "title": "Semester Syllabus Quiz",
                "due_date": {"year": 2026, "month": 9, "day": 1},
                "due_time": {"hours": 23, "minutes": 59},
            },
            # 2. Historical assignment (created 40 days ago, no deadline)
            {
                "id": "cw-aged",
                "title": "Intro Survey",
                "created": "2026-09-05T08:00:00Z",
            },
            # 3. Genuinely new upcoming assignment (due in 5 days)
            {
                "id": "cw-upcoming",
                "title": "Midterm Lab 3",
                "due_date": {"year": 2026, "month": 10, "day": 20},
                "due_time": {"hours": 23, "minutes": 59},
                "created": "2026-10-14T08:00:00Z",
            },
        ]

        watcher = AssignmentWatcher(
            state_store=self.store,
            on_event=self._event_handler,
            credentials="fake-creds",
            user_id=self.user_id,
            filter_historical=True,
            now=self.now,
        )

        events = watcher.check_once()

        # Only the genuinely new upcoming assignment should produce a notification event!
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "new")
        self.assertEqual(events[0].title, "Midterm Lab 3")
        self.assertEqual(len(self.events_dispatched), 1)

        # But ALL 3 assignments must now be baselined in StateStore!
        stored = self.store.get_all_assignments(self.user_id)
        self.assertEqual(len(stored), 3)
        stored_ids = {s["assignment_id"] for s in stored}
        self.assertIn("cw-past", stored_ids)
        self.assertIn("cw-aged", stored_ids)
        self.assertIn("cw-upcoming", stored_ids)

    # --------------------------------------------------------------------------
    # 4. Repeated Polling Idempotency (Zero Notifications, Suppressed)
    # --------------------------------------------------------------------------

    @patch("classpilot.watcher.study_client.fetch_courses")
    @patch("classpilot.watcher.study_client.fetch_assignments")
    def test_repeated_polling_after_initialization_is_completely_silent(
        self, mock_fetch_assignments, mock_fetch_courses
    ):
        """Repeated poll cycles with no changes emit 0 events and send 0 notifications."""
        mock_fetch_courses.return_value = [{"id": "c1", "name": "Computer Science"}]
        mock_fetch_assignments.return_value = [
            {
                "id": "cw-upcoming",
                "title": "Midterm Lab 3",
                "due_date": {"year": 2026, "month": 10, "day": 20},
                "due_time": {"hours": 23, "minutes": 59},
            },
        ]

        watcher = AssignmentWatcher(
            state_store=self.store,
            on_event=self._event_handler,
            credentials="fake-creds",
            user_id=self.user_id,
            filter_historical=True,
            now=self.now,
        )

        # First run: 1 event
        events_run1 = watcher.check_once()
        self.assertEqual(len(events_run1), 1)

        # Second run: 0 events (suppressed as unchanged)
        events_run2 = watcher.check_once()
        self.assertEqual(len(events_run2), 0)

        # Third run: 0 events (still suppressed)
        events_run3 = watcher.check_once()
        self.assertEqual(len(events_run3), 0)
        self.assertEqual(len(self.events_dispatched), 1)

    # --------------------------------------------------------------------------
    # 5. Genuine New Assignments After Initialization
    # --------------------------------------------------------------------------

    @patch("classpilot.watcher.study_client.fetch_courses")
    @patch("classpilot.watcher.study_client.fetch_assignments")
    def test_genuine_new_assignment_after_initialization_is_notified(
        self, mock_fetch_assignments, mock_fetch_courses
    ):
        """Assignments added after initialization are detected as NEW and dispatched."""
        mock_fetch_courses.return_value = [{"id": "c1", "name": "Computer Science"}]

        # Initial baseline: 1 assignment
        coursework_list = [
            {
                "id": "cw-1",
                "title": "Initial HW",
                "due_date": {"year": 2026, "month": 10, "day": 18},
            }
        ]
        mock_fetch_assignments.return_value = coursework_list

        watcher = AssignmentWatcher(
            state_store=self.store,
            on_event=self._event_handler,
            credentials="fake-creds",
            user_id=self.user_id,
            filter_historical=True,
            now=self.now,
        )

        watcher.check_once()
        self.assertEqual(len(self.events_dispatched), 1)

        # Teacher posts a new assignment AFTER initialization
        coursework_list.append({
            "id": "cw-brand-new",
            "title": "Brand New Assignment",
            "due_date": {"year": 2026, "month": 10, "day": 28},
            "created": "2026-10-15T10:00:00Z",
        })

        events_run2 = watcher.check_once()
        self.assertEqual(len(events_run2), 1)
        self.assertEqual(events_run2[0].kind, "new")
        self.assertEqual(events_run2[0].title, "Brand New Assignment")
        self.assertEqual(len(self.events_dispatched), 2)

    # --------------------------------------------------------------------------
    # 6. Deadline Updates Detection
    # --------------------------------------------------------------------------

    @patch("classpilot.watcher.study_client.fetch_courses")
    @patch("classpilot.watcher.study_client.fetch_assignments")
    def test_deadline_update_detected_and_dispatched(
        self, mock_fetch_assignments, mock_fetch_courses
    ):
        """When an assignment deadline changes, emit deadline_updated event."""
        mock_fetch_courses.return_value = [{"id": "c1", "name": "Math"}]
        cw = {
            "id": "cw-math",
            "title": "Problem Set 4",
            "due_date": {"year": 2026, "month": 10, "day": 18},
            "due_time": {"hours": 17},
        }
        mock_fetch_assignments.return_value = [cw]

        watcher = AssignmentWatcher(
            state_store=self.store,
            on_event=self._event_handler,
            credentials="fake-creds",
            user_id=self.user_id,
            filter_historical=True,
            now=self.now,
        )

        watcher.check_once()
        self.assertEqual(len(self.events_dispatched), 1)

        # Teacher extends deadline to Oct 22
        cw["due_date"] = {"year": 2026, "month": 10, "day": 22}

        events_run2 = watcher.check_once()
        self.assertEqual(len(events_run2), 1)
        self.assertEqual(events_run2[0].kind, "deadline_updated")
        self.assertEqual(events_run2[0].previous_due_date, {"year": 2026, "month": 10, "day": 18})
        self.assertEqual(events_run2[0].due_date, {"year": 2026, "month": 10, "day": 22})

    # --------------------------------------------------------------------------
    # 7. Duplicate Suppression After Deadline Update
    # --------------------------------------------------------------------------

    @patch("classpilot.watcher.study_client.fetch_courses")
    @patch("classpilot.watcher.study_client.fetch_assignments")
    def test_duplicate_suppression_after_deadline_update(
        self, mock_fetch_assignments, mock_fetch_courses
    ):
        """After updating a deadline, subsequent polls suppress it."""
        mock_fetch_courses.return_value = [{"id": "c1", "name": "Math"}]
        cw = {
            "id": "cw-math",
            "title": "Problem Set 4",
            "due_date": {"year": 2026, "month": 10, "day": 18},
        }
        mock_fetch_assignments.return_value = [cw]

        watcher = AssignmentWatcher(
            state_store=self.store,
            on_event=self._event_handler,
            credentials="fake-creds",
            user_id=self.user_id,
            filter_historical=True,
            now=self.now,
        )

        watcher.check_once()
        # Extend deadline
        cw["due_date"] = {"year": 2026, "month": 10, "day": 22}
        watcher.check_once()
        self.assertEqual(len(self.events_dispatched), 2)

        # Run again with same updated deadline
        events_run3 = watcher.check_once()
        self.assertEqual(len(events_run3), 0)
        self.assertEqual(len(self.events_dispatched), 2)

    # --------------------------------------------------------------------------
    # 8. Course Error Isolation
    # --------------------------------------------------------------------------

    @patch("classpilot.watcher.study_client.fetch_courses")
    @patch("classpilot.watcher.study_client.fetch_assignments")
    def test_course_level_error_isolation(
        self, mock_fetch_assignments, mock_fetch_courses
    ):
        """A failure in one course does not stop polling for other courses."""
        mock_fetch_courses.return_value = [
            {"id": "c-failing", "name": "Failing Course"},
            {"id": "c-healthy", "name": "Healthy Course"},
        ]

        def side_effect(creds, course_id):
            if course_id == "c-failing":
                raise RuntimeError("Coursework permission error HTTP 403")
            return [
                {
                    "id": "cw-healthy-1",
                    "title": "Lab 1",
                    "due_date": {"year": 2026, "month": 10, "day": 25},
                }
            ]

        mock_fetch_assignments.side_effect = side_effect

        watcher = AssignmentWatcher(
            state_store=self.store,
            on_event=self._event_handler,
            credentials="fake-creds",
            user_id=self.user_id,
            filter_historical=True,
            now=self.now,
        )

        events = watcher.check_once()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].title, "Lab 1")

    # --------------------------------------------------------------------------
    # 9. User Isolation
    # --------------------------------------------------------------------------

    def test_user_isolation_between_distinct_accounts(self):
        """Assignments for user A do not suppress or affect user B."""
        user_a = "usr-alice"
        user_b = "usr-bob"

        cw = {
            "id": "cw-shared",
            "title": "Shared Assignment",
            "dueDate": {"year": 2026, "month": 10, "day": 25},
        }

        # Classify for user A -> NEW
        dec_a = classify_assignment(cw, "c1", "Class", self.store, user_id=user_a, now=self.now)
        self.assertEqual(dec_a.classification, AssignmentClassification.NEW)
        self.store.record_new_assignment("c1", "cw-shared", "Shared Assignment", cw["dueDate"], None, user_id=user_a)

        # Classify for user B -> still NEW (user B has not recorded it yet!)
        dec_b = classify_assignment(cw, "c1", "Class", self.store, user_id=user_b, now=self.now)
        self.assertEqual(dec_b.classification, AssignmentClassification.NEW)

        # Record for user B
        self.store.record_new_assignment("c1", "cw-shared", "Shared Assignment", cw["dueDate"], None, user_id=user_b)

        # Now both are UNCHANGED
        dec_a2 = classify_assignment(cw, "c1", "Class", self.store, user_id=user_a, now=self.now)
        dec_b2 = classify_assignment(cw, "c1", "Class", self.store, user_id=user_b, now=self.now)
        self.assertEqual(dec_a2.classification, AssignmentClassification.UNCHANGED)
        self.assertEqual(dec_b2.classification, AssignmentClassification.UNCHANGED)


if __name__ == "__main__":
    unittest.main()
