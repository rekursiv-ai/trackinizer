"""Browser-facing Google OAuth routes: login, callback, and logout.

These three routes issue session cookies from a Google OpenID Connect
round-trip. ``/auth/login`` mints a state nonce, stashes the post-login
redirect in a signed cookie, and sends the browser to Google's consent
screen. ``/auth/callback`` verifies the state, exchanges the code for
tokens, fetches userinfo, upserts the ``users`` row, and sets the session
cookie. ``/auth/logout`` clears the cookie and redirects home.

When the Google client id/secret or the session secret are unset, the
routes return 503 with a clear message rather than crashing; Bearer auth
keeps working, since only the OAuth surface is disabled.

The callback enforces the email allowlist: off-list emails get 403 and
never produce a ``users`` row, while on-list emails are inserted on first
login and have their ``last_login`` bumped on each later callback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, cast
from urllib.parse import urlencode, urlsplit

import logging
import secrets
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

import httpx

from trackinizer.lib.postgres import Conn
from trackinizer.server.api._routes_shared import engine_of
from trackinizer.server.auth import Role, allowlist_match
from trackinizer.server.config import Config
from trackinizer.server.session import (
    clear_oauth_state_cookie,
    clear_session_cookie,
    issue_oauth_state,
    read_oauth_state_cookie,
    set_session_cookie,
)


_logger = logging.getLogger(__name__)


__all__ = [
    "GOOGLE_AUTHORIZE_URL",
    "GOOGLE_TOKEN_URL",
    "GOOGLE_USERINFO_URL",
    "auth_callback_route",
    "auth_login_route",
    "auth_logout_route",
]


router = APIRouter()

GOOGLE_AUTHORIZE_URL: Final[str] = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL: Final[str] = "https://oauth2.googleapis.com/token"  # noqa: S105 -- OAuth endpoint URL.
GOOGLE_USERINFO_URL: Final[str] = "https://openidconnect.googleapis.com/v1/userinfo"

# email/profile populate the userinfo fields inserted into users; openid
# is required by the OIDC spec.
_OAUTH_SCOPE: Final[str] = "openid email profile"

# Where to land after login when no ?next= was given or the cookie was lost.
_DEFAULT_NEXT_URL: Final[str] = "/"


@router.get("/auth/login")
async def auth_login_route(request: Request, next: str = "/") -> RedirectResponse:
    """Redirect the browser to Google's consent screen.

    Generates a state nonce, signs ``{state, next}`` into the OAuth state
    cookie (CSRF binding plus the post-login URL stash), and returns a 302
    to Google's authorize endpoint with the standard OIDC parameters.
    Raises 503 when the OAuth settings are unconfigured.

    ``next`` is the same-origin path to land on after login; it must start
    with ``/`` so this can't act as an open redirector.
    """
    settings = _resolve_oauth_settings(request)
    safe_next = next if _same_origin_path(next) else _DEFAULT_NEXT_URL
    state = secrets.token_urlsafe(32)
    params = {
        "client_id": settings.client_id,
        "redirect_uri": settings.redirect_uri,
        "response_type": "code",
        "scope": _OAUTH_SCOPE,
        "state": state,
        # Let a multi-account user pick which Google identity to use;
        # without it Google silently picks the first signed-in account.
        "prompt": "select_account",
    }
    target = f"{GOOGLE_AUTHORIZE_URL}?{urlencode(params)}"
    response = RedirectResponse(url=target, status_code=302)
    issue_oauth_state(
        response,
        state=state,
        next_url=safe_next,
        secret=settings.session_secret,
    )
    return response


@router.get("/auth/callback")
async def auth_callback_route(
    request: Request,
    code: str = "",
    state: str = "",
) -> Response:
    """Finish the OAuth round-trip, set the session cookie, and redirect home.

    ``code`` is Google's authorization code and ``state`` is the echoed
    nonce, which must match the value signed into the state cookie or the
    request is rejected. On success, returns a 302 to the stashed
    post-login URL with the session cookie set and the state cookie
    cleared.

    Raises 503 when OAuth is unconfigured, 400 when the state cookie is
    missing or mismatched or the code is absent, and 403 when the
    authenticated email isn't on the allowlist.
    """
    settings = _resolve_oauth_settings(request)
    if not code:
        raise HTTPException(status_code=400, detail="missing authorization code")
    cookie = read_oauth_state_cookie(request.cookies, secret=settings.session_secret)
    if cookie is None:
        raise HTTPException(status_code=400, detail="missing or expired oauth state")
    stashed_state, next_url = cookie
    # Constant-time compare so an attacker can't time-walk the nonce.
    if not secrets.compare_digest(stashed_state, state):
        raise HTTPException(status_code=400, detail="oauth state mismatch")
    tokens = await _exchange_code_for_tokens(settings, code=code)
    userinfo = await _fetch_userinfo(access_token=tokens["access_token"])
    # users.email is a case-sensitive UNIQUE column, so normalize at ingest:
    # two casings of one address must collapse to a single row, not split
    # ownership. ``allowlist_match`` already compares case-insensitively.
    email = _require_string(userinfo, "email").lower()
    if not _require_bool(userinfo, "email_verified"):
        raise HTTPException(status_code=403, detail="google email not verified")
    name = _optional_name(userinfo) or email
    engine = engine_of(request)
    async with engine.acquire() as conn:
        role = await allowlist_match(conn, email=email)
        if role is None:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"{email!r} is not on the trackinizer allowlist. "
                    "Account access must be granted by an administrator."
                ),
            )
        user_id, status = await _upsert_user_on_login(
            conn, email=email, name=name, role=role
        )
    if status != "active":
        raise HTTPException(
            status_code=403,
            detail=(
                f"{email!r} account is disabled. "
                "Account access must be re-enabled by an administrator."
            ),
        )
    response = RedirectResponse(url=next_url, status_code=302)
    set_session_cookie(
        response,
        user_id=str(user_id),
        secret=settings.session_secret,
        max_age_seconds=settings.session_max_age_seconds,
    )
    clear_oauth_state_cookie(response)
    return response


def _same_origin_path(value: str) -> bool:
    """Whether ``value`` is a safe same-origin redirect target.

    Must start with a single ``/`` (so it can't become an absolute or
    protocol-relative URL) and carry no C0 control or DEL character. A
    ``next`` reaches the ``Location`` header and the signed state cookie,
    so an embedded CR/LF would enable header injection / response
    splitting; rejecting the whole control range closes that off.
    """
    if not value.startswith("/") or value.startswith(("//", "/\\")):
        return False
    return all(ch >= " " and ch != "\x7f" for ch in value)


def _request_is_same_origin(request: Request) -> bool:
    """Whether a state-changing request originates from this server's origin.

    Browsers attach ``Origin`` to cross-origin (and most same-origin) POSTs and
    ``Referer`` to navigations. A request is same-origin when the first such
    header present has the same HOST (``netloc``) as the request's own ``Host``
    header; a request carrying neither (a non-browser client) has no CSRF vector
    and is treated as same-origin.

    Compared on host only, NOT ``scheme://host``: behind a TLS-terminating proxy
    the app sees ``request.url.scheme == "http"`` while the browser's ``Origin``
    is ``https://...``, so a scheme-sensitive compare would 403 a legitimate
    same-origin logout (the same proxy hazard that makes the OAuth flow require
    an explicit ``redirect_uri`` rather than ``request.url_for``). Host identity
    is the load-bearing CSRF check; a same-host scheme mismatch is not a
    cross-site forgery.
    """
    expected_host = request.headers.get("host", "")
    origin = request.headers.get("origin")
    if origin is not None:
        return urlsplit(origin).netloc == expected_host
    referer = request.headers.get("referer")
    if referer is not None:
        return urlsplit(referer).netloc == expected_host
    return True


@router.post("/auth/logout")
async def auth_logout_route(request: Request) -> RedirectResponse:
    """Clear the session cookie and redirect the user back to ``/``.

    Logout mutates the session cookie, so it is a state-changing POST that a
    cross-origin page could trigger under ``SameSite=Lax`` (which allows
    top-level same-site requests to carry the cookie) -- a forced-logout CSRF.
    Reject a request whose ``Origin`` (or, as a fallback, ``Referer``) names a
    different origin than this server's; a request with neither header (a
    non-browser client such as the CLI) has no CSRF vector and is allowed.
    """
    if not _request_is_same_origin(request):
        raise HTTPException(status_code=403, detail="cross-origin logout rejected")
    response = RedirectResponse(url="/", status_code=302)
    clear_session_cookie(response)
    return response


@dataclass(frozen=True, slots=True, kw_only=True)
class _OAuthSettings:
    """Resolved OAuth deployment settings."""

    client_id: str
    client_secret: str
    redirect_uri: str
    session_secret: str
    session_max_age_seconds: int


def _resolve_oauth_settings(request: Request) -> _OAuthSettings:
    """Pull the OAuth settings off app state; raise 503 if any are missing.

    Consolidating the "OAuth not configured" check here keeps each route
    from enumerating every setting. The 503 names the missing env var
    rather than a generic "service unavailable", since the likely cause
    is a deployer who forgot one secret.
    """
    config = cast(Config | None, getattr(request.app.state, "config", None))
    if config is None:
        raise HTTPException(
            status_code=503,
            detail="oauth not configured (no Config on app.state)",
        )
    del request
    missing: list[str] = []
    if not config.oauth_google_client_id:
        missing.append("TRACKINIZER_GOOGLE_CLIENT_ID")
    if not config.oauth_google_client_secret:
        missing.append("TRACKINIZER_GOOGLE_CLIENT_SECRET")
    if not config.oauth_redirect_uri:
        # An explicit redirect_uri is required: behind a TLS-terminating
        # proxy, request.url_for would synthesize the wrong scheme/host
        # and break the round-trip silently.
        missing.append("TRACKINIZER_OAUTH_REDIRECT_URI")
    if not config.session_secret:
        missing.append("TRACKINIZER_SESSION_SECRET")
    if missing:
        raise HTTPException(
            status_code=503,
            detail=(
                "oauth not configured; set "
                + ", ".join(missing)
                + " on the server environment"
            ),
        )
    return _OAuthSettings(
        # The missing-list check above guarantees these are set; the cast
        # narrows away the str | None the type checker still sees.
        client_id=cast(str, config.oauth_google_client_id),
        client_secret=cast(str, config.oauth_google_client_secret),
        redirect_uri=cast(str, config.oauth_redirect_uri),
        session_secret=cast(str, config.session_secret),
        session_max_age_seconds=config.session_max_age_seconds,
    )


async def _exchange_code_for_tokens(
    settings: _OAuthSettings,
    *,
    code: str,
) -> dict[str, object]:
    """Exchange the authorization code at Google's token endpoint and return the JSON."""
    async with _http_client() as client:
        response = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.client_id,
                "client_secret": settings.client_secret,
                "redirect_uri": settings.redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    if response.status_code != 200:
        _log_google_failure(
            "token exchange", endpoint=GOOGLE_TOKEN_URL, response=response
        )
        raise HTTPException(
            status_code=400,
            detail="google token exchange failed",
        )
    payload = _parse_google_json(response, op="token exchange")
    if not isinstance(payload, dict) or "access_token" not in payload:
        raise HTTPException(
            status_code=400,
            detail="google token exchange returned no access_token",
        )
    return cast(dict[str, object], payload)


