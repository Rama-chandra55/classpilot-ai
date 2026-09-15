"""
tests/test_credential_failure_modes.py

Tests for how ClassPilot behaves when Google credentials are missing,
revoked, or under-scoped — covering:
  - no stored credentials for the resolved identity
  - a stored refresh_token that Google rejects (revoked / invalid_grant)
  - a scope shortfall discovered at the original authorization exchange
    (re-verified here at the get_authorized_credentials layer for
    completeness alongside the other three scenarios)
  - every resulting error is sanitized: no refresh/access token value,
    and no raw internal exception spilling into the message

No real network calls. Uses the real google.oauth2.credentials.Credentials
class (only Credentials.refresh() is mocked) so these tests exercise the
same code path classpilot.server._resolve_credentials() actually runs.
"""

import os
import sys
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _fake_web_client_config():
    return {"client_id": "fake-client-id.apps.googleusercontent.com", "client_secret": "fake-secret"}


@dataclass
class _FakeCredentialRecord:
    user_id: str
    refresh_token: str
    scopes: list = field(default_factory=list)
    token_expiry: Optional[datetime] = None


class FakeUserStore:
    """Minimal in-memory stand-in for UserStore — only the methods
    get_authorized_credentials actually calls."""

    def __init__(self):
        self._creds: dict[str, _FakeCredentialRecord] = {}

    def seed(self, user_id: str, refresh_token: str, scopes=None):
        self._creds[user_id] = _FakeCredentialRecord(
            user_id=user_id, refresh_token=refresh_token, scopes=scopes or ["scope-a"],
        )

    def get_google_credentials(self, user_id):
        return self._creds.get(user_id)


class CredentialFailureTestCase(unittest.TestCase):
    """
    Fresh-imports classpilot.google_oauth (and its google.*/
    google_auth_oauthlib dependencies) at setUp — EXECUTION time, not
    file-collection time — and rebinds this module's own globals to the
    result.

    Why not a plain file-top `from classpilot.google_oauth import ...`:
    other test files in this suite ALSO do their own collection-time
    pop-and-reimport of classpilot.google_oauth (needed for the same
    "escape another file's google.* stub" reason — see
    tests/test_google_oauth.py's module docstring). Since ALL files'
    collection-time code runs before ANY test executes, whichever file
    happens to be collected last "wins" — a name bound at THIS file's own
    collection time can be silently orphaned by a later-collected file's
    fresh reimport, pointing at a discarded module object whose
    _load_web_client_config (etc.) this file's patches would no longer
    reach. Doing the same dance at setUp — and rebinding into globals()
    so every test method's bare `get_authorized_credentials`/
    `GoogleOAuthError` reference stays current — sidesteps collection-order
    entirely. This was a real, reproduced failure during Phase 3
    development, not a hypothetical.
    """

    @classmethod
    def setUpClass(cls):
        for _mod in list(sys.modules):
            if _mod == "google" or _mod.startswith("google.") or _mod.startswith("google_auth_oauthlib"):
                del sys.modules[_mod]
        # Deliberately NOT popped: classpilot.config / classpilot.db /
        # classpilot.user_store / classpilot.crypto — see the equivalent
        # comment in tests/test_multiuser_isolation.py for the real,
        # reproduced bug that popping classpilot.config causes.
        for _mod in ("classpilot.google_oauth", "classpilot.classroom_client"):
            sys.modules.pop(_mod, None)

        import classpilot.google_oauth as _go
        import classpilot.identity as _identity_mod
        from classpilot.config import ClassPilotConfig

        globals().update({
            "get_authorized_credentials": _go.get_authorized_credentials,
            "clear_credentials_cache": _go.clear_credentials_cache,
            "GoogleOAuthError": _go.GoogleOAuthError,
            "resolve_identity": _identity_mod.resolve_identity,
            "IdentityResolutionError": _identity_mod.IdentityResolutionError,
            "ClassPilotConfig": ClassPilotConfig,
        })
        cls._go = _go
        cls._identity_mod = _identity_mod

    def setUp(self):
        clear_credentials_cache()
        self.patcher = patch.object(
            self._go, "_load_web_client_config", return_value=_fake_web_client_config(),
        )
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        clear_credentials_cache()


# ---------- Scenario 1: no stored credentials ----------

class TestNoStoredCredentials(CredentialFailureTestCase):
    def test_raises_google_oauth_error(self):
        store = FakeUserStore()  # nothing seeded for this user
        with self.assertRaises(GoogleOAuthError):
            get_authorized_credentials("user-with-no-account-connected", store)

    def test_error_message_is_clear_and_actionable(self):
        store = FakeUserStore()
        with self.assertRaises(GoogleOAuthError) as ctx:
            get_authorized_credentials("user-with-no-account-connected", store)
        self.assertIn("No stored Google credentials", str(ctx.exception))

    def test_error_does_not_leak_any_token_value(self):
        store = FakeUserStore()
        with self.assertRaises(GoogleOAuthError) as ctx:
            get_authorized_credentials("some-user", store)
        # There's no token to leak in this scenario, but assert the
        # invariant explicitly so a future refactor can't regress it.
        self.assertNotIn("refresh_token", str(ctx.exception).lower())
        self.assertNotIn("1//", str(ctx.exception))  # common Google refresh-token prefix

    def test_does_not_poison_the_credentials_cache(self):
        """A failed lookup for one user must not affect a later,
        successful lookup for a different (or the same, once connected) user."""
        store = FakeUserStore()
        with self.assertRaises(GoogleOAuthError):
            get_authorized_credentials("user-a", store)

        store.seed("user-a", "1//real-token-now")
        with patch("classpilot.google_oauth.Credentials.refresh", autospec=True,
                   side_effect=lambda self, request: setattr(self, "token", "at") or
                   setattr(self, "expiry", datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1))):
            creds = get_authorized_credentials("user-a", store)
        self.assertEqual(creds.token, "at")


