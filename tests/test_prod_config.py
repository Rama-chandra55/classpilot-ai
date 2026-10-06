import os
import unittest
from unittest.mock import patch

from classpilot.config import ClassPilotConfig

class TestProdConfig(unittest.TestCase):

    def setUp(self):
        # Provide base valid config environment so we can test one aspect failing at a time.
        self.base_env = {
            "K_SERVICE": "1",
            "DATABASE_URL": "postgresql://mock:mock@mock/mock",
            "TOKEN_ENCRYPTION_KEY": "mock_key",
            "LLM_API_KEY": "mock_key",
            "MCP_AUTH_MODE": "remote",
        }

    def test_cloud_run_requires_database_url(self):
        with patch.dict(os.environ, self.base_env, clear=True):
            os.environ["DATABASE_URL"] = ""
            cfg = ClassPilotConfig()
            with self.assertRaisesRegex(ValueError, "DATABASE_URL must be explicitly set"):
                cfg.validate()

    def test_cloud_run_requires_token_encryption_key(self):
        with patch.dict(os.environ, self.base_env, clear=True):
            del os.environ["TOKEN_ENCRYPTION_KEY"]
            cfg = ClassPilotConfig()
            with self.assertRaisesRegex(ValueError, "TOKEN_ENCRYPTION_KEY is strictly required"):
                cfg.validate()

    def test_cloud_run_rejects_mcp_auth_mode_local_dev(self):
        with patch.dict(os.environ, self.base_env, clear=True):
            os.environ["MCP_AUTH_MODE"] = "local_dev"
            cfg = ClassPilotConfig()
            with self.assertRaisesRegex(ValueError, "MCP_AUTH_MODE must be 'remote'"):
                cfg.validate()

    def test_cloud_run_rejects_classpilot_dev_user_id(self):
        with patch.dict(os.environ, self.base_env, clear=True):
            os.environ["CLASSPILOT_DEV_USER_ID"] = "mock-uuid"
            cfg = ClassPilotConfig()
            with self.assertRaisesRegex(ValueError, "CLASSPILOT_DEV_USER_ID is not allowed"):
                cfg.validate()

    def test_db_pool_min_size_parses_correctly(self):
        with patch.dict(os.environ, {"DB_POOL_MIN_SIZE": "2"}, clear=True):
            cfg = ClassPilotConfig()
            self.assertEqual(cfg.db_pool_min_size, 2)

    def test_db_pool_max_size_parses_correctly(self):
        with patch.dict(os.environ, {"DB_POOL_MAX_SIZE": "8"}, clear=True):
            cfg = ClassPilotConfig()
            self.assertEqual(cfg.db_pool_max_size, 8)

    def test_pool_min_0_accepted(self):
        with patch.dict(os.environ, self.base_env, clear=True):
            os.environ["DB_POOL_MIN_SIZE"] = "0"
            os.environ["DB_POOL_MAX_SIZE"] = "5"
            cfg = ClassPilotConfig()
            cfg.validate()  # Should not raise
            self.assertEqual(cfg.db_pool_min_size, 0)
            self.assertEqual(cfg.db_pool_max_size, 5)

    def test_pool_min_greater_than_max_rejected(self):
        with patch.dict(os.environ, self.base_env, clear=True):
            os.environ["DB_POOL_MIN_SIZE"] = "10"
            os.environ["DB_POOL_MAX_SIZE"] = "5"
            cfg = ClassPilotConfig()
            with self.assertRaisesRegex(ValueError, "cannot be greater than DB_POOL_MAX_SIZE"):
                cfg.validate()

    def test_pool_negative_min_rejected(self):
        with patch.dict(os.environ, self.base_env, clear=True):
            os.environ["DB_POOL_MIN_SIZE"] = "-1"
            cfg = ClassPilotConfig()
            with self.assertRaisesRegex(ValueError, "cannot be negative"):
                cfg.validate()

    def test_pool_max_less_than_one_rejected(self):
        with patch.dict(os.environ, self.base_env, clear=True):
            os.environ["DB_POOL_MAX_SIZE"] = "0"
            cfg = ClassPilotConfig()
            with self.assertRaisesRegex(ValueError, "must be at least 1"):
                cfg.validate()

    def test_port_takes_precedence_over_mcp_http_port(self):
        with patch.dict(os.environ, {"PORT": "8080", "MCP_HTTP_PORT": "9000"}, clear=True):
            cfg = ClassPilotConfig()
            self.assertEqual(cfg.mcp_http_port, 8080)
            # Should bind to 0.0.0.0 because PORT is present
            self.assertEqual(cfg.mcp_http_host, "0.0.0.0")

    def test_google_web_credentials_path_defaults_and_override(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = ClassPilotConfig()
            self.assertEqual(cfg.google_web_credentials_path, "google_web_credentials.json")

        with patch.dict(os.environ, {"GOOGLE_WEB_CREDENTIALS_PATH": "/secrets/google_web_credentials.json"}, clear=True):
            cfg = ClassPilotConfig()
            self.assertEqual(cfg.google_web_credentials_path, "/secrets/google_web_credentials.json")

if __name__ == "__main__":
    unittest.main(verbosity=2)
