"""Inquiry read/delete and change-log routes.

This module owns the canonical ``/api/inquiries/*`` and
``/api/change_log*`` surface, including the change-log SSE stream. The
web-facing SSE (``/api/web/subscribe``) and the search routes live in
``web.py`` instead.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, cast

import asyncio
import json
import logging
import time
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from trackinizer.lib.custom_json import MutableJSON
from trackinizer.lib.postgres import DatabaseEngine
from trackinizer.server.api._deps import get_store, tag_kind, tag_row
from trackinizer.server.api._regex_guard import regex_failures_as_400
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
    IDENTITY_COLUMNS,
    VALUELESS_FILTER_OPS,
    Filter,
    FilterOp,
    canonical_filter_field,
)
from trackinizer.wire.routes import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
)


_FILTER_OPS: frozenset[str] = frozenset(FILTER_OPS)

# Identity/housekeeping columns the schema declares directly: not editable,
# carry no ColumnSpec, and so aren't surfaced by flat_column_specs. The
# flattened marginal_cost_* axes are not listed here; they come from
# flat_column_specs like every other spec'd column.
#
# Imported rather than re-listed: ``filters`` needs the same five for its
# NOT-NULL derivation, and two hand-written copies of one schema fact drift
# the moment a sixth column is declared. ``column_shapes.COLUMN_SHAPES`` and
# ``grammar._IDENTITY_FILTER_COLUMNS`` are deliberately NOT unified with
# this: the first maps each column to a SQL shape, and the second omits
# ``kind`` because the CLI names kinds positionally rather than filtering on
# them. Same members today, different questions.

router = APIRouter()
_logger = logging.getLogger(__name__)


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

    ``max_length`` is a typed cap on the decoded list's LENGTH: FastAPI
    parses the whole body first, then rejects an oversize list with 422. It
    bounds how many ids a handler will look up, NOT how many bytes the server
    will read -- a 4GB body measured 30.77s of buffering before its 422.
    There is NO byte bound in this application: counting bytes in an ASGI
    ``receive`` does not stop a chunked sender (measured against both a
    hand-rolled middleware and Starlette's own ``max_body_size``: 50MB
    consumed against a 1MB limit). A real bound belongs to whatever owns the
    socket -- uvicorn or the reverse proxy. (REV-OPUS-30 recorded this cap;
    its "pre-decode" claim was wrong.) The response
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
    kind: Annotated[list[Inquiry.InquiryKind], Query(max_length=MAX_LIST_LIMIT)],
    status: Inquiry.Status | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
    seq_range: Annotated[list[str] | None, Query(max_length=MAX_LIST_LIMIT)] = None,
    filter_: Annotated[
        list[str] | None, Query(alias="filter", max_length=MAX_LIST_LIMIT)
    ] = None,
) -> list[MutableJSON]:
    """List inquiries across one or more ``kind`` query params.

    Results from every requested kind are concatenated, one block per
    DISTINCT kind: a repeated ``kind`` param yields one block, not several.
    ``limit`` and
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
    # Dedup before iterating: only nine kinds exist, so a repeated param can
    # only re-run a query whose answer is already in hand. Without this, 200
    # copies of ``kind=Issue`` ran 200 queries and returned 9.3MB where one
    # copy returns 46KB. ``dict.fromkeys`` keeps the caller's order.
    for one_kind in dict.fromkeys(kind):
        # Parsed per kind because the field whitelist is kind-specific, but a
        # ``re`` operand still reaches ``re.compile`` once per (kind, filter)
        # pair. The dedup above bounds one factor; ``max_length`` on the param
        # bounds the other.
        filters = tuple(_parse_filter_param(raw, one_kind) for raw in (filter_ or ()))
        started = time.perf_counter()
        rows: list[Inquiry] = []
        outcome = "success"
        error_type = ""
        try:
            # A ``re`` filter lowers to a Postgres ``~``, so this query can
            # still fail two ways: a pattern POSIX rejects for a reason the
            # wire type's dialect gate does not enumerate, and a pattern that
            # matches for an unbounded time. ``regex_failures_as_400`` reports
            # both as the caller errors they are. The statement timeout that
            # bounds the second is set inside ``list_kind``, which owns the
            # connection -- taking one here as well would be a reentrant
            # acquire.
            with regex_failures_as_400():
                rows = await store.list_kind(
                    one_kind,
                    status=status,
                    limit=limit,
                    offset=offset,
                    seq_ranges=seq_ranges,
                    filters=filters,
                )
        except asyncio.CancelledError:
            outcome = "cancelled"
            error_type = "CancelledError"
            raise
        except BaseException as error:
            outcome = "failure"
            error_type = type(error).__name__
            raise
        finally:
            duration_sec = time.perf_counter() - started
            request_id = str(getattr(request.state, "request_id", ""))
            _logger.info(
                "event=trackinizer_query_completed stage=list_inquiries "
                "outcome=%s kind=%s filter_count=%d returned_rows=%d "
                "duration_sec=%.6f request_id=%s error_type=%s",
                outcome,
                one_kind,
                len(filters),
                len(rows),
                duration_sec,
                request_id,
                error_type,
                extra={
                    "event": "trackinizer_query_completed",
                    "stage": "list_inquiries",
                    "outcome": outcome,
                    "kind": one_kind,
                    "filter_count": len(filters),
                    "returned_rows": len(rows),
                    "duration_sec": duration_sec,
                    "request_id": request_id,
                    "error_type": error_type,
                },
            )
        out.extend(tag_row(r) for r in rows)
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
    return [tag_row(r) for r in rows]


@router.get("/api/inquiries/{kind}/{seq}")
async def by_seq_route(
    kind: Inquiry.InquiryKind,
    seq: int,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> MutableJSON:
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
    are a shared workspace, so any writer may purge an unowned row. A claimed
    row must first release its owner through the compare-and-set owner route.
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
    # The change-log slice keeps its own, larger default; the inquiry-list
    # default and cap come from the shared wire contract.
    limit: int = 200,
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
    return IDENTITY_COLUMNS | frozenset(declared)


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
    if valueless and obj.get("value", "") != "":
        # Accepting and ignoring it would answer a question the caller did not
        # ask: ``{"op": "isnull", "value": "Dan"}`` reads as "owner is Dan".
        raise HTTPException(status_code=400, detail=f"filter op {op!r} takes no value")
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
    try:
        # Every rule decidable from the clause alone -- the length cap, the
        # ambiguous-escape gate, whether Python can compile it, the dialect
        # gate, and the presence-op check -- lives on the wire type, so the CLI
        # cannot construct a filter this route would have refused, and a copy
        # here could only drift. That drift was real: the presence check ran
        # HERE only, so a direct ``Filter`` and the store both accepted
        # ``isnull`` on a NOT-NULL column, which matches nothing.
        #
        # Whether the op is admissible for the COLUMN's SQL is still not
        # decidable here: that needs the store's own table, and
        # ``_partition_filters`` asks it.
        return Filter(field=canonical, op=cast(FilterOp, op), value=value)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
