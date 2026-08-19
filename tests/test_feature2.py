"""
tests/test_feature2.py  —  Feature 2: AI Funny Notifications
Pure unittest (no pytest dependency).
"""

import os, sys, tempfile, types, smtplib, unittest
from email import message_from_string
from email.header import decode_header
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── stub every external dep before importing classpilot ──────────────────────
def _stub(*names):
    for name in names:
        parts = name.split(".")
        for i in range(1, len(parts) + 1):
            key = ".".join(parts[:i])
            if key not in sys.modules:
                m = types.ModuleType(key)
                sys.modules[key] = m
                if i > 1:
                    setattr(sys.modules[".".join(parts[:i-1])], parts[i-1], m)

# Pre-create apscheduler.schedulers as a package stub before submodule imports
import types as _types
_aps = _types.ModuleType("apscheduler"); _aps.__path__ = []; import sys as _sys; _sys.modules["apscheduler"] = _aps
_aps_s = _types.ModuleType("apscheduler.schedulers"); _aps_s.__path__ = []; _sys.modules["apscheduler.schedulers"] = _aps_s; _aps.schedulers = _aps_s
del _types, _aps, _aps_s, _sys

_stub(
    "anthropic", "dotenv", "fastmcp",
    "google.auth.transport.requests", "google.oauth2.credentials",
    "google_auth_oauthlib.flow",
    "googleapiclient.discovery", "googleapiclient.http", "googleapiclient.errors",
    "apscheduler.schedulers.blocking", "apscheduler.schedulers.background", "apscheduler.executors.pool",
)
sys.modules["dotenv"].load_dotenv = lambda *a, **k: None
sys.modules["fastmcp"].FastMCP = type("FastMCP", (), {"__init__": lambda s,*a,**k: None, "tool": lambda s: (lambda f: f)})
sys.modules["google.auth.transport.requests"].Request = object
sys.modules["google.oauth2.credentials"].Credentials = object
sys.modules["google_auth_oauthlib.flow"].InstalledAppFlow = object
sys.modules["googleapiclient.discovery"].build = lambda *a,**k: None
sys.modules["googleapiclient.discovery"].Resource = object
sys.modules["googleapiclient.http"].MediaIoBaseUpload = object
sys.modules["googleapiclient.http"].MediaIoBaseDownload = object
sys.modules["googleapiclient.errors"].HttpError = Exception
sys.modules["apscheduler.schedulers.blocking"].BlockingScheduler = type("BS", (), {"__init__": lambda s,**k: None, "add_job": lambda s,*a,**k: None, "start": lambda s: None})
sys.modules["apscheduler.schedulers.background"].BackgroundScheduler = type("BGS",(),{"__init__":lambda s,**k:None,"add_job":lambda s,*a,**k:None,"start":lambda s:None,"shutdown":lambda s,**k:None,"remove_job":lambda s,j:None,"running":True})
sys.modules["apscheduler.executors.pool"].ThreadPoolExecutor = type("TPE", (), {"__init__": lambda s,**k: None})

# ── imports ───────────────────────────────────────────────────────────────────
from classpilot.events import EventType
from classpilot.llm.base import LLMProvider, LLMProviderError
from classpilot.notifier.base import Notifier, NotifierError
from classpilot.notification_service import NotificationService, PendingNotification
from classpilot.state_store import StateStore
from classpilot.watcher import AssignmentWatcher
from classpilot.main import build_handle_event

# ── test doubles ──────────────────────────────────────────────────────────────
class FakeLLM(LLMProvider):
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail
    def generate_message(self, event_type, assignment_name):
        if self.fail:
            raise LLMProviderError("LLM unavailable")
        self.calls.append((event_type, assignment_name))
        return f"{assignment_name} just dropped 💀 Your professor chose violence."

class FakeNotifier(Notifier):
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail
    def send(self, subject, body):
        if self.fail:
            raise NotifierError("SMTP unavailable")
        self.sent.append({"subject": subject, "body": body})

def _make_watcher(assignments):
    import classpilot.watcher as wm
    wm.classroom = types.SimpleNamespace(
        list_courses=lambda page_size=20, course_states=None: [{"id": "c1", "name": "DBMS"}],
        list_assignments=lambda course_id, page_size=20: assignments,
    )
    db = tempfile.mktemp(suffix=".db")
    return StateStore(db), db


class NullDeadlineScheduler:
    """No-op stub for Feature 2 tests — deadline scheduling not under test here."""
    def schedule(self, event): pass
    def reschedule(self, event): pass


