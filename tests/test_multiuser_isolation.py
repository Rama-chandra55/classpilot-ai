"""
tests/test_multiuser_isolation.py

Two-user isolation coverage for Phase 3 — the core promise of this phase
is that identifying a user internally (never via a caller-supplied
user_id) and threading their own Google credentials through every tool
actually keeps two students' data, watchers, and state completely apart.

Sections:
  - Credentials (Postgres-backed)
  - Tool execution routing (mocked study_client, real server.py tools)
  - Drive/material access (get_material)
  - State store physical isolation (SQLite — the widened-PK fix)
  - Watchers (user-scoped AppServices)
  - Caches (_credentials_cache / server._user_services)
  - Concurrent identity isolation, tested at components that already take
    explicit user_id/credentials (get_authorized_credentials) — NOT by
    monkeypatching resolve_identity() differently per thread, which would
    only be meaningful once Phase 4 makes resolve_identity() itself
    context-aware.

Postgres-backed sections skip gracefully (not a failure) if no Postgres
is reachable, matching the rest of this suite.
"""

import os
import sys
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Force a clean re-import of the real google.*/google_auth_oauthlib
# libraries — see tests/test_google_oauth.py's module docstring for the
# full rationale (other test files stub these at their own collection
# time; this file drives real Credentials objects, only mocking
# Credentials.refresh()).
# NOTE: module-level google.*/classpilot purging deliberately NOT done here —
# see _PostgresTestCase.setUpClass below, which does it at EXECUTION time
# instead. Doing it at collection time is unreliable: every test file's
# collection-time code runs before ANY test executes, so a later-collected
# file's `sys.modules["google.oauth2"] = <path-less fake>` stub silently
# wins, and classpilot.google_oauth's real `from google.oauth2 import
# id_token` then fails with "unknown location". This was a real, reproduced
# failure during Phase 3 development.

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


_HAS_POSTGRES = _postgres_available()
_SKIP_REASON = f"No reachable Postgres at {TEST_DATABASE_URL} — see README's Database setup section."


def _make_refreshing_side_effect(access_token: str):
    def _refresh(self, request):
        self.token = access_token
        self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
    return _refresh


@unittest.skipUnless(_HAS_POSTGRES, _SKIP_REASON)
class _PostgresTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Purge and fresh-import at EXECUTION time (not collection time) —
        # see the note at the top of this file for why. apscheduler is
        # included because other files stub it with non-functional fakes
        # that AppServices construction would otherwise pick up.
        for _mod in list(sys.modules):
            if _mod == "google" or _mod.startswith("google.") \
               or _mod.startswith("google_auth_oauthlib") or _mod.startswith("apscheduler"):
                del sys.modules[_mod]
        # Deliberately NOT popped: classpilot.config / classpilot.db /
        # classpilot.user_store / classpilot.crypto. classpilot.db and
        # classpilot.crypto each capture a one-time `from .config import
        # get_config` binding at their own first import and are never
        # reloaded, so replacing classpilot.config with a second module
        # instance permanently diverges them from the classpilot PACKAGE's
        # own `.config` attribute — a later test's `config_mod._config =
        # None` then resets the WRONG instance and DB/encryption silently
        # use a stale config. That was a real, reproduced Phase 2 bug; see
        # tests/test_cross_file_isolation.py.
        for _mod in ("classpilot.google_oauth", "classpilot.classroom_client",
                     "classpilot.server", "classpilot.services", "classpilot.watcher",
                     "classpilot.deadline_scheduler", "classpilot.scheduler"):
            sys.modules.pop(_mod, None)

        os.environ["DATABASE_URL"] = TEST_DATABASE_URL
        os.environ["TOKEN_ENCRYPTION_KEY"] = os.environ.get("TOKEN_ENCRYPTION_KEY") or _generate_key()

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
        from classpilot.db import get_connection
        from classpilot.google_oauth import clear_credentials_cache
        with get_connection() as conn:
            conn.execute("TRUNCATE users CASCADE")
        clear_credentials_cache()
        self._config_patcher = patch(
            "classpilot.google_oauth._load_web_client_config",
            return_value={"client_id": "fake.apps.googleusercontent.com", "client_secret": "fake-secret"},
        )
        self._config_patcher.start()

    def tearDown(self):
        self._config_patcher.stop()
        from classpilot.google_oauth import clear_credentials_cache
        clear_credentials_cache()

    @staticmethod
    def _unique_sub() -> str:
        return f"test-sub-{uuid.uuid4()}"

    def _make_two_users(self):
        from classpilot.user_store import UserStore
        store = UserStore()
        user_a = store.get_or_create_user(self._unique_sub(), email="alice@school.edu", display_name="Alice")
        user_b = store.get_or_create_user(self._unique_sub(), email="bob@school.edu", display_name="Bob")
        store.save_google_credentials(user_a.id, refresh_token="1//refresh-token-A", scopes=["scope-a"])
        store.save_google_credentials(user_b.id, refresh_token="1//refresh-token-B", scopes=["scope-a"])
        return store, user_a, user_b


