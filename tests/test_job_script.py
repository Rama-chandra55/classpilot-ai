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

    @patch("classpilot.job.get_authorized_credentials")
    @patch("classpilot.job.run_diagnostics")
    @patch("classpilot.job.build_user_services")
    def test_job_dry_run_flag(self, mock_build, mock_diagnostics, mock_get_creds):
        self.store.set_watcher_enabled(self.user_a_id, True)
        mock_get_creds.return_value = MagicMock()
        
        with patch.object(sys, "argv", ["job.py", "--dry-run"]):
            main()
            
        mock_diagnostics.assert_called_once()
        mock_build.assert_not_called()


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


class TestJobDiagnostics(unittest.TestCase):
    """
    Unit and regression tests for ClassPilot background job diagnostics (--dry-run).
    These tests do not require a live PostgreSQL database.
    """

    @patch("classpilot.job.logger")
    def test_run_diagnostics_success(self, mock_logger):
        from classpilot.job import run_diagnostics
        from classpilot.events import EventType

        mock_creds = MagicMock()
        mock_config = MagicMock()
        mock_config.llm_provider = "groq"
        mock_config.notifier_backend = "email"
        mock_config.smtp_host = "smtp.example.com"
        mock_config.smtp_port = 587
        mock_config.smtp_user = "user@example.com"
        mock_config.smtp_password = "password"

        with patch("googleapiclient.discovery.build") as mock_build, \
             patch("classpilot.llm.create_llm_provider") as mock_llm_factory, \
             patch("smtplib.SMTP") as mock_smtp:

            mock_client = MagicMock()
            mock_build.return_value = mock_client
            mock_client.courses.return_value.list.return_value.execute.return_value = {
                "courses": [{"id": "c1", "name": "Math", "courseState": "ACTIVE"}]
            }
            mock_client.courses.return_value.courseWork.return_value.list.return_value.execute.return_value = {
                "courseWork": [{"id": "a1", "title": "Math HW"}]
            }

            mock_llm = MagicMock()
            mock_llm.generate_message.return_value = "Diagnostic test response"
            mock_llm_factory.return_value = mock_llm

            run_diagnostics("user_1", mock_creds, mock_config)

        # 1. Classroom verification: build called with classroom v1 and credentials
        mock_build.assert_called_once_with("classroom", "v1", credentials=mock_creds)
        mock_client.courses.return_value.list.assert_called_once_with(pageSize=50)
        mock_logger.info.assert_any_call("   [PASS] Successfully fetched %d course(s)", 1)
        mock_client.courses.return_value.courseWork.return_value.list.assert_called_once_with(
            courseId="c1", pageSize=50
        )
        mock_logger.info.assert_any_call(
            "   [PASS] Course '%s' (id: %s): Successfully retrieved %d assignment(s)",
            "Math", "c1", 1
        )
        mock_logger.info.assert_any_call(
            "   Classroom coursework summary for user %s: %d passed, %d failed, %d skipped",
            "user_1", 1, 0, 0
        )

        # 2. LLM verification: uses llm_provider and actual generate_message signature
        mock_llm_factory.assert_called_once_with(mock_config)
        mock_llm.generate_message.assert_called_once_with(EventType.NEW_ASSIGNMENT, "Diagnostic Test Assignment")
        mock_logger.info.assert_any_call("   [PASS] Successfully generated text using %s", "groq")

        # 3. SMTP verification: authenticated without sending email
        mock_smtp.assert_called_once_with("smtp.example.com", 587, timeout=15)
        smtp_instance = mock_smtp.return_value.__enter__.return_value
        smtp_instance.starttls.assert_called_once()
        smtp_instance.login.assert_called_once_with("user@example.com", "password")
        smtp_instance.sendmail.assert_not_called()
        mock_logger.info.assert_any_call("   [PASS] Successfully connected and authenticated to SMTP server")

    @patch("classpilot.job.logger")
    def test_run_diagnostics_coursework_empty_list(self, mock_logger):
        """Empty coursework list is reported as [PASS] with 0 assignments and counted in summary."""
        from classpilot.job import run_diagnostics

        mock_creds = MagicMock()
        mock_config = MagicMock()
        mock_config.llm_provider = "groq"
        mock_config.notifier_backend = "email"
        mock_config.smtp_host = "smtp.example.com"
        mock_config.smtp_port = 587
        mock_config.smtp_user = "user@example.com"
        mock_config.smtp_password = "password"

        with patch("googleapiclient.discovery.build") as mock_build, \
             patch("classpilot.llm.create_llm_provider") as mock_llm_factory, \
             patch("smtplib.SMTP"):

            mock_client = MagicMock()
            mock_build.return_value = mock_client
            mock_client.courses.return_value.list.return_value.execute.return_value = {
                "courses": [{"id": "c2", "name": "History", "courseState": "ACTIVE"}]
            }
            mock_client.courses.return_value.courseWork.return_value.list.return_value.execute.return_value = {
                "courseWork": []
            }

            mock_llm = MagicMock()
            mock_llm.generate_message.return_value = "Diagnostic test response"
            mock_llm_factory.return_value = mock_llm

            run_diagnostics("user_1", mock_creds, mock_config)

        mock_logger.info.assert_any_call(
            "   [PASS] Course '%s' (id: %s): 0 assignments found (empty coursework list)",
            "History", "c2"
        )
        mock_logger.info.assert_any_call(
            "   Classroom coursework summary for user %s: %d passed, %d failed, %d skipped",
            "user_1", 1, 0, 0
        )

    @patch("classpilot.job.logger")
    def test_run_diagnostics_coursework_http_403_error(self, mock_logger):
        """HTTP 403 error on coursework is reported as [FAIL] with permission denied details."""
        from classpilot.job import run_diagnostics

        mock_creds = MagicMock()
        mock_config = MagicMock()
        mock_config.llm_provider = "groq"
        mock_config.notifier_backend = "email"
        mock_config.smtp_host = "smtp.example.com"
        mock_config.smtp_port = 587
        mock_config.smtp_user = "user@example.com"
        mock_config.smtp_password = "password"

        with patch("googleapiclient.discovery.build") as mock_build, \
             patch("classpilot.llm.create_llm_provider") as mock_llm_factory, \
             patch("smtplib.SMTP"):

            mock_client = MagicMock()
            mock_build.return_value = mock_client
            mock_client.courses.return_value.list.return_value.execute.return_value = {
                "courses": [{"id": "c3", "name": "Restricted Physics", "courseState": "ACTIVE"}]
            }

            # Simulate HTTP 403 HttpError
            mock_resp = MagicMock()
            mock_resp.status = 403
            http_err = Exception("HttpError 403: The caller does not have permission")
            http_err.resp = mock_resp
            http_err.reason = "Permission denied for coursework"
            mock_client.courses.return_value.courseWork.return_value.list.return_value.execute.side_effect = http_err

            mock_llm = MagicMock()
            mock_llm.generate_message.return_value = "Diagnostic test response"
            mock_llm_factory.return_value = mock_llm

            run_diagnostics("user_1", mock_creds, mock_config)

        mock_logger.error.assert_any_call(
            "   [FAIL] Course '%s' (id: %s): HTTP 403 Forbidden - %s",
            "Restricted Physics", "c3", "Permission denied for coursework"
        )
        mock_logger.info.assert_any_call(
            "   Classroom coursework summary for user %s: %d passed, %d failed, %d skipped",
            "user_1", 0, 1, 0
        )
        # LLM and SMTP diagnostics still run
        mock_logger.info.assert_any_call("   [PASS] Successfully generated text using %s", "groq")
        mock_logger.info.assert_any_call("   [PASS] Successfully connected and authenticated to SMTP server")

    @patch("classpilot.job.logger")
    def test_run_diagnostics_coursework_skips_inactive_course(self, mock_logger):
        """Non-active courses (e.g. ARCHIVED) are reported as [SKIP] and not queried for coursework."""
        from classpilot.job import run_diagnostics

        mock_creds = MagicMock()
        mock_config = MagicMock()
        mock_config.llm_provider = "groq"
        mock_config.notifier_backend = "email"
        mock_config.smtp_host = "smtp.example.com"
        mock_config.smtp_port = 587
        mock_config.smtp_user = "user@example.com"
        mock_config.smtp_password = "password"

        with patch("googleapiclient.discovery.build") as mock_build, \
             patch("classpilot.llm.create_llm_provider") as mock_llm_factory, \
             patch("smtplib.SMTP"):

            mock_client = MagicMock()
            mock_build.return_value = mock_client
            mock_client.courses.return_value.list.return_value.execute.return_value = {
                "courses": [{"id": "c4", "name": "Old Chemistry", "courseState": "ARCHIVED"}]
            }

            mock_llm = MagicMock()
            mock_llm.generate_message.return_value = "Diagnostic test response"
            mock_llm_factory.return_value = mock_llm

            run_diagnostics("user_1", mock_creds, mock_config)

        mock_client.courses.return_value.courseWork.return_value.list.assert_not_called()
        mock_logger.info.assert_any_call(
            "   [SKIP] Course '%s' (id: %s): Course is not active (state: %s)",
            "Old Chemistry", "c4", "ARCHIVED"
        )
        mock_logger.info.assert_any_call(
            "   Classroom coursework summary for user %s: %d passed, %d failed, %d skipped",
            "user_1", 0, 0, 1
        )

    @patch("classpilot.job.logger")
    def test_run_diagnostics_coursework_pagination_capped(self, mock_logger):
        """Coursework pagination follows nextPageToken and caps at max_coursework_pages."""
        from classpilot.job import run_diagnostics

        mock_creds = MagicMock()
        mock_config = MagicMock()
        mock_config.llm_provider = "groq"
        mock_config.notifier_backend = "email"
        mock_config.smtp_host = "smtp.example.com"
        mock_config.smtp_port = 587
        mock_config.smtp_user = "user@example.com"
        mock_config.smtp_password = "password"

        with patch("googleapiclient.discovery.build") as mock_build, \
             patch("classpilot.llm.create_llm_provider") as mock_llm_factory, \
             patch("smtplib.SMTP"):

            mock_client = MagicMock()
            mock_build.return_value = mock_client
            mock_client.courses.return_value.list.return_value.execute.return_value = {
                "courses": [{"id": "c5", "name": "Huge Course", "courseState": "ACTIVE"}]
            }

            # Return 4 pages with nextPageTokens
            page1 = {"courseWork": [{"id": "a1"}], "nextPageToken": "token2"}
            page2 = {"courseWork": [{"id": "a2"}], "nextPageToken": "token3"}
            page3 = {"courseWork": [{"id": "a3"}], "nextPageToken": "token4"}
            page4 = {"courseWork": [{"id": "a4"}], "nextPageToken": "token5"}
            mock_client.courses.return_value.courseWork.return_value.list.return_value.execute.side_effect = [
                page1, page2, page3, page4
            ]

            mock_llm = MagicMock()
            mock_llm.generate_message.return_value = "Diagnostic test response"
            mock_llm_factory.return_value = mock_llm

            run_diagnostics("user_1", mock_creds, mock_config)

        # Capped at max 3 pages (a1, a2, a3 = 3 assignments)
        mock_logger.info.assert_any_call(
            "   [PASS] Course '%s' (id: %s): Successfully retrieved %d assignment(s)",
            "Huge Course", "c5", 3
        )

    @patch("classpilot.job.logger")
    def test_run_diagnostics_sanitizes_tokens_and_secrets(self, mock_logger):
        """Error messages during coursework fetch never log API keys or bearer tokens."""
        from classpilot.job import run_diagnostics

        mock_creds = MagicMock()
        mock_config = MagicMock()
        mock_config.llm_provider = "groq"
        mock_config.notifier_backend = "email"
        mock_config.smtp_host = "smtp.example.com"
        mock_config.smtp_port = 587
        mock_config.smtp_user = "user@example.com"
        mock_config.smtp_password = "password"

        secret_key = "AIzaSySuperSecretApiKey1234567890123"
        secret_token = "ya29.a0AfH6SecretBearerToken123"

        with patch("googleapiclient.discovery.build") as mock_build, \
             patch("classpilot.llm.create_llm_provider") as mock_llm_factory, \
             patch("smtplib.SMTP"):

            mock_client = MagicMock()
            mock_build.return_value = mock_client
            mock_client.courses.return_value.list.return_value.execute.return_value = {
                "courses": [{"id": "c6", "name": "Secret Course", "courseState": "ACTIVE"}]
            }

            mock_client.courses.return_value.courseWork.return_value.list.return_value.execute.side_effect = (
                Exception(f"Error at https://api.com?key={secret_key} with Bearer {secret_token}")
            )

            mock_llm = MagicMock()
            mock_llm.generate_message.return_value = "Diagnostic test response"
            mock_llm_factory.return_value = mock_llm

            run_diagnostics("user_1", mock_creds, mock_config)

        for call in mock_logger.error.call_args_list:
            log_str = " ".join(str(arg) for arg in call.args)
            assert secret_key not in log_str
            assert secret_token not in log_str
            assert "[REDACTED]" in log_str or "[REDACTED_API_KEY]" in log_str


    @patch("classpilot.job.logger")
    def test_run_diagnostics_with_real_classpilot_config(self, mock_logger):
        """Regression test: ClassPilotConfig defines llm_provider, NOT llm_backend."""
        from classpilot.job import run_diagnostics
        from classpilot.config import ClassPilotConfig

        config = ClassPilotConfig(
            llm_provider="groq",
            llm_api_key="test-key",
            llm_model="llama-3.1-8b-instant",
            notifier_backend="email",
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_user="test@example.com",
            smtp_password="secret-password",
        )
        # Ensure 'llm_backend' does NOT exist on the real config
        self.assertFalse(hasattr(config, "llm_backend"))

        mock_creds = MagicMock()
        with patch("googleapiclient.discovery.build") as mock_build, \
             patch("classpilot.llm.create_llm_provider") as mock_llm_factory, \
             patch("smtplib.SMTP"):

            mock_client = MagicMock()
            mock_build.return_value = mock_client
            mock_client.courses().list().execute.return_value = {"courses": []}

            mock_llm = MagicMock()
            mock_llm.generate_message.return_value = "Generated message"
            mock_llm_factory.return_value = mock_llm

            # Must run without raising AttributeError: 'ClassPilotConfig' object has no attribute 'llm_backend'
            run_diagnostics("user_1", mock_creds, config)

        mock_logger.info.assert_any_call("   [PASS] Successfully fetched %d course(s)", 0)
        mock_logger.info.assert_any_call("   [PASS] Successfully generated text using %s", "groq")

    @patch("classpilot.job.logger")
    def test_run_diagnostics_with_gemini_provider(self, mock_logger):
        """Verify diagnostics flow functions with LLM_PROVIDER=gemini."""
        from classpilot.job import run_diagnostics
        from classpilot.config import ClassPilotConfig

        config = ClassPilotConfig(
            llm_provider="gemini",
            llm_api_key="test-gemini-key",
            llm_model="gemini-2.5-flash-lite",
            notifier_backend="email",
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_user="test@example.com",
            smtp_password="password",
        )

        mock_creds = MagicMock()
        with patch("googleapiclient.discovery.build") as mock_build, \
             patch("classpilot.llm.create_llm_provider") as mock_llm_factory, \
             patch("smtplib.SMTP"):

            mock_client = MagicMock()
            mock_build.return_value = mock_client
            mock_client.courses.return_value.list.return_value.execute.return_value = {"courses": []}

            mock_llm = MagicMock()
            mock_llm.generate_message.return_value = "Gemini notification"
            mock_llm_factory.return_value = mock_llm

            run_diagnostics("user_1", mock_creds, config)

        mock_logger.info.assert_any_call("   [PASS] Successfully generated text using %s", "gemini")

    @patch("classpilot.job.logger")
    def test_classroom_failure_does_not_prevent_llm_and_smtp(self, mock_logger):
        """Regression test: Classroom failure must not block Groq/LLM or SMTP diagnostics."""
        from classpilot.job import run_diagnostics

        mock_creds = MagicMock()
        mock_config = MagicMock()
        mock_config.llm_provider = "groq"
        mock_config.notifier_backend = "email"
        mock_config.smtp_host = "smtp.example.com"
        mock_config.smtp_port = 587
        mock_config.smtp_user = "u"
        mock_config.smtp_password = "p"

        with patch("googleapiclient.discovery.build", side_effect=Exception("Classroom API quota exceeded")), \
             patch("classpilot.llm.create_llm_provider") as mock_llm_factory, \
             patch("smtplib.SMTP"):

            mock_llm = MagicMock()
            mock_llm.generate_message.return_value = "LLM ok"
            mock_llm_factory.return_value = mock_llm

            run_diagnostics("user_1", mock_creds, mock_config)

        # Classroom failed but was caught and logged
        mock_logger.error.assert_any_call("   [FAIL] Classroom fetch failed: %s", unittest.mock.ANY)

        # Groq and SMTP diagnostics still executed successfully
        mock_logger.info.assert_any_call("   [PASS] Successfully generated text using %s", "groq")
        mock_logger.info.assert_any_call("   [PASS] Successfully connected and authenticated to SMTP server")

    @patch("classpilot.job.logger")
    def test_llm_failure_does_not_prevent_smtp(self, mock_logger):
        """Regression test: LLM failure must not block SMTP diagnostics."""
        from classpilot.job import run_diagnostics

        mock_creds = MagicMock()
        mock_config = MagicMock()
        mock_config.llm_provider = "groq"
        mock_config.notifier_backend = "email"
        mock_config.smtp_host = "smtp.example.com"
        mock_config.smtp_port = 587
        mock_config.smtp_user = "u"
        mock_config.smtp_password = "p"

        with patch("googleapiclient.discovery.build") as mock_build, \
             patch("classpilot.llm.create_llm_provider", side_effect=Exception("Groq API rate limit exceeded")), \
             patch("smtplib.SMTP"):

            mock_client = MagicMock()
            mock_build.return_value = mock_client
            mock_client.courses().list().execute.return_value = {"courses": [{"id": "1"}]}

            run_diagnostics("user_1", mock_creds, mock_config)

        mock_logger.info.assert_any_call("   [PASS] Successfully fetched %d course(s)", 1)
        mock_logger.error.assert_any_call("   [FAIL] LLM verification failed: %s", unittest.mock.ANY)
        mock_logger.info.assert_any_call("   [PASS] Successfully connected and authenticated to SMTP server")

    @patch("classpilot.job.logger")
    def test_llm_empty_response_failure(self, mock_logger):
        """LLM returning empty string is treated as a diagnostic failure."""
        from classpilot.job import run_diagnostics

        mock_creds = MagicMock()
        mock_config = MagicMock()
        mock_config.llm_provider = "groq"
        mock_config.notifier_backend = "email"
        mock_config.smtp_host = "smtp.example.com"
        mock_config.smtp_port = 587
        mock_config.smtp_user = "u"
        mock_config.smtp_password = "p"

        with patch("googleapiclient.discovery.build"), \
             patch("classpilot.llm.create_llm_provider") as mock_llm_factory, \
             patch("smtplib.SMTP"):

            mock_llm = MagicMock()
            mock_llm.generate_message.return_value = ""
            mock_llm_factory.return_value = mock_llm

            run_diagnostics("user_1", mock_creds, mock_config)

        mock_logger.error.assert_any_call("   [FAIL] LLM verification failed: %s", unittest.mock.ANY)

    @patch("classpilot.job.logger")
    def test_smtp_failure_handling(self, mock_logger):
        """SMTP authentication failure is caught and logged as [FAIL]."""
        from classpilot.job import run_diagnostics

        mock_creds = MagicMock()
        mock_config = MagicMock()
        mock_config.llm_provider = "groq"
        mock_config.notifier_backend = "email"
        mock_config.smtp_host = "smtp.example.com"
        mock_config.smtp_port = 587
        mock_config.smtp_user = "u"
        mock_config.smtp_password = "p"

        with patch("googleapiclient.discovery.build"), \
             patch("classpilot.llm.create_llm_provider") as mock_llm_factory, \
             patch("smtplib.SMTP", side_effect=Exception("SMTP auth error")):

            mock_llm = MagicMock()
            mock_llm.generate_message.return_value = "ok"
            mock_llm_factory.return_value = mock_llm

            run_diagnostics("user_1", mock_creds, mock_config)

        mock_logger.error.assert_any_call("   [FAIL] SMTP verification failed: %s", unittest.mock.ANY)

    @patch("classpilot.job.logger")
    def test_smtp_skipped_for_non_email_backend(self, mock_logger):
        """When notifier backend is not email, SMTP check is skipped cleanly."""
        from classpilot.job import run_diagnostics

        mock_creds = MagicMock()
        mock_config = MagicMock()
        mock_config.llm_provider = "groq"
        mock_config.notifier_backend = "webhook"

        with patch("googleapiclient.discovery.build"), \
             patch("classpilot.llm.create_llm_provider") as mock_llm_factory, \
             patch("smtplib.SMTP") as mock_smtp:

            mock_llm = MagicMock()
            mock_llm.generate_message.return_value = "ok"
            mock_llm_factory.return_value = mock_llm

            run_diagnostics("user_1", mock_creds, mock_config)

        mock_smtp.assert_not_called()
        mock_logger.info.assert_any_call(
            "   [SKIP] Notifier backend is '%s', skipping SMTP verification", "webhook"
        )

    def test_classroom_client_module_is_module_not_callable(self):
        """Ensure classpilot.classroom_client.classroom is a module."""
        import types
        from classpilot.classroom_client import classroom
        self.assertIsInstance(classroom, types.ModuleType)

    @patch("classpilot.job.StateStore")
    @patch("classpilot.job.UserStore")
    @patch("classpilot.job.get_authorized_credentials")
    @patch("classpilot.job.run_diagnostics")
    @patch("classpilot.job.build_user_services")
    def test_dry_run_flag_in_main(
        self, mock_build, mock_diagnostics, mock_get_creds, mock_user_store_cls, mock_state_store_cls
    ):
        """Ensure --dry-run invokes run_diagnostics and never touches production build_user_services."""
        mock_state_store = mock_state_store_cls.return_value
        mock_state_store.get_enabled_watchers.return_value = ["user-1"]

        mock_user = MagicMock()
        mock_user.email = "u@example.com"
        mock_user_store = mock_user_store_cls.return_value
        mock_user_store.get_user_by_id.return_value = mock_user

        mock_creds = MagicMock()
        mock_get_creds.return_value = mock_creds

        with patch.object(sys, "argv", ["job.py", "--dry-run"]):
            main()

        mock_diagnostics.assert_called_once()
        mock_build.assert_not_called()


