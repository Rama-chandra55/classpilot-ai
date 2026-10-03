"""
MCP Authentication (Phase 4)

ClassPilot's OWN authentication surface, facing MCP clients (Claude,
Cursor, ChatGPT, ...). Strictly separate from Google OAuth:

    MCP client  --[ClassPilot MCP token]-->  ClassPilot
    ClassPilot  --[Google OAuth token]---->  Google Classroom/Drive

An MCP client never sees a Google access or refresh token. It receives a
ClassPilot-issued opaque token, which this module maps back to a
`users.id`; classpilot.google_oauth then resolves that user's own Google
credentials server-side. That indirection is the whole point — it means a
compromised MCP client token grants access only to ClassPilot's 8 tools
for one user, never the user's raw Google credentials.

Why a custom TokenVerifier instead of FastMCP's OAuthProxy:
  Every FastMCP authentication advisory found while building this phase
  targets OAuthProxy specifically — CVE-2025-69196 / GHSA-5h2m-4q8j-pqpj
  (tokens issued for the proxy's base_url rather than bound to the
  requested resource, enabling reuse across MCP servers), CVE-2026-27124
  (missing consent verification in the proxy callback -> confused
  deputy), and AIKIDO-2026-10735 (inbound Authorization header forwarded
  to unrelated downstream servers). The installed FastMCP (4.0.x) is
  above all of those fixed versions, but issuing our own tokens against
  FastMCP's `TokenVerifier` interface avoids that entire component — and
  therefore that entire class of vulnerability — rather than depending on
  it being correctly patched. It also keeps audience binding explicit and
  under our control (see `resource` below).

Token storage: only sha256(token) is persisted (see the mcp_access_tokens
table in classpilot/db.py). A database compromise yields hashes, not
usable bearer tokens. Plaintext tokens exist only in the issuing HTTP
response and in the client's own storage, and are never logged.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastmcp.server.auth import AccessToken, TokenVerifier

from .config import get_config
from .db import get_connection

logger = logging.getLogger(__name__)

ACCESS_TOKEN_PREFIX = "cpat_"   # ClassPilot Access Token
REFRESH_TOKEN_PREFIX = "cprt_"  # ClassPilot Refresh Token

# The single scope ClassPilot's own tools require. Kept deliberately
# separate from Google's scope list (classpilot/classroom_client.py) —
# these are ClassPilot's permissions, not Google's.
MCP_SCOPE_STUDY = "classpilot:study"
DEFAULT_MCP_SCOPES = [MCP_SCOPE_STUDY]


def hash_token(token: str) -> str:
    """sha256 hex of a token — the only form ever written to the DB."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _generate_token(prefix: str) -> str:
    return f"{prefix}{secrets.token_urlsafe(40)}"


# ── Issuance ──────────────────────────────────────────────────────────────────

def issue_token_pair(
    user_id: str,
    client_id: str,
    scopes: Optional[list[str]] = None,
    resource: Optional[str] = None,
) -> dict:
    """
    Issue a fresh ClassPilot MCP access+refresh token pair for `user_id`.

    Returns the OAuth 2.1 token-response dict (plaintext tokens included —
    this is the ONLY place they exist in plaintext server-side; the caller
    must return them straight to the client and never log them).
    """
    config = get_config()
    scopes = scopes or list(DEFAULT_MCP_SCOPES)
    resource = resource or config.mcp_public_base_url

    access_token = _generate_token(ACCESS_TOKEN_PREFIX)
    refresh_token = _generate_token(REFRESH_TOKEN_PREFIX)
    expires_in = config.mcp_access_token_ttl_seconds
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    with get_connection() as conn:
        conn.execute(
            "INSERT INTO mcp_access_tokens "
            "(token_hash, user_id, client_id, scopes, token_type, resource, expires_at) "
            "VALUES (%s, %s, %s, %s, 'access', %s, %s)",
            (hash_token(access_token), user_id, client_id, scopes, resource, expires_at),
        )
        conn.execute(
            "INSERT INTO mcp_access_tokens "
            "(token_hash, user_id, client_id, scopes, token_type, resource, expires_at) "
            "VALUES (%s, %s, %s, %s, 'refresh', %s, NULL)",
            (hash_token(refresh_token), user_id, client_id, scopes, resource),
        )

    logger.info("Issued MCP token pair for user_id=%s client_id=%s", user_id, client_id)
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "scope": " ".join(scopes),
    }


