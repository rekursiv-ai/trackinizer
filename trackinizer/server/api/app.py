"""FastAPI app, lifespan, and exception handlers."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid4

import asyncio
import logging
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import asyncpg

from trackinizer.lib.custom_json import SchemaError
from trackinizer.server.api import (
    admin_routes,
    auth_routes,
    edge,
    edit,
    meta_routes,
    metrics_routes,
    oauth_routes,
    query,
    sessions_routes,
    submit,
)
from trackinizer.server.api.idempotency import ChangeIdMiddleware
from trackinizer.server.auth import seed_no_auth_user
from trackinizer.server.config import (
    Config,
    build_embedder,
    build_engine,
)
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.store.core import Store
from trackinizer.types.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)


if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send


_logger = logging.getLogger(__name__)


__all__ = [
    "RequestLoggingMiddleware",
    "app",
    "check_violation_handler",
    "conflict_handler",
    "fk_violation_handler",
    "lifespan",
    "not_found_handler",
    "schema_handler",
    "unique_violation_handler",
]


class RequestLoggingMiddleware:
    """Correlate and time every HTTP request without buffering responses."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        span = _RequestLogSpan.from_scope(scope, send=send)
        try:
            await self._app(scope, receive, span.send)
        except asyncio.CancelledError:
            span.log(outcome="cancelled", error_type="CancelledError")
            raise
        except BaseException as error:
            span.log(outcome="failure", error_type=type(error).__name__)
            raise
        else:
            span.log(outcome=_http_outcome(span.status_code))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Open the engine, build the ``Store``, and apply the schema for the app's lifetime."""
    config = cast(Config, getattr(app.state, "config", None)) or Config.from_env()
    if config.auth_disabled:
        # ``--no-auth`` / ``TRACKINIZER_NO_AUTH`` collapses every request to a
        # synthetic admin -- anyone who can reach the port can edit everything.
        # It exists only for ephemeral local demos, so a server that reaches
        # the lifespan with it set must announce it loudly (API-47).
        _logger.critical(
            "AUTH IS DISABLED: every request resolves to a synthetic admin. "
            "This is for local demos only -- never expose this server to an "
            "untrusted network."
        )
    async with build_engine(config) as engine:
        app.state.engine = engine
        app.state.store = Store(engine, embed=build_embedder(config.embedder))
        # Process-local routing buffer for inbound (world -> session) messages;
        # separate from event capture. The sessions routes read it off state.
        app.state.inbound = InboundQueue()
        # Keep the resolved config on app.state so OAuth routes and the
        # session-cookie path in current_user can read the signing secret
        # and Google client credentials. main() mounts the SPA separately.
        app.state.config = config
        await app.state.store.bootstrap()
        if config.auth_disabled:
            # The synthetic no-auth principal must exist as an active user so
            # its submits pass the account-attribution gate; seed it here, once
            # the schema is in place.
            async with engine.acquire() as conn:
                await seed_no_auth_user(conn)
        yield
        # Bracket the engine teardown so an operator (and the shutdown-latency
        # investigation) can see where time goes: a gap BEFORE this line is
        # uvicorn draining in-flight connections; a gap until "engine closed"
        # is the engine/PGlite teardown itself. info-level so it lands in the
        # server log alongside uvicorn's own "Shutting down" lines.
        _logger.info("shutdown: closing engine")
    _logger.info("shutdown: engine closed")


app = FastAPI(title="Trackinizer", lifespan=lifespan)
app.add_middleware(ChangeIdMiddleware)
app.add_middleware(RequestLoggingMiddleware)
for route_module in (
    admin_routes,
    auth_routes,
    edge,
    edit,
    meta_routes,
    metrics_routes,
    oauth_routes,
    query,
    sessions_routes,
    submit,
):
    app.include_router(route_module.router)


@app.exception_handler(ConflictError)
async def conflict_handler(request: Request, exc: ConflictError) -> JSONResponse:
    """Translate a ``ConflictError`` into HTTP 409."""
    del request
    return JSONResponse(
        status_code=409,
        content={"detail": str(exc), "code": exc.code},
    )


@app.exception_handler(ValidationError)
async def validation_handler(request: Request, exc: ValidationError) -> JSONResponse:
    """Translate a ``ValidationError`` into HTTP 422 (Unprocessable Content).

    A malformed request -- semantically invalid on its own terms (per RFC 9110
    422) -- distinct from a ``ConflictError`` (409) clash with existing state.
    """
    del request
    return JSONResponse(
        status_code=422,
        content={"detail": str(exc), "code": exc.code},
    )


@app.exception_handler(SchemaError)
async def schema_handler(request: Request, exc: SchemaError) -> JSONResponse:
    """Translate a codec ``SchemaError`` into HTTP 422.

    A stray key in a client-supplied ``message`` body reaches the codec
    through ``EventBody.to_event`` on the append-events path. It is a
    malformed request, not a server fault, but the codec raises a
    ``ValueError`` -- which matched no handler and so surfaced as a 500.
    """
    del request
    return JSONResponse(
        status_code=422,
        content={"detail": str(exc), "code": "schema"},
    )


