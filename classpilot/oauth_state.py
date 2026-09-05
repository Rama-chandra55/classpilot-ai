"""
OAuth State Store

Single-use, expiring CSRF protection for classpilot/oauth_web.py's Google
web OAuth flow — AND the PKCE code_verifier lifecycle that must accompany
it. A `state` value (and the code_verifier generated alongside it) is
created when /auth/google/login issues an authorization URL, embedded in
that URL, and must come back unchanged on /auth/google/callback — proving
the callback really followed from a login this server initiated, not a
forged/replayed request, and letting the token exchange complete PKCE
correctly.

Why code_verifier lives here too: google_auth_oauthlib's Flow object
generates a fresh, random code_verifier every time authorization_url() is
called, storing it only as an attribute on that specific Flow *instance*.
The /login and /callback legs of this flow are two separate HTTP requests
handled by two separate Flow objects, so the verifier from /login is gone
by the time /callback needs it — unless something persists it in between.
A process-global variable would work for a single dev process but breaks
the moment there's more than one worker process (a later-phase deployment
concern) and doesn't survive a restart in the (possibly minutes-long)
window while a student is on Google's consent screen. Storing it in the
same Postgres-backed, single-use, expiring row as the CSRF state avoids
both problems and keeps the two pieces of "this login attempt's secret
state" atomic with each other — one row, one lifecycle, one deletion.

Postgres-backed (not an in-memory dict): the interval between a student
clicking "Sign in with Google" and Google redirecting back can be minutes
(they might get distracted mid-consent-screen), and this needs to survive
a process restart in that window, and to work correctly if a later phase
runs more than one server process. See the oauth_states table comment in
classpilot/db.py for the same reasoning.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from .db import get_connection

logger = logging.getLogger(__name__)

DEFAULT_STATE_TTL_SECONDS = 600  # 10 minutes — generous for a human to complete consent


def generate_state() -> str:
    """
    Generate a new CSRF state token value. Pure — does not touch the
    database. Callers must build the authorization URL with this value
    (which also causes the Flow to generate its code_verifier) *before*
    calling register_state(), since the verifier isn't known until after
    authorization_url() has run.
    """
    return secrets.token_urlsafe(32)


def register_state(state: str, code_verifier: str) -> None:
    """
    Persist a (state, code_verifier) pair for later, single-use retrieval
    by consume_state(). Called after build_authorization_url() has already
    embedded `state` in the URL and produced its matching `code_verifier`.
    """
    if not state or not code_verifier:
        raise ValueError("Both state and code_verifier are required and cannot be empty.")
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO oauth_states (state, code_verifier) VALUES (%s, %s)",
            (state, code_verifier),
        )


def consume_state(state: str, ttl_seconds: int = DEFAULT_STATE_TTL_SECONDS) -> Optional[str]:
    """
    Validate and immediately invalidate a state token, returning its
    associated code_verifier on success.

    Returns the code_verifier string only if `state` was a real,
    not-yet-consumed, not-expired token this server issued. The row is
    deleted unconditionally the moment it's looked up — whether it turns
    out to be valid or expired — so a state can never be consumed twice
    and an expired state can never later become "valid" simply because
    some future caller happens to check it with a larger ttl_seconds.

    Returns None for anything else (missing, already used, expired, or
    forged): the caller (the callback route) must treat that as a hard
    rejection, not a retry, and must not attempt a token exchange at all
    (there is no code_verifier to use).
    """
    if not state:
        return None
    with get_connection() as conn:
        row = conn.execute(
            "DELETE FROM oauth_states WHERE state = %s RETURNING created_at, code_verifier",
            (state,),
        ).fetchone()
    if row is None:
        logger.warning("Rejected OAuth callback: state token missing or already used.")
        return None
    created_at, code_verifier = row
    if datetime.now(timezone.utc) - created_at > timedelta(seconds=ttl_seconds):
        logger.warning("Rejected OAuth callback: state token expired.")
        return None
    return code_verifier


def purge_expired_states(ttl_seconds: int = DEFAULT_STATE_TTL_SECONDS) -> int:
    """
    Delete any lingering state rows that were never consumed at all (e.g. a
    student who abandoned the consent screen) — consume_state() already
    deletes a row the moment it's looked up (valid or expired), so this
    only ever finds rows nobody ever tried to consume. Just housekeeping,
    not required for correctness. Safe to call periodically or on process
    startup; returns the number of rows removed.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "DELETE FROM oauth_states WHERE created_at <= now() - %s RETURNING state",
            (timedelta(seconds=ttl_seconds),),
        ).fetchall()
    return len(rows)
