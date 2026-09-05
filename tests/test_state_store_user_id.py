"""
tests/test_state_store_user_id.py

Tests for the Phase 1 additive changes to classpilot/state_store.py:
  - Every method accepts an optional user_id, defaulting to DEFAULT_USER_ID
    so existing call sites (watcher.py, deadline_scheduler.py — unchanged
    in this phase) behave byte-for-byte as before.
  - A pre-existing (pre-Phase-1) SQLite database that lacks the user_id
    column upgrades cleanly via an ALTER TABLE migration, without losing
    data or breaking on "no such column: user_id".
  - user_id actually scopes reads/writes when a caller does pass it
    explicitly (schema readiness for a later phase).

Pure unittest, real (temp-file) SQLite — no mocking needed, matching the
existing StateStore test style in tests/test_feature2.py/test_feature3.py.
"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from classpilot.state_store import StateStore, DEFAULT_USER_ID


def _tmp_db_path() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)  # StateStore creates it fresh
    return path


class TestDefaultUserIdBackwardCompatibility(unittest.TestCase):
    """Calling every method exactly as watcher.py/deadline_scheduler.py do
    today (no user_id argument) must behave identically to before this
    change — this is the core 'preserve current single-user flow' check."""

    def setUp(self):
        self.db = _tmp_db_path()
        self.store = StateStore(self.db)

    def tearDown(self):
        if os.path.exists(self.db):
            os.remove(self.db)

    def test_upsert_and_get_known_assignment_without_user_id(self):
        self.store.upsert_assignment("c1", "a1", "Essay", None, None)
        result = self.store.get_known_assignment("c1", "a1")
        self.assertIsNotNone(result)
        self.assertEqual(result["title"], "Essay")
        self.assertEqual(result["user_id"], DEFAULT_USER_ID)

    def test_reminder_dedup_without_user_id(self):
        self.assertFalse(self.store.has_sent_reminder("c1", "a1", 60))
        self.store.mark_reminder_sent("c1", "a1", 60)
        self.assertTrue(self.store.has_sent_reminder("c1", "a1", 60))

    def test_upsert_twice_updates_not_duplicates(self):
        self.store.upsert_assignment("c1", "a1", "v1", None, None)
        self.store.upsert_assignment("c1", "a1", "v2", None, None)
        result = self.store.get_known_assignment("c1", "a1")
        self.assertEqual(result["title"], "v2")


class TestExplicitUserIdScoping(unittest.TestCase):
    """Schema readiness: when a caller DOES pass user_id, it actually
    scopes the row — proving the column is real and queryable, ready for
    a later phase to wire up to real per-student identity."""

    def setUp(self):
        self.db = _tmp_db_path()
        self.store = StateStore(self.db)

    def tearDown(self):
        if os.path.exists(self.db):
            os.remove(self.db)

    def test_known_assignment_stores_the_given_user_id(self):
        self.store.upsert_assignment("c1", "a1", "Essay", None, None, user_id="alice")
        result = self.store.get_known_assignment("c1", "a1", user_id="alice")
        self.assertEqual(result["user_id"], "alice")

    def test_get_known_assignment_with_wrong_user_id_returns_none(self):
        """Even though (course_id, assignment_id) is still the physical
        primary key in Phase 1 (see state_store.py's migration docstring
        for why), a read filtered by a *different* user_id must not
        return another user's row."""
        self.store.upsert_assignment("c1", "a1", "Essay", None, None, user_id="alice")
        result = self.store.get_known_assignment("c1", "a1", user_id="bob")
        self.assertIsNone(result)

    def test_reminder_dedup_scoped_by_user_id(self):
        self.store.mark_reminder_sent("c1", "a1", 60, user_id="alice")
        self.assertTrue(self.store.has_sent_reminder("c1", "a1", 60, user_id="alice"))
        self.assertFalse(self.store.has_sent_reminder("c1", "a1", 60, user_id="bob"))

    def test_default_user_id_constant_value(self):
        """Documented, stable sentinel — pinning this in a test guards
        against an accidental rename silently changing existing rows'
        effective identity."""
        self.assertEqual(DEFAULT_USER_ID, "default")


class TestPreExistingDatabaseMigration(unittest.TestCase):
    """
    The critical safety check: a classpilot_state.db created by the
    PRE-Phase-1 schema (no user_id column at all) must upgrade cleanly —
    no data loss, no crash — the first time it's opened with the new code.
    """

    def setUp(self):
        self.db = _tmp_db_path()
        # Manually create the table using the *old* (pre-Phase-1) schema,
        # bypassing StateStore entirely, then insert data the old way.
        conn = sqlite3.connect(self.db)
        conn.executescript(
            """
            CREATE TABLE known_assignments (
                course_id       TEXT NOT NULL,
                assignment_id   TEXT NOT NULL,
                title           TEXT,
                due_date_json   TEXT,
                due_time_json   TEXT,
                first_seen_at   TEXT DEFAULT CURRENT_TIMESTAMP,
                last_updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (course_id, assignment_id)
            );
            CREATE TABLE sent_reminders (
                course_id       TEXT NOT NULL,
                assignment_id   TEXT NOT NULL,
                offset_minutes  INTEGER NOT NULL,
                sent_at         TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (course_id, assignment_id, offset_minutes)
            );
            """
        )
        conn.execute(
            "INSERT INTO known_assignments (course_id, assignment_id, title) "
            "VALUES ('c1', 'a1', 'Pre-existing essay')"
        )
        conn.execute(
            "INSERT INTO sent_reminders (course_id, assignment_id, offset_minutes) "
            "VALUES ('c1', 'a1', 1440)"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        if os.path.exists(self.db):
            os.remove(self.db)

    def test_old_database_opens_without_error(self):
        # Constructing StateStore against the pre-Phase-1 file must not raise.
        StateStore(self.db)

    def test_pre_existing_row_is_preserved_after_migration(self):
        store = StateStore(self.db)
        result = store.get_known_assignment("c1", "a1")
        self.assertIsNotNone(result)
        self.assertEqual(result["title"], "Pre-existing essay")

    def test_pre_existing_row_gets_default_user_id(self):
        store = StateStore(self.db)
        result = store.get_known_assignment("c1", "a1")
        self.assertEqual(result["user_id"], DEFAULT_USER_ID)

    def test_pre_existing_reminder_dedup_preserved(self):
        store = StateStore(self.db)
        self.assertTrue(store.has_sent_reminder("c1", "a1", 1440))

    def test_new_writes_after_migration_work_normally(self):
        store = StateStore(self.db)
        store.upsert_assignment("c2", "a2", "New essay", None, None)
        result = store.get_known_assignment("c2", "a2")
        self.assertEqual(result["title"], "New essay")

    def test_migration_is_idempotent(self):
        """Opening an already-migrated database a second time must not
        error (ALTER TABLE ADD COLUMN must not be attempted twice)."""
        StateStore(self.db)
        StateStore(self.db)  # second open — must not raise "duplicate column"

    def test_migration_does_not_alter_row_count(self):
        conn = sqlite3.connect(self.db)
        before = conn.execute("SELECT COUNT(*) FROM known_assignments").fetchone()[0]
        conn.close()

        StateStore(self.db)

        conn = sqlite3.connect(self.db)
        after = conn.execute("SELECT COUNT(*) FROM known_assignments").fetchone()[0]
        conn.close()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
