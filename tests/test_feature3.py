"""
tests/test_feature3.py  —  Feature 3: Deadline Alerts (Job Architecture)
Pure unittest, no external dependencies.
"""

import os, sys, types, unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

def _stub(*names):
    for name in names:
        parts = name.split(".")
        for i in range(1, len(parts)+1):
            key = ".".join(parts[:i])
            if key not in sys.modules:
                m = types.ModuleType(key)
                m.__path__ = []
                sys.modules[key] = m
                if i > 1:
                    setattr(sys.modules[".".join(parts[:i-1])], parts[i-1], m)

_stub(
    "anthropic","dotenv","fastmcp",
    "google.auth.transport.requests","google.oauth2.credentials",
    "google_auth_oauthlib.flow",
    "googleapiclient.discovery","googleapiclient.http","googleapiclient.errors",
)
sys.modules["dotenv"].load_dotenv = lambda *a,**k: None
sys.modules["fastmcp"].FastMCP = type("FastMCP",(),{"__init__":lambda s,*a,**k:None,"tool":lambda s:(lambda f:f)})

from classpilot.events import EventType
from classpilot.deadline_scheduler import DeadlineScheduler, _parse_due_datetime
from classpilot.llm.base import LLMProvider, LLMProviderError
from classpilot.notifier.base import Notifier
from classpilot.notification_service import NotificationService
from classpilot.config import ClassPilotConfig

class FakeLLM(LLMProvider):
    def __init__(self, fail=False):
        self.calls = []; self.fail = fail
    def generate_message(self, et, name):
        if self.fail: raise LLMProviderError("down")
        self.calls.append((et, name))
        return f"{name} deadline approaching"

class FakeNotifier(Notifier):
    def __init__(self): self.sent = []
    def send(self, subject, body): self.sent.append({"subject":subject,"body":body})

class MockStateStore:
    def __init__(self):
        self.known = []
        self.sent = set()
    def get_all_assignments(self, user_id):
        return self.known
    def mark_reminder_sent(self, cid, aid, off, user_id="default"):
        key = (user_id, cid, aid, off)
        if key in self.sent:
            return False
        self.sent.add(key)
        return True

def _make_scheduler(offsets=(24*60, 6*60, 60, 15)):
    cfg = ClassPilotConfig.__new__(ClassPilotConfig)
    cfg.deadline_alert_offsets_minutes = offsets
    store = MockStateStore()
    llm = FakeLLM()
    notifier = FakeNotifier()
    svc = NotificationService(llm, notifier)
    ds = DeadlineScheduler(store, svc, cfg)
    return ds, store, llm, notifier

def _future_event(minutes_from_now=120):
    due = datetime.now(tz=timezone.utc) + timedelta(minutes=minutes_from_now)
    import json
    return {
        "course_id": "c1",
        "assignment_id": "a1",
        "title": "DBMS HW1",
        "due_date_json": json.dumps({"year": due.year, "month": due.month, "day": due.day}),
        "due_time_json": json.dumps({"hours": due.hour, "minutes": due.minute, "seconds": due.second})
    }

class TestParseDueDatetime(unittest.TestCase):
    def test_returns_utc_datetime(self):
        dt = _parse_due_datetime({"year":2026,"month":12,"day":1},{"hours":23,"minutes":59})
        self.assertIsInstance(dt, datetime)
        self.assertEqual(dt.tzinfo, timezone.utc)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.hour, 23)

class TestEvaluateDeadlines(unittest.TestCase):
    def test_evaluates_and_fires_future_reminders(self):
        ds, store, llm, notifier = _make_scheduler(offsets=(24*60, 6*60, 60, 15))
        # Deadline 90 minutes from now -> 24h, 6h offsets have passed, 60m and 15m are future
        # wait, if 24h passed, the fire_at was yesterday. So fire_at < now -> SHOULD FIRE if not sent!
        # Ah, in DeadlineScheduler: fire_at = due_dt - timedelta. If fire_at <= now, it fires.
        # But wait, we modified deadline_scheduler to only fire if fire_at <= now AND due_dt > now.
        store.known = [_future_event(minutes_from_now=90)]
        ds.evaluate_all()
        # 24h and 6h fire_at are in the past. 60m and 15m are in the future (fire_at > now).
        # So only 24h and 6h should fire!
        self.assertEqual(len(llm.calls), 2)
        
    def test_skips_already_sent(self):
        ds, store, llm, notifier = _make_scheduler(offsets=(24*60,))
        store.known = [_future_event(minutes_from_now=90)]
        # Mark 24h already sent
        store.mark_reminder_sent("c1", "a1", 24*60)
        ds.evaluate_all()
        self.assertEqual(len(llm.calls), 0)

if __name__ == "__main__":
    unittest.main(verbosity=2)
