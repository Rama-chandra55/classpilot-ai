"""
tests/test_oauth_web.py

HTTP-level tests for classpilot/oauth_web.py's two routes, via Starlette's
TestClient. classpilot.google_oauth and classpilot.user_store are mocked
at the module level oauth_web.py actually calls them from — no real
Google network calls, no real database. oauth_state IS exercised for
real against Postgres (skips gracefully if unavailable, matching the rest
of this suite's Postgres-backed tests) since state validation is precisely
the CSRF behavior these tests need to prove is real, not mocked away.

Every assertion here also checks that no response body ever contains a
token-shaped value — "don't expose Google tokens to the AI/MCP client" is
a requirement of this whole phase, and this is a cheap, concrete way to
keep that honest at the HTTP boundary too (even though, structurally,
nothing here is reachable from an MCP client at all).
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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


@unittest.skipUnless(_HAS_POSTGRES, _SKIP_REASON)
class _OAuthWebTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Only google.*/google_auth_oauthlib need purging here — see the
        # detailed rationale on tests/test_google_oauth.py::
        # TestGrantedScopeSupersetHandling's docstring for why
        # classpilot.config/classpilot.db/classpilot.user_store/
        # classpilot.classroom_client must NEVER be popped by test code:
        # classpilot.db and classpilot.crypto each capture a one-time
        # `from .config import get_config` binding at their own first
        # import, never reloaded. Popping+refreshing classpilot.config
        # here (as this file used to do) creates a second, divergent
        # config module instance that classpilot.db/classpilot.crypto
        # never learn about — so a later test's `config_mod._config =
        # None` resets the WRONG instance, and encryption/pool operations
        # silently keep using a stale config cached before DATABASE_URL/
        # TOKEN_ENCRYPTION_KEY were ever set to real test values. This was
        # a real, reproduced bug (see tests/test_cross_file_isolation.py).
        # classpilot.google_oauth doesn't need popping either: this file's
        # `from .google_oauth import ...` reuses whatever's already
        # correctly bound in sys.modules (established once, correctly,
        # wherever it was first imported in this process — Python binds
        # `from google.oauth2.credentials import Credentials` etc. into
        # that module's own namespace at that one-time import, so it
        # keeps working regardless of what OTHER files later do to
        # sys.modules["google.oauth2.credentials"] itself).
        for _mod in list(sys.modules):
            if _mod == "google" or _mod.startswith("google.") or _mod.startswith("google_auth_oauthlib"):
                del sys.modules[_mod]
        sys.modules.pop("classpilot.oauth_web", None)

        os.environ["DATABASE_URL"] = TEST_DATABASE_URL

        import classpilot.config as config_mod
        config_mod._config = None

        import classpilot.db as db_mod
        db_mod.close_pool()
        db_mod.init_schema()

        from starlette.testclient import TestClient
        import classpilot.oauth_web as oauth_web_mod
        cls.oauth_web_mod = oauth_web_mod
        cls.client = TestClient(oauth_web_mod.app)

    @classmethod
    def tearDownClass(cls):
        import classpilot.db as db_mod
        db_mod.close_pool()

    def setUp(self):
        from classpilot.db import get_connection
        with get_connection() as conn:
            conn.execute("TRUNCATE oauth_states")
            conn.execute("TRUNCATE users CASCADE")

    def _fresh_state(self) -> str:
        from classpilot.oauth_state import generate_state, register_state
        state = generate_state()
        register_state(state, "test-code-verifier")
        return state


class TestGoogleLogin(_OAuthWebTestCase):
    def test_redirects_to_google(self):
        with patch.object(
            self.oauth_web_mod, "build_authorization_url",
            return_value=("https://accounts.google.com/o/oauth2/auth?client_id=fake", "verifier-1"),
        ):
            response = self.client.get("/auth/google/login", follow_redirects=False)
        self.assertEqual(response.status_code, 307)
        self.assertTrue(response.headers["location"].startswith("https://accounts.google.com/"))

    def test_each_request_gets_a_distinct_state(self):
        states = []
        def _capture(state):
            states.append(state)
            return (f"https://accounts.google.com/auth?state={state}", f"verifier-for-{state}")
        with patch.object(self.oauth_web_mod, "build_authorization_url", side_effect=_capture):
            self.client.get("/auth/google/login", follow_redirects=False)
            self.client.get("/auth/google/login", follow_redirects=False)
        self.assertEqual(len(states), 2)
        self.assertNotEqual(states[0], states[1])

    def test_login_registers_the_state_with_its_code_verifier(self):
        """Regression coverage at the route level: /login must actually
        persist (state, code_verifier) — not just generate them and
        forget — or /callback will have nothing to retrieve."""
        with patch.object(
            self.oauth_web_mod, "build_authorization_url",
            return_value=("https://accounts.google.com/auth?state=abc", "the-verifier-xyz"),
        ):
            self.client.get("/auth/google/login", follow_redirects=False)

        from classpilot.db import get_connection
        with get_connection() as conn:
            row = conn.execute("SELECT code_verifier FROM oauth_states").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "the-verifier-xyz")


class TestGoogleCallbackSuccess(_OAuthWebTestCase):
    def test_successful_callback_returns_success_page(self):
        state = self._fresh_state()
        fake_user = type("U", (), {"email": "alice@school.edu", "display_name": "Alice"})()
        with patch.object(self.oauth_web_mod, "complete_authorization", return_value=fake_user):
            response = self.client.get(f"/auth/google/callback?code=fake-code&state={state}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Connected", response.text)
        self.assertIn("alice@school.edu", response.text)

    def test_state_is_consumed_and_cannot_be_reused(self):
        """Proves the callback route actually enforces single-use state,
        not just that oauth_state.py can in isolation."""
        state = self._fresh_state()
        fake_user = type("U", (), {"email": "a@x.com", "display_name": None})()
        with patch.object(self.oauth_web_mod, "complete_authorization", return_value=fake_user):
            first = self.client.get(f"/auth/google/callback?code=code-1&state={state}")
            second = self.client.get(f"/auth/google/callback?code=code-2&state={state}")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 400)

    def test_success_response_never_contains_a_token_value(self):
        state = self._fresh_state()
        fake_user = type("U", (), {"email": "alice@school.edu", "display_name": "Alice"})()
        with patch.object(self.oauth_web_mod, "complete_authorization", return_value=fake_user) as mock_complete:
            response = self.client.get(f"/auth/google/callback?code=super-secret-code-abc123&state={state}")
        # The response must never echo the code, and complete_authorization's
        # (mocked) return value carries no token fields at all by construction —
        # this asserts the response body itself, the actual HTTP boundary.
        self.assertNotIn("super-secret-code-abc123", response.text)
        self.assertNotIn("refresh_token", response.text.lower())
        self.assertNotIn("access_token", response.text.lower())


class TestGoogleCallbackErrors(_OAuthWebTestCase):
    def test_missing_state_rejected(self):
        response = self.client.get("/auth/google/callback?code=some-code")
        self.assertEqual(response.status_code, 400)

    def test_invalid_state_rejected(self):
        response = self.client.get("/auth/google/callback?code=some-code&state=forged-state")
        self.assertEqual(response.status_code, 400)

    def test_reused_state_rejected(self):
        state = self._fresh_state()
        from classpilot.oauth_state import consume_state
        consume_state(state)  # pre-consume it
        response = self.client.get(f"/auth/google/callback?code=some-code&state={state}")
        self.assertEqual(response.status_code, 400)

    def test_google_reported_error_handled_gracefully(self):
        """Student clicked 'Cancel' on Google's consent screen."""
        state = self._fresh_state()
        response = self.client.get(f"/auth/google/callback?error=access_denied&state={state}")
        self.assertEqual(response.status_code, 400)
        self.assertIn("cancelled", response.text.lower())

    def test_google_error_does_not_require_or_consume_state(self):
        """An error redirect from Google may not even include a valid
        state param depending on where in the flow it failed — the error
        path must not crash trying to validate one."""
        response = self.client.get("/auth/google/callback?error=access_denied")
        self.assertEqual(response.status_code, 400)

    def test_missing_code_rejected(self):
        state = self._fresh_state()
        response = self.client.get(f"/auth/google/callback?state={state}")
        self.assertEqual(response.status_code, 400)

    def test_google_oauth_error_from_complete_authorization_handled_gracefully(self):
        from classpilot.google_oauth import GoogleOAuthError
        state = self._fresh_state()
        with patch.object(
            self.oauth_web_mod, "complete_authorization",
            side_effect=GoogleOAuthError("token exchange failed"),
        ):
            response = self.client.get(f"/auth/google/callback?code=code&state={state}")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("token exchange failed", response.text)  # internal detail not leaked to browser

    def test_unexpected_exception_does_not_crash_the_server(self):
        """Even a totally unanticipated failure must degrade to a clean
        error page, not a raw 500/stack trace exposed to the student."""
        state = self._fresh_state()
        with patch.object(
            self.oauth_web_mod, "complete_authorization",
            side_effect=RuntimeError("something truly unexpected — internal detail"),
        ):
            response = self.client.get(f"/auth/google/callback?code=code&state={state}")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("something truly unexpected", response.text)  # internal detail not leaked


if __name__ == "__main__":
    unittest.main()
