"""
tests/test_feature3.py  —  Feature 3: Deadline Alerts
Pure unittest, no external dependencies (all stubbed via conftest pattern).
"""

import os, sys, tempfile, types, unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, call, patch

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

# Pre-create apscheduler.schedulers as a package stub before submodule imports
import types as _types
_aps = _types.ModuleType("apscheduler"); _aps.__path__ = []; import sys as _sys; _sys.modules["apscheduler"] = _aps
_aps_s = _types.ModuleType("apscheduler.schedulers"); _aps_s.__path__ = []; _sys.modules["apscheduler.schedulers"] = _aps_s; _aps.schedulers = _aps_s
del _types, _aps, _aps_s, _sys

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
from classpilot.state_store import StateStore
from classpilot.watcher import AssignmentEvent
from classpilot.deadline_scheduler import (
    DeadlineScheduler, _parse_due_datetime, _fire_reminder, _offset_label
)
from classpilot.llm.base import LLMProvider, LLMProviderError
from classpilot.notifier.base import Notifier, NotifierError
from classpilot.notification_service import NotificationService
from classpilot.config import ClassPilotConfig

# ── helpers ───────────────────────────────────────────────────────────────────
class FakeLLM(LLMProvider):
    def __init__(self, fail=False):
        self.calls = []; self.fail = fail
    def generate_message(self, et, name):
        if self.fail: raise LLMProviderError("down")
        self.calls.append((et, name))
        return f"{name} deadline approaching 🔥"

class FakeNotifier(Notifier):
    def __init__(self): self.sent = []
    def send(self, subject, body): self.sent.append({"subject":subject,"body":body})

def _make_scheduler(offsets=(24*60, 6*60, 60, 15)):
    """Return a DeadlineScheduler with a mock APScheduler attached."""
    cfg = ClassPilotConfig.__new__(ClassPilotConfig)
    cfg.deadline_alert_offsets_minutes = offsets
    db = tempfile.mktemp(suffix=".db")
    store = StateStore(db)
    llm = FakeLLM()
    notifier = FakeNotifier()
    svc = NotificationService(llm, notifier)
    ds = DeadlineScheduler(store, svc, cfg)
    mock_ap = MagicMock()
    mock_ap.remove_job = MagicMock(side_effect=Exception("not found"))  # default: job missing
    ds.attach_scheduler(mock_ap)
    return ds, store, llm, notifier, mock_ap, db

def _future_event(minutes_from_now=120, kind="new"):
    """AssignmentEvent with a due datetime N minutes in the future."""
    due = datetime.now(tz=timezone.utc) + timedelta(minutes=minutes_from_now)
    return AssignmentEvent(
        kind=kind,
        course_id="c1", course_name="DBMS",
        assignment_id="a1", title="DBMS HW1",
        due_date={"year":due.year,"month":due.month,"day":due.day},
        due_time={"hours":due.hour,"minutes":due.minute,"seconds":due.second},
    )

def _past_event():
    """AssignmentEvent whose deadline has already passed."""
    due = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    return AssignmentEvent(
        kind="new", course_id="c1", course_name="DBMS",
        assignment_id="a2", title="DBMS HW2",
        due_date={"year":due.year,"month":due.month,"day":due.day},
        due_time={"hours":due.hour,"minutes":due.minute,"seconds":due.second},
    )

# ═════════════════════════════════════════════════════════════════════════════
class TestParseDueDatetime(unittest.TestCase):
    def test_returns_utc_datetime(self):
        dt = _parse_due_datetime({"year":2026,"month":12,"day":1},{"hours":23,"minutes":59})
        self.assertIsInstance(dt, datetime)
        self.assertEqual(dt.tzinfo, timezone.utc)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.hour, 23)

    def test_missing_due_date_returns_none(self):
        self.assertIsNone(_parse_due_datetime(None, {"hours":10}))

    def test_missing_due_time_defaults_to_2359(self):
        dt = _parse_due_datetime({"year":2026,"month":1,"day":1}, None)
        self.assertEqual(dt.hour, 23)
        self.assertEqual(dt.minute, 59)

    def test_malformed_date_returns_none(self):
        self.assertIsNone(_parse_due_datetime({"bad":"key"}, None))


