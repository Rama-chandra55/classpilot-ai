"""
tests/test_google_oauth.py

Unit tests for classpilot/google_oauth.py. No real network calls and no
Postgres needed — Google's Flow/Credentials/id_token verification are
mocked at their exact network-touching boundaries (Flow.fetch_token,
Credentials.refresh, id_token.verify_oauth2_token), while everything
around them (scope handling, ExchangedIdentity construction, the
refresh-token-preservation rule, and the refresh-only-when-needed cache)
runs for real. UserStore is a lightweight hand-rolled fake here rather
than the real Postgres-backed one, so these tests are fast, deterministic,
and don't depend on a database — the real UserStore is already covered
end-to-end by tests/test_user_store.py.
"""

import os
import sys
import unittest
import contextlib
import importlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from unittest.mock import MagicMock, patch

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Other test files in this suite stub google.oauth2.credentials /
# google.auth.transport.requests / google_auth_oauthlib.flow with fake,
# path-less ModuleType objects (see the identical rationale documented at
# the top of tests/test_scope_audit.py for classroom_suite_mcp — same
# root cause: pytest collects every test file into one shared sys.modules
# cache before running any of them). Those fakes have no real __path__, so
# `from google.oauth2 import id_token` fails with a misleading
# "unknown location" ImportError if google.oauth2 is already cached as one
# of those stubs. This module needs the REAL, installed google-auth /
# google-auth-oauthlib libraries (it mocks only their specific
# network-touching methods, not the modules themselves — see the module
# docstring), so force a clean re-import here.
for _mod in list(sys.modules):
    if _mod == "google" or _mod.startswith("google.") or _mod.startswith("google_auth_oauthlib"):
        del sys.modules[_mod]
# Only classpilot.google_oauth needs popping here — a ONE-TIME fresh
# import at collection time is safe (it establishes the single canonical
# instance this whole file uses; no repeated create/destroy/"restore"
# cycle occurs at collection time, so there's no identity-divergence risk
# the way there is for the per-test-method dance in
# TestGrantedScopeSupersetHandling below). classpilot.config,
# classpilot.db, and classpilot.user_store must NEVER be popped by test
# code at all — see that class's docstring for the real, reproduced bug
# this caused when it was tried.
sys.modules.pop("classpilot.google_oauth", None)

from classpilot.google_oauth import (
    ExchangedIdentity,
    GoogleOAuthError,
    build_authorization_url,
    complete_authorization,
    exchange_code_for_user,
    get_authorized_credentials,
    get_web_flow_scopes,
    clear_credentials_cache,
    _persist_credentials,
    _verify_required_scopes_present,
)


# ---------- A minimal fake UserStore (see module docstring for why) ----------

@dataclass
class _FakeUserRecord:
    id: str
    google_sub: str
    email: Optional[str] = None
    display_name: Optional[str] = None


@dataclass
class _FakeCredentialRecord:
    user_id: str
    refresh_token: str
    scopes: list = field(default_factory=list)
    token_expiry: Optional[datetime] = None


class FakeUserStore:
    """Records calls the same way the real UserStore's public methods
    behave, entirely in-memory — no DB, no encryption (irrelevant here;
    that's tested directly in test_crypto.py and test_user_store.py)."""

    def __init__(self):
        self._users: dict[str, _FakeUserRecord] = {}   # by sub
        self._creds: dict[str, _FakeCredentialRecord] = {}  # by user_id
        self.save_calls: list[tuple] = []

    def get_or_create_user(self, google_sub, email=None, display_name=None):
        if google_sub in self._users:
            existing = self._users[google_sub]
            if email:
                existing.email = email
            if display_name:
                existing.display_name = display_name
            return existing
        record = _FakeUserRecord(id=f"uid-{len(self._users)+1}", google_sub=google_sub,
                                  email=email, display_name=display_name)
        self._users[google_sub] = record
        return record

    def save_google_credentials(self, user_id, refresh_token, scopes, token_expiry=None):
        if not refresh_token:
            raise ValueError("refresh_token is required and cannot be empty.")
        self.save_calls.append((user_id, refresh_token, list(scopes), token_expiry))
        self._creds[user_id] = _FakeCredentialRecord(
            user_id=user_id, refresh_token=refresh_token, scopes=list(scopes), token_expiry=token_expiry,
        )

    def get_google_credentials(self, user_id):
        return self._creds.get(user_id)

    def delete_google_credentials(self, user_id):
        self._creds.pop(user_id, None)


def _fake_web_client_config():
    return {"client_id": "fake-client-id.apps.googleusercontent.com", "client_secret": "fake-secret"}


class GoogleOAuthTestCase(unittest.TestCase):
    def setUp(self):
        clear_credentials_cache()
        self.patcher = patch(
            "classpilot.google_oauth._load_web_client_config",
            return_value=_fake_web_client_config(),
        )
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        clear_credentials_cache()


# ---------- Scopes ----------

