"""Same-origin checks for state-changing requests and WebSocket handshakes.

Compared on host only, NOT ``scheme://host``: behind a TLS-terminating proxy the app
sees ``http`` while the browser's ``Origin`` is ``https://...``, so a scheme-sensitive
compare would refuse a legitimate same-origin request. Host identity is the load-bearing
check; a same-host scheme mismatch is not a cross-site forgery.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit


if TYPE_CHECKING:
    from fastapi import HTTPException
    from starlette.requests import HTTPConnection
else:
    from wrapt import lazy_import

    HTTPException = lazy_import("fastapi", "HTTPException")


__all__ = [
    "is_own_page_write",
    "is_same_origin",
    "is_strictly_same_origin",
    "require_own_page_write",
]


# A header ``urlsplit`` cannot parse (``https://[`` opens an IPv6 host it never closes)
# names no origin, so it is foreign; uncaught, its ``ValueError`` answered 500.
def is_same_origin(request: HTTPConnection) -> bool:
    """Whether a request originates from this server's origin.

    The first of ``Origin`` and ``Referer`` that is present must name this ``Host``. A
    request with neither (a non-browser client) has no CSRF vector and passes.

    Args:
      request: The request or WebSocket handshake.

    Returns:
      same: Whether the request may have come from this server's own pages.

    """
    claimed = request.headers.get("origin")
    if claimed is None:
        claimed = request.headers.get("referer")
    if claimed is None:
        return True
    return _names_this_host(request, claimed)


def is_strictly_same_origin(request: HTTPConnection) -> bool:
    """Whether the request carries an ``Origin`` that names this server's ``Host``.

    A shell is opened by a browser, which always sends ``Origin`` on a ``POST`` and on
    a WebSocket handshake, so a missing header is refused, not trusted.

    Args:
      request: The request or WebSocket handshake.

    Returns:
      same: Whether ``Origin`` is present and names this server's ``Host``.

    """
    claimed = request.headers.get("origin")
    return claimed is not None and _names_this_host(request, claimed)


def is_own_page_write(request: HTTPConnection) -> bool:
    """Whether a request that changes state came from this server's own pages.

    It must carry an ``Origin`` naming this ``Host``, as a browser's always does on a
    method other than ``GET`` and ``HEAD`` (:func:`is_strictly_same_origin`), and any
    ``Sec-Fetch-Site`` it carries must say ``same-origin``. ``SameSite=Lax`` keys on the
    site, not the origin, so a page on a sibling host of the same registrable domain
    sends the session cookie with its form; only these headers tell it apart.

    Args:
      request: The request.

    Returns:
      own: Whether the request may have come from this server's own pages.

    """
    fetched_from = request.headers.get("sec-fetch-site")
    return fetched_from in {None, "same-origin"} and is_strictly_same_origin(request)


def require_own_page_write(request: HTTPConnection) -> None:
    """Refuse a request a session cookie signs in that writes from another page.

    Every method but ``GET``, ``HEAD`` and ``OPTIONS`` writes, and must pass
    :func:`is_own_page_write`. A WebSocket handshake has no method here and is no
    write; the terminal's socket checks its ``Origin`` itself.

    Args:
      request: A request signed in by a session cookie.

    Raises:
      HTTPException: 403 for a write that did not come from this server's pages.

    """
    method = cast("str", request.scope.get("method", "GET"))
    if method in {"GET", "HEAD", "OPTIONS"} or is_own_page_write(request):
        return
    raise HTTPException(
        status_code=403,
        detail="A write signed in by a session cookie must come from this site's pages.",
    )


def _names_this_host(request: HTTPConnection, claimed: str) -> bool:
    """Whether the origin URL ``claimed`` has this request's ``Host``."""
    try:
        return urlsplit(claimed).netloc == request.headers.get("host", "")
    except ValueError:
        return False
