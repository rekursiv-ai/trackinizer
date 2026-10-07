"""Shared canvas operations for browser and authorized agent clients."""

from __future__ import annotations

from functools import partial
from typing import Annotated

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from trackinizer.server.api._deps import get_assistant, get_hub, get_inbound
from trackinizer.server.api._routes_shared import engine_of, require_browser
from trackinizer.server.api.visuals_routes import visual_catalog
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.chat_hub import (
    HighlightFrame,
    NavigateFrame,
    WorkspaceFrame,
    iter_workspace_events,
)
from trackinizer.server.notify import iter_changed_ids
from trackinizer.server.visuals.chats import (
    ChatConversationNotFoundError,
    ChatRequestConflictError,
    conversations_of,
)
from trackinizer.server.visuals.workspace_store import (
    PartnerBusyError,
    ReplayConflictError,
    RevisionConflictError,
    WorkspaceContextChangedError,
    WorkspaceKeyRefusedError,
    WorkspaceSessionUnavailableError,
    apply_workspace_operation,
    create_default_workspace,
    read_workspace,
    send_workspace_message,
)
from trackinizer.server.visuals.workspaces import (
    ApplyWorkspaceOperation,
    Highlight,
    Navigate,
    WorkspaceConflict,
    WorkspaceMessageReceipt,
    WorkspaceMessageRequest,
    WorkspaceState,
)


router = APIRouter()