class TestGetWebFlowScopes(GoogleOAuthTestCase):
    def setUp(self):
        super().setUp()
        # get_web_flow_scopes() reads classpilot.classroom_client's already-
        # imported, already-scope-patched module — and other test files in
        # this suite (test_study_tools.py, test_scope_audit.py,
        # test_http_server.py) each re-patch classroom_suite_mcp.auth.SCOPES
        # from a DIFFERENT starting point (some from a stub SCOPES=[], some
        # from the real vendor defaults) during THEIR OWN module-level
        # collection code, which runs for every test file in the suite
        # before any test function executes — including ones collected
        # after this file. So by the time this test's body actually runs,
        # whichever file was collected last has already overwritten the
        # cached scope-patch result. Force a clean re-import against the
        # REAL vendor defaults right here, at execution time, so this
        # test's outcome doesn't depend on collection order across the
        # suite (same rationale as tests/test_scope_audit.py).
        for _mod in ("classpilot.classroom_client", "classroom_suite_mcp", "classroom_suite_mcp.auth"):
            sys.modules.pop(_mod, None)

    def test_includes_oidc_identity_scopes(self):
        scopes = get_web_flow_scopes()
        self.assertIn("openid", scopes)
        self.assertIn("https://www.googleapis.com/auth/userinfo.email", scopes)
        self.assertIn("https://www.googleapis.com/auth/userinfo.profile", scopes)

    def test_includes_phase1_classroom_scopes(self):
        scopes = get_web_flow_scopes()
        self.assertIn("https://www.googleapis.com/auth/classroom.courses.readonly", scopes)
        self.assertIn("https://www.googleapis.com/auth/drive.readonly", scopes)

    def test_does_not_include_dropped_phase1_scopes(self):
        """Reuses the SAME curated list Phase 1 audited — not a duplicated
        copy that could drift and silently regrant a dropped scope."""
        scopes = get_web_flow_scopes()
        self.assertFalse(any("coursework.students" in s for s in scopes))
        self.assertFalse(any("documents" in s for s in scopes))
        self.assertFalse(any("rosters" in s for s in scopes))
        self.assertNotIn("https://www.googleapis.com/auth/drive", scopes)

    def test_no_duplicate_scopes(self):
        scopes = get_web_flow_scopes()
        self.assertEqual(len(scopes), len(set(scopes)))


# ---------- Authorization URL ----------

class TestBuildAuthorizationUrl(GoogleOAuthTestCase):
    def test_requests_offline_access_and_consent_prompt(self):
        mock_flow = MagicMock()
        mock_flow.code_verifier = "generated-verifier-xyz"
        mock_flow.authorization_url.return_value = ("https://accounts.google.com/o/oauth2/auth?x=1", "state123")
        with patch("classpilot.google_oauth.Flow.from_client_secrets_file", return_value=mock_flow):
            url, code_verifier = build_authorization_url("state123")

        self.assertEqual(url, "https://accounts.google.com/o/oauth2/auth?x=1")
        self.assertEqual(code_verifier, "generated-verifier-xyz")
        _, kwargs = mock_flow.authorization_url.call_args
        self.assertEqual(kwargs["access_type"], "offline")
        self.assertEqual(kwargs["prompt"], "consent")

    def test_state_passed_through_to_flow_construction(self):
        mock_flow = MagicMock()
        mock_flow.code_verifier = "v"
        mock_flow.authorization_url.return_value = ("https://x", "s")
        with patch("classpilot.google_oauth.Flow.from_client_secrets_file", return_value=mock_flow) as mock_ctor:
            build_authorization_url("my-csrf-state")
        _, kwargs = mock_ctor.call_args
        self.assertEqual(kwargs.get("state"), "my-csrf-state")

    def test_redirect_uri_set_from_config(self):
        mock_flow = MagicMock()
        mock_flow.code_verifier = "v"
        mock_flow.authorization_url.return_value = ("https://x", "s")
        with patch("classpilot.google_oauth.Flow.from_client_secrets_file", return_value=mock_flow):
            build_authorization_url("state123")
        from classpilot.config import get_config
        self.assertEqual(mock_flow.redirect_uri, get_config().google_oauth_redirect_uri)

    def test_returns_the_flows_own_code_verifier_not_a_freshly_generated_one(self):
        """This is the crux of the PKCE fix: the caller (oauth_web.py) must
        receive the EXACT verifier this Flow generated so it can be
        persisted and later handed back to a *different* Flow instance
        during the token exchange."""
        mock_flow = MagicMock()
        mock_flow.authorization_url.side_effect = lambda **k: (
            setattr(mock_flow, "code_verifier", "the-real-generated-verifier") or ("https://x", "s")
        )
        mock_flow.code_verifier = None  # not yet generated before authorization_url() runs
        with patch("classpilot.google_oauth.Flow.from_client_secrets_file", return_value=mock_flow):
            _, code_verifier = build_authorization_url("state123")
        self.assertEqual(code_verifier, "the-real-generated-verifier")