# ═════════════════════════════════════════════════════════════════════════════
class TestEventType(unittest.TestCase):
    def test_all_three_events_exist(self):
        for e in (EventType.NEW_ASSIGNMENT, EventType.DEADLINE_REMINDER, EventType.SUBMISSION_SUCCESS):
            self.assertIsInstance(e, EventType)

    def test_string_values(self):
        self.assertEqual(EventType.NEW_ASSIGNMENT, "NEW_ASSIGNMENT")
        self.assertEqual(EventType.DEADLINE_REMINDER, "DEADLINE_REMINDER")
        self.assertEqual(EventType.SUBMISSION_SUCCESS, "SUBMISSION_SUCCESS")


class TestLLMProviderInterface(unittest.TestCase):
    def test_cannot_instantiate_abstract_base(self):
        with self.assertRaises(TypeError):
            LLMProvider()

    def test_concrete_subclass_works(self):
        msg = FakeLLM().generate_message(EventType.NEW_ASSIGNMENT, "DBMS")
        self.assertIsInstance(msg, str)
        self.assertTrue(len(msg) > 0)


class TestAnthropicProvider(unittest.TestCase):
    def _provider(self, text="DBMS just dropped 💀"):
        from classpilot.llm.anthropic_provider import AnthropicProvider
        block = MagicMock(); block.type = "text"; block.text = text
        resp = MagicMock(); resp.content = [block]
        client = MagicMock(); client.messages.create.return_value = resp
        p = AnthropicProvider.__new__(AnthropicProvider)
        p._client = client; p._model = "claude-sonnet-4-6"
        return p, client

    def test_uses_correct_model(self):
        p, c = self._provider()
        p.generate_message(EventType.NEW_ASSIGNMENT, "DBMS")
        self.assertEqual(c.messages.create.call_args[1]["model"], "claude-sonnet-4-6")

    def test_system_prompt_is_sent(self):
        from classpilot.llm.anthropic_provider import SYSTEM_PROMPT
        p, c = self._provider()
        p.generate_message(EventType.NEW_ASSIGNMENT, "DBMS")
        self.assertEqual(c.messages.create.call_args[1]["system"], SYSTEM_PROMPT)

    def test_assignment_name_in_user_message(self):
        p, c = self._provider()
        p.generate_message(EventType.NEW_ASSIGNMENT, "OS Assignment")
        content = c.messages.create.call_args[1]["messages"][0]["content"]
        self.assertIn("OS Assignment", content)

    def test_all_event_types_handled(self):
        for et in EventType:
            p, _ = self._provider()
            result = p.generate_message(et, "CN Assignment")
            self.assertIsInstance(result, str)

    def test_strips_surrounding_quotes(self):
        p, _ = self._provider(text='"hello world"')
        result = p.generate_message(EventType.NEW_ASSIGNMENT, "DBMS")
        self.assertFalse(result.startswith('"'))
        self.assertFalse(result.endswith('"'))

    def test_raises_on_empty_response(self):
        p, _ = self._provider(text="")
        with self.assertRaises(LLMProviderError):
            p.generate_message(EventType.NEW_ASSIGNMENT, "DBMS")

    def test_raises_on_api_exception(self):
        p, c = self._provider()
        c.messages.create.side_effect = Exception("timeout")
        with self.assertRaises(LLMProviderError):
            p.generate_message(EventType.NEW_ASSIGNMENT, "DBMS")

    def test_max_tokens_is_60(self):
        p, c = self._provider()
        p.generate_message(EventType.NEW_ASSIGNMENT, "DBMS")
        self.assertEqual(c.messages.create.call_args[1]["max_tokens"], 60)

    def test_temperature_is_1(self):
        p, c = self._provider()
        p.generate_message(EventType.NEW_ASSIGNMENT, "DBMS")
        self.assertEqual(c.messages.create.call_args[1]["temperature"], 1.0)


