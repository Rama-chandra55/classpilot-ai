"""
Identity

The internal abstraction for "who is calling this tool right now" — kept
strictly separate from Google credentials (classpilot.google_oauth) and
from MCP tool signatures.

Why this exists: every MCP tool needs to know which student it's acting
on behalf of, but user_id must never be a caller-controlled tool
argument — it's a security identity, not a preference. resolve_identity()
is the single seam every tool calls to find out.

Why it's a pure function, not a global: the "current user" is not a
process-wide fact. resolve_identity() takes nothing and returns a fresh,
immutable RequestIdentity on every call, so:
  - concurrent calls (any number of threads/requests, from different
    users) never share or race over identity state — nothing is written
    anywhere,
  - the resolution SOURCE can change (Phase 3: one configured dev user;
    Phase 4: the authenticated MCP request) without touching a single
    call site or tool signature.

Phase 4 implementation: identity comes from the authenticated MCP request
context. FastMCP's `get_access_token()` reads the verified token off the
CURRENT HTTP request scope (falling back to a ContextVar) — i.e. it is
request-scoped by construction, never a module-level mutable global, so
concurrent requests from different users cannot observe each other's
identity. The token's `subject` is the PostgreSQL `users.id`, set by
classpilot.mcp_auth.ClassPilotTokenVerifier after validating the token
against the mcp_access_tokens table.

Fail-closed: in the default "remote" auth mode, an unauthenticated
request raises IdentityResolutionError. CLASSPILOT_DEV_USER_ID is
consulted ONLY when MCP_AUTH_MODE=local_dev is explicitly set — it is
ignored entirely otherwise, so forgetting to change a setting denies
access rather than silently granting everyone the dev user's account.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import get_config

logger = logging.getLogger(__name__)

# Imported once, at module load, and referenced indirectly so tests can
# patch `classpilot.identity._get_access_token` without having to fight
# other test files' `fastmcp` stubs. This is a function REFERENCE, not
# identity state — the per-request value it returns is read fresh on
# every call (FastMCP backs it with the HTTP request scope / a
# ContextVar), so nothing here is a shared mutable identity global.
try:
    from fastmcp.server.dependencies import get_access_token as _get_access_token
except Exception:  # pragma: no cover - fastmcp is always installed in practice
    _get_access_token = None


class IdentityResolutionError(Exception):
    """Raised when no usable identity can be resolved for this request —
    an unauthenticated remote request, or local_dev mode without
    CLASSPILOT_DEV_USER_ID set. Tools should let this surface as a clear,
    sanitized error rather than falling back to any other identity source
    (never token.json, never another user)."""


@dataclass(frozen=True)
class RequestIdentity:
    """
    An immutable snapshot of "who is making this call". `user_id` is the
    PostgreSQL `users.id` (UUID, as text) that classpilot.user_store /
    classpilot.google_oauth key everything on.

    `source` is purely diagnostic (for logging/tests) — never branch
    application logic on it outside of this module.
    """
    user_id: str
    source: str  # "mcp_session" (authenticated) | "local_dev" (explicit dev mode)


def _identity_from_mcp_request() -> "RequestIdentity | None":
    """
    Read the authenticated identity off the CURRENT MCP request, or None
    if this call isn't running inside an authenticated request.

    Isolated into its own function so the request-scoped lookup is
    testable and so resolve_identity() below stays a readable policy
    decision rather than a mix of policy and plumbing.
    """
    if _get_access_token is None:  # pragma: no cover - fastmcp always present
        return None

    try:
        access_token = _get_access_token()
    except Exception:
        # No active request context (e.g. called outside a tool invocation).
        return None

    if access_token is None:
        return None

    user_id = getattr(access_token, "subject", None)
    if not user_id:
        # A verified token that somehow carries no subject is unusable —
        # treat as unauthenticated rather than guessing.
        logger.warning("Authenticated MCP request carried no subject; rejecting.")
        return None

    return RequestIdentity(user_id=user_id, source="mcp_session")


def resolve_identity() -> RequestIdentity:
    """
    Resolve the identity for the current call.

    Order of resolution:
      1. The authenticated MCP request context (always preferred).
      2. ONLY if MCP_AUTH_MODE=local_dev: CLASSPILOT_DEV_USER_ID.
      3. Otherwise: raise. Never falls back to another identity source.

    Takes no arguments — identity can never be supplied or influenced by
    a caller, and no MCP tool exposes it as a parameter.
    """
    identity = _identity_from_mcp_request()
    if identity is not None:
        return identity

    config = get_config()
    if config.is_local_dev_auth:
        user_id = config.classpilot_dev_user_id
        if not user_id:
            raise IdentityResolutionError(
                "MCP_AUTH_MODE=local_dev is set but CLASSPILOT_DEV_USER_ID is "
                "empty. Set it to the PostgreSQL user id of an account "
                "connected via the web OAuth flow (classpilot-ai-oauth)."
            )
        return RequestIdentity(user_id=user_id, source="local_dev")

    raise IdentityResolutionError(
        "This request is not authenticated. Connect your Google account to "
        "ClassPilot and authorize this MCP client, then try again."
    )
