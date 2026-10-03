"""FastAPI app, lifespan, and exception handlers."""

from __future__ import annotations

from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Final, cast
from urllib.parse import quote
from uuid import UUID, uuid4

import asyncio
import logging
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.types import Send

import asyncpg

from trackinizer.addons.addon import ServerContext
from trackinizer.addons.deployment import Deployment, supervise
from trackinizer.lib.custom_json import IntCodec, SchemaError
from trackinizer.server.api import (
    addons_routes,
    admin_routes,
    auth_routes,
    edge,
    edit,
    export_routes,
    meta_routes,
    metrics_routes,
    oauth_routes,
    preset_routes,
    query,
    reports_routes,
    session_ir_routes,
    sessions_routes,
    submit,
    timeline_routes,
    visuals_routes,
    workspace_routes,
)
from trackinizer.server.api.addons_routes import deployment_of
from trackinizer.server.api.idempotency import ChangeIdMiddleware
from trackinizer.server.auth import seed_no_auth_user
from trackinizer.server.authority_sweep import authority_sweep_loop
from trackinizer.server.config import (
    Config,
    build_embedder,
    build_engine,
)
from trackinizer.server.embedders import registry
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.session_reaper import session_reaper_loop
from trackinizer.server.store.core import Store
from trackinizer.server.subscriber import push_changes_to_live_subscribers
from trackinizer.server.visuals.catalog import default_workspace
from trackinizer.types.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from starlette.types import ASGIApp, Message, Receive, Scope

    from trackinizer.types.embedder import QueryEmbedder


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
        """Apply to the input."""
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
    """Open the engine, build the ``Store``, and apply the schema for the app's lifetime.

    Args:
      app: FastAPI application instance.

    Yields:
      nothing: Yields control after startup, resumes on shutdown.

    """
    config: object = getattr(app.state, "config", None)
    if not isinstance(config, Config):
        config = Config.from_env()
    if config.auth_disabled:
        # ``--no-auth`` / ``TRACKINIZER_NO_AUTH`` collapses every request to a
        # synthetic admin -- anyone who can reach the port can edit everything.
        # It exists only for ephemeral local demos, so a server that reaches
        # the lifespan with it set must announce it loudly (API-47).
        _logger.critical(
            "AUTH IS DISABLED: every request resolves to a synthetic admin. "
            "This is for local demos only -- never expose this server to an "
            "untrusted network.",
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
        # Resolve the session-search embedder ONCE at startup and cache it on
        # app.state (web.py reads it there). A real model is degraded to None
        # when its weights are absent -- never downloaded in-band -- and warmed
        # in the background when present, so the first query pays inference
        # only.
        warm_task = _resolve_session_embedder(app, config)
        # Subscriber push: copies committed change rows into subscribers'
        # live-session inbound queues (doorbell-driven; see subscriber_push).
        push_task = asyncio.create_task(
            push_changes_to_live_subscribers(app.state.store, app.state.inbound),
        )
        # Authority sweep: recomputes the derived load-bearing (PageRank)
        # columns off the request path, coalescing edge-change bursts.
        authority_task = asyncio.create_task(authority_sweep_loop(app.state.store))
        # Session reaper: closes sessions whose run went silent (killed, host
        # crashed), so a dead agent stops showing as live.
        reaper_task = asyncio.create_task(
            session_reaper_loop(app.state.store, inbound=app.state.inbound),
        )
        addon_tasks = _start_addon_services(
            deployment_of(app),
            context=ServerContext(store=app.state.store, inbound=app.state.inbound),
        )
        try:
            yield
        finally:
            for addon_task in addon_tasks:
                addon_task.cancel()
                with suppress(asyncio.CancelledError):
                    await addon_task
            push_task.cancel()
            with suppress(asyncio.CancelledError):
                await push_task
            authority_task.cancel()
            with suppress(asyncio.CancelledError):
                await authority_task
            reaper_task.cancel()
            with suppress(asyncio.CancelledError):
                await reaper_task
            if warm_task is not None:
                warm_task.cancel()
                with suppress(asyncio.CancelledError):
                    await warm_task
        # Bracket the engine teardown so an operator (and the shutdown-latency
        # investigation) can see where time goes: a gap BEFORE this line is
        # uvicorn draining in-flight connections; a gap until "engine closed"
        # is the engine/PGlite teardown itself. info-level so it lands in the
        # server log alongside uvicorn's own "Shutting down" lines.
        _logger.info("shutdown: closing engine")
    _logger.info("shutdown: engine closed")


# ``registry.weights_present`` is torch-free: a stub / unset name returns True
# without importing anything heavy, and a real model imports only its own module
# (transitively torch) to probe the HF cache -- so a full-text-only server never
# pays the import.
# Returns the warm-up task when one was scheduled (weights present), else ``None`` (no
# model, or degraded because weights are absent).
def _resolve_session_embedder(
    app: FastAPI,
    config: Config,
) -> asyncio.Task[None] | None:
    """Cache the session embedder on ``app.state``; degrade or warm as needed."""
    name = config.session_embedder
    embedder = registry.build_session_embedder(name)
    if embedder is None:
        app.state.session_embedder = None
        return None
    if registry.is_weightless(name):
        # A stub has no weights to load, so it is ready immediately and needs no
        # background warm.
        app.state.session_embedder = embedder
        return None
    if registry.weights_present(name):
        # A real model with cached weights is ready. Warm the lazy load off the
        # request path so the first query pays inference only, not the load.
        app.state.session_embedder = embedder
        return asyncio.create_task(_warm_session_embedder(embedder))
    _logger.error(
        "session embedder %r configured but its weights are NOT in the HF "
        "cache; semantic session search is DISABLED (full-text only). "
        "Run `python -m trackinizer.server.prep_models` on this host "
        "to download them, then restart. A request will NEVER download "
        "them in-band.",
        name,
    )
    app.state.session_embedder = None
    return None


async def _warm_session_embedder(embedder: QueryEmbedder) -> None:
    """Trigger the embedder's lazy weight load once, in the background."""
    _ = await embedder.embed_query("warm")


# Each addon service is supervised on its own: a crash is logged and the service
# restarted with backoff, so a failing addon never takes the API down with it.
def _start_addon_services(
    deployment: Deployment,
    context: ServerContext,
) -> list[asyncio.Task[None]]:
    """Start every server service of ``deployment``; return their tasks."""
    return [
        asyncio.create_task(
            supervise(f"{name}.{service.name}", run=partial(service.run, context)),
        )
        for name, manifest in deployment.manifests.items()
        for service in manifest.server_services
    ]


# Every route module's router, in inclusion order. ``_build_app`` includes them, and
# the web app's schema dump builds its own app from the same tuple rather than
# copying the module-level ``app``, which tests reconfigure.
ROUTERS: Final = (
    addons_routes.router,
    admin_routes.router,
    auth_routes.router,
    edge.router,
    edit.router,
    export_routes.router,
    meta_routes.router,
    metrics_routes.router,
    oauth_routes.router,
    preset_routes.router,
    query.router,
    reports_routes.router,
    session_ir_routes.router,
    sessions_routes.router,
    submit.router,
    timeline_routes.router,
    visuals_routes.router,
    workspace_routes.router,
)


def _build_app() -> FastAPI:
    """Assemble the application: middleware, then every route module's router."""
    built = FastAPI(title="Trackinizer", lifespan=lifespan)
    built.add_middleware(ChangeIdMiddleware)
    built.add_middleware(RequestLoggingMiddleware)
    for router in ROUTERS:
        built.include_router(router)
    # The visual routes read the catalog from ``app.state.visual_catalog``, so it
    # is set on every app; ``attach_deployment`` swaps in a configured one.
    built.state.deployment = Deployment.Config().make()
    built.state.visual_catalog = default_workspace()
    return built


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
            self.status_code = IntCodec.coerce(cast(object, message["status"]))
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
        # Percent-encode what the client sent, as uvicorn's access log does:
        # decoded, a newline in it forges a whole log line and a space forges a
        # field, from any request, authenticated or not (S6-06).
        method = quote(self.method)
        path = quote(self.path)
        # A failure is logged at WARNING, the level a deployment that keeps its
        # logs small runs at, with the request id the web app shows beside the
        # error, so a user's report finds this line. Every other request stays
        # INFO: a line per request is volume such a deployment turns off.
        _logger.log(
            logging.WARNING if outcome == "failure" else logging.INFO,
            "event=trackinizer_request_completed stage=http_request "
            "outcome=%s method=%s path=%s status_code=%d "
            "response_start_sec=%.6f duration_sec=%.6f request_id=%s "
            "worker_pid=%d error_type=%s",
            outcome,
            method,
            path,
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
                "method": method,
                "path": path,
                "status_code": self.status_code,
                "response_start_sec": self.response_start_sec,
                "duration_sec": duration_sec,
                "request_id": self.request_id,
                "worker_pid": os.getpid(),
                "error_type": error_type,
            },
        )