class TestNotificationService(unittest.TestCase):
    def test_llm_called_once(self):
        llm = FakeLLM()
        NotificationService(llm, FakeNotifier()).notify(EventType.NEW_ASSIGNMENT, "DBMS")
        self.assertEqual(len(llm.calls), 1)

    def test_correct_event_and_name_passed_to_llm(self):
        llm = FakeLLM()
        NotificationService(llm, FakeNotifier()).notify(EventType.NEW_ASSIGNMENT, "OS Assignment")
        self.assertEqual(llm.calls[0], (EventType.NEW_ASSIGNMENT, "OS Assignment"))

    def test_returns_true_on_success(self):
        self.assertTrue(NotificationService(FakeLLM(), FakeNotifier()).notify(EventType.NEW_ASSIGNMENT, "DBMS"))

    def test_returns_false_when_llm_fails(self):
        self.assertFalse(NotificationService(FakeLLM(fail=True), FakeNotifier()).notify(EventType.NEW_ASSIGNMENT, "DBMS"))

    def test_returns_false_when_notifier_fails(self):
        self.assertFalse(NotificationService(FakeLLM(), FakeNotifier(fail=True)).notify(EventType.NEW_ASSIGNMENT, "DBMS"))

    def test_notifier_not_called_when_llm_fails(self):
        n = FakeNotifier()
        NotificationService(FakeLLM(fail=True), n).notify(EventType.NEW_ASSIGNMENT, "DBMS")
        self.assertEqual(len(n.sent), 0)

    def test_retry_does_not_call_llm_again(self):
        llm = FakeLLM()
        n = FakeNotifier(fail=True)
        svc = NotificationService(llm, n)
        svc.notify(EventType.NEW_ASSIGNMENT, "OS")
        self.assertEqual(len(llm.calls), 1)
        n.fail = False
        svc.retry(PendingNotification(EventType.NEW_ASSIGNMENT, "OS", "OS is waiting 💀"))
        self.assertEqual(len(llm.calls), 1, "LLM must NOT be called on retry")

    def test_retry_delivers_preserved_message(self):
        n = FakeNotifier()
        svc = NotificationService(FakeLLM(), n)
        msg = "OS Assignment: your free time is over."
        svc.retry(PendingNotification(EventType.NEW_ASSIGNMENT, "OS Assignment", msg))
        self.assertIn(msg, n.sent[0]["body"])


class TestEmailBody(unittest.TestCase):
    def _email(self, name="DBMS Assignment"):
        n = FakeNotifier()
        NotificationService(FakeLLM(), n).notify(EventType.NEW_ASSIGNMENT, name)
        return n.sent[0]

    def test_subject_contains_assignment_name(self):
        self.assertIn("DBMS Assignment", self._email()["subject"])

    def test_body_first_line_is_assignment_name(self):
        self.assertEqual(self._email()["body"].split("\n")[0], "DBMS Assignment")

    def test_body_contains_ai_message(self):
        self.assertIn("just dropped", self._email()["body"])

    def test_body_ends_with_signature(self):
        self.assertTrue(self._email()["body"].endswith("— ClassPilot AI 🤖"))

    def test_body_has_only_name_message_signature(self):
        non_empty = [l for l in self._email()["body"].split("\n") if l.strip()]
        self.assertEqual(len(non_empty), 3)  # name, ai message, signature


class TestEmailNotifier(unittest.TestCase):
    def _notifier(self):
        from classpilot.notifier.email_notifier import EmailNotifier
        return EmailNotifier("smtp.example.com", 587, "from@x.com", "secret", "to@x.com")

    def test_raises_on_missing_config(self):
        from classpilot.notifier.email_notifier import EmailNotifier
        with self.assertRaises(NotifierError):
            EmailNotifier("", 587, "", "", "")

    def test_correct_subject_in_mime(self):
        notifier = self._notifier()
        captured = {}
        def fake_send(f, t, msg): captured["msg"] = message_from_string(msg)
        with patch("smtplib.SMTP") as mock:
            mock.return_value.__enter__.return_value.sendmail.side_effect = fake_send
            notifier.send("📚 Test Subject", "Body text")
        raw = captured["msg"]["Subject"]
        decoded = "".join(
            p.decode(e or "utf-8") if isinstance(p, bytes) else p
            for p, e in decode_header(raw)
        )
        self.assertEqual(decoded, "📚 Test Subject")

    def test_correct_recipient(self):
        notifier = self._notifier()
        captured = {}
        def fake_send(f, t, msg): captured["to"] = t
        with patch("smtplib.SMTP") as mock:
            mock.return_value.__enter__.return_value.sendmail.side_effect = fake_send
            notifier.send("Subj", "Body")
        self.assertIn("to@x.com", captured["to"])

    def test_raises_on_smtp_error(self):
        with patch("smtplib.SMTP") as mock:
            mock.return_value.__enter__.side_effect = smtplib.SMTPException("auth failed")
            with self.assertRaises(NotifierError, msg="Failed to send email"):
                self._notifier().send("S", "B")

    def test_raises_on_network_error(self):
        with patch("smtplib.SMTP") as mock:
            mock.return_value.__enter__.side_effect = OSError("refused")
            with self.assertRaises(NotifierError, msg="Network error"):
                self._notifier().send("S", "B")


