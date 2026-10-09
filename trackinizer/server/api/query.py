"""Inquiry read/delete and change-log routes.

This module owns the canonical ``/api/inquiries/*`` and
``/api/change_log*`` surface, including the change-log SSE stream. The
web-facing SSE (``/api/web/subscribe``) and the search routes live in
``web.py`` instead.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import datetime
from functools import cache
from typing import TYPE_CHECKING, Annotated, Literal, cast, get_args, get_type_hints

import asyncio
import json
import logging
import time
import uuid

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import TypeAdapter

from trackinizer.lib.codec import from_plain, loads
from trackinizer.lib.postgres import DatabaseEngine
from trackinizer.server.api._deps import get_store, tag_kind, tag_row
from trackinizer.server.api._regex_guard import regex_failures_as_400
from trackinizer.server.api._routes_shared import (
    idempotency_key,
    parse_fields,
    parse_seq_ranges,
)
from trackinizer.server.api.locks import require_unlocked
from trackinizer.server.api.session_access import require_chat_opener_of
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.notify import iter_sse_events
from trackinizer.server.primitives import lookup_kinds
from trackinizer.types.change_log import Change, Snapshot
from trackinizer.types.columns import (
    flat_column_specs,
    storage_name,
)
from trackinizer.types.cost import Cost
from trackinizer.types.inquiries import (
    KIND_TO_CLASS,
    Inquiry,
)
from trackinizer.wire.bodies import ClaimNextIssue, FieldMutation
from trackinizer.wire.column_shapes import COLUMN_SHAPES, ColumnShape
from trackinizer.wire.filters import (
    IDENTITY_COLUMNS,
    VALUELESS_FILTER_OPS,
    Filter,
    FilterOp,
    canonical_filter_field,
)
from trackinizer.wire.json_types import MutableJSON, MutableJSONValue
from trackinizer.wire.routes import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    inquiry_relation_fields,
)
from trackinizer.wire.session_record_fields import SESSION_RECORD_FIELDS


if TYPE_CHECKING:
    from trackinizer.server.store.read import Ancestor


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
    """Next issue route.

    Args:
      request: FastAPI request object for middleware access.
      identity: Authenticated user identity, viewer-role-gated.

    Returns:
      result: The MutableJSON | None.

    """
    del identity
    return tag_kind(await get_store(request).next_issue())


# Declared here, beside its read-only twin and ahead of ``/api/inquiries/{kind}``:
# Starlette matches in registration order, so a later declaration would let the
# generic route swallow ``next_issue`` as a ``kind`` and 422 on it.
@router.post("/api/inquiries/next_issue")
async def claim_next_issue_route(
    req: ClaimNextIssue,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> MutableJSON | None:
    """Atomically select and claim the next available Issue.

    Unlike the ``GET`` twin, this reserves what it returns: selection and
    the owner write are one statement, so concurrent callers receive
    different issues instead of all receiving the first one and overwriting
    each other's claim.

    ``null`` means "nothing claimable right now", NOT "all work is
    finished" -- an eligible row may simply be locked by another in-flight
    claim, and a later request may succeed.

    Requires an ``Idempotency-Key``: the acquisition is a mutation, and a
    retry whose first attempt already committed must return that same issue
    rather than consuming a second one.

    Args:
      req: Claim body naming the new owner and the audit actor.
      request: FastAPI request object for middleware access.
      identity: Authenticated writer; enforced by Depends(require_role("writer")).

    Returns:
      result: The claimed Issue, or None when nothing is available.

    """
    if idempotency_key(request) is None:
        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key header is required to claim an issue",
        )
    return tag_kind(
        await get_store(request).claim_next_issue(
            owner=req.owner,
            api_key_id=identity.api_key_id,
            actor=req.actor or identity.email,
            reason=req.reason,
        ),
    )


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

    Args:
      ids: List of UUIDs to resolve; capped at MAX_LIST_LIMIT by FastAPI.
      request: FastAPI request object for middleware access.
      identity: Authenticated user identity, viewer-role-gated.

    Returns:
      body: Mapping with "found" (id->kind dict) and "missing" (unknown ids).

    """
    del identity
    async with get_store(request).engine.acquire() as conn:
        kinds = await lookup_kinds(conn, ids)
    found = {str(rid): kind for rid, kind in kinds.items()}
    missing = [str(rid) for rid in ids if rid not in kinds]
    body: MutableJSON = {"found": {**found}, "missing": [*missing]}
    return body


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
        list[str] | None,
        Query(alias="filter", max_length=MAX_LIST_LIMIT),
    ] = None,
    fields: Annotated[list[str] | None, Query(max_length=MAX_LIST_LIMIT)] = None,
    ancestors: Literal["narrows"] | None = None,
) -> list[MutableJSON]:
    """List inquiries across one or more ``kind`` query params.

    Results from every requested kind are concatenated, one block per
    DISTINCT kind: a repeated ``kind`` param yields one block, not several.
    ``limit`` and
    ``offset`` apply per kind, since each kind runs its own query. At
    least one ``kind`` is required. Each ``seq_range`` param is one
    inclusive ``a..b`` interval; their union selects rows across disjoint
    seq windows in a single query.

    Each ``fields`` param names one key every row keeps; the others are left
    out. A key only another kind's rows carry is absent from this kind's. The
    edges are read only when a relation (``narrows``, ``proved_by``, ...) is
    named. Without ``fields`` each row is whole.

    ``ancestors=narrows`` adds ``ancestors`` to each row, after ``fields``: the
    Issues it narrows, theirs, and so on up to the roots, nearest first, each
    as ``{id, kind, seq, title, status, child_ids}``. ``child_ids`` names which
    of the row and its listed ancestors narrow that one, so a row with several
    parents keeps them all. The walk stops 8 levels up and at 200 ancestors
    for the whole response, nearest first.

    Args:
      request: FastAPI request object for middleware access.
      identity: Authenticated user identity, viewer-role-gated.
      kind: Inquiry kinds to list; deduplicated before query.
      status: Optional status filter applied to each kind's query.
      limit: Rows per kind (validated in [1, MAX_LIST_LIMIT]).
      offset: Skip this many rows in each kind's result.
      seq_range: Inclusive seq intervals; union selects rows.
      filter_: JSON filter expressions; one per repeated param.
      fields: Row keys to send, one per repeated param; unset sends every key.
      ancestors: ``narrows`` adds each row's ``narrows`` ancestry; unset adds none.

    Returns:
      out: One JSON object per distinct kind, with inquiries for that kind.

    """
    del identity
    if limit < 1 or limit > MAX_LIST_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"limit must be in [1, {MAX_LIST_LIMIT}]",
        )
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0")
    # Inquiry ``seq`` starts at 1.
    seq_ranges = parse_seq_ranges(seq_range, min_seq=1)
    names = parse_fields(fields)
    edges = names is None or not names.isdisjoint(inquiry_relation_fields())
    listed: list[Inquiry] = []
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
                    edges=edges,
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
        listed.extend(rows)
    out = [
        tag_row(r)
        if names is None
        else {k: v for k, v in tag_row(r).items() if k in names}
        for r in listed
    ]
    if ancestors is not None:
        ancestry = await store.narrows_ancestors([r.id for r in listed])
        for row, payload in zip(listed, out, strict=True):
            entries: list[MutableJSONValue] = [
                _ancestor_json(a) for a in ancestry[row.id]
            ]
            payload["ancestors"] = entries
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
    """Cost route."""
    del identity
    return _require_found(await get_store(request).cost_for(target_id, deep=deep))


