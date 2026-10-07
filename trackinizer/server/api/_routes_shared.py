"""Helpers shared across API route modules."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal, Protocol, cast
from uuid import UUID

from trackinizer.wire.routes import inquiry_row_fields
from trackinizer.wire.seq_ranges import SeqRange, parse_seq_range


if TYPE_CHECKING:
    from collections.abc import Sequence

    from fastapi import HTTPException, Request

    from trackinizer.lib.postgres import DatabaseEngine
    from trackinizer.server.auth import AuthIdentity
else:
    from wrapt import lazy_import

    HTTPException = lazy_import("fastapi", "HTTPException")  # ~120 ms.


RoleLiteral = Literal["viewer", "writer", "admin"]


class _State(Protocol):
    engine: DatabaseEngine


def engine_of(request: Request) -> DatabaseEngine:
    """Return the DatabaseEngine held on app state.

    Args:
      request: Request.

    Returns:
      engine: The DatabaseEngine.

    """
    app = cast(_App, request.app)
    engine: DatabaseEngine = app.state.engine
    return engine


def parse_seq_ranges(
    raw: Sequence[str] | None,
    *,
    min_seq: int,
) -> tuple[SeqRange, ...]:
    """Decode repeated ``seq_range=a..b`` params into a union, raising 400.

    The one route adapter over the shared :func:`parse_seq_range`: every
    list-style endpoint maps its repeated ``seq_range`` params through here,
    so the grammar and the ``min_seq`` lower-bound check live in a single
    place and a malformed param is a uniform 400 across routes.

    Args:
      raw: Query param values or None if absent.
      min_seq: Lower-bound seq (error if range below this).

    Returns:
      result: Tuple of parsed SeqRange objects.

    """
    try:
        return tuple(parse_seq_range(text, min_seq=min_seq) for text in (raw or ()))
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err


def parse_fields(raw: Sequence[str] | None) -> frozenset[str] | None:
    """Decode repeated ``fields=<name>`` params into the keys to send, raising 400.

    ``None`` (no ``fields`` param) sends every key. A name some kind's rows
    carry is valid on any request; a row without that key just omits it.

    Args:
      raw: Query param values or None if absent.

    Returns:
      names: The keys each row keeps, or None for the whole row.

    """
    if raw is None:
        return None
    for name in raw:
        if name not in inquiry_row_fields():
            raise HTTPException(status_code=400, detail=f"unknown field {name!r}")
    return frozenset(raw)


def iso_format(value: object) -> str | None:
    """Render a datetime column as an ISO 8601 string, or None.

    Args:
      value: datetime object or None.

    Returns:
      result: The str | None.

    """
    if value is None:
        return None
    assert isinstance(value, datetime)
    return value.isoformat()


# ``ChangeIdMiddleware`` parses the header once (rejecting a malformed key with
# 400) and stashes the validated UUID on ``request.state``; this just reads it,
# so the parse and its failure branch live in exactly one place. The ``getattr``
# default covers a request that never passed through the middleware (a bare test
# app), leaving the caller to decide whether an absent key is acceptable.
def idempotency_key(request: Request) -> UUID | None:
    """Return the request's already-parsed ``Idempotency-Key``, or ``None``."""
    key = getattr(request.state, "idempotency_key", None)
    if key is not None and not isinstance(key, UUID):
        raise ValueError("Expected key is None or isinstance(key, UUID).")
    return key


def require_browser(identity: AuthIdentity) -> None:
    """Refuse an API key on a route that is the signed-in browser's alone.

    Args:
      identity: The authenticated principal.

    Raises:
      HTTPException: 403 when the principal is an API key.

    """
    if identity.api_key_id is not None:
        raise HTTPException(status_code=403, detail="Browser session required")


class _App(Protocol):
    state: _State