class TestRequestedScopesMatchCuratedSet(GoogleOAuthTestCase):
    """
    Regression coverage for the reported "old/broader scopes" observation.
    Proves the *outgoing request Phase 2 itself builds* asks for exactly
    the curated scope set — not classroom.coursework.students,
    classroom.rosters.readonly, full drive, or drive.file. (Google may
    still ECHO BACK broader scopes at the callback via
    include_granted_scopes=true if this Google Cloud project has older,
    pre-existing account-level consent grants from before the scope audit —
    that's Google's own incremental-authorization behavior reflecting
    authorization history, not something this code requests, and
    per-instructions this fix does not touch include_granted_scopes or try
    to revoke old grants.)
    """

    def test_flow_is_constructed_with_exactly_the_curated_scopes(self):
        mock_flow = MagicMock()
        mock_flow.code_verifier = "v"
        mock_flow.authorization_url.return_value = ("https://x", "s")
        with patch("classpilot.google_oauth.Flow.from_client_secrets_file", return_value=mock_flow) as mock_ctor:
            build_authorization_url("state123")
        _, kwargs = mock_ctor.call_args
        requested_scopes = set(kwargs["scopes"])
        self.assertEqual(requested_scopes, set(get_web_flow_scopes()))

    def test_requested_scopes_never_include_dropped_phase1_scopes(self):
        mock_flow = MagicMock()
        mock_flow.code_verifier = "v"
        mock_flow.authorization_url.return_value = ("https://x", "s")
        with patch("classpilot.google_oauth.Flow.from_client_secrets_file", return_value=mock_flow) as mock_ctor:
            build_authorization_url("state123")
        requested_scopes = mock_ctor.call_args[1]["scopes"]
        self.assertFalse(any("coursework.students" in s for s in requested_scopes))
        self.assertFalse(any("rosters" in s for s in requested_scopes))
        self.assertNotIn("https://www.googleapis.com/auth/drive", requested_scopes)
        self.assertFalse(any("drive.file" in s for s in requested_scopes))


# ---------- Code exchange ----------

def _mock_flow_with_credentials(id_token="fake.id.token", refresh_token="1//fake-refresh",
                                 scopes=None, granted_scopes=None, expiry=None):
    """
    Build a mock Flow whose .credentials mimics a real
    google.oauth2.credentials.Credentials object closely enough for these
    tests — critically, distinguishing `.scopes` (what THIS request asked
    for) from `.granted_scopes` (what Google's token response actually
    returned), exactly as the real library does (see
    google_auth_oauthlib.helpers.credentials_from_session, which sets
    `scopes=session.scope` but `granted_scopes=session.token.get("scope")`
    — two different things). Production code reads `.granted_scopes` for
    scope verification; tests that don't care about scope verification
    specifically get a granted_scopes default equal to the full curated
    set, so they pass that check transparently.
    """
    flow = MagicMock()
    creds = MagicMock()
    creds.id_token = id_token
    creds.refresh_token = refresh_token
    creds.scopes = scopes or ["scope-a", "scope-b"]
    creds.granted_scopes = granted_scopes if granted_scopes is not None else get_web_flow_scopes()
    creds.expiry = expiry
    flow.credentials = creds
    return flow


class TestExchangeCodeForUser(GoogleOAuthTestCase):
    def test_successful_exchange_returns_identity(self):
        flow = _mock_flow_with_credentials()
        claims = {"sub": "google-sub-42", "email": "alice@school.edu", "name": "Alice"}
        with patch("classpilot.google_oauth.Flow.from_client_secrets_file", return_value=flow), \
             patch("classpilot.google_oauth.google_id_token.verify_oauth2_token", return_value=claims):
            identity = exchange_code_for_user("auth-code-123", "some-verifier")

        self.assertEqual(identity.sub, "google-sub-42")
        self.assertEqual(identity.email, "alice@school.edu")
        self.assertEqual(identity.name, "Alice")
        self.assertEqual(identity.refresh_token, "1//fake-refresh")
        flow.fetch_token.assert_called_once_with(code="auth-code-123")

    def test_fetch_token_failure_wrapped_in_google_oauth_error(self):
        flow = MagicMock()
        flow.fetch_token.side_effect = Exception("invalid_grant")
        with patch("classpilot.google_oauth.Flow.from_client_secrets_file", return_value=flow):
            with self.assertRaises(GoogleOAuthError):
                exchange_code_for_user("bad-code", "verifier")

    def test_missing_id_token_raises(self):
        flow = _mock_flow_with_credentials(id_token=None)
        with patch("classpilot.google_oauth.Flow.from_client_secrets_file", return_value=flow):
            with self.assertRaises(GoogleOAuthError):
                exchange_code_for_user("code", "verifier")

    def test_id_token_verification_failure_wrapped(self):
        flow = _mock_flow_with_credentials()
        with patch("classpilot.google_oauth.Flow.from_client_secrets_file", return_value=flow), \
             patch("classpilot.google_oauth.google_id_token.verify_oauth2_token",
                   side_effect=ValueError("bad signature")):
            with self.assertRaises(GoogleOAuthError):
                exchange_code_for_user("code", "verifier")

    def test_claims_missing_sub_raises(self):
        flow = _mock_flow_with_credentials()
        with patch("classpilot.google_oauth.Flow.from_client_secrets_file", return_value=flow), \
             patch("classpilot.google_oauth.google_id_token.verify_oauth2_token",
                   return_value={"email": "no-sub@x.com"}):
            with self.assertRaises(GoogleOAuthError):
                exchange_code_for_user("code", "verifier")

    def test_refresh_token_none_is_preserved_as_none_on_the_identity(self):
        """exchange_code_for_user itself doesn't decide what to do about a
        missing refresh_token — that's _persist_credentials's job (see
        below). This just confirms the identity object honestly reflects
        what Google actually returned."""
        flow = _mock_flow_with_credentials(refresh_token=None)
        claims = {"sub": "sub-1", "email": "a@x.com"}
        with patch("classpilot.google_oauth.Flow.from_client_secrets_file", return_value=flow), \
             patch("classpilot.google_oauth.google_id_token.verify_oauth2_token", return_value=claims):
            identity = exchange_code_for_user("code", "verifier")
        self.assertIsNone(identity.refresh_token)