class TestJobEmailNotificationTest(unittest.TestCase):
    """
    Automated regression tests for ClassPilot --test-email mode:
    1. Exactly one email sent on success with [CLASSPILOT TEST] prefix.
    2. Optional course_id parameter searches only that course.
    3. No assignment available exits cleanly with no email sent.
    4. Invalid recipient email rejects before querying.
    5. Google Classroom HTTP 403 handled gracefully.
    6. Gemini generation failure handled gracefully.
    7. SMTP failure handled gracefully.
    8. Never modifies notification history or dedup claims.
    9. Never falls back to production watcher execution.
    """

    def setUp(self):
        from classpilot.config import ClassPilotConfig
        self.config = ClassPilotConfig(
            llm_provider="gemini",
            llm_api_key="test-key",
            llm_model="gemini-2.5-flash-lite",
            notifier_backend="email",
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_user="sender@example.com",
            smtp_password="password",
        )

    @patch("classpilot.job.StateStore")
    @patch("classpilot.job.UserStore")
    @patch("classpilot.job.get_authorized_credentials")
    @patch("googleapiclient.discovery.build")
    @patch("classpilot.llm.create_llm_provider")
    @patch("classpilot.notifier.create_notifier")
    def test_test_email_success_sends_exactly_one_email(
        self, mock_notifier_factory, mock_llm_factory, mock_build,
        mock_get_creds, mock_user_store_cls, mock_state_store_cls
    ):
        """Exactly one email is sent to recipient_email with [CLASSPILOT TEST] prefix."""
        from classpilot.job import run_test_email

        mock_state_store = mock_state_store_cls.return_value
        mock_state_store.get_enabled_watchers.return_value = ["usr-1234"]

        mock_client = MagicMock()
        mock_build.return_value = mock_client
        mock_client.courses.return_value.list.return_value.execute.return_value = {
            "courses": [{"id": "c101", "name": "Calculus", "courseState": "ACTIVE"}]
        }
        mock_client.courses.return_value.courseWork.return_value.list.return_value.execute.return_value = {
            "courseWork": [{
                "id": "a1",
                "title": "Midterm Review",
                "dueDate": {"year": 2026, "month": 10, "day": 15},
                "dueTime": {"hours": 17, "minutes": 0},
            }]
        }

        mock_llm = MagicMock()
        mock_llm.generate_test_notification.return_value = "Calculus Midterm Review is due on 2026-10-15."
        mock_llm_factory.return_value = mock_llm

        mock_notifier = MagicMock()
        mock_notifier_factory.return_value = mock_notifier

        result = run_test_email(self.config, recipient_email="student@example.com")

        self.assertTrue(result)
        mock_notifier_factory.assert_called_once_with(self.config, to_email_override="student@example.com")
        mock_notifier.send.assert_called_once()
        _, kwargs = mock_notifier.send.call_args
        subject = kwargs["subject"]
        body = kwargs["body"]
        self.assertTrue(subject.startswith("[CLASSPILOT TEST]"))
        self.assertIn("Calculus", subject)
        self.assertIn("Midterm Review", subject)
        self.assertIn("Calculus Midterm Review is due on 2026-10-15.", body)
        self.assertIn("2026-10-15 17:00 UTC", body)
        self.assertIn("student@example.com", body)

    @patch("classpilot.job.StateStore")
    @patch("classpilot.job.UserStore")
    @patch("classpilot.job.get_authorized_credentials")
    @patch("googleapiclient.discovery.build")
    @patch("classpilot.llm.create_llm_provider")
    @patch("classpilot.notifier.create_notifier")
    def test_test_email_with_explicit_course_id(
        self, mock_notifier_factory, mock_llm_factory, mock_build,
        mock_get_creds, mock_user_store_cls, mock_state_store_cls
    ):
        """When course_id is supplied, only that course is fetched via courses().get()."""
        from classpilot.job import run_test_email

        mock_state_store = mock_state_store_cls.return_value
        mock_state_store.get_enabled_watchers.return_value = ["usr-1234"]

        mock_client = MagicMock()
        mock_build.return_value = mock_client
        mock_client.courses.return_value.get.return_value.execute.return_value = {
            "id": "c999", "name": "Physics", "courseState": "ACTIVE"
        }
        mock_client.courses.return_value.courseWork.return_value.list.return_value.execute.return_value = {
            "courseWork": [{"id": "a9", "title": "Lab 1"}]
        }

        mock_llm = MagicMock()
        mock_llm.generate_test_notification.return_value = "Physics Lab 1 notification."
        mock_llm_factory.return_value = mock_llm

        mock_notifier = MagicMock()
        mock_notifier_factory.return_value = mock_notifier

        result = run_test_email(self.config, recipient_email="student@example.com", course_id="c999")

        self.assertTrue(result)
        mock_client.courses.return_value.get.assert_called_once_with(id="c999")
        mock_client.courses.return_value.list.assert_not_called()
        mock_notifier.send.assert_called_once()

    @patch("classpilot.job.StateStore")
    @patch("classpilot.job.UserStore")
    @patch("classpilot.job.get_authorized_credentials")
    @patch("googleapiclient.discovery.build")
    @patch("classpilot.llm.create_llm_provider")
    @patch("classpilot.notifier.create_notifier")
    def test_test_email_no_assignment_available(
        self, mock_notifier_factory, mock_llm_factory, mock_build,
        mock_get_creds, mock_user_store_cls, mock_state_store_cls
    ):
        """If no assignment is found, exits clearly without sending any email."""
        from classpilot.job import run_test_email

        mock_state_store = mock_state_store_cls.return_value
        mock_state_store.get_enabled_watchers.return_value = ["usr-1234"]

        mock_client = MagicMock()
        mock_build.return_value = mock_client
        mock_client.courses.return_value.list.return_value.execute.return_value = {
            "courses": [{"id": "c101", "name": "Empty Course", "courseState": "ACTIVE"}]
        }
        mock_client.courses.return_value.courseWork.return_value.list.return_value.execute.return_value = {
            "courseWork": []
        }

        mock_notifier = MagicMock()
        mock_notifier_factory.return_value = mock_notifier

        result = run_test_email(self.config, recipient_email="student@example.com")

        self.assertFalse(result)
        mock_notifier.send.assert_not_called()
        mock_llm_factory.assert_not_called()

    @patch("classpilot.notifier.create_notifier")
    def test_test_email_invalid_recipient_email(self, mock_notifier_factory):
        """Invalid recipient email addresses are rejected before any API calls."""
        from classpilot.job import run_test_email

        invalid_emails = ["not-an-email", "", "   ", "missingdomain@", "@nodomain.com"]
        for inv in invalid_emails:
            result = run_test_email(self.config, recipient_email=inv)
            self.assertFalse(result)
        mock_notifier_factory.assert_not_called()

    @patch("classpilot.job.StateStore")
    @patch("classpilot.job.UserStore")
    @patch("classpilot.job.get_authorized_credentials")
    @patch("googleapiclient.discovery.build")
    @patch("classpilot.notifier.create_notifier")
    def test_test_email_google_classroom_http_403(
        self, mock_notifier_factory, mock_build,
        mock_get_creds, mock_user_store_cls, mock_state_store_cls
    ):
        """HTTP 403 on Google Classroom stops execution and sends no email."""
        from classpilot.job import run_test_email

        mock_state_store = mock_state_store_cls.return_value
        mock_state_store.get_enabled_watchers.return_value = ["usr-1234"]

        mock_client = MagicMock()
        mock_build.return_value = mock_client
        mock_resp = MagicMock(status=403)
        err = Exception("403 Forbidden")
        err.resp = mock_resp
        mock_client.courses.return_value.list.return_value.execute.side_effect = err

        mock_notifier = MagicMock()
        mock_notifier_factory.return_value = mock_notifier

        result = run_test_email(self.config, recipient_email="student@example.com")

        self.assertFalse(result)
        mock_notifier.send.assert_not_called()

    @patch("classpilot.job.StateStore")
    @patch("classpilot.job.UserStore")
    @patch("classpilot.job.get_authorized_credentials")
    @patch("googleapiclient.discovery.build")
    @patch("classpilot.llm.create_llm_provider")
    @patch("classpilot.notifier.create_notifier")
    def test_test_email_gemini_generation_failure(
        self, mock_notifier_factory, mock_llm_factory, mock_build,
        mock_get_creds, mock_user_store_cls, mock_state_store_cls
    ):
        """Gemini generation failure is caught, logged, and sends no email."""
        from classpilot.job import run_test_email

        mock_state_store = mock_state_store_cls.return_value
        mock_state_store.get_enabled_watchers.return_value = ["usr-1234"]

        mock_client = MagicMock()
        mock_build.return_value = mock_client
        mock_client.courses.return_value.list.return_value.execute.return_value = {
            "courses": [{"id": "c101", "name": "Calculus", "courseState": "ACTIVE"}]
        }
        mock_client.courses.return_value.courseWork.return_value.list.return_value.execute.return_value = {
            "courseWork": [{"id": "a1", "title": "Homework"}]
        }

        mock_llm = MagicMock()
        mock_llm.generate_test_notification.side_effect = Exception("Gemini API error")
        mock_llm_factory.return_value = mock_llm

        mock_notifier = MagicMock()
        mock_notifier_factory.return_value = mock_notifier

        result = run_test_email(self.config, recipient_email="student@example.com")

        self.assertFalse(result)
        mock_notifier.send.assert_not_called()

    @patch("classpilot.job.StateStore")
    @patch("classpilot.job.UserStore")
    @patch("classpilot.job.get_authorized_credentials")
    @patch("googleapiclient.discovery.build")
    @patch("classpilot.llm.create_llm_provider")
    @patch("classpilot.notifier.create_notifier")
    def test_test_email_smtp_failure(
        self, mock_notifier_factory, mock_llm_factory, mock_build,
        mock_get_creds, mock_user_store_cls, mock_state_store_cls
    ):
        """SMTP error during sending returns False cleanly."""
        from classpilot.job import run_test_email

        mock_state_store = mock_state_store_cls.return_value
        mock_state_store.get_enabled_watchers.return_value = ["usr-1234"]

        mock_client = MagicMock()
        mock_build.return_value = mock_client
        mock_client.courses.return_value.list.return_value.execute.return_value = {
            "courses": [{"id": "c101", "name": "Calculus", "courseState": "ACTIVE"}]
        }
        mock_client.courses.return_value.courseWork.return_value.list.return_value.execute.return_value = {
            "courseWork": [{"id": "a1", "title": "Homework"}]
        }

        mock_llm = MagicMock()
        mock_llm.generate_test_notification.return_value = "Test notification"
        mock_llm_factory.return_value = mock_llm

        mock_notifier = MagicMock()
        mock_notifier.send.side_effect = Exception("SMTP connection failure")
        mock_notifier_factory.return_value = mock_notifier

        result = run_test_email(self.config, recipient_email="student@example.com")

        self.assertFalse(result)

    @patch("classpilot.job.StateStore")
    @patch("classpilot.job.UserStore")
    @patch("classpilot.job.get_authorized_credentials")
    @patch("googleapiclient.discovery.build")
    @patch("classpilot.llm.create_llm_provider")
    @patch("classpilot.notifier.create_notifier")
    def test_test_email_no_changes_to_notification_history(
        self, mock_notifier_factory, mock_llm_factory, mock_build,
        mock_get_creds, mock_user_store_cls, mock_state_store_cls
    ):
        """Notification claims and history in StateStore are never touched during test email."""
        from classpilot.job import run_test_email

        mock_state_store = mock_state_store_cls.return_value
        mock_state_store.get_enabled_watchers.return_value = ["usr-1234"]

        mock_client = MagicMock()
        mock_build.return_value = mock_client
        mock_client.courses.return_value.list.return_value.execute.return_value = {
            "courses": [{"id": "c101", "name": "Calculus", "courseState": "ACTIVE"}]
        }
        mock_client.courses.return_value.courseWork.return_value.list.return_value.execute.return_value = {
            "courseWork": [{"id": "a1", "title": "Homework"}]
        }

        mock_llm = MagicMock()
        mock_llm.generate_test_notification.return_value = "Test notification"
        mock_llm_factory.return_value = mock_llm

        mock_notifier = MagicMock()
        mock_notifier_factory.return_value = mock_notifier

        run_test_email(self.config, recipient_email="student@example.com")

        # Crucial: verify StateStore mutation methods were NEVER called
        self.assertFalse(hasattr(mock_state_store, "claim_event") and mock_state_store.claim_event.called)
        self.assertFalse(hasattr(mock_state_store, "record_notification") and mock_state_store.record_notification.called)
        mock_state_store.set_watcher_enabled.assert_not_called()

    @patch("classpilot.job.run_test_email", return_value=True)
    @patch("classpilot.job.build_user_services")
    @patch("sys.exit")
    def test_main_test_email_success_exits_0_and_never_runs_watcher(
        self, mock_exit, mock_build_services, mock_run_test_email
    ):
        """When --test-email is passed and succeeds, exits 0 and never triggers watcher."""
        with patch.object(sys, "argv", ["job.py", "--test-email", "--recipient-email", "test@example.com"]):
            main()

        mock_run_test_email.assert_called_once()
        mock_exit.assert_called_once_with(0)
        mock_build_services.assert_not_called()

    @patch("classpilot.job.run_test_email", return_value=False)
    @patch("classpilot.job.build_user_services")
    @patch("sys.exit")
    def test_main_test_email_failure_exits_1_and_never_runs_watcher(
        self, mock_exit, mock_build_services, mock_run_test_email
    ):
        """When --test-email fails (e.g. no assignments), exits 1 and never falls through to watcher."""
        with patch.object(sys, "argv", ["job.py", "--test-email", "--recipient-email", "test@example.com"]):
            main()

        mock_run_test_email.assert_called_once()
        mock_exit.assert_called_once_with(1)
        mock_build_services.assert_not_called()

    @patch("classpilot.job.build_user_services")
    @patch("sys.exit")
    def test_main_test_email_missing_recipient_exits_1_and_never_runs_watcher(
        self, mock_exit, mock_build_services
    ):
        """When --test-email is passed without recipient, exits 1 and never falls through to watcher."""
        import os
        with patch.object(sys, "argv", ["job.py", "--test-email"]), \
             patch.dict(os.environ, {}, clear=True):
            main()

        mock_exit.assert_called_once_with(1)
        mock_build_services.assert_not_called()


if __name__ == "__main__":
    unittest.main()

