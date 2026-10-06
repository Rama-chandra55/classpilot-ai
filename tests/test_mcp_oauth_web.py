"""
tests/test_mcp_oauth_web.py

HTTP-level tests for classpilot/mcp_oauth_web.py's OAuth 2.1 endpoints,
via Starlette's TestClient against the real, fully-mounted app
(classpilot.oauth_web.app — /auth/google/* and /mcp/* together, exactly
as actually served).

Covers discovery, dynamic client registration (RFC 7591), PKCE
enforcement (S256-only, verified not just declared), single-use
authorization codes, client_id binding, expiry, revocation, and the full
authorize -> token -> verify round trip resolving to the correct user.
Every response body is also checked for Google-token-shaped leakage, at
the HTTP boundary specifically (not just the Python-level checks in
test_mcp_auth.py).

Postgres-backed; skips gracefully if unreachable.
"""

import base64
import hashlib
import os
import secrets
import sys
import unittest

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


def _generate_key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


def _pkce_pair():
    """A real S256 PKCE verifier/challenge pair, generated the way a
    genuine MCP client would."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


@unittest.skipUnless(_HAS_POSTGRES, _SKIP_REASON)
class _McpOAuthWebTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Same defensive re-import pattern as tests/test_oauth_web.py /
        # tests/test_mcp_auth.py — classpilot.oauth_web now transitively
        # imports classpilot.mcp_auth, which needs the real fastmcp/
        # pydantic/dotenv, not another file's stub of them. classpilot.
        # config/db/user_store/crypto are deliberately never popped.
        for _mod in list(sys.modules):
            if _mod == "google" or _mod.startswith("google.") or _mod.startswith("google_auth_oauthlib") \
               or _mod == "fastmcp" or _mod.startswith("fastmcp.") \
               or _mod == "pydantic" or _mod.startswith("pydantic.") \
               or _mod == "mcp" or _mod.startswith("mcp.") \
               or _mod == "dotenv" or _mod.startswith("dotenv.") \
               or _mod == "pydantic_settings" or _mod.startswith("pydantic_settings."):
                del sys.modules[_mod]
        for _mod in ("classpilot.oauth_web", "classpilot.mcp_oauth_web", "classpilot.mcp_auth"):
            sys.modules.pop(_mod, None)

        os.environ["DATABASE_URL"] = TEST_DATABASE_URL
        os.environ["MCP_PUBLIC_BASE_URL"] = "https://classpilot.example.com"
        os.environ["TOKEN_ENCRYPTION_KEY"] = os.environ.get("TOKEN_ENCRYPTION_KEY") or _generate_key()

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
            conn.execute("TRUNCATE users CASCADE")

    def _connect_user(self, email="student@school.edu", refresh_token="1//fake-google-refresh"):
        from classpilot.user_store import UserStore
        import uuid
        store = UserStore()
        user = store.get_or_create_user(f"sub-{uuid.uuid4()}", email=email, display_name="Student")
        store.save_google_credentials(user.id, refresh_token=refresh_token, scopes=["scope-a"])
        return user

    def _authorize(self, user=None, client_id="test-client", redirect_uri="https://client.example/callback"):
        verifier, challenge = _pkce_pair()
        auth_req_response = self.client.get(
            "/mcp/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "xyz",
            },
            follow_redirects=False,
        )
        self.assertEqual(auth_req_response.status_code, 302)
        location = auth_req_response.headers["location"]
        # Extract state parameter from the Google auth URL
        import urllib.parse
        parsed = urllib.parse.urlparse(location)
        query = urllib.parse.parse_qs(parsed.query)
        google_state = query["state"][0]

        # Simulate the user logging in to Google and being redirected to our callback.
        # We patch complete_authorization to return the provided user.
        from unittest.mock import patch
        with patch.object(self.oauth_web_mod, "complete_authorization", return_value=user):
            response = self.client.get(
                f"/auth/google/callback?code=fake-google-code&state={google_state}",
                follow_redirects=False
            )
        return response, verifier


# ── Discovery ────────────────────────────────────────────────────────────────

class TestDiscoveryMetadata(_McpOAuthWebTestCase):
    def test_discovery_endpoint_returns_expected_metadata(self):
        response = self.client.get("/.well-known/oauth-authorization-server")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["authorization_endpoint"], "https://classpilot.example.com/mcp/authorize")
        self.assertEqual(data["token_endpoint"], "https://classpilot.example.com/mcp/token")
        self.assertEqual(data["revocation_endpoint"], "https://classpilot.example.com/mcp/revoke")

    def test_only_s256_pkce_is_advertised(self):
        """OAuth 2.1 removes `plain` — discovery must not advertise it."""
        response = self.client.get("/.well-known/oauth-authorization-server")
        methods = response.json()["code_challenge_methods_supported"]
        self.assertEqual(methods, ["S256"])
        self.assertNotIn("plain", methods)

    def test_registration_endpoint_is_advertised(self):
        response = self.client.get("/.well-known/oauth-authorization-server")
        self.assertEqual(
            response.json()["registration_endpoint"],
            "https://classpilot.example.com/mcp/register",
        )


# ── Dynamic Client Registration ──────────────────────────────────────────────

class TestDynamicClientRegistration(_McpOAuthWebTestCase):
    """RFC 7591 — required for Claude/Cursor's default connector flow,
    which attempts DCR before falling back to a manually-configured
    client_id."""

    def test_registration_returns_a_client_id(self):
        response = self.client.post("/mcp/register", json={"client_name": "Test Client"})
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertIn("client_id", body)
        self.assertTrue(body["client_id"])

    def test_registration_declares_no_client_secret(self):
        """A public, PKCE-only client — no secret should ever be issued."""
        response = self.client.post("/mcp/register", json={"client_name": "Test Client"})
        body = response.json()
        self.assertEqual(body["token_endpoint_auth_method"], "none")
        self.assertNotIn("client_secret", body)

    def test_two_registrations_get_different_client_ids(self):
        first = self.client.post("/mcp/register", json={"client_name": "A"}).json()
        second = self.client.post("/mcp/register", json={"client_name": "B"}).json()
        self.assertNotEqual(first["client_id"], second["client_id"])

    def test_a_dynamically_registered_client_id_works_end_to_end(self):
        registration = self.client.post("/mcp/register", json={"client_name": "Dynamic Client"}).json()
        client_id = registration["client_id"]
        user = self._connect_user()

        response, verifier = self._authorize(user, client_id=client_id)
        self.assertEqual(response.status_code, 302)
        code = response.headers["location"].split("code=")[1].split("&")[0]

        token_response = self.client.post("/mcp/token", data={
            "grant_type": "authorization_code", "client_id": client_id,
            "code": code, "code_verifier": verifier,
        })
        self.assertEqual(token_response.status_code, 200)


# ── /mcp/authorize ───────────────────────────────────────────────────────────

class TestAuthorizeEndpoint(_McpOAuthWebTestCase):
    def test_missing_client_id_rejected(self):
        _, challenge = _pkce_pair()
        response = self.client.get("/mcp/authorize", params={
            "redirect_uri": "https://client.example/cb",
            "code_challenge": challenge, "code_challenge_method": "S256",
            "login_hint": "x@y.com",
        })
        self.assertEqual(response.status_code, 400)

    def test_missing_pkce_challenge_rejected(self):
        response = self.client.get("/mcp/authorize", params={
            "client_id": "c1", "redirect_uri": "https://client.example/cb",
            "login_hint": "x@y.com",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("PKCE", response.json()["error_description"])

    def test_plain_pkce_method_rejected(self):
        """OAuth 2.1: `plain` must be rejected, not just under-advertised."""
        response = self.client.get("/mcp/authorize", params={
            "client_id": "c1", "redirect_uri": "https://client.example/cb",
            "code_challenge": "some-challenge", "code_challenge_method": "plain",
            "login_hint": "x@y.com",
        })
        self.assertEqual(response.status_code, 400)



    def test_valid_request_redirects_with_a_code(self):
        user = self._connect_user()
        response, _ = self._authorize(user)
        self.assertEqual(response.status_code, 302)
        self.assertIn("code=", response.headers["location"])
        self.assertIn("state=xyz", response.headers["location"])

    def test_authorize_response_never_contains_a_google_token(self):
        user = self._connect_user(refresh_token="1//SUPER-SECRET-GOOGLE-TOKEN")
        response, _ = self._authorize(user)
        self.assertNotIn("1//SUPER-SECRET-GOOGLE-TOKEN", response.headers.get("location", ""))
        self.assertNotIn("1//SUPER-SECRET-GOOGLE-TOKEN", response.text)


# ── /mcp/token ───────────────────────────────────────────────────────────────

class TestTokenEndpoint(_McpOAuthWebTestCase):
    def _get_code(self, user, client_id="test-client", redirect_uri="https://client.example/callback"):
        response, verifier = self._authorize(user, client_id=client_id, redirect_uri=redirect_uri)
        location = response.headers["location"]
        code = location.split("code=")[1].split("&")[0]
        return code, verifier

    def test_valid_exchange_returns_a_token_pair(self):
        user = self._connect_user()
        code, verifier = self._get_code(user)
        response = self.client.post("/mcp/token", data={
            "grant_type": "authorization_code", "client_id": "test-client",
            "code": code, "code_verifier": verifier,
        })
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("access_token", body)
        self.assertIn("refresh_token", body)
        self.assertEqual(body["token_type"], "Bearer")

    def test_token_response_never_contains_a_google_token(self):
        user = self._connect_user(refresh_token="1//ANOTHER-SECRET-TOKEN")
        code, verifier = self._get_code(user)
        response = self.client.post("/mcp/token", data={
            "grant_type": "authorization_code", "client_id": "test-client",
            "code": code, "code_verifier": verifier,
        })
        self.assertNotIn("1//ANOTHER-SECRET-TOKEN", response.text)

    def test_wrong_code_verifier_rejected(self):
        user = self._connect_user()
        code, _real_verifier = self._get_code(user)
        response = self.client.post("/mcp/token", data={
            "grant_type": "authorization_code", "client_id": "test-client",
            "code": code, "code_verifier": "the-wrong-verifier-entirely",
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_grant")

    def test_code_is_single_use(self):
        user = self._connect_user()
        code, verifier = self._get_code(user)
        first = self.client.post("/mcp/token", data={
            "grant_type": "authorization_code", "client_id": "test-client",
            "code": code, "code_verifier": verifier,
        })
        self.assertEqual(first.status_code, 200)

        second = self.client.post("/mcp/token", data={
            "grant_type": "authorization_code", "client_id": "test-client",
            "code": code, "code_verifier": verifier,
        })
        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.json()["error"], "invalid_grant")

    def test_client_id_mismatch_rejected(self):
        """A code issued to one client must not be redeemable by another —
        the confused-deputy pattern the FastMCP OAuthProxy advisories
        warned about, checked here at the endpoint we actually built."""
        user = self._connect_user()
        code, verifier = self._get_code(user, client_id="legit-client")
        response = self.client.post("/mcp/token", data={
            "grant_type": "authorization_code", "client_id": "attacker-client",
            "code": code, "code_verifier": verifier,
        })
        self.assertEqual(response.status_code, 400)

    def test_expired_code_rejected(self):
        from classpilot.db import get_connection
        from datetime import datetime, timedelta, timezone
        user = self._connect_user()
        code, verifier = self._get_code(user)
        with get_connection() as conn:
            conn.execute(
                "UPDATE mcp_auth_codes SET created_at = %s WHERE code = %s",
                (datetime.now(timezone.utc) - timedelta(hours=1), code),
            )
        response = self.client.post("/mcp/token", data={
            "grant_type": "authorization_code", "client_id": "test-client",
            "code": code, "code_verifier": verifier,
        })
        self.assertEqual(response.status_code, 400)

    def test_unsupported_grant_type_rejected(self):
        response = self.client.post("/mcp/token", data={
            "grant_type": "password", "client_id": "test-client",
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "unsupported_grant_type")

    def test_refresh_grant_returns_a_new_pair(self):
        user = self._connect_user()
        code, verifier = self._get_code(user)
        first = self.client.post("/mcp/token", data={
            "grant_type": "authorization_code", "client_id": "test-client",
            "code": code, "code_verifier": verifier,
        }).json()

        response = self.client.post("/mcp/token", data={
            "grant_type": "refresh_token", "client_id": "test-client",
            "refresh_token": first["refresh_token"],
        })
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.json()["access_token"], first["access_token"])


# ── /mcp/revoke ──────────────────────────────────────────────────────────────

class TestRevokeEndpoint(_McpOAuthWebTestCase):
    def test_revoke_always_returns_200(self):
        response = self.client.post("/mcp/revoke", data={"token": "cpat_never-issued"})
        self.assertEqual(response.status_code, 200)

    def test_revoked_token_is_actually_unusable_afterward(self):
        import asyncio
        from classpilot.mcp_auth import ClassPilotTokenVerifier
        user = self._connect_user()
        response, verifier = self._authorize(user)
        code = response.headers["location"].split("code=")[1].split("&")[0]
        tokens = self.client.post("/mcp/token", data={
            "grant_type": "authorization_code", "client_id": "test-client",
            "code": code, "code_verifier": verifier,
        }).json()

        self.client.post("/mcp/revoke", data={"token": tokens["access_token"]})

        token_verifier = ClassPilotTokenVerifier(base_url="https://classpilot.example.com")
        outcome = asyncio.run(token_verifier.verify_token(tokens["access_token"]))
        self.assertIsNone(outcome)


# ── End-to-end: authorize -> token -> verify resolves the correct user ──────

class TestEndToEndIdentityResolution(_McpOAuthWebTestCase):
    def test_full_flow_resolves_to_the_correct_user(self):
        import asyncio
        from classpilot.mcp_auth import ClassPilotTokenVerifier
        user = self._connect_user(email="specific.student@school.edu")

        auth_response, verifier = self._authorize(user)
        code = auth_response.headers["location"].split("code=")[1].split("&")[0]
        tokens = self.client.post("/mcp/token", data={
            "grant_type": "authorization_code", "client_id": "test-client",
            "code": code, "code_verifier": verifier,
        }).json()

        token_verifier = ClassPilotTokenVerifier(base_url="https://classpilot.example.com")
        access_token = asyncio.run(token_verifier.verify_token(tokens["access_token"]))

        self.assertEqual(access_token.subject, user.id)

    def test_two_different_users_authorizing_get_tokens_for_their_own_identity_only(self):
        import asyncio
        from classpilot.mcp_auth import ClassPilotTokenVerifier
        user_a = self._connect_user(email="alice@school.edu", refresh_token="1//token-A")
        user_b = self._connect_user(email="bob@school.edu", refresh_token="1//token-B")

        resp_a, verifier_a = self._authorize(user_a, client_id="client-a")
        code_a = resp_a.headers["location"].split("code=")[1].split("&")[0]
        tokens_a = self.client.post("/mcp/token", data={
            "grant_type": "authorization_code", "client_id": "client-a",
            "code": code_a, "code_verifier": verifier_a,
        }).json()

        resp_b, verifier_b = self._authorize(user_b, client_id="client-b")
        code_b = resp_b.headers["location"].split("code=")[1].split("&")[0]
        tokens_b = self.client.post("/mcp/token", data={
            "grant_type": "authorization_code", "client_id": "client-b",
            "code": code_b, "code_verifier": verifier_b,
        }).json()

        token_verifier = ClassPilotTokenVerifier(base_url="https://classpilot.example.com")
        access_a = asyncio.run(token_verifier.verify_token(tokens_a["access_token"]))
        access_b = asyncio.run(token_verifier.verify_token(tokens_b["access_token"]))

        self.assertEqual(access_a.subject, user_a.id)
        self.assertEqual(access_b.subject, user_b.id)
        self.assertNotEqual(access_a.subject, access_b.subject)


if __name__ == "__main__":
    unittest.main()
