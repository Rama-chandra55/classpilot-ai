"""
Google Web OAuth

The Phase 2 web-server OAuth flow: build a Google authorization URL,
exchange an authorization code for tokens, verify the ID token to get the
student's stable Google identity, and (later, ad-hoc — not yet wired into
any MCP tool) build a live, authorized `Credentials` object for a stored
user, refreshing only when actually needed.

Deliberately separate from vendor_classroom_suite_mcp/auth.py's
InstalledAppFlow-based single-user flow — that keeps working unchanged as
the fallback. This module only ever talks to a distinct "Web application"
type OAuth client (see classpilot/config.py's google_web_credentials_path).

Scopes: reuses the EXACT curated Classroom/Drive scope list
classroom_client.py already patched onto the vendored auth module in
Phase 1 (not a duplicated copy that could drift) — plus three OIDC
identity scopes (`openid`, `email`, `profile`) that Phase 1's scopes don't
include, because the web flow additionally needs Google to hand back an
ID token containing the student's stable `sub` claim. OIDC identity scopes
are not "sensitive" or "restricted" scopes for Google's verification
review, so adding them doesn't change this app's scope-minimization
posture from Phase 1 — they carry no Classroom/Drive access of their own.

Refresh-token handling (see complete_authorization / _persist_credentials):
Google only reliably returns a refresh_token on a user's FIRST
authorization; a later re-authorization (re-consent, scope change, etc.)
may come back with `refresh_token=None` even though the access grant is
otherwise fine. Persisting that None would silently destroy a working
credential and lock the student out. This module always keeps a
previously-stored refresh token if Google doesn't send a new one for that
call — see _persist_credentials.

Access-token caching (see get_authorized_credentials): the database only
ever stores the refresh_token (Phase 1 decision — access tokens are cheap
to re-derive and not worth persisting). Since nothing is stored to compare
against between calls, this module keeps a small in-process cache of the
live `Credentials` object per user_id so a valid, not-yet-expired access
token is reused rather than refreshed on every single call.

PKCE code_verifier lifecycle (see build_authorization_url /
exchange_code_for_user): google_auth_oauthlib's Flow object generates a
fresh, random code_verifier as a side effect of calling
authorization_url(), stored only on that Flow *instance*. Since /login and
/callback are two separate HTTP requests — and therefore two separate Flow
objects — the verifier from /login would be gone by the time /callback
needs it unless something persists it in between. That persistence lives
in classpilot/oauth_state.py, alongside the CSRF state it's paired with;
this module's job is just to produce the verifier (build_authorization_url)
and consume an explicitly-supplied one (exchange_code_for_user) rather
than silently regenerating a new, non-matching one.

Granted-scope superset handling (see exchange_code_for_user /
_verify_required_scopes_present): build_authorization_url sets
include_granted_scopes=true (needed to preserve Google's normal
incremental-authorization behavior — not something this fix removes).
When this Google Cloud project has OTHER, older OAuth clients (e.g. the
Desktop app behind the token.json fallback) that were previously granted
broader scopes, Google's token response can legitimately include those
scopes too, even though the CURRENT request only asked for the curated
set. oauthlib's own token-response parser
(oauthlib.oauth2.rfc6749.parameters.validate_token_parameters) treats ANY
difference between the requested and granted scope sets — including a
pure superset — as an error and raises a bare `Warning` exception, unless
the OAUTHLIB_RELAX_TOKEN_SCOPE environment variable is set. This module
sets that variable, narrowly, only around the single fetch_token() call
(see _relaxed_token_scope_validation) so it can never affect any other
oauthlib usage in this process (including the Desktop flow's own
exchange). Because that relaxation disables oauthlib's check in BOTH
directions (extra scopes AND missing scopes), this module then explicitly
re-verifies that every one of ClassPilot's own required scopes was
actually granted (_verify_required_scopes_present) — an extra granted
scope is fine and expected; a MISSING required one must still fail
authorization, loudly.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token as google_id_token
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from .config import get_config
from .user_store import UserStore

logger = logging.getLogger(__name__)

_TOKEN_URI = "https://oauth2.googleapis.com/token"

_OIDC_IDENTITY_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]


class GoogleOAuthError(Exception):
    """Raised for any failure in the Google web OAuth flow — authorization
    URL construction, code exchange, ID token verification, credential
    refresh, or persisting credentials."""


@dataclass(frozen=True)
class ExchangedIdentity:
    sub: str
    email: Optional[str]
    name: Optional[str]
    refresh_token: Optional[str]  # may be None — see module docstring
    scopes: list[str]
    token_expiry: Optional[datetime]


# ---------- Scopes ----------

def get_web_flow_scopes() -> list[str]:
    """
    The scopes requested during the web OAuth flow: Phase 1's curated
    Classroom/Drive scopes (read live off the vendored auth module, not a
    hardcoded duplicate — see module docstring) plus the OIDC identity
    scopes needed to get an ID token back.
    """
    import classpilot.classroom_client  # noqa: F401 - import side effect: patches vendor SCOPES
    from classroom_suite_mcp import auth as _vendor_auth

    combined = list(_vendor_auth.SCOPES)
    for scope in _OIDC_IDENTITY_SCOPES:
        if scope not in combined:
            combined.append(scope)
    return combined


# ---------- Web client config ----------

_web_client_config_cache: Optional[dict] = None


def _load_web_client_config() -> dict:
    """Read and cache google_web_credentials.json's 'web' section (client_id
    and client_secret) — needed for building refresh-only Credentials
    objects outside of a full Flow (see get_authorized_credentials)."""
    global _web_client_config_cache
    if _web_client_config_cache is None:
        import json
        path = get_config().google_web_credentials_path
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            raise GoogleOAuthError(
                f"Could not read Google web OAuth client config at '{path}': {exc}. "
                "Download a 'Web application' type OAuth client JSON from Google "
                "Cloud Console and set GOOGLE_WEB_CREDENTIALS_PATH."
            ) from exc
        _web_client_config_cache = data.get("web", data)
    return _web_client_config_cache


def _reset_web_client_config_cache() -> None:  # pragma: no cover - test helper
    global _web_client_config_cache
    _web_client_config_cache = None


# ---------- Authorization URL / code exchange ----------

def _build_flow(state: Optional[str] = None) -> Flow:
    config = get_config()
    flow = Flow.from_client_secrets_file(
        config.google_web_credentials_path,
        scopes=get_web_flow_scopes(),
        state=state,
    )
    flow.redirect_uri = config.google_oauth_redirect_uri
    return flow


def build_authorization_url(state: str) -> tuple[str, str]:
    """
    Build the Google consent-screen URL for a student to visit.

    Returns (auth_url, code_verifier). The Flow used to build this URL
    generates a fresh PKCE code_verifier as a side effect of calling
    authorization_url() below; that verifier is only ever held on this
    (about-to-be-discarded) Flow instance, so the caller MUST persist it
    via classpilot.oauth_state.register_state(state, code_verifier) —
    otherwise the later token exchange in exchange_code_for_user() has no
    way to reconstruct it and Google will reject the exchange with
    "Missing code verifier".
    """
    flow = _build_flow(state=state)
    auth_url, _ = flow.authorization_url(
        access_type="offline",       # required to receive a refresh_token
        prompt="consent",            # ask every time, so a refresh_token is issued when possible
        include_granted_scopes="true",
    )
    return auth_url, flow.code_verifier


def exchange_code_for_user(code: str, code_verifier: str) -> ExchangedIdentity:
    """
    Exchange an authorization code for tokens and verify the ID token.

    `code_verifier` must be the exact value returned alongside the
    authorization URL this code came from (see build_authorization_url) —
    retrieved via classpilot.oauth_state.consume_state(state). A fresh
    Flow object is built here (a new HTTP request, potentially a different
    process from the one that built the authorization URL), so its
    code_verifier is explicitly set rather than auto-generated — an
    auto-generated one would not match the code_challenge Google already
    received during /login, and the exchange would fail.

    Does NOT touch the database — pure exchange + verification. Raises
    GoogleOAuthError on any failure (invalid/expired code, network error,
    ID token verification failure, a missing 'sub' claim, or a granted
    scope set missing one of ClassPilot's required scopes — see
    _verify_required_scopes_present).
    """
    if not code_verifier:
        raise GoogleOAuthError(
            "No code_verifier supplied for this token exchange — the PKCE "
            "flow cannot complete. This should never happen when called via "
            "the callback route, which requires consume_state() to succeed "
            "(and return a code_verifier) before calling this function."
        )
    flow = _build_flow()
    flow.code_verifier = code_verifier
    flow.autogenerate_code_verifier = False  # defensive: never silently replace it with a fresh one
    try:
        with _relaxed_token_scope_validation():
            flow.fetch_token(code=code)
    except Exception as exc:
        raise GoogleOAuthError(f"Failed to exchange authorization code: {exc}") from exc

    credentials = flow.credentials
    # credentials.scopes reflects what THIS request ASKED for (session.scope) —
    # credentials.granted_scopes is what Google's token response actually
    # returned (may legitimately be a superset; see module docstring).
    # Verifying required scopes against the wrong one would make this check
    # a no-op (it would just compare our request to itself).
    granted_scopes = list(credentials.granted_scopes or [])
    _verify_required_scopes_present(granted_scopes)

    if not credentials.id_token:
        raise GoogleOAuthError(
            "Google did not return an ID token — cannot identify the student. "
            "This should not happen when the 'openid' scope is requested; "
            "check the OAuth client configuration."
        )

    try:
        claims = google_id_token.verify_oauth2_token(
            credentials.id_token,
            GoogleAuthRequest(),
            audience=_load_web_client_config().get("client_id"),
        )
    except Exception as exc:
        raise GoogleOAuthError(f"Failed to verify Google ID token: {exc}") from exc

    sub = claims.get("sub")
    if not sub:
        raise GoogleOAuthError("Google ID token did not include a 'sub' claim.")

    return ExchangedIdentity(
        sub=sub,
        email=claims.get("email"),
        name=claims.get("name"),
        refresh_token=credentials.refresh_token,
        scopes=granted_scopes,
        token_expiry=credentials.expiry,
    )


@contextlib.contextmanager
def _relaxed_token_scope_validation():
    """
    Temporarily relax oauthlib's strict token-response scope validation
    for a single token exchange call — see the module docstring's
    "Granted-scope superset handling" section for the full rationale.

    oauthlib.oauth2.rfc6749.parameters.validate_token_parameters() raises
    a bare `Warning` (an actually-raised exception, not a soft
    warnings.warn) whenever the scope Google returns differs AT ALL —
    including a pure superset — from what this specific request asked
    for. OAUTHLIB_RELAX_TOKEN_SCOPE is oauthlib's own documented
    environment-variable escape hatch for this. It's applied here via a
    save/restore around only this one call, rather than set process-wide,
    so it can never affect any other oauthlib usage in this process —
    including the Desktop InstalledAppFlow's own token exchange in
    vendor_classroom_suite_mcp/auth.py (the token.json fallback), which
    must keep behaving exactly as it does today.
    """
    key = "OAUTHLIB_RELAX_TOKEN_SCOPE"
    previous = os.environ.get(key)
    os.environ[key] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def _verify_required_scopes_present(granted_scopes: list[str]) -> None:
    """
    Independently verify every one of ClassPilot's required (curated)
    scopes was actually granted.

    This exists BECAUSE _relaxed_token_scope_validation disables
    oauthlib's own scope check in both directions — extra granted scopes
    (expected, benign, handled above) AND missing required scopes (not
    benign) would otherwise both pass through silently. This function is
    what still catches the second case: if Google's response is missing
    any scope ClassPilot actually needs (e.g. the student didn't approve
    every permission on the consent screen), authorization must fail
    loudly here rather than proceeding with silently-reduced access.

    Extra granted scopes beyond the required set are never added to
    anything ClassPilot considers its "requested" configuration —
    get_web_flow_scopes() remains a pure function of the curated Phase 1
    list + OIDC scopes, entirely unaffected by what any particular
    authorization happened to be granted.
    """
    required = set(get_web_flow_scopes())
    granted = set(granted_scopes)
    missing = required - granted
    if missing:
        raise GoogleOAuthError(
            "Google did not grant all permissions ClassPilot requires. "
            f"Missing scope(s): {', '.join(sorted(missing))}. Please "
            "authorize again and approve every requested permission."
        )


# ---------- Callback-side: identify + persist ----------

def _persist_credentials(
    user_store: UserStore,
    user_id: str,
    new_refresh_token: Optional[str],
    scopes: list[str],
    token_expiry: Optional[datetime],
) -> None:
    """
    Store this authorization's credentials, WITHOUT ever overwriting an
    already-stored, valid refresh token with None.

    Google only reliably issues a refresh_token on a student's first
    authorization (or after they've revoked and re-granted access) — a
    routine re-authorization can come back with refresh_token=None while
    the previously-issued one is still perfectly valid. Treating a missing
    new refresh_token as "nothing to update" (falling back to whatever is
    already stored) rather than as an error or a destructive overwrite is
    the whole point of this function.
    """
    if new_refresh_token:
        user_store.save_google_credentials(
            user_id, refresh_token=new_refresh_token, scopes=scopes, token_expiry=token_expiry,
        )
        return

    existing = user_store.get_google_credentials(user_id)
    if existing is None:
        raise GoogleOAuthError(
            "Google did not return a refresh token for this authorization, and "
            "no previously stored credentials exist for this account. Revoke "
            "ClassPilot's access under your Google Account's 'Third-party apps "
            "& services' settings, then authorize again."
        )
    logger.info(
        "Google did not return a new refresh_token on re-authorization for "
        "user_id=%s — keeping the existing stored refresh token.",
        user_id,
    )
    user_store.save_google_credentials(
        user_id, refresh_token=existing.refresh_token, scopes=scopes, token_expiry=token_expiry,
    )


def complete_authorization(code: str, code_verifier: str, user_store: UserStore):
    """
    Full callback-side flow: exchange the code (using the PKCE
    code_verifier retrieved from classpilot.oauth_state.consume_state),
    get-or-create the ClassPilot user (keyed on the stable Google `sub`,
    never on email alone), and persist credentials per the
    refresh-token-preservation rule above. Returns the UserRecord.
    """
    identity = exchange_code_for_user(code, code_verifier)
    user = user_store.get_or_create_user(
        identity.sub, email=identity.email, display_name=identity.name,
    )
    _persist_credentials(
        user_store, user.id, identity.refresh_token, identity.scopes, identity.token_expiry,
    )
    return user


# ---------- Per-user authorized credentials (refresh only when needed) ----------

_credentials_cache: dict[str, Credentials] = {}
_credentials_cache_lock = threading.Lock()


def get_authorized_credentials(
    user_id: str, user_store: UserStore, force_refresh: bool = False,
) -> Credentials:
    """
    Return a live, valid `Credentials` object for this user, refreshing
    the Google access token only when there's no cached one or the cached
    one has expired (or `force_refresh=True`) — not unconditionally on
    every call. The refresh_token itself is never re-fetched from the
    database on a cache hit either.

    Raises GoogleOAuthError if no credentials are stored for this user, or
    if the refresh call itself fails (e.g. the student revoked access).
    """
    with _credentials_cache_lock:
        cached = _credentials_cache.get(user_id)
    if cached is not None and not force_refresh and cached.valid:
        return cached

    record = user_store.get_google_credentials(user_id)
    if record is None:
        raise GoogleOAuthError(f"No stored Google credentials for user_id={user_id}.")

    client_config = _load_web_client_config()
    creds = Credentials(
        token=None,
        refresh_token=record.refresh_token,
        token_uri=_TOKEN_URI,
        client_id=client_config.get("client_id"),
        client_secret=client_config.get("client_secret"),
        scopes=record.scopes,
    )
    try:
        creds.refresh(GoogleAuthRequest())
    except Exception as exc:
        raise GoogleOAuthError(
            f"Failed to refresh Google credentials for user_id={user_id}: {exc}. "
            "The student may need to re-authorize (their refresh token may "
            "have been revoked)."
        ) from exc

    with _credentials_cache_lock:
        _credentials_cache[user_id] = creds
    return creds


def clear_credentials_cache(user_id: Optional[str] = None) -> None:
    """Drop cached live credentials — for tests, and for a future
    revocation flow to make sure a freshly-deleted credential can't still
    be served from cache."""
    with _credentials_cache_lock:
        if user_id is None:
            _credentials_cache.clear()
        else:
            _credentials_cache.pop(user_id, None)