class TestOffsetLabel(unittest.TestCase):
    def test_minutes(self):
        self.assertEqual(_offset_label(15), "15min")

    def test_hours(self):
        self.assertEqual(_offset_label(60),  "1h")
        self.assertEqual(_offset_label(360), "6h")
        self.assertEqual(_offset_label(1440),"24h")


class TestDeadlineSchedulerAttach(unittest.TestCase):
    def test_schedule_before_attach_logs_error_and_does_not_crash(self):
        cfg = ClassPilotConfig.__new__(ClassPilotConfig)
        cfg.deadline_alert_offsets_minutes = (60,)
        db = tempfile.mktemp(suffix=".db")
        store = StateStore(db)
        svc = NotificationService(FakeLLM(), FakeNotifier())
        ds = DeadlineScheduler(store, svc, cfg)
        # Should not raise — just log an error
        ds.schedule(_future_event(minutes_from_now=120))
        os.remove(db)


class TestScheduleJobs(unittest.TestCase):
    def test_registers_jobs_for_future_offsets_only(self):
        # Deadline 90 minutes from now → only the 15-min offset qualifies
        # (60-min, 6h, 24h are all already past relative to the due time)
        ds, store, llm, notifier, mock_ap, db = _make_scheduler(offsets=(24*60, 6*60, 60, 15))
        event = _future_event(minutes_from_now=30)
        ds.schedule(event)
        # Only the 15-min offset has fire_at in the future
        add_calls = mock_ap.add_job.call_args_list
        self.assertEqual(len(add_calls), 1)
        args = add_calls[0]
        self.assertIn("15", args[1]["id"])
        os.remove(db)

    def test_registers_all_four_jobs_for_far_future_deadline(self):
        ds, store, llm, notifier, mock_ap, db = _make_scheduler()
        # 2 days from now — all four offsets are in the future
        event = _future_event(minutes_from_now=2*24*60 + 30)
        ds.schedule(event)
        self.assertEqual(mock_ap.add_job.call_count, 4)
        os.remove(db)

    def test_skips_all_jobs_for_past_deadline(self):
        ds, store, llm, notifier, mock_ap, db = _make_scheduler()
        ds.schedule(_past_event())
        mock_ap.add_job.assert_not_called()
        os.remove(db)

    def test_skips_already_sent_reminders(self):
        ds, store, llm, notifier, mock_ap, db = _make_scheduler(offsets=(15,))
        event = _future_event(minutes_from_now=30)
        store.mark_reminder_sent(event.course_id, event.assignment_id, 15)
        ds.schedule(event)
        mock_ap.add_job.assert_not_called()
        os.remove(db)

    def test_no_deadline_skips_scheduling(self):
        ds, store, llm, notifier, mock_ap, db = _make_scheduler()
        event = AssignmentEvent(
            kind="new", course_id="c1", course_name="DBMS",
            assignment_id="a1", title="DBMS HW1",
            due_date=None, due_time=None,
        )
        ds.schedule(event)
        mock_ap.add_job.assert_not_called()
        os.remove(db)

    def test_job_id_contains_course_assignment_and_offset(self):
        ds, store, llm, notifier, mock_ap, db = _make_scheduler(offsets=(15,))
        event = _future_event(minutes_from_now=30)
        ds.schedule(event)
        job_id = mock_ap.add_job.call_args[1]["id"]
        self.assertIn("c1", job_id)
        self.assertIn("a1", job_id)
        self.assertIn("15", job_id)
        os.remove(db)

    def test_replace_existing_is_true(self):
        ds, store, llm, notifier, mock_ap, db = _make_scheduler(offsets=(15,))
        event = _future_event(minutes_from_now=30)
        ds.schedule(event)
        self.assertTrue(mock_ap.add_job.call_args[1]["replace_existing"])
        os.remove(db)

    def test_date_trigger_is_used(self):
        ds, store, llm, notifier, mock_ap, db = _make_scheduler(offsets=(15,))
        event = _future_event(minutes_from_now=30)
        ds.schedule(event)
        self.assertEqual(mock_ap.add_job.call_args[1]['trigger'], "date")
        os.remove(db)


