"""Authenticated publication and exact-version reads of shared Artifacts."""

from __future__ import annotations

from typing import Annotated

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from trackinizer.server.api._deps import get_store
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.visuals.reports import (
    ArtifactContentConflictError,
    ArtifactContentRevision,
    PublishArtifactContent,
    publish_artifact_content,
    read_artifact_content,
)


router = APIRouter()


@router.post(
    "/api/artifacts/content",
    response_model=ArtifactContentRevision,
    status_code=201,
)
async def publish_artifact_content_route(
    body: PublishArtifactContent,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
    key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")],
) -> ArtifactContentRevision:
    """Publish a bounded revision and its Issue-produced Artifact atomically.

    Args:
      body: HTML or structured Artifact content and graph citations.
      request: Request carrying the graph Store.
      identity: Authenticated writer or agent credential.
      key: Retry-safe publication key.

    Returns:
      revision: Immutable Artifact content shared with signed-in teammates.

    """
    try:
        return await publish_artifact_content(
            get_store(request),
            user_id=identity.user_id,
            author=identity.email,
            api_key_id=identity.api_key_id,
            body=body,
            key=key,
        )
    except ArtifactContentConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get(
    "/api/artifacts/{artifact_id}/content",
    response_model=ArtifactContentRevision,
)
async def read_artifact_content_route(
    artifact_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> ArtifactContentRevision:
    """Read the content held by one Artifact with team access.

    Args:
      artifact_id: Canonical Artifact identity for the publication.
      request: Request carrying the graph Store.
      identity: Authenticated teammate.

    Returns:
      artifact: Exact revision and its frozen citations.

    """
    del identity
    artifact = await read_artifact_content(get_store(request), artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact content not found.")
    return artifact