@router.get("/api/inquiries/{target_id}/proves_belief")
async def proves_belief_route(
    target_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> list[MutableJSON]:
    """Proves belief route."""
    del identity
    rows = await get_store(request).proves_belief(target_id)
    return [tag_row(r) for r in rows]


@router.get("/api/inquiries/{target_id}/confidence")
async def confidence_route(
    target_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> dict[str, float]:
    """Return the derived confidence of a Belief/Experiment, 404 if absent."""
    del identity
    confidence = await get_store(request).confidence_for(target_id)
    return {"confidence": _require_found(confidence)}


@router.get("/api/inquiries/{target_id}/authority")
async def authority_route(
    target_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> dict[str, float]:
    """Return a row's derived load-bearing authority scores, 404 if absent."""
    del identity
    return _require_found(await get_store(request).authority_for(target_id))


@router.get("/api/inquiries/{kind}/{seq}")
async def by_seq_route(
    kind: Inquiry.InquiryKind,
    seq: int,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> MutableJSON:
    """Resolve a short-ref ``kind#seq`` to the full inquiry.

    Args:
      kind: Inquiry kind (Issue, Belief, etc.).
      seq: Per-kind sequence number starting at 1.
      request: FastAPI request object for middleware access.
      identity: Authenticated user identity, viewer-role-gated.

    Returns:
      result: Full inquiry record tagged with kind and seq.

    """
    del identity
    async with get_store(request).engine.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM inquiries WHERE kind = $1 AND seq = $2",
            kind,
            seq,
        )
        if row is None:
            raise HTTPException(status_code=404, detail=f"{kind}#{seq} not found")
        target_id = cast(uuid.UUID, row["id"])
    return _require_found(tag_kind(await get_store(request).get_inquiry(target_id)))


@router.get("/api/inquiries/{target_id}")
async def get_inquiry_route(
    target_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> MutableJSON:
    """Get inquiry route."""
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
    row must first release its owner through the compare-and-set owner route. A
    science chat is the exception: it is public and permanent, so only the key that
    opened it may purge it.

    Args:
      target_id: UUID of the inquiry to delete.
      req: Mutation body (actor, reason).
      request: FastAPI request object for middleware access.
      identity: Authenticated user identity, writer-role-gated.

    Returns:
      result: Mapping with "id" (inquiry UUID) and "change_id" (purge operation).

    """
    await require_unlocked(request, identity, [target_id], include_peers=True)
    await require_chat_opener_of(request, identity, target_id)
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

    Args:
      request: FastAPI request object for middleware access.
      identity: Authenticated user identity, viewer-role-gated.

    Returns:
      result: Server-sent events stream emitting change ids.

    """
    del identity
    app_obj = cast(FastAPI, request.app)
    state_data = cast(
        dict[str, object],
        object.__getattribute__(app_obj.state, "_state"),
    )
    engine = cast(DatabaseEngine, state_data["engine"])
    return StreamingResponse(iter_sse_events(engine), media_type="text/event-stream")


@router.get("/api/change_log/{change_id}")
async def get_change_route(
    change_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> Change:
    """Get change route."""
    del identity
    change = await get_store(request).get_change(change_id)
    if change is None:
        raise HTTPException(status_code=404, detail="change not found")
    return change


@router.get("/api/change_log", response_model=list[Change])
async def list_change_log_route(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    *,
    since: datetime | None = None,
    after_id: uuid.UUID | None = None,
    actor: Inquiry.Actor | None = None,
    subject_id: uuid.UUID | None = None,
    subject_kind: Inquiry.InquiryKind | None = None,
    kind: Annotated[list[Change.Kind] | None, Query(max_length=MAX_LIST_LIMIT)] = None,
    # The change-log slice keeps its own, larger default; the inquiry-list
    # default and cap come from the shared wire contract.
    limit: int = 200,
    brief: bool = False,
) -> list[Change] | Response:
    """Return a filtered, newest-first slice of the change log.

    ``kind`` repeats, matching any of its values. Every filter runs before
    ``limit``, so a page of other kinds never hides a matching one behind it.

    ``brief`` sends each snapshot (``old``, ``new``) with its set keys alone,
    and its free text (``title``, ``description``, ...) cut to the first 32
    characters: enough to tell a set value from an unset one. Ids, statuses and
    every other value stay whole.

    Args:
      request: FastAPI request object for middleware access.
      identity: Authenticated user identity, viewer-role-gated.
      since: Minimum timestamp (inclusive); defaults to earliest.
      after_id: Only rows with id > after_id; used for pagination.
      actor: Exact match on the change's actor email.
      subject_id: UUID of the changed inquiry.
      subject_kind: Inquiry kind of the changed subject.
      kind: Types of change (field edit, edge mutation, etc.) to keep.
      limit: Maximum rows returned (validated in [1, MAX_LIST_LIMIT]).
      brief: Drop unset snapshot keys and cut snapshot text short.

    Returns:
      result: Newest-first ordered list of changes matching the filters.

    """
    del identity
    if limit < 1 or limit > MAX_LIST_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"limit must be in [1, {MAX_LIST_LIMIT}]",
        )
    changes = await get_store(request).list_changes(
        since=since,
        after_id=after_id,
        actor=actor,
        subject_id=subject_id,
        subject_kind=subject_kind,
        kinds=kind or (),
        limit=limit,
    )
    if not brief:
        return changes
    # Returned as a response, not rows: ``response_model`` would validate each
    # row back into a ``Change`` and put every dropped key back as null.
    return JSONResponse([_brief_change(change) for change in changes])


# Derived from ``flat_column_specs`` so the whitelist tracks the Inquiry hierarchy
# automatically, including the flattened ``marginal_cost_*`` axes. Each flat column maps
# through :func:`storage_name`, since ``canonical_filter_field`` resolves a filter to
# its storage column (``priority`` -> ``issue_priority``) and the whitelist validates
# the canonical name.
def _filter_columns_for(kind: Inquiry.InquiryKind) -> frozenset[str]:
    """Return the canonical SQL column names a ``Filter`` may target for ``kind``."""
    cls = KIND_TO_CLASS[kind]
    declared = {
        storage_name(name, flat.spec)
        for source in (Inquiry, cls)
        for name, flat in flat_column_specs(source).items()
    }
    # IR record kinds are filterable on the one kind that HAS records. They
    # carry no ColumnSpec (their values live in ``session_records``), so the
    # spec walk cannot see them.
    records = SESSION_RECORD_FIELDS if kind == "AgentSession" else ()
    # Parents answered from ``edges`` (``narrows``), on the kinds whose rows have
    # that relation. No ColumnSpec either.
    own = {f.name for f in fields(cls)}
    parents = {
        column
        for column, shape in COLUMN_SHAPES.items()
        if shape is ColumnShape.PARENT and column in own
    }
    return IDENTITY_COLUMNS | frozenset(declared) | frozenset(records) | parents


# ``field`` may arrive as a CLI-friendly alias (``kind``, ``agent-cost``, ``result``,
# ...) or the canonical SQL column name. Both resolve through
# ``canonical_filter_field``, so the returned ``Filter`` always carries the canonical
# column, which is what the whitelist validates against.
def _parse_filter_param(raw: str, kind: Inquiry.InquiryKind) -> Filter:
    """Decode one ``filter=<json>`` query param, raising 400 on bad input."""
    try:
        payload = loads(raw)
    except json.JSONDecodeError as err:
        raise HTTPException(
            status_code=400,
            detail=f"filter is not valid JSON: {err}",
        ) from err
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="filter must be a JSON object")
    obj = from_plain(payload, dict[str, object])
    field = obj.get("field")
    op = obj.get("op")
    # The presence ops carry no operand; default a missing value to "". Gate on
    # ``isinstance`` first so an unhashable ``op`` (a JSON list/dict) fails the
    # 400 below instead of raising in the set membership test.
    # A presence op carries no operand, so a MISSING value is the well-formed
    # spelling and defaults to "". Supplying one anyway is refused by
    # ``validate_clause`` below, like every other rule about the clause.
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


def _ancestor_json(ancestor: Ancestor) -> MutableJSON:
    """Serialize one ``narrows`` ancestor for the list response."""
    return {
        "id": str(ancestor.id),
        "kind": ancestor.kind,
        "seq": ancestor.seq,
        "title": ancestor.title,
        "status": ancestor.status,
        "child_ids": [str(child) for child in ancestor.child_ids],
    }


def _brief_change(change: Change) -> dict[str, object]:
    """Serialize ``change`` with unset snapshot keys dropped and snapshot text cut."""
    # Pydantic's JSON dump is Any; convert narrows the runtime shape below.
    dumped = cast(object, _change_adapter().dump_python(change, mode="json"))
    row = from_plain(dumped, dict[str, object])
    text = _snapshot_text_fields()
    for side in ("old", "new"):
        row[side] = {
            key: value[:32] if key in text and isinstance(value, str) else value
            for key, value in from_plain(row[side], dict[str, object]).items()
            if value is not None
        }
    return row


# Serializes as ``response_model`` does, so a brief row differs from a whole one only
# in its snapshots: the same keys, and times in the same format.
@cache
def _change_adapter() -> TypeAdapter[Change]:
    return TypeAdapter(Change)


# Typed ``str | None``: the free text, as opposed to ids, enums, actors, numbers and
# lists, which a brief row keeps whole so a client can still match and name them.
@cache
def _snapshot_text_fields() -> frozenset[str]:
    hints = get_type_hints(Snapshot)
    return frozenset(f.name for f in fields(Snapshot) if str in get_args(hints[f.name]))


# A read addressing a specific id (inquiry, short-ref, cost) that finds no row is 404,
# not a 200 with a null body -- consistent with ``get_change`` / ``get_edge``
# (API-08/24).
def _require_found[T](value: T | None) -> T:
    """Return ``value`` or raise 404 when it is ``None``."""
    if value is None:
        raise HTTPException(status_code=404, detail="not found")
    return value
