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

    def record_new_assignment(
        self,
        course_id: str,
        assignment_id: str,
        title: str,
        due_date: Optional[dict],
        due_time: Optional[dict],
        user_id: str = DEFAULT_USER_ID,
    ) -> bool:
        """
        Atomic claim for a new assignment.
        Returns True ONLY if this exact invocation inserted the row.
        """
        with db.get_connection() as conn:
            row = conn.execute(
                """
                INSERT INTO known_assignments
                    (user_id, course_id, assignment_id, title, due_date_json, due_time_json)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
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

    def update_assignment_deadline(
        self,
        course_id: str,
        assignment_id: str,
        title: str,
        new_due_date: Optional[dict],
        new_due_time: Optional[dict],
        old_due_date_json: Optional[str],
        old_due_time_json: Optional[str],
        user_id: str = DEFAULT_USER_ID,
    ) -> bool:
        """
        Optimistic concurrency claim for a deadline change.
        Returns True ONLY if this exact invocation changed the deadline.
        """
        with db.get_connection() as conn:
            # We match on the exact old JSON string we observed. If another
            # worker changed it first, this UPDATE affects 0 rows.
            # (Note: we use IS NOT DISTINCT FROM to safely match NULLs in Postgres)
            row = conn.execute(
                """
                UPDATE known_assignments SET
                    title = %s,
                    due_date_json = %s,
                    due_time_json = %s,
                    last_updated_at = now()
                WHERE user_id = %s
                  AND course_id = %s
                  AND assignment_id = %s
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

    # ---------- Reminder dedup (Feature 3, used later) ----------

    def has_sent_reminder(
        self, course_id: str, assignment_id: str, offset_minutes: int, user_id: str = DEFAULT_USER_ID
    ) -> bool:
        with db.get_connection() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM sent_reminders
                WHERE user_id = %s AND course_id = %s AND assignment_id = %s AND offset_minutes = %s
                """,
                (user_id, course_id, assignment_id, offset_minutes),
            ).fetchone()
            return row is not None

    def mark_reminder_sent(
        self, course_id: str, assignment_id: str, offset_minutes: int, user_id: str = DEFAULT_USER_ID
    ) -> bool:
        """
        Atomic claim for sending a reminder.
        Returns True ONLY if this exact invocation inserted the row.
        """
        with db.get_connection() as conn:
            row = conn.execute(
                """
                INSERT INTO sent_reminders (user_id, course_id, assignment_id, offset_minutes)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING 1
                """,
                (user_id, course_id, assignment_id, offset_minutes),
            ).fetchone()
            return row is not None
