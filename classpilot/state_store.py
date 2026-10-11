"""
Persistent state store for ClassPilot AI.

Tracks which assignments we've already seen/notified about, and (for Feature 3
later) which deadline reminders have already fired - so restarts and repeated
polling never produce duplicate notifications.

Phase 5A: Migrated to PostgreSQL.
  - Removes local SQLite `.db` file dependence.
  - Shares the `classpilot.db` process-wide connection pool.
  - Uses `psycopg.rows.dict_row` at the cursor level to preserve the dict-like 
    behavior expected by downstream consumers, without polluting the shared pool.
  - Concurrency/thread-safety is handled natively by the connection pool.

Phase 5B: Job Concurrency
  - Atomic claims (RETURNING 1) used to prevent duplicate LLM generation/emails.
"""

import json
import logging
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.rows import dict_row

from . import db

logger = logging.getLogger(__name__)

# Used only as an explicit opt-in default for callers that genuinely have
# no per-user context (e.g. a quick manual/test invocation) — every real
# call site in watcher.py/deadline_scheduler.py passes a real user_id as
# of Phase 3.
DEFAULT_USER_ID = "default"


class StateStore:
    """
    Wrapper around the Postgres database used for dedup state.
    """

    def __init__(self):
        # We no longer manage schema directly here. The tables are managed
        # by classpilot.db._SCHEMA and init_schema().
        pass

    # ---------- Watcher Enablement ----------

    def set_watcher_enabled(self, user_id: str, enabled: bool) -> None:
        """Toggle whether the background job should poll for this user."""
        with db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO watcher_configs (user_id, enabled)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    enabled = EXCLUDED.enabled,
                    updated_at = now()
                """,
                (user_id, enabled),
            )

    def get_watcher_enabled(self, user_id: str) -> bool:
        """Check if a specific user has the watcher enabled."""
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT enabled FROM watcher_configs WHERE user_id = %s",
                (user_id,),
            ).fetchone()
            return bool(row[0]) if row else False

    def get_enabled_watchers(self) -> List[str]:
        """Return a list of user_ids that have the watcher enabled."""
        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT user_id::text FROM watcher_configs WHERE enabled = true"
            ).fetchall()
            return [str(row[0]) for row in rows]

    # ---------- Assignment tracking (Feature 1) ----------

    def get_known_assignment(
        self, course_id: str, assignment_id: str, user_id: str = DEFAULT_USER_ID
    ) -> Optional[Dict[str, Any]]:
        with db.get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cursor:
                row = cursor.execute(
                    "SELECT * FROM known_assignments WHERE user_id = %s AND course_id = %s AND assignment_id = %s",
                    (user_id, course_id, assignment_id),
                ).fetchone()
                return dict(row) if row else None

    def get_all_assignments(self, user_id: str) -> List[Dict[str, Any]]:
        """Return all known assignments for a user (used to evaluate upcoming deadlines)."""
        with db.get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cursor:
                rows = cursor.execute(
                    "SELECT * FROM known_assignments WHERE user_id = %s",
                    (user_id,),
                ).fetchall()
                return [dict(row) for row in rows]

    def upsert_assignment_metadata(
        self,
        course_id: str,
        assignment_id: str,
        title: str,
        due_date: Optional[dict],
        due_time: Optional[dict],
        user_id: str = DEFAULT_USER_ID,
    ) -> None:
        """
        Legacy upsert for purely keeping title/metadata up to date
        without triggering notification semantics.
        """
        with db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO known_assignments
                    (user_id, course_id, assignment_id, title, due_date_json, due_time_json)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id, course_id, assignment_id) DO UPDATE SET
                    title = EXCLUDED.title,
                    due_date_json = EXCLUDED.due_date_json,
                    due_time_json = EXCLUDED.due_time_json,
                    last_updated_at = now()
                """,
                (
                    user_id,
                    course_id,
                    assignment_id,
                    title,
                    json.dumps(due_date) if due_date else None,
                    json.dumps(due_time) if due_time else None,
                ),
            )

    # ---------- Assignment tracking (Feature 1 & Claim Lifecycle) ----------

    def claim_new_assignment(
        self,
        course_id: str,
        assignment_id: str,
        title: str,
        due_date: Optional[dict],
        due_time: Optional[dict],
        user_id: str = DEFAULT_USER_ID,
    ) -> bool:
        """
        Concurrency-safe claim for a newly discovered assignment.
        Transitions state to 'pending'.
        Returns True ONLY if this worker won the claim (either by inserting a new row
        or by reclaiming an existing 'retryable' row).
        """
        # If a test subclass overrides record_new_assignment, delegate to it
        if type(self).record_new_assignment is not StateStore.record_new_assignment and type(self).claim_new_assignment is StateStore.claim_new_assignment:
            return self.record_new_assignment(course_id, assignment_id, title, due_date, due_time, user_id=user_id)

        # In-memory mock store fallback
        if hasattr(self, "_known"):
            key = (user_id, course_id, assignment_id)
            if key in self._known:
                rec = self._known[key]
                if rec.get("status") == "retryable":
                    rec["status"] = "pending"
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
                "retry_count": 0,
            }
            return True

        with db.get_connection() as conn:
            # 1. Try to insert as 'pending'
            row = conn.execute(
                """
                INSERT INTO known_assignments
                    (user_id, course_id, assignment_id, title, due_date_json, due_time_json,
                     status, claimed_at, retry_count)
                VALUES (%s, %s, %s, %s, %s, %s, 'pending', now(), 0)
                ON CONFLICT (user_id, course_id, assignment_id) DO NOTHING
                RETURNING 1
                """,
                (
                    user_id,
                    course_id,
                    assignment_id,
                    title,
                    json.dumps(due_date) if due_date else None,
                    json.dumps(due_time) if due_time else None,
                ),
            ).fetchone()
            if row is not None:
                return True

            # 2. If it was already inserted but is in 'retryable' state, reclaim it!
            row_reclaim = conn.execute(
                """
                UPDATE known_assignments
                SET status = 'pending',
                    claimed_at = now()
                WHERE user_id = %s AND course_id = %s AND assignment_id = %s
                  AND status = 'retryable'
                RETURNING 1
                """,
                (user_id, course_id, assignment_id),
            ).fetchone()
            return row_reclaim is not None

    def mark_assignment_sent(
        self,
        course_id: str,
        assignment_id: str,
        user_id: str = DEFAULT_USER_ID,
    ) -> bool:
        """
        Transitions claim state to 'sent' upon confirmed email delivery.
        """
        if hasattr(self, "_known"):
            key = (user_id, course_id, assignment_id)
            if key in self._known:
                self._known[key]["status"] = "sent"
                return True
            return False

        with db.get_connection() as conn:
            row = conn.execute(
                """
                UPDATE known_assignments
                SET status = 'sent',
                    sent_at = now(),
                    last_error = NULL
                WHERE user_id = %s AND course_id = %s AND assignment_id = %s
                RETURNING 1
                """,
                (user_id, course_id, assignment_id),
            ).fetchone()
            return row is not None

    def mark_assignment_retryable(
        self,
        course_id: str,
        assignment_id: str,
        error: str = "",
        user_id: str = DEFAULT_USER_ID,
    ) -> bool:
        """
        Transitions claim state from 'pending' to 'retryable' upon delivery failure.
        Preserves deduplication while permitting future runs to safely retry.
        """
        if hasattr(self, "_known"):
            key = (user_id, course_id, assignment_id)
            if key in self._known:
                self._known[key]["status"] = "retryable"
                self._known[key]["retry_count"] = self._known[key].get("retry_count", 0) + 1
                self._known[key]["last_error"] = error
                return True
            return False

        with db.get_connection() as conn:
            row = conn.execute(
                """
                UPDATE known_assignments
                SET status = 'retryable',
                    retry_count = retry_count + 1,
                    last_error = %s
                WHERE user_id = %s AND course_id = %s AND assignment_id = %s
                RETURNING 1
                """,
                (error, user_id, course_id, assignment_id),
            ).fetchone()
            return row is not None

    def record_baseline_assignment(
        self,
        course_id: str,
        assignment_id: str,
        title: str,
        due_date: Optional[dict],
        due_time: Optional[dict],
        user_id: str = DEFAULT_USER_ID,
    ) -> bool:
        """
        Records an assignment as part of the historical baseline (first run).
        Directly sets status='sent' so no notification is ever dispatched.
        """
        if type(self).record_new_assignment is not StateStore.record_new_assignment and type(self).record_baseline_assignment is StateStore.record_baseline_assignment:
            return self.record_new_assignment(course_id, assignment_id, title, due_date, due_time, user_id=user_id)

        if hasattr(self, "_known"):
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
                "status": "sent",
            }
            return True

        with db.get_connection() as conn:
            row = conn.execute(
                """
                INSERT INTO known_assignments
                    (user_id, course_id, assignment_id, title, due_date_json, due_time_json,
                     status, sent_at)
                VALUES (%s, %s, %s, %s, %s, %s, 'sent', now())
                ON CONFLICT (user_id, course_id, assignment_id) DO NOTHING
                RETURNING 1
                """,
                (
                    user_id,
                    course_id,
                    assignment_id,
                    title,
                    json.dumps(due_date) if due_date else None,
                    json.dumps(due_time) if due_time else None,
                ),
            ).fetchone()
            return row is not None

    def record_new_assignment(
        self,
        course_id: str,
        assignment_id: str,
        title: str,
        due_date: Optional[dict],
        due_time: Optional[dict],
        user_id: str = DEFAULT_USER_ID,
        is_baseline: bool = False,
    ) -> bool:
        """
        Atomic claim for a new assignment (or historical baseline).
        Returns True ONLY if this exact invocation inserted or claimed the row.
        """
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
        new_due_date: Optional[dict],
        new_due_time: Optional[dict],
        old_due_date_json: Optional[str] = None,
        old_due_time_json: Optional[str] = None,
        user_id: str = DEFAULT_USER_ID,
    ) -> bool:
        """
        Concurrency-safe claim for an assignment deadline change.
        Sets deadline_status='pending' and saves pending due date.
        """
        if type(self).update_assignment_deadline is not StateStore.update_assignment_deadline and type(self).claim_deadline_update is StateStore.claim_deadline_update:
            return self.update_assignment_deadline(
                course_id, assignment_id, title, new_due_date, new_due_time, old_due_date_json, old_due_time_json, user_id=user_id
            )

        if hasattr(self, "_known"):
            key = (user_id, course_id, assignment_id)
            existing = self._known.get(key)
            if not existing:
                return False
            if old_due_date_json is not None and existing.get("due_date_json") != old_due_date_json:
                return False
            if old_due_time_json is not None and existing.get("due_time_json") != old_due_time_json:
                return False
            existing["title"] = title
            existing["deadline_status"] = "pending"
            existing["pending_due_date_json"] = json.dumps(new_due_date) if new_due_date else None
            existing["pending_due_time_json"] = json.dumps(new_due_time) if new_due_time else None
            return True

        with db.get_connection() as conn:
            row = conn.execute(
                """
                UPDATE known_assignments
                SET title = %s,
                    deadline_status = 'pending',
                    pending_due_date_json = %s,
                    pending_due_time_json = %s,
                    deadline_claimed_at = now()
                WHERE user_id = %s
                  AND course_id = %s
                  AND assignment_id = %s
                  AND deadline_status IN ('sent', 'retryable')
                  AND due_date_json IS NOT DISTINCT FROM %s
                  AND due_time_json IS NOT DISTINCT FROM %s
                RETURNING 1
                """,
                (
                    title,
                    json.dumps(new_due_date) if new_due_date else None,
                    json.dumps(new_due_time) if new_due_time else None,
                    user_id,
                    course_id,
                    assignment_id,
                    old_due_date_json,
                    old_due_time_json,
                ),
            ).fetchone()
            return row is not None

    def mark_deadline_update_sent(
        self,
        course_id: str,
        assignment_id: str,
        user_id: str = DEFAULT_USER_ID,
    ) -> bool:
        """
        Commits pending deadline change into permanent due_date_json and sets deadline_status='sent'.
        """
        if hasattr(self, "_known"):
            key = (user_id, course_id, assignment_id)
            if key in self._known:
                existing = self._known[key]
                existing["deadline_status"] = "sent"
                if "pending_due_date_json" in existing:
                    existing["due_date_json"] = existing.pop("pending_due_date_json")
                if "pending_due_time_json" in existing:
                    existing["due_time_json"] = existing.pop("pending_due_time_json")
                return True
            return False

        with db.get_connection() as conn:
            row = conn.execute(
                """
                UPDATE known_assignments
                SET due_date_json = pending_due_date_json,
                    due_time_json = pending_due_time_json,
                    pending_due_date_json = NULL,
                    pending_due_time_json = NULL,
                    deadline_status = 'sent',
                    deadline_sent_at = now(),
                    deadline_last_error = NULL,
                    last_updated_at = now()
                WHERE user_id = %s AND course_id = %s AND assignment_id = %s
                RETURNING 1
                """,
                (user_id, course_id, assignment_id),
            ).fetchone()
            return row is not None

    def mark_deadline_update_retryable(
        self,
        course_id: str,
        assignment_id: str,
        error: str = "",
        user_id: str = DEFAULT_USER_ID,
    ) -> bool:
        """
        Marks deadline update as 'retryable' on delivery failure, leaving baseline deadline intact.
        """
        if hasattr(self, "_known"):
            key = (user_id, course_id, assignment_id)
            if key in self._known:
                self._known[key]["deadline_status"] = "retryable"
                return True
            return False

        with db.get_connection() as conn:
            row = conn.execute(
                """
                UPDATE known_assignments
                SET deadline_status = 'retryable',
                    deadline_retry_count = deadline_retry_count + 1,
                    deadline_last_error = %s
                WHERE user_id = %s AND course_id = %s AND assignment_id = %s
                RETURNING 1
                """,
                (error, user_id, course_id, assignment_id),
            ).fetchone()
            return row is not None

    def update_assignment_deadline(
        self,
        course_id: str,
        assignment_id: str,
        title: str,
        new_due_date: Optional[dict],
        new_due_time: Optional[dict],
        old_due_date_json: Optional[str] = None,
        old_due_time_json: Optional[str] = None,
        user_id: str = DEFAULT_USER_ID,
    ) -> bool:
        """Backward-compatible wrapper for claiming a deadline update."""
        return self.claim_deadline_update(
            course_id=course_id,
            assignment_id=assignment_id,
            title=title,
            new_due_date=new_due_date,
            new_due_time=new_due_time,
            old_due_date_json=old_due_date_json,
            old_due_time_json=old_due_time_json,
            user_id=user_id,
        )

    # ---------- Reminder dedup (Feature 3 & Claim Lifecycle) ----------

    def claim_reminder(
        self,
        course_id: str,
        assignment_id: str,
        offset_minutes: int,
        user_id: str = DEFAULT_USER_ID,
    ) -> bool:
        """
        Concurrency-safe claim for sending a deadline reminder.
        Sets status='pending'.
        """
        if type(self).mark_reminder_sent is not StateStore.mark_reminder_sent and type(self).claim_reminder is StateStore.claim_reminder:
            return self.mark_reminder_sent(course_id, assignment_id, offset_minutes, user_id=user_id)

        if hasattr(self, "_sent_reminders") and isinstance(self._sent_reminders, set):
            key = (user_id, course_id, assignment_id, offset_minutes)
            if key in self._sent_reminders:
                return False
            self._sent_reminders.add(key)
            return True
        elif hasattr(self, "_sent_reminders") and isinstance(self._sent_reminders, dict):
            key = (user_id, course_id, assignment_id, offset_minutes)
            if key in self._sent_reminders:
                rec = self._sent_reminders[key]
                if rec.get("status") == "retryable":
                    rec["status"] = "pending"
                    return True
                return False
            self._sent_reminders[key] = {"status": "pending"}
            return True

        with db.get_connection() as conn:
            # 1. Try insert as pending
            row = conn.execute(
                """
                INSERT INTO sent_reminders (user_id, course_id, assignment_id, offset_minutes,
                                            status, claimed_at, retry_count)
                VALUES (%s, %s, %s, %s, 'pending', now(), 0)
                ON CONFLICT (user_id, course_id, assignment_id, offset_minutes) DO NOTHING
                RETURNING 1
                """,
                (user_id, course_id, assignment_id, offset_minutes),
            ).fetchone()
            if row is not None:
                return True

            # 2. Try reclaim retryable
            row_reclaim = conn.execute(
                """
                UPDATE sent_reminders
                SET status = 'pending',
                    claimed_at = now()
                WHERE user_id = %s AND course_id = %s AND assignment_id = %s AND offset_minutes = %s
                  AND status = 'retryable'
                RETURNING 1
                """,
                (user_id, course_id, assignment_id, offset_minutes),
            ).fetchone()
            return row_reclaim is not None

    def mark_reminder_sent_success(
        self,
        course_id: str,
        assignment_id: str,
        offset_minutes: int,
        user_id: str = DEFAULT_USER_ID,
    ) -> bool:
        if hasattr(self, "_sent_reminders") and isinstance(self._sent_reminders, dict):
            key = (user_id, course_id, assignment_id, offset_minutes)
            if key in self._sent_reminders:
                self._sent_reminders[key]["status"] = "sent"
                return True
            return False
        elif hasattr(self, "_sent_reminders") and isinstance(self._sent_reminders, set):
            self._sent_reminders.add((user_id, course_id, assignment_id, offset_minutes))
            return True

        with db.get_connection() as conn:
            row = conn.execute(
                """
                UPDATE sent_reminders
                SET status = 'sent',
                    sent_at = now(),
                    last_error = NULL
                WHERE user_id = %s AND course_id = %s AND assignment_id = %s AND offset_minutes = %s
                RETURNING 1
                """,
                (user_id, course_id, assignment_id, offset_minutes),
            ).fetchone()
            return row is not None

    def mark_reminder_retryable(
        self,
        course_id: str,
        assignment_id: str,
        offset_minutes: int,
        error: str = "",
        user_id: str = DEFAULT_USER_ID,
    ) -> bool:
        if hasattr(self, "_sent_reminders") and isinstance(self._sent_reminders, dict):
            key = (user_id, course_id, assignment_id, offset_minutes)
            if key in self._sent_reminders:
                self._sent_reminders[key]["status"] = "retryable"
                return True
            return False
        elif hasattr(self, "_sent_reminders") and isinstance(self._sent_reminders, set):
            return True

        with db.get_connection() as conn:
            row = conn.execute(
                """
                UPDATE sent_reminders
                SET status = 'retryable',
                    retry_count = retry_count + 1,
                    last_error = %s
                WHERE user_id = %s AND course_id = %s AND assignment_id = %s AND offset_minutes = %s
                RETURNING 1
                """,
                (error, user_id, course_id, assignment_id, offset_minutes),
            ).fetchone()
            return row is not None

    def mark_reminder_sent(
        self,
        course_id: str,
        assignment_id: str,
        offset_minutes: int,
        user_id: str = DEFAULT_USER_ID,
    ) -> bool:
        """Backward-compatible wrapper for claiming a reminder."""
        return self.claim_reminder(course_id, assignment_id, offset_minutes, user_id=user_id)

    def has_sent_reminder(
        self, course_id: str, assignment_id: str, offset_minutes: int, user_id: str = DEFAULT_USER_ID
    ) -> bool:
        """
        Returns True if reminder is already sent or actively pending in another worker.
        """
        if hasattr(self, "_sent_reminders") and isinstance(self._sent_reminders, set):
            return (user_id, course_id, assignment_id, offset_minutes) in self._sent_reminders
        elif hasattr(self, "_sent_reminders") and isinstance(self._sent_reminders, dict):
            rec = self._sent_reminders.get((user_id, course_id, assignment_id, offset_minutes))
            return rec is not None and rec.get("status") in ("sent", "pending")

        with db.get_connection() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM sent_reminders
                WHERE user_id = %s AND course_id = %s AND assignment_id = %s AND offset_minutes = %s
                  AND status IN ('sent', 'pending')
                """,
                (user_id, course_id, assignment_id, offset_minutes),
            ).fetchone()
            return row is not None

    def recover_abandoned_claims(
        self, abandoned_timeout_seconds: int = 300, user_id: Optional[str] = None
    ) -> int:
        """
        Recovers claims that were left in 'pending' state due to worker process crashes,
        network dropouts, or terminations older than abandoned_timeout_seconds.
        Transitions them to 'retryable' so future poll cycles can safely retry them.
        """
        recovered = 0
        with db.get_connection() as conn:
            # 1. Recover abandoned assignment claims
            query_assign = """
                UPDATE known_assignments
                SET status = 'retryable',
                    last_error = 'Claim abandoned (worker timeout)'
                WHERE status = 'pending'
                  AND claimed_at < now() - (%s || ' seconds')::interval
            """
            params_assign = [str(abandoned_timeout_seconds)]
            if user_id:
                query_assign += " AND user_id = %s"
                params_assign.append(user_id)
            cursor_assign = conn.execute(query_assign, tuple(params_assign))
            recovered += cursor_assign.rowcount if hasattr(cursor_assign, "rowcount") else 0

            # 2. Recover abandoned deadline claims
            query_deadline = """
                UPDATE known_assignments
                SET deadline_status = 'retryable',
                    deadline_last_error = 'Claim abandoned (worker timeout)'
                WHERE deadline_status = 'pending'
                  AND deadline_claimed_at < now() - (%s || ' seconds')::interval
            """
            params_deadline = [str(abandoned_timeout_seconds)]
            if user_id:
                query_deadline += " AND user_id = %s"
                params_deadline.append(user_id)
            cursor_deadline = conn.execute(query_deadline, tuple(params_deadline))
            recovered += cursor_deadline.rowcount if hasattr(cursor_deadline, "rowcount") else 0

            # 3. Recover abandoned reminder claims
            query_reminder = """
                UPDATE sent_reminders
                SET status = 'retryable',
                    last_error = 'Claim abandoned (worker timeout)'
                WHERE status = 'pending'
                  AND claimed_at < now() - (%s || ' seconds')::interval
            """
            params_reminder = [str(abandoned_timeout_seconds)]
            if user_id:
                query_reminder += " AND user_id = %s"
                params_reminder.append(user_id)
            cursor_reminder = conn.execute(query_reminder, tuple(params_reminder))
            recovered += cursor_reminder.rowcount if hasattr(cursor_reminder, "rowcount") else 0

        if recovered > 0:
            logger.info("Recovered %d abandoned notification claim(s) to 'retryable'", recovered)
        return recovered

    def delete_known_assignment(
        self, course_id: str, assignment_id: str, user_id: str = DEFAULT_USER_ID
    ) -> bool:
        """Delete a known assignment record (legacy fallback)."""
        with db.get_connection() as conn:
            row = conn.execute(
                """
                DELETE FROM known_assignments
                WHERE user_id = %s AND course_id = %s AND assignment_id = %s
                RETURNING 1
                """,
                (user_id, course_id, assignment_id),
            ).fetchone()
            return row is not None

    def unmark_reminder_sent(
        self, course_id: str, assignment_id: str, offset_minutes: int, user_id: str = DEFAULT_USER_ID
    ) -> bool:
        """Remove a reminder record (legacy fallback)."""
        with db.get_connection() as conn:
            row = conn.execute(
                """
                DELETE FROM sent_reminders
                WHERE user_id = %s AND course_id = %s AND assignment_id = %s AND offset_minutes = %s
                RETURNING 1
                """,
                (user_id, course_id, assignment_id, offset_minutes),
            ).fetchone()
            return row is not None


class ReadOnlyStateStore(StateStore):
    """
    Read-only adapter for StateStore that strictly forbids and prevents any database writes.
    Used during watcher simulations to ensure zero mutations to PostgreSQL records,
    notification claims, checkpoints, or assignment snapshots.
    """

    def __init__(self, real_store: Optional[StateStore] = None):
        super().__init__()
        self._real_store = real_store or StateStore()
        # In-memory tracking for simulation session
        self._simulated_known: Dict[tuple, Dict[str, Any]] = {}
        self._simulated_sent_reminders: Dict[tuple, Dict[str, Any]] = {}

    # ---------- Read Methods (delegate to real store or in-memory overlay) ----------

    def get_watcher_enabled(self, user_id: str) -> bool:
        return self._real_store.get_watcher_enabled(user_id)

    def get_enabled_watchers(self) -> List[str]:
        return self._real_store.get_enabled_watchers()

    def get_known_assignment(
        self, course_id: str, assignment_id: str, user_id: str = DEFAULT_USER_ID
    ) -> Optional[Dict[str, Any]]:
        sim_key = (user_id, course_id, assignment_id)
        if sim_key in self._simulated_known:
            return self._simulated_known[sim_key]
        return self._real_store.get_known_assignment(course_id, assignment_id, user_id=user_id)

    def get_all_assignments(self, user_id: str) -> List[Dict[str, Any]]:
        db_assignments = self._real_store.get_all_assignments(user_id)
        # Merge in any simulated assignments that were added during this session
        res = {(a["course_id"], a["assignment_id"]): a for a in db_assignments}
        for (u, c, a), val in self._simulated_known.items():
            if u == user_id:
                res[(c, a)] = val
        return list(res.values())

    def has_sent_reminder(
        self, course_id: str, assignment_id: str, offset_minutes: int, user_id: str = DEFAULT_USER_ID
    ) -> bool:
        key = (user_id, course_id, assignment_id, offset_minutes)
        if key in self._simulated_sent_reminders:
            rec = self._simulated_sent_reminders[key]
            return rec.get("status") in ("sent", "pending")
        return self._real_store.has_sent_reminder(course_id, assignment_id, offset_minutes, user_id=user_id)

    # ---------- Write Methods (blocked, pure in-memory simulation) ----------

    def claim_new_assignment(
        self,
        course_id: str,
        assignment_id: str,
        title: str,
        due_date: Optional[dict],
        due_time: Optional[dict],
        user_id: str = DEFAULT_USER_ID,
    ) -> bool:
        key = (user_id, course_id, assignment_id)
        if key in self._simulated_known:
            rec = self._simulated_known[key]
            if rec.get("status") == "retryable":
                rec["status"] = "pending"
                return True
            return False
        if self._real_store.get_known_assignment(course_id, assignment_id, user_id=user_id):
            return False
        self._simulated_known[key] = {
            "user_id": user_id,
            "course_id": course_id,
            "assignment_id": assignment_id,
            "title": title,
            "due_date_json": json.dumps(due_date) if due_date else None,
            "due_time_json": json.dumps(due_time) if due_time else None,
            "status": "pending",
            "retry_count": 0,
        }
        return True

    def mark_assignment_sent(
        self, course_id: str, assignment_id: str, user_id: str = DEFAULT_USER_ID
    ) -> bool:
        key = (user_id, course_id, assignment_id)
        if key in self._simulated_known:
            self._simulated_known[key]["status"] = "sent"
            return True
        return True

    def mark_assignment_retryable(
        self, course_id: str, assignment_id: str, error: str = "", user_id: str = DEFAULT_USER_ID
    ) -> bool:
        key = (user_id, course_id, assignment_id)
        if key in self._simulated_known:
            self._simulated_known[key]["status"] = "retryable"
            self._simulated_known[key]["retry_count"] = self._simulated_known[key].get("retry_count", 0) + 1
            self._simulated_known[key]["last_error"] = error
            return True
        return True

    def record_baseline_assignment(
        self,
        course_id: str,
        assignment_id: str,
        title: str,
        due_date: Optional[dict],
        due_time: Optional[dict],
        user_id: str = DEFAULT_USER_ID,
    ) -> bool:
        key = (user_id, course_id, assignment_id)
        if key in self._simulated_known or self._real_store.get_known_assignment(course_id, assignment_id, user_id=user_id):
            return False
        self._simulated_known[key] = {
            "user_id": user_id,
            "course_id": course_id,
            "assignment_id": assignment_id,
            "title": title,
            "due_date_json": json.dumps(due_date) if due_date else None,
            "due_time_json": json.dumps(due_time) if due_time else None,
            "status": "sent",
        }
        return True

    def record_new_assignment(
        self,
        course_id: str,
        assignment_id: str,
        title: str,
        due_date: Optional[dict],
        due_time: Optional[dict],
        user_id: str = DEFAULT_USER_ID,
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
        new_due_date: Optional[dict],
        new_due_time: Optional[dict],
        old_due_date_json: Optional[str] = None,
        old_due_time_json: Optional[str] = None,
        user_id: str = DEFAULT_USER_ID,
    ) -> bool:
        key = (user_id, course_id, assignment_id)
        existing = self._simulated_known.get(key)
        if not existing:
            real = self._real_store.get_known_assignment(course_id, assignment_id, user_id=user_id)
            if real:
                existing = dict(real)
                self._simulated_known[key] = existing
            else:
                return False
        existing["title"] = title
        existing["deadline_status"] = "pending"
        existing["pending_due_date_json"] = json.dumps(new_due_date) if new_due_date else None
        existing["pending_due_time_json"] = json.dumps(new_due_time) if new_due_time else None
        return True

    def mark_deadline_update_sent(
        self, course_id: str, assignment_id: str, user_id: str = DEFAULT_USER_ID
    ) -> bool:
        key = (user_id, course_id, assignment_id)
        if key in self._simulated_known:
            existing = self._simulated_known[key]
            existing["deadline_status"] = "sent"
            existing["due_date_json"] = existing.get("pending_due_date_json")
            existing["due_time_json"] = existing.get("pending_due_time_json")
            return True
        return True

    def mark_deadline_update_retryable(
        self, course_id: str, assignment_id: str, error: str = "", user_id: str = DEFAULT_USER_ID
    ) -> bool:
        key = (user_id, course_id, assignment_id)
        if key in self._simulated_known:
            self._simulated_known[key]["deadline_status"] = "retryable"
            self._simulated_known[key]["deadline_last_error"] = error
            return True
        return True

    def update_assignment_deadline(
        self,
        course_id: str,
        assignment_id: str,
        title: str,
        new_due_date: Optional[dict],
        new_due_time: Optional[dict],
        old_due_date_json: Optional[str] = None,
        old_due_time_json: Optional[str] = None,
        user_id: str = DEFAULT_USER_ID,
    ) -> bool:
        return self.claim_deadline_update(
            course_id=course_id,
            assignment_id=assignment_id,
            title=title,
            new_due_date=new_due_date,
            new_due_time=new_due_time,
            old_due_date_json=old_due_date_json,
            old_due_time_json=old_due_time_json,
            user_id=user_id,
        )

    def claim_reminder(
        self,
        course_id: str,
        assignment_id: str,
        offset_minutes: int,
        user_id: str = DEFAULT_USER_ID,
    ) -> bool:
        key = (user_id, course_id, assignment_id, offset_minutes)
        if key in self._simulated_sent_reminders:
            rec = self._simulated_sent_reminders[key]
            if rec.get("status") == "retryable":
                rec["status"] = "pending"
                return True
            return False
        if self._real_store.has_sent_reminder(course_id, assignment_id, offset_minutes, user_id=user_id):
            return False
        self._simulated_sent_reminders[key] = {
            "user_id": user_id,
            "course_id": course_id,
            "assignment_id": assignment_id,
            "offset_minutes": offset_minutes,
            "status": "pending",
            "retry_count": 0,
        }
        return True

    def mark_reminder_sent_success(
        self, course_id: str, assignment_id: str, offset_minutes: int, user_id: str = DEFAULT_USER_ID
    ) -> bool:
        key = (user_id, course_id, assignment_id, offset_minutes)
        if key in self._simulated_sent_reminders:
            self._simulated_sent_reminders[key]["status"] = "sent"
            return True
        return True

    def mark_reminder_retryable(
        self, course_id: str, assignment_id: str, offset_minutes: int, error: str = "", user_id: str = DEFAULT_USER_ID
    ) -> bool:
        key = (user_id, course_id, assignment_id, offset_minutes)
        if key in self._simulated_sent_reminders:
            self._simulated_sent_reminders[key]["status"] = "retryable"
            self._simulated_sent_reminders[key]["retry_count"] = self._simulated_sent_reminders[key].get("retry_count", 0) + 1
            self._simulated_sent_reminders[key]["last_error"] = error
            return True
        return True

    def mark_reminder_sent(
        self, course_id: str, assignment_id: str, offset_minutes: int, user_id: str = DEFAULT_USER_ID
    ) -> bool:
        return self.claim_reminder(course_id, assignment_id, offset_minutes, user_id=user_id)

    def recover_abandoned_claims(
        self, abandoned_timeout_seconds: int = 300, user_id: Optional[str] = None
    ) -> int:
        recovered = 0
        for rec in self._simulated_known.values():
            if rec.get("status") == "pending":
                rec["status"] = "retryable"
                recovered += 1
            if rec.get("deadline_status") == "pending":
                rec["deadline_status"] = "retryable"
                recovered += 1
        for rec in self._simulated_sent_reminders.values():
            if rec.get("status") == "pending":
                rec["status"] = "retryable"
                recovered += 1
        return recovered

    def delete_known_assignment(
        self, course_id: str, assignment_id: str, user_id: str = DEFAULT_USER_ID
    ) -> bool:
        key = (user_id, course_id, assignment_id)
        if key in self._simulated_known:
            del self._simulated_known[key]
            return True
        return False

    def unmark_reminder_sent(
        self, course_id: str, assignment_id: str, offset_minutes: int, user_id: str = DEFAULT_USER_ID
    ) -> bool:
        key = (user_id, course_id, assignment_id, offset_minutes)
        if key in self._simulated_sent_reminders:
            del self._simulated_sent_reminders[key]
            return True
        return False

    def set_watcher_enabled(self, user_id: str, enabled: bool) -> None:
        raise RuntimeError("Write attempted on ReadOnlyStateStore: set_watcher_enabled")

    def upsert_assignment_metadata(self, *args, **kwargs) -> None:
        raise RuntimeError("Write attempted on ReadOnlyStateStore: upsert_assignment_metadata")

