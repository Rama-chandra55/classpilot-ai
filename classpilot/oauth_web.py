"""
OAuth Web App

A small, standalone Starlette app implementing the two HTTP legs of the
Phase 2 web OAuth flow:

    GET /auth/google/login     -> redirect the student's browser to Google
    GET /auth/google/callback  -> Google redirects back here with a code

This is deliberately NOT part of classpilot/server.py or http_server.py —
it has nothing to do with the MCP transport or any MCP tool, and nothing
here is reachable from an MCP client. That separation is what makes "don't
expose Google tokens to the AI/MCP client" true by construction: there is
no code path connecting this module to a tool response. Wiring student
identity into MCP tool calls is later-phase work.

Run standalone:
    classpilot-ai-oauth
or
    python -m classpilot.oauth_web

No token value (access or refresh) is ever included in any HTTP response
this app sends — every response is a plain, generic status page.
"""

from __future__ import annotations

import logging

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

from .config import get_config
from .db import init_schema
from .google_oauth import GoogleOAuthError, build_authorization_url, complete_authorization
from .oauth_state import generate_state, register_state, consume_state
from .user_store import UserStore

logger = logging.getLogger(__name__)


def _html_page(title: str, message: str, ok: bool = True) -> HTMLResponse:
    color = "#1a7f37" if ok else "#c62828"
    return HTMLResponse(
        f"""<!DOCTYPE html>
<html><head><title>{title}</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 480px; margin: 80px auto; text-align: center;">
  <h2 style="color: {color};">{title}</h2>
  <p>{message}</p>
</body></html>""",
        status_code=200 if ok else 400,
    )


async def google_login(request: Request) -> RedirectResponse:
    """
    Start the flow: issue a CSRF state, build the authorization URL (which
    generates this attempt's PKCE code_verifier as a side effect), persist
    the (state, code_verifier) pair together, and redirect to Google.
    """
    state = generate_state()
    auth_url, code_verifier = build_authorization_url(state)
    register_state(state, code_verifier)
    return RedirectResponse(auth_url)


async def google_callback(request: Request) -> HTMLResponse:
    """
    Handle Google's redirect back. Validates state (CSRF), then either:
      - the student denied consent (Google sends `error=...`)
      - a genuine failure occurred exchanging/verifying tokens
        (GoogleOAuthError) or anything else unexpected
      - success: user identified and credentials stored
    In every case, the response is a plain status page — never a token
    value or raw exception detail.
    """
    error = request.query_params.get("error")
    if error:
        logger.info("Google OAuth callback reported an error: %s", error)
        return _html_page(
            "Sign-in cancelled",
            "You didn't grant ClassPilot access to your Google account. "
            "You can close this window and try again if that was a mistake.",
            ok=False,
        )

    state = request.query_params.get("state")
    code_verifier = consume_state(state)
    if code_verifier is None:
        return _html_page(
            "Sign-in link expired or invalid",
            "This sign-in link is no longer valid — it may have already been "
            "used, or taken too long to complete. Please start again.",
            ok=False,
        )

    code = request.query_params.get("code")
    if not code:
        return _html_page(
            "Sign-in failed",
            "Google's response was missing an authorization code. Please try again.",
            ok=False,
        )

    try:
        user = complete_authorization(code, code_verifier, UserStore())
    except GoogleOAuthError as exc:
        logger.error("OAuth callback failed: %s", exc)
        return _html_page(
            "Sign-in failed",
            "Something went wrong connecting your Google account. "
            "Please try again, or contact support if this keeps happening.",
            ok=False,
        )
    except Exception:
        # Anything genuinely unexpected (not a GoogleOAuthError) must still
        # degrade to a clean, generic page — never a raw stack trace or
        # exception message exposed to the student's browser. The real
        # exception is logged server-side (with traceback) for debugging.
        logger.exception("Unexpected error in OAuth callback")
        return _html_page(
            "Sign-in failed",
            "Something unexpected went wrong. Please try again, or contact "
            "support if this keeps happening.",
            ok=False,
        )

    display = user.email or user.display_name or "your Google account"
    return _html_page(
        "Connected!",
        f"ClassPilot is now connected to {display}. You can close this window.",
        ok=True,
    )


routes = [
    Route("/auth/google/login", google_login),
    Route("/auth/google/callback", google_callback),
]

app = Starlette(routes=routes)


def main() -> None:
    """Entry point for the `classpilot-ai-oauth` console script."""
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    config = get_config()
    init_schema()
    logger.info(
        "Starting ClassPilot OAuth web app on http://%s:%d — visit /auth/google/login to connect an account",
        config.oauth_web_host, config.oauth_web_port,
    )
    uvicorn.run(app, host=config.oauth_web_host, port=config.oauth_web_port)


if __name__ == "__main__":
    main()
