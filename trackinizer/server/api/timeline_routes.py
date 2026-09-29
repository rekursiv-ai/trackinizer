"""Read a bounded Issue evidence timeline for the timeline visual."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from trackinizer.server.api._deps import get_store
from trackinizer.server.auth import require_role
from trackinizer.server.visuals.timeline import (
    UnsupportedTimelineTargetError,
    load_timeline,
)


router = APIRouter()


class TimelineRecord(BaseModel):
    """Bounded record projection shared by timeline rows and citations."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    kind: Literal["Issue", "Experiment", "Belief"]
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
    evidence: list[TimelineEvidence] = Field(max_length=6)
    evidence_truncated: bool


class TimelineDirection(BaseModel):
    """One direct Issue direction and its capped results."""

    model_config = ConfigDict(extra="forbid")

    issue: TimelineRecord
    results: list[TimelineExperiment] = Field(max_length=5)
    results_truncated: bool


class EvidenceTimelineResponse(BaseModel):
    """The bounded response contract consumed by the generated web client."""

    model_config = ConfigDict(extra="forbid")

    target: TimelineRecord
    issue: TimelineRecord | None
    selected_result: TimelineExperiment | None
    root_results: list[TimelineExperiment] = Field(max_length=5)
    root_results_truncated: bool
    directions: list[TimelineDirection] = Field(max_length=12)
    directions_truncated: bool
    unresolved_questions: list[TimelineRecord] = Field(max_length=12)


@router.get(
    "/api/visuals/timeline/{record_id}",
    dependencies=[Depends(require_role("viewer"))],
)
async def evidence_timeline_route(
    record_id: UUID,
    request: Request,
    direction_limit: Annotated[int, Query(ge=1, le=12)] = 8,
    results_per_direction: Annotated[int, Query(ge=1, le=5)] = 3,
) -> EvidenceTimelineResponse:
    """Return an Issue or Experiment timeline with hard bounded SQL limits.

    Args:
      record_id: Selected Issue or Experiment UUID.
      request: Request with the shared Store.
      direction_limit: Number of direct child Issues to return, at most 12.
      results_per_direction: Experiments per Issue, at most five.

    Returns:
      timeline: Bounded record, result, and signed evidence projection.

    """
    store = get_store(request)
    try:
        async with store.engine.acquire() as conn:
            result = await load_timeline(
                conn,
                record_id,
                direction_limit=direction_limit,
                results_per_direction=results_per_direction,
            )
    except UnsupportedTimelineTargetError as error:
        raise HTTPException(
            status_code=422,
            detail="Evidence timeline supports Issue and Experiment records only.",
        ) from error
    if result is None:
        raise HTTPException(status_code=404, detail="Issue or Experiment not found")
    return EvidenceTimelineResponse.model_validate(result)