def _http_outcome(status_code: int) -> str:
    if status_code < 400:
        return "success"
    if status_code < 500:
        return "rejected"
    return "failure"


app = _build_app()


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

    Args:
      request: FastAPI Request object (unused).
      exc: ValidationError with detail and code fields.

    Returns:
      response: JSON response with 422 status, detail, and code.

    """
    del request
    return JSONResponse(
        status_code=422,
        content={"detail": str(exc), "code": exc.code},
    )


@app.exception_handler(SchemaError)
async def schema_handler(request: Request, exc: SchemaError) -> JSONResponse:
    """Translate a codec ``SchemaError`` into HTTP 422.

    A stray key in a client-supplied record ``payload`` reaches the codec
    through ``RecordBody``'s decode on the append-records path. It is a
    malformed request, not a server fault, but the codec raises a
    ``ValueError`` -- which matched no handler and so surfaced as a 500.

    Args:
      request: FastAPI Request object (unused).
      exc: SchemaError from codec validation.

    Returns:
      response: JSON response with 422 status, detail, and code='schema'.

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

    Args:
      request: FastAPI Request object (unused).
      exc: NotFoundError with detail and code fields.

    Returns:
      response: JSON response with 404 status, detail, and code.

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

    Args:
      request: FastAPI Request object (unused).
      exc: ForeignKeyViolationError from asyncpg (unused).

    Returns:
      response: JSON response with 409 status and generic detail message.

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

    Args:
      request: FastAPI Request object (unused).
      exc: CheckViolationError from asyncpg (unused).

    Returns:
      response: JSON response with 409 status and generic detail message.

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

    Args:
      request: FastAPI Request object (unused).
      exc: UniqueViolationError from asyncpg (unused).

    Returns:
      response: JSON response with 409 status and generic detail message.

    """
    del request, exc
    return JSONResponse(
        status_code=409,
        content={"detail": "unique constraint violated"},
    )
