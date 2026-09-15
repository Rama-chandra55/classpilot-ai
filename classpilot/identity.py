"""
Identity

The internal abstraction for "who is calling this tool right now" — kept
strictly separate from Google credentials (classpilot.google_oauth) and
from MCP tool signatures.

Why this exists (Phase 3): every MCP tool needs to know which student it's
acting on behalf of, but user_id must never be a caller-controlled tool
argument — it's a security identity, not a preference. resolve_identity()
is the single seam every tool calls to find out.

Why it's a pure function, not a global (Phase 3 requirement): the
"current user" is not a process-wide fact — even today, with a single
configured dev user, treating it as global mutable state would be the
wrong shape to build on. resolve_identity() takes nothing and returns a
fresh, immutable RequestIdentity on every call, so:
  - concurrent calls (any number of threads/requests) never share or race
    over identity state — nothing is written anywhere,
  - Phase 4 can replace this function's *body* (resolve from an
    authenticated MCP session/request context instead of one configured
    value) without touching a single call site or tool signature.

Phase 3 implementation: reads CLASSPILOT_DEV_USER_ID from config. This is
intentionally a real, PostgreSQL-backed user_id (a UUID from the `users`
table, established via the Phase 2 web OAuth flow) — not a sentinel like
state_store.DEFAULT_USER_ID, which is a separate, SQLite-only concept for
watcher dedup state and is unrelated to Google credential identity.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import get_config


class IdentityResolutionError(Exception):
    """Raised when no usable identity can be resolved for this request —
    e.g. CLASSPILOT_DEV_USER_ID is unset. Tools should let this surface as
    a clear, sanitized error rather than falling back to any other
    identity source (see module docstring: never silently fall back to
    token.json for an identified-user request path)."""


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
    source: str  # "dev_config" today; "mcp_session" from Phase 4 onward


def resolve_identity() -> RequestIdentity:
    """
    Resolve the identity for the current call.

    Phase 3: reads the single configured CLASSPILOT_DEV_USER_ID. Raises
    IdentityResolutionError if it isn't set — this must fail loudly, not
    silently hand back an empty/sentinel identity that could accidentally
    resolve to someone else's data or fall through to token.json.

    Phase 4 will replace this function's body to resolve from an
    authenticated MCP request/session context instead. Every caller
    (all 8 MCP tools) is written against this function, not against
    config directly, so that swap touches only this file.
    """
    user_id = get_config().classpilot_dev_user_id
    if not user_id:
        raise IdentityResolutionError(
            "No user identity could be resolved for this request. "
            "Set CLASSPILOT_DEV_USER_ID in your .env to the PostgreSQL "
            "user id of an account you've connected via the web OAuth "
            "flow (classpilot-ai-oauth)."
        )
    return RequestIdentity(user_id=user_id, source="dev_config")
