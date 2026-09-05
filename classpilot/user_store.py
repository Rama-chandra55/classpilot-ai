"""
User Store

CRUD for the multi-user foundation: `users` (identity, keyed on Google's
stable `sub` claim) and `google_oauth_credentials` (one encrypted Google
refresh token per user).

This module is the storage substrate a later phase's web OAuth flow will
write to and read from — it is not yet wired into the live single-user
auth path (vendor auth.py / token.json), which keeps working unchanged.

Design notes:
  - Only the refresh_token is persisted. Access tokens are short-lived
    (~1hr) and cheap to re-derive from the refresh token on demand, so
    there's no reason to give them a standing home in the database where
    they'd be one more thing that could leak.
  - `google_sub`, not email, is the durable identity key — see the schema
    comment in classpilot/db.py. Email is stored for display/debugging
    only and must never be used to look up a user's credentials.
  - Key rotation (multiple active TOKEN_ENCRYPTION_KEYs, a stored
    key_version per row) is intentionally out of scope for Phase 1. If/when
    that's needed: add a `key_version SMALLINT` column defaulting to the
    current key's version, and have crypto.py try the version-appropriate
    key on decrypt. Not built now because there is exactly one key and one
    environment to worry about at this stage — adding the machinery early
    would be speculative complexity with nothing yet to validate it against.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .crypto import encrypt_token, decrypt_token
from .db import get_connection

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UserRecord:
    id: str  # UUID, as text
    google_sub: str
    email: Optional[str]
    display_name: Optional[str]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class GoogleCredentialRecord:
    user_id: str
    refresh_token: str          # decrypted — never logged, never repr'd by default below
    scopes: list[str]
    token_expiry: Optional[datetime]
    updated_at: datetime

    def __repr__(self) -> str:  # pragma: no cover - defensive, not exercised by assertions
        # Deliberately do not include refresh_token, even in debug output —
        # a stray `print(record)` or log line must never leak the secret.
        return (
            f"GoogleCredentialRecord(user_id={self.user_id!r}, "
            f"refresh_token=<redacted>, scopes={self.scopes!r}, "
            f"token_expiry={self.token_expiry!r})"
        )


def _row_to_user(row) -> UserRecord:
    return UserRecord(
        id=str(row[0]), google_sub=row[1], email=row[2], display_name=row[3],
        created_at=row[4], updated_at=row[5],
    )


class UserStore:
    """Postgres-backed CRUD for users and their Google OAuth credentials."""

    def get_or_create_user(
        self,
        google_sub: str,
        email: Optional[str] = None,
        display_name: Optional[str] = None,
    ) -> UserRecord:
        """
        Look up a user by their Google `sub` claim, creating them if this
        is the first time we've seen them. If they already exist and a new
        email/display_name is provided, refresh those fields (people do
        change their display name; email changes are rarer but possible).
        """
        if not google_sub:
            raise ValueError("google_sub is required and cannot be empty.")

        with get_connection() as conn:
            row = conn.execute(
                """
                INSERT INTO users (google_sub, email, display_name)
                VALUES (%s, %s, %s)
                ON CONFLICT (google_sub) DO UPDATE SET
                    email        = COALESCE(EXCLUDED.email, users.email),
                    display_name = COALESCE(EXCLUDED.display_name, users.display_name),
                    updated_at   = now()
                RETURNING id, google_sub, email, display_name, created_at, updated_at
                """,
                (google_sub, email, display_name),
            ).fetchone()
        return _row_to_user(row)

    def get_user_by_google_sub(self, google_sub: str) -> Optional[UserRecord]:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT id, google_sub, email, display_name, created_at, updated_at "
                "FROM users WHERE google_sub = %s",
                (google_sub,),
            ).fetchone()
        return _row_to_user(row) if row else None

    def get_user_by_id(self, user_id: str) -> Optional[UserRecord]:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT id, google_sub, email, display_name, created_at, updated_at "
                "FROM users WHERE id = %s",
                (user_id,),
            ).fetchone()
        return _row_to_user(row) if row else None

    # ---------- Google OAuth credentials ----------

    def save_google_credentials(
        self,
        user_id: str,
        refresh_token: str,
        scopes: list[str],
        token_expiry: Optional[datetime] = None,
    ) -> None:
        """Encrypt and store (or replace) a user's Google refresh token."""
        if not refresh_token:
            raise ValueError("refresh_token is required and cannot be empty.")
        encrypted = encrypt_token(refresh_token)
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO google_oauth_credentials
                    (user_id, encrypted_refresh_token, scopes, token_expiry)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    encrypted_refresh_token = EXCLUDED.encrypted_refresh_token,
                    scopes                  = EXCLUDED.scopes,
                    token_expiry            = EXCLUDED.token_expiry,
                    updated_at              = now()
                """,
                (user_id, encrypted, scopes, token_expiry),
            )
        logger.info("Stored Google credentials for user_id=%s (token value not logged)", user_id)

    def get_google_credentials(self, user_id: str) -> Optional[GoogleCredentialRecord]:
        """Fetch and decrypt a user's stored Google refresh token."""
        with get_connection() as conn:
            row = conn.execute(
                "SELECT user_id, encrypted_refresh_token, scopes, token_expiry, updated_at "
                "FROM google_oauth_credentials WHERE user_id = %s",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        refresh_token = decrypt_token(bytes(row[1]))
        return GoogleCredentialRecord(
            user_id=str(row[0]), refresh_token=refresh_token, scopes=list(row[2]),
            token_expiry=row[3], updated_at=row[4],
        )

    def delete_google_credentials(self, user_id: str) -> None:
        """
        Remove a user's stored Google credentials (revocation primitive).

        Does not call Google's token-revocation endpoint — that's a
        network call belonging to the OAuth flow layer built in a later
        phase. This only removes ClassPilot's own stored copy.
        """
        with get_connection() as conn:
            conn.execute(
                "DELETE FROM google_oauth_credentials WHERE user_id = %s",
                (user_id,),
            )
        logger.info("Deleted stored Google credentials for user_id=%s", user_id)