@app.exception_handler(NotFoundError)
async def not_found_handler(request: Request, exc: NotFoundError) -> JSONResponse:
    """Translate a ``NotFoundError`` into HTTP 404.

    ``NotFoundError`` subclasses ``ConflictError``; FastAPI dispatches to
    the most specific registered handler, so a not-found mutation gets 404
    while a genuine state clash still falls through to 409.
    """
    del request
    return JSONResponse(
        status_code=404,
        content={"detail": str(exc), "code": exc.code},
    )


@app.exception_handler(asyncpg.ForeignKeyViolationError)
async def fk_violation_handler(
    request: Request,
    exc: asyncpg.ForeignKeyViolationError,
) -> JSONResponse:
    """Translate a foreign-key violation (bogus edge target id) into HTTP 409.

    Without this, the violation leaks as a raw asyncpg exception and
    surfaces as 500, leaving the client unable to tell a server bug from
    a bad reference. ``exc.detail`` is dropped: it names internal columns /
    constraints (``Key (from_id)=(...) is not present``), which must not
    reach the client (REV-OPUS-03). The generic message is enough for the
    caller to know the reference was bad.
    """
    del request, exc
    return JSONResponse(
        status_code=409,
        content={"detail": "foreign key violation"},
    )


@app.exception_handler(asyncpg.CheckViolationError)
async def check_violation_handler(
    request: Request,
    exc: asyncpg.CheckViolationError,
) -> JSONResponse:
    """Translate a schema CHECK violation into HTTP 409.

    These come from the per-kind CHECK constraints on ``inquiries`` and
    the kind-vs-column gates on ``change_log``: the client supplied a
    value the database refused, which is a client error, not a server bug.
    ``exc.detail`` is dropped: it names the violated constraint / column,
    internal schema detail the client must not see (REV-OPUS-03).
    """
    del request, exc
    return JSONResponse(
        status_code=409,
        content={"detail": "check constraint violated"},
    )


@app.exception_handler(asyncpg.UniqueViolationError)
async def unique_violation_handler(
    request: Request,
    exc: asyncpg.UniqueViolationError,
) -> JSONResponse:
    """Translate a unique-constraint violation into HTTP 409.

    ``exc.detail`` is dropped: it names the conflicting key / column
    values, internal detail the client must not see (REV-OPUS-03).
    """
    del request, exc
    return JSONResponse(
        status_code=409,
        content={"detail": "unique constraint violated"},
    )


@dataclass(slots=True, kw_only=True)
class _RequestLogSpan:
    downstream: Send
    request_id: str
    method: str
    path: str
    started: float
    status_code: int = 0
    response_start_sec: float = 0.0
    logged: bool = False

    @classmethod
    def from_scope(cls, scope: Scope, *, send: Send) -> _RequestLogSpan:
        request_id = _request_id_from_scope(scope)
        state = cast(dict[str, object], scope.setdefault("state", {}))
        state["request_id"] = request_id
        return cls(
            downstream=send,
            request_id=request_id,
            method=cast(str, scope.get("method", "")),
            path=cast(str, scope.get("path", "")),
            started=time.perf_counter(),
        )

    async def send(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.status_code = int(message["status"])
            self.response_start_sec = time.perf_counter() - self.started
            headers = list(cast(list[tuple[bytes, bytes]], message.get("headers", [])))
            headers = [
                (name, value)
                for name, value in headers
                if name.lower() != b"x-request-id"
            ]
            headers.append((b"x-request-id", self.request_id.encode("ascii")))
            message["headers"] = headers
        await self.downstream(message)

    def log(self, *, outcome: str, error_type: str = "") -> None:
        if self.logged:
            return
        self.logged = True
        duration_sec = time.perf_counter() - self.started
        _logger.info(
            "event=trackinizer_request_completed stage=http_request "
            "outcome=%s method=%s path=%s status_code=%d "
            "response_start_sec=%.6f duration_sec=%.6f request_id=%s "
            "worker_pid=%d error_type=%s",
            outcome,
            self.method,
            self.path,
            self.status_code,
            self.response_start_sec,
            duration_sec,
            self.request_id,
            os.getpid(),
            error_type,
            extra={
                "event": "trackinizer_request_completed",
                "stage": "http_request",
                "outcome": outcome,
                "method": self.method,
                "path": self.path,
                "status_code": self.status_code,
                "response_start_sec": self.response_start_sec,
                "duration_sec": duration_sec,
                "request_id": self.request_id,
                "worker_pid": os.getpid(),
                "error_type": error_type,
            },
        )


def _request_id_from_scope(scope: Scope) -> str:
    raw_headers = cast(list[tuple[bytes, bytes]], scope.get("headers", []))
    raw = next(
        (
            value.decode("ascii", errors="ignore")
            for name, value in raw_headers
            if name.lower() == b"x-request-id"
        ),
        "",
    )
    try:
        return str(UUID(raw))
    except ValueError:
        return str(uuid4())


def _http_outcome(status_code: int) -> str:
    if status_code < 400:
        return "success"
    if status_code < 500:
        return "rejected"
    return "failure"
