"""
Production notification safety safeguards regression tests for ClassPilot AI.

Validates all 10 core safety requirements:
1. Global notification cap (MAX_NOTIFICATIONS_PER_RUN) across new assignments,
   deadline updates, and reminders.
2. Accurate metrics tracking: attempted, successful, failed, deferred, and suppressed.
3. Concurrency-safe claim lifecycle: pending -> sent, pending -> retryable on failure,
   retryable -> pending -> sent on retry.
4. Abandoned claim recovery for timed-out pending states.
5. Deduplication preservation (retries do not remove deduplication).
6. PRODUCTION_TEST_USER_ID allowlist enforcement and fail-closed security.
7. Double-layer protection preventing accidental emails to non-allowlisted students.
8. Multi-account global budget sharing.
9. Zero side-effects in read-only simulation mode.
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from classpilot.budget import NotificationBudget
from classpilot.config import ClassPilotConfig
from classpilot.deadline_scheduler import DeadlineScheduler
from classpilot.events import EventType
from classpilot.job import main
from classpilot.services import build_user_services
from classpilot.state_store import ReadOnlyStateStore, StateStore
from classpilot.watcher import AssignmentEvent, AssignmentWatcher


class InMemoryTestStore(StateStore):
    """
    Thread-safe in-memory StateStore simulation for testing
    safeguards and claim lifecycles without requiring a live PostgreSQL instance.
    """

    def __init__(self):
        self._known = {}
        self._sent_reminders = {}
        self._enabled_watchers = set()
        self._lock = threading.Lock()

    def get_watcher_enabled(self, user_id: str) -> bool:
        with self._lock:
            return user_id in self._enabled_watchers

    def set_watcher_enabled(self, user_id: str, enabled: bool) -> None:
        with self._lock:
            if enabled:
                self._enabled_watchers.add(user_id)
            else:
                self._enabled_watchers.discard(user_id)

    def get_enabled_watchers(self) -> list[str]:
        with self._lock:
            return list(self._enabled_watchers)

    def get_known_assignment(self, course_id: str, assignment_id: str, user_id: str = "default"):
        with self._lock:
            return self._known.get((user_id, course_id, assignment_id))

    def get_all_assignments(self, user_id: str):
        with self._lock:
            return [v for (u, c, a), v in self._known.items() if u == user_id]

    def claim_new_assignment(
        self,
        course_id: str,
        assignment_id: str,
        title: str,
        due_date: dict | None,
        due_time: dict | None,
        user_id: str = "default",
    ) -> bool:
        key = (user_id, course_id, assignment_id)
        with self._lock:
            if key in self._known:
                rec = self._known[key]
                if rec.get("status") == "retryable":
                    rec["status"] = "pending"
                    rec["claimed_at"] = datetime.now(timezone.utc)
                    return True
                return False
            self._known[key] = {
                "user_id": user_id,
                "course_id": course_id,
                "assignment_id": assignment_id,
                "title": title,
                "due_date_json": json.dumps(due_date) if due_date else None,
                "due_time_json": json.dumps(due_time) if due_time else None,
                "status": "pending",
                "claimed_at": datetime.now(timezone.utc),
                "retry_count": 0,
                "deadline_status": "sent",
            }
            return True

    def mark_assignment_sent(self, course_id: str, assignment_id: str, user_id: str = "default") -> bool:
        key = (user_id, course_id, assignment_id)
        with self._lock:
            if key in self._known:
                self._known[key]["status"] = "sent"
                self._known[key]["sent_at"] = datetime.now(timezone.utc)
                return True
            return False

    def mark_assignment_retryable(
        self, course_id: str, assignment_id: str, error: str = "", user_id: str = "default"
    ) -> bool:
        key = (user_id, course_id, assignment_id)
        with self._lock:
            if key in self._known:
                self._known[key]["status"] = "retryable"
                self._known[key]["retry_count"] = self._known[key].get("retry_count", 0) + 1
                self._known[key]["last_error"] = error
                return True
            return False

    def record_baseline_assignment(
        self,
        course_id: str,
        assignment_id: str,
        title: str,
        due_date: dict | None,
        due_time: dict | None,
        user_id: str = "default",
    ) -> bool:
        key = (user_id, course_id, assignment_id)
        with self._lock:
            if key in self._known:
                return False
            self._known[key] = {
                "user_id": user_id,
                "course_id": course_id,
                "assignment_id": assignment_id,
                "title": title,
                "due_date_json": json.dumps(due_date) if due_date else None,
                "due_time_json": json.dumps(due_time) if due_time else None,
                "status": "sent",
                "sent_at": datetime.now(timezone.utc),
                "retry_count": 0,
                "deadline_status": "sent",
            }
            return True

    def record_new_assignment(
        self,
        course_id: str,
        assignment_id: str,
        title: str,
        due_date: dict | None,
        due_time: dict | None,
        user_id: str = "default",
        is_baseline: bool = False,
    ) -> bool:
        if is_baseline:
            return self.record_baseline_assignment(
                course_id, assignment_id, title, due_date, due_time, user_id=user_id
            )
        return self.claim_new_assignment(
            course_id, assignment_id, title, due_date, due_time, user_id=user_id
        )

    def claim_deadline_update(
        self,
        course_id: str,
        assignment_id: str,
        title: str,
        new_due_date: dict | None,
        new_due_time: dict | None,
        old_due_date_json: str | None = None,
        old_due_time_json: str | None = None,
        user_id: str = "default",
    ) -> bool:
        key = (user_id, course_id, assignment_id)
        with self._lock:
            existing = self._known.get(key)
            if not existing:
                return False
            if old_due_date_json is not None and existing["due_date_json"] != old_due_date_json:
                return False
            if old_due_time_json is not None and existing["due_time_json"] != old_due_time_json:
                return False
            if existing.get("deadline_status") not in ("sent", "retryable"):
                return False
            existing["title"] = title
            existing["deadline_status"] = "pending"
            existing["pending_due_date_json"] = json.dumps(new_due_date) if new_due_date else None
            existing["pending_due_time_json"] = json.dumps(new_due_time) if new_due_time else None
            existing["deadline_claimed_at"] = datetime.now(timezone.utc)
            return True

    def mark_deadline_update_sent(self, course_id: str, assignment_id: str, user_id: str = "default") -> bool:
        key = (user_id, course_id, assignment_id)
        with self._lock:
            existing = self._known.get(key)
            if not existing:
                return False
            existing["due_date_json"] = existing.get("pending_due_date_json")
            existing["due_time_json"] = existing.get("pending_due_time_json")
            existing["pending_due_date_json"] = None
            existing["pending_due_time_json"] = None
            existing["deadline_status"] = "sent"
            existing["deadline_sent_at"] = datetime.now(timezone.utc)
            return True

    def mark_deadline_update_retryable(
        self, course_id: str, assignment_id: str, error: str = "", user_id: str = "default"
    ) -> bool:
        key = (user_id, course_id, assignment_id)
        with self._lock:
            existing = self._known.get(key)
            if not existing:
                return False
            existing["deadline_status"] = "retryable"
            existing["deadline_retry_count"] = existing.get("deadline_retry_count", 0) + 1
            existing["deadline_last_error"] = error
            return True

    def update_assignment_deadline(self, *args, **kwargs) -> bool:
        return self.claim_deadline_update(*args, **kwargs)

    def claim_reminder(
        self, course_id: str, assignment_id: str, offset_minutes: int, user_id: str = "default"
    ) -> bool:
        key = (user_id, course_id, assignment_id, offset_minutes)
        with self._lock:
            if key in self._sent_reminders:
                rec = self._sent_reminders[key]
                if rec.get("status") == "retryable":
                    rec["status"] = "pending"
                    rec["claimed_at"] = datetime.now(timezone.utc)
                    return True
                return False
            self._sent_reminders[key] = {
                "user_id": user_id,
                "course_id": course_id,
                "assignment_id": assignment_id,
                "offset_minutes": offset_minutes,
                "status": "pending",
                "claimed_at": datetime.now(timezone.utc),
                "retry_count": 0,
            }
            return True

    def mark_reminder_sent_success(
        self, course_id: str, assignment_id: str, offset_minutes: int, user_id: str = "default"
    ) -> bool:
        key = (user_id, course_id, assignment_id, offset_minutes)
        with self._lock:
            if key in self._sent_reminders:
                self._sent_reminders[key]["status"] = "sent"
                self._sent_reminders[key]["sent_at"] = datetime.now(timezone.utc)
                return True
            return False

    def mark_reminder_retryable(
        self, course_id: str, assignment_id: str, offset_minutes: int, error: str = "", user_id: str = "default"
    ) -> bool:
        key = (user_id, course_id, assignment_id, offset_minutes)
        with self._lock:
            if key in self._sent_reminders:
                self._sent_reminders[key]["status"] = "retryable"
                self._sent_reminders[key]["retry_count"] = self._sent_reminders[key].get("retry_count", 0) + 1
                self._sent_reminders[key]["last_error"] = error
                return True
            return False

    def mark_reminder_sent(self, *args, **kwargs) -> bool:
        return self.claim_reminder(*args, **kwargs)

    def has_sent_reminder(
        self, course_id: str, assignment_id: str, offset_minutes: int, user_id: str = "default"
    ) -> bool:
        key = (user_id, course_id, assignment_id, offset_minutes)
        with self._lock:
            rec = self._sent_reminders.get(key)
            return rec is not None and rec.get("status") in ("sent", "pending")

    def recover_abandoned_claims(self, abandoned_timeout_seconds: int = 300, user_id: str | None = None) -> int:
        now = datetime.now(timezone.utc)
        threshold = timedelta(seconds=abandoned_timeout_seconds)
        recovered = 0
        with self._lock:
            for rec in self._known.values():
                if rec.get("status") == "pending":
                    claimed = rec.get("claimed_at")
                    if claimed and (now - claimed) > threshold:
                        rec["status"] = "retryable"
                        rec["last_error"] = "Claim abandoned (worker timeout)"
                        recovered += 1
                if rec.get("deadline_status") == "pending":
                    claimed = rec.get("deadline_claimed_at")
                    if claimed and (now - claimed) > threshold:
                        rec["deadline_status"] = "retryable"
                        rec["deadline_last_error"] = "Claim abandoned (worker timeout)"
                        recovered += 1
            for rec in self._sent_reminders.values():
                if rec.get("status") == "pending":
                    claimed = rec.get("claimed_at")
                    if claimed and (now - claimed) > threshold:
                        rec["status"] = "retryable"
                        rec["last_error"] = "Claim abandoned (worker timeout)"
                        recovered += 1
        return recovered

    def delete_known_assignment(self, course_id: str, assignment_id: str, user_id: str = "default") -> bool:
        key = (user_id, course_id, assignment_id)
        with self._lock:
            if key in self._known:
                del self._known[key]
                return True
            return False

    def unmark_reminder_sent(
        self, course_id: str, assignment_id: str, offset_minutes: int, user_id: str = "default"
    ) -> bool:
        key = (user_id, course_id, assignment_id, offset_minutes)
        with self._lock:
            if key in self._sent_reminders:
                del self._sent_reminders[key]
                return True
            return False


class TestNotificationBudgetUnit(unittest.TestCase):
    """Test unit behavior of NotificationBudget."""

    def test_budget_initialization_and_limits(self):
        b = NotificationBudget(limit=3)
        self.assertEqual(b.limit, 3)
        self.assertEqual(b.remaining, 3)
        self.assertTrue(b.can_send())

        self.assertTrue(b.record_attempt())
        b.record_success()
        self.assertEqual(b.remaining, 2)

        self.assertTrue(b.record_attempt())
        b.record_success()
        self.assertEqual(b.remaining, 1)

        self.assertTrue(b.record_attempt())
        b.record_success()
        self.assertEqual(b.remaining, 0)
        self.assertFalse(b.can_send())

        # Next attempts are deferred
        self.assertFalse(b.record_attempt())
        b.record_deferred()
        b.record_suppressed()

        summary = b.summary()
        self.assertEqual(summary["limit"], 3)
        self.assertEqual(summary["attempted"], 3)
        self.assertEqual(summary["successful"], 3)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["deferred"], 1)
        self.assertEqual(summary["suppressed"], 1)
        self.assertEqual(summary["remaining"], 0)

    def test_budget_thread_safety(self):
        budget = NotificationBudget(limit=10)

        def worker():
            for _ in range(5):
                if budget.record_attempt():
                    budget.record_success()
                else:
                    budget.record_deferred()

        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = [ex.submit(worker) for _ in range(8)]
            for f in futures:
                f.result()

        summary = budget.summary()
        self.assertEqual(summary["attempted"], 10)
        self.assertEqual(summary["successful"], 10)
        self.assertEqual(summary["remaining"], 0)
        self.assertEqual(summary["deferred"], 30)  # 40 total attempts - 10 allowed = 30 deferred


class TestClaimLifecycleTransitions(unittest.TestCase):
    """Test concurrency-safe claim lifecycle states: pending, sent, retryable, recovery."""

    def setUp(self):
        self.store = InMemoryTestStore()
        self.user_id = "user-lifecycle"
        self.store.set_watcher_enabled(self.user_id, True)

    def test_new_assignment_lifecycle_pending_to_sent(self):
        # 1. Unclaimed -> Pending claim
        won = self.store.claim_new_assignment("c1", "a1", "Essay", None, None, user_id=self.user_id)
        self.assertTrue(won)
        rec = self.store.get_known_assignment("c1", "a1", user_id=self.user_id)
        self.assertEqual(rec["status"], "pending")

        # Second concurrent attempt cannot claim while pending
        self.assertFalse(self.store.claim_new_assignment("c1", "a1", "Essay", None, None, user_id=self.user_id))

        # 2. Confirmed delivery -> Sent
        self.assertTrue(self.store.mark_assignment_sent("c1", "a1", user_id=self.user_id))
        rec = self.store.get_known_assignment("c1", "a1", user_id=self.user_id)
        self.assertEqual(rec["status"], "sent")

        # Future run sees 'sent' and cannot re-claim
        self.assertFalse(self.store.claim_new_assignment("c1", "a1", "Essay", None, None, user_id=self.user_id))

    def test_new_assignment_lifecycle_pending_to_retryable_to_sent(self):
        # 1. Claim pending
        self.assertTrue(self.store.claim_new_assignment("c1", "a2", "Project", None, None, user_id=self.user_id))

        # 2. Delivery failure -> Transition to retryable (deduplication preserved)
        self.assertTrue(self.store.mark_assignment_retryable("c1", "a2", error="SMTP 500", user_id=self.user_id))
        rec = self.store.get_known_assignment("c1", "a2", user_id=self.user_id)
        self.assertEqual(rec["status"], "retryable")
        self.assertEqual(rec["retry_count"], 1)
        self.assertEqual(rec["last_error"], "SMTP 500")

        # 3. Next run reclaims retryable -> pending
        reclaimed = self.store.claim_new_assignment("c1", "a2", "Project", None, None, user_id=self.user_id)
        self.assertTrue(reclaimed)
        rec = self.store.get_known_assignment("c1", "a2", user_id=self.user_id)
        self.assertEqual(rec["status"], "pending")

        # 4. Success -> Sent
        self.assertTrue(self.store.mark_assignment_sent("c1", "a2", user_id=self.user_id))
        self.assertEqual(self.store.get_known_assignment("c1", "a2", user_id=self.user_id)["status"], "sent")

    def test_abandoned_pending_claims_recovery(self):
        # Insert a pending claim with an artificial old timestamp
        self.store.claim_new_assignment("c1", "a-hang", "Hanging Task", None, None, user_id=self.user_id)
        old_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        self.store._known[(self.user_id, "c1", "a-hang")]["claimed_at"] = old_time

        # Recovery should transition abandoned pending -> retryable
        recovered = self.store.recover_abandoned_claims(abandoned_timeout_seconds=300)
        self.assertEqual(recovered, 1)

        rec = self.store.get_known_assignment("c1", "a-hang", user_id=self.user_id)
        self.assertEqual(rec["status"], "retryable")

    def test_concurrency_safe_single_winner_claim(self):
        winners = []

        def attempt_claim():
            if self.store.claim_new_assignment("c1", "a-race", "Race Test", None, None, user_id=self.user_id):
                winners.append(1)

        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = [ex.submit(attempt_claim) for _ in range(8)]
            for f in futures:
                f.result()

        # Exactly 1 worker wins the pending claim
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.store.get_known_assignment("c1", "a-race", user_id=self.user_id)["status"], "pending")


class TestGlobalNotificationCap(unittest.TestCase):
    """Test enforcement of MAX_NOTIFICATIONS_PER_RUN across watcher and scheduler."""

    def setUp(self):
        self.store = InMemoryTestStore()
        self.user_id = "test-user-safeguard"
        self.store.set_watcher_enabled(self.user_id, True)

    def test_watcher_respects_cap_and_preserves_unclaimed_assignments(self):
        """When budget is exhausted, watcher defers without claiming in DB."""
        budget = NotificationBudget(limit=2)
        dispatched = []

        def handle_event(event: AssignmentEvent):
            dispatched.append(event)
            return True

        watcher = AssignmentWatcher(
            state_store=self.store,
            on_event=handle_event,
            credentials=MagicMock(),
            user_id=self.user_id,
            filter_historical=False,  # Treat all as fresh
            budget=budget,
        )

        mock_assignments = [
            {"id": f"assign-{i}", "title": f"Assignment {i}", "dueDate": {"year": 2026, "month": 11, "day": 1}}
            for i in range(5)
        ]

        with patch("classpilot.study_client.fetch_courses", return_value=[{"id": "course-1"}]), \
             patch("classpilot.study_client.fetch_assignments", return_value=mock_assignments):
            watcher.check_once()

        # Exactly 2 were delivered, 3 deferred
        self.assertEqual(len(dispatched), 2)
        summary = budget.summary()
        self.assertEqual(summary["attempted"], 2)
        self.assertEqual(summary["successful"], 2)
        self.assertEqual(summary["deferred"], 3)

        # In DB, ONLY the 2 delivered assignments were claimed and marked sent!
        self.assertEqual(self.store.get_known_assignment("course-1", "assign-0", self.user_id)["status"], "sent")
        self.assertEqual(self.store.get_known_assignment("course-1", "assign-1", self.user_id)["status"], "sent")
        self.assertIsNone(self.store.get_known_assignment("course-1", "assign-2", self.user_id))
        self.assertIsNone(self.store.get_known_assignment("course-1", "assign-3", self.user_id))
        self.assertIsNone(self.store.get_known_assignment("course-1", "assign-4", self.user_id))

        # On a second run with a fresh budget, assign-2, assign-3, assign-4 are discovered and processed!
        fresh_budget = NotificationBudget(limit=5)
        dispatched_second = []

        watcher2 = AssignmentWatcher(
            state_store=self.store,
            on_event=lambda e: dispatched_second.append(e) or True,
            credentials=MagicMock(),
            user_id=self.user_id,
            filter_historical=False,
            budget=fresh_budget,
        )

        with patch("classpilot.study_client.fetch_courses", return_value=[{"id": "course-1"}]), \
             patch("classpilot.study_client.fetch_assignments", return_value=mock_assignments):
            watcher2.check_once()

        self.assertEqual(len(dispatched_second), 3)
        self.assertEqual({e.assignment_id for e in dispatched_second}, {"assign-2", "assign-3", "assign-4"})

    def test_deadline_scheduler_respects_cap_and_preserves_unclaimed_reminders(self):
        """When budget is exhausted, scheduler defers reminders without claiming them."""
        due_date = {"year": 2026, "month": 10, "day": 15}
        due_time = {"hours": 12, "minutes": 30}
        for i in range(3):
            self.store.record_baseline_assignment(
                "course-1", f"assign-{i}", f"Title {i}", due_date, due_time, user_id=self.user_id
            )

        config = ClassPilotConfig()
        config.deadline_alert_offsets_minutes = (15,)
        mock_svc = MagicMock()
        mock_svc.notify.return_value = True

        budget = NotificationBudget(limit=1)
        scheduler = DeadlineScheduler(
            state_store=self.store,
            notification_service=mock_svc,
            config=config,
            user_id=self.user_id,
            budget=budget,
        )

        fake_now = datetime(2026, 10, 15, 12, 15, 0, tzinfo=timezone.utc)
        with patch("classpilot.deadline_scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            scheduler.evaluate_all()

        # Only 1 was sent due to cap, 2 deferred
        self.assertEqual(mock_svc.notify.call_count, 1)
        summary = budget.summary()
        self.assertEqual(summary["attempted"], 1)
        self.assertEqual(summary["successful"], 1)
        self.assertEqual(summary["deferred"], 2)

        # In DB, only 1 reminder was sent
        sent_count = sum(
            1 for i in range(3)
            if self.store.has_sent_reminder("course-1", f"assign-{i}", 15, user_id=self.user_id)
        )
        self.assertEqual(sent_count, 1)

    def test_shared_budget_across_multiple_users(self):
        """Global budget is shared and decremented across distinct student accounts."""
        budget = NotificationBudget(limit=3)

        store = self.store
        user_a = "user-alpha"
        user_b = "user-beta"
        store.set_watcher_enabled(user_a, True)
        store.set_watcher_enabled(user_b, True)

        delivered_a = []
        delivered_b = []

        w_a = AssignmentWatcher(
            state_store=store,
            on_event=lambda e: delivered_a.append(e) or True,
            credentials=MagicMock(),
            user_id=user_a,
            filter_historical=False,
            budget=budget,
        )
        w_b = AssignmentWatcher(
            state_store=store,
            on_event=lambda e: delivered_b.append(e) or True,
            credentials=MagicMock(),
            user_id=user_b,
            filter_historical=False,
            budget=budget,
        )

        assignments_a = [{"id": "a-1", "title": "A1"}, {"id": "a-2", "title": "A2"}]
        assignments_b = [{"id": "b-1", "title": "B1"}, {"id": "b-2", "title": "B2"}]

        with patch("classpilot.study_client.fetch_courses", return_value=[{"id": "course-1"}]), \
             patch("classpilot.study_client.fetch_assignments", return_value=assignments_a):
            w_a.check_once()

        self.assertEqual(len(delivered_a), 2)
        self.assertEqual(budget.remaining, 1)

        with patch("classpilot.study_client.fetch_courses", return_value=[{"id": "course-1"}]), \
             patch("classpilot.study_client.fetch_assignments", return_value=assignments_b):
            w_b.check_once()

        # User B gets 1, and 1 deferred because global cap was 3!
        self.assertEqual(len(delivered_b), 1)
        self.assertEqual(budget.remaining, 0)
        self.assertEqual(budget.summary()["attempted"], 3)
        self.assertEqual(budget.summary()["deferred"], 1)


class TestSafeRetryOnDeliveryFailure(unittest.TestCase):
    """Test that delivery failures transition claims to retryable without losing state."""

    def setUp(self):
        self.store = InMemoryTestStore()
        self.user_id = "test-user-retry"
        self.store.set_watcher_enabled(self.user_id, True)

    def test_new_assignment_marked_retryable_on_failure(self):
        fail_email = True

        def handle_event(event: AssignmentEvent):
            if fail_email:
                return False
            return True

        watcher = AssignmentWatcher(
            state_store=self.store,
            on_event=handle_event,
            credentials=MagicMock(),
            user_id=self.user_id,
            filter_historical=False,
        )

        assignment = [{"id": "assign-retry-1", "title": "Math Quiz", "dueDate": {"year": 2026, "month": 11, "day": 1}}]

        with patch("classpilot.study_client.fetch_courses", return_value=[{"id": "course-1"}]), \
             patch("classpilot.study_client.fetch_assignments", return_value=assignment):
            # Attempt 1 fails
            watcher.check_once()

        # Record is in 'retryable' state with retry_count=1
        rec = self.store.get_known_assignment("course-1", "assign-retry-1", self.user_id)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["status"], "retryable")
        self.assertEqual(rec["retry_count"], 1)

        # Attempt 2 succeeds
        fail_email = False
        with patch("classpilot.study_client.fetch_courses", return_value=[{"id": "course-1"}]), \
             patch("classpilot.study_client.fetch_assignments", return_value=assignment):
            watcher.check_once()

        rec = self.store.get_known_assignment("course-1", "assign-retry-1", self.user_id)
        self.assertEqual(rec["status"], "sent")

    def test_deadline_update_marked_retryable_on_failure(self):
        old_due = {"year": 2026, "month": 10, "day": 20}
        self.store.record_baseline_assignment(
            "course-1", "assign-upd", "Project", old_due, None, user_id=self.user_id
        )

        fail_email = True

        def handle_event(event: AssignmentEvent):
            if fail_email:
                return False
            return True

        watcher = AssignmentWatcher(
            state_store=self.store,
            on_event=handle_event,
            credentials=MagicMock(),
            user_id=self.user_id,
            filter_historical=False,
        )

        new_due = {"year": 2026, "month": 10, "day": 25}
        assignment = [{"id": "assign-upd", "title": "Project", "dueDate": new_due}]

        with patch("classpilot.study_client.fetch_courses", return_value=[{"id": "course-1"}]), \
             patch("classpilot.study_client.fetch_assignments", return_value=assignment):
            # Attempt 1 fails
            watcher.check_once()

        rec = self.store.get_known_assignment("course-1", "assign-upd", self.user_id)
        self.assertEqual(rec["deadline_status"], "retryable")
        # Baseline deadline unchanged
        self.assertEqual(json.loads(rec["due_date_json"]), old_due)

        # Attempt 2 succeeds
        fail_email = False
        with patch("classpilot.study_client.fetch_courses", return_value=[{"id": "course-1"}]), \
             patch("classpilot.study_client.fetch_assignments", return_value=assignment):
            watcher.check_once()

        rec = self.store.get_known_assignment("course-1", "assign-upd", self.user_id)
        self.assertEqual(rec["deadline_status"], "sent")
        self.assertEqual(json.loads(rec["due_date_json"]), new_due)

    def test_reminder_marked_retryable_on_failure(self):
        due_date = {"year": 2026, "month": 10, "day": 15}
        due_time = {"hours": 12, "minutes": 30}
        self.store.record_baseline_assignment(
            "course-1", "assign-rem", "Physics Lab", due_date, due_time, user_id=self.user_id
        )

        mock_svc = MagicMock()
        mock_svc.notify.return_value = False  # Delivery failure

        config = ClassPilotConfig()
        scheduler = DeadlineScheduler(
            state_store=self.store,
            notification_service=mock_svc,
            config=config,
            user_id=self.user_id,
        )

        fake_now = datetime(2026, 10, 15, 12, 15, 0, tzinfo=timezone.utc)
        with patch("classpilot.deadline_scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            scheduler.evaluate_all()

        key = (self.user_id, "course-1", "assign-rem", 15)
        self.assertEqual(self.store._sent_reminders[key]["status"], "retryable")

        # Now succeeds on next cycle
        mock_svc.notify.return_value = True
        with patch("classpilot.deadline_scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            scheduler.evaluate_all()

        self.assertEqual(self.store._sent_reminders[key]["status"], "sent")


class TestProductionTestUserIdAllowlist(unittest.TestCase):
    """Test PRODUCTION_TEST_USER_ID allowlist, fail-closed security, and student isolation."""

    @patch("classpilot.job.StateStore")
    @patch("classpilot.job.UserStore")
    @patch("classpilot.job.get_authorized_credentials")
    @patch("classpilot.job.build_user_services")
    def test_valid_allowlist_restricts_to_target_account(
        self, mock_build, mock_get_creds, mock_user_store_cls, mock_state_store_cls
    ):
        mock_state = MagicMock()
        mock_state.get_enabled_watchers.return_value = ["target-user-id", "other-user-id"]
        mock_state_store_cls.return_value = mock_state

        mock_user_store = MagicMock()
        mock_user = MagicMock(email="target@college.edu")
        mock_user_store.get_user_by_id.side_effect = lambda uid: mock_user if uid == "target-user-id" else None
        mock_user_store_cls.return_value = mock_user_store

        mock_svc = MagicMock()
        mock_build.return_value = mock_svc

        with patch("classpilot.job.get_config") as mock_conf:
            conf = ClassPilotConfig()
            conf.production_test_user_id = "target-user-id"
            conf.max_notifications_per_run = 5
            mock_conf.return_value = conf

            with patch.object(sys, "argv", ["job.py"]):
                main()

        # Strictly target-user-id was processed
        mock_build.assert_called_once()
        self.assertEqual(mock_build.call_args[0][0], "target-user-id")
        mock_svc.run_poll_cycle.assert_called_once()

    @patch("classpilot.job.StateStore")
    @patch("classpilot.job.UserStore")
    def test_allowlist_fails_closed_when_user_not_found(self, mock_user_store_cls, mock_state_store_cls):
        mock_state = MagicMock()
        mock_state.get_enabled_watchers.return_value = ["some-user"]
        mock_state_store_cls.return_value = mock_state

        mock_user_store = MagicMock()
        mock_user_store.get_user_by_id.return_value = None
        mock_user_store_cls.return_value = mock_user_store

        with patch("classpilot.job.get_config") as mock_conf:
            conf = ClassPilotConfig()
            conf.production_test_user_id = "missing-user-id"
            mock_conf.return_value = conf

            with patch.object(sys, "argv", ["job.py"]):
                with self.assertRaises(SystemExit) as cm:
                    main()
                self.assertEqual(cm.exception.code, 1)

    @patch("classpilot.job.StateStore")
    @patch("classpilot.job.UserStore")
    def test_allowlist_fails_closed_when_watcher_not_enabled(self, mock_user_store_cls, mock_state_store_cls):
        mock_state = MagicMock()
        mock_state.get_enabled_watchers.return_value = ["other-user"]
        mock_state_store_cls.return_value = mock_state

        mock_user_store = MagicMock()
        mock_user_store.get_user_by_id.return_value = MagicMock(email="target@college.edu")
        mock_user_store_cls.return_value = mock_user_store

        with patch("classpilot.job.get_config") as mock_conf:
            conf = ClassPilotConfig()
            conf.production_test_user_id = "disabled-user-id"
            mock_conf.return_value = conf

            with patch.object(sys, "argv", ["job.py"]):
                with self.assertRaises(SystemExit) as cm:
                    main()
                self.assertEqual(cm.exception.code, 1)

    @patch("classpilot.job.StateStore")
    @patch("classpilot.job.UserStore")
    @patch("classpilot.job.get_authorized_credentials")
    def test_allowlist_fails_closed_when_credentials_unauthorized(
        self, mock_get_creds, mock_user_store_cls, mock_state_store_cls
    ):
        mock_state = MagicMock()
        mock_state.get_enabled_watchers.return_value = ["unauthorized-user-id"]
        mock_state_store_cls.return_value = mock_state

        mock_user_store = MagicMock()
        mock_user_store.get_user_by_id.return_value = MagicMock(email="unauth@college.edu")
        mock_user_store_cls.return_value = mock_user_store

        mock_get_creds.side_effect = RuntimeError("OAuth token expired")

        with patch("classpilot.job.get_config") as mock_conf:
            conf = ClassPilotConfig()
            conf.production_test_user_id = "unauthorized-user-id"
            mock_conf.return_value = conf

            with patch.object(sys, "argv", ["job.py"]):
                with self.assertRaises(SystemExit) as cm:
                    main()
                self.assertEqual(cm.exception.code, 1)

    def test_build_user_services_blocks_non_allowlisted_user(self):
        """Secondary defense line in build_user_services throws PermissionError for any other user."""
        with patch("classpilot.services.get_config") as mock_conf:
            conf = ClassPilotConfig()
            conf.production_test_user_id = "target-only-user"
            mock_conf.return_value = conf

            with self.assertRaises(PermissionError):
                build_user_services("unintended-other-student", MagicMock())


class TestSimulationZeroSideEffects(unittest.TestCase):
    """Verify ReadOnlyStateStore guarantees zero mutations in simulation."""

    def test_readonly_state_store_zero_database_mutations(self):
        mock_real = MagicMock(spec=StateStore)
        mock_real.get_known_assignment.return_value = None
        ro_store = ReadOnlyStateStore(mock_real)

        # Simulation claims assignment in-memory
        claimed = ro_store.claim_new_assignment("c1", "a1", "Test Assignment", None, None, user_id="sim-user")
        self.assertTrue(claimed)

        # Real store was NEVER called
        mock_real.claim_new_assignment.assert_not_called()
        mock_real.record_new_assignment.assert_not_called()
        mock_real.mark_assignment_sent.assert_not_called()

        # Read-only store tracks state in-memory
        rec = ro_store.get_known_assignment("c1", "a1", user_id="sim-user")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["status"], "pending")
