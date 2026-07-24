"""Inquiry read/delete and change-log routes.

This module owns the canonical ``/api/inquiries/*`` and
``/api/change_log*`` surface, including the change-log SSE stream. The
web-facing SSE (``/api/web/subscribe``) and the search routes live in
``web.py`` instead.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, cast

import json
import re
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from trackinizer.lib.custom_json import MutableJSON
from trackinizer.lib.postgres import DatabaseEngine
from trackinizer.server.api._deps import get_store, tag_kind
from trackinizer.server.api._routes_shared import (
    parse_seq_ranges,
)
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.notify import iter_sse_events
from trackinizer.server.primitives import lookup_kinds
from trackinizer.types.change_log import Change
from trackinizer.types.columns import (
    flat_column_specs,
    storage_name,
)
from trackinizer.types.cost import Cost
from trackinizer.types.inquiries import (
    KIND_TO_CLASS,
    Inquiry,
)
from trackinizer.wire.bodies import FieldMutation
from trackinizer.wire.filters import (
    FILTER_OPS,
    MAX_FILTER_VALUE_CHARS,
    VALUELESS_FILTER_OPS,
    Filter,
    FilterOp,
    canonical_filter_field,
    validate_presence_op,
)
from trackinizer.wire.routes import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
)


_FILTER_OPS: frozenset[str] = frozenset(FILTER_OPS)

# Identity/housekeeping columns the schema declares directly: not
# editable, carry no ColumnSpec, and so aren't surfaced by
# flat_column_specs. The flattened marginal_cost_* axes are not listed
# here; they come from flat_column_specs like every other spec'd column.
_IDENTITY_COLUMNS: frozenset[str] = frozenset(
    {"id", "kind", "seq", "created", "modified"}
)

router = APIRouter()


# The change-log slice keeps its own, larger default; the inquiry-list
# default and cap come from the shared wire contract.
_DEFAULT_CHANGE_LIMIT = 200


# -- Inquiry read -----------------------------------------------------------


@router.get("/api/inquiries/next_issue")
async def next_issue_route(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> MutableJSON | None:
    del identity
    return tag_kind(await get_store(request).next_issue())


@router.post("/api/inquiries/lookup")
async def lookup_route(
    ids: Annotated[list[uuid.UUID], Body(max_length=MAX_LIST_LIMIT)],
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> MutableJSON:
    """Resolve many ``UUID -> kind`` mappings in one round-trip.

    The cap is a typed ``max_length`` on the body, so an oversize list is
    rejected (422) before the route decodes it -- the DoS bound is
    pre-decode, not a post-decode route check (REV-OPUS-30). The response
    is ``{"found": {id: kind}, "missing": [id]}`` so a caller learns which
    ids were unknown rather than having them silently dropped from a flat
    mapping (REV-OPUS-12).
    """
    del identity
    async with get_store(request).engine.acquire() as conn:
        kinds = await lookup_kinds(conn, ids)
    found = {str(rid): kind for rid, kind in kinds.items()}
    missing = [str(rid) for rid in ids if rid not in kinds]
    return cast(MutableJSON, {"found": found, "missing": missing})


@router.get("/api/inquiries")
async def list_inquiries_route(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    *,
    kind: Annotated[list[Inquiry.InquiryKind], Query()],
    status: Inquiry.Status | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
    seq_range: Annotated[list[str] | None, Query()] = None,
    filter_: Annotated[list[str] | None, Query(alias="filter")] = None,
) -> list[MutableJSON]:
    """List inquiries across one or more ``kind`` query params.

    Results from every requested kind are concatenated. ``limit`` and
    ``offset`` apply per kind, since each kind runs its own query. At
    least one ``kind`` is required. Each ``seq_range`` param is one
    inclusive ``a..b`` interval; their union selects rows across disjoint
    seq windows in a single query.
    """
    del identity
    if limit < 1 or limit > MAX_LIST_LIMIT:
        raise HTTPException(
            status_code=400, detail=f"limit must be in [1, {MAX_LIST_LIMIT}]"
        )
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0")
    # Inquiry ``seq`` starts at 1.
    seq_ranges = parse_seq_ranges(seq_range, min_seq=1)
    out: list[MutableJSON] = []
    store = get_store(request)
    for one_kind in kind:
        filters = tuple(_parse_filter_param(raw, one_kind) for raw in (filter_ or ()))
        rows = await store.list_kind(
            one_kind,
            status=status,
            limit=limit,
            offset=offset,
            seq_ranges=seq_ranges,
            filters=filters,
        )
        out.extend(tag for r in rows if (tag := tag_kind(r)) is not None)
    return out


# Register the static-suffix routes (/cost, /proves_belief) before the
# /{kind}/{seq} route so a UUID in the first segment isn't matched as a
# kind and rejected by InquiryKind validation. Starlette matches in
# registration order.
@router.get("/api/inquiries/{target_id}/cost")
async def cost_route(
    target_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    deep: bool = False,
) -> Cost:
    del identity
    return _require_found(await get_store(request).cost_for(target_id, deep=deep))


def _require_found[T](value: T | None) -> T:
    """Return ``value`` or raise 404 when it is ``None``.

    A read addressing a specific id (inquiry, short-ref, cost) that finds
    no row is 404, not a 200 with a null body -- consistent with
    ``get_change`` / ``get_edge`` (API-08/24).
    """
    if value is None:
        raise HTTPException(status_code=404, detail="not found")
    return value


@router.get("/api/inquiries/{target_id}/proves_belief")
async def proves_belief_route(
    target_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> list[MutableJSON]:
    del identity
    rows = await get_store(request).proves_belief(target_id)
    return [tag for r in rows if (tag := tag_kind(r)) is not None]


@router.get("/api/inquiries/{kind}/{seq}")
async def by_seq_route(
    kind: Inquiry.InquiryKind,
    seq: int,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> MutableJSON | None:
    """Resolve a short-ref ``kind#seq`` to the full inquiry."""
    del identity
    async with get_store(request).engine.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM inquiries WHERE kind = $1 AND seq = $2", kind, seq
        )
        if row is None:
            raise HTTPException(status_code=404, detail=f"{kind}#{seq} not found")
    return _require_found(tag_kind(await get_store(request).get_inquiry(row["id"])))