class TestEndToEnd(unittest.TestCase):
    BASE = {"id": "a1", "title": "DBMS HW1",
            "due_date": {"year": 2026, "month": 7, "day": 5}, "due_time": {"hours": 23}}

    def test_new_assignment_triggers_llm_and_email(self):
        assignments = [dict(self.BASE)]
        store, db = _make_watcher(assignments)
        llm, n = FakeLLM(), FakeNotifier()
        svc = NotificationService(llm, n)
        watcher = AssignmentWatcher(store, on_event=build_handle_event(svc, NullDeadlineScheduler()))
        watcher.check_once()
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(llm.calls[0][0], EventType.NEW_ASSIGNMENT)
        self.assertEqual(llm.calls[0][1], "DBMS HW1")
        self.assertEqual(len(n.sent), 1)
        os.remove(db)

    def test_idle_polls_are_completely_silent(self):
        assignments = [dict(self.BASE)]
        store, db = _make_watcher(assignments)
        llm, n = FakeLLM(), FakeNotifier()
        svc = NotificationService(llm, n)
        watcher = AssignmentWatcher(store, on_event=build_handle_event(svc, NullDeadlineScheduler()))
        watcher.check_once()
        watcher.check_once()
        watcher.check_once()
        self.assertEqual(len(llm.calls), 1, "LLM called on idle poll — bug")
        self.assertEqual(len(n.sent), 1,    "Email sent on idle poll — bug")
        os.remove(db)

    def test_deadline_change_does_not_notify_in_feature2(self):
        assignments = [dict(self.BASE)]
        store, db = _make_watcher(assignments)
        llm, n = FakeLLM(), FakeNotifier()
        svc = NotificationService(llm, n)
        watcher = AssignmentWatcher(store, on_event=build_handle_event(svc, NullDeadlineScheduler()))
        watcher.check_once()
        calls_after_first = len(llm.calls)
        assignments[0]["due_date"] = {"year": 2026, "month": 8, "day": 1}
        watcher.check_once()
        self.assertEqual(len(llm.calls), calls_after_first, "LLM fired on deadline_updated — F3 only")
        self.assertEqual(len(n.sent), 1,                    "Email fired on deadline_updated — F3 only")
        os.remove(db)

    def test_multiple_assignments_each_get_notification(self):
        assignments = [
            {"id": "a1", "title": "DBMS HW1",  "due_date": None, "due_time": None},
            {"id": "a2", "title": "OS Project", "due_date": None, "due_time": None},
            {"id": "a3", "title": "CN Lab",     "due_date": None, "due_time": None},
        ]
        store, db = _make_watcher(assignments)
        llm, n = FakeLLM(), FakeNotifier()
        svc = NotificationService(llm, n)
        watcher = AssignmentWatcher(store, on_event=build_handle_event(svc, NullDeadlineScheduler()))
        watcher.check_once()
        self.assertEqual(len(llm.calls), 3)
        self.assertEqual(len(n.sent), 3)
        names = [c[1] for c in llm.calls]
        self.assertIn("DBMS HW1", names)
        self.assertIn("OS Project", names)
        self.assertIn("CN Lab", names)
        os.remove(db)

    def test_dedup_across_restarts(self):
        """StateStore on same DB file prevents re-notification after process restart."""
        assignments = [dict(self.BASE)]
        store, db = _make_watcher(assignments)
        llm, n = FakeLLM(), FakeNotifier()
        svc = NotificationService(llm, n)
        watcher = AssignmentWatcher(store, on_event=build_handle_event(svc, NullDeadlineScheduler()))
        watcher.check_once()
        self.assertEqual(len(llm.calls), 1)

        # Simulate restart: new watcher, same DB
        store2 = StateStore(db)
        llm2, n2 = FakeLLM(), FakeNotifier()
        svc2 = NotificationService(llm2, n2)
        watcher2 = AssignmentWatcher(store2, on_event=build_handle_event(svc2, NullDeadlineScheduler()))
        watcher2.check_once()
        self.assertEqual(len(llm2.calls), 0, "Re-notified after restart — dedup bug")
        self.assertEqual(len(n2.sent),    0, "Re-emailed after restart — dedup bug")
        os.remove(db)


if __name__ == "__main__":
    unittest.main(verbosity=2)
