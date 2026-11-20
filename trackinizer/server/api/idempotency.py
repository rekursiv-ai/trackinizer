"""Carry the ``Idempotency-Key`` request header into ``Store``'s contextvar.

Replay and dedup semantics live in ``Store.emit_change``; this module is
only the parsing hop. The header is the idempotency primitive for edits
and other non-submit mutations. Submits instead carry an
``idempotency_key`` in the request body, which ``Store._submit_generic``
plumbs into the same contextvar before ``emit_change`` runs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import uuid

from fastapi.responses import JSONResponse
from starlette.datastructures import Headers

from trackinizer.server.store.change_id_slot import (
    set_client_change_id,
)


if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send


# A plain ASGI middleware, not Starlette's ``BaseHTTPMiddleware``: that one hands the app
# a receive of its own, through which ``Request.is_disconnected`` never hears that the
# caller has gone, so a daemon's held session wait would hand a start to a dead daemon.
class ChangeIdMiddleware:
    """Parse ``Idempotency-Key`` into ``set_client_change_id`` for each request."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Run the app with the request's key set, or refuse a malformed one."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        raw = Headers(scope=scope).get("Idempotency-Key")
        # Kept on the request's state, so a route reads the key as parsed here, once,
        # and each spelling of one UUID is one key (``_routes_shared.idempotency_key``).
        state = cast("dict[str, object]", scope.setdefault("state", {}))
        state["idempotency_key"] = None
        if raw is None:
            await self._app(scope, receive, send)
            return
        try:
            key = uuid.UUID(raw)
        except ValueError:
            refused = JSONResponse(
                status_code=400,
                content={"detail": "Idempotency-Key is not a valid UUID"},
            )
            await refused(scope, receive, send)
            return
        state["idempotency_key"] = key
        set_client_change_id(key)
        try:
            await self._app(scope, receive, send)
        finally:
            # The app runs in this task, whose context outlives the request.
            set_client_change_id(None)
