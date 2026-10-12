"""Shared canvas operations for browser and authorized agent clients."""

from __future__ import annotations

from functools import partial
from typing import Annotated

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from trackinizer.server.api._deps import get_assistant, get_hub, get_inbound
from trackinizer.server.api._routes_shared import engine_of, require_browser
from trackinizer.server.api.visuals_routes import visual_catalog
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.chat_hub import (
    ChatFrame,
    HighlightFrame,
    NavigateFrame,
    PreferencesFrame,
    WorkspaceFrame,
    iter_workspace_events,
)
from trackinizer.server.notify import iter_changed_ids
from trackinizer.server.visuals.workspace_store import (
    ReplayConflictError,
    RevisionConflictError,
    StationNameTakenError,
    WorkspaceKeyRefusedError,
    apply_workspace_operation,
    list_workspaces,
    new_workspace,
    open_latest_workspace,
    read_workspace,
    rename_workspace,
    set_workspace_layout,
    set_workspace_page,
    workspace_for_chat,
)
from trackinizer.server.visuals.workspaces import (
    ApplyWorkspaceOperation,
    ChatWindow,
    Highlight,
    Navigate,
    RenameWorkspace,
    SetPage,
    SetPreferences,
    StationLayout,
    WorkspaceConflict,
    WorkspaceState,
    WorkspaceSummary,
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
    move of the page; ``highlight`` the inquiries it points at; ``chat`` whether it
    hid (``hidden``) the tab's Chat window; and ``changed`` an
    inquiry id, as ``/api/web/subscribe`` relays it, so a tab needs this one stream.
    A Chat conversation is an AgentSession, so a line added to it reaches every
    viewer who reads the session's audience as the session's ``changed`` id; no frame
    is Chat's own. The stream opens with a comment and sends another after 25 s without
    a frame. A subscriber more than 256 frames behind is dropped; it reconnects from
    the ``workspace`` frame.

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
            is_active=partial(_owner_is_active, request, user_id=identity.user_id),
            changes=iter_changed_ids(
                engine_of(request),
                audiences=identity.read_audiences,
            ),
        ),
        media_type="text/event-stream",
    )