def _generate_key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


# ── Credentials isolation ───────────────────────────────────────────────────

class TestCredentialIsolation(_PostgresTestCase):
    def test_each_user_gets_their_own_refresh_token(self):
        from classpilot.google_oauth import get_authorized_credentials
        store, user_a, user_b = self._make_two_users()

        with patch("classpilot.google_oauth.Credentials.refresh", autospec=True,
                   side_effect=_make_refreshing_side_effect("token-for-A")):
            creds_a = get_authorized_credentials(user_a.id, store)
        with patch("classpilot.google_oauth.Credentials.refresh", autospec=True,
                   side_effect=_make_refreshing_side_effect("token-for-B")):
            creds_b = get_authorized_credentials(user_b.id, store)

        self.assertEqual(creds_a.refresh_token, "1//refresh-token-A")
        self.assertEqual(creds_b.refresh_token, "1//refresh-token-B")
        self.assertNotEqual(creds_a.token, creds_b.token)

    def test_cached_credentials_are_never_shared_between_users(self):
        from classpilot.google_oauth import get_authorized_credentials
        store, user_a, user_b = self._make_two_users()

        with patch("classpilot.google_oauth.Credentials.refresh", autospec=True,
                   side_effect=_make_refreshing_side_effect("token-A")):
            creds_a_first = get_authorized_credentials(user_a.id, store)
        with patch("classpilot.google_oauth.Credentials.refresh", autospec=True,
                   side_effect=_make_refreshing_side_effect("token-B")):
            creds_b_first = get_authorized_credentials(user_b.id, store)

        # Second calls — should hit each user's OWN cache entry, not the other's.
        creds_a_second = get_authorized_credentials(user_a.id, store)
        creds_b_second = get_authorized_credentials(user_b.id, store)

        self.assertIs(creds_a_first, creds_a_second)
        self.assertIs(creds_b_first, creds_b_second)
        self.assertIsNot(creds_a_second, creds_b_second)

    def test_clearing_one_users_cache_does_not_affect_the_other(self):
        from classpilot.google_oauth import get_authorized_credentials, clear_credentials_cache
        store, user_a, user_b = self._make_two_users()

        with patch("classpilot.google_oauth.Credentials.refresh", autospec=True,
                   side_effect=_make_refreshing_side_effect("token-A")) as mock_refresh_a:
            get_authorized_credentials(user_a.id, store)
        with patch("classpilot.google_oauth.Credentials.refresh", autospec=True,
                   side_effect=_make_refreshing_side_effect("token-B")):
            get_authorized_credentials(user_b.id, store)

        clear_credentials_cache(user_a.id)

        # A's cache cleared -> refreshes again; B's cache untouched -> no refresh.
        with patch("classpilot.google_oauth.Credentials.refresh", autospec=True,
                   side_effect=_make_refreshing_side_effect("token-A-2")) as mock_refresh_a2:
            get_authorized_credentials(user_a.id, store)
        self.assertEqual(mock_refresh_a2.call_count, 1)

        with patch("classpilot.google_oauth.Credentials.refresh", autospec=True) as mock_refresh_b2:
            get_authorized_credentials(user_b.id, store)
        mock_refresh_b2.assert_not_called()


# ── Tool execution routing ──────────────────────────────────────────────────

