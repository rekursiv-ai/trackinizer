"""``POST /auth/logout``: end the caller's browser session.

Signing in is a deployment's choice of provider, but every server reads the
session cookie (:func:`auth.current_user`), so every server can clear it. The
web app's sign-out posts here.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from trackinizer.server.session import clear_session_cookie


__all__ = ["auth_logout_route"]


router = APIRouter()


# The name and docstring are the route's operationId and description in the
# committed OpenAPI dump, which the web app is typed against and the web deploy
# compares with the live server's; a change to either is a schema change.
@router.post("/auth/logout")
async def auth_logout_route(request: Request) -> RedirectResponse:
    """Clear the session cookie and redirect the user back to ``/``.

    Logout mutates the session cookie, so it is a state-changing POST that a
    cross-origin page could trigger under ``SameSite=Lax`` (which allows
    top-level same-site requests to carry the cookie) -- a forced-logout CSRF.
    Reject a request whose ``Origin`` (or, as a fallback, ``Referer``) names a
    different origin than this server's; a request with neither header (a
    non-browser client such as the CLI) has no CSRF vector and is allowed.

    Args:
      request: HTTP request with Origin/Referer for CSRF validation.

    Returns:
      response: 302 redirect to '/' with session cookie cleared.

    """
    if not _request_is_same_origin(request):
        raise HTTPException(status_code=403, detail="cross-origin logout rejected")
    response = RedirectResponse(url="/", status_code=302)
    clear_session_cookie(response)
    return response


# Browsers attach ``Origin`` to cross-origin (and most same-origin) POSTs and
# ``Referer`` to navigations. A request is same-origin when the first such header
# present has the same HOST (``netloc``) as the request's own ``Host`` header; a request
# carrying neither (a non-browser client) has no CSRF vector and is treated as same-
# origin.
#
# Compared on host only, NOT ``scheme://host``: behind a TLS-terminating proxy the app
# sees ``request.url.scheme == "http"`` while the browser's ``Origin`` is
# ``https://...``, so a scheme-sensitive compare would 403 a legitimate same-origin
# logout. Host identity is the load-bearing CSRF check; a same-host scheme mismatch is
# not a cross-site forgery.
#
# A header ``urlsplit`` cannot parse (``https://[`` opens an IPv6 host it never closes)
# names no origin, so it is foreign; uncaught, its ``ValueError`` answered 500.
def _request_is_same_origin(request: Request) -> bool:
    """Whether a state-changing request originates from this server's origin."""
    claimed = request.headers.get("origin")
    if claimed is None:
        claimed = request.headers.get("referer")
    if claimed is None:
        return True
    try:
        return urlsplit(claimed).netloc == request.headers.get("host", "")
    except ValueError:
        return False
