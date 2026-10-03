"""
tests/test_mcp_auth.py

Security-focused tests for classpilot/mcp_auth.py (Phase 4): ClassPilot's
own MCP-facing token issuance and verification, kept strictly separate
from Google OAuth (classpilot/google_oauth.py).

Covers:
  - issuance / storage (hashed, never plaintext)
  - verification (ClassPilotTokenVerifier — the FastMCP-facing surface)
  - spoofing: a token issued for user A must never resolve to user B,
    and a forged/tampered token must never verify
  - token leakage: no Google access/refresh token ever appears anywhere
    in the MCP token issuance path or stored rows
  - revocation (single token, and cascading refresh-token revocation)
  - refresh rotation
  - concurrent-user verification (real ThreadPoolExecutor, two real users)
  - failure modes: missing / expired / revoked / malformed tokens all
    fail closed

Postgres-backed; skips gracefully (not a failure) if unreachable,
matching the rest of this suite.
"""

import os
import sys
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
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


def _generate_key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


@unittest.skipUnless(_HAS_POSTGRES, _SKIP_REASON)
class _McpAuthTestCase(unittest.TestCase):
    """
    Same defensive re-import pattern used throughout this suite (see
    tests/test_multiuser_isolation.py / tests/test_oauth_web.py for the
    full rationale): classpilot.mcp_auth does a real, unconditional
    `from fastmcp.server.auth import AccessToken, TokenVerifier` at
    module load, which needs the genuine fastmcp/pydantic/dotenv
    packages, not another test file's stub of them. classpilot.config/
    classpilot.db/classpilot.user_store/classpilot.crypto are
    deliberately NEVER popped (see the same files for the real,
    reproduced bug that causes).
    """

    @classmethod
    def setUpClass(cls):
        for _mod in list(sys.modules):
            if _mod == "fastmcp" or _mod.startswith("fastmcp.") \
               or _mod == "pydantic" or _mod.startswith("pydantic.") \
               or _mod == "mcp" or _mod.startswith("mcp.") \
               or _mod == "dotenv" or _mod.startswith("dotenv.") \
               or _mod == "pydantic_settings" or _mod.startswith("pydantic_settings."):
                del sys.modules[_mod]
        for _mod in ("classpilot.mcp_auth", "classpilot.mcp_oauth_web", "classpilot.oauth_web"):
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
        with get_connection() as conn:
            conn.execute("TRUNCATE users CASCADE")  # cascades to mcp_access_tokens/mcp_auth_codes

    @staticmethod
    def _unique_sub() -> str:
        return f"test-sub-{uuid.uuid4()}"

    def _make_user(self, email="student@school.edu"):
        from classpilot.user_store import UserStore
        store = UserStore()
        return store.get_or_create_user(self._unique_sub(), email=email, display_name="Student")

    def _make_two_users(self):
        return self._make_user("alice@school.edu"), self._make_user("bob@school.edu")


# ── Issuance & storage ──────────────────────────────────────────────────────

class TestTokenIssuanceAndStorage(_McpAuthTestCase):
    def test_issue_returns_a_full_token_response(self):
        from classpilot.mcp_auth import issue_token_pair
        user = self._make_user()
        result = issue_token_pair(user.id, "client-1")
        self.assertIn("access_token", result)
        self.assertIn("refresh_token", result)
        self.assertEqual(result["token_type"], "Bearer")
        self.assertIn("expires_in", result)

    def test_access_and_refresh_tokens_are_distinct(self):
        from classpilot.mcp_auth import issue_token_pair
        user = self._make_user()
        result = issue_token_pair(user.id, "client-1")
        self.assertNotEqual(result["access_token"], result["refresh_token"])

    def test_only_a_hash_is_stored_never_the_plaintext_token(self):
        """The core anti-leak property for stored tokens: even with full
        DB read access, the plaintext token must not be recoverable."""
        from classpilot.mcp_auth import issue_token_pair
        from classpilot.db import get_connection
        user = self._make_user()
        result = issue_token_pair(user.id, "client-1")

        with get_connection() as conn:
            rows = conn.execute(
                "SELECT token_hash FROM mcp_access_tokens WHERE user_id = %s", (user.id,)
            ).fetchall()
        stored_hashes = {r[0] for r in rows}
        self.assertNotIn(result["access_token"], stored_hashes)
        self.assertNotIn(result["refresh_token"], stored_hashes)
        for h in stored_hashes:
            self.assertEqual(len(h), 64)  # sha256 hex digest length
            self.assertNotIn(result["access_token"][:10], h)
            self.assertNotIn(result["refresh_token"][:10], h)

    def test_two_issuances_for_the_same_user_produce_different_tokens(self):
        from classpilot.mcp_auth import issue_token_pair
        user = self._make_user()
        first = issue_token_pair(user.id, "client-1")
        second = issue_token_pair(user.id, "client-1")
        self.assertNotEqual(first["access_token"], second["access_token"])