class TestToolExecutionIsolation(_PostgresTestCase):
    """
    Confirms server.py's tools resolve identity->credentials internally
    and pass the CORRECT user's credentials into study_client — never a
    caller-supplied value (no tool takes a user_id parameter at all) and
    never another user's cached credentials.
    """

    def test_list_classes_uses_the_resolved_users_credentials(self):
        import classpilot.server as srv
        store, user_a, user_b = self._make_two_users()

        captured_credentials = []

        def fake_fetch_courses(credentials, page_size=100):
            captured_credentials.append(credentials)
            return []

        with patch.object(srv, "fetch_courses", side_effect=fake_fetch_courses):
            with patch.object(srv, "_resolve_credentials", return_value=(
                MagicMock(user_id=user_a.id), "credentials-for-A",
            )):
                srv.list_classes()
            with patch.object(srv, "_resolve_credentials", return_value=(
                MagicMock(user_id=user_b.id), "credentials-for-B",
            )):
                srv.list_classes()

        self.assertEqual(captured_credentials, ["credentials-for-A", "credentials-for-B"])

    def test_list_classes_signature_has_no_user_identity_parameter(self):
        """The core requirement: user_id must never be a tool argument."""
        import inspect
        import classpilot.server as srv
        for tool_name in (
            "list_classes", "list_modules", "list_materials", "get_material",
            "search_classroom", "get_upcoming_deadlines",
            "start_assignment_watcher", "stop_assignment_watcher",
        ):
            fn = getattr(srv, tool_name)
            params = set(inspect.signature(fn).parameters)
            self.assertFalse(
                params & {"user_id", "identity", "user", "uid"},
                f"{tool_name} must not accept a caller-controlled identity parameter, got {params}",
            )


# ── Drive / material access isolation ───────────────────────────────────────

class TestMaterialAccessIsolation(_PostgresTestCase):
    def test_get_material_reads_drive_files_with_the_resolved_users_credentials(self):
        import classpilot.server as srv
        store, user_a, user_b = self._make_two_users()

        material = {
            "id": "m1", "kind": "MATERIAL", "title": "Notes.pdf", "description": "",
            "topic_id": "1", "state": "PUBLISHED", "due_date": None, "due_time": None,
            "link": "http://x", "created": "",
            "attachments": [{"type": "drive", "id": "f1", "title": "Notes.pdf", "link": "http://drive/f1"}],
        }
        fake_file_result = {
            "readable": True, "content": "hello", "mime_type": "application/pdf",
            "name": "Notes.pdf", "link": "http://drive/f1", "note": "", "visuals": [],
        }

        captured = []

        def fake_read_drive_file(credentials, file_id):
            captured.append(credentials)
            return fake_file_result

        with patch.object(srv, "fetch_course_work_materials", return_value=[material]), \
             patch.object(srv, "read_drive_file", side_effect=fake_read_drive_file):
            with patch.object(srv, "_resolve_credentials", return_value=(
                MagicMock(user_id=user_a.id), "creds-A",
            )):
                srv.get_material(course_id="c1", material_id="m1")
            with patch.object(srv, "_resolve_credentials", return_value=(
                MagicMock(user_id=user_b.id), "creds-B",
            )):
                srv.get_material(course_id="c1", material_id="m1")

        self.assertEqual(captured, ["creds-A", "creds-B"])


# ── State store physical isolation (SQLite — no Postgres needed) ───────────

class TestStateStoreIsolation(unittest.TestCase):
    """
    The exact bug the widened PRIMARY KEY fixes: two students sharing a
    course_id + assignment_id (Classroom IDs aren't per-student) must
    never overwrite or block each other's dedup state.
    """

    def setUp(self):
        from classpilot.state_store import StateStore
        self.db = tempfile.mktemp(suffix=".db")
        self.store = StateStore(self.db)

    def tearDown(self):
        if os.path.exists(self.db):
            os.remove(self.db)

    def test_known_assignment_upsert_does_not_overwrite_another_users_row(self):
        self.store.upsert_assignment("c1", "a1", "A's title", None, None, user_id="userA")
        self.store.upsert_assignment("c1", "a1", "B's title", None, None, user_id="userB")

        row_a = self.store.get_known_assignment("c1", "a1", user_id="userA")
        row_b = self.store.get_known_assignment("c1", "a1", user_id="userB")
        self.assertEqual(row_a["title"], "A's title")
        self.assertEqual(row_b["title"], "B's title")

    def test_reminder_dedup_is_independent_per_user_for_the_same_assignment(self):
        self.store.mark_reminder_sent("c1", "a1", 60, user_id="userA")
        self.assertTrue(self.store.has_sent_reminder("c1", "a1", 60, user_id="userA"))
        self.assertFalse(self.store.has_sent_reminder("c1", "a1", 60, user_id="userB"))

        self.store.mark_reminder_sent("c1", "a1", 60, user_id="userB")
        self.assertTrue(self.store.has_sent_reminder("c1", "a1", 60, user_id="userB"))

    def test_updating_users_own_assignment_repeatedly_does_not_affect_the_other_user(self):
        self.store.upsert_assignment("c1", "a1", "A v1", None, None, user_id="userA")
        self.store.upsert_assignment("c1", "a1", "B v1", None, None, user_id="userB")
        self.store.upsert_assignment("c1", "a1", "A v2", {"year": 2027, "month": 1, "day": 1}, None, user_id="userA")

        self.assertEqual(self.store.get_known_assignment("c1", "a1", user_id="userA")["title"], "A v2")
        self.assertEqual(self.store.get_known_assignment("c1", "a1", user_id="userB")["title"], "B v1")