# ---------- Regression: the actual "Missing code verifier" bug ----------

class TestPkceCodeVerifierLifecycle(GoogleOAuthTestCase):
    """
    Direct regression coverage for the reported failure:
        Failed to exchange authorization code: (invalid_grant) Missing code verifier.

    Root cause (see google_oauth.py's module docstring): google_auth_oauthlib's
    Flow generates a fresh, random code_verifier per *instance*.
    build_authorization_url() and exchange_code_for_user() necessarily use
    two DIFFERENT Flow instances (two separate HTTP requests) — so unless
    the exact verifier from the first is explicitly threaded into the
    second, the second either has none or has a mismatched auto-generated
    one, and Google rejects the exchange.
    """

    def test_exchange_sets_the_supplied_verifier_on_the_new_flow_before_fetching(self):
        """The core fix: exchange_code_for_user must set flow.code_verifier
        to the EXACT value passed in — not leave it at None (which is what
        caused the real bug) and not silently auto-generate a new,
        non-matching one."""
        flow = _mock_flow_with_credentials()
        claims = {"sub": "sub-1", "email": "a@x.com"}
        captured_verifier_at_fetch_time = {}

        def _capture_fetch_token(**kwargs):
            captured_verifier_at_fetch_time["value"] = flow.code_verifier
            return {}

        flow.fetch_token.side_effect = _capture_fetch_token

        with patch("classpilot.google_oauth.Flow.from_client_secrets_file", return_value=flow), \
             patch("classpilot.google_oauth.google_id_token.verify_oauth2_token", return_value=claims):
            exchange_code_for_user("code", "the-verifier-from-login")

        self.assertEqual(captured_verifier_at_fetch_time["value"], "the-verifier-from-login")

    def test_exchange_disables_autogenerate_so_it_never_silently_replaces_the_verifier(self):
        flow = _mock_flow_with_credentials()
        claims = {"sub": "sub-1", "email": "a@x.com"}
        with patch("classpilot.google_oauth.Flow.from_client_secrets_file", return_value=flow), \
             patch("classpilot.google_oauth.google_id_token.verify_oauth2_token", return_value=claims):
            exchange_code_for_user("code", "verifier-abc")
        self.assertFalse(flow.autogenerate_code_verifier)

    def test_missing_code_verifier_raises_before_attempting_exchange(self):
        """Fails fast and clearly rather than reproducing Google's cryptic
        'Missing code verifier' error — no fetch_token call should even
        be attempted."""
        flow = MagicMock()
        with patch("classpilot.google_oauth.Flow.from_client_secrets_file", return_value=flow):
            with self.assertRaises(GoogleOAuthError) as ctx:
                exchange_code_for_user("code", "")
        self.assertIn("code_verifier", str(ctx.exception))
        flow.fetch_token.assert_not_called()

    def test_none_code_verifier_raises_before_attempting_exchange(self):
        flow = MagicMock()
        with patch("classpilot.google_oauth.Flow.from_client_secrets_file", return_value=flow):
            with self.assertRaises(GoogleOAuthError):
                exchange_code_for_user("code", None)
        flow.fetch_token.assert_not_called()

    def test_full_login_to_callback_round_trip_uses_matching_verifier(self):
        """End-to-end simulation of the real flow: build_authorization_url's
        verifier is exactly what exchange_code_for_user ends up using,
        going through the same (state -> verifier) hand-off oauth_web.py's
        actual route handlers perform (register_state / consume_state),
        proving the full lifecycle — not just the exchange function in
        isolation."""
        import classpilot.oauth_state as oauth_state_mod

        login_flow = MagicMock()
        login_flow.code_verifier = None
        def _generate_verifier_like_the_real_library(**kwargs):
            login_flow.code_verifier = "auto-generated-during-authorization-url"
            return ("https://accounts.google.com/auth?...", "state-echo")
        login_flow.authorization_url.side_effect = _generate_verifier_like_the_real_library

        exchange_flow = _mock_flow_with_credentials()
        claims = {"sub": "sub-1", "email": "a@x.com"}

        flows_in_order = [login_flow, exchange_flow]
        with patch("classpilot.google_oauth.Flow.from_client_secrets_file",
                   side_effect=lambda *a, **k: flows_in_order.pop(0)):
            # /login
            auth_url, code_verifier = build_authorization_url("csrf-state")
            self.assertEqual(code_verifier, "auto-generated-during-authorization-url")

            # In between: this is what oauth_web.py's google_login does —
            # persist (state, code_verifier) — and what a real Postgres
            # round-trip via oauth_state.py already proves survives a
            # process boundary (see test_oauth_state.py). Here we only
            # need the value itself to flow through correctly.
            stored_verifier = code_verifier

            # /callback — a NEW Flow instance, as a real second HTTP
            # request would use
            with patch("classpilot.google_oauth.google_id_token.verify_oauth2_token", return_value=claims):
                exchange_code_for_user("code-from-google", stored_verifier)

        self.assertEqual(exchange_flow.code_verifier, "auto-generated-during-authorization-url")


# ---------- Regression: "Scope has changed" / historical-scope superset ----------

