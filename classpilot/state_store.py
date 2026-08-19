"""
Persistent state store for ClassPilot AI.

Tracks which assignments we've already seen/notified about, and (for Feature 3
later) which deadline reminders have already fired - so restarts and repeated
polling never produce duplicate notifications.
"""

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS known_assignments (
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
            conn.executescript(_SCHEMA)
        logger.debug("State store schema ready at %s", self.db_path)

    # ---------- Assignment tracking (Feature 1) ----------

    def get_known_assignment(self, course_id: str, assignment_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM known_assignments WHERE course_id = ? AND assignment_id = ?",
                (course_id, assignment_id),
            ).fetchone()
            return dict(row) if row else None

    def upsert_assignment(
        self,
        course_id: str,
        assignment_id: str,
        title: str,
        due_date: Optional[dict],
        due_time: Optional[dict],
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO known_assignments
                    (course_id, assignment_id, title, due_date_json, due_time_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(course_id, assignment_id) DO UPDATE SET
                    title = excluded.title,
                    due_date_json = excluded.due_date_json,
                    due_time_json = excluded.due_time_json,
                    last_updated_at = CURRENT_TIMESTAMP
                """,
                (
                    course_id,
                    assignment_id,
                    title,
                    json.dumps(due_date) if due_date else None,
                    json.dumps(due_time) if due_time else None,
                ),
            )

    # ---------- Reminder dedup (Feature 3, used later) ----------

    def has_sent_reminder(self, course_id: str, assignment_id: str, offset_minutes: int) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM sent_reminders
                WHERE course_id = ? AND assignment_id = ? AND offset_minutes = ?
                """,
                (course_id, assignment_id, offset_minutes),
            ).fetchone()
            return row is not None

    def mark_reminder_sent(self, course_id: str, assignment_id: str, offset_minutes: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO sent_reminders (course_id, assignment_id, offset_minutes)
                VALUES (?, ?, ?)
                """,
                (course_id, assignment_id, offset_minutes),
            )
