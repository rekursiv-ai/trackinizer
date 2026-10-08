"""Authenticated publication and exact-version reads of shared Artifacts."""

from __future__ import annotations

from typing import Annotated

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse

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


@router.get(
    "/api/artifacts/{artifact_id}/html",
    response_class=HTMLResponse,
    responses={404: {"description": "No HTML Artifact with this id."}},
)
async def read_artifact_html_route(
    artifact_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> HTMLResponse:
    """Serve one HTML Artifact revision as a page: the link an agent shares.

    v2 embeds this same URL in its Artifact frame. The ``sandbox`` directive
    gives the page an opaque origin even when opened directly, so its scripts
    cannot read Trackinizer cookies or storage, nor call the API.

    Args:
      artifact_id: Canonical Artifact identity for the revision.
      request: Request carrying the graph Store.
      identity: Authenticated teammate.

    Returns:
      page: The stored HTML under the sandbox Content Security Policy.

    """
    del identity
    artifact = await read_artifact_content(get_store(request), artifact_id)
    if artifact is None or artifact.html is None:
        raise HTTPException(status_code=404, detail="HTML Artifact not found.")
    # The script and font allowlist matches Claude Sites, so a page built for
    # Claude Sites works here. ``connect-src 'none'`` keeps ``fetch`` from
    # reaching anything, and ``frame-ancestors 'self'`` lets only Trackinizer
    # embed it. ``private``: no shared cache may keep team content.
    # ``no-transform``: without it Cloudflare rewrites email-shaped text such as
    # ``user:%s@github.com`` into "[email protected]" and injects decoder and
    # bot-detection scripts this policy blocks, so viewers see and copy broken
    # text. Compression survives: caddy compresses before Cloudflare.
    return HTMLResponse(
        artifact.html,
        headers={
            "Content-Security-Policy": "; ".join(
                [
                    "sandbox allow-scripts allow-popups",
                    "default-src 'none'",
                    "base-uri 'none'",
                    "connect-src 'none'",
                    "font-src data: https://fonts.gstatic.com",
                    "form-action 'none'",
                    "frame-ancestors 'self'",
                    "frame-src 'none'",
                    "img-src data: blob:",
                    "manifest-src 'none'",
                    "media-src data: blob:",
                    "object-src 'none'",
                    (
                        "script-src 'unsafe-inline' 'unsafe-eval' "
                        "https://cdnjs.cloudflare.com "
                        "https://cdn.jsdelivr.net/npm/ https://unpkg.com "
                        "https://cdn.tailwindcss.com https://code.jquery.com"
                    ),
                    "style-src 'unsafe-inline' https://fonts.googleapis.com",
                    "worker-src 'none'",
                ],
            ),
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "private, no-transform",
        },
    )
