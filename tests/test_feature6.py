"""
tests/test_feature6.py  —  Feature 6: Success Notification
Pure unittest, all external deps stubbed.
"""

import os, sys, types, unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── stub external deps ────────────────────────────────────────────────────────
def _stub(*names):
    for name in names:
        parts = name.split(".")
        for i in range(1, len(parts)+1):
            key = ".".join(parts[:i])
            if key not in sys.modules:
                m = types.ModuleType(key)
                m.__path__ = []  # mark as package so submodules can be imported
                sys.modules[key] = m
                if i > 1:
                    setattr(sys.modules[".".join(parts[:i-1])], parts[i-1], m)

_stub(
    "anthropic","dotenv","fastmcp",
    "google.auth.transport.requests","google.oauth2.credentials",
    "google_auth_oauthlib.flow",
    "googleapiclient.discovery","googleapiclient.http","googleapiclient.errors",
    "apscheduler.schedulers.blocking","apscheduler.schedulers.background","apscheduler.executors.pool",
)
sys.modules["dotenv"].load_dotenv = lambda *a,**k: None
sys.modules["fastmcp"].FastMCP = type("FastMCP",(),{"__init__":lambda s,*a,**k:None,"tool":lambda s:(lambda f:f)})
sys.modules["google.auth.transport.requests"].Request = object
sys.modules["google.oauth2.credentials"].Credentials = object
sys.modules["google_auth_oauthlib.flow"].InstalledAppFlow = object
sys.modules["googleapiclient.discovery"].build = lambda *a,**k: None
sys.modules["googleapiclient.discovery"].Resource = object
sys.modules["googleapiclient.http"].MediaIoBaseUpload = object
sys.modules["googleapiclient.http"].MediaIoBaseDownload = object
sys.modules["googleapiclient.errors"].HttpError = Exception
sys.modules["apscheduler.schedulers.blocking"].BlockingScheduler = type("BS",(),{"__init__":lambda s,**k:None,"add_job":lambda s,*a,**k:None,"start":lambda s:None})
sys.modules["apscheduler.schedulers.background"].BackgroundScheduler = type("BGS",(),{"__init__":lambda s,**k:None,"add_job":lambda s,*a,**k:None,"start":lambda s:None,"shutdown":lambda s,**k:None,"remove_job":lambda s,j:None,"running":True})
sys.modules["apscheduler.executors.pool"].ThreadPoolExecutor = type("TPE",(),{"__init__":lambda s,**k:None})

# ── imports ───────────────────────────────────────────────────────────────────
from classpilot.events import EventType
from classpilot.llm.base import LLMProvider, LLMProviderError
from classpilot.notifier.base import Notifier, NotifierError
from classpilot.notification_service import NotificationService
from classpilot.submission_flow import SubmissionFlow, AssignmentMatch

# ── test doubles ──────────────────────────────────────────────────────────────
class FakeLLM(LLMProvider):
    def __init__(self, fail=False):
        self.calls = []; self.fail = fail
    def generate_message(self, et, name):
        if self.fail: raise LLMProviderError("LLM down")
        self.calls.append((et, name)); return f"🎉 {name} submitted. Now breathe."

class FakeNotifier(Notifier):
    def __init__(self, fail=False):
        self.sent = []; self.fail = fail
    def send(self, subject, body):
        if self.fail: raise NotifierError("SMTP down")
        self.sent.append({"subject": subject, "body": body})

def _make_classroom(submissions=None, turnin_result=None, fail_turnin=False):
    m = MagicMock()
    m.list_submissions.return_value = submissions or [{"id":"s1","state":"CREATED"}]
    if fail_turnin:
        m.turn_in_submission.side_effect = Exception("TurnIn failed")
    else:
        m.turn_in_submission.return_value = turnin_result or {
            "id":"s1","state":"TURNED_IN","alternateLink":"http://classroom.google.com/x"
        }
    return m

ASSIGNMENT = AssignmentMatch("c1","DBMS","a1","DBMS Final Project", 0.95)


