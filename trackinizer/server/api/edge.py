"""Routes for reading and mutating edges between inquiries.

An edge's identity (``from_id``, ``edge_kind``, ``to_id``) lives in the
URL path, so bodies carry only the mutation payload. Per-field set and
unset reuse the inquiry field bodies (``FieldSet``, ``FieldMutation``),
and add/sub on ``labels`` reuses ``FieldOp``. The scalar annotations
``priority``, ``note``, and ``valence`` accept ``PUT`` to set and
``DELETE`` to clear, but not ``PATCH``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Annotated, Literal, cast

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from trackinizer.lib.custom_json import MutableJSON
from trackinizer.server.api._deps import get_store
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.store.core import Store
from trackinizer.types.edges import Edge
from trackinizer.types.errors import ConflictError, ValidationError
from trackinizer.types.inquiries import Issue
from trackinizer.wire.bodies import (
    ActorMixin,
    FieldMutation,
    FieldOp,
    FieldSet,
)
from trackinizer.wire.edge_bodies import (
    CreateEdge,
    CreateEdgeBatch,
)
from trackinizer.wire.routes import (
    EdgeFieldRoute,
    edge_field_path,
    edge_field_routes,
)


router = APIRouter()


@router.get("/api/edges/{from_id}/{edge_kind}/{to_id}")
async def get_edge_route(
    from_id: uuid.UUID,
    edge_kind: Edge.Kind,
    to_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> MutableJSON | None:
    """Get edge route.

    Args:
      from_id: UUID of the source inquiry.
      edge_kind: Type of edge (proves, favors, cites_paper, etc.).
      to_id: UUID of the target inquiry.
      request: FastAPI request object for middleware access.
      identity: Authenticated user identity, viewer-role-gated.

    Returns:
      result: The MutableJSON | None.

    """
    del identity
    edge = await get_store(request).get_edge(
        from_id=from_id, to_id=to_id, edge_kind=edge_kind
    )
    if edge is None:
        raise HTTPException(status_code=404, detail="edge not found")
    return _edge_json(edge)


@router.post("/api/edges/batch")
async def create_edge_batch_route(
    req: CreateEdgeBatch,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> MutableJSON:
    """Create many edges in one round-trip, reporting per-item success.

    Args:
      req: Batch request body (items list).
      request: FastAPI request object for middleware access.
      identity: Authenticated user identity, writer-role-gated.

    Returns:
      response: JSON with "ok" flag and per-item success/error list.

    """
    store = get_store(request)
    items: list[_EdgeBatchSuccess | _EdgeBatchFailure] = []
    for index, item in enumerate(req.items):
        try:
            await store.add_edge(
                from_id=item.from_id,
                to_id=item.to_id,
                edge_kind=item.edge_kind,
                priority=item.priority,
                note=item.note,
                valence=item.valence,
                labels=item.labels or (),
                reason=item.reason,
                api_key_id=identity.api_key_id,
                actor=_actor_of(item, identity),
            )
        except Exception as err:  # noqa: BLE001 -- batch response preserves partial success context.
            items.append(_EdgeBatchFailure(index=index, error=_safe_batch_error(err)))
            items.extend(
                _EdgeBatchFailure(index=skipped, error="skipped after earlier failure")
                for skipped in range(index + 1, len(req.items))
            )
            return {"ok": False, "items": [r.model_dump() for r in items]}
        items.append(_EdgeBatchSuccess())
    return {"ok": True, "items": [r.model_dump() for r in items]}


@router.post("/api/edges/{from_id}/{edge_kind}/{to_id}")
async def create_edge_route(
    from_id: uuid.UUID,
    edge_kind: Edge.Kind,
    to_id: uuid.UUID,
    req: CreateEdge,
    request: Request,
    *,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> MutableJSON:
    """Create edge route.

    Args:
      from_id: UUID of the source inquiry.
      edge_kind: Type of edge (proves, favors, cites_paper, etc.).
      to_id: UUID of the target inquiry.
      req: Edge creation body (priority, note, valence, labels).
      request: FastAPI request object for middleware access.
      identity: Authenticated user identity, writer-role-gated.

    Returns:
      response: JSON with edge mutation result (change_id, created flag).

    """
    store = get_store(request)
    change_id, created = await store.add_edge(
        from_id=from_id,
        to_id=to_id,
        edge_kind=edge_kind,
        priority=req.priority,
        note=req.note,
        valence=req.valence,
        labels=req.labels or (),
        reason=req.reason,
        api_key_id=identity.api_key_id,
        actor=_actor_of(req, identity),
    )
    return _edge_response(change_id, created=created)


@router.delete("/api/edges/{from_id}/{edge_kind}/{to_id}")
async def delete_edge_route(
    from_id: uuid.UUID,
    edge_kind: Edge.Kind,
    to_id: uuid.UUID,
    req: FieldMutation,
    request: Request,
    *,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> MutableJSON:
    """Delete edge route.

    Args:
      from_id: UUID of the source inquiry.
      edge_kind: Type of edge (proves, favors, cites_paper, etc.).
      to_id: UUID of the target inquiry.
      req: Mutation body (actor, reason).
      request: FastAPI request object for middleware access.
      identity: Authenticated user identity, writer-role-gated.

    Returns:
      response: JSON with edge mutation result (change_id, created flag).

    """
    store = get_store(request)
    change_id = await store.remove_edge(
        from_id=from_id,
        to_id=to_id,
        edge_kind=edge_kind,
        reason=req.reason,
        api_key_id=identity.api_key_id,
        actor=_actor_of(req, identity),
    )
    return _edge_response(change_id)


@router.patch("/api/edges/{from_id}/{edge_kind}/{to_id}/labels")
async def patch_edge_labels_route(
    from_id: uuid.UUID,
    edge_kind: Edge.Kind,
    to_id: uuid.UUID,
    req: FieldOp[str],
    request: Request,
    *,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> MutableJSON:
    """Add or remove one label on an edge, the only ``PATCH``-able annotation.

    Args:
      from_id: UUID of the source inquiry.
      edge_kind: Type of edge (proves, favors, cites_paper, etc.).
      to_id: UUID of the target inquiry.
      req: Mutation body (op: "add"|"remove", value, reason).
      request: FastAPI request object for middleware access.
      identity: Authenticated user identity, writer-role-gated.

    Returns:
      response: JSON with edge mutation result (change_id, created flag).

    """
    store = get_store(request)
    method = store.add_edge_label if req.op == "add" else store.remove_edge_label
    change_id = await method(
        from_id=from_id,
        to_id=to_id,
        edge_kind=edge_kind,
        label=req.value,
        reason=req.reason,
        api_key_id=identity.api_key_id,
        actor=_actor_of(req, identity),
    )
    return _edge_response(change_id)


# ``value`` is already validated against the column type by the typed ``FieldSet[T]``
# body bound per field. Passing it through the matching per-column keyword keeps the
# Store call type-correct.
async def _set_edge_annotation(
    store: Store,
    field: str,
    value: object,
    *,
    from_id: uuid.UUID,
    to_id: uuid.UUID,
    edge_kind: Edge.Kind,
    reason: str = "",
    api_key_id: uuid.UUID | None,
    actor: str,
) -> uuid.UUID | None:
    """Set one annotation column; the rest stay absent."""
    if field == "priority":
        return await store.set_edge_annotation(
            from_id=from_id,
            to_id=to_id,
            edge_kind=edge_kind,
            priority=cast(Issue.Priority | None, value),
            reason=reason,
            api_key_id=api_key_id,
            actor=actor,
        )
    if field == "note":
        return await store.set_edge_annotation(
            from_id=from_id,
            to_id=to_id,
            edge_kind=edge_kind,
            note=cast(str | None, value),
            reason=reason,
            api_key_id=api_key_id,
            actor=actor,
        )
    if field == "valence":
        return await store.set_edge_annotation(
            from_id=from_id,
            to_id=to_id,
            edge_kind=edge_kind,
            valence=cast(float | None, value),
            reason=reason,
            api_key_id=api_key_id,
            actor=actor,
        )
    if field == "labels":
        return await store.set_edge_annotation(
            from_id=from_id,
            to_id=to_id,
            edge_kind=edge_kind,
            labels=cast(Sequence[str] | None, value),
            reason=reason,
            api_key_id=api_key_id,
            actor=actor,
        )
    # Every ``edge_field_routes`` column maps to one ``set_edge_annotation``
    # kwarg above; a new annotation column without a branch here would
    # silently fall through to the labels write. Fail loudly instead.
    raise HTTPException(
        status_code=500,
        detail=f"no edge-annotation setter wired for field {field!r}",
    )


def _edge_json(edge: Edge) -> MutableJSON:
    """Serialize an ``Edge`` as flat JSON."""
    return {
        "from_id": str(edge.from_id),
        "from_kind": edge.from_kind,
        "to_id": str(edge.to_id),
        "to_kind": edge.to_kind,
        "edge_kind": edge.edge_kind,
        "priority": edge.priority,
        "note": edge.note,
        "valence": edge.valence,
        "labels": None if edge.labels is None else list(edge.labels),
    }


# ``change_id`` is ``None`` for a no-op. ``created`` distinguishes a brand-new edge from
# an upserted (annotation-applied) existing one, so the CLI can echo "added" vs
# "annotated" without a second round-trip.
def _edge_response(
    change_id: uuid.UUID | None, *, created: bool = False
) -> MutableJSON:
    """Build the edge-mutation response."""
    return {
        "change_id": None if change_id is None else str(change_id),
        "created": created,
    }


class _EdgeBatchSuccess(BaseModel):
    ok: Literal[True] = True


class _EdgeBatchFailure(BaseModel):
    ok: Literal[False] = False
    index: int
    error: str


def _actor_of(body: ActorMixin, identity: AuthIdentity) -> str:
    return body.actor or identity.email


# A :class:`ConflictError` (cycle, bad target kind) or :class:`ValidationError` (self-
# loop, priority on a non-priority kind) carries an author-written, sanitized message,
# so it is surfaced verbatim. Anything else -- notably a raw asyncpg violation whose
# ``DETAIL`` would leak internal column / constraint names -- collapses to a generic
# message; the per-item context (index) still tells the caller which item failed without
# exposing server internals.
def _safe_batch_error(err: Exception) -> str:
    """Client-safe message for one failed batch item."""
    if isinstance(err, (ConflictError, ValidationError)):
        return str(err)
    return "edge could not be created"


def _make_edge_put(route: EdgeFieldRoute) -> Callable[..., Awaitable[MutableJSON]]:
    """Build the typed ``PUT`` handler that sets one edge annotation field."""

    async def handler(
        from_id: uuid.UUID,
        edge_kind: Edge.Kind,
        to_id: uuid.UUID,
        body: FieldSet[object],
        request: Request,
        *,
        identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
    ) -> MutableJSON:
        store = get_store(request)
        change_id = await _set_edge_annotation(
            store,
            route.column,
            body.value,
            from_id=from_id,
            to_id=to_id,
            edge_kind=edge_kind,
            reason=body.reason,
            api_key_id=identity.api_key_id,
            actor=_actor_of(body, identity),
        )
        return _edge_response(change_id)

    handler.__name__ = f"set_edge_{route.column}_route"
    handler.__qualname__ = handler.__name__
    handler.__annotations__["body"] = FieldSet[route.value_type]  # ty: ignore[invalid-type-form] -- see `api/edit.py::_make_put`: the pydantic subscript is load-bearing
    return handler


def _make_edge_delete(route: EdgeFieldRoute) -> Callable[..., Awaitable[MutableJSON]]:
    """Build the ``DELETE`` handler that clears one edge annotation field."""

    async def handler(
        from_id: uuid.UUID,
        edge_kind: Edge.Kind,
        to_id: uuid.UUID,
        body: FieldMutation,
        request: Request,
        *,
        identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
    ) -> MutableJSON:
        store = get_store(request)
        change_id = await _set_edge_annotation(
            store,
            route.column,
            None,
            from_id=from_id,
            to_id=to_id,
            edge_kind=edge_kind,
            reason=body.reason,
            api_key_id=identity.api_key_id,
            actor=_actor_of(body, identity),
        )
        return _edge_response(change_id)

    handler.__name__ = f"unset_edge_{route.column}_route"
    handler.__qualname__ = handler.__name__
    return handler


def _register_edge_field_routes() -> None:
    """Attach one PUT and one DELETE per annotation column to ``router``."""
    for edge_route in edge_field_routes():
        path = edge_field_path(edge_route.column)
        router.put(path)(_make_edge_put(edge_route))
        router.delete(path)(_make_edge_delete(edge_route))


_register_edge_field_routes()
