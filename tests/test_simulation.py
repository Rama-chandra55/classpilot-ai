"""
tests/test_simulation.py — Regression tests for ClassPilot Watcher Simulation

Verifies:
1. First-run baseline behavior (historical assignments suppressed).
2. Genuine new assignments detected.
3. Assignment updates detected.
4. Deadline reminder windows evaluated.
5. Deduplication (existing assignments & sent reminders suppressed).
6. Repeated simulation runs produce identical results (pure idempotence).
7. Course-level HTTP 403 isolation (skips forbidden course, continues rest).
8. Zero database mutations.
9. Zero emails sent via SMTP.
10. No production watcher execution.
11. Deterministic account selection and failing closed on ambiguity.
12. Workflow dispatch configuration and env var isolation.
"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from classpilot.config import ClassPilotConfig
from classpilot.events import EventType
from classpilot.job import main
from classpilot.simulation import (
    ReadOnlyStateStore,
    SimulationResult,
    format_simulation_summary,
    run_watcher_simulation,
)
from classpilot.state_store import StateStore
from classpilot.watcher import is_historical_assignment


class TestWatcherSimulation(unittest.TestCase):
    def setUp(self):
        self.config = ClassPilotConfig.__new__(ClassPilotConfig)
        self.config.llm_provider = "gemini"
        self.config.llm_model = "gemini-2.5-flash-lite"
        self.config.notifier_backend = "email"
        self.config.smtp_host = "smtp.example.com"
        self.config.smtp_port = 587
        self.config.smtp_user = "user@example.com"
        self.config.smtp_password = "password"
        self.config.notify_to_email = "student@example.com"
        self.config.deadline_alert_offsets_minutes = (24 * 60, 6 * 60, 60, 15)
        self.config.log_level = "INFO"

        self.fixed_now = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)

    # 1. First-run baseline behavior (historical assignments suppressed)
    @patch("classpilot.simulation.StateStore")
    @patch("classpilot.simulation.UserStore")
    @patch("classpilot.simulation.get_authorized_credentials")
    @patch("googleapiclient.discovery.build")
    def test_first_run_baseline_historical_assignments_suppressed(
        self, mock_build, mock_get_creds, mock_user_store_cls, mock_state_store_cls
    ):
        """Historical assignments (past due date) are suppressed on first startup and not notified."""
        mock_state = mock_state_store_cls.return_value
        mock_state.get_enabled_watchers.return_value = ["usr-college-1234"]
        mock_state.get_known_assignment.return_value = None
        mock_state.get_all_assignments.return_value = []
        mock_state.has_sent_reminder.return_value = False

        mock_user = mock_user_store_cls.return_value
        mock_user.get_user_by_id.return_value = MagicMock(id="usr-college-1234")

        mock_client = MagicMock()
        mock_build.return_value = mock_client
        mock_client.courses.return_value.list.return_value.execute.return_value = {
            "courses": [{"id": "c1", "name": "Calculus", "courseState": "ACTIVE"}]
        }

        # Historical assignment: due on 2026-09-01 (before fixed_now 2026-10-15)
        past_assignment = {
            "id": "a-past-1",
            "title": "Old Exam Review",
            "dueDate": {"year": 2026, "month": 9, "day": 1},
            "dueTime": {"hours": 23, "minutes": 59},
        }
        mock_client.courses.return_value.courseWork.return_value.list.return_value.execute.return_value = {
            "courseWork": [past_assignment]
        }

        res = run_watcher_simulation(
            self.config,
            user_id="usr-college-1234",
            now=self.fixed_now,
        )

        self.assertTrue(res.success)
        self.assertEqual(len(res.detected_new_assignments), 0)
        self.assertEqual(len(res.suppressed_historical_assignments), 1)
        self.assertEqual(res.suppressed_historical_assignments[0]["title"], "Old Exam Review")

    # 2. Genuine new assignments
    @patch("classpilot.simulation.StateStore")
    @patch("classpilot.simulation.UserStore")
    @patch("classpilot.simulation.get_authorized_credentials")
    @patch("googleapiclient.discovery.build")
    def test_genuine_new_assignments_detected(
        self, mock_build, mock_get_creds, mock_user_store_cls, mock_state_store_cls
    ):
        """Active/future assignments not in StateStore are detected as new assignment notifications."""
        mock_state = mock_state_store_cls.return_value
        mock_state.get_enabled_watchers.return_value = ["usr-college-1234"]
        mock_state.get_known_assignment.return_value = None
        mock_state.get_all_assignments.return_value = []
        mock_state.has_sent_reminder.return_value = False

        mock_user = mock_user_store_cls.return_value
        mock_user.get_user_by_id.return_value = MagicMock(id="usr-college-1234")

        mock_client = MagicMock()
        mock_build.return_value = mock_client
        mock_client.courses.return_value.list.return_value.execute.return_value = {
            "courses": [{"id": "c1", "name": "Calculus", "courseState": "ACTIVE"}]
        }

        # Future assignment: due in 10 days
        future_assignment = {
            "id": "a-future-1",
            "title": "Problem Set 5",
            "dueDate": {"year": 2026, "month": 10, "day": 25},
            "dueTime": {"hours": 17, "minutes": 0},
        }
        mock_client.courses.return_value.courseWork.return_value.list.return_value.execute.return_value = {
            "courseWork": [future_assignment]
        }

        res = run_watcher_simulation(
            self.config,
            user_id="usr-college-1234",
            now=self.fixed_now,
        )

        self.assertTrue(res.success)
        self.assertEqual(len(res.detected_new_assignments), 1)
        self.assertEqual(res.detected_new_assignments[0]["title"], "Problem Set 5")
        self.assertIn("Problem Set 5", res.detected_new_assignments[0]["predicted_subject"])

    # 3. Assignment updates
    @patch("classpilot.simulation.StateStore")
    @patch("classpilot.simulation.UserStore")
    @patch("classpilot.simulation.get_authorized_credentials")
    @patch("googleapiclient.discovery.build")
    def test_assignment_deadline_updates_detected(
        self, mock_build, mock_get_creds, mock_user_store_cls, mock_state_store_cls
    ):
        """When an assignment is already known but its deadline changed, update is detected."""
        mock_state = mock_state_store_cls.return_value
        mock_state.get_enabled_watchers.return_value = ["usr-college-1234"]
        mock_state.get_known_assignment.return_value = {
            "course_id": "c1",
            "assignment_id": "a-upd-1",
            "title": "Project Phase 1",
            "due_date_json": json.dumps({"year": 2026, "month": 10, "day": 20}),
            "due_time_json": json.dumps({"hours": 12, "minutes": 0}),
        }
        mock_state.get_all_assignments.return_value = []
        mock_state.has_sent_reminder.return_value = False

        mock_user = mock_user_store_cls.return_value
        mock_user.get_user_by_id.return_value = MagicMock(id="usr-college-1234")

        mock_client = MagicMock()
        mock_build.return_value = mock_client
        mock_client.courses.return_value.list.return_value.execute.return_value = {
            "courses": [{"id": "c1", "name": "Calculus", "courseState": "ACTIVE"}]
        }

        # Extended deadline
        changed_assignment = {
            "id": "a-upd-1",
            "title": "Project Phase 1",
            "dueDate": {"year": 2026, "month": 10, "day": 22},
            "dueTime": {"hours": 12, "minutes": 0},
        }
        mock_client.courses.return_value.courseWork.return_value.list.return_value.execute.return_value = {
            "courseWork": [changed_assignment]
        }

        res = run_watcher_simulation(
            self.config,
            user_id="usr-college-1234",
            now=self.fixed_now,
        )

        self.assertTrue(res.success)
        self.assertEqual(len(res.detected_deadline_updates), 1)
        self.assertEqual(res.detected_deadline_updates[0]["title"], "Project Phase 1")
        self.assertIn("Deadline Changed", res.detected_deadline_updates[0]["predicted_subject"])

    # 4. Deadline reminder windows
    @patch("classpilot.simulation.StateStore")
    @patch("classpilot.simulation.UserStore")
    @patch("classpilot.simulation.get_authorized_credentials")
    @patch("googleapiclient.discovery.build")
    def test_deadline_reminder_windows_detected(
        self, mock_build, mock_get_creds, mock_user_store_cls, mock_state_store_cls
    ):
        """Assignments within the reminder window (e.g. 24h, 6h) predict reminders."""
        mock_state = mock_state_store_cls.return_value
        mock_state.get_enabled_watchers.return_value = ["usr-college-1234"]
        # Already known assignment
        mock_state.get_known_assignment.return_value = {
            "course_id": "c1",
            "assignment_id": "a-rem-1",
            "title": "Physics Quiz",
            "due_date_json": json.dumps({"year": 2026, "month": 10, "day": 15}),
            "due_time_json": json.dumps({"hours": 14, "minutes": 0}),
        }
        mock_state.get_all_assignments.return_value = []
        mock_state.has_sent_reminder.return_value = False

        mock_user = mock_user_store_cls.return_value
        mock_user.get_user_by_id.return_value = MagicMock(id="usr-college-1234")

        mock_client = MagicMock()
        mock_build.return_value = mock_client
        mock_client.courses.return_value.list.return_value.execute.return_value = {
            "courses": [{"id": "c1", "name": "Physics", "courseState": "ACTIVE"}]
        }

        # Due in 2 hours (2026-10-15 14:00 UTC vs now 12:00 UTC)
        # 24h and 6h windows have passed (fire_at <= now < due_dt)
        # 60m and 15m windows have not been reached yet (fire_at > now)
        assignment = {
            "id": "a-rem-1",
            "title": "Physics Quiz",
            "dueDate": {"year": 2026, "month": 10, "day": 15},
            "dueTime": {"hours": 14, "minutes": 0},
        }
        mock_client.courses.return_value.courseWork.return_value.list.return_value.execute.return_value = {
            "courseWork": [assignment]
        }

        res = run_watcher_simulation(
            self.config,
            user_id="usr-college-1234",
            now=self.fixed_now,
        )

        self.assertTrue(res.success)
        # 24h and 6h offsets triggered
        self.assertEqual(len(res.detected_deadline_reminders), 2)
        offsets = [r["offset_label"] for r in res.detected_deadline_reminders]
        self.assertIn("24h", offsets)
        self.assertIn("6h", offsets)

    # 5. Deduplication (already known assignments & sent reminders suppressed)
    @patch("classpilot.simulation.StateStore")
    @patch("classpilot.simulation.UserStore")
    @patch("classpilot.simulation.get_authorized_credentials")
    @patch("googleapiclient.discovery.build")
    def test_deduplication_suppresses_already_known_and_already_sent(
        self, mock_build, mock_get_creds, mock_user_store_cls, mock_state_store_cls
    ):
        """Unchanged assignments and already sent reminders are recorded as suppressed."""
        mock_state = mock_state_store_cls.return_value
        mock_state.get_enabled_watchers.return_value = ["usr-college-1234"]
        mock_state.get_known_assignment.return_value = {
            "course_id": "c1",
            "assignment_id": "a-same-1",
            "title": "Lab Report 1",
            "due_date_json": json.dumps({"year": 2026, "month": 10, "day": 15}),
            "due_time_json": json.dumps({"hours": 14, "minutes": 0}),
        }
        mock_state.get_all_assignments.return_value = []
        # Reminders already sent
        mock_state.has_sent_reminder.return_value = True

        mock_user = mock_user_store_cls.return_value
        mock_user.get_user_by_id.return_value = MagicMock(id="usr-college-1234")

        mock_client = MagicMock()
        mock_build.return_value = mock_client
        mock_client.courses.return_value.list.return_value.execute.return_value = {
            "courses": [{"id": "c1", "name": "Physics", "courseState": "ACTIVE"}]
        }
        mock_client.courses.return_value.courseWork.return_value.list.return_value.execute.return_value = {
            "courseWork": [{
                "id": "a-same-1",
                "title": "Lab Report 1",
                "dueDate": {"year": 2026, "month": 10, "day": 15},
                "dueTime": {"hours": 14, "minutes": 0},
            }]
        }

        res = run_watcher_simulation(
            self.config,
            user_id="usr-college-1234",
            now=self.fixed_now,
        )

        self.assertTrue(res.success)
        self.assertEqual(len(res.detected_new_assignments), 0)
        self.assertEqual(len(res.detected_deadline_updates), 0)
        self.assertEqual(len(res.suppressed_existing_assignments), 1)
        self.assertEqual(len(res.suppressed_reminders), 2)  # 24h and 6h were already sent

    # 6. Repeated simulation runs
    @patch("classpilot.simulation.StateStore")
    @patch("classpilot.simulation.UserStore")
    @patch("classpilot.simulation.get_authorized_credentials")
    @patch("googleapiclient.discovery.build")
    def test_repeated_simulation_runs_idempotence(
        self, mock_build, mock_get_creds, mock_user_store_cls, mock_state_store_cls
    ):
        """Running the simulation repeatedly yields identical predictions without residual mutations."""
        mock_state = mock_state_store_cls.return_value
        mock_state.get_enabled_watchers.return_value = ["usr-college-1234"]
        mock_state.get_known_assignment.return_value = None
        mock_state.get_all_assignments.return_value = []
        mock_state.has_sent_reminder.return_value = False

        mock_user = mock_user_store_cls.return_value
        mock_user.get_user_by_id.return_value = MagicMock(id="usr-college-1234")

        mock_client = MagicMock()
        mock_build.return_value = mock_client
        mock_client.courses.return_value.list.return_value.execute.return_value = {
            "courses": [{"id": "c1", "name": "Calculus", "courseState": "ACTIVE"}]
        }
        mock_client.courses.return_value.courseWork.return_value.list.return_value.execute.return_value = {
            "courseWork": [{
                "id": "a1",
                "title": "Homework 1",
                "dueDate": {"year": 2026, "month": 10, "day": 25},
            }]
        }

        res1 = run_watcher_simulation(self.config, user_id="usr-college-1234", now=self.fixed_now)
        res2 = run_watcher_simulation(self.config, user_id="usr-college-1234", now=self.fixed_now)

        self.assertEqual(len(res1.detected_new_assignments), len(res2.detected_new_assignments))
        self.assertEqual(res1.database_mutations_count, 0)
        self.assertEqual(res2.database_mutations_count, 0)

    # 7. Course-level HTTP 403 isolation
    @patch("classpilot.simulation.StateStore")
    @patch("classpilot.simulation.UserStore")
    @patch("classpilot.simulation.get_authorized_credentials")
    @patch("googleapiclient.discovery.build")
    def test_course_level_http_403_error_isolation(
        self, mock_build, mock_get_creds, mock_user_store_cls, mock_state_store_cls
    ):
        """HTTP 403 on one course is isolated; simulation continues with remaining courses."""
        mock_state = mock_state_store_cls.return_value
        mock_state.get_enabled_watchers.return_value = ["usr-college-1234"]
        mock_state.get_known_assignment.return_value = None
        mock_state.get_all_assignments.return_value = []
        mock_state.has_sent_reminder.return_value = False

        mock_user = mock_user_store_cls.return_value
        mock_user.get_user_by_id.return_value = MagicMock(id="usr-college-1234")

        mock_client = MagicMock()
        mock_build.return_value = mock_client
        mock_client.courses.return_value.list.return_value.execute.return_value = {
            "courses": [
                {"id": "c-forbidden", "name": "Secret Lab", "courseState": "ACTIVE"},
                {"id": "c-accessible", "name": "Open Course", "courseState": "ACTIVE"},
            ]
        }

        def cw_side_effect(courseId, pageSize):
            if courseId == "c-forbidden":
                err = Exception("403 Forbidden")
                err.resp = MagicMock(status=403)
                raise err
            return MagicMock(execute=MagicMock(return_value={
                "courseWork": [{
                    "id": "a-open",
                    "title": "Open HW",
                    "dueDate": {"year": 2026, "month": 10, "day": 28},
                }]
            }))

        mock_client.courses.return_value.courseWork.return_value.list.side_effect = cw_side_effect

        res = run_watcher_simulation(self.config, user_id="usr-college-1234", now=self.fixed_now)

        self.assertTrue(res.success)
        self.assertEqual(len(res.forbidden_courses), 1)
        self.assertEqual(res.forbidden_courses[0]["id"], "c-forbidden")
        self.assertEqual(len(res.accessible_courses), 1)
        self.assertEqual(len(res.detected_new_assignments), 1)

    # 8. Zero database mutations
    def test_readonly_state_store_strictly_prevents_database_mutations(self):
        """ReadOnlyStateStore adapter raises on mutations and does not execute SQL."""
        mock_real_store = MagicMock(spec=StateStore)
        mock_real_store.get_known_assignment.return_value = None
        mock_real_store.has_sent_reminder.return_value = False
        ro_store = ReadOnlyStateStore(mock_real_store)

        with self.assertRaises(RuntimeError):
            ro_store.set_watcher_enabled("usr-1", True)

        with self.assertRaises(RuntimeError):
            ro_store.upsert_assignment_metadata("c1", "a1", "title", None, None)

        # Claims return simulated True without calling underlying DB mutation methods
        self.assertTrue(ro_store.record_new_assignment("c1", "a1", "T", None, None))
        mock_real_store.record_new_assignment.assert_not_called()

        self.assertTrue(ro_store.update_assignment_deadline("c1", "a1", "T", None, None, None, None))
        mock_real_store.update_assignment_deadline.assert_not_called()

        self.assertTrue(ro_store.mark_reminder_sent("c1", "a1", 60))
        mock_real_store.mark_reminder_sent.assert_not_called()

    # 9. Zero emails sent & Zero Gemini calls
    @patch("classpilot.simulation.StateStore")
    @patch("classpilot.simulation.UserStore")
    @patch("classpilot.simulation.get_authorized_credentials")
    @patch("googleapiclient.discovery.build")
    @patch("smtplib.SMTP")
    def test_simulation_zero_emails_and_zero_gemini_calls(
        self, mock_smtp, mock_build, mock_get_creds, mock_user_store_cls, mock_state_store_cls
    ):
        """Simulation never contacts SMTP and never invokes LLM."""
        mock_state = mock_state_store_cls.return_value
        mock_state.get_enabled_watchers.return_value = ["usr-college-1234"]
        mock_state.get_known_assignment.return_value = None
        mock_state.get_all_assignments.return_value = []
        mock_state.has_sent_reminder.return_value = False

        mock_user = mock_user_store_cls.return_value
        mock_user.get_user_by_id.return_value = MagicMock(id="usr-college-1234")

        mock_client = MagicMock()
        mock_build.return_value = mock_client
        mock_client.courses.return_value.list.return_value.execute.return_value = {
            "courses": [{"id": "c1", "name": "Calculus", "courseState": "ACTIVE"}]
        }
        mock_client.courses.return_value.courseWork.return_value.list.return_value.execute.return_value = {
            "courseWork": [{
                "id": "a1",
                "title": "Homework 1",
                "dueDate": {"year": 2026, "month": 10, "day": 25},
            }]
        }

        with patch("classpilot.llm.create_llm_provider") as mock_llm_factory, \
             patch("classpilot.notifier.create_notifier") as mock_notifier_factory:
            res = run_watcher_simulation(self.config, user_id="usr-college-1234", now=self.fixed_now)

            mock_llm_factory.assert_not_called()
            mock_notifier_factory.assert_not_called()
            mock_smtp.assert_not_called()

        self.assertEqual(res.emails_sent_count, 0)
        self.assertEqual(res.gemini_calls_count, 0)

    # 10. No production watcher execution
    @patch("classpilot.simulation.run_watcher_simulation")
    @patch("classpilot.job.build_user_services")
    @patch("sys.exit")
    def test_main_simulate_watcher_flag_never_executes_production_watcher(
        self, mock_exit, mock_build_services, mock_run_sim
    ):
        """Passing --simulate-watcher executes simulation and never falls into production watcher."""
        mock_run_sim.return_value = MagicMock(success=True)

        with patch.object(sys, "argv", ["job.py", "--simulate-watcher", "--user-id", "usr-1234"]):
            main()

        mock_run_sim.assert_called_once()
        mock_exit.assert_called_once_with(0)
        mock_build_services.assert_not_called()

    # 11. Deterministic account selection and failing closed
    @patch("classpilot.simulation.StateStore")
    @patch("classpilot.simulation.UserStore")
    def test_simulation_account_selection_fails_closed_on_ambiguity(
        self, mock_user_store_cls, mock_state_store_cls
    ):
        """Simulation fails closed when multiple enabled accounts exist without SIMULATION_USER_ID."""
        mock_state = mock_state_store_cls.return_value
        mock_state.get_enabled_watchers.return_value = ["usr-1", "usr-2"]

        res = run_watcher_simulation(self.config, user_id=None)

        self.assertFalse(res.success)

    @patch("classpilot.simulation.StateStore")
    @patch("classpilot.simulation.UserStore")
    def test_simulation_account_selection_fails_closed_when_not_enabled(
        self, mock_user_store_cls, mock_state_store_cls
    ):
        """Simulation fails closed when configured SIMULATION_USER_ID is not enabled."""
        mock_state = mock_state_store_cls.return_value
        mock_state.get_enabled_watchers.return_value = ["usr-1"]

        res = run_watcher_simulation(self.config, user_id="disabled-user")

        self.assertFalse(res.success)

    # 12. Workflow dispatch file validation
    def test_watcher_simulation_workflow_file_validity(self):
        """Verifies .github/workflows/classpilot-watcher-simulation.yml configuration."""
        workflow_path = (
            Path(__file__).parent.parent
            / ".github"
            / "workflows"
            / "classpilot-watcher-simulation.yml"
        )
        self.assertTrue(workflow_path.is_file(), f"Workflow file not found at {workflow_path}")

        content = workflow_path.read_text(encoding="utf-8")
        parsed = yaml.safe_load(content)

        # Must have name
        self.assertEqual(parsed.get("name"), "ClassPilot Watcher Simulation")

        # Must be workflow_dispatch only — no schedule or push
        triggers = parsed.get("on") or parsed.get(True) or {}
        self.assertIn("workflow_dispatch", triggers)
        self.assertNotIn("schedule", triggers)
        self.assertNotIn("push", triggers)

        job_steps = parsed["jobs"]["run-simulation"]["steps"]
        sim_step = next((s for s in job_steps if s.get("name") == "Run Watcher Simulation"), None)
        self.assertIsNotNone(sim_step, "Could not find 'Run Watcher Simulation' step")

        env = sim_step.get("env", {})
        self.assertIn("SIMULATION_USER_ID", env)
        self.assertIn("SIMULATION_COURSE_ID", env)

        run_cmd = sim_step.get("run", "")
        self.assertIn("python -m classpilot.job --simulate-watcher", run_cmd)
        self.assertNotIn("${{ inputs.", run_cmd)
        self.assertNotIn("${{ secrets.", run_cmd)

    def test_format_simulation_summary_output(self):
        """Verifies summary format contains all required metrics."""
        result = SimulationResult(
            user_id="usr-1234",
            masked_user_id="***1234",
            total_courses_checked=3,
            accessible_courses=[{"id": "c1", "name": "Calculus"}],
            forbidden_courses=[{"id": "c2", "name": "Physics", "reason": "HTTP 403"}],
            failed_courses=[],
            total_assignments_evaluated=10,
            detected_new_assignments=[{"title": "HW 1"}],
            detected_deadline_updates=[],
            detected_deadline_reminders=[{"title": "Quiz"}],
            overdue_notifications_count=0,
            suppressed_existing_assignments=[{"title": "HW 2"}],
            suppressed_historical_assignments=[{"title": "HW 0"}],
            suppressed_reminders=[],
            database_mutations_count=0,
            emails_sent_count=0,
            gemini_calls_count=0,
            production_watcher_triggered=False,
            success=True,
        )
        report = format_simulation_summary(result)
        self.assertIn("CLASSPILOT WATCHER SIMULATION REPORT", report)
        self.assertIn("***1234", report)
        self.assertIn("New Assignment Notifications:      1", report)
        self.assertIn("Upcoming Deadline Reminders:        1", report)
        self.assertIn("Suppressed (Already Known in DB):  1", report)
        self.assertIn("Suppressed (Historical Baseline):   1", report)
        self.assertIn("READ-ONLY ENFORCED", report)
        self.assertIn("ZERO EMAILS", report)
        self.assertIn("ZERO TOKEN COST", report)
        self.assertIn("NO (SAFE)", report)


if __name__ == "__main__":
    unittest.main()