@router.get("/api/workspaces/{workspace_id}/events")
async def workspace_events_route(
    workspace_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> StreamingResponse:
    """Stream a canvas's changes to its owner's browser as server-sent events.

    Each frame is ``data: <json>`` with ``t``, the server's epoch milliseconds:
    ``workspace`` carries the whole canvas on open, after every change by a
    browser or an agent, and when its partner changes; ``navigate`` an agent's
    move of the page; ``highlight`` the inquiries it points at; ``message`` a stored
    Chat line; ``status`` an assistant's status; ``delivered`` how far the partner
    has read a conversation; and ``changed`` an inquiry id, as
    ``/api/web/subscribe`` relays it, so a tab needs
    this one stream. After the ``workspace`` frame come each conversation's
    current status and delivered ``seq``. The stream opens with a comment and
    sends another after 25 s without a frame. A subscriber more than 256 frames
    behind is dropped; it reconnects from the ``workspace`` frame.

    Args:
      workspace_id: Canvas to follow.
      request: Request carrying the database engine and the event hub.
      identity: Authenticated browser account, which must own the canvas.

    Returns:
      stream: Server-sent frames.

    """
    require_browser(identity)
    read_state = partial(
        _current_state,
        request,
        user_id=identity.user_id,
        workspace_id=workspace_id,
    )
    await read_state()
    return StreamingResponse(
        iter_workspace_events(
            get_hub(request),
            workspace_id=workspace_id,
            read_state=read_state,
            read_conversations=partial(
                conversations_of,
                engine_of(request),
                user_id=identity.user_id,
                workspace_id=workspace_id,
            ),
            is_active=partial(_owner_is_active, request, user_id=identity.user_id),
            changes=iter_changed_ids(engine_of(request)),
        ),
        media_type="text/event-stream",
    )


@router.post(
    "/api/workspaces/{workspace_id}/messages",
    response_model=WorkspaceMessageReceipt,
)
async def workspace_message_route(
    workspace_id: uuid.UUID,
    body: WorkspaceMessageRequest,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")],
) -> WorkspaceMessageReceipt:
    """Store a viewer's message, publish it, and queue it for this canvas's partner.

    The partner is the assistant's newest live session.
    A retry with the same key and request returns the original receipt without
    queueing again; the same key for a different request is 409.

    Args:
      workspace_id: Canvas carrying the partner.
      body: Text, optional conversation and chat visual identity.
      request: Request carrying the database engine and inbound queue.
      identity: Authenticated browser sender.
      key: Required retry-safe idempotency key.

    Returns:
      receipt: Partner session, conversation and the stored message.

    """
    require_browser(identity)
    try:
        result = await send_workspace_message(
            engine_of(request),
            user_id=identity.user_id,
            workspace_id=workspace_id,
            body=body,
            key=key,
            source=identity.email,
            inbound=get_inbound(request),
            assistant=get_assistant(request),
            hub=get_hub(request),
        )
    except ChatConversationNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except WorkspaceSessionUnavailableError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except PartnerBusyError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except WorkspaceContextChangedError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ChatRequestConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if result is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return result


@router.post("/api/workspaces", response_model=WorkspaceState)
async def create_workspace_route(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> WorkspaceState:
    """Create or reopen the principal's default visual workspace.

    Args:
      request: Request carrying the database engine.
      identity: Authenticated browser account.

    Returns:
      state: The principal's default workspace.

    """
    require_browser(identity)
    return await create_default_workspace(
        engine_of(request),
        user_id=identity.user_id,
        catalog=visual_catalog(request).catalog(),
        inbound=get_inbound(request),
        assistant=get_assistant(request),
    )


@router.get("/api/workspaces/{workspace_id}", response_model=WorkspaceState)
async def read_workspace_route(
    workspace_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> WorkspaceState:
    """Read the principal's own workspace, or an assistant's partner canvas.

    Args:
      workspace_id: Requested canvas.
      request: FastAPI request with a database engine.
      identity: Authenticated workspace owner, or the assistant's agent key.

    Returns:
      state: The canvas and its current revision.

    """
    try:
        state = await read_workspace(
            engine_of(request),
            user_id=identity.user_id,
            workspace_id=workspace_id,
            inbound=get_inbound(request),
            assistant=get_assistant(request),
            agent_api_key_id=identity.api_key_id,
        )
    except WorkspaceKeyRefusedError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    if state is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return state


@router.post(
    "/api/workspaces/{workspace_id}/operations",
    response_model=WorkspaceState,
    responses={409: {"model": WorkspaceConflict}},
)
async def workspace_operation_route(
    workspace_id: uuid.UUID,
    body: ApplyWorkspaceOperation,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")],
) -> WorkspaceState | JSONResponse:
    """Apply one revisioned operation and return the updated canvas.

    A ``navigate`` or ``highlight`` operation, which only an agent key may send,
    changes neither a visual nor the revision: it pushes a ``navigate`` or
    ``highlight`` frame. A replay of an applied key returns the original state and
    publishes nothing.

    Args:
      workspace_id: Canvas to change.
      body: Expected revision and visual operation.
      request: Request carrying the database engine.
      identity: Authenticated workspace owner.
      key: Retry-safe idempotency key.

    Returns:
      state: Updated canvas, or a conflict with the current revision.

    """
    try:
        applied = await apply_workspace_operation(
            engine_of(request),
            user_id=identity.user_id,
            workspace_id=workspace_id,
            key=key,
            body=body,
            catalog=visual_catalog(request).catalog(),
            inbound=get_inbound(request),
            assistant=get_assistant(request),
            agent_api_key_id=identity.api_key_id,
        )
    except RevisionConflictError as error:
        return JSONResponse(
            status_code=409,
            content=WorkspaceConflict(
                detail="stale workspace revision",
                current=error.current,
            ).model_dump(mode="json"),
        )
    except ReplayConflictError as error:
        return JSONResponse(
            status_code=409,
            content=WorkspaceConflict(
                detail=str(error),
                current=error.current,
            ).model_dump(mode="json"),
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except WorkspaceKeyRefusedError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    if applied is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if not applied.replayed:
        hub = get_hub(request)
        if isinstance(body.operation, Navigate):
            hub.publish(workspace_id, frame=NavigateFrame(route=body.operation.route))
        elif isinstance(body.operation, Highlight):
            hub.publish(workspace_id, frame=HighlightFrame(ids=body.operation.ids))
        else:
            hub.publish(workspace_id, frame=WorkspaceFrame(state=applied.state))
    return applied.state


async def _current_state(
    request: Request,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> WorkspaceState:
    """Read the owner's canvas with its partner, or 404."""
    state = await read_workspace(
        engine_of(request),
        user_id=user_id,
        workspace_id=workspace_id,
        inbound=get_inbound(request),
        assistant=get_assistant(request),
    )
    if state is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return state


async def _owner_is_active(request: Request, *, user_id: uuid.UUID) -> bool:
    """Say whether the canvas's owner is still an active user."""
    async with engine_of(request).acquire() as conn:
        active = await conn.fetchval(
            "SELECT 1 FROM users WHERE id = $1 AND status = 'active'",
            user_id,
        )
    return active is not None
