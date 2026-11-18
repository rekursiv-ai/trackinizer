"""Browser routes for a user's Trackinizer-backed workstreams."""

from __future__ import annotations

from typing import Annotated

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from treekle import from_plain

from trackinizer.server.api._deps import get_store
from trackinizer.server.api._routes_shared import engine_of, require_browser
from trackinizer.server.api.session_access import resolve_asker
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.visuals.workstreams import (
    CreateWorkstream,
    ScratchWorkstreamError,
    UpdateWorkstream,
    WorkstreamInfo,
    WorkstreamOrder,
    WorkstreamSummary,
    create_workstream,
    list_workstreams,
    open_scratch,
    set_workstream_order,
    update_workstream,
    workstream_info,
)


router = APIRouter()


@router.get("/api/workstreams", response_model=list[WorkstreamSummary])
async def list_workstreams_route(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    q: str | None = None,
    hidden: bool = False,
    asker: str | None = None,
) -> list[WorkstreamSummary]:
    """List the caller's workstreams, optionally searching and including hidden.

    Args:
      request: Request carrying the database engine and store.
      identity: Authenticated browser account.
      q: Optional title substring.
      hidden: Whether to include hidden workstreams.
      asker: The account the assistant reads for, when supplied.

    Returns:
      workstreams: Matching workstreams in bar order.

    """
    engine = engine_of(request)
    email = identity.email
    user_id = identity.user_id
    if asker is None:
        require_browser(identity)
    else:
        found = await resolve_asker(request, identity=identity, email=asker)
        if found is None:
            raise HTTPException(
                status_code=403,
                detail="Only the assistant may list workstreams for an asker.",
            )
        email = found.email
        async with engine.acquire() as conn:
            resolved_id = await conn.fetchval(
                "SELECT id FROM users WHERE email = $1 AND status = 'active'",
                email.lower(),
            )
        if resolved_id is None:
            return []
        user_id = from_plain(resolved_id, uuid.UUID)
    await open_scratch(
        engine,
        store=get_store(request),
        user_id=user_id,
        email=email,
    )
    return await list_workstreams(
        engine,
        user_id=user_id,
        email=email,
        q=q,
        include_hidden=hidden,
    )


@router.post("/api/workstreams", response_model=WorkstreamSummary)
async def create_workstream_route(
    body: CreateWorkstream,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> WorkstreamSummary:
    """Create an owned workstream Issue.

    Args:
      body: Workstream title.
      request: Request carrying the database engine and store.
      identity: Authenticated browser account.

    Returns:
      workstream: The created Issue summary.

    """
    require_browser(identity)
    return await create_workstream(
        engine_of(request),
        store=get_store(request),
        user_id=identity.user_id,
        email=identity.email,
        title=body.title,
    )


@router.put("/api/workstreams/order", response_model=list[WorkstreamSummary])
async def set_workstream_order_route(
    body: WorkstreamOrder,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> list[WorkstreamSummary]:
    """Save the caller's workstream bar order.

    Args:
      body: Workstream ids in the desired order.
      request: Request carrying the database engine.
      identity: Authenticated browser account.

    Returns:
      workstreams: The updated list.

    """
    require_browser(identity)
    entries = await set_workstream_order(
        engine_of(request),
        user_id=identity.user_id,
        email=identity.email,
        ids=body.ids,
    )
    if entries is None:
        raise HTTPException(status_code=404, detail="Workstream not found")
    return entries


@router.patch(
    "/api/workstreams/{workstream_id}",
    response_model=WorkstreamSummary,
    responses={409: {"description": "Scratch cannot be hidden."}},
)
async def update_workstream_route(
    workstream_id: uuid.UUID,
    body: UpdateWorkstream,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> WorkstreamSummary:
    """Rename or change visibility and pin state on an owned workstream.

    Args:
      workstream_id: Issue to update.
      body: Title and/or hidden and pinned changes.
      request: Request carrying the database engine and store.
      identity: Authenticated browser account.

    Returns:
      workstream: The updated Issue summary.

    """
    require_browser(identity)
    try:
        summary = await update_workstream(
            engine_of(request),
            store=get_store(request),
            user_id=identity.user_id,
            email=identity.email,
            workstream_id=workstream_id,
            title=body.title,
            hidden=body.hidden,
            pinned=body.pinned,
        )
    except ScratchWorkstreamError as error:
        raise HTTPException(
            status_code=409,
            detail="Scratch cannot be hidden",
        ) from error
    if summary is None:
        raise HTTPException(status_code=404, detail="Workstream not found")
    return summary


@router.get("/api/workstreams/{workstream_id}", response_model=WorkstreamInfo)
async def workstream_info_route(
    workstream_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> WorkstreamInfo:
    """Read an owned workstream and its stations.

    Args:
      workstream_id: Issue to read.
      request: Request carrying the database engine.
      identity: Authenticated browser account.

    Returns:
      info: The Issue summary and attached stations.

    """
    require_browser(identity)
    result = await workstream_info(
        engine_of(request),
        user_id=identity.user_id,
        email=identity.email,
        workstream_id=workstream_id,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Workstream not found")
    return result