# ── Token leakage ────────────────────────────────────────────────────────────

class TestNoGoogleTokenLeakage(_McpAuthTestCase):
    """
    The architectural promise of this whole phase: an MCP client receives
    ONLY ClassPilot's own opaque tokens. A Google refresh_token (always
    prefixed "1//" in real Google responses) or access_token must never
    appear anywhere in the issuance response or in what's persisted for
    the MCP-facing tables.
    """

    def test_issued_token_response_contains_no_google_token_shape(self):
        from classpilot.mcp_auth import issue_token_pair
        from classpilot.user_store import UserStore
        user = self._make_user()
        UserStore().save_google_credentials(
            user.id, refresh_token="1//THIS-IS-A-SECRET-GOOGLE-REFRESH-TOKEN", scopes=["scope-a"],
        )
        result = issue_token_pair(user.id, "client-1")
        blob = str(result)
        self.assertNotIn("1//THIS-IS-A-SECRET-GOOGLE-REFRESH-TOKEN", blob)
        self.assertFalse(result["access_token"].startswith("1//"))
        self.assertFalse(result["refresh_token"].startswith("1//"))

    def test_mcp_access_tokens_table_never_stores_a_google_token_value(self):
        from classpilot.mcp_auth import issue_token_pair
        from classpilot.user_store import UserStore
        from classpilot.db import get_connection
        user = self._make_user()
        UserStore().save_google_credentials(
            user.id, refresh_token="1//ANOTHER-SECRET-GOOGLE-TOKEN", scopes=["scope-a"],
        )
        issue_token_pair(user.id, "client-1")

        with get_connection() as conn:
            rows = conn.execute(
                "SELECT token_hash, client_id, scopes::text, resource FROM mcp_access_tokens "
                "WHERE user_id = %s", (user.id,),
            ).fetchall()
        for row in rows:
            for field in row:
                self.assertNotIn("1//ANOTHER-SECRET-GOOGLE-TOKEN", str(field))

    def test_classpilot_token_prefixes_are_never_google_prefixes(self):
        from classpilot.mcp_auth import ACCESS_TOKEN_PREFIX, REFRESH_TOKEN_PREFIX
        self.assertFalse(ACCESS_TOKEN_PREFIX.startswith("1//"))
        self.assertFalse(ACCESS_TOKEN_PREFIX.startswith("ya29."))
        self.assertFalse(REFRESH_TOKEN_PREFIX.startswith("1//"))


# ── Verification (the FastMCP-facing surface) ───────────────────────────────

class TestClassPilotTokenVerifier(_McpAuthTestCase):
    def test_valid_token_verifies_with_correct_subject(self):
        import asyncio
        from classpilot.mcp_auth import issue_token_pair, ClassPilotTokenVerifier
        user = self._make_user()
        result = issue_token_pair(user.id, "client-1")

        verifier = ClassPilotTokenVerifier(base_url="https://classpilot.example.com")
        access_token = asyncio.run(verifier.verify_token(result["access_token"]))

        self.assertIsNotNone(access_token)
        self.assertEqual(access_token.subject, user.id)
        self.assertEqual(access_token.client_id, "client-1")

    def test_refresh_token_does_not_verify_as_an_access_token(self):
        import asyncio
        from classpilot.mcp_auth import issue_token_pair, ClassPilotTokenVerifier
        user = self._make_user()
        result = issue_token_pair(user.id, "client-1")

        verifier = ClassPilotTokenVerifier(base_url="https://classpilot.example.com")
        outcome = asyncio.run(verifier.verify_token(result["refresh_token"]))
        self.assertIsNone(outcome)

    def test_completely_unknown_token_fails_closed(self):
        import asyncio
        from classpilot.mcp_auth import ClassPilotTokenVerifier
        verifier = ClassPilotTokenVerifier(base_url="https://classpilot.example.com")
        outcome = asyncio.run(verifier.verify_token("cpat_this-was-never-issued"))
        self.assertIsNone(outcome)

    def test_empty_token_fails_closed(self):
        import asyncio
        from classpilot.mcp_auth import ClassPilotTokenVerifier
        verifier = ClassPilotTokenVerifier(base_url="https://classpilot.example.com")
        outcome = asyncio.run(verifier.verify_token(""))
        self.assertIsNone(outcome)

    def test_storage_failure_during_verification_fails_closed_not_open(self):
        """A DB error during verification must never be interpreted as
        'valid' — that would be a fail-open security bug."""
        import asyncio
        from classpilot.mcp_auth import ClassPilotTokenVerifier
        verifier = ClassPilotTokenVerifier(base_url="https://classpilot.example.com")
        with patch("classpilot.mcp_auth.lookup_access_token", side_effect=RuntimeError("db down")):
            outcome = asyncio.run(verifier.verify_token("cpat_whatever"))
        self.assertIsNone(outcome)

    def test_expired_token_fails_closed(self):
        import asyncio
        from classpilot.mcp_auth import issue_token_pair, ClassPilotTokenVerifier
        from classpilot.db import get_connection
        user = self._make_user()
        result = issue_token_pair(user.id, "client-1")

        # Simulate expiry without waiting for the real TTL.
        from classpilot.mcp_auth import hash_token
        with get_connection() as conn:
            conn.execute(
                "UPDATE mcp_access_tokens SET expires_at = %s WHERE token_hash = %s",
                (datetime.now(timezone.utc) - timedelta(seconds=1), hash_token(result["access_token"])),
            )

        verifier = ClassPilotTokenVerifier(base_url="https://classpilot.example.com")
        outcome = asyncio.run(verifier.verify_token(result["access_token"]))
        self.assertIsNone(outcome)


