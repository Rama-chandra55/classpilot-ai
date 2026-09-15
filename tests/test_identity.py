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
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from classpilot.identity import resolve_identity, RequestIdentity, IdentityResolutionError
from classpilot.config import ClassPilotConfig


def _config_with_dev_user(user_id: str) -> ClassPilotConfig:
    cfg = ClassPilotConfig()
    cfg.classpilot_dev_user_id = user_id
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

    def test_source_is_dev_config_in_phase_3(self):
        with patch("classpilot.identity.get_config", return_value=_config_with_dev_user("u1")):
            identity = resolve_identity()
        self.assertEqual(identity.source, "dev_config")

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