# ═════════════════════════════════════════════════════════════════════════════
class TestSuccessNotificationFires(unittest.TestCase):
    """Feature 6 core: success notification fires automatically after turn-in."""

    def _flow(self, llm=None, notifier_fail=False, fail_turnin=False):
        import classpilot.submission_flow as sf
        sf.classroom = _make_classroom(fail_turnin=fail_turnin)
        sf.drive = MagicMock()
        svc = NotificationService(llm or FakeLLM(), FakeNotifier(fail=notifier_fail))
        return SubmissionFlow(notification_service=svc), svc

    def test_success_notification_sent_after_turn_in(self):
        flow, svc = self._flow()
        result = flow.turn_in(ASSIGNMENT)
        self.assertTrue(result.success)
        # NotificationService must have delivered exactly 1 message
        notifier = svc._notifier
        self.assertEqual(len(notifier.sent), 1)

    def test_success_notification_uses_submission_success_event_type(self):
        llm = FakeLLM()
        flow, svc = self._flow(llm=llm)
        flow.turn_in(ASSIGNMENT)
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(llm.calls[0][0], EventType.SUBMISSION_SUCCESS)

    def test_success_notification_uses_assignment_name(self):
        llm = FakeLLM()
        flow, svc = self._flow(llm=llm)
        flow.turn_in(ASSIGNMENT)
        self.assertEqual(llm.calls[0][1], "DBMS Final Project")

    def test_success_email_subject_contains_assignment_name(self):
        flow, svc = self._flow()
        flow.turn_in(ASSIGNMENT)
        subject = svc._notifier.sent[0]["subject"]
        self.assertIn("DBMS Final Project", subject)

    def test_success_email_body_contains_ai_message(self):
        flow, svc = self._flow()
        flow.turn_in(ASSIGNMENT)
        body = svc._notifier.sent[0]["body"]
        self.assertIn("DBMS Final Project submitted", body)

    def test_success_email_ends_with_signature(self):
        flow, svc = self._flow()
        flow.turn_in(ASSIGNMENT)
        body = svc._notifier.sent[0]["body"]
        self.assertTrue(body.endswith("— ClassPilot AI 🤖"))

    def test_no_notification_when_turn_in_fails(self):
        """If turn-in fails, success notification must NOT fire."""
        flow, svc = self._flow(fail_turnin=True)
        result = flow.turn_in(ASSIGNMENT)
        self.assertFalse(result.success)
        self.assertEqual(len(svc._notifier.sent), 0)

    def test_turn_in_result_still_success_even_if_notification_fails(self):
        """Notification failure must not retroactively fail the turn-in."""
        flow, svc = self._flow(notifier_fail=True)
        result = flow.turn_in(ASSIGNMENT)
        # Turn-in itself succeeded — notification failure is silent
        self.assertTrue(result.success)
        self.assertEqual(result.state, "TURNED_IN")

    def test_turn_in_result_still_success_if_llm_fails(self):
        """LLM failure for the success message must not affect turn-in result."""
        flow, svc = self._flow(llm=FakeLLM(fail=True))
        result = flow.turn_in(ASSIGNMENT)
        self.assertTrue(result.success)
        self.assertEqual(result.state, "TURNED_IN")


class TestNoNotificationWithoutService(unittest.TestCase):
    """SubmissionFlow without notification_service must work exactly as before."""

    def _flow_bare(self, fail_turnin=False):
        import classpilot.submission_flow as sf
        sf.classroom = _make_classroom(fail_turnin=fail_turnin)
        sf.drive = MagicMock()
        return SubmissionFlow()  # no notification_service

    def test_turn_in_succeeds_without_notification_service(self):
        flow = self._flow_bare()
        result = flow.turn_in(ASSIGNMENT)
        self.assertTrue(result.success)
        self.assertEqual(result.state, "TURNED_IN")

    def test_no_error_raised_without_notification_service(self):
        flow = self._flow_bare()
        try:
            flow.turn_in(ASSIGNMENT)
        except Exception as exc:
            self.fail(f"turn_in raised unexpectedly: {exc}")

    def test_attach_unaffected_by_feature6(self):
        """attach_file must never trigger a success notification."""
        import classpilot.submission_flow as sf
        sf.classroom = MagicMock()
        sf.classroom.list_submissions.return_value = [{"id":"s1","state":"CREATED"}]
        sf.classroom.attach_submission_files.return_value = {"id":"s1","state":"CREATED","alternateLink":"x"}
        sf.drive = MagicMock()
        llm = FakeLLM()
        svc = NotificationService(llm, FakeNotifier())
        flow = SubmissionFlow(notification_service=svc)
        from classpilot.submission_flow import FileMatch
        file = FileMatch("f1","DBMS_Final.pdf","application/pdf", 0.9)
        flow.attach_file(ASSIGNMENT, file)
        # attach must not trigger any LLM call or notification
        self.assertEqual(len(llm.calls), 0)
        self.assertEqual(len(svc._notifier.sent), 0)


