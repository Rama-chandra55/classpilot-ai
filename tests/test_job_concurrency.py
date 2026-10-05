"""
Tests for Phase 5B Job Architecture concurrency limits.
Ensures optimistic claims and RETURNING 1 constraints work.
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from classpilot.db import init_schema
from classpilot.state_store import StateStore
import classpilot.db as db


class TestJobConcurrency(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # We need a real PostgreSQL test database for atomic queries
        if not os.environ.get("TEST_DATABASE_URL"):
            raise unittest.SkipTest("TEST_DATABASE_URL not set")
            
        os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
        import classpilot.config as config_mod
        config_mod._config = None
        
        db.close_pool()
        db.init_schema()

    @classmethod
    def tearDownClass(cls):
        db.close_pool()

    def setUp(self):
        # Clear out tables before each test
        with db.get_connection() as conn:
            conn.execute("TRUNCATE watcher_configs, sent_reminders, known_assignments, users CASCADE")
        
        self.store = StateStore()
        # Insert a fake user
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO users (id, google_sub, email) VALUES (%s, %s, %s)",
                ("00000000-0000-0000-0000-000000000001", "test-sub-123", "test@example.com")
            )
        self.user_id = "00000000-0000-0000-0000-000000000001"

    def test_new_assignment_concurrent_claim(self):
        """Only one worker can claim a new assignment."""
        # Worker A claims
        won_A = self.store.record_new_assignment(
            "c1", "a1", "Title", {"year": 2026}, {"hours": 12}, user_id=self.user_id
        )
        self.assertTrue(won_A)
        
        # Worker B tries to claim the SAME new assignment
        won_B = self.store.record_new_assignment(
            "c1", "a1", "Title", {"year": 2026}, {"hours": 12}, user_id=self.user_id
        )
        self.assertFalse(won_B)
        
        # Verify db state
        with db.get_connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM known_assignments").fetchone()[0]
            self.assertEqual(count, 1)

    def test_deadline_reminder_concurrent_claim(self):
        """Only one worker can claim a deadline reminder offset."""
        won_A = self.store.mark_reminder_sent("c1", "a1", 60, user_id=self.user_id)
        self.assertTrue(won_A)
        
        won_B = self.store.mark_reminder_sent("c1", "a1", 60, user_id=self.user_id)
        self.assertFalse(won_B)

        with db.get_connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM sent_reminders").fetchone()[0]
            self.assertEqual(count, 1)

    def test_deadline_update_concurrent_claim(self):
        """Only one worker can claim a deadline update."""
        # Initial insert
        self.store.record_new_assignment(
            "c1", "a1", "Title", {"year": 2026}, {"hours": 12}, user_id=self.user_id
        )
        import json
        old_date_json = json.dumps({"year": 2026})
        old_time_json = json.dumps({"hours": 12})
        
        # Worker A claims the update from 2026 -> 2027
        won_A = self.store.update_assignment_deadline(
            "c1", "a1", "Title", {"year": 2027}, {"hours": 12}, 
            old_due_date_json=old_date_json, old_due_time_json=old_time_json, user_id=self.user_id
        )
        self.assertTrue(won_A)
        
        # Worker B tries to claim the SAME update from 2026 -> 2027
        won_B = self.store.update_assignment_deadline(
            "c1", "a1", "Title", {"year": 2027}, {"hours": 12}, 
            old_due_date_json=old_date_json, old_due_time_json=old_time_json, user_id=self.user_id
        )
        self.assertFalse(won_B)

    def test_watcher_config(self):
        """Test enabling and disabling the watcher."""
        self.assertFalse(self.store.get_watcher_enabled(self.user_id))
        self.store.set_watcher_enabled(self.user_id, True)
        self.assertTrue(self.store.get_watcher_enabled(self.user_id))
        self.assertEqual(self.store.get_enabled_watchers(), [self.user_id])
        
        self.store.set_watcher_enabled(self.user_id, False)
        self.assertFalse(self.store.get_watcher_enabled(self.user_id))
        self.assertEqual(self.store.get_enabled_watchers(), [])

if __name__ == "__main__":
    unittest.main()