@router.post("/api/workspaces", response_model=WorkspaceState)
async def create_workspace_route(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> WorkspaceState:
    """Open the station the principal changed last, making the first if none.

    Args:
      request: Request carrying the database engine.
      identity: Authenticated browser account.

    Returns:
      state: The principal's station changed last.

    """
    require_browser(identity)
    return await open_latest_workspace(
        engine_of(request),
        user_id=identity.user_id,
        catalog=visual_catalog(request).catalog(),
        inbound=get_inbound(request),
        assistant=get_assistant(request),
    )


@router.get("/api/workspaces", response_model=list[WorkspaceSummary])
async def list_workspaces_route(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> list[WorkspaceSummary]:
    """List the principal's stations, the one changed last first.

    Args:
      request: Request carrying the database engine.
      identity: Authenticated browser account.

    Returns:
      stations: Their ids, names, chats, pages and times.

    """
    require_browser(identity)
    return await list_workspaces(engine_of(request), user_id=identity.user_id)


@router.post("/api/workspaces/new", response_model=WorkspaceState)
async def new_workspace_route(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> WorkspaceState:
    """Make a station named with the lowest free ``unnamed-workstream#N``.

    Args:
      request: Request carrying the database engine.
      identity: Authenticated browser account.

    Returns:
      state: The new station.

    """
    require_browser(identity)
    return await new_workspace(
        engine_of(request),
        user_id=identity.user_id,
        catalog=visual_catalog(request).catalog(),
        inbound=get_inbound(request),
        assistant=get_assistant(request),
    )


@router.post(
    "/api/workspaces/for-chat/{conversation_id}",
    response_model=WorkspaceState,
)
async def workspace_for_chat_route(
    conversation_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> WorkspaceState:
    """Find the principal's station that holds a chat, or make one bound to it.

    A chat that was deleted is 410 ``chat_deleted``, and no station is bound to it.

    Args:
      conversation_id: The science chat to continue.
      request: Request carrying the database engine.
      identity: Authenticated browser account.

    Returns:
      state: The station that holds the chat.

    """
    require_browser(identity)
    return await workspace_for_chat(
        engine_of(request),
        user_id=identity.user_id,
        conversation_id=conversation_id,
        catalog=visual_catalog(request).catalog(),
        inbound=get_inbound(request),
        assistant=get_assistant(request),
    )


@router.patch(
    "/api/workspaces/{workspace_id}",
    response_model=WorkspaceState,
    responses={409: {"description": "The principal has a station of that name."}},
)
async def rename_workspace_route(
    workspace_id: uuid.UUID,
    body: RenameWorkspace,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> WorkspaceState:
    """Rename one of the principal's stations.

    Args:
      workspace_id: The station.
      body: The new name, 1 to 120 characters once trimmed.
      request: Request carrying the database engine.
      identity: Authenticated browser account, which must own the station.

    Returns:
      state: The renamed station.

    """
    require_browser(identity)
    try:
        state = await rename_workspace(
            engine_of(request),
            user_id=identity.user_id,
            workspace_id=workspace_id,
            name=body.name,
            inbound=get_inbound(request),
            assistant=get_assistant(request),
        )
    except StationNameTakenError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if state is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return state


@router.put("/api/workspaces/{workspace_id}/page", status_code=204)
async def set_workspace_page_route(
    workspace_id: uuid.UUID,
    body: SetPage,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> Response:
    """Remember the page a station is on, so switching back can restore it.

    No revision changes and nothing is published.

    Args:
      workspace_id: The station.
      body: The `#/...` page.
      request: Request carrying the database engine.
      identity: Authenticated browser account, which must own the station.

    Returns:
      response: Empty.

    """
    require_browser(identity)
    if not await set_workspace_page(
        engine_of(request),
        user_id=identity.user_id,
        workspace_id=workspace_id,
        route=body.route,
    ):
        raise HTTPException(status_code=404, detail="Workspace not found")
    return Response(status_code=204)


@router.put("/api/workspaces/{workspace_id}/layout", status_code=204)
async def set_workspace_layout_route(
    workspace_id: uuid.UUID,
    body: StationLayout,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> Response:
    """Remember how a station's columns and windows were left.

    Each key in the body replaces the stored key; the rest stay. No revision
    changes and nothing is published.

    Args:
      workspace_id: The station.
      body: The layout keys to store, as fractions of the stage.
      request: Request carrying the database engine.
      identity: Authenticated browser account, which must own the station.

    Returns:
      response: Empty.

    """
    require_browser(identity)
    if not await set_workspace_layout(
        engine_of(request),
        user_id=identity.user_id,
        workspace_id=workspace_id,
        layout=body,
    ):
        raise HTTPException(status_code=404, detail="Workspace not found")
    return Response(status_code=204)


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

    A ``navigate``, ``highlight``, ``chat`` or ``preferences`` operation, which only an
    agent key may send, changes neither a visual nor the revision: it pushes a
    ``navigate``, ``highlight`` or ``chat`` frame to this station, or, for
    ``preferences``, which changes the owner's preferences, a ``preferences`` frame
    to every station of the owner. A replay of an applied key returns the original state and
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
        elif isinstance(body.operation, ChatWindow):
            hub.publish(workspace_id, frame=ChatFrame(hidden=body.operation.hidden))
        elif isinstance(body.operation, SetPreferences):
            # The stream is the owner's browser alone, so their tabs get every key.
            if applied.preferences is not None:
                for station in applied.stations:
                    hub.publish(
                        station,
                        frame=PreferencesFrame(preferences=applied.preferences),
                    )
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