async def _fetch_userinfo(*, access_token: object) -> dict[str, object]:
    """Fetch Google's userinfo endpoint using the bearer access token."""
    if not isinstance(access_token, str):
        raise HTTPException(
            status_code=400, detail="google access_token is not a string"
        )
    async with _http_client() as client:
        response = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if response.status_code != 200:
        _log_google_failure(
            "userinfo fetch", endpoint=GOOGLE_USERINFO_URL, response=response
        )
        raise HTTPException(
            status_code=400,
            detail="google userinfo fetch failed",
        )
    payload = _parse_google_json(response, op="userinfo")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="google userinfo not an object")
    return cast(dict[str, object], payload)


def _parse_google_json(response: httpx.Response, *, op: str) -> object:
    """Parse a Google JSON response, raising 400 on invalid JSON."""
    try:
        return response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"google {op} returned invalid json",
        ) from exc


def _log_google_failure(op: str, *, endpoint: str, response: httpx.Response) -> None:
    """Log a non-2xx Google response with only safe metadata.

    The raw body is never logged: Google's OAuth error bodies can echo
    back the client id, redirect uri, access token, or scopes, and logs
    are retained longer and shared more widely than HTTP responses. What
    gets logged is the operation name, endpoint, status code, and, when
    the body is JSON with a string ``error`` field, the short opaque
    error code (such as ``invalid_grant``). The free-form
    ``error_description`` is dropped because it can carry the same
    sensitive material as the raw body.
    """
    error_code = "unparsed"
    try:
        payload: object = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        code = cast(dict[str, object], payload).get("error")
        if isinstance(code, str) and code:
            error_code = code
    _logger.warning(
        "google %s failed: status=%d endpoint=%s error=%s",
        op,
        response.status_code,
        endpoint,
        error_code,
    )


