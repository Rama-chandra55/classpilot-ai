"""
tests/test_identity.py

Tests for classpilot/identity.py — the internal request/user identity
abstraction. No database or network needed; only classpilot.config's
CLASSPILOT_DEV_USER_ID setting matters.

Key properties under test (Phase 3 requirements):
  - user_id is never exposed as, or derived from, an MCP tool argument —
    resolve_identity() takes nothing.
  - No process-global mutable identity state: resolve_identity() returns
    a fresh, immutable RequestIdentity every call, so concurrent callers
    never share or race over identity state.
  - Fails loudly (IdentityResolutionError) rather than silently defaulting
    to an empty/sentinel identity when unconfigured.
"""

import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from classpilot.identity import resolve_identity, RequestIdentity, IdentityResolutionError
from classpilot.config import ClassPilotConfig


def _config_with_dev_user(user_id: str, mode: str = "local_dev") -> ClassPilotConfig:
    """Phase 4: CLASSPILOT_DEV_USER_ID is only consulted when
    MCP_AUTH_MODE=local_dev is explicitly set, so these tests opt into
    that mode deliberately. The fail-closed "remote" default is covered
    separately by TestRemoteModeFailsClosed below."""
    cfg = ClassPilotConfig()
    cfg.classpilot_dev_user_id = user_id
    cfg.mcp_auth_mode = mode
    return cfg


class TestResolveIdentity(unittest.TestCase):
    def test_returns_configured_user_id(self):
        with patch("classpilot.identity.get_config", return_value=_config_with_dev_user("user-abc-123")):
            identity = resolve_identity()
        self.assertEqual(identity.user_id, "user-abc-123")

    def test_returns_request_identity_instance(self):
        with patch("classpilot.identity.get_config", return_value=_config_with_dev_user("u1")):
            identity = resolve_identity()
        self.assertIsInstance(identity, RequestIdentity)

    def test_source_is_local_dev_when_dev_mode_enabled(self):
        with patch("classpilot.identity.get_config", return_value=_config_with_dev_user("u1")):
            identity = resolve_identity()
        self.assertEqual(identity.source, "local_dev")

    def test_empty_user_id_raises(self):
        with patch("classpilot.identity.get_config", return_value=_config_with_dev_user("")):
            with self.assertRaises(IdentityResolutionError):
                resolve_identity()

    def test_error_message_is_actionable_not_a_stack_trace(self):
        with patch("classpilot.identity.get_config", return_value=_config_with_dev_user("")):
            with self.assertRaises(IdentityResolutionError) as ctx:
                resolve_identity()
        self.assertIn("CLASSPILOT_DEV_USER_ID", str(ctx.exception))

    def test_each_call_returns_a_new_object_not_a_cached_singleton(self):
        """No process-global mutable identity — every call is independent."""
        with patch("classpilot.identity.get_config", return_value=_config_with_dev_user("u1")):
            first = resolve_identity()
            second = resolve_identity()
        self.assertEqual(first, second)   # same value...
        self.assertIsNot(first, second)   # ...but never the same object

    def test_identity_is_immutable(self):
        with patch("classpilot.identity.get_config", return_value=_config_with_dev_user("u1")):
            identity = resolve_identity()
        with self.assertRaises(Exception):  # frozen dataclass -> FrozenInstanceError
            identity.user_id = "someone-else"

    def test_takes_no_arguments(self):
        """The core security property: nothing about identity can be
        supplied by a caller — there is no parameter to control."""
        import inspect
        sig = inspect.signature(resolve_identity)
        self.assertEqual(len(sig.parameters), 0)


class TestResolveIdentityConcurrency(unittest.TestCase):
    """
    Phase 3 has exactly one configured identity, so concurrent calls are
    expected to all resolve to the SAME user_id — the point of these
    tests is proving that's safe (no shared mutable state, no exceptions,
    no partially-constructed objects under concurrent access), not that
    different threads see different identities (that's a Phase 4 concern,
    tested instead at components that already accept explicit identity —
    see tests/test_multiuser_isolation.py's concurrency tests).
    """

    def test_concurrent_calls_all_succeed_with_consistent_result(self):
        with patch("classpilot.identity.get_config", return_value=_config_with_dev_user("stable-user")):
            with ThreadPoolExecutor(max_workers=16) as pool:
                results = list(pool.map(lambda _: resolve_identity(), range(64)))

        self.assertTrue(all(r.user_id == "stable-user" for r in results))
        self.assertEqual(len(results), 64)

    def test_concurrent_calls_produce_distinct_objects(self):
        """Guards against an accidental future regression toward a cached
        global singleton — every result must be its own object."""
        with patch("classpilot.identity.get_config", return_value=_config_with_dev_user("stable-user")):
            with ThreadPoolExecutor(max_workers=16) as pool:
                results = list(pool.map(lambda _: resolve_identity(), range(32)))

        ids_seen = {id(r) for r in results}
        self.assertEqual(len(ids_seen), len(results))


