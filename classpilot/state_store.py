"""
Persistent state store for ClassPilot AI.

Tracks which assignments we've already seen/notified about, and (for Feature 3
later) which deadline reminders have already fired - so restarts and repeated
polling never produce duplicate notifications.

Multi-user readiness (Phase 1):
  Every method now accepts an optional `user_id` parameter, and both tables
  carry a `user_id` column, so this schema is ready for the watcher to
  become multi-user-aware in a later phase. Nothing here is wired up to
  real per-user identity yet — `watcher.py` and `deadline_scheduler.py`
  don't pass `user_id` today, so every call falls back to DEFAULT_USER_ID
  and behavior is byte-for-byte identical to before this change. This is
  intentionally schema/API readiness only, not a functional multi-tenancy
  change — see the Phase 1 architecture notes for why threading real
  identity through the watcher's polling loop is deferred to the phase
  that actually builds the OAuth layer supplying that identity.

  This table deliberately stays on SQLite in Phase 1 (unlike the new
  users/google_oauth_credentials tables in classpilot/db.py, which are
  Postgres) — it's low-sensitivity operational dedup cache, not identity
  or credential data, and migrating it now (before the watcher is actually
  multi-user-aware) would add real risk to already-working code for no
  present benefit. Revisit this alongside the watcher rework.
"""

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Used when a caller doesn't pass user_id — i.e. every call site today.
# Keeps today's single-user behavior byte-for-byte unchanged while the
# schema itself is already tenant-isolation-ready.
DEFAULT_USER_ID = "default"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS known_assignments (
    user_id         TEXT NOT NULL DEFAULT 'default',
    course_id       TEXT NOT NULL,
    assignment_id   TEXT NOT NULL,
    title           TEXT,
    due_date_json   TEXT,
    due_time_json   TEXT,
    first_seen_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    last_updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (course_id, assignment_id)
);

CREATE TABLE IF NOT EXISTS sent_reminders (
    user_id         TEXT NOT NULL DEFAULT 'default',
    course_id       TEXT NOT NULL,
    assignment_id   TEXT NOT NULL,
    offset_minutes  INTEGER NOT NULL,
    sent_at         TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (course_id, assignment_id, offset_minutes)
);
"""


class StateStore:
    """
    Thread-safe wrapper around a small SQLite database used for dedup state.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            self._migrate_add_user_id_column(conn, "known_assignments")
            self._migrate_add_user_id_column(conn, "sent_reminders")
            conn.executescript(_SCHEMA)
        logger.debug("State store schema ready at %s", self.db_path)

    @staticmethod
    def _migrate_add_user_id_column(conn: sqlite3.Connection, table: str) -> None:
        """
        Add a `user_id` column to a pre-existing table that predates it
        (i.e. a classpilot_state.db from before this change), so upgrading
        doesn't break on "no such column: user_id".

        `user_id` is a plain, non-key column in Phase 1 — the PRIMARY KEY
        stays (course_id, assignment_id) on both migrated and fresh
        tables, unchanged from before this column existed. It doesn't need
        to be part of the key yet because nothing writes a second, real
        user_id here yet: `watcher.py`/`deadline_scheduler.py` don't pass
        `user_id` today, so every row is DEFAULT_USER_ID, and the existing
        key already correctly prevents duplicates for that single tenant.
        Widening the key to (user_id, course_id, assignment_id) becomes
        necessary once the watcher itself becomes multi-user aware in a
        later phase — do that as part of that change, with real thought
        about migrating any multi-row data that exists by then.
        """
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if not cols:
            return  # table doesn't exist yet — _SCHEMA's CREATE TABLE will make it fresh, correctly
        if "user_id" not in cols:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN user_id TEXT NOT NULL DEFAULT '{DEFAULT_USER_ID}'"
            )
            logger.info("Migrated existing '%s' table: added user_id column", table)

    # ---------- Assignment tracking (Feature 1) ----------

    def get_known_assignment(
        self, course_id: str, assignment_id: str, user_id: str = DEFAULT_USER_ID
    ) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM known_assignments WHERE user_id = ? AND course_id = ? AND assignment_id = ?",
                (user_id, course_id, assignment_id),
            ).fetchone()
            return dict(row) if row else None

    def upsert_assignment(
        self,
        course_id: str,
        assignment_id: str,
        title: str,
        due_date: Optional[dict],
        due_time: Optional[dict],
        user_id: str = DEFAULT_USER_ID,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO known_assignments
                    (user_id, course_id, assignment_id, title, due_date_json, due_time_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(course_id, assignment_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    title = excluded.title,
                    due_date_json = excluded.due_date_json,
                    due_time_json = excluded.due_time_json,
                    last_updated_at = CURRENT_TIMESTAMP
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

    # ---------- Reminder dedup (Feature 3, used later) ----------

    def has_sent_reminder(
        self, course_id: str, assignment_id: str, offset_minutes: int, user_id: str = DEFAULT_USER_ID
    ) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM sent_reminders
                WHERE user_id = ? AND course_id = ? AND assignment_id = ? AND offset_minutes = ?
                """,
                (user_id, course_id, assignment_id, offset_minutes),
            ).fetchone()
            return row is not None

    def mark_reminder_sent(
        self, course_id: str, assignment_id: str, offset_minutes: int, user_id: str = DEFAULT_USER_ID
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO sent_reminders (user_id, course_id, assignment_id, offset_minutes)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, course_id, assignment_id, offset_minutes),
            )