def _fake_token_response(scope_list, include_id_token=True):
    """
    Build a real requests.Response mimicking Google's token endpoint,
    good enough to drive the REAL oauthlib/requests_oauthlib parsing and
    scope-validation code paths (not a mock of our own logic) — this is
    what makes these regression tests trustworthy: they exercise the
    actual third-party code that raised the reported
    '(invalid_grant) Missing code verifier'-adjacent
    'Scope has changed from ... to ...' error, not a hand-rolled stand-in
    for it.
    """
    import json as _json
    resp = requests.Response()
    resp.status_code = 200
    resp.headers["Content-Type"] = "application/json"
    body = {
        "access_token": "fake-access-token",
        "refresh_token": "fake-refresh-token",
        "expires_in": 3600,
        "token_type": "Bearer",
        "scope": " ".join(scope_list),
    }
    if include_id_token:
        body["id_token"] = "fake.id.token"
    resp._content = _json.dumps(body).encode()
    resp.request = MagicMock(url="https://oauth2.googleapis.com/token")
    return resp


_HISTORICAL_EXTRA_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/classroom.rosters.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.students",
]


class TestGrantedScopeSupersetHandling(GoogleOAuthTestCase):
    """
    Direct regression coverage for the reported failure:
        Scope has changed from "<curated scopes>" to "<curated + historical>".

    Root cause (see google_oauth.py's module docstring, "Granted-scope
    superset handling"): oauthlib.oauth2.rfc6749.parameters.
    validate_token_parameters() raises a bare `Warning` exception whenever
    the token response's scope differs AT ALL from what was requested —
    including a pure superset, which is exactly what happens when
    include_granted_scopes=true (required, not removed by this fix)
    surfaces broader scopes this Google Cloud project's OTHER, older OAuth
    clients previously had granted.

    These tests drive the REAL requests_oauthlib/oauthlib code by mocking
    only the HTTP layer (requests.Session.request) — not
    exchange_code_for_user's own logic — so they genuinely prove the fix
    resolves the actual third-party library behavior, not just an
    assumption about it. That requires a real (fake-content) client
    secrets file on disk, since _build_flow() reads one for real here.

    Module-isolation approach (IMPORTANT — this class's setUp went through
    three iterations before landing on a genuinely correct one; each
    earlier attempt caused a different, real, reproduced bug, documented
    here so the same mistakes aren't repeated):

    Attempt 1 — pop classpilot.config/user_store/classroom_client/
    google_oauth from sys.modules, run the test against fresh copies, then
    "restore" the originals afterward via `sys.modules[key] = orig_obj`.
    BROKE: a raw sys.modules[key] = obj reassignment only fixes the
    sys.modules DICT entry — it does not update the classpilot PACKAGE's
    own `.config` ATTRIBUTE, which `import classpilot.config as x`
    actually resolves through (a getattr on the parent module, per
    CPython's IMPORT_FROM opcode semantics for dotted-with-alias imports —
    not a fresh sys.modules dict lookup). That left the two permanently
    diverged after this class ran, so a LATER test's `config_mod._config
    = None; ...; get_config().database_url` was resetting a DIFFERENT
    config instance than the one classpilot.db's `get_config` was
    permanently bound to (captured once, at db.py's own first-ever
    import, never reloaded) — so a DATABASE_URL reset never reached pool
    construction, silently falling back to config.py's hardcoded default
    DSN and failing outright in any environment without a working
    'classpilot' role. (See tests/test_cross_file_isolation.py.)

    Attempt 2 — don't touch classpilot.config/db/user_store/
    classroom_client at all (correct — see below), and don't touch
    classpilot.google_oauth either, reasoning that its Flow/Credentials/
    etc. names, bound once at this FILE's own collection time (after this
    file's own google.* purge), would stay correct regardless of what
    OTHER files later did to sys.modules. BROKE: other test files (e.g.
    test_study_tools.py) UNCONDITIONALLY mutate
    `sys.modules["google.oauth2.credentials"].Credentials = object` at
    THEIR OWN collection time — an in-place ATTRIBUTE mutation on the
    module object, not a sys.modules replacement. google_auth_oauthlib's
    OWN internal code (flow.py, helpers.py) resolves
    `google.oauth2.credentials.Credentials` via a live attribute-chain
    walk at CALL time, off a `google` name each of THEM bound once at
    THEIR OWN historical import — so that mutation corrupts real,
    already-imported Flow/Credentials functionality no matter when
    classpilot.google_oauth itself was imported, and no per-file ordering
    trick avoids it.

    Attempt 3 (this one) — pop and freshly re-import ONLY the third-party
    google.*/google_auth_oauthlib modules (safe: nothing in this codebase
    needs THEM to keep a stable identity), and refresh
    classpilot.google_oauth via importlib.reload() so its Flow/Credentials
    bindings pick up the freshly-reimported, un-mutated real libraries.
    Done ONCE in setUpClass (not per test method — reload() is not free
    and isn't needed more than once), and the refreshed
    names/classes/functions are propagated into THIS TEST FILE's own
    module globals (globals().update(...)) so every OTHER test class
    below this one in the file — which use bare names like
    `GoogleOAuthError`/`exchange_code_for_user` imported once at file
    collection time — see the SAME, consistent, post-reload set of
    objects rather than a stale pre-reload copy (reload() creates a brand
    new `class GoogleOAuthError(Exception)` object, and a bare name bound
    via `from classpilot.google_oauth import GoogleOAuthError` at
    collection time does NOT automatically follow a later reload() of
    that module — it has to be explicitly re-bound). Test classes ABOVE
    this one in the file already finished running before this reload
    happens (pytest runs unittest.TestCase classes in file-definition
    order), so they're unaffected.
    """

    @classmethod
    def setUpClass(cls):
        for _mod in list(sys.modules):
            if _mod == "google" or _mod.startswith("google.") or _mod.startswith("google_auth_oauthlib"):
                del sys.modules[_mod]
        import classpilot.google_oauth as _module_ref
        importlib.reload(_module_ref)
        # Propagate the refreshed objects into this test file's own
        # globals so every test class below this one (which reference
        # these as bare, collection-time-bound names) sees a consistent,
        # post-reload world instead of a stale pre-reload copy.
        globals().update({
            name: getattr(_module_ref, name)
            for name in (
                "ExchangedIdentity", "GoogleOAuthError", "build_authorization_url",
                "complete_authorization", "exchange_code_for_user",
                "get_authorized_credentials", "get_web_flow_scopes",
                "clear_credentials_cache", "_persist_credentials",
                "_verify_required_scopes_present",
            )
        })

    def setUp(self):
        super().setUp()
        import json, tempfile
        fake_client = {
            "web": {
                "client_id": "fake-client-id.apps.googleusercontent.com",
                "client_secret": "fake-secret",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost:8090/auth/google/callback"],
            }
        }
        self._creds_path = tempfile.mktemp(suffix=".json")
        with open(self._creds_path, "w") as f:
            json.dump(fake_client, f)
        self._config_patcher = patch(
            "classpilot.google_oauth.get_config",
            return_value=type("Cfg", (), {
                "google_web_credentials_path": self._creds_path,
                "google_oauth_redirect_uri": "http://localhost:8090/auth/google/callback",
            })(),
        )
        self._config_patcher.start()

    def tearDown(self):
        self._config_patcher.stop()
        import os as _os
        _os.remove(self._creds_path)
        super().tearDown()

    def _run_exchange(self, granted_scope_list, code_verifier="test-verifier"):
        with patch("requests.Session.request", return_value=_fake_token_response(granted_scope_list)), \
             patch("classpilot.google_oauth.google_id_token.verify_oauth2_token",
                   return_value={"sub": "sub-1", "email": "a@x.com"}):
            return exchange_code_for_user("real-auth-code", code_verifier)

    def test_exact_scope_match_succeeds(self):
        """Baseline: no superset involved at all — must keep working
        exactly as before this fix (this would already pass without any
        of the new relaxation/verification code)."""
        curated = get_web_flow_scopes()
        identity = self._run_exchange(curated)
        self.assertEqual(identity.sub, "sub-1")
        self.assertEqual(set(identity.scopes), set(curated))

    def test_required_scopes_plus_historical_extras_succeeds(self):
        """The exact reported bug scenario: Google returns every curated
        scope PLUS old broad scopes from the Desktop clients. Must not
        raise, and the extra scopes should be visible on the identity
        (they were genuinely granted) without being treated as required."""
        curated = get_web_flow_scopes()
        identity = self._run_exchange(curated + _HISTORICAL_EXTRA_SCOPES)
        self.assertEqual(identity.sub, "sub-1")
        self.assertTrue(set(curated).issubset(set(identity.scopes)))
        self.assertTrue(set(_HISTORICAL_EXTRA_SCOPES).issubset(set(identity.scopes)))

    def test_google_omits_a_required_scope_fails(self):
        """The security-critical counterpart: relaxing oauthlib's
        (overly strict, both-directions) check must NOT mean a response
        missing something ClassPilot actually needs silently succeeds."""
        curated = get_web_flow_scopes()
        missing_drive = [s for s in curated if "drive" not in s]
        with self.assertRaises(GoogleOAuthError) as ctx:
            self._run_exchange(missing_drive)
        self.assertIn("drive.readonly", str(ctx.exception))

    def test_google_omits_a_required_scope_even_with_historical_extras_fails(self):
        """Missing-required-scope detection must still work even when the
        response ALSO contains a superset of historical extras — i.e. a
        large granted set doesn't hide a missing required one."""
        curated = get_web_flow_scopes()
        missing_one = [s for s in curated if s != "openid"]
        with self.assertRaises(GoogleOAuthError) as ctx:
            self._run_exchange(missing_one + _HISTORICAL_EXTRA_SCOPES)
        self.assertIn("openid", str(ctx.exception))

    def test_requested_scope_configuration_remains_the_curated_set(self):
        """Extra granted scopes must never leak into what ClassPilot
        considers its OWN requested configuration — get_web_flow_scopes()
        must be identical before and after an exchange that received a
        historical-extras superset."""
        curated_before = set(get_web_flow_scopes())
        self._run_exchange(get_web_flow_scopes() + _HISTORICAL_EXTRA_SCOPES)
        curated_after = set(get_web_flow_scopes())
        self.assertEqual(curated_before, curated_after)
        self.assertFalse(set(_HISTORICAL_EXTRA_SCOPES) & curated_after)

    def test_without_the_fix_the_real_library_would_raise_on_superset(self):
        """Sanity check that this test suite would actually catch a
        regression: with oauthlib's relaxation NOT applied, the exact
        superset scenario from test_required_scopes_plus_historical_extras_succeeds
        genuinely raises via the real library — proving the fix (not the
        test's mocking) is what makes that scenario pass."""
        curated = get_web_flow_scopes()
        with patch("requests.Session.request",
                   return_value=_fake_token_response(curated + _HISTORICAL_EXTRA_SCOPES)):
            with patch("classpilot.google_oauth.google_id_token.verify_oauth2_token",
                       return_value={"sub": "sub-1", "email": "a@x.com"}):
                # Force the relaxation OFF for this one call to prove the
                # underlying library really does raise without it.
                with patch("classpilot.google_oauth._relaxed_token_scope_validation",
                           side_effect=lambda: _no_op_context()):
                    with self.assertRaises(GoogleOAuthError) as ctx:
                        exchange_code_for_user("real-auth-code", "test-verifier")
        self.assertIn("Scope has changed", str(ctx.exception))


