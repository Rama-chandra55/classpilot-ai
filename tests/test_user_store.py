"""
tests/test_user_store.py

Integration tests for classpilot/user_store.py + classpilot/db.py against a
REAL PostgreSQL database — unlike the rest of this test suite, which is
fully mocked/stubbed, these tests need a live Postgres instance because
the whole point of this phase is proving the schema, encryption, and CRUD
actually work end-to-end, not just that the SQL strings look plausible.

Skips gracefully (not a failure) if no Postgres is reachable, the same way
tests/test_visual_extractor.py already skips its `reportlab`-dependent
cases when that optional package isn't installed — so `pytest tests/`
still passes cleanly in this environment/CI without a database, but gives
real, meaningful coverage wherever one is set up (see README's "Database
setup" section for how to point this at a local instance).

Connection target: DATABASE_URL env var if set, else
postgresql://classpilot:classpilot@localhost:5432/classpilot_test — a
SEPARATE database from the app's own `classpilot` dev database, so running
tests never touches real dev data.
"""

import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cryptography.fernet import Fernet

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://classpilot:classpilot_dev_pw@127.0.0.1:5432/classpilot_test",
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
    f"No reachable Postgres at {TEST_DATABASE_URL} — set TEST_DATABASE_URL "
    "or start a local Postgres to run these integration tests. See README's "
    "Database setup section."
)
_HAS_POSTGRES = _postgres_available()