class TestSuccessMessagePersonality(unittest.TestCase):
    """Verify SUBMISSION_SUCCESS hint exists and produces the right LLM call."""

    def test_submission_success_hint_defined_in_provider(self):
        from classpilot.llm.anthropic_provider import _EVENT_HINTS
        self.assertIn(EventType.SUBMISSION_SUCCESS, _EVENT_HINTS)

    def test_submission_success_hint_mentions_assignment_name(self):
        from classpilot.llm.anthropic_provider import _EVENT_HINTS
        hint = _EVENT_HINTS[EventType.SUBMISSION_SUCCESS].format(assignment_name="DBMS")
        self.assertIn("DBMS", hint)

    def test_submission_success_subject_template_exists(self):
        from classpilot.notification_service import _SUBJECTS
        self.assertIn(EventType.SUBMISSION_SUCCESS, _SUBJECTS)
        subject = _SUBJECTS[EventType.SUBMISSION_SUCCESS].format(assignment_name="DBMS")
        self.assertIn("DBMS", subject)

    def test_anthropic_provider_generates_success_message(self):
        from classpilot.llm.anthropic_provider import AnthropicProvider
        block = MagicMock(); block.type = "text"
        block.text = "🎉 Mission Complete. DBMS Assignment survived. Now breathe."
        resp = MagicMock(); resp.content = [block]
        client = MagicMock(); client.messages.create.return_value = resp
        provider = AnthropicProvider.__new__(AnthropicProvider)
        provider._client = client; provider._model = "claude-sonnet-4-6"
        msg = provider.generate_message(EventType.SUBMISSION_SUCCESS, "DBMS Assignment")
        self.assertIsInstance(msg, str)
        self.assertGreater(len(msg), 0)

    def test_success_message_uses_same_system_prompt(self):
        """Proves Feature 6 reuses the ONE personality prompt, not a new one."""
        from classpilot.llm.anthropic_provider import AnthropicProvider, SYSTEM_PROMPT
        block = MagicMock(); block.type = "text"; block.text = "Well done 🎉"
        resp = MagicMock(); resp.content = [block]
        client = MagicMock(); client.messages.create.return_value = resp
        provider = AnthropicProvider.__new__(AnthropicProvider)
        provider._client = client; provider._model = "claude-sonnet-4-6"
        provider.generate_message(EventType.SUBMISSION_SUCCESS, "DBMS Assignment")
        self.assertEqual(client.messages.create.call_args[1]["system"], SYSTEM_PROMPT)


class TestFullSubmissionFlowWithAllFeatures(unittest.TestCase):
    """End-to-end: Features 4 → 5 → 6 in sequence with one SubmissionFlow."""

    def test_complete_flow_find_attach_turnin_notify(self):
        import classpilot.submission_flow as sf
        from classpilot.submission_flow import FileMatch

        # Set up mocks
        sf.classroom = MagicMock()
        sf.classroom.list_courses.return_value = [{"id":"c1","name":"DBMS"}]
        sf.classroom.list_assignments.return_value = [{"id":"a1","title":"DBMS Final Project"}]
        sf.classroom.list_submissions.return_value = [{"id":"s1","state":"CREATED"}]
        sf.classroom.attach_submission_files.return_value = {"id":"s1","state":"CREATED","alternateLink":"x"}
        sf.classroom.turn_in_submission.return_value = {"id":"s1","state":"TURNED_IN","alternateLink":"x"}
        sf.drive = MagicMock()
        sf.drive.search_files.return_value = [{"id":"f1","name":"DBMS_Final.pdf","mime_type":"application/pdf"}]
        sf.drive.list_files.return_value = []

        llm = FakeLLM()
        notifier = FakeNotifier()
        svc = NotificationService(llm, notifier)
        flow = SubmissionFlow(notification_service=svc)

        # Step 1: find assignment (Feature 4)
        assignment = flow.find_assignment("DBMS Final Project")
        self.assertIsNotNone(assignment)

        # Step 2: find file (Feature 4)
        file = flow.find_best_file("DBMS Final Project")
        self.assertIsNotNone(file)

        # Step 3: attach (Feature 4) — no notification yet
        attach_result = flow.attach_file(assignment, file)
        self.assertTrue(attach_result.success)
        self.assertEqual(len(llm.calls), 0)  # no LLM call yet

        # Step 4: turn in (Feature 5) — triggers success notification (Feature 6)
        turnin_result = flow.turn_in(assignment)
        self.assertTrue(turnin_result.success)
        self.assertEqual(turnin_result.state, "TURNED_IN")

        # Feature 6: exactly one success notification sent
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(llm.calls[0][0], EventType.SUBMISSION_SUCCESS)
        self.assertEqual(len(notifier.sent), 1)
        self.assertIn("DBMS Final Project", notifier.sent[0]["subject"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
