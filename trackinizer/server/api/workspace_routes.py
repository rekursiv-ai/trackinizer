"""Browser routes for a user's Trackinizer-backed workspaces."""

from __future__ import annotations

from typing import Annotated

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from treekle import from_plain

from trackinizer.server.api._deps import get_store
from trackinizer.server.api._routes_shared import engine_of, require_browser
from trackinizer.server.api.session_access import resolve_asker
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.visuals.workspaces import (
    CreateWorkspace,
    ScratchWorkspaceError,
    UpdateWorkspace,
    WorkspaceInfo,
    WorkspaceOrder,
    WorkspaceSummary,
    create_workspace,
    list_workspaces,
    open_scratch,
    remember_opened_canvas,
    set_workspace_order,
    update_workspace,
    workspace_info,
)


router = APIRouter()


@router.get("/api/workspaces", response_model=list[WorkspaceSummary])
async def list_workspaces_route(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    q: str | None = None,
    hidden: bool = False,
    asker: str | None = None,
) -> list[WorkspaceSummary]:
    """List the caller's workspaces, optionally searching and including hidden.

    Args:
      request: Request carrying the database engine and store.
      identity: Authenticated browser account.
      q: Optional title substring.
      hidden: Whether to include hidden workspaces.
      asker: The account the assistant reads for, when supplied.

    Returns:
      workspaces: Matching workspaces in bar order.

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
                detail="Only the assistant may list workspaces for an asker.",
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
    return await list_workspaces(
        engine,
        user_id=user_id,
        email=email,
        q=q,
        include_hidden=hidden,
    )


@router.post("/api/workspaces", response_model=WorkspaceSummary)
async def create_workspace_route(
    body: CreateWorkspace,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> WorkspaceSummary:
    """Create an owned workspace Issue.

    Args:
      body: Workspace title.
      request: Request carrying the database engine and store.
      identity: Authenticated browser account.

    Returns:
      workspace: The created Issue summary.

    """
    require_browser(identity)
    return await create_workspace(
        engine_of(request),
        store=get_store(request),
        user_id=identity.user_id,
        email=identity.email,
        title=body.title,
    )


@router.put("/api/workspaces/order", response_model=list[WorkspaceSummary])
async def set_workspace_order_route(
    body: WorkspaceOrder,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> list[WorkspaceSummary]:
    """Save the caller's workspace bar order.

    Args:
      body: Workspace ids in the desired order.
      request: Request carrying the database engine.
      identity: Authenticated browser account.

    Returns:
      workspaces: The updated list.

    """
    require_browser(identity)
    entries = await set_workspace_order(
        engine_of(request),
        user_id=identity.user_id,
        email=identity.email,
        ids=body.ids,
    )
    if entries is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return entries


@router.patch(
    "/api/workspaces/{workspace_id}",
    response_model=WorkspaceSummary,
    responses={409: {"description": "Scratch cannot be hidden."}},
)
async def update_workspace_route(
    workspace_id: uuid.UUID,
    body: UpdateWorkspace,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> WorkspaceSummary:
    """Rename or change visibility and pin state on an owned workspace.

    Args:
      workspace_id: Issue to update.
      body: Title and/or hidden and pinned changes.
      request: Request carrying the database engine and store.
      identity: Authenticated browser account.

    Returns:
      workspace: The updated Issue summary.

    """
    require_browser(identity)
    try:
        summary = await update_workspace(
            engine_of(request),
            store=get_store(request),
            user_id=identity.user_id,
            email=identity.email,
            workspace_id=workspace_id,
            title=body.title,
            hidden=body.hidden,
            pinned=body.pinned,
        )
    except ScratchWorkspaceError as error:
        raise HTTPException(
            status_code=409,
            detail="Scratch cannot be hidden",
        ) from error
    if summary is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return summary


@router.get("/api/workspaces/{workspace_id}", response_model=WorkspaceInfo)
async def workspace_info_route(
    workspace_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> WorkspaceInfo:
    """Read an owned workspace and its canvases.

    Args:
      workspace_id: Issue to read.
      request: Request carrying the database engine.
      identity: Authenticated browser account.

    Returns:
      info: The Issue summary and attached canvases.

    """
    require_browser(identity)
    result = await workspace_info(
        engine_of(request),
        user_id=identity.user_id,
        email=identity.email,
        workspace_id=workspace_id,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return result


@router.put("/api/canvases/{canvas_id}/opened", status_code=204)
async def remember_opened_canvas_route(
    canvas_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> Response:
    """Remember a canvas as the one opened last in its workspace.

    Switching to the workspace opens it again, in any browser. No revision changes
    and nothing is published.

    Args:
      canvas_id: The canvas opened.
      request: Request carrying the database engine.
      identity: Authenticated browser account, which must own the canvas.

    Returns:
      response: Empty.

    """
    require_browser(identity)
    if not await remember_opened_canvas(
        engine_of(request),
        user_id=identity.user_id,
        email=identity.email,
        canvas_id=canvas_id,
    ):
        raise HTTPException(status_code=404, detail="Canvas not found")
    return Response(status_code=204)
