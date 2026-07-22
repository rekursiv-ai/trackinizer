"""FastAPI app, lifespan, and exception handlers."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import cast

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import asyncpg

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


_logger = logging.getLogger(__name__)


__all__ = [
    "app",
    "check_violation_handler",
    "conflict_handler",
    "fk_violation_handler",
    "lifespan",
    "not_found_handler",
    "unique_violation_handler",
]


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
