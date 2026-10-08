"""Read a bounded lineage and timeline of any record for the timeline visual."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from trackinizer.lib.codec import from_plain
from trackinizer.server.api._deps import get_store
from trackinizer.server.api.visuals_routes import visual_catalog
from trackinizer.server.auth import require_role
from trackinizer.server.visuals.timeline import load_timeline
from trackinizer.types.inquiries import Inquiry


if TYPE_CHECKING:
    from trackinizer.server.visuals.catalog import ParameterDescription


router = APIRouter()


class TimelineRecord(BaseModel):
    """Bounded record projection shared by timeline rows and citations."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    kind: Inquiry.InquiryKind
    seq: int
    title: str = Field(max_length=2_000)
    status: str
    created: datetime
    modified: datetime
    description: str | None = Field(max_length=2_000)
    outcome: str | None = Field(max_length=2_000)


class TimelineEvidence(BaseModel):
    """One signed citation and its linked claim row."""

    model_config = ConfigDict(extra="forbid")

    claim: TimelineRecord
    edge_kind: Literal["proves", "favors"]
    valence: float | None = Field(ge=-1, le=1)
    note: str | None = Field(max_length=2_000)


class TimelineExperiment(BaseModel):
    """One Experiment and its capped signed citations."""

    model_config = ConfigDict(extra="forbid")

    record: TimelineRecord
    evidence: list[TimelineEvidence]
    evidence_truncated: bool


class TimelineDirection(BaseModel):
    """One direct Issue direction and its capped results."""

    model_config = ConfigDict(extra="forbid")

    issue: TimelineRecord
    results: list[TimelineExperiment]
    results_truncated: bool


class EvidenceTimelineResponse(BaseModel):
    """The bounded response contract consumed by the generated web client."""

    model_config = ConfigDict(extra="forbid")

    target: TimelineRecord
    issue: TimelineRecord | None
    leads: list[TimelineRecord]
    selected_result: TimelineExperiment | None
    root_results: list[TimelineExperiment]
    root_results_truncated: bool
    directions: list[TimelineDirection]
    directions_truncated: bool
    unresolved_questions: list[TimelineRecord]


@router.get(
    "/api/visuals/timeline/{record_id}",
    dependencies=[Depends(require_role("viewer"))],
)
async def evidence_timeline_route(
    record_id: UUID,
    request: Request,
    direction_limit: Annotated[int | None, Query()] = None,
    results_per_direction: Annotated[int | None, Query()] = None,
) -> EvidenceTimelineResponse:
    """Return the lineage and timeline of a record of any kind, with hard bounded SQL.

    The anchor Issue supplies `leads` (its `narrows` ancestors, at most three,
    farthest first), results, and directions. An Issue is its own anchor; an
    Experiment is anchored on the Issue that produced it; any other kind stays
    the record and takes the nearest `produced_by` Issue, with the anchor itself
    as its nearest lead. A record with no anchor is returned alone.

    Args:
      record_id: Selected record UUID, of any kind.
      request: Request with the shared Store.
      direction_limit: Direct child Issues to return; the catalog sets the default
        and maximum.
      results_per_direction: Experiments per Issue; the catalog sets the default
        and maximum.

    Returns:
      timeline: Bounded record, result, and signed evidence projection.

    """
    descriptor = visual_catalog(request).visual("trax.timeline")
    if descriptor is None:
        raise HTTPException(status_code=404, detail="Timeline visual is not enabled")
    schema = descriptor.parameter_schema
    direction_count = _bounded(
        "direction_limit",
        requested=direction_limit,
        schemas=schema,
    )
    result_count = _bounded(
        "results_per_direction",
        requested=results_per_direction,
        schemas=schema,
    )
    store = get_store(request)
    async with store.engine.acquire() as conn:
        result = await load_timeline(
            conn,
            record_id,
            direction_limit=direction_count,
            results_per_direction=result_count,
        )
    if result is None:
        raise HTTPException(status_code=404, detail="Record not found")
    return EvidenceTimelineResponse.model_validate(result)


def _bounded(
    name: str,
    requested: int | None,
    schemas: dict[str, ParameterDescription],
) -> int:
    """Apply the catalog default and reject a value outside its bounds."""
    schema = schemas[name]
    value = from_plain(schema.default if requested is None else requested, int)
    minimum = from_plain(schema.minimum, int)
    maximum = from_plain(schema.maximum, int)
    if value < minimum or value > maximum:
        raise HTTPException(
            status_code=422,
            detail=f"{name} must be between {minimum} and {maximum}.",
        )
    return value
