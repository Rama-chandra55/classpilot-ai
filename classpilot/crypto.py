"""
Token Encryption

Encrypts/decrypts Google OAuth refresh tokens before they touch the
database, and decrypts them on the way back out. This is the single
highest-value security control in the multi-user persistence foundation:
a leaked plaintext refresh_token is a standing, silent path into a
student's entire Drive and Classroom, so it must never be written to disk
(or a DB backup, or a log line) unencrypted.

Uses Fernet (AES-128-CBC + HMAC, from the `cryptography` package) — a
well-audited, deliberately simple authenticated-encryption scheme that's
the right level of complexity for "encrypt one secret string before a DB
write, decrypt it after a DB read." It is not a general-purpose crypto
toolkit, and that's the point: less surface area to get wrong.

Key management (Phase 1 scope):
  - One active key, read from the TOKEN_ENCRYPTION_KEY environment
    variable (see classpilot/config.py). Generate one with:
        python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  - The key is never logged, never has a default value, and is validated
    lazily (only when encrypt/decrypt is actually called) so importing
    this module, or running any code path that doesn't touch encrypted
    credentials, never requires it to be set.
  - Key rotation is intentionally out of scope for Phase 1 — see the
    module docstring in classpilot/user_store.py for the planned upgrade
    path (multiple key support, keyed by a stored key_version column).
"""

from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken

from .config import get_config

logger = logging.getLogger(__name__)


class TokenEncryptionError(Exception):
    """Raised when TOKEN_ENCRYPTION_KEY is missing/malformed, or a stored
    ciphertext can't be decrypted (wrong key, corrupted data, or tampering)."""


def _get_fernet() -> Fernet:
    key = get_config().token_encryption_key
    if not key:
        raise TokenEncryptionError(
            "TOKEN_ENCRYPTION_KEY is not set. Generate one with:\n"
            '  python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"\n'
            "and add it to your .env file. This key encrypts stored Google "
            "refresh tokens — do not commit it, and never reuse it across "
            "environments (dev/staging/prod should each have their own)."
        )
    try:
        return Fernet(key.encode("utf-8") if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise TokenEncryptionError(
            f"TOKEN_ENCRYPTION_KEY is not a valid Fernet key: {exc}. "
            "It must be the exact 44-character urlsafe-base64 string "
            "produced by Fernet.generate_key()."
        ) from exc


def encrypt_token(plaintext: str) -> bytes:
    """
    Encrypt a secret string (a Google refresh_token) for storage.

    Returns opaque ciphertext bytes safe to store in a BYTEA/bytea column.
    Raises TokenEncryptionError if no valid key is configured.
    """
    if plaintext is None:
        raise ValueError("Cannot encrypt None — pass an empty string explicitly if intended.")
    fernet = _get_fernet()
    return fernet.encrypt(plaintext.encode("utf-8"))


def decrypt_token(ciphertext: bytes) -> str:
    """
    Decrypt a value previously produced by encrypt_token().

    Raises TokenEncryptionError if no valid key is configured, or if the
    ciphertext can't be decrypted with the currently-configured key (wrong
    key, corrupted row, or tampering — Fernet's HMAC step means these are
    indistinguishable, which is the correct, safe behavior).
    """
    fernet = _get_fernet()
    try:
        return fernet.decrypt(bytes(ciphertext)).decode("utf-8")
    except InvalidToken as exc:
        logger.error("Failed to decrypt a stored token — wrong key or corrupted/tampered data.")
        raise TokenEncryptionError(
            "Could not decrypt the stored value with the configured "
            "TOKEN_ENCRYPTION_KEY. Either the key has changed since this "
            "value was encrypted, or the stored data is corrupted."
        ) from exc
