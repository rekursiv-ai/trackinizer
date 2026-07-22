"""Helpers shared across API route modules."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Literal, cast

from fastapi import HTTPException

from trackinizer.lib.postgres import DatabaseEngine
from trackinizer.wire.seq_ranges import SeqRange, parse_seq_range


if TYPE_CHECKING:
    from fastapi import Request


RoleLiteral = Literal["viewer", "writer", "admin"]


def engine_of(request: Request) -> DatabaseEngine:
    """Return the DatabaseEngine held on app state."""
    engine: DatabaseEngine = request.app.state.engine
    return engine


def parse_seq_ranges(
    raw: Sequence[str] | None, *, min_seq: int
) -> tuple[SeqRange, ...]:
    """Decode repeated ``seq_range=a..b`` params into a union, raising 400.

    The one route adapter over the shared :func:`parse_seq_range`: every
    list-style endpoint maps its repeated ``seq_range`` params through here,
    so the grammar and the ``min_seq`` lower-bound check live in a single
    place and a malformed param is a uniform 400 across routes.
    """
    try:
        return tuple(parse_seq_range(text, min_seq=min_seq) for text in (raw or ()))
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err


def iso_format(value: object) -> str | None:
    """Render a datetime column as an ISO 8601 string, or None."""
    if value is None:
        return None
    return cast(datetime, value).isoformat()
