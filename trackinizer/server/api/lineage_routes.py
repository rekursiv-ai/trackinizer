"""Read bounded, typed relationship pages without requiring campaign launch metadata."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from trackinizer.server.api._deps import get_store
from trackinizer.server.api.timeline_routes import TimelineRecord
from trackinizer.server.auth import require_role
from trackinizer.server.visuals.lineage import load_lineage_page
from trackinizer.types.edges import Edge
from trackinizer.types.inquiries import Inquiry


router = APIRouter()


class LineageEdge(BaseModel):
    """One stored relationship, with its direction and signed annotation."""

    model_config = ConfigDict(extra="forbid")

    from_id: UUID
    to_id: UUID
    edge_kind: Edge.Kind
    valence: float | None = Field(ge=-1, le=1)
    note: str | None = Field(max_length=2_000)


class LineagePage(BaseModel):
    """A paged neighborhood whose selected record is outside the peer quota."""

    model_config = ConfigDict(extra="forbid")

    target: TimelineRecord
    records: list[TimelineRecord] = Field(max_length=50)
    edges: list[LineageEdge] = Field(max_length=350)
    next_cursor: str | None


@router.get(
    "/api/visuals/lineage/{record_id}",
    dependencies=[Depends(require_role("viewer"))],
)
async def lineage_page_route(
    record_id: UUID,
    *,
    request: Request,
    direction: Literal["in", "out"] = "in",
    edge_kind: Annotated[list[Edge.Kind] | None, Query()] = None,
    peer_kind: Inquiry.InquiryKind | None = None,
    page_size: Annotated[int, Query(ge=1, le=50)] = 20,
    cursor: Annotated[str, Query(max_length=1_024)] = "",
) -> LineagePage:
    """Read typed research links without requiring a launch or a single parent.

    Args:
      record_id: The selected inquiry UUID.
      request: Request carrying the Store.
      direction: Read incoming or outgoing stored edges.
      edge_kind: Relationship kinds, defaulting to taxonomy and provenance.
      peer_kind: Restrict peers to one inquiry kind.
      page_size: Distinct peer quota, at most fifty.
      cursor: Continuation bound to this record and these filters.

    Returns:
      page: Selected record, peer records, directed edges and continuation.

    """
    store = get_store(request)
    try:
        async with store.engine.acquire() as conn:
            result = await load_lineage_page(
                conn,
                target_id=record_id,
                direction=direction,
                edge_kinds=tuple(edge_kind)
                if edge_kind is not None
                else ("narrows", "produced_by"),
                peer_kind=peer_kind,
                page_size=page_size,
                cursor=cursor,
            )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if result is None:
        raise HTTPException(status_code=404, detail="Record not found")
    return LineagePage.model_validate(result)