def refresh_token_pair(refresh_token: str, client_id: str) -> Optional[dict]:
    """
    Exchange a valid refresh token for a NEW token pair, rotating the
    refresh token (OAuth 2.1 requires rotation for public clients).
    Returns None if the refresh token is unknown, revoked, or belongs to
    a different client.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT user_id, client_id, scopes, resource FROM mcp_access_tokens "
            "WHERE token_hash = %s AND token_type = 'refresh' AND revoked_at IS NULL",
            (hash_token(refresh_token),),
        ).fetchone()
        if row is None:
            logger.warning("Rejected MCP refresh: unknown or revoked refresh token.")
            return None
        user_id, stored_client_id, scopes, resource = row
        if stored_client_id != client_id:
            logger.warning("Rejected MCP refresh: client_id mismatch.")
            return None
        # Rotate: the presented refresh token is single-use.
        conn.execute(
            "UPDATE mcp_access_tokens SET revoked_at = now() WHERE token_hash = %s",
            (hash_token(refresh_token),),
        )

    return issue_token_pair(str(user_id), client_id, list(scopes), resource)


def revoke_token(token: str) -> bool:
    """
    Revoke a single token (RFC 7009). Returns True if a live token was
    revoked. Revoking a refresh token also revokes every access token
    issued to the same user+client, so a client logout is complete.
    """
    with get_connection() as conn:
        row = conn.execute(
            "UPDATE mcp_access_tokens SET revoked_at = now() "
            "WHERE token_hash = %s AND revoked_at IS NULL "
            "RETURNING user_id, client_id, token_type",
            (hash_token(token),),
        ).fetchone()
        if row is None:
            return False
        user_id, client_id, token_type = row
        if token_type == "refresh":
            conn.execute(
                "UPDATE mcp_access_tokens SET revoked_at = now() "
                "WHERE user_id = %s AND client_id = %s AND revoked_at IS NULL",
                (user_id, client_id),
            )
    return True


def revoke_all_for_user(user_id: str) -> int:
    """Revoke every live MCP token for a user (account-level disconnect)."""
    with get_connection() as conn:
        rows = conn.execute(
            "UPDATE mcp_access_tokens SET revoked_at = now() "
            "WHERE user_id = %s AND revoked_at IS NULL RETURNING token_hash",
            (user_id,),
        ).fetchall()
    return len(rows)


# ── Verification ──────────────────────────────────────────────────────────────

def lookup_access_token(token: str) -> Optional[dict]:
    """
    Resolve a plaintext access token to its record, or None if it is
    unknown, revoked, expired, or not an access token.

    Fails closed on every abnormal condition — there is no branch here
    that returns a usable record for a token we can't fully validate.
    """
    if not token:
        return None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT user_id, client_id, scopes, resource, expires_at, revoked_at "
            "FROM mcp_access_tokens "
            "WHERE token_hash = %s AND token_type = 'access'",
            (hash_token(token),),
        ).fetchone()

    if row is None:
        return None
    user_id, client_id, scopes, resource, expires_at, revoked_at = row
    if revoked_at is not None:
        return None
    if expires_at is not None and datetime.now(timezone.utc) >= expires_at:
        return None

    return {
        "user_id": str(user_id),
        "client_id": client_id,
        "scopes": list(scopes),
        "resource": resource,
        "expires_at": expires_at,
    }


class ClassPilotTokenVerifier(TokenVerifier):
    """
    FastMCP TokenVerifier backed by the mcp_access_tokens table.

    Returns a FastMCP AccessToken whose `subject` is the ClassPilot
    `users.id` — that is what classpilot.identity.resolve_identity()
    reads, per request, to determine who is calling. Returning None
    causes FastMCP to reject the request with a 401 challenge, which is
    the fail-closed path for missing/invalid/expired/revoked tokens.
    """

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            record = lookup_access_token(token)
        except Exception:
            # A storage failure must never be interpreted as "valid".
            logger.exception("MCP token verification failed due to a storage error")
            return None

        if record is None:
            # Deliberately no token value, prefix, or hash in this log line.
            logger.info("Rejected MCP request: invalid, expired, or revoked access token.")
            return None

        expires_at = record["expires_at"]
        return AccessToken(
            token=token,
            client_id=record["client_id"],
            scopes=record["scopes"],
            expires_at=int(expires_at.timestamp()) if expires_at else None,
            resource=record["resource"],
            subject=record["user_id"],
            claims={"classpilot_user_id": record["user_id"]},
        )
