"""``GET /api/export``: the whole graph as JSON lines.

The line shape is :mod:`trackinizer.wire.wire_export`'s. Thin like every
route: one :meth:`~trackinizer.server.store.export._ExportMixin.export_graph`
call, then serialization. The store has already released its connection by
the time the body streams, so a slow reader holds nothing but its own rows.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from trackinizer.server.api._deps import get_store
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.wire.wire_export import (
    EXPORT_API_PATH,
    EXPORT_FORMAT,
    EXPORT_MEDIA_TYPE,
    EXPORT_VERSION,
)


if TYPE_CHECKING:
    from collections.abc import Iterator

    from trackinizer.server.store.export import GraphExport


__all__ = [
    "export_lines",
    "export_route",
    "router",
]


router = APIRouter()


@router.get(EXPORT_API_PATH)
async def export_route(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> StreamingResponse:
    """Stream every exported row as one JSON object per line.

    Viewer-gated: it is a read, and a backup or mirror job should be able to
    run on a read-only key.

    Args:
      request: FastAPI request object for store access.
      identity: Authenticated user identity, viewer-role-gated.

    Returns:
      result: The header line, then one line per row.

    """
    del identity
    graph = await get_store(request).export_graph()
    return StreamingResponse(export_lines(graph), media_type=EXPORT_MEDIA_TYPE)


def export_lines(graph: GraphExport) -> Iterator[bytes]:
    """Serialize ``graph`` into the export's lines, header first.

    Args:
      graph: What :meth:`export_graph` read.

    Yields:
      line: One UTF-8 JSON object with its trailing newline.

    """
    yield _line(
        {
            "format": EXPORT_FORMAT,
            "version": EXPORT_VERSION,
            "migrations": list(graph.migrations),
        },
    )
    for table, rows in graph.tables:
        for row in rows:
            yield _line({"table": table, "row": row})


def _line(value: object) -> bytes:
    """One compact JSON object and its newline.

    ``allow_nan=False`` because the output must parse anywhere: a non-finite
    float is a bug to surface here, not a bare ``NaN`` a strict reader rejects.
    """
    text = json.dumps(
        value,
        default=_as_json,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return f"{text}\n".encode()


def _as_json(value: object) -> str:
    """Spell the column types JSON has no literal for.

    Args:
      value: A value ``json`` could not encode on its own.

    Returns:
      text: ``str`` of a UUID, ISO 8601 of a timestamp.

    """
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"no JSON form for {type(value).__name__}: {value!r}")
