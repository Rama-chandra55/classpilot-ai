"""
Database

Postgres connection pooling and schema management for the multi-user
persistence foundation (users + encrypted Google OAuth credentials).

Deliberately NOT an ORM/migration framework — this mirrors the existing
codebase's style (see classpilot/state_store.py's raw-SQL, idempotent
`CREATE TABLE IF NOT EXISTS` approach) rather than introducing SQLAlchemy
or Alembic for what is currently two tables. If the schema grows
significantly in a later phase, revisit that decision — this module's
`init_schema()` is intentionally the only place schema lives, so swapping
it for a real migration tool later is a contained change.

The pool is created lazily on first use, not at import time, so importing
this module (or anything that imports it) never requires a reachable
Postgres server — only code paths that actually call get_connection()
(classpilot/user_store.py) do.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg_pool import ConnectionPool

from .config import get_config

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    google_sub   TEXT UNIQUE NOT NULL,   -- Google's stable 'sub' claim — the
                                         -- durable identity key (never email:
                                         -- a student's email can change,
                                         -- google_sub does not)
    email        TEXT,
    display_name TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS google_oauth_credentials (
    user_id                 UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    encrypted_refresh_token BYTEA NOT NULL,   -- Fernet ciphertext; see classpilot/crypto.py.
                                              -- Never store the refresh token in plaintext.
    scopes                  TEXT[] NOT NULL,
    token_expiry            TIMESTAMPTZ,       -- last known access-token expiry (informational;
                                              -- access tokens are re-derived on demand, not stored)
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Phase 2: short-lived, single-use CSRF protection for the web OAuth flow,
-- AND the PKCE code_verifier that must accompany it (google_auth_oauthlib's
-- Flow object generates a fresh code_verifier per instance, and /login and
-- /callback are two separate Flow instances across two separate HTTP
-- requests — see classpilot/oauth_state.py's module docstring for why this
-- has to be persisted here rather than kept in memory). A row is created
-- when /auth/google/login issues an authorization URL, and deleted the
-- moment /auth/google/callback consumes it (or once expired) — see
-- classpilot/oauth_state.py. Postgres-backed rather than an in-memory
-- dict so the state survives a process restart between the two legs of
-- the flow, and so a horizontally-scaled deployment (later phase) doesn't
-- need sticky sessions for this to work.
CREATE TABLE IF NOT EXISTS oauth_states (
    state         TEXT PRIMARY KEY,
    code_verifier TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Phase 4: ClassPilot-issued MCP access/refresh tokens.
--
-- These are ClassPilot's OWN tokens, issued to MCP clients (Claude,
-- Cursor, ChatGPT, ...) — entirely separate from the Google tokens in
-- google_oauth_credentials. An MCP client NEVER receives a Google
-- access or refresh token; it receives one of these, which ClassPilot
-- exchanges internally for that user's Google credentials on each call.
--
-- Only a SHA-256 hash of each token is stored, never the token itself:
-- a database compromise must not yield usable bearer tokens. Lookup is
-- by hash, so the plaintext only ever exists in the issuing response
-- and in the client's own storage.
CREATE TABLE IF NOT EXISTS mcp_access_tokens (
    token_hash    TEXT PRIMARY KEY,           -- sha256(token), hex
    user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_id     TEXT NOT NULL,
    scopes        TEXT[] NOT NULL DEFAULT '{}',
    token_type    TEXT NOT NULL DEFAULT 'access',  -- 'access' | 'refresh'
    resource      TEXT,                       -- RFC 8707 audience binding
    expires_at    TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS mcp_access_tokens_user_id_idx
    ON mcp_access_tokens (user_id);

-- Phase 4: short-lived OAuth 2.1 authorization codes for the MCP-facing
-- flow (PKCE-protected, single-use). Separate from oauth_states above,
-- which belongs to the GOOGLE-facing flow.
CREATE TABLE IF NOT EXISTS mcp_auth_codes (
    code                  TEXT PRIMARY KEY,
    user_id               UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_id             TEXT NOT NULL,
    redirect_uri          TEXT NOT NULL,
    code_challenge        TEXT NOT NULL,
    code_challenge_method TEXT NOT NULL DEFAULT 'S256',
    scopes                TEXT[] NOT NULL DEFAULT '{}',
    resource              TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Phase 5A: Persistent State Migration
-- 
-- Moved from SQLite (state_store.py) to PostgreSQL.
-- Tracks known assignments and sent reminders to deduplicate notifications.
CREATE TABLE IF NOT EXISTS known_assignments (
    user_id         TEXT NOT NULL DEFAULT 'default',
    course_id       TEXT NOT NULL,
    assignment_id   TEXT NOT NULL,
    title           TEXT,
    due_date_json   TEXT,
    due_time_json   TEXT,
    first_seen_at   TIMESTAMPTZ DEFAULT now(),
    last_updated_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (user_id, course_id, assignment_id)
);

CREATE TABLE IF NOT EXISTS sent_reminders (
    user_id         TEXT NOT NULL DEFAULT 'default',
    course_id       TEXT NOT NULL,
    assignment_id   TEXT NOT NULL,
    offset_minutes  INTEGER NOT NULL,
    sent_at         TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (user_id, course_id, assignment_id, offset_minutes)
);

CREATE TABLE IF NOT EXISTS watcher_configs (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    enabled BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_pool: "ConnectionPool | None" = None


def get_pool() -> ConnectionPool:
    """Return the process-wide Postgres connection pool, creating it (but
    not necessarily connecting yet — psycopg_pool connects lazily/in the
    background) on first call."""
    global _pool
    if _pool is None:
        cfg = get_config()
        dsn = cfg.database_url
        _pool = ConnectionPool(
            conninfo=dsn,
            min_size=cfg.db_pool_min_size,
            max_size=cfg.db_pool_max_size,
            open=True
        )
        logger.debug("Postgres connection pool created (min=%s, max=%s)", cfg.db_pool_min_size, cfg.db_pool_max_size)
    return _pool


def close_pool() -> None:
    """Close the pool. Mainly for clean test teardown and graceful shutdown."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def get_connection() -> Iterator[psycopg.Connection]:
    """
    Borrow a connection from the pool for a single unit of work.

    Commits on success, rolls back on exception — the same commit/rollback
    contract classpilot/state_store.py's `_connect()` already uses, kept
    consistent here deliberately.
    """
    pool = get_pool()
    with pool.connection() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def init_schema() -> None:
    """Create the users/google_oauth_credentials/oauth_states tables if
    they don't already exist, and migrate an existing oauth_states table
    that predates the code_verifier column (Phase 2's PKCE fix). Safe to
    call on every process startup."""
    with get_connection() as conn:
        conn.execute(_SCHEMA)
        _migrate_add_code_verifier_column(conn)
    logger.debug("Postgres schema ready")


def _migrate_add_code_verifier_column(conn: psycopg.Connection) -> None:
    """
    Add oauth_states.code_verifier for a database created by the original
    Phase 2 schema (state, created_at only — before the PKCE fix added
    code_verifier). Since oauth_states rows are short-lived (single-use,
    ~10 minute expiry) and hold no durable identity or credential data,
    any pre-existing row is safe to simply drop rather than back-fill —
    it represents an in-flight login attempt from before this migration
    ran, which would fail PKCE verification anyway (that's the very bug
    being fixed) and will just need the student to click "Sign in with
    Google" again.
    """
    exists = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'oauth_states' AND column_name = 'code_verifier'"
    ).fetchone()
    if exists is None:
        conn.execute("DELETE FROM oauth_states")  # see docstring — safe to discard
        conn.execute("ALTER TABLE oauth_states ADD COLUMN code_verifier TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE oauth_states ALTER COLUMN code_verifier DROP DEFAULT")
        logger.info("Migrated existing 'oauth_states' table: added code_verifier column")
