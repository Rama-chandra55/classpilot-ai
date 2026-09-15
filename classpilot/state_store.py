"""
Persistent state store for ClassPilot AI.

Tracks which assignments we've already seen/notified about, and (for Feature 3
later) which deadline reminders have already fired - so restarts and repeated
polling never produce duplicate notifications.

Multi-user isolation (Phase 3):
  known_assignments and sent_reminders are now keyed by
  (user_id, course_id, assignment_id[, offset_minutes]) — user_id is part
  of the PRIMARY KEY, not just a plain column. This matters concretely:
  two students enrolled in the SAME course see the SAME course_id and the
  SAME assignment_id for a shared assignment (Classroom IDs aren't
  per-student). Under the old (course_id, assignment_id)-only key, a
  second student's poll would silently overwrite the first student's row
  (`ON CONFLICT(course_id, assignment_id) DO UPDATE SET user_id =
  excluded.user_id, ...`), and a second student's reminder-sent marker
  could never be recorded at all if the first student's row already
  occupied that key (`INSERT OR IGNORE` blocked by the PK collision) —
  causing duplicate "new assignment" notifications for one student and
  endlessly repeated reminders for the other. Widening the key makes this
  physically impossible: every row is uniquely addressed per student.

  Watchers/schedulers are user-scoped as of Phase 3 (see watcher.py,
  deadline_scheduler.py, services.py) and now pass real user_id values
  here — this is no longer schema-readiness-only, as it was in Phase 1.

  Migration: see _migrate_add_user_id_column (pre-Phase-1 databases that
  predate the user_id column entirely) and _migrate_widen_primary_key
  (Phase 1/2 databases that have the column but not yet in the PRIMARY
  KEY). Both are idempotent — safe to run on every process startup — and
  detected via PRAGMA table_info rather than a version flag, so an
  interrupted/partial upgrade can't leave the schema in an ambiguous
  state.

  This table deliberately stays on SQLite (unlike the users/
  google_oauth_credentials tables in classpilot/db.py, which are
  Postgres) — it's low-sensitivity operational dedup cache, not identity
  or credential data, and there's no present need to add a second
  database dependency just for this.
"""

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Used only as an explicit opt-in default for callers that genuinely have
# no per-user context (e.g. a quick manual/test invocation) — every real
# call site in watcher.py/deadline_scheduler.py passes a real user_id as
# of Phase 3.
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
    PRIMARY KEY (user_id, course_id, assignment_id)
);