# ── Spoofing ─────────────────────────────────────────────────────────────────

class TestSpoofingResistance(_McpAuthTestCase):
    """A token issued for one user must never resolve to, or grant access
    as, a different user — under any manipulation a client could plausibly
    attempt."""

    def test_users_own_token_never_resolves_to_another_user(self):
        import asyncio
        from classpilot.mcp_auth import issue_token_pair, ClassPilotTokenVerifier
        user_a, user_b = self._make_two_users()
        result_a = issue_token_pair(user_a.id, "client-1")

        verifier = ClassPilotTokenVerifier(base_url="https://classpilot.example.com")
        access_token = asyncio.run(verifier.verify_token(result_a["access_token"]))

        self.assertEqual(access_token.subject, user_a.id)
        self.assertNotEqual(access_token.subject, user_b.id)

    def test_truncated_token_does_not_verify(self):
        """A client that only observes a PREFIX of another user's token
        (e.g. via a partial log leak) must not be able to complete it."""
        import asyncio
        from classpilot.mcp_auth import issue_token_pair, ClassPilotTokenVerifier
        user = self._make_user()
        result = issue_token_pair(user.id, "client-1")

        verifier = ClassPilotTokenVerifier(base_url="https://classpilot.example.com")
        truncated = result["access_token"][:-5]
        outcome = asyncio.run(verifier.verify_token(truncated))
        self.assertIsNone(outcome)

    def test_flipping_one_character_invalidates_the_token(self):
        import asyncio
        from classpilot.mcp_auth import issue_token_pair, ClassPilotTokenVerifier
        user = self._make_user()
        result = issue_token_pair(user.id, "client-1")

        tampered = result["access_token"][:-1] + ("x" if result["access_token"][-1] != "x" else "y")
        verifier = ClassPilotTokenVerifier(base_url="https://classpilot.example.com")
        outcome = asyncio.run(verifier.verify_token(tampered))
        self.assertIsNone(outcome)

    def test_a_users_refresh_token_cannot_be_used_to_impersonate_another_client(self):
        from classpilot.mcp_auth import issue_token_pair, refresh_token_pair
        user = self._make_user()
        result = issue_token_pair(user.id, "client-legit")
        forged_attempt = refresh_token_pair(result["refresh_token"], "client-attacker")
        self.assertIsNone(forged_attempt)


# ── Revocation ───────────────────────────────────────────────────────────────