if __name__ == "__main__":
    unittest.main()


# ── Phase 4: fail-closed remote auth + request-scoped identity ────────────────

class TestRemoteModeFailsClosed(unittest.TestCase):
    """
    The core Phase 4 security property: in the DEFAULT "remote" mode, an
    unauthenticated request must be rejected — CLASSPILOT_DEV_USER_ID is
    ignored entirely, so a forgotten/misconfigured setting denies access
    rather than silently granting every caller the dev user's account.
    """

    def test_remote_mode_is_the_default(self):
        cfg = ClassPilotConfig()
        self.assertEqual(cfg.mcp_auth_mode, "remote")
        self.assertFalse(cfg.is_local_dev_auth)

    def test_unauthenticated_remote_request_raises(self):
        cfg = _config_with_dev_user("some-dev-user", mode="remote")
        with patch("classpilot.identity.get_config", return_value=cfg):
            with patch("classpilot.identity._identity_from_mcp_request", return_value=None):
                with self.assertRaises(IdentityResolutionError):
                    resolve_identity()

    def test_dev_user_id_is_ignored_entirely_in_remote_mode(self):
        """Even with a perfectly valid CLASSPILOT_DEV_USER_ID configured,
        remote mode must NOT fall back to it."""
        cfg = _config_with_dev_user("a-real-looking-user-id", mode="remote")
        with patch("classpilot.identity.get_config", return_value=cfg):
            with patch("classpilot.identity._identity_from_mcp_request", return_value=None):
                with self.assertRaises(IdentityResolutionError) as ctx:
                    resolve_identity()
        self.assertNotIn("a-real-looking-user-id", str(ctx.exception))

    def test_local_dev_mode_without_dev_user_id_also_raises(self):
        cfg = _config_with_dev_user("", mode="local_dev")
        with patch("classpilot.identity.get_config", return_value=cfg):
            with patch("classpilot.identity._identity_from_mcp_request", return_value=None):
                with self.assertRaises(IdentityResolutionError):
                    resolve_identity()


class TestIdentityFromAuthenticatedRequest(unittest.TestCase):
    """Identity resolved from the authenticated MCP request context always
    wins, and is never influenced by config or by any caller argument."""

    def test_authenticated_request_identity_is_used(self):
        authed = RequestIdentity(user_id="user-from-token", source="mcp_session")
        cfg = _config_with_dev_user("dev-user-should-be-ignored", mode="local_dev")
        with patch("classpilot.identity.get_config", return_value=cfg):
            with patch("classpilot.identity._identity_from_mcp_request", return_value=authed):
                identity = resolve_identity()
        self.assertEqual(identity.user_id, "user-from-token")
        self.assertEqual(identity.source, "mcp_session")

    def test_authenticated_identity_beats_local_dev_fallback(self):
        """Even in local_dev mode, a real authenticated token wins — dev
        mode is a fallback, never an override."""
        authed = RequestIdentity(user_id="real-user", source="mcp_session")
        cfg = _config_with_dev_user("dev-user", mode="local_dev")
        with patch("classpilot.identity.get_config", return_value=cfg):
            with patch("classpilot.identity._identity_from_mcp_request", return_value=authed):
                self.assertEqual(resolve_identity().user_id, "real-user")

    def test_token_without_subject_is_treated_as_unauthenticated(self):
        """A verified token carrying no subject is unusable — must fail
        closed rather than guessing an identity."""
        import classpilot.identity as identity_mod
        fake_token = MagicMock()
        fake_token.subject = None
        with patch("classpilot.identity._get_access_token", return_value=fake_token):
            self.assertIsNone(identity_mod._identity_from_mcp_request())

    def test_no_request_context_returns_none_rather_than_raising(self):
        import classpilot.identity as identity_mod
        with patch("classpilot.identity._get_access_token",
                   side_effect=RuntimeError("no active request")):
            self.assertIsNone(identity_mod._identity_from_mcp_request())

    def test_subject_is_read_from_the_token_not_from_any_argument(self):
        """resolve_identity() takes no arguments and the user_id comes
        solely from the verified token's subject — nothing a caller sends
        can influence it."""
        import classpilot.identity as identity_mod
        fake_token = MagicMock()
        fake_token.subject = "subject-user-id"
        with patch("classpilot.identity._get_access_token", return_value=fake_token):
            identity = identity_mod._identity_from_mcp_request()
        self.assertEqual(identity.user_id, "subject-user-id")
        self.assertEqual(identity.source, "mcp_session")
