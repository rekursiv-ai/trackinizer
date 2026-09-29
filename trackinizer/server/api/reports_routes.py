"""Authenticated publication and exact-version reads of shared reports."""

from __future__ import annotations

from typing import Annotated

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from trackinizer.server.api._deps import get_store
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.visuals.reports import (
    PublishReport,
    ReportConflictError,
    ReportRevision,
    publish_report,
    read_report_revision,
)


router = APIRouter()


@router.post("/api/reports", response_model=ReportRevision, status_code=201)
async def publish_report_route(
    body: PublishReport,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
    key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")],
) -> ReportRevision:
    """Publish a bounded revision and its Issue-produced Artifact atomically.

    Args:
      body: HTML or structured report draft and graph citations.
      request: Request carrying the graph Store.
      identity: Authenticated writer or agent credential.
      key: Retry-safe publication key.

    Returns:
      revision: Immutable report revision shared with signed-in teammates.

    """
    try:
        return await publish_report(
            get_store(request),
            user_id=identity.user_id,
            author=identity.email,
            api_key_id=identity.api_key_id,
            body=body,
            key=key,
        )
    except ReportConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get(
    "/api/reports/{report_id}/revisions/{revision}",
    response_model=ReportRevision,
)
async def read_report_revision_route(
    report_id: uuid.UUID,
    revision: int,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> ReportRevision:
    """Read one immutable report revision with deployment-wide team access.

    Args:
      report_id: Stable report identity.
      revision: Exact publication version.
      request: Request carrying the graph Store.
      identity: Authenticated teammate.

    Returns:
      report: Exact revision and its frozen citations.

    """
    del identity
    if revision < 1:
        raise HTTPException(status_code=422, detail="Revision must be positive.")
    report = await read_report_revision(get_store(request), report_id, revision)
    if report is None:
        raise HTTPException(status_code=404, detail="Report revision not found.")
    return report