@contextlib.contextmanager
def _no_op_context():
    yield


# ---------- Direct unit tests for the two new helpers ----------

class TestVerifyRequiredScopesPresent(GoogleOAuthTestCase):
    def test_exact_match_passes(self):
        curated = get_web_flow_scopes()
        _verify_required_scopes_present(curated)  # must not raise

    def test_superset_passes(self):
        curated = get_web_flow_scopes()
        _verify_required_scopes_present(curated + _HISTORICAL_EXTRA_SCOPES)  # must not raise

    def test_missing_one_required_scope_raises(self):
        curated = get_web_flow_scopes()
        incomplete = curated[:-1]
        with self.assertRaises(GoogleOAuthError):
            _verify_required_scopes_present(incomplete)

    def test_empty_granted_scopes_raises(self):
        with self.assertRaises(GoogleOAuthError):
            _verify_required_scopes_present([])

    def test_error_lists_every_missing_scope(self):
        with self.assertRaises(GoogleOAuthError) as ctx:
            _verify_required_scopes_present([])
        for scope in get_web_flow_scopes():
            self.assertIn(scope, str(ctx.exception))


# ---------- Requirement 2: never overwrite a valid refresh token with None ----------

class TestPersistCredentialsPreservesExistingRefreshToken(GoogleOAuthTestCase):
    def test_new_refresh_token_is_stored_normally(self):
        store = FakeUserStore()
        user = store.get_or_create_user("sub-1")
        _persist_credentials(store, user.id, "new-refresh-token", ["scope-a"], None)
        creds = store.get_google_credentials(user.id)
        self.assertEqual(creds.refresh_token, "new-refresh-token")

    def test_none_refresh_token_with_existing_stored_token_preserves_it(self):
        """The core requirement: a re-authorization that comes back with
        refresh_token=None must NOT destroy the previously working token."""
        store = FakeUserStore()
        user = store.get_or_create_user("sub-1")
        store.save_google_credentials(user.id, refresh_token="original-token", scopes=["old-scope"])

        _persist_credentials(store, user.id, None, ["new-scope-a", "new-scope-b"], None)

        creds = store.get_google_credentials(user.id)
        self.assertEqual(creds.refresh_token, "original-token")  # unchanged
        self.assertEqual(set(creds.scopes), {"new-scope-a", "new-scope-b"})  # still updates scopes

    def test_save_is_never_called_with_a_falsy_token(self):
        """Defense in depth: even if _persist_credentials had a bug, the
        underlying store call itself must never be reachable with a
        falsy refresh_token — assert on the actual call arguments."""
        store = FakeUserStore()
        user = store.get_or_create_user("sub-1")
        store.save_google_credentials(user.id, refresh_token="original-token", scopes=["a"])

        _persist_credentials(store, user.id, None, ["b"], None)

        for call in store.save_calls:
            self.assertTrue(call[1])  # refresh_token position — must be truthy every time

    def test_none_refresh_token_with_no_existing_credentials_raises(self):
        store = FakeUserStore()
        user = store.get_or_create_user("sub-1")
        with self.assertRaises(GoogleOAuthError):
            _persist_credentials(store, user.id, None, ["a"], None)
        self.assertIsNone(store.get_google_credentials(user.id))  # nothing written

    def test_complete_authorization_end_to_end_preserves_refresh_token_on_reauth(self):
        """Full flow: first authorization stores a token; a second
        're-authorization' for the same Google account that doesn't
        return a new refresh_token must keep the first one intact."""
        store = FakeUserStore()

        first_flow = _mock_flow_with_credentials(refresh_token="first-real-token")
        with patch("classpilot.google_oauth.Flow.from_client_secrets_file", return_value=first_flow), \
             patch("classpilot.google_oauth.google_id_token.verify_oauth2_token",
                   return_value={"sub": "stable-sub", "email": "a@x.com"}):
            user1 = complete_authorization("code-1", "verifier-1", store)

        second_flow = _mock_flow_with_credentials(refresh_token=None)  # Google omits it this time
        with patch("classpilot.google_oauth.Flow.from_client_secrets_file", return_value=second_flow), \
             patch("classpilot.google_oauth.google_id_token.verify_oauth2_token",
                   return_value={"sub": "stable-sub", "email": "a@x.com"}):
            user2 = complete_authorization("code-2", "verifier-2", store)

        self.assertEqual(user1.id, user2.id)  # same stable Google identity -> same ClassPilot user
        creds = store.get_google_credentials(user1.id)
        self.assertEqual(creds.refresh_token, "first-real-token")  # preserved, not wiped