@unittest.skipUnless(_HAS_POSTGRES, _SKIP_REASON)
class _PostgresTestCase(unittest.TestCase):
    """Common setup: point classpilot.config at the test database + a
    throwaway encryption key, ensure schema exists, clean tables between
    tests so cases don't interfere with each other."""

    @classmethod
    def setUpClass(cls):
        os.environ["DATABASE_URL"] = TEST_DATABASE_URL
        os.environ["TOKEN_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

        # Force fresh config/pool so the env vars above actually take
        # effect, regardless of what any earlier test/module already
        # cached — classpilot.config is a process-wide singleton.
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
            conn.execute("TRUNCATE users CASCADE")

    @staticmethod
    def _unique_sub() -> str:
        return f"test-sub-{uuid.uuid4()}"


class TestGetOrCreateUser(_PostgresTestCase):
    def test_creates_new_user(self):
        from classpilot.user_store import UserStore
        store = UserStore()
        sub = self._unique_sub()
        user = store.get_or_create_user(sub, email="alice@school.edu", display_name="Alice")
        self.assertEqual(user.google_sub, sub)
        self.assertEqual(user.email, "alice@school.edu")
        self.assertEqual(user.display_name, "Alice")
        self.assertTrue(user.id)  # a real UUID string was assigned

    def test_second_call_with_same_sub_returns_same_user_id(self):
        from classpilot.user_store import UserStore
        store = UserStore()
        sub = self._unique_sub()
        first = store.get_or_create_user(sub, email="a@x.com")
        second = store.get_or_create_user(sub, email="a@x.com")
        self.assertEqual(first.id, second.id)

    def test_second_call_updates_email_and_display_name(self):
        from classpilot.user_store import UserStore
        store = UserStore()
        sub = self._unique_sub()
        first = store.get_or_create_user(sub, email="old@x.com", display_name="Old Name")
        second = store.get_or_create_user(sub, email="new@x.com", display_name="New Name")
        self.assertEqual(second.id, first.id)
        self.assertEqual(second.email, "new@x.com")
        self.assertEqual(second.display_name, "New Name")

    def test_omitting_email_on_update_keeps_existing_value(self):
        """COALESCE behavior: calling get_or_create_user again without an
        email must not blank out a previously stored one."""
        from classpilot.user_store import UserStore
        store = UserStore()
        sub = self._unique_sub()
        store.get_or_create_user(sub, email="keep-me@x.com")
        again = store.get_or_create_user(sub)  # no email passed this time
        self.assertEqual(again.email, "keep-me@x.com")

    def test_two_different_subs_get_different_users(self):
        from classpilot.user_store import UserStore
        store = UserStore()
        u1 = store.get_or_create_user(self._unique_sub())
        u2 = store.get_or_create_user(self._unique_sub())
        self.assertNotEqual(u1.id, u2.id)

    def test_empty_google_sub_rejected(self):
        from classpilot.user_store import UserStore
        store = UserStore()
        with self.assertRaises(ValueError):
            store.get_or_create_user("")


class TestUserLookup(_PostgresTestCase):
    def test_get_user_by_google_sub(self):
        from classpilot.user_store import UserStore
        store = UserStore()
        sub = self._unique_sub()
        created = store.get_or_create_user(sub, email="x@x.com")
        found = store.get_user_by_google_sub(sub)
        self.assertEqual(found.id, created.id)

    def test_get_user_by_google_sub_missing_returns_none(self):
        from classpilot.user_store import UserStore
        store = UserStore()
        self.assertIsNone(store.get_user_by_google_sub("nonexistent-sub-xyz"))

    def test_get_user_by_id(self):
        from classpilot.user_store import UserStore
        store = UserStore()
        created = store.get_or_create_user(self._unique_sub())
        found = store.get_user_by_id(created.id)
        self.assertEqual(found.google_sub, created.google_sub)

    def test_get_user_by_id_missing_returns_none(self):
        from classpilot.user_store import UserStore
        store = UserStore()
        self.assertIsNone(store.get_user_by_id("00000000-0000-0000-0000-000000000000"))


class TestGoogleCredentials(_PostgresTestCase):
    def test_save_and_retrieve_roundtrips_refresh_token(self):
        from classpilot.user_store import UserStore
        store = UserStore()
        user = store.get_or_create_user(self._unique_sub())
        store.save_google_credentials(
            user.id, refresh_token="1//fake-refresh-token",
            scopes=["classroom.courses.readonly", "drive.readonly"],
        )
        creds = store.get_google_credentials(user.id)
        self.assertEqual(creds.refresh_token, "1//fake-refresh-token")
        self.assertEqual(set(creds.scopes), {"classroom.courses.readonly", "drive.readonly"})

    def test_refresh_token_stored_encrypted_not_plaintext(self):
        """Read the raw column directly — the whole point of this table is
        that the plaintext token never touches disk."""
        from classpilot.db import get_connection
        from classpilot.user_store import UserStore
        store = UserStore()
        user = store.get_or_create_user(self._unique_sub())
        secret = "1//super-secret-refresh-token-value"
        store.save_google_credentials(user.id, refresh_token=secret, scopes=["x"])

        with get_connection() as conn:
            row = conn.execute(
                "SELECT encrypted_refresh_token FROM google_oauth_credentials WHERE user_id = %s",
                (user.id,),
            ).fetchone()
        raw_bytes = bytes(row[0])
        self.assertNotIn(secret.encode(), raw_bytes)

    def test_missing_credentials_returns_none(self):
        from classpilot.user_store import UserStore
        store = UserStore()
        user = store.get_or_create_user(self._unique_sub())
        self.assertIsNone(store.get_google_credentials(user.id))

    def test_save_twice_replaces_not_duplicates(self):
        from classpilot.user_store import UserStore
        store = UserStore()
        user = store.get_or_create_user(self._unique_sub())
        store.save_google_credentials(user.id, refresh_token="old-token", scopes=["a"])
        store.save_google_credentials(user.id, refresh_token="new-token", scopes=["a", "b"])
        creds = store.get_google_credentials(user.id)
        self.assertEqual(creds.refresh_token, "new-token")
        self.assertEqual(set(creds.scopes), {"a", "b"})

    def test_delete_removes_credentials(self):
        from classpilot.user_store import UserStore
        store = UserStore()
        user = store.get_or_create_user(self._unique_sub())
        store.save_google_credentials(user.id, refresh_token="token", scopes=["a"])
        store.delete_google_credentials(user.id)
        self.assertIsNone(store.get_google_credentials(user.id))

    def test_delete_nonexistent_credentials_does_not_raise(self):
        from classpilot.user_store import UserStore
        store = UserStore()
        user = store.get_or_create_user(self._unique_sub())
        store.delete_google_credentials(user.id)  # never saved any — must not error

    def test_empty_refresh_token_rejected(self):
        from classpilot.user_store import UserStore
        store = UserStore()
        user = store.get_or_create_user(self._unique_sub())
        with self.assertRaises(ValueError):
            store.save_google_credentials(user.id, refresh_token="", scopes=["a"])

    def test_deleting_user_cascades_to_credentials(self):
        """ON DELETE CASCADE on the foreign key — deleting a user must not
        leave an orphaned credentials row behind."""
        from classpilot.db import get_connection
        from classpilot.user_store import UserStore
        store = UserStore()
        user = store.get_or_create_user(self._unique_sub())
        store.save_google_credentials(user.id, refresh_token="token", scopes=["a"])

        with get_connection() as conn:
            conn.execute("DELETE FROM users WHERE id = %s", (user.id,))

        self.assertIsNone(store.get_google_credentials(user.id))

    def test_credential_record_repr_never_leaks_refresh_token(self):
        from classpilot.user_store import UserStore
        store = UserStore()
        user = store.get_or_create_user(self._unique_sub())
        secret = "1//should-never-appear-in-repr"
        store.save_google_credentials(user.id, refresh_token=secret, scopes=["a"])
        creds = store.get_google_credentials(user.id)
        self.assertNotIn(secret, repr(creds))
        self.assertIn("redacted", repr(creds))


class TestSchemaConstraints(_PostgresTestCase):
    def test_google_sub_is_unique(self):
        from classpilot.db import get_connection
        with get_connection() as conn:
            conn.execute("INSERT INTO users (google_sub) VALUES ('dup-sub-test')")
            with self.assertRaises(Exception):
                conn.execute("INSERT INTO users (google_sub) VALUES ('dup-sub-test')")

    def test_credentials_require_existing_user(self):
        """Foreign key constraint: can't attach credentials to a user_id
        that doesn't exist in the users table."""
        from classpilot.db import get_connection
        with get_connection() as conn:
            with self.assertRaises(Exception):
                conn.execute(
                    "INSERT INTO google_oauth_credentials "
                    "(user_id, encrypted_refresh_token, scopes) "
                    "VALUES ('00000000-0000-0000-0000-000000000000', %s, %s)",
                    (b"fake", ["a"]),
                )


if __name__ == "__main__":
    unittest.main()
