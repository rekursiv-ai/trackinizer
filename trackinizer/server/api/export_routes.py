"""``GET /api/export``: the whole graph, or one selector's subgraph, as JSON lines.

The line shape is :mod:`trackinizer.wire.wire_export`'s. Thin like every
route: one :meth:`~trackinizer.server.store.export._ExportMixin.export_graph`
call, then serialization. The store has already released its connection by
the time the body streams, so a slow reader holds nothing but its own rows.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Annotated, cast
from uuid import UUID

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from trackinizer.lib.custom_json import DictCodec, loads
from trackinizer.server.api._deps import get_store
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.wire.filters import (
    VALUELESS_FILTER_OPS,
    Filter,
    FilterOp,
    canonical_filter_field,
)
from trackinizer.wire.routes import MAX_LIST_LIMIT
from trackinizer.wire.wire_export import (
    EXPORT_API_PATH,
    EXPORT_FILTER_PARAM,
    EXPORT_FORMAT,
    EXPORT_MEDIA_TYPE,
    EXPORT_SELECTOR_FIELDS,
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
    filter_: Annotated[
        list[str] | None,
        Query(alias=EXPORT_FILTER_PARAM, max_length=MAX_LIST_LIMIT),
    ] = None,
) -> StreamingResponse:
    """Stream every exported row as one JSON object per line.

    Viewer-gated: it is a read, and a backup or mirror job should be able to
    run on a read-only key.

    Args:
      request: FastAPI request object for store access.
      identity: Authenticated user identity, viewer-role-gated.
      filter_: Selector clauses, one per repeated param, ANDed. None exports
        the whole graph.

    Returns:
      result: The header line, then one line per row.

    """
    del identity
    selector = tuple(_parse_selector_param(raw) for raw in (filter_ or ()))
    graph = await get_store(request).export_graph(selector=selector)
    return StreamingResponse(export_lines(graph), media_type=EXPORT_MEDIA_TYPE)


# Deliberately NOT ``query._parse_filter_param``: that one validates the field against
# one kind's columns, and an export spans every kind. The clause rules it enforces are
# the wire type's, which ``Filter`` runs in its own ``__post_init__``, so they are not
# re-implemented here either.
def _parse_selector_param(raw: str) -> Filter:
    """Decode one ``filter=<json>`` selector clause, raising 400 on bad input."""
    try:
        payload = loads(raw)
    except json.JSONDecodeError as err:
        raise HTTPException(
            status_code=400,
            detail=f"filter is not valid JSON: {err}",
        ) from err
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="filter must be a JSON object")
    obj = DictCodec.coerce(payload)
    field = obj.get("field")
    op = obj.get("op")
    # A presence op carries no operand, so a missing value is its well-formed
    # spelling; ``Filter`` refuses one supplied anyway.
    valueless = isinstance(op, str) and op in VALUELESS_FILTER_OPS
    value = obj.get("value", "") if valueless else obj.get("value")
    if (
        not isinstance(field, str)
        or not isinstance(op, str)
        or not isinstance(value, str)
    ):
        raise HTTPException(
            status_code=400,
            detail="filter requires string field/op/value entries",
        )
    canonical = canonical_filter_field(field)
    if canonical not in EXPORT_SELECTOR_FIELDS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"export selector cannot filter on {field!r}; "
                f"expected one of {sorted(EXPORT_SELECTOR_FIELDS)}"
            ),
        )
    try:
        return Filter(field=canonical, op=cast(FilterOp, op), value=value)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err


def export_lines(graph: GraphExport) -> Iterator[bytes]:
    """Serialize ``graph`` into the export's lines, header first.

    Args:
      graph: What :meth:`export_graph` read.

    Yields:
      line: One UTF-8 JSON object with its trailing newline.

    """
    header: dict[str, object] = {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "migrations": list(graph.migrations),
    }
    # Absent rather than empty on a whole-graph export, so those stay
    # byte-identical to the ones written before selectors existed.
    if graph.selector:
        header["selector"] = [
            {"field": f.field, "op": f.op, "value": f.value} for f in graph.selector
        ]
    yield _line(header)
    for table, rows in graph.tables:
        for row in rows:
            yield _line({"table": table, "row": row})


# ``allow_nan=False`` because the output must parse anywhere: a non-finite float is a
# bug to surface here, not a bare ``NaN`` a strict reader rejects.
def _line(value: object) -> bytes:
    """One compact JSON object and its newline."""
    text = json.dumps(
        value,
        default=_as_json,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return f"{text}\n".encode()


def _as_json(value: object) -> str:
    """Spell the column types JSON has no literal for."""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"no JSON form for {type(value).__name__}: {value!r}")
