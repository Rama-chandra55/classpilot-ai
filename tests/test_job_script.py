import os
import sys
import unittest
from unittest.mock import patch, MagicMock
from concurrent.futures import ThreadPoolExecutor



from classpilot.db import init_schema
import classpilot.db as db
from classpilot.state_store import StateStore
from classpilot.user_store import UserStore
from classpilot.job import main

class TestJobExecution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
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
        with db.get_connection() as conn:
            conn.execute("TRUNCATE watcher_configs, sent_reminders, known_assignments, users CASCADE")
        
        self.store = StateStore()
        self.user_store = UserStore()
        
        self.user_a_id = "00000000-0000-0000-0000-00000000000a"
        self.user_b_id = "00000000-0000-0000-0000-00000000000b"
        
        with db.get_connection() as conn:
            conn.execute("INSERT INTO users (id, google_sub, email) VALUES (%s, %s, %s)", (self.user_a_id, "sub-a", "user_a@example.com"))
            conn.execute("INSERT INTO users (id, google_sub, email) VALUES (%s, %s, %s)", (self.user_b_id, "sub-b", "user_b@example.com"))

    @patch("classpilot.job.get_authorized_credentials")
    @patch("classpilot.job.build_user_services")
    def test_job_polls_only_enabled_users(self, mock_build, mock_get_creds):
        self.store.set_watcher_enabled(self.user_a_id, True)
        self.store.set_watcher_enabled(self.user_b_id, False)
        
        mock_creds_a = MagicMock()
        mock_get_creds.side_effect = lambda uid, _: mock_creds_a if uid == self.user_a_id else None
        
        mock_svc_a = MagicMock()
        mock_build.return_value = mock_svc_a
        
        main()
        
        # User A was polled
        mock_build.assert_called_once_with(self.user_a_id, mock_creds_a, to_email="user_a@example.com")
        mock_svc_a.run_poll_cycle.assert_called_once()
        
        from unittest.mock import ANY
        # User B was skipped
        mock_get_creds.assert_called_once_with(self.user_a_id, ANY)  # Not called for B

    @patch("classpilot.services.create_llm_provider")
    @patch("classpilot.services.create_notifier")
    @patch("classpilot.job.get_authorized_credentials")
    def test_concurrent_job_workers_only_notify_once(self, mock_creds, mock_notifier_factory, mock_llm_factory):
        self.store.set_watcher_enabled(self.user_a_id, True)
        
        mock_creds.return_value = MagicMock()
        mock_notifier = MagicMock()
        mock_notifier_factory.return_value = mock_notifier
        mock_llm_factory.return_value = MagicMock()
        
        # We need the real run_poll_cycle to execute to test the DB concurrency claim.
        # But we don't want to actually hit Google Classroom.
        with patch("classpilot.study_client.fetch_courses") as mock_list_classes, \
             patch("classpilot.study_client.fetch_assignments") as mock_list_coursework:
            
            mock_list_classes.return_value = [{"id": "c1", "name": "Course 1"}]
            mock_list_coursework.return_value = [
                {
                    "id": "a1",
                    "title": "Assignment 1",
                    "dueTime": {"hours": 23, "minutes": 59},
                    "dueDate": {"year": 2099, "month": 12, "day": 31}, # far future
                }
            ]
            
            # Run 5 concurrent background jobs trying to process the exact same user payload
            def worker():
                main()
                
            with ThreadPoolExecutor(max_workers=5) as p:
                list(p.map(lambda _: worker(), range(5)))
                
        # The mock_notifier should have only sent EXACTLY ONE notification for the new assignment,
        # despite 5 workers racing to process it.
        # It's an atomic claim: first one inserts returning 1, others get None, only the winner sends the email.
        self.assertEqual(mock_notifier.send.call_count, 1)

    @patch("classpilot.services.create_llm_provider")
    @patch("classpilot.services.create_notifier")
    @patch("classpilot.job.get_authorized_credentials")
    def test_concurrent_job_workers_only_send_deadline_reminder_once(self, mock_creds, mock_notifier_factory, mock_llm_factory):
        # Insert the assignment directly first so it's NOT a "new" assignment.
        self.store.record_new_assignment("c1", "a1", "Assgn", {"year": 2026, "month": 10, "day": 10}, {"hours": 12}, user_id=self.user_a_id)
        
        self.store.set_watcher_enabled(self.user_a_id, True)
        mock_creds.return_value = MagicMock()
        mock_notifier = MagicMock()
        mock_notifier_factory.return_value = mock_notifier
        
        # We mock LLM to return some message
        mock_llm = MagicMock()
        mock_llm.generate.return_value = "Reminder body"
        mock_llm_factory.return_value = mock_llm
        
        with patch("classpilot.study_client.fetch_courses") as mock_list_classes, \
             patch("classpilot.study_client.fetch_assignments") as mock_list_coursework, \
             patch("classpilot.deadline_scheduler.datetime") as mock_datetime:
            
            # Pretend it is currently exactly 1 hour before the due date.
            # Due date is 2026-10-10 12:00:00 UTC. 
            import datetime
            due = datetime.datetime(2026, 10, 10, 12, 0, tzinfo=datetime.timezone.utc)
            mock_datetime.now.return_value = due - datetime.timedelta(minutes=60)
            mock_datetime.side_effect = lambda *args, **kw: datetime.datetime(*args, **kw)
            
            mock_list_classes.return_value = [{"id": "c1", "name": "Course 1"}]
            mock_list_coursework.return_value = [
                {
                    "id": "a1",
                    "title": "Assgn",
                    "dueTime": {"hours": 12},
                    "dueDate": {"year": 2026, "month": 10, "day": 10},
                }
            ]
            
            def worker():
                main()
                
            with ThreadPoolExecutor(max_workers=5) as p:
                list(p.map(lambda _: worker(), range(5)))
                
        # Exactly one worker wins the claim for the 60-minute reminder
        self.assertEqual(mock_notifier.send.call_count, 1)

if __name__ == "__main__":
    unittest.main()
