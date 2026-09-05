"""
tests/test_crypto.py

Tests for classpilot/crypto.py — Fernet-based encryption for stored Google
refresh tokens. No database or network needed; only classpilot.config's
TOKEN_ENCRYPTION_KEY setting matters, set/unset per-test via monkeypatch.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cryptography.fernet import Fernet

import classpilot.crypto as crypto_mod
from classpilot.crypto import encrypt_token, decrypt_token, TokenEncryptionError
from classpilot.config import ClassPilotConfig


def _config_with_key(key: str) -> ClassPilotConfig:
    cfg = ClassPilotConfig()
    cfg.token_encryption_key = key
    return cfg


class TestEncryptDecryptRoundtrip(unittest.TestCase):
    def setUp(self):
        self.key = Fernet.generate_key().decode()
        self.patcher = patch("classpilot.crypto.get_config", return_value=_config_with_key(self.key))
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_roundtrip_preserves_plaintext(self):
        secret = "1//0gAbCdEfGhIjKlMnOpQrStUvWxYz-fake-refresh-token"
        ciphertext = encrypt_token(secret)
        self.assertEqual(decrypt_token(ciphertext), secret)

    def test_ciphertext_is_not_plaintext(self):
        secret = "1//fake-refresh-token"
        ciphertext = encrypt_token(secret)
        self.assertNotIn(secret.encode(), ciphertext)

    def test_ciphertext_is_bytes(self):
        ciphertext = encrypt_token("some-token")
        self.assertIsInstance(ciphertext, bytes)

    def test_empty_string_roundtrips(self):
        ciphertext = encrypt_token("")
        self.assertEqual(decrypt_token(ciphertext), "")

    def test_unicode_content_roundtrips(self):
        secret = "token-with-ünïcödé-🔒"
        self.assertEqual(decrypt_token(encrypt_token(secret)), secret)

    def test_same_plaintext_produces_different_ciphertext(self):
        """Fernet includes a random IV/nonce — encrypting the same secret
        twice must not produce identical ciphertext (which would leak
        equality information about stored tokens)."""
        secret = "1//same-token"
        self.assertNotEqual(encrypt_token(secret), encrypt_token(secret))

    def test_wrong_key_cannot_decrypt(self):
        secret = "1//fake-refresh-token"
        ciphertext = encrypt_token(secret)
        other_key = Fernet.generate_key().decode()
        with patch("classpilot.crypto.get_config", return_value=_config_with_key(other_key)):
            with self.assertRaises(TokenEncryptionError):
                decrypt_token(ciphertext)

    def test_corrupted_ciphertext_raises_not_crashes(self):
        ciphertext = encrypt_token("1//fake-refresh-token")
        corrupted = ciphertext[:-4] + b"xxxx"
        with self.assertRaises(TokenEncryptionError):
            decrypt_token(corrupted)

    def test_encrypt_none_raises_value_error(self):
        with self.assertRaises(ValueError):
            encrypt_token(None)


class TestMissingOrInvalidKey(unittest.TestCase):
    def test_missing_key_raises_clear_error_on_encrypt(self):
        with patch("classpilot.crypto.get_config", return_value=_config_with_key("")):
            with self.assertRaises(TokenEncryptionError) as ctx:
                encrypt_token("secret")
            self.assertIn("TOKEN_ENCRYPTION_KEY", str(ctx.exception))

    def test_missing_key_raises_clear_error_on_decrypt(self):
        with patch("classpilot.crypto.get_config", return_value=_config_with_key("")):
            with self.assertRaises(TokenEncryptionError):
                decrypt_token(b"whatever")

    def test_malformed_key_raises_clear_error(self):
        with patch("classpilot.crypto.get_config", return_value=_config_with_key("not-a-valid-fernet-key")):
            with self.assertRaises(TokenEncryptionError) as ctx:
                encrypt_token("secret")
            self.assertIn("not a valid Fernet key", str(ctx.exception))

    def test_error_message_never_contains_the_actual_key(self):
        """The exception message helps the operator fix the config — it
        must never echo the (invalid) key value itself back out, since
        that could still be a real secret typed in wrong."""
        bad_key = "sk-totally-not-fernet-but-secretlooking-12345"
        with patch("classpilot.crypto.get_config", return_value=_config_with_key(bad_key)):
            with self.assertRaises(TokenEncryptionError) as ctx:
                encrypt_token("secret")
            self.assertNotIn(bad_key, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
