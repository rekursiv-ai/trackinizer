"""Same-origin checks for state-changing requests and WebSocket handshakes.

Compared on host only, NOT ``scheme://host``: behind a TLS-terminating proxy the app
sees ``http`` while the browser's ``Origin`` is ``https://...``, so a scheme-sensitive
compare would refuse a legitimate same-origin request. Host identity is the load-bearing
check; a same-host scheme mismatch is not a cross-site forgery.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlsplit


if TYPE_CHECKING:
    from starlette.requests import HTTPConnection


__all__ = ["is_same_origin", "is_strictly_same_origin"]


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


def _names_this_host(request: HTTPConnection, claimed: str) -> bool:
    """Whether the origin URL ``claimed`` has this request's ``Host``."""
    try:
        return urlsplit(claimed).netloc == request.headers.get("host", "")
    except ValueError:
        return False
