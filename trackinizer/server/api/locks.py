"""Keep a locked inquiry changeable by an admin alone.

An admin sets ``inquiries.locked`` through ``PUT /api/admin/inquiries/{id}/lock``.
While it holds, nobody else edits the row's fields, adds or removes its edges, or
deletes it; the routes that write call :func:`require_unlocked` with the rows they
are about to change, and a locked one answers 403. An edge belongs to both its
endpoints, so an edge write is refused when either end is locked, and a purge when
the row is linked to a locked one (the purge deletes that edge).

Setting the lock records no change: it adds no change kind, and leaves ``modified``
alone so that locking the rules Issue does not move the rules version. Who set or
cleared it, and when, goes to ``inquiry_lock_log``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, cast

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from trackinizer.lib.codec import from_plain
from trackinizer.server.api._deps import get_store
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.wire.json_types import MutableJSON


if TYPE_CHECKING:
    from collections.abc import Collection, Sequence


__all__ = [
    "LockBody",
    "referenced_ids",
    "require_unlocked",
    "set_lock_route",
]


router = APIRouter()


class LockBody(BaseModel):
    """Request body for ``PUT /api/admin/inquiries/{id}/lock``."""

    locked: bool


async def require_unlocked(
    request: Request,
    identity: AuthIdentity,
    row_ids: Collection[uuid.UUID],
    *,
    include_peers: bool = False,
) -> None:
    """Refuse a write that touches a locked row unless the caller is an admin.

    Args:
      request: The request, which carries the store.
      identity: Authenticated caller; an admin passes without a query.
      row_ids: The rows the route is about to change.
      include_peers: Also refuse when a row is linked by an edge to a locked one,
        for a write that deletes the row's edges.

    Raises:
      HTTPException: 403 when a named row, or with ``include_peers`` a linked one,
        is locked and the caller is not an admin.

    """
    ids = list(row_ids)
    if identity.role == "admin" or not ids:
        return
    async with get_store(request).engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT kind, seq FROM inquiries WHERE locked AND (id = ANY($1) OR ($2 "
            "AND id IN (SELECT to_id FROM edges WHERE from_id = ANY($1) "
            "UNION SELECT from_id FROM edges WHERE to_id = ANY($1)))) "
            "ORDER BY created, id LIMIT 1",
            ids,
            include_peers,
        )
    row = next(iter(rows), None)
    if row is not None:
        raise HTTPException(
            status_code=403,
            detail=(
                f"{from_plain(row['kind'], str)} #{from_plain(row['seq'], int)} "
                "is locked; only an admin may change it"
            ),
        )


def referenced_ids(body: object) -> set[uuid.UUID]:
    """Collect the row ids a submit body names, except its idempotency keys.

    Every UUID in a submit body but ``idempotency_key`` is the id of an existing
    row: an Issue's parents and prerequisites, an Experiment's code changes, a
    Belief's citations, a batch edge's endpoints.

    Args:
      body: A submit body, or a value inside one.

    Returns:
      ids: Each referenced row id.

    """
    if isinstance(body, uuid.UUID):
        return {body}
    if isinstance(body, BaseModel):
        fields: list[object] = [
            getattr(body, name)
            for name in type(body).model_fields
            if name != "idempotency_key"
        ]
        return referenced_ids(fields)
    if isinstance(body, (list, tuple)):
        items = cast("Sequence[object]", body)
        return {found for item in items for found in referenced_ids(item)}
    return set()


@router.put("/api/admin/inquiries/{target_id}/lock")
async def set_lock_route(
    target_id: uuid.UUID,
    body: LockBody,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("admin"))],
) -> MutableJSON:
    """Lock or unlock one inquiry; admin only.

    Args:
      target_id: The inquiry to lock or unlock.
      body: Whether it is locked from now on.
      request: The request, which carries the store.
      identity: Authenticated admin.

    Returns:
      result: The inquiry's ``id`` and its ``locked`` flag.

    """
    async with get_store(request).engine.acquire() as conn:
        locked = await conn.fetchval(
            "WITH changed AS (UPDATE inquiries SET locked = $2 WHERE id = $1 "
            "RETURNING locked), logged AS (INSERT INTO inquiry_lock_log "
            "(inquiry_id, locked, actor) SELECT $1, locked, $3 FROM changed) "
            "SELECT locked FROM changed",
            target_id,
            body.locked,
            identity.email,
        )
    if locked is None:
        raise HTTPException(status_code=404, detail="inquiry not found")
    return {"id": str(target_id), "locked": from_plain(locked, bool)}