# ---------- Requirement 1: refresh only when needed, not on every call ----------

class TestGetAuthorizedCredentialsRefreshCaching(GoogleOAuthTestCase):
    def _store_with_credentials(self, user_id="uid-1", refresh_token="1//fake-refresh"):
        store = FakeUserStore()
        store._creds[user_id] = _FakeCredentialRecord(
            user_id=user_id, refresh_token=refresh_token, scopes=["scope-a"],
        )
        return store

    @staticmethod
    def _make_refreshing_mock():
        """A side effect for Credentials.refresh() that behaves like a real
        refresh: sets a live token + a future expiry, so .valid becomes True."""
        def _refresh(self, request):
            self.token = "fresh-access-token"
            self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
        return _refresh

    def test_first_call_refreshes_and_caches(self):
        store = self._store_with_credentials()
        with patch("classpilot.google_oauth.Credentials.refresh", autospec=True,
                   side_effect=self._make_refreshing_mock()) as mock_refresh:
            creds = get_authorized_credentials("uid-1", store)
        self.assertEqual(mock_refresh.call_count, 1)
        self.assertEqual(creds.token, "fresh-access-token")

    def test_second_call_within_validity_does_not_refresh_again(self):
        """The core requirement: a second call while the cached token is
        still valid must NOT call .refresh() again."""
        store = self._store_with_credentials()
        with patch("classpilot.google_oauth.Credentials.refresh", autospec=True,
                   side_effect=self._make_refreshing_mock()) as mock_refresh:
            first = get_authorized_credentials("uid-1", store)
            second = get_authorized_credentials("uid-1", store)

        self.assertEqual(mock_refresh.call_count, 1)  # NOT 2
        self.assertIs(first, second)  # same cached object returned

    def test_second_call_does_not_hit_the_store_again(self):
        """Proves the cache is actually short-circuiting, not just
        happening to avoid a redundant refresh — no DB round-trip either."""
        store = self._store_with_credentials()
        store.get_google_credentials = MagicMock(wraps=store.get_google_credentials)
        with patch("classpilot.google_oauth.Credentials.refresh", autospec=True,
                   side_effect=self._make_refreshing_mock()):
            get_authorized_credentials("uid-1", store)
            get_authorized_credentials("uid-1", store)
        self.assertEqual(store.get_google_credentials.call_count, 1)

    def test_refresh_called_again_after_cached_token_expires(self):
        store = self._store_with_credentials()
        with patch("classpilot.google_oauth.Credentials.refresh", autospec=True,
                   side_effect=self._make_refreshing_mock()) as mock_refresh:
            creds = get_authorized_credentials("uid-1", store)
            # Simulate real time passing / the access token expiring
            creds.expiry = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
            get_authorized_credentials("uid-1", store)
        self.assertEqual(mock_refresh.call_count, 2)

    def test_force_refresh_bypasses_a_still_valid_cache(self):
        store = self._store_with_credentials()
        with patch("classpilot.google_oauth.Credentials.refresh", autospec=True,
                   side_effect=self._make_refreshing_mock()) as mock_refresh:
            get_authorized_credentials("uid-1", store)
            get_authorized_credentials("uid-1", store, force_refresh=True)
        self.assertEqual(mock_refresh.call_count, 2)

    def test_different_users_cached_independently(self):
        store = self._store_with_credentials(user_id="uid-1", refresh_token="token-1")
        store._creds["uid-2"] = _FakeCredentialRecord(user_id="uid-2", refresh_token="token-2", scopes=["a"])
        with patch("classpilot.google_oauth.Credentials.refresh", autospec=True,
                   side_effect=self._make_refreshing_mock()) as mock_refresh:
            get_authorized_credentials("uid-1", store)
            get_authorized_credentials("uid-2", store)
            get_authorized_credentials("uid-1", store)  # cached, no new refresh
            get_authorized_credentials("uid-2", store)  # cached, no new refresh
        self.assertEqual(mock_refresh.call_count, 2)  # one per user, not four

    def test_no_stored_credentials_raises(self):
        store = FakeUserStore()
        with self.assertRaises(GoogleOAuthError):
            get_authorized_credentials("nonexistent-user", store)

    def test_refresh_failure_wrapped_in_google_oauth_error(self):
        store = self._store_with_credentials()
        with patch("classpilot.google_oauth.Credentials.refresh", autospec=True,
                   side_effect=Exception("invalid_grant: token revoked")):
            with self.assertRaises(GoogleOAuthError):
                get_authorized_credentials("uid-1", store)

    def test_clear_credentials_cache_for_one_user(self):
        store = self._store_with_credentials()
        with patch("classpilot.google_oauth.Credentials.refresh", autospec=True,
                   side_effect=self._make_refreshing_mock()) as mock_refresh:
            get_authorized_credentials("uid-1", store)
            clear_credentials_cache("uid-1")
            get_authorized_credentials("uid-1", store)
        self.assertEqual(mock_refresh.call_count, 2)

    def test_clear_credentials_cache_all_users(self):
        store = self._store_with_credentials(user_id="uid-1", refresh_token="token-1")
        store._creds["uid-2"] = _FakeCredentialRecord(user_id="uid-2", refresh_token="token-2", scopes=["a"])
        with patch("classpilot.google_oauth.Credentials.refresh", autospec=True,
                   side_effect=self._make_refreshing_mock()) as mock_refresh:
            get_authorized_credentials("uid-1", store)
            get_authorized_credentials("uid-2", store)
            clear_credentials_cache()
            get_authorized_credentials("uid-1", store)
            get_authorized_credentials("uid-2", store)
        self.assertEqual(mock_refresh.call_count, 4)


if __name__ == "__main__":
    unittest.main()
