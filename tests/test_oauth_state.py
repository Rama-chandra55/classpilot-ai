"""
tests/test_oauth_state.py

Integration tests for classpilot/oauth_state.py against a REAL Postgres
database — mirrors tests/test_user_store.py's approach: skips gracefully
(not a failure) if no Postgres is reachable, so `pytest tests/` stays
green without one set up, but gives real coverage wherever one is.

Covers both the CSRF state lifecycle AND the PKCE code_verifier that now
travels alongside it in the same row (see the module docstring in
classpilot/oauth_state.py for why they're stored together) — this is the
regression coverage for the "Missing code verifier" bug: a state's
code_verifier must survive being written at /login time and read back
unchanged at /callback time, exactly once.
"""

import os
import sys
import time
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


_SKIP_REASON = (
    f"No reachable Postgres at {TEST_DATABASE_URL} — see README's Database setup section."
)
_HAS_POSTGRES = _postgres_available()


@unittest.skipUnless(_HAS_POSTGRES, _SKIP_REASON)
class _PostgresTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["DATABASE_URL"] = TEST_DATABASE_URL

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
            conn.execute("TRUNCATE oauth_states")


class TestGenerateAndRegisterState(_PostgresTestCase):
    def test_generate_state_returns_nonempty_string(self):
        from classpilot.oauth_state import generate_state
        state = generate_state()
        self.assertIsInstance(state, str)
        self.assertGreater(len(state), 20)

    def test_two_generated_states_are_different(self):
        from classpilot.oauth_state import generate_state
        self.assertNotEqual(generate_state(), generate_state())

    def test_generate_state_does_not_touch_the_database(self):
        """Pure function — generating a state value alone must not create
        a row (register_state does that explicitly, once the code_verifier
        is also known)."""
        from classpilot.oauth_state import generate_state
        from classpilot.db import get_connection
        generate_state()
        with get_connection() as conn:
            count = conn.execute("SELECT count(*) FROM oauth_states").fetchone()[0]
        self.assertEqual(count, 0)

    def test_register_state_rejects_empty_state(self):
        from classpilot.oauth_state import register_state
        with self.assertRaises(ValueError):
            register_state("", "some-verifier")

    def test_register_state_rejects_empty_code_verifier(self):
        from classpilot.oauth_state import register_state
        with self.assertRaises(ValueError):
            register_state("some-state", "")


class TestConsumeStateReturnsCodeVerifier(_PostgresTestCase):
    """
    Regression coverage for the real bug: exchange_code_for_user failed
    with "(invalid_grant) Missing code verifier" because the verifier
    generated at /login time never reached /callback. These tests prove
    the round-trip: whatever code_verifier was registered alongside a
    state is exactly what comes back out of consume_state for that state.
    """

    def test_valid_state_returns_its_registered_code_verifier(self):
        from classpilot.oauth_state import generate_state, register_state, consume_state
        state = generate_state()
        register_state(state, "the-exact-pkce-verifier-abc123")
        self.assertEqual(consume_state(state), "the-exact-pkce-verifier-abc123")

    def test_different_states_carry_independent_verifiers(self):
        from classpilot.oauth_state import generate_state, register_state, consume_state
        state_a = generate_state()
        state_b = generate_state()
        register_state(state_a, "verifier-for-a")
        register_state(state_b, "verifier-for-b")
        self.assertEqual(consume_state(state_b), "verifier-for-b")
        self.assertEqual(consume_state(state_a), "verifier-for-a")

    def test_state_is_single_use(self):
        """The core CSRF guarantee: consuming a state a second time must fail."""
        from classpilot.oauth_state import generate_state, register_state, consume_state
        state = generate_state()
        register_state(state, "verifier")
        self.assertEqual(consume_state(state), "verifier")
        self.assertIsNone(consume_state(state))

    def test_unknown_state_rejected(self):
        from classpilot.oauth_state import consume_state
        self.assertIsNone(consume_state("forged-state-value-that-was-never-issued"))

    def test_empty_state_rejected(self):
        from classpilot.oauth_state import consume_state
        self.assertIsNone(consume_state(""))

    def test_none_state_rejected(self):
        from classpilot.oauth_state import consume_state
        self.assertIsNone(consume_state(None))

    def test_expired_state_rejected(self):
        from classpilot.oauth_state import generate_state, register_state, consume_state
        state = generate_state()
        register_state(state, "verifier")
        # ttl_seconds=0 -> "created_at > now() - 0 seconds" is false immediately
        self.assertIsNone(consume_state(state, ttl_seconds=0))

    def test_expired_state_is_deleted_even_though_rejected(self):
        """consume_state deletes the row unconditionally, whether or not
        it turns out to be expired — an expired state (and its verifier)
        can never later become 'valid' just because some future call
        happens to pass a larger ttl_seconds (there is no future call to
        make: it's gone)."""
        from classpilot.oauth_state import generate_state, register_state, consume_state
        state = generate_state()
        register_state(state, "verifier")
        time.sleep(1.1)
        self.assertIsNone(consume_state(state, ttl_seconds=1))    # expired -> rejected
        self.assertIsNone(consume_state(state, ttl_seconds=600))  # already deleted -> still rejected

    def test_state_not_expired_within_ttl(self):
        from classpilot.oauth_state import generate_state, register_state, consume_state
        state = generate_state()
        register_state(state, "verifier")
        time.sleep(0.05)
        self.assertEqual(consume_state(state, ttl_seconds=600), "verifier")

    def test_code_verifier_survives_a_simulated_process_restart(self):
        """The whole reason this lives in Postgres rather than an
        in-memory dict: it must survive the /login handler's process
        state being gone by the time /callback runs (a genuinely different
        request, and in production potentially a different worker
        process). Simulate that by dropping and recreating the connection
        pool between register and consume."""
        from classpilot.oauth_state import generate_state, register_state, consume_state
        import classpilot.db as db_mod

        state = generate_state()
        register_state(state, "verifier-that-must-survive")
        db_mod.close_pool()  # simulate the pool/process being torn down
        # get_pool() lazily recreates it on next use — this is exactly
        # what happens across two separate real HTTP request handlers.
        self.assertEqual(consume_state(state), "verifier-that-must-survive")


class TestPurgeExpiredStates(_PostgresTestCase):
    def test_purge_removes_only_expired_rows(self):
        from classpilot.oauth_state import generate_state, register_state, purge_expired_states
        register_state(generate_state(), "v1")
        register_state(generate_state(), "v2")
        removed = purge_expired_states(ttl_seconds=0)  # everything looks "expired" at ttl=0
        self.assertGreaterEqual(removed, 2)

    def test_purge_does_not_remove_fresh_states_with_real_ttl(self):
        from classpilot.oauth_state import generate_state, register_state, purge_expired_states, consume_state
        state = generate_state()
        register_state(state, "verifier")
        purge_expired_states(ttl_seconds=600)  # generous TTL — nothing should be purged
        self.assertEqual(consume_state(state), "verifier")  # still there


if __name__ == "__main__":
    unittest.main()
