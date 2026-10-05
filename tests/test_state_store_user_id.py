"""
tests/test_state_store_user_id.py

Tests for the Phase 5A changes to classpilot/state_store.py:
  - Validates that state_store works correctly against a REAL PostgreSQL database.
  - Tests deduplication logic (known assignments and sent reminders).
  - Validates that concurrent inserts are handled safely (via ON CONFLICT DO NOTHING / UPDATE).
  - Tests user_id isolation (a user cannot read/overwrite another user's dedup state).

Skips gracefully if no Postgres is reachable, just like test_user_store.py.
"""

import os
import sys
import threading
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from classpilot.state_store import StateStore, DEFAULT_USER_ID

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://classpilot:classpilot@127.0.0.1:5432/classpilot_test",
)


def _postgres_available() -> bool:
    try:
        import psycopg
        with psycopg.connect(TEST_DATABASE_URL, connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


_SKIP_REASON = (
    f"No reachable Postgres at {TEST_DATABASE_URL} — set TEST_DATABASE_URL "
    "or start a local Postgres to run these integration tests."
)
_HAS_POSTGRES = _postgres_available()


@unittest.skipUnless(_HAS_POSTGRES, _SKIP_REASON)
class _PostgresStateStoreTestCase(unittest.TestCase):
    """Common setup: point config at the test database, ensure schema exists,
    clean state tables between tests."""

    @classmethod
    def setUpClass(cls):
        os.environ["DATABASE_URL"] = TEST_DATABASE_URL

        import classpilot.config as config_mod
        config_mod._config = None

        import classpilot.db as db_mod
        db_mod.close_pool()
        db_mod.init_schema()

    @classmethod
    def tearDownClass(cls):
        import classpilot.db as db_mod
        db_mod.close_pool()

    def setUp(self):
        # We don't TRUNCATE here globally per the user's request.
        # Instead, we generate unique user IDs per test to naturally isolate them
        # without affecting other concurrently running tests.
        self.store = StateStore()

    @staticmethod
    def _unique_user() -> str:
        return f"test-user-{uuid.uuid4()}"


class TestStateStoreIsolation(_PostgresStateStoreTestCase):
    def test_upsert_and_get_known_assignment(self):
        user1 = self._unique_user()
        self.store.upsert_assignment_metadata("c1", "a1", "Essay", None, None, user_id=user1)
        
        result = self.store.get_known_assignment("c1", "a1", user_id=user1)
        self.assertIsNotNone(result)
        self.assertEqual(result["title"], "Essay")
        self.assertEqual(result["user_id"], user1)

    def test_upsert_twice_updates_not_duplicates(self):
        user1 = self._unique_user()
        self.store.upsert_assignment_metadata("c1", "a1", "v1", None, None, user_id=user1)
        self.store.upsert_assignment_metadata("c1", "a1", "v2", None, None, user_id=user1)
        
        result = self.store.get_known_assignment("c1", "a1", user_id=user1)
        self.assertIsNotNone(result)
        self.assertEqual(result["title"], "v2")

    def test_get_known_assignment_with_wrong_user_returns_none(self):
        user1 = self._unique_user()
        user2 = self._unique_user()
        self.store.upsert_assignment_metadata("c1", "a1", "Essay", None, None, user_id=user1)
        
        result = self.store.get_known_assignment("c1", "a1", user_id=user2)
        self.assertIsNone(result)

    def test_reminder_dedup_scoped_by_user(self):
        user1 = self._unique_user()
        user2 = self._unique_user()
        
        self.assertFalse(self.store.has_sent_reminder("c1", "a1", 60, user_id=user1))
        
        self.store.mark_reminder_sent("c1", "a1", 60, user_id=user1)
        self.assertTrue(self.store.has_sent_reminder("c1", "a1", 60, user_id=user1))
        
        # User2 should not have this reminder sent
        self.assertFalse(self.store.has_sent_reminder("c1", "a1", 60, user_id=user2))


class TestStateStoreConcurrency(_PostgresStateStoreTestCase):
    def test_concurrent_mark_reminder_sent(self):
        """Test that multiple threads attempting to mark the same reminder don't crash."""
        user1 = self._unique_user()
        
        def _mark():
            self.store.mark_reminder_sent("c1", "a2", 30, user_id=user1)

        threads = [threading.Thread(target=_mark) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
            
        self.assertTrue(self.store.has_sent_reminder("c1", "a2", 30, user_id=user1))

    def test_concurrent_upsert_assignment_metadata(self):
        """Test that multiple threads attempting to upsert the same assignment don't crash."""
        user1 = self._unique_user()
        
        def _upsert(i):
            self.store.upsert_assignment_metadata("c1", "a3", f"Title {i}", None, None, user_id=user1)

        threads = [threading.Thread(target=_upsert, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
            
        result = self.store.get_known_assignment("c1", "a3", user_id=user1)
        self.assertIsNotNone(result)
        self.assertTrue(result["title"].startswith("Title"))


if __name__ == "__main__":
    unittest.main()