class TestReschedule(unittest.TestCase):
    def test_reschedule_cancels_then_registers(self):
        ds, store, llm, notifier, mock_ap, db = _make_scheduler(offsets=(15,))
        mock_ap.remove_job = MagicMock()  # succeeds silently

        event = _future_event(minutes_from_now=30)
        ds.reschedule(event)

        # remove_job called for the one offset
        mock_ap.remove_job.assert_called_once()
        # add_job called for the new fire time
        self.assertEqual(mock_ap.add_job.call_count, 1)
        os.remove(db)

    def test_reschedule_tolerates_missing_old_job(self):
        ds, store, llm, notifier, mock_ap, db = _make_scheduler(offsets=(15,))
        # remove_job raises (job never existed) — should not crash
        mock_ap.remove_job = MagicMock(side_effect=Exception("JobLookupError"))
        event = _future_event(minutes_from_now=30)
        ds.reschedule(event)           # must not raise
        self.assertEqual(mock_ap.add_job.call_count, 1)
        os.remove(db)


class TestFireReminder(unittest.TestCase):
    """Tests for the top-level _fire_reminder function called by APScheduler."""

    def _store(self):
        db = tempfile.mktemp(suffix=".db")
        return StateStore(db), db

    def test_calls_notification_service_with_deadline_reminder(self):
        store, db = self._store()
        llm = FakeLLM(); notifier = FakeNotifier()
        svc = NotificationService(llm, notifier)
        _fire_reminder("c1","a1","DBMS HW1", 60, store, svc)
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(llm.calls[0][0], EventType.DEADLINE_REMINDER)
        self.assertEqual(llm.calls[0][1], "DBMS HW1")
        os.remove(db)

    def test_marks_reminder_sent_after_delivery(self):
        store, db = self._store()
        svc = NotificationService(FakeLLM(), FakeNotifier())
        _fire_reminder("c1","a1","DBMS HW1", 60, store, svc)
        self.assertTrue(store.has_sent_reminder("c1","a1", 60))
        os.remove(db)

    def test_skips_if_already_sent(self):
        store, db = self._store()
        store.mark_reminder_sent("c1","a1", 60)
        llm = FakeLLM(); svc = NotificationService(llm, FakeNotifier())
        _fire_reminder("c1","a1","DBMS HW1", 60, store, svc)
        self.assertEqual(len(llm.calls), 0)
        os.remove(db)

    def test_does_not_mark_sent_if_delivery_fails(self):
        store, db = self._store()
        llm = FakeLLM(fail=True)
        svc = NotificationService(llm, FakeNotifier())
        _fire_reminder("c1","a1","DBMS HW1", 60, store, svc)
        self.assertFalse(store.has_sent_reminder("c1","a1", 60))
        os.remove(db)

    def test_all_four_offsets_fire_independently(self):
        store, db = self._store()
        svc = NotificationService(FakeLLM(), FakeNotifier())
        for offset in (24*60, 6*60, 60, 15):
            _fire_reminder("c1","a1","DBMS HW1", offset, store, svc)
        for offset in (24*60, 6*60, 60, 15):
            self.assertTrue(store.has_sent_reminder("c1","a1", offset))
        os.remove(db)


