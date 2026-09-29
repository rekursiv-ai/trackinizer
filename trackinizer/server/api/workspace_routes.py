"""Shared canvas operations for browser and authorized agent clients."""

from __future__ import annotations

from typing import Annotated

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from trackinizer.server.api._deps import get_inbound
from trackinizer.server.api._routes_shared import engine_of
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.inbound import InboundReplayConflictError
from trackinizer.server.visuals.workspace_store import (
    ReplayConflictError,
    RevisionConflictError,
    WorkspaceContextChangedError,
    WorkspaceDisabledError,
    WorkspacePairingError,
    WorkspaceSessionUnavailableError,
    apply_workspace_operation,
    create_default_workspace,
    list_connectable_sessions,
    read_workspace,
    send_workspace_message,
    set_workspace_connection,
    workspace_connection_status,
)
from trackinizer.server.visuals.workspaces import (
    ApplyWorkspaceOperation,
    ConnectableSession,
    WorkspaceConflict,
    WorkspaceConnection,
    WorkspaceConnectionStatus,
    WorkspaceMessageReceipt,
    WorkspaceMessageRequest,
    WorkspaceState,
)


router = APIRouter()


@router.get(
    "/api/workspaces/{workspace_id}/connection",
    response_model=WorkspaceConnectionStatus,
)
async def workspace_connection_status_route(
    workspace_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> WorkspaceConnectionStatus:
    """Report the stored pairing's current status to its browser owner.

    Args:
      workspace_id: Canvas whose pairing is checked.
      request: Request carrying the database engine.
      identity: Authenticated browser account.

    Returns:
      status: Direct live, ended, or unavailable status.

    """
    if identity.api_key_id is not None:
        raise HTTPException(status_code=403, detail="Browser session required")
    try:
        result = await workspace_connection_status(
            engine_of(request),
            identity.user_id,
            workspace_id,
        )
    except WorkspaceDisabledError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    if result is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return result


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
    """Queue a viewer's message for the live session paired with this canvas.

    Args:
      workspace_id: Canvas carrying the paired session.
      body: Text and optional chat visual identity.
      request: Request carrying the database engine and inbound queue.
      identity: Authenticated browser sender.
      key: Required retry-safe idempotency key.

    Returns:
      receipt: Paired session and pending queue depth.

    """
    if identity.api_key_id is not None:
        raise HTTPException(status_code=403, detail="Browser session required")
    try:
        result = await send_workspace_message(
            engine_of(request),
            identity.user_id,
            workspace_id,
            body,
            key,
            source=identity.email,
            inbound=get_inbound(request),
        )
    except WorkspaceDisabledError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except WorkspaceSessionUnavailableError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except WorkspaceContextChangedError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except InboundReplayConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if result is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return result


@router.get(
    "/api/workspaces/sessions/connectable",
    response_model=list[ConnectableSession],
)
async def connectable_sessions_route(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> list[ConnectableSession]:
    """List live sessions the interactive browser may connect.

    Args:
      request: Request carrying the database engine.
      identity: Authenticated browser account.

    Returns:
      sessions: Pairable live sessions owned by this account.

    """
    if identity.api_key_id is not None:
        raise HTTPException(status_code=403, detail="Browser session required")
    try:
        return await list_connectable_sessions(engine_of(request), identity.user_id)
    except WorkspaceDisabledError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error


@router.put(
    "/api/workspaces/{workspace_id}/connection",
    response_model=WorkspaceState,
    responses={409: {"model": WorkspaceConflict}},
)
async def workspace_connection_route(
    workspace_id: uuid.UUID,
    body: WorkspaceConnection,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> WorkspaceState | JSONResponse:
    """Let the signed-in browser pair or disconnect a live agent session.

    Args:
      workspace_id: Canvas to connect.
      body: Requested session and expected revision.
      request: Request carrying the database engine.
      identity: Authenticated browser account.

    Returns:
      state: Updated canvas, or a stale-revision conflict.

    """
    if identity.api_key_id is not None:
        raise HTTPException(status_code=403, detail="Browser session required")
    try:
        state = await set_workspace_connection(
            engine_of(request),
            identity.user_id,
            workspace_id,
            body,
        )
    except RevisionConflictError as error:
        return JSONResponse(
            status_code=409,
            content=WorkspaceConflict(
                detail="stale workspace revision",
                current=error.current,
            ).model_dump(mode="json"),
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except WorkspaceDisabledError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    if state is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return state


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
    if identity.api_key_id is not None:
        raise HTTPException(status_code=403, detail="Browser session required")
    try:
        return await create_default_workspace(engine_of(request), identity.user_id)
    except WorkspaceDisabledError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error


@router.get("/api/workspaces/{workspace_id}", response_model=WorkspaceState)
async def read_workspace_route(
    workspace_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> WorkspaceState:
    """Read only the principal's own workspace.

    Args:
      workspace_id: Requested canvas.
      request: FastAPI request with a database engine.
      identity: Authenticated workspace owner.

    Returns:
      state: The owned canvas and its current revision.

    """
    try:
        state = await read_workspace(
            engine_of(request),
            identity.user_id,
            workspace_id,
            agent_api_key_id=identity.api_key_id,
        )
    except WorkspaceDisabledError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except WorkspacePairingError as error:
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
        state = await apply_workspace_operation(
            engine_of(request),
            identity.user_id,
            workspace_id,
            key,
            body,
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
    except WorkspaceDisabledError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except WorkspacePairingError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    if state is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return state