class TestRevocation(_McpAuthTestCase):
    def test_revoked_access_token_fails_verification(self):
        import asyncio
        from classpilot.mcp_auth import issue_token_pair, revoke_token, ClassPilotTokenVerifier
        user = self._make_user()
        result = issue_token_pair(user.id, "client-1")

        self.assertTrue(revoke_token(result["access_token"]))

        verifier = ClassPilotTokenVerifier(base_url="https://classpilot.example.com")
        outcome = asyncio.run(verifier.verify_token(result["access_token"]))
        self.assertIsNone(outcome)

    def test_revoking_refresh_token_cascades_to_its_access_token(self):
        from classpilot.mcp_auth import issue_token_pair, revoke_token, lookup_access_token
        user = self._make_user()
        result = issue_token_pair(user.id, "client-1")

        revoke_token(result["refresh_token"])

        self.assertIsNone(lookup_access_token(result["access_token"]))

    def test_revoking_unknown_token_returns_false_not_an_error(self):
        from classpilot.mcp_auth import revoke_token
        self.assertFalse(revoke_token("cpat_never-issued"))

    def test_revoke_all_for_user_clears_every_live_token(self):
        from classpilot.mcp_auth import issue_token_pair, revoke_all_for_user, lookup_access_token
        user = self._make_user()
        r1 = issue_token_pair(user.id, "client-1")
        r2 = issue_token_pair(user.id, "client-2")

        count = revoke_all_for_user(user.id)
        self.assertGreaterEqual(count, 4)  # 2 access + 2 refresh
        self.assertIsNone(lookup_access_token(r1["access_token"]))
        self.assertIsNone(lookup_access_token(r2["access_token"]))

    def test_revoke_all_for_user_a_does_not_affect_user_b(self):
        from classpilot.mcp_auth import issue_token_pair, revoke_all_for_user, lookup_access_token
        user_a, user_b = self._make_two_users()
        issue_token_pair(user_a.id, "client-1")
        result_b = issue_token_pair(user_b.id, "client-1")

        revoke_all_for_user(user_a.id)

        self.assertIsNotNone(lookup_access_token(result_b["access_token"]))


# ── Refresh rotation ─────────────────────────────────────────────────────────

class TestRefreshRotation(_McpAuthTestCase):
    def test_refresh_issues_a_new_pair(self):
        from classpilot.mcp_auth import issue_token_pair, refresh_token_pair
        user = self._make_user()
        original = issue_token_pair(user.id, "client-1")
        renewed = refresh_token_pair(original["refresh_token"], "client-1")

        self.assertIsNotNone(renewed)
        self.assertNotEqual(renewed["access_token"], original["access_token"])
        self.assertNotEqual(renewed["refresh_token"], original["refresh_token"])

    def test_old_refresh_token_is_single_use(self):
        """OAuth 2.1 rotation: once a refresh token has been used, it must
        not work again — reuse is a signal of token theft."""
        from classpilot.mcp_auth import issue_token_pair, refresh_token_pair
        user = self._make_user()
        original = issue_token_pair(user.id, "client-1")
        refresh_token_pair(original["refresh_token"], "client-1")

        replay_attempt = refresh_token_pair(original["refresh_token"], "client-1")
        self.assertIsNone(replay_attempt)

    def test_old_access_token_still_works_until_it_naturally_expires(self):
        """Rotating the refresh token doesn't retroactively kill an
        already-issued, still-valid access token."""
        from classpilot.mcp_auth import issue_token_pair, refresh_token_pair, lookup_access_token
        user = self._make_user()
        original = issue_token_pair(user.id, "client-1")
        refresh_token_pair(original["refresh_token"], "client-1")

        self.assertIsNotNone(lookup_access_token(original["access_token"]))


# ── Concurrent-user verification ─────────────────────────────────────────────

class TestConcurrentUserVerification(_McpAuthTestCase):
    """
    Real concurrency, at the component that already accepts an explicit
    token (ClassPilotTokenVerifier.verify_token) — matching how this
    suite tests concurrency elsewhere (see test_multiuser_isolation.py):
    not by monkeypatching per-thread, but by exercising the real,
    already-safe verification path under real concurrent load with two
    real users.
    """

    def test_concurrent_verification_never_crosses_users(self):
        import asyncio
        from classpilot.mcp_auth import issue_token_pair, ClassPilotTokenVerifier
        user_a, user_b = self._make_two_users()
        result_a = issue_token_pair(user_a.id, "client-1")
        result_b = issue_token_pair(user_b.id, "client-1")
        verifier = ClassPilotTokenVerifier(base_url="https://classpilot.example.com")

        def verify(token, expected_user_id):
            access_token = asyncio.run(verifier.verify_token(token))
            return access_token is not None and access_token.subject == expected_user_id

        tasks = [(result_a["access_token"], user_a.id)] * 25 + [(result_b["access_token"], user_b.id)] * 25

        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(lambda t: verify(*t), tasks))

        self.assertTrue(all(results), "Some concurrent verification returned the WRONG user's identity")


if __name__ == "__main__":
    unittest.main()
