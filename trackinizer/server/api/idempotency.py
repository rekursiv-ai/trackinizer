"""Carry the ``Idempotency-Key`` request header into ``Store``'s contextvar.

Replay and dedup semantics live in ``Store.emit_change``; this module is
only the parsing hop. The header is the idempotency primitive for edits
and other non-submit mutations. Submits instead carry an
``idempotency_key`` in the request body, which ``Store._submit_generic``
plumbs into the same contextvar before ``emit_change`` runs.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, override

import uuid

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from trackinizer.server.store.change_id_slot import (
    set_client_change_id,
)


if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response


class ChangeIdMiddleware(BaseHTTPMiddleware):
    """Parse ``Idempotency-Key`` into ``set_client_change_id`` for each request."""

    @override
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        raw = request.headers.get("Idempotency-Key")
        if raw is None:
            request.state.idempotency_key = None
            return await call_next(request)
        try:
            key = uuid.UUID(raw)
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"detail": "Idempotency-Key is not a valid UUID"},
            )
        set_client_change_id(key)
        # Stash the parsed key on request.state so route helpers read the
        # already-validated value instead of re-parsing the header (and so the
        # parse-failure branch lives here once, not duplicated per reader).
        request.state.idempotency_key = key
        try:
            return await call_next(request)
        finally:
            # Redundant today since the contextvar is task-scoped, but
            # kept so a future pure-ASGI rewrite (which would share the
            # parent task's context with the app) can't leak the key.
            set_client_change_id(None)