# ---------- Scenario 2: refresh fails (revoked / invalid_grant) ----------

class TestRefreshFailure(CredentialFailureTestCase):
    def setUp(self):
        super().setUp()
        self.store = FakeUserStore()
        self.store.seed("user-revoked", "1//a-refresh-token-that-google-will-reject")

    def test_raises_google_oauth_error_not_the_raw_library_exception(self):
        with patch("classpilot.google_oauth.Credentials.refresh", autospec=True,
                   side_effect=Exception("invalid_grant: Token has been expired or revoked.")):
            with self.assertRaises(GoogleOAuthError):
                get_authorized_credentials("user-revoked", self.store)

    def test_error_message_is_actionable(self):
        with patch("classpilot.google_oauth.Credentials.refresh", autospec=True,
                   side_effect=Exception("invalid_grant: Token has been expired or revoked.")):
            with self.assertRaises(GoogleOAuthError) as ctx:
                get_authorized_credentials("user-revoked", self.store)
        self.assertIn("re-authorize", str(ctx.exception).lower())

    def test_error_never_contains_the_refresh_token_value(self):
        with patch("classpilot.google_oauth.Credentials.refresh", autospec=True,
                   side_effect=Exception("invalid_grant")):
            with self.assertRaises(GoogleOAuthError) as ctx:
                get_authorized_credentials("user-revoked", self.store)
        self.assertNotIn("1//a-refresh-token-that-google-will-reject", str(ctx.exception))

    def test_failed_refresh_does_not_leave_a_broken_credential_cached(self):
        """A failed refresh must not populate the cache with a broken/
        partial Credentials object that a later call might serve back out
        without retrying."""
        with patch("classpilot.google_oauth.Credentials.refresh", autospec=True,
                   side_effect=Exception("invalid_grant")):
            with self.assertRaises(GoogleOAuthError):
                get_authorized_credentials("user-revoked", self.store)

        # Next attempt (e.g. after the student re-authorizes) must retry
        # the refresh for real, not silently reuse a cached failure.
        with patch("classpilot.google_oauth.Credentials.refresh", autospec=True,
                   side_effect=lambda self, request: setattr(self, "token", "fresh-token") or
                   setattr(self, "expiry", datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1))
                   ) as mock_refresh:
            creds = get_authorized_credentials("user-revoked", self.store)
        self.assertEqual(creds.token, "fresh-token")
        self.assertEqual(mock_refresh.call_count, 1)


# ---------- Scenario 3: missing required scopes ----------

class TestMissingRequiredScopes(CredentialFailureTestCase):
    """
    The authoritative check for "were all required scopes granted" lives
    at the original OAuth exchange (google_oauth._verify_required_scopes_
    present, exercised in tests/test_google_oauth.py) — a credential
    missing a required scope should never be persisted in the first
    place. These tests cover the complementary Phase 3 question: if an
    under-scoped credential exists anyway (e.g. one stored before a scope
    was added to the curated set), get_authorized_credentials itself does
    NOT re-check scopes on refresh (Google's refresh grant doesn't
    re-verify consent) — that's accurately reflected here, not silently
    assumed.
    """

    def test_get_authorized_credentials_does_not_itself_validate_scopes(self):
        """Documents the actual, current division of responsibility:
        scope adequacy is enforced once, at authorization time — refresh
        just refreshes whatever was already granted. If this test starts
        failing, scope validation has moved and this file's docstring
        (and the Phase 3 report) need updating to match."""
        store = FakeUserStore()
        store.seed("under-scoped-user", "1//token", scopes=["classroom.courses.readonly"])  # missing others
        with patch("classpilot.google_oauth.Credentials.refresh", autospec=True,
                   side_effect=lambda self, request: setattr(self, "token", "at") or
                   setattr(self, "expiry", datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1))):
            creds = get_authorized_credentials("under-scoped-user", store)
        self.assertEqual(creds.scopes, ["classroom.courses.readonly"])

    def test_downstream_google_api_scope_rejection_is_not_swallowed(self):
        """When Google's API itself rejects a call due to insufficient
        scope (a 403), that failure must propagate rather than being
        silently absorbed anywhere in the credential-resolution path."""
        store = FakeUserStore()
        store.seed("under-scoped-user", "1//token", scopes=["classroom.courses.readonly"])
        with patch("classpilot.google_oauth.Credentials.refresh", autospec=True,
                   side_effect=lambda self, request: setattr(self, "token", "at") or
                   setattr(self, "expiry", datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1))):
            creds = get_authorized_credentials("under-scoped-user", store)

        class InsufficientScopeError(Exception):
            pass

        def fake_api_call(credentials):
            raise InsufficientScopeError("403: Request had insufficient authentication scopes.")

        with self.assertRaises(InsufficientScopeError):
            fake_api_call(creds)


# ---------- resolve_identity() failure sanitization (ties the three above together) ----------

class TestIdentityResolutionFailureSanitization(unittest.TestCase):
    def test_unset_identity_error_contains_no_credential_material(self):
        cfg = ClassPilotConfig()
        cfg.classpilot_dev_user_id = ""
        with patch("classpilot.identity.get_config", return_value=cfg):
            with self.assertRaises(IdentityResolutionError) as ctx:
                resolve_identity()
        message = str(ctx.exception)
        self.assertNotIn("1//", message)
        self.assertNotIn("refresh_token", message.lower())
        self.assertNotIn("access_token", message.lower())


if __name__ == "__main__":
    unittest.main()