CREATE TABLE IF NOT EXISTS sent_reminders (
    user_id         TEXT NOT NULL DEFAULT 'default',
    course_id       TEXT NOT NULL,
    assignment_id   TEXT NOT NULL,
    offset_minutes  INTEGER NOT NULL,
    sent_at         TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, course_id, assignment_id, offset_minutes)
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
            # Order matters: a database old enough to be missing user_id
            # entirely must get the column added before we can consider
            # widening the primary key to include it.
            self._migrate_add_user_id_column(conn, "known_assignments")
            self._migrate_add_user_id_column(conn, "sent_reminders")
            self._migrate_widen_primary_key(
                conn, "known_assignments",
                new_pk=("user_id", "course_id", "assignment_id"),
                columns=("user_id", "course_id", "assignment_id", "title",
                         "due_date_json", "due_time_json", "first_seen_at", "last_updated_at"),
            )
            self._migrate_widen_primary_key(
                conn, "sent_reminders",
                new_pk=("user_id", "course_id", "assignment_id", "offset_minutes"),
                columns=("user_id", "course_id", "assignment_id", "offset_minutes", "sent_at"),
            )
            conn.executescript(_SCHEMA)
        logger.debug("State store schema ready at %s", self.db_path)

    @staticmethod
    def _migrate_add_user_id_column(conn: sqlite3.Connection, table: str) -> None:
        """
        Add a `user_id` column to a pre-Phase-1 table that predates it
        entirely, so upgrading doesn't break on "no such column: user_id".
        Every existing row becomes DEFAULT_USER_ID, matching the only
        identity that could possibly have written it (this table's schema
        had no user dimension at all before this migration existed).
        """
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if not cols:
            return  # table doesn't exist yet — _SCHEMA's CREATE TABLE will make it fresh, correctly
        if "user_id" not in cols:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN user_id TEXT NOT NULL DEFAULT '{DEFAULT_USER_ID}'"
            )
            logger.info("Migrated existing '%s' table: added user_id column", table)

    @staticmethod
    def _migrate_widen_primary_key(
        conn: sqlite3.Connection, table: str, new_pk: tuple[str, ...], columns: tuple[str, ...],
    ) -> None:
        """
        Widen `table`'s PRIMARY KEY to include user_id (Phase 3), if it
        doesn't already. Detected via PRAGMA table_info's `pk` column
        (0 = not part of the key; 1, 2, 3... = position within it) rather
        than a stored schema-version flag, so this is self-verifying and
        safe to run on every startup — an interrupted upgrade just gets
        retried, not skipped.

        SQLite has no ALTER TABLE for primary keys, so this rebuilds the
        table: create a correctly-keyed replacement, copy every row into
        it, drop the old table, rename the replacement into place — done
        as one atomic operation within the caller's transaction. This is
        lossless and collision-free by construction: every existing row
        was already unique under the OLD (narrower) key, so it stays
        unique under the new, WIDER key too — widening a key can only
        ever preserve uniqueness among rows that already satisfied a
        subset of it, never create a new collision.
        """
        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not info:
            return  # table doesn't exist yet — _SCHEMA's CREATE TABLE will make it fresh, correctly

        current_pk_cols = {row[1] for row in info if row[5] > 0}  # row[5] is the `pk` field
        if current_pk_cols == set(new_pk):
            return  # already widened — idempotent no-op

        tmp_table = f"{table}__migrating"
        conn.execute(f"DROP TABLE IF EXISTS {tmp_table}")  # in case a prior attempt was interrupted

        # Build the replacement table's DDL by re-using _SCHEMA's own
        # column definitions for this table, but with the NEW primary key
        # — extracted at call time so this stays a single source of truth
        # rather than a second, hand-copied CREATE TABLE statement.
        create_stmt = _extract_create_table(table, new_pk)
        conn.execute(create_stmt.replace(f"TABLE IF NOT EXISTS {table}", f"TABLE {tmp_table}"))

        col_list = ", ".join(columns)
        conn.execute(f"INSERT INTO {tmp_table} ({col_list}) SELECT {col_list} FROM {table}")
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {tmp_table} RENAME TO {table}")
        logger.info(
            "Migrated existing '%s' table: widened PRIMARY KEY to include user_id (%s)",
            table, ", ".join(new_pk),
        )

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
                ON CONFLICT(user_id, course_id, assignment_id) DO UPDATE SET
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


def _extract_create_table(table: str, new_pk: tuple[str, ...]) -> str:
    """
    Pull `table`'s CREATE TABLE statement out of the module-level _SCHEMA
    (the single source of truth for both tables' column definitions) and
    confirm its PRIMARY KEY matches `new_pk` — used by
    _migrate_widen_primary_key so the migration's replacement table can
    never drift out of sync with the real, current schema definition.
    """
    marker = f"CREATE TABLE IF NOT EXISTS {table} ("
    start = _SCHEMA.index(marker)
    end = _SCHEMA.index(");", start) + 2
    stmt = _SCHEMA[start:end]
    expected_pk = f"PRIMARY KEY ({', '.join(new_pk)})"
    assert expected_pk in stmt, (
        f"_SCHEMA's {table} definition doesn't match the requested PK {new_pk} — "
        "update _SCHEMA and this migration together."
    )
    return stmt


