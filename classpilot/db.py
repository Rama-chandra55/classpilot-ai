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
"""

_pool: "ConnectionPool | None" = None


def get_pool() -> ConnectionPool:
    """Return the process-wide Postgres connection pool, creating it (but
    not necessarily connecting yet — psycopg_pool connects lazily/in the
    background) on first call."""
    global _pool
    if _pool is None:
        dsn = get_config().database_url
        _pool = ConnectionPool(conninfo=dsn, min_size=1, max_size=10, open=True)
        logger.debug("Postgres connection pool created")
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
    """Create the users/google_oauth_credentials tables if they don't
    already exist. Safe to call on every process startup."""
    with get_connection() as conn:
        conn.execute(_SCHEMA)
    logger.debug("Postgres schema ready")
