"""
MCP-facing OAuth 2.1 endpoints (Phase 4)

HTTP routes an MCP client (Claude, Cursor, ChatGPT, ...) uses to obtain a
ClassPilot MCP token:

    GET  /.well-known/oauth-authorization-server   discovery (RFC 8414)
    POST /mcp/register                             dynamic client registration (RFC 7591)
    GET  /mcp/authorize                            start authorization (PKCE required)
    POST /mcp/token                                code -> token, refresh -> token
    POST /mcp/revoke                               revoke a token (RFC 7009)

Relationship to the Google flow in oauth_web.py — these are two distinct
OAuth flows and must not be conflated:

    MCP client --[this flow]--> ClassPilot --[oauth_web.py's flow]--> Google

/mcp/authorize requires that the end user has ALREADY connected their
Google account via the Google flow, because the whole point of the
resulting MCP token is to stand in for that stored Google credential. If
they haven't, this sends them through the Google flow first.

No Google access or refresh token is ever emitted by any route here.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from .config import get_config
from .db import get_connection
from .mcp_auth import (
    DEFAULT_MCP_SCOPES,
    issue_token_pair,
    refresh_token_pair,
    revoke_token,
)
from .user_store import UserStore

logger = logging.getLogger(__name__)

AUTH_CODE_TTL_SECONDS = 300  # 5 minutes (OAuth 2.1 recommends <= 10 min)


def _json_error(error: str, description: str, status: int = 400) -> JSONResponse:
    """RFC 6749 §5.2 error response. Never includes token material."""
    return JSONResponse({"error": error, "error_description": description}, status_code=status)


def _verify_pkce(code_verifier: str, code_challenge: str, method: str) -> bool:
    """
    Validate a PKCE code_verifier against the stored challenge.

    Only S256 is accepted — OAuth 2.1 removes `plain`, and accepting it
    would let a network attacker who intercepts the authorization code
    complete the exchange.
    """
    if method != "S256":
        return False
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(expected, code_challenge)


async def oauth_metadata(request: Request) -> JSONResponse:
    """RFC 8414 authorization server metadata, for MCP client discovery."""
    base = get_config().mcp_public_base_url.rstrip("/")
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/mcp/authorize",
        "token_endpoint": f"{base}/mcp/token",
        "revocation_endpoint": f"{base}/mcp/revoke",
        "registration_endpoint": f"{base}/mcp/register",
        "scopes_supported": list(DEFAULT_MCP_SCOPES),
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],  # OAuth 2.1: no `plain`
        "token_endpoint_auth_methods_supported": ["none"],  # public clients + PKCE
    })


async def mcp_register(request: Request) -> JSONResponse:
    """
    RFC 7591 Dynamic Client Registration.

    Claude's default connector flow (and some other MCP clients) attempt
    DCR before falling back to a manually-configured client_id, so this
    is required for a smooth "paste URL, click Connect" experience even
    though ClassPilot doesn't maintain a meaningful client allow-list —
    every /mcp/authorize and /mcp/token request is scoped by whichever
    client_id is presented, consistently, across that request pair (see
    the client_id-mismatch checks in mcp_token below), which is the
    standard trust model for public, PKCE-only OAuth clients registered
    this way (no client secret is issued — token_endpoint_auth_method is
    "none").
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    client_id = secrets.token_urlsafe(16)
    return JSONResponse(
        {
            "client_id": client_id,
            "client_name": body.get("client_name", "MCP Client"),
            "redirect_uris": body.get("redirect_uris", []),
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        status_code=201,
    )


async def mcp_authorize(request: Request):
    """
    Authorization endpoint. Requires PKCE (S256).

    Instead of trusting an arbitrary login_hint, this initiates a secure
    Google OAuth flow and bridges the MCP request context via the state
    token. When Google redirects back to ClassPilot's callback, ClassPilot
    identifies the user and automatically issues the MCP auth code.
    """
    params = request.query_params
    client_id = params.get("client_id")
    redirect_uri = params.get("redirect_uri")
    code_challenge = params.get("code_challenge")
    method = params.get("code_challenge_method", "")
    state = params.get("state")
    resource = params.get("resource")

    if not client_id or not redirect_uri:
        return _json_error("invalid_request", "client_id and redirect_uri are required.")
    if not code_challenge or method != "S256":
        return _json_error(
            "invalid_request",
            "PKCE is required: supply code_challenge with code_challenge_method=S256.",
        )

    from .oauth_state import generate_state, register_state
    from .google_oauth import build_authorization_url

    state_token = generate_state()
    auth_url, code_verifier = build_authorization_url(state_token)
    
    register_state(
        state_token,
        code_verifier,
        mcp_client_id=client_id,
        mcp_redirect_uri=redirect_uri,
        mcp_code_challenge=code_challenge,
        mcp_code_challenge_method=method,
        mcp_state=state,
        mcp_resource=resource,
    )
    
    return RedirectResponse(auth_url, status_code=302)


async def mcp_token(request: Request) -> JSONResponse:
    """Token endpoint: authorization_code (with PKCE) and refresh_token grants."""
    form = await request.form()
    grant_type = form.get("grant_type")
    client_id = form.get("client_id")

    if not client_id:
        return _json_error("invalid_client", "client_id is required.", status=401)

    if grant_type == "refresh_token":
        presented = form.get("refresh_token")
        if not presented:
            return _json_error("invalid_request", "refresh_token is required.")
        result = refresh_token_pair(presented, client_id)
        if result is None:
            return _json_error("invalid_grant", "Refresh token is invalid or revoked.")
        return JSONResponse(result)

    if grant_type != "authorization_code":
        return _json_error("unsupported_grant_type", f"Unsupported grant_type: {grant_type}")

    code = form.get("code")
    code_verifier = form.get("code_verifier")
    if not code or not code_verifier:
        return _json_error("invalid_request", "code and code_verifier are required.")

    # Single-use: the code row is deleted as it is read, so a replayed
    # code can never be exchanged twice even if intercepted.
    with get_connection() as conn:
        row = conn.execute(
            "DELETE FROM mcp_auth_codes WHERE code = %s "
            "RETURNING user_id, client_id, redirect_uri, code_challenge, "
            "          code_challenge_method, scopes, resource, created_at",
            (code,),
        ).fetchone()

    if row is None:
        return _json_error("invalid_grant", "Authorization code is invalid or already used.")

    (user_id, stored_client_id, _redirect_uri, challenge,
     method, scopes, resource, created_at) = row

    if datetime.now(timezone.utc) - created_at > timedelta(seconds=AUTH_CODE_TTL_SECONDS):
        return _json_error("invalid_grant", "Authorization code has expired.")
    if stored_client_id != client_id:
        logger.warning("Rejected MCP token exchange: client_id mismatch.")
        return _json_error("invalid_grant", "Authorization code was issued to another client.")
    if not _verify_pkce(code_verifier, challenge, method):
        logger.warning("Rejected MCP token exchange: PKCE verification failed.")
        return _json_error("invalid_grant", "PKCE verification failed.")

    return JSONResponse(
        issue_token_pair(str(user_id), client_id, list(scopes), resource)
    )


async def mcp_revoke(request: Request) -> JSONResponse:
    """
    RFC 7009 revocation. Always returns 200 regardless of whether the
    token existed — per the RFC, and so this endpoint can't be used as an
    oracle to probe which tokens are valid.
    """
    form = await request.form()
    token = form.get("token")
    if token:
        revoke_token(token)
    return JSONResponse({}, status_code=200)


routes = [
    Route("/.well-known/oauth-authorization-server", oauth_metadata, methods=["GET"]),
    Route("/mcp/register", mcp_register, methods=["POST"]),
    Route("/mcp/authorize", mcp_authorize, methods=["GET"]),
    Route("/mcp/token", mcp_token, methods=["POST"]),
    Route("/mcp/revoke", mcp_revoke, methods=["POST"]),
]
