"""``POST /auth/logout``: end the caller's browser session.

Signing in is a deployment's choice of provider, but every server reads the
session cookie (:func:`auth.current_user`), so every server can clear it. The
web app's sign-out posts here.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from trackinizer.server.api._origin import is_same_origin
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
    if not is_same_origin(request):
        raise HTTPException(status_code=403, detail="cross-origin logout rejected")
    response = RedirectResponse(url="/", status_code=302)
    clear_session_cookie(response)
    return response