# ── Watcher isolation ────────────────────────────────────────────────────────

class TestWatcherIsolation(_PostgresTestCase):
    def test_starting_user_a_watcher_does_not_create_or_affect_user_bs_watcher(self):
        import classpilot.server as srv
        store, user_a, user_b = self._make_two_users()
        srv._user_services.clear()
        self.addCleanup(srv._user_services.clear)

        with patch("classpilot.services.build_scheduler") as mock_build_sched, \
             patch("classpilot.services.create_llm_provider", return_value=MagicMock()), \
             patch("classpilot.services.create_notifier", return_value=MagicMock()):
            mock_apscheduler = MagicMock(running=True)
            mock_build_sched.return_value = mock_apscheduler

            with patch.object(srv, "_resolve_credentials", return_value=(
                MagicMock(user_id=user_a.id), "creds-A",
            )):
                srv.start_assignment_watcher()

        self.assertIn(user_a.id, srv._user_services)
        self.assertNotIn(user_b.id, srv._user_services)
        self.assertTrue(srv._user_services[user_a.id].is_watcher_running)

    def test_user_b_cannot_stop_user_as_watcher(self):
        """The core requirement: stop_assignment_watcher only ever
        resolves and acts on the CALLER's own AppServices entry — there
        is no way to address another user's watcher because user_id is
        never a parameter (see TestToolExecutionIsolation) and the lookup
        key always comes from _resolve_credentials()."""
        import classpilot.server as srv
        store, user_a, user_b = self._make_two_users()
        srv._user_services.clear()
        self.addCleanup(srv._user_services.clear)

        with patch("classpilot.services.build_scheduler") as mock_build_sched, \
             patch("classpilot.services.create_llm_provider", return_value=MagicMock()), \
             patch("classpilot.services.create_notifier", return_value=MagicMock()):
            mock_apscheduler_a = MagicMock(running=True)
            mock_build_sched.return_value = mock_apscheduler_a
            with patch.object(srv, "_resolve_credentials", return_value=(
                MagicMock(user_id=user_a.id), "creds-A",
            )):
                srv.start_assignment_watcher()

            # User B calls stop_assignment_watcher — resolves to B's OWN
            # (nonexistent-yet) entry, never touches A's. Still inside the
            # AppServices-construction patches, since resolving B's own
            # entry for the first time also builds a fresh AppServices.
            with patch.object(srv, "_resolve_credentials", return_value=(
                MagicMock(user_id=user_b.id), "creds-B",
            )):
                result = srv.stop_assignment_watcher()

        self.assertFalse(result.running)  # B never had one running
        self.assertTrue(srv._user_services[user_a.id].is_watcher_running)  # A's untouched
        mock_apscheduler_a.shutdown.assert_not_called()

    def test_watcher_polls_classroom_with_its_owners_credentials(self):
        import classpilot.server as srv
        store, user_a, user_b = self._make_two_users()
        srv._user_services.clear()
        self.addCleanup(srv._user_services.clear)

        with patch("classpilot.services.build_scheduler"), \
             patch("classpilot.services.create_llm_provider", return_value=MagicMock()), \
             patch("classpilot.services.create_notifier", return_value=MagicMock()), \
             patch.object(srv, "_resolve_credentials", return_value=(
                MagicMock(user_id=user_a.id), "creds-A-object",
            )):
            identity, credentials = srv._resolve_credentials()
            services = srv._get_user_services(identity, credentials)

        self.assertEqual(services.watcher._credentials, "creds-A-object")
        self.assertEqual(services.watcher._user_id, user_a.id)


# ── Cache isolation ──────────────────────────────────────────────────────────

