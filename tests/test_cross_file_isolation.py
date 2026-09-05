"""
tests/test_cross_file_isolation.py

Regression coverage for a real, reproduced cross-test-file state-pollution
bug: running `pytest tests/test_google_oauth.py tests/test_oauth_state.py`
caused every test_oauth_state.py test to fail with
`psycopg_pool.PoolTimeout` / `password authentication failed for user
"classpilot"` — even though test_oauth_state.py's own setUpClass
explicitly sets DATABASE_URL to the correct TEST_DATABASE_URL and resets
classpilot.config's cached singleton.

Root cause (see the detailed docstring on
tests/test_google_oauth.py::TestGrantedScopeSupersetHandling for the full
account): that test class used to pop-and-"restore" classpilot.config
(and classpilot.user_store/classpilot.classroom_client/
classpilot.google_oauth) in sys.modules directly. A raw
`sys.modules[key] = original_obj` reassignment only fixes the sys.modules
DICT entry — it does not update the classpilot PACKAGE's own `.config`
ATTRIBUTE, which `import classpilot.config as x` actually resolves
through (a getattr on the parent module, per CPython's IMPORT_FROM opcode
semantics for dotted-with-alias imports). That left the two permanently
diverged after the class ran, so test_oauth_state.py's later
`config_mod._config = None` was resetting a different config instance
than the one classpilot.db.get_config was bound to (captured once, at
db.py's own first import, never reloaded) — so the DATABASE_URL reset
never reached pool construction, and classpilot.db silently fell back to
config.py's hardcoded default DSN.

This is tested here via a NESTED pytest subprocess invocation — the only
way to genuinely prove "does running these two files together, in this
order, actually work end-to-end", since the bug is specifically about
state that only manifests across a real multi-file pytest session, not
within a single test function.

Requires a reachable Postgres (skips gracefully otherwise, matching the
rest of this suite's Postgres-backed tests) and can take a few seconds
per case since each one spawns a real pytest subprocess.
"""

import os
import subprocess
import sys
import unittest

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
_SKIP_REASON = (
    f"No reachable Postgres at {TEST_DATABASE_URL} — see README's Database setup section."
)

_TESTS_DIR = os.path.dirname(__file__)
_PROJECT_ROOT = os.path.join(_TESTS_DIR, "..")


def _run_pytest_subprocess(*relative_test_paths: str) -> subprocess.CompletedProcess:
    """
    Spawn a genuinely separate pytest process running exactly the given
    test files, in the given order — this is what actually exercises
    cross-file sys.modules state the way a real `pytest tests/a.py
    tests/b.py` invocation does; calling test classes directly from
    within this process would not reproduce the bug, since it depends on
    pytest's own file collection/execution ordering.
    """
    paths = [os.path.join(_TESTS_DIR, p) for p in relative_test_paths]
    env = dict(os.environ)
    env["TEST_DATABASE_URL"] = TEST_DATABASE_URL
    return subprocess.run(
        [sys.executable, "-m", "pytest", *paths, "-v", "--tb=short"],
        cwd=_PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


@unittest.skipUnless(_HAS_POSTGRES, _SKIP_REASON)
class TestCrossFileModuleIsolation(unittest.TestCase):
    """
    Each case here spawns a real, separate pytest subprocess — these are
    slower than the rest of the suite (a few seconds each), which is why
    they live in their own dedicated test class rather than being folded
    into test_google_oauth.py or test_oauth_state.py directly.
    """

    def test_google_oauth_then_oauth_state_order_passes(self):
        """
        The exact reported failing command:
            pytest tests/test_google_oauth.py tests/test_oauth_state.py -v

        Must fully pass, with test_oauth_state.py's Postgres-backed tests
        genuinely running (not silently skipped due to a connection
        failure masking the same underlying bug as a false negative).
        """
        result = _run_pytest_subprocess("test_google_oauth.py", "test_oauth_state.py")
        self.assertEqual(
            result.returncode, 0,
            f"Subprocess failed.\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )
        self.assertNotIn("PoolTimeout", result.stdout)
        self.assertNotIn("password authentication failed", result.stdout)
        # Guard against a false-negative pass via skip: the whole point is
        # that test_oauth_state.py's DB-backed tests actually execute.
        self.assertNotIn(
            "SKIPPED", result.stdout,
            "test_oauth_state.py tests were skipped instead of actually "
            "running — this would hide a regression of the same bug "
            "behind a passing subprocess exit code.",
        )
        self.assertIn("passed", result.stdout)

    def test_oauth_state_then_google_oauth_order_also_passes(self):
        """Reverse order — the fix must not be order-dependent itself."""
        result = _run_pytest_subprocess("test_oauth_state.py", "test_google_oauth.py")
        self.assertEqual(
            result.returncode, 0,
            f"Subprocess failed.\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )
        self.assertNotIn("PoolTimeout", result.stdout)
        self.assertNotIn("password authentication failed", result.stdout)
        self.assertNotIn("SKIPPED", result.stdout)
        self.assertIn("passed", result.stdout)

    def test_oauth_state_alone_still_passes(self):
        """Baseline sanity check: test_oauth_state.py in isolation (the
        scenario that always worked) must keep working after this fix."""
        result = _run_pytest_subprocess("test_oauth_state.py")
        self.assertEqual(
            result.returncode, 0,
            f"Subprocess failed.\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )
        self.assertNotIn("SKIPPED", result.stdout)

    def test_google_oauth_scope_superset_tests_still_pass_standalone(self):
        """Baseline sanity check: the scope-superset regression tests
        (this fix's sibling feature) must keep passing on their own,
        proving the isolation fix didn't regress that one."""
        result = _run_pytest_subprocess("test_google_oauth.py")
        self.assertEqual(
            result.returncode, 0,
            f"Subprocess failed.\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