class TestDeadlineAlertMessages(unittest.TestCase):
    """Each reminder must generate a FRESH AI message (not cached/hardcoded)."""

    def test_each_reminder_generates_unique_llm_call(self):
        store = StateStore(tempfile.mktemp(suffix=".db"))
        llm = FakeLLM()
        svc = NotificationService(llm, FakeNotifier())
        for offset in (24*60, 6*60, 60, 15):
            _fire_reminder("c1","a1","OS Assignment", offset, store, svc)
        self.assertEqual(len(llm.calls), 4)
        # Every call must be DEADLINE_REMINDER (not NEW_ASSIGNMENT)
        for et, name in llm.calls:
            self.assertEqual(et, EventType.DEADLINE_REMINDER)
            self.assertEqual(name, "OS Assignment")


class TestEndToEndFeature3(unittest.TestCase):
    """Full flow: watcher detects new assignment → handle_event → jobs registered."""

    def test_new_assignment_schedules_reminders(self):
        import classpilot.watcher as wm
        wm.classroom = types.SimpleNamespace(
            list_courses=lambda page_size=20, course_states=None: [{"id":"c1","name":"DBMS"}],
            list_assignments=lambda course_id, page_size=20: [{
                "id":"a1","title":"DBMS HW1",
                "due_date":{"year":2027,"month":1,"day":1},
                "due_time":{"hours":23,"minutes":0,"seconds":0},
            }],
        )
        from classpilot.watcher import AssignmentWatcher
        from classpilot.main import build_handle_event

        cfg = ClassPilotConfig.__new__(ClassPilotConfig)
        cfg.deadline_alert_offsets_minutes = (24*60, 6*60, 60, 15)
        db = tempfile.mktemp(suffix=".db")
        store = StateStore(db)
        llm = FakeLLM(); notifier = FakeNotifier()
        svc = NotificationService(llm, notifier)
        ds = DeadlineScheduler(store, svc, cfg)
        mock_ap = MagicMock()
        mock_ap.remove_job = MagicMock(side_effect=Exception("not found"))
        ds.attach_scheduler(mock_ap)

        watcher = AssignmentWatcher(store, on_event=build_handle_event(svc, ds))
        watcher.check_once()

        # Feature 2: new-assignment notification fired
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(llm.calls[0][0], EventType.NEW_ASSIGNMENT)
        # Feature 3: all 4 reminder jobs registered (deadline is far future)
        self.assertEqual(mock_ap.add_job.call_count, 4)
        os.remove(db)

    def test_deadline_update_reschedules_jobs(self):
        assignments = [{
            "id":"a1","title":"DBMS HW1",
            "due_date":{"year":2027,"month":1,"day":1},
            "due_time":{"hours":23,"minutes":0,"seconds":0},
        }]
        import classpilot.watcher as wm
        wm.classroom = types.SimpleNamespace(
            list_courses=lambda page_size=20, course_states=None: [{"id":"c1","name":"DBMS"}],
            list_assignments=lambda course_id, page_size=20: assignments,
        )
        from classpilot.watcher import AssignmentWatcher
        from classpilot.main import build_handle_event

        cfg = ClassPilotConfig.__new__(ClassPilotConfig)
        cfg.deadline_alert_offsets_minutes = (60, 15)
        db = tempfile.mktemp(suffix=".db")
        store = StateStore(db)
        svc = NotificationService(FakeLLM(), FakeNotifier())
        ds = DeadlineScheduler(store, svc, cfg)
        mock_ap = MagicMock()
        mock_ap.remove_job = MagicMock()
        ds.attach_scheduler(mock_ap)

        watcher = AssignmentWatcher(store, on_event=build_handle_event(svc, ds))
        watcher.check_once()             # detects new → 2 jobs added
        first_add_count = mock_ap.add_job.call_count

        # Change the deadline
        assignments[0]["due_date"] = {"year":2027,"month":6,"day":1}
        watcher.check_once()             # detects deadline_updated → reschedule

        # remove_job called for each old offset, add_job called again
        self.assertGreater(mock_ap.remove_job.call_count, 0)
        self.assertGreater(mock_ap.add_job.call_count, first_add_count)
        os.remove(db)


if __name__ == "__main__":
    unittest.main(verbosity=2)