class TestCacheIsolation(_PostgresTestCase):
    def test_user_services_dict_has_independent_entries_per_user(self):
        import classpilot.server as srv
        store, user_a, user_b = self._make_two_users()
        srv._user_services.clear()
        self.addCleanup(srv._user_services.clear)

        identity_a = MagicMock(user_id=user_a.id)
        identity_b = MagicMock(user_id=user_b.id)

        with patch("classpilot.services.build_scheduler"), \
             patch("classpilot.services.create_llm_provider", return_value=MagicMock()), \
             patch("classpilot.services.create_notifier", return_value=MagicMock()):
            services_a = srv._get_user_services(identity_a, "creds-A")
            services_b = srv._get_user_services(identity_b, "creds-B")

        self.assertIsNot(services_a, services_b)
        self.assertEqual(services_a.user_id, user_a.id)
        self.assertEqual(services_b.user_id, user_b.id)
        self.assertEqual(services_a.credentials, "creds-A")
        self.assertEqual(services_b.credentials, "creds-B")

    def test_looking_up_the_same_user_twice_returns_the_same_instance(self):
        import classpilot.server as srv
        store, user_a, _user_b = self._make_two_users()
        srv._user_services.clear()
        self.addCleanup(srv._user_services.clear)

        identity_a = MagicMock(user_id=user_a.id)
        with patch("classpilot.services.build_scheduler"), \
             patch("classpilot.services.create_llm_provider", return_value=MagicMock()), \
             patch("classpilot.services.create_notifier", return_value=MagicMock()):
            first = srv._get_user_services(identity_a, "creds-A")
            second = srv._get_user_services(identity_a, "creds-A")
        self.assertIs(first, second)


# ── Concurrent identity isolation ───────────────────────────────────────────

class TestConcurrentIdentityIsolation(_PostgresTestCase):
    """
    Per the approved plan: concurrency is tested at a component that
    already accepts explicit user identity/credentials
    (get_authorized_credentials(user_id, store)) — not by monkeypatching
    resolve_identity() differently per thread, which would only be
    meaningful once Phase 4 makes it context-aware. This proves the
    ALREADY-lock-guarded _credentials_cache in google_oauth.py is
    genuinely safe under real concurrent, multi-user load.
    """

    def test_concurrent_lookups_for_two_users_never_cross_over(self):
        from classpilot.google_oauth import get_authorized_credentials
        store, user_a, user_b = self._make_two_users()

        def _dynamic_refresh(self, request):
            # Deliberately keyed off the credentials instance's OWN
            # refresh_token, not a per-thread patch — patch() itself
            # isn't safe to enter/exit concurrently from multiple
            # threads against the same class attribute, so this test
            # uses exactly ONE patch for the whole concurrent run.
            self.token = f"access-token-for::{self.refresh_token}"
            self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)

        def resolve_for(user_id, expected_refresh_token):
            creds = get_authorized_credentials(user_id, store)
            return creds.refresh_token == expected_refresh_token and \
                creds.token == f"access-token-for::{expected_refresh_token}"

        tasks = [(user_a.id, "1//refresh-token-A")] * 20 + [(user_b.id, "1//refresh-token-B")] * 20

        with patch("classpilot.google_oauth.Credentials.refresh", autospec=True, side_effect=_dynamic_refresh):
            with ThreadPoolExecutor(max_workers=10) as pool:
                results = list(pool.map(lambda t: resolve_for(*t), tasks))

        self.assertTrue(all(results), "Some concurrent lookup returned the WRONG user's refresh token")

    def test_concurrent_lookups_populate_exactly_two_cache_entries(self):
        from classpilot.google_oauth import get_authorized_credentials, _credentials_cache
        store, user_a, user_b = self._make_two_users()

        def _dynamic_refresh(self, request):
            self.token = f"access-for::{self.refresh_token}"
            self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)

        tasks = [user_a.id, user_b.id] * 25
        with patch("classpilot.google_oauth.Credentials.refresh", autospec=True, side_effect=_dynamic_refresh):
            with ThreadPoolExecutor(max_workers=10) as pool:
                list(pool.map(get_authorized_credentials, tasks, [store] * len(tasks)))

        cached_for_test_users = {k for k in _credentials_cache if k in (user_a.id, user_b.id)}
        self.assertEqual(cached_for_test_users, {user_a.id, user_b.id})


if __name__ == "__main__":
    unittest.main()