@router.get("/api/inquiries/{target_id}")
async def get_inquiry_route(
    target_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> MutableJSON:
    del identity
    return _require_found(tag_kind(await get_store(request).get_inquiry(target_id)))


# -- Inquiry delete ---------------------------------------------------------


@router.delete("/api/inquiries/{target_id}")
async def delete_inquiry_route(
    target_id: uuid.UUID,
    req: FieldMutation,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> MutableJSON:
    """Purge an inquiry row and its edges.

    Writer-gated like every other mutation; inquiries (including AgentSessions)
    are a shared workspace, so any writer may purge any row.
    """
    store = get_store(request)
    change_id = await store.purge(
        target_id,
        api_key_id=identity.api_key_id,
        actor=req.actor or identity.email,
        reason=req.reason,
    )
    return {
        "id": str(target_id),
        "change_id": None if change_id is None else str(change_id),
    }


# -- Change log -------------------------------------------------------------


@router.get("/api/change_log/stream")
async def change_log_stream_route(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> StreamingResponse:
    """Stream change ids over SSE.

    Shares ``iter_sse_events`` with ``/api/web/subscribe`` so both emit
    one wire shape; offline catch-up uses ``GET /api/change_log``.
    """
    del identity
    engine = cast(DatabaseEngine, request.app.state.engine)
    return StreamingResponse(iter_sse_events(engine), media_type="text/event-stream")


@router.get("/api/change_log/{change_id}")
async def get_change_route(
    change_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> Change:
    del identity
    change = await get_store(request).get_change(change_id)
    if change is None:
        raise HTTPException(status_code=404, detail="change not found")
    return change


@router.get("/api/change_log")
async def list_change_log_route(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    *,
    since: datetime | None = None,
    after_id: uuid.UUID | None = None,
    actor: Inquiry.Actor | None = None,
    subject_id: uuid.UUID | None = None,
    subject_kind: Inquiry.InquiryKind | None = None,
    kind: Change.Kind | None = None,
    limit: int = _DEFAULT_CHANGE_LIMIT,
) -> list[Change]:
    """Return a filtered, newest-first slice of the change log."""
    del identity
    if limit < 1 or limit > MAX_LIST_LIMIT:
        raise HTTPException(
            status_code=400, detail=f"limit must be in [1, {MAX_LIST_LIMIT}]"
        )
    return await get_store(request).list_changes(
        since=since,
        after_id=after_id,
        actor=actor,
        subject_id=subject_id,
        subject_kind=subject_kind,
        kind=kind,
        limit=limit,
    )


def _filter_columns_for(kind: Inquiry.InquiryKind) -> frozenset[str]:
    """Return the canonical SQL column names a ``Filter`` may target for ``kind``.

    Derived from ``flat_column_specs`` so the whitelist tracks the Inquiry
    hierarchy automatically, including the flattened ``marginal_cost_*``
    axes. Each flat column maps through :func:`storage_name`, since
    ``canonical_filter_field`` resolves a filter to its storage column
    (``priority`` -> ``issue_priority``) and the whitelist validates the
    canonical name.
    """
    cls = KIND_TO_CLASS[kind]
    declared = {
        storage_name(name, flat.spec)
        for source in (Inquiry, cls)
        for name, flat in flat_column_specs(source).items()
    }
    return _IDENTITY_COLUMNS | frozenset(declared)


def _parse_filter_param(raw: str, kind: Inquiry.InquiryKind) -> Filter:
    """Decode one ``filter=<json>`` query param, raising 400 on bad input.

    ``field`` may arrive as a CLI-friendly alias (``kind``, ``agent-cost``,
    ``result``, ...) or the canonical SQL column name. Both resolve through
    ``canonical_filter_field``, so the returned ``Filter`` always carries
    the canonical column, which is what the whitelist validates against.
    """
    try:
        payload = cast(object, json.loads(raw))
    except json.JSONDecodeError as err:
        raise HTTPException(
            status_code=400, detail=f"filter is not valid JSON: {err}"
        ) from err
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="filter must be a JSON object")
    obj = cast(dict[str, object], payload)
    field = obj.get("field")
    op = obj.get("op")
    # The presence ops carry no operand; default a missing value to "". Gate on
    # ``isinstance`` first so an unhashable ``op`` (a JSON list/dict) fails the
    # 400 below instead of raising in the set membership test.
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
    if op not in _FILTER_OPS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown filter op {op!r}; expected one of {sorted(_FILTER_OPS)}",
        )
    canonical = canonical_filter_field(field)
    if canonical not in _filter_columns_for(kind):
        raise HTTPException(
            status_code=400,
            detail=f"unknown filter field {field!r} for {kind}",
        )
    if (
        presence_err := validate_presence_op(canonical, cast(FilterOp, op))
    ) is not None:
        raise HTTPException(status_code=400, detail=presence_err)
    # Cap the operand BEFORE compiling it: an over-long ``re`` / ``nre`` pattern
    # could drive catastrophic backtracking in ``re.compile`` itself.
    if len(value) > MAX_FILTER_VALUE_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"filter value exceeds {MAX_FILTER_VALUE_CHARS} characters",
        )
    if op in ("re", "nre"):
        try:
            re.compile(value)
        except re.error as err:
            raise HTTPException(
                status_code=400, detail=f"invalid regex {value!r}: {err}"
            ) from err
    return Filter(field=canonical, op=cast(FilterOp, op), value=value)
