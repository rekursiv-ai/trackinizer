"""Signed browser-session cookies for auth v2 Phase 3.

The OAuth callback issues a session cookie carrying the authenticated
user's id; subsequent requests resolve to that user via :func:`current_user`
without round-tripping Google again. The cookie body is signed with
``itsdangerous.URLSafeTimedSerializer`` so the server can verify integrity
and reject any cookie older than the configured TTL.

Cookie attributes follow the Phase 3 spec in the Auth section of ``docs/design.md``:
``HttpOnly`` (no JS access), ``Secure`` (HTTPS only), ``SameSite=Lax``
(safe under top-level cross-site GETs but blocked on form POSTs from
other origins), and ``Path=/`` (cookie travels with every request to
the trackinizer host).

Two cookies are signed by the same secret with different salts:

* :data:`SESSION_COOKIE_NAME` -- the long-lived browser session
  (``session_max_age_seconds``).
* :data:`OAUTH_STATE_COOKIE_NAME` -- a short-lived CSRF binding for the
  OAuth round-trip (``OAUTH_STATE_MAX_AGE_SECONDS``); the cookie also
  stashes the post-login redirect URL so the callback can resume where
  the user left off.

Both cookies are opaque to clients; only the server holds the signing key.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Literal, Protocol, cast

from itsdangerous import BadSignature, URLSafeTimedSerializer


class _SetsCookies(Protocol):
    """Structural type for the subset of :class:`fastapi.Response` we use.

    Helpers in this module only need ``set_cookie`` / ``delete_cookie``;
    typing against :class:`Response` itself would force tests to fake
    the whole class (or ``cast(Any, ...)``) to pass a stub that only
    implements those two methods.
    """

    def set_cookie(
        self,
        key: str,
        value: str = "",
        *,
        max_age: int | None = None,
        httponly: bool = False,
        secure: bool = False,
        samesite: Literal["lax", "strict", "none"] | None = "lax",
        path: str | None = "/",
    ) -> None: ...

    def delete_cookie(
        self,
        key: str,
        *,
        path: str = "/",
        httponly: bool = False,
        secure: bool = False,
        samesite: Literal["lax", "strict", "none"] | None = "lax",
    ) -> None: ...


__all__ = [
    "OAUTH_STATE_COOKIE_NAME",
    "OAUTH_STATE_MAX_AGE_SECONDS",
    "SESSION_COOKIE_NAME",
    "clear_oauth_state_cookie",
    "clear_session_cookie",
    "issue_oauth_state",
    "read_oauth_state_cookie",
    "read_session_cookie",
    "set_session_cookie",
]


SESSION_COOKIE_NAME: Final[str] = (
    "trackinizer_session"  # config-globals: ignore -- fixed cookie name (wire/protocol contract), not a tunable
)
OAUTH_STATE_COOKIE_NAME: Final[str] = (
    "trackinizer_oauth_state"  # config-globals: ignore -- fixed cookie name (wire/protocol contract), not a tunable
)

# Salts namespace the signature so a session cookie cannot be replayed as
# an OAuth state cookie (or vice-versa) even though both share one key.
_SESSION_SALT: Final[str] = (
    "trackinizer.session.v1"  # config-globals: ignore -- signature-namespacing salt (protocol constant), not a tunable
)
_OAUTH_STATE_SALT: Final[str] = (
    "trackinizer.oauth_state.v1"  # config-globals: ignore -- signature-namespacing salt (protocol constant), not a tunable
)

# OAuth round-trip should finish in seconds; ten minutes is generous and
# bounds replay risk on a stolen state cookie.
OAUTH_STATE_MAX_AGE_SECONDS: Final[int] = (
    10 * 60
)  # config-globals: ignore -- shared default; threading would duplicate across 3 call sites (issue/read/clear) and the exported wire constant


def set_session_cookie(
    response: _SetsCookies,
    *,
    user_id: str,
    secret: str,
    max_age_seconds: int,
) -> None:
    """Sign and attach the browser-session cookie to ``response``.

    Args:
      response: The outgoing :class:`fastapi.Response`; the cookie is
        appended via ``Set-Cookie``.
      user_id: ``users.id`` of the authenticated principal. Stringified
        in the cookie payload so the format is JSON-friendly.
      secret: HMAC key used to sign the cookie. Must come from
        :attr:`Config.session_secret`; rotating it invalidates every
        live session.
      max_age_seconds: Cookie TTL. The browser drops the cookie after
        this many seconds; the server re-checks freshness on each read
        via the timestamped signature.

    """
    serializer = URLSafeTimedSerializer(secret, salt=_SESSION_SALT)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=serializer.dumps({"user_id": user_id}),
        max_age=max_age_seconds,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def read_session_cookie(
    cookies: Mapping[str, str],
    *,
    secret: str,
    max_age_seconds: int,
) -> str | None:
    """Return the ``user_id`` stored in the session cookie, or ``None``.

    Args:
      cookies: Mapping of cookie name to value; pass ``request.cookies``
        from a FastAPI route. The helper takes the dict instead of the
        request so tests don't have to fake a ``Request`` object.
      secret: HMAC key the cookie was signed with. A mismatch yields
        ``None`` -- the same outcome as no cookie at all so the caller
        only has to handle one "no session" branch.
      max_age_seconds: Reject cookies older than this. Tampered or
        truncated cookies, expired signatures, and unknown payload
        shapes all fall through to ``None``.

    Returns:
      user_id: Stringified ``users.id`` when the cookie is valid and
        unexpired, else ``None``.

    """
    raw = cookies.get(SESSION_COOKIE_NAME)
    if not raw:
        return None
    serializer = URLSafeTimedSerializer(secret, salt=_SESSION_SALT)
    try:
        payload = serializer.loads(raw, max_age=max_age_seconds)
    except BadSignature:
        return None
    if not isinstance(payload, dict):
        return None
    user_id = cast(dict[str, object], payload).get("user_id")
    if not isinstance(user_id, str):
        return None
    return user_id


def clear_session_cookie(response: _SetsCookies) -> None:
    """Append a ``Set-Cookie`` that expires the session cookie immediately."""
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=True,
        samesite="lax",
    )


def issue_oauth_state(
    response: _SetsCookies,
    *,
    state: str,
    next_url: str,
    secret: str,
) -> None:
    """Sign and attach the OAuth state cookie carrying ``state`` + ``next_url``.

    Args:
      response: The outgoing redirect response.
      state: Random nonce also handed to Google as the ``state`` query
        parameter. The callback verifies the two halves match.
      next_url: Where to redirect after a successful login. Stashed in
        the cookie so the callback can resume the original request even
        when the OAuth round-trip crosses minutes.
      secret: HMAC key used to sign the cookie.

    """
    serializer = URLSafeTimedSerializer(secret, salt=_OAUTH_STATE_SALT)
    response.set_cookie(
        key=OAUTH_STATE_COOKIE_NAME,
        value=serializer.dumps({"state": state, "next": next_url}),
        max_age=OAUTH_STATE_MAX_AGE_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def read_oauth_state_cookie(
    cookies: Mapping[str, str],
    *,
    secret: str,
) -> tuple[str, str] | None:
    """Return the ``(state, next_url)`` pair stashed at login start, or ``None``.

    Args:
      cookies: Mapping of cookie name to value; pass ``request.cookies``
        from the callback route. Takes the dict (not the request) so
        tests don't have to fake a ``Request`` object.
      secret: HMAC key the cookie was signed with.

    Returns:
      state_and_next: ``(state, next_url)`` when the cookie is valid;
        ``None`` when missing, expired, tampered, or wrong shape.

    """
    raw = cookies.get(OAUTH_STATE_COOKIE_NAME)
    if not raw:
        return None
    serializer = URLSafeTimedSerializer(secret, salt=_OAUTH_STATE_SALT)
    try:
        payload = serializer.loads(raw, max_age=OAUTH_STATE_MAX_AGE_SECONDS)
    except BadSignature:
        return None
    if not isinstance(payload, dict):
        return None
    narrowed = cast(dict[str, object], payload)
    state = narrowed.get("state")
    next_url = narrowed.get("next")
    if not isinstance(state, str) or not isinstance(next_url, str):
        return None
    return state, next_url


def clear_oauth_state_cookie(response: _SetsCookies) -> None:
    """Append a ``Set-Cookie`` that expires the OAuth state cookie."""
    response.delete_cookie(
        key=OAUTH_STATE_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=True,
        samesite="lax",
    )