def _http_client(*, timeout_seconds: float = 10.0) -> httpx.AsyncClient:
    """Build the httpx client used for both Google round-trips.

    Tests monkey-patch this module-level helper to inject an
    ``httpx.MockTransport`` so the suite never hits Google. Routing both
    call sites through one helper lets a single patch cover them without
    threading a client argument into each route.
    """
    return httpx.AsyncClient(timeout=timeout_seconds)


async def _upsert_user_on_login(
    conn: Conn,
    *,
    email: str,
    name: str,
    role: Role,
) -> tuple[uuid.UUID, str]:
    """Insert or update the ``users`` row for one OAuth callback.

    A first-login email creates a new row with ``role`` from the
    allowlist. Subsequent logins keep the existing role and status, so an
    admin who later flipped someone to ``viewer`` won't see them silently
    re-elevated just because they stayed on the admin allowlist. The
    display ``name`` is refreshed from ``EXCLUDED.name`` on every login so a
    Google profile rename propagates (it carries no authorization weight).

    ``last_login`` is only bumped when the existing row is active.
    Bumping it on a disabled account would advertise a denied callback as
    a real session and confuse the admin page, so the conflict path
    leaves the column unchanged when the status isn't active. That's done
    with a ``CASE`` rather than a ``WHERE`` so the row still appears in
    ``RETURNING`` and the callback can read the post-upsert status to
    answer 403 before issuing a session cookie.

    Returns the resolved ``users.id`` and the post-upsert status.
    """
    row = await conn.fetchrow(
        "INSERT INTO users (id, email, name, role, status, last_login) "
        "VALUES ($1, $2, $3, $4, 'active', clock_timestamp()) "
        "ON CONFLICT (email) DO UPDATE SET "
        "name = EXCLUDED.name, last_login = "
        "CASE WHEN users.status = 'active' THEN clock_timestamp() "
        "ELSE users.last_login END "
        "RETURNING id, status",
        uuid.uuid4(),
        email,
        name,
        role,
    )
    if row is None:
        raise RuntimeError("user upsert returned no row")
    return cast(uuid.UUID, row["id"]), cast(str, row["status"])


def _optional_name(payload: dict[str, object]) -> str:
    """Return the userinfo ``name`` if it's a non-empty string, else ``""``.

    Google's ``name`` is optional and free-form; a non-string or absent
    value yields the empty string so the caller falls back to the email
    rather than casting a non-string into the ``users.name`` column.
    """
    value = payload.get("name")
    return value if isinstance(value, str) else ""


def _require_string(payload: dict[str, object], key: str) -> str:
    """Pull a non-empty string field from the userinfo payload, or raise 400."""
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise HTTPException(
            status_code=400,
            detail=f"google userinfo missing {key!r}",
        )
    return value


def _require_bool(payload: dict[str, object], key: str) -> bool:
    """Pull a boolean field from the userinfo payload, defaulting to ``False``."""
    value = payload.get(key, False)
    return value if isinstance(value, bool) else False
