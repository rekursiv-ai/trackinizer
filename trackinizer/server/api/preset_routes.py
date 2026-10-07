"""Authenticated routes for reusable canvas views and agent workflows."""

from __future__ import annotations

from typing import Annotated

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from trackinizer.server.api._deps import get_assistant, get_hub, get_inbound
from trackinizer.server.api._routes_shared import engine_of, require_browser
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.chat_hub import WorkspaceFrame
from trackinizer.server.visuals.presets import (
    CreatePreset,
    OpenPreset,
    UpdatePreset,
    WorkspacePreset,
    create_preset,
    delete_preset,
    list_presets,
    open_preset,
    read_preset,
    update_preset,
)
from trackinizer.server.visuals.workspace_store import (
    ReplayConflictError,
    RevisionConflictError,
    WorkspaceDisabledError,
)
from trackinizer.server.visuals.workspaces import WorkspaceConflict, WorkspaceState


router = APIRouter()


@router.get("/api/workspace-presets", response_model=list[WorkspacePreset])
async def list_presets_route(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> list[WorkspacePreset]:
    """List named views for the signed-in account."""
    require_browser(identity)
    try:
        return await list_presets(engine_of(request), identity.user_id)
    except WorkspaceDisabledError as error:
        raise _disabled(error) from error


@router.post(
    "/api/workspace-presets",
    response_model=WorkspacePreset,
    responses={409: {"model": WorkspaceConflict}},
)
async def create_preset_route(
    body: CreatePreset,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")],
) -> WorkspacePreset | JSONResponse:
    """Snapshot an owned canvas at its expected revision.

    Args:
      body: Named workflow and source canvas revision.
      request: Request carrying the database engine.
      identity: Signed-in account.
      key: Durable retry identity for this save.

    Returns:
      preset: Saved snapshot or revision/key conflict.

    """
    require_browser(identity)
    try:
        preset = await create_preset(
            engine_of(request),
            user_id=identity.user_id,
            body=body,
            key=key,
            inbound=get_inbound(request),
            assistant=get_assistant(request),
        )
    except (RevisionConflictError, ReplayConflictError) as error:
        return JSONResponse(
            status_code=409,
            content=WorkspaceConflict(
                detail=str(error),
                current=error.current,
            ).model_dump(mode="json"),
        )
    except WorkspaceDisabledError as error:
        raise _disabled(error) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if preset is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return preset


@router.get("/api/workspace-presets/{preset_id}", response_model=WorkspacePreset)
async def read_preset_route(
    preset_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> WorkspacePreset:
    """Read one owned named view or workflow.

    Args:
      preset_id: Saved view identifier.
      request: Request carrying the database engine.
      identity: Signed-in account.

    Returns:
      preset: The owned snapshot.

    """
    require_browser(identity)
    try:
        preset = await read_preset(engine_of(request), identity.user_id, preset_id)
    except WorkspaceDisabledError as error:
        raise _disabled(error) from error
    if preset is None:
        raise HTTPException(status_code=404, detail="Preset not found")
    return preset


@router.post(
    "/api/workspace-presets/{preset_id}/open",
    response_model=WorkspaceState,
    responses={409: {"model": WorkspaceConflict}},
)
async def open_preset_route(
    preset_id: uuid.UUID,
    body: OpenPreset,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")],
) -> WorkspaceState | JSONResponse:
    """Restore a saved visual arrangement and disconnect the previous agent.

    Args:
      preset_id: Snapshot to open.
      body: Target canvas and expected revision.
      request: Request carrying the database engine.
      identity: Signed-in account.
      key: Durable retry identity for this restore.

    Returns:
      state: Restored canvas or revision/key conflict.

    """
    require_browser(identity)
    try:
        applied = await open_preset(
            engine_of(request),
            user_id=identity.user_id,
            preset_id=preset_id,
            body=body,
            key=key,
            inbound=get_inbound(request),
            assistant=get_assistant(request),
        )
    except (RevisionConflictError, ReplayConflictError) as error:
        return JSONResponse(
            status_code=409,
            content=WorkspaceConflict(
                detail=str(error),
                current=error.current,
            ).model_dump(mode="json"),
        )
    except WorkspaceDisabledError as error:
        raise _disabled(error) from error
    if applied is None:
        raise HTTPException(status_code=404, detail="Preset or workspace not found")
    if not applied.replayed:
        get_hub(request).publish(
            applied.state.id,
            frame=WorkspaceFrame(state=applied.state),
        )
    return applied.state


@router.put("/api/workspace-presets/{preset_id}", response_model=WorkspacePreset)
async def update_preset_route(
    preset_id: uuid.UUID,
    body: UpdatePreset,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> WorkspacePreset:
    """Rename a view or revise its workflow continuation instructions.

    Args:
      preset_id: Snapshot to change.
      body: Supplied metadata fields.
      request: Request carrying the database engine.
      identity: Signed-in account.

    Returns:
      preset: Updated snapshot.

    """
    require_browser(identity)
    try:
        preset = await update_preset(
            engine_of(request),
            identity.user_id,
            preset_id,
            body,
        )
    except WorkspaceDisabledError as error:
        raise _disabled(error) from error
    if preset is None:
        raise HTTPException(status_code=404, detail="Preset not found")
    return preset


@router.delete("/api/workspace-presets/{preset_id}", status_code=204)
async def delete_preset_route(
    preset_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> None:
    """Delete one owned saved view.

    Args:
      preset_id: Snapshot to delete.
      request: Request carrying the database engine.
      identity: Signed-in account.

    """
    require_browser(identity)
    try:
        removed = await delete_preset(engine_of(request), identity.user_id, preset_id)
    except WorkspaceDisabledError as error:
        raise _disabled(error) from error
    if not removed:
        raise HTTPException(status_code=404, detail="Preset not found")


def _disabled(error: WorkspaceDisabledError) -> HTTPException:
    """Keep the existing workspace opt-in response contract."""
    return HTTPException(status_code=403, detail=str(error))
