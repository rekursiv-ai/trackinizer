"""Shared canvas operations for browser and authorized agent clients."""

from __future__ import annotations

from functools import partial
from typing import Annotated

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from treekle import from_plain

from trackinizer.server.api._deps import (
    get_assistant,
    get_hub,
    get_inbound,
    get_store,
)
from trackinizer.server.api._routes_shared import engine_of, require_browser
from trackinizer.server.api.session_access import resolve_asker, session_reader
from trackinizer.server.api.visuals_routes import visual_catalog
from trackinizer.server.audience import SessionReader, TranscriptPings
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
from trackinizer.server.visuals.partners import live_chat_session
from trackinizer.server.visuals.science_chats import read_head
from trackinizer.server.visuals.session_placement import move_station_sessions
from trackinizer.server.visuals.workspace_store import (
    ProviderUnavailableError,
    ReplayConflictError,
    RevisionConflictError,
    StationNameTakenError,
    WorkspaceKeyRefusedError,
    apply_workspace_operation,
    clear_chat_station,
    list_workspaces,
    name_unnamed_station,
    new_workspace,
    open_chat_session,
    open_latest_workspace,
    open_personal_settings,
    read_workspace,
    rename_workspace,
    set_session_campaign,
    set_station_flags,
    set_station_workstream,
    set_workspace_layout,
    set_workspace_page,
    settings_refs_for_chat,
    suggest_workstream_for_chat,
    workspace_for_chat,
)
from trackinizer.server.visuals.workspaces import (
    ApplyWorkspaceOperation,
    ChatWindow,
    ChooseProvider,
    Highlight,
    NameChat,
    Navigate,
    NewStation,
    SessionCampaign,
    SetPage,
    SetPreferences,
    SettingsArtifact,
    StationLayout,
    UpdateWorkspace,
    WorkspaceConflict,
    WorkspaceSettingsRefs,
    WorkspaceState,
    WorkspaceSummary,
    WorkstreamSuggestion,
)
from trackinizer.server.visuals.workstreams import (
    SetStationWorkstream,
    check_workstream,
    open_scratch,
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
    viewer who reads the session as the session's ``changed`` id; no frame is Chat's
    own. The stream opens with a comment and sends another after 25 s without
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
        reader=session_reader(request, identity),
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
                hears=TranscriptPings(
                    engine=engine_of(request),
                    reader=session_reader(request, identity),
                ).hears,
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
    workstream_id = await open_scratch(
        engine_of(request),
        store=get_store(request),
        user_id=identity.user_id,
        email=identity.email,
    )
    return await open_latest_workspace(
        engine_of(request),
        user_id=identity.user_id,
        catalog=visual_catalog(request).catalog(),
        inbound=get_inbound(request),
        assistant=get_assistant(request),
        workstream_id=workstream_id,
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
    body: NewStation | None = None,
) -> WorkspaceState:
    """Make a station named with the lowest free ``unnamed-workstream#N``.

    It starts on the ``provider`` the body names, else on the caller's
    ``chat_provider`` preference. A provider with no machine to run on is taken all
    the same: nothing runs until a machine is picked. A ``claude`` or ``codex``
    station is given its chat and the chat's session at once, which its partner
    names, so a run can be started attached to it before any line.

    Args:
      request: Request carrying the database engine.
      identity: Authenticated browser account.
      body: Optional workstream and provider; an unset workstream selects Scratch.

    Returns:
      state: The new station.

    """
    require_browser(identity)
    engine = engine_of(request)
    scratch_id = await open_scratch(
        engine,
        store=get_store(request),
        user_id=identity.user_id,
        email=identity.email,
    )
    workstream_id = body.workstream_id if body is not None else None
    if workstream_id is None:
        workstream_id = scratch_id
    elif not await check_workstream(
        engine,
        email=identity.email,
        workstream_id=workstream_id,
    ):
        raise HTTPException(status_code=404, detail="Workstream not found")
    state = await new_workspace(
        engine,
        user_id=identity.user_id,
        catalog=visual_catalog(request).catalog(),
        inbound=get_inbound(request),
        assistant=get_assistant(request),
        workstream_id=workstream_id,
        provider=None if body is None else body.provider,
    )
    return await _with_chat_session(request, identity, state=state)


@router.post("/api/workspaces/{workspace_id}/clear-chat", response_model=WorkspaceState)
async def clear_chat_route(
    workspace_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")],
) -> WorkspaceState:
    """Copy a station's canvas into a fresh, chatless station.

    Args:
      workspace_id: Station to clear.
      request: Request carrying the database engine.
      identity: Authenticated browser account, which must own the station.
      key: Retry-safe idempotency key.

    Returns:
      state: The new station, or the same station for a retry.

    """
    require_browser(identity)
    state = await clear_chat_station(
        engine_of(request),
        user_id=identity.user_id,
        workspace_id=workspace_id,
        key=key,
        inbound=get_inbound(request),
        assistant=get_assistant(request),
    )
    if state is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return state


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
    workstream_id = await open_scratch(
        engine_of(request),
        store=get_store(request),
        user_id=identity.user_id,
        email=identity.email,
    )
    return await workspace_for_chat(
        engine_of(request),
        user_id=identity.user_id,
        conversation_id=conversation_id,
        catalog=visual_catalog(request).catalog(),
        inbound=get_inbound(request),
        assistant=get_assistant(request),
        workstream_id=workstream_id,
    )


@router.post(
    "/api/workspaces/for-chat/{conversation_id}/name",
    response_model=WorkspaceState,
)
async def name_for_chat_route(
    conversation_id: uuid.UUID,
    body: NameChat,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    asker: str | None = None,
) -> WorkspaceState:
    """Name an unnamed station, or return its existing name unchanged.

    Only the configured assistant may name for an ``asker``. A user's name wins
    over a late suggestion; the server returns that station unchanged.

    Args:
      conversation_id: The science chat the station holds.
      body: Suggested name, trimmed and bounded to 120 characters.
      request: Request carrying the database engine and event hub.
      identity: The assistant's agent key.
      asker: The account the assistant names for.

    Returns:
      state: The named station, or its unchanged state when already named.

    """
    found = await resolve_asker(request, identity=identity, email=asker)
    if found is None:
        raise HTTPException(
            status_code=403,
            detail="Only the assistant may name a station for an asker.",
        )
    state = await name_unnamed_station(
        engine_of(request),
        email=found.email,
        conversation_id=conversation_id,
        name=body.name,
        inbound=get_inbound(request),
        assistant=get_assistant(request),
    )
    if state is None:
        raise HTTPException(status_code=404, detail="Station for chat not found")
    get_hub(request).publish(state.id, frame=WorkspaceFrame(state=state))
    return state


@router.post(
    "/api/workspaces/for-chat/{conversation_id}/suggestion",
    response_model=WorkspaceState,
)
async def suggest_workstream_for_chat_route(
    conversation_id: uuid.UUID,
    body: WorkstreamSuggestion,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    asker: str | None = None,
) -> WorkspaceState:
    """Store the assistant's workstream suggestion on the asker's chat station.

    Args:
      conversation_id: The science chat the station holds.
      body: Suggested title and optional owned workstream id.
      request: Request carrying the database engine and event hub.
      identity: The assistant's agent key.
      asker: The account the assistant suggests for.

    Returns:
      state: The updated station.

    """
    found = await resolve_asker(request, identity=identity, email=asker)
    if found is None:
        raise HTTPException(
            status_code=403,
            detail="Only the assistant may suggest a workstream for an asker.",
        )
    state = await suggest_workstream_for_chat(
        engine_of(request),
        user_id=await _user_id_for_email(request, email=found.email),
        email=found.email,
        conversation_id=conversation_id,
        suggestion=body,
        inbound=get_inbound(request),
        assistant=get_assistant(request),
    )
    if state is None:
        raise HTTPException(
            status_code=404,
            detail="Station or workstream for chat not found",
        )
    get_hub(request).publish(state.id, frame=WorkspaceFrame(state=state))
    return state


@router.put(
    "/api/workspaces/{workspace_id}/workstream",
    response_model=WorkspaceState,
)
async def set_station_workstream_route(
    workspace_id: uuid.UUID,
    body: SetStationWorkstream,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> WorkspaceState:
    """Move an owned station to one of the caller's workstreams.

    Args:
      workspace_id: Station to move.
      body: Destination workstream Issue id.
      request: Request carrying the database engine.
      identity: Authenticated browser account.

    Returns:
      state: The station after it moves.

    """
    require_browser(identity)
    state = await set_station_workstream(
        engine_of(request),
        user_id=identity.user_id,
        email=identity.email,
        workspace_id=workspace_id,
        workstream_id=body.workstream_id,
        inbound=get_inbound(request),
        assistant=get_assistant(request),
    )
    if state is None:
        raise HTTPException(status_code=404, detail="Workspace or workstream not found")
    await move_station_sessions(
        engine_of(request),
        store=get_store(request),
        workspace_id=workspace_id,
        email=identity.email,
        api_key_id=identity.api_key_id,
        workstream_id=body.workstream_id,
    )
    return state


@router.get(
    "/api/workspaces/for-chat/{conversation_id}/settings",
    response_model=WorkspaceSettingsRefs,
)
async def settings_refs_for_chat_route(
    conversation_id: uuid.UUID,
    *,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    asker: str | None = None,
) -> WorkspaceSettingsRefs:
    """Resolve the asker's settings rows and head session for a chat.

    Only the configured assistant may read for an ``asker``, and it reads the asker's
    own station, so one writer's setup never governs another's launch from a shared
    chat. A launch takes this setup where the asker's line leaves a field unset.

    Args:
      conversation_id: The science chat the station holds.
      request: Request carrying the database engine and the configuration.
      identity: The assistant's agent key.
      asker: The account the assistant reads for.

    Returns:
      setup: The station's setup, or a null ``campaign`` when there is none.

    """
    found = await resolve_asker(request, identity=identity, email=asker)
    if found is None:
        raise HTTPException(
            status_code=403,
            detail="Only the assistant may read a campaign setup for an asker.",
        )
    head = await read_head(
        engine_of(request),
        assistant=get_assistant(request),
        caller=identity,
        conversation_id=conversation_id,
        reader=session_reader(request, identity),
    )
    return await settings_refs_for_chat(
        get_store(request),
        user_id=await _user_id_for_email(request, email=found.email),
        email=found.email,
        conversation_id=conversation_id,
        session_id=None if head is None else head.session_id,
    )


@router.patch(
    "/api/workspaces/{workspace_id}",
    response_model=WorkspaceState,
    responses={409: {"description": "The principal has a station of that name."}},
)
async def rename_workspace_route(
    workspace_id: uuid.UUID,
    body: UpdateWorkspace,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> WorkspaceState:
    """Rename or change flags on one of the principal's stations.

    Args:
      workspace_id: The station.
      body: A new name and/or the hidden and pinned flags.
      request: Request carrying the database engine.
      identity: Authenticated browser account, which must own the station.

    Returns:
      state: The updated station.

    """
    require_browser(identity)
    inbound = get_inbound(request)
    assistant = get_assistant(request)
    try:
        state = None
        if body.name is not None:
            state = await rename_workspace(
                engine_of(request),
                user_id=identity.user_id,
                workspace_id=workspace_id,
                name=body.name,
                inbound=inbound,
                assistant=assistant,
            )
            if state is None:
                raise HTTPException(status_code=404, detail="Workspace not found")
        if body.hidden is not None or body.pinned is not None:
            state = await set_station_flags(
                engine_of(request),
                user_id=identity.user_id,
                workspace_id=workspace_id,
                hidden=body.hidden,
                pinned=body.pinned,
                inbound=inbound,
                assistant=assistant,
            )
            if state is None:
                raise HTTPException(status_code=404, detail="Workspace not found")
        if state is None:
            raise HTTPException(status_code=404, detail="Workspace not found")
    except StationNameTakenError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
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


@router.put(
    "/api/workspaces/{workspace_id}/session-settings",
    response_model=SessionCampaign,
)
async def set_session_settings_route(
    workspace_id: uuid.UUID,
    *,
    body: SessionCampaign,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> SessionCampaign:
    """Store campaign settings on the current head session of an owned station.

    Only the station owner may write. Other session app keys survive this update.

    Args:
      workspace_id: The station.
      body: The campaign setup to store, or ``None`` to clear it.
      request: Request carrying the database engine.
      identity: Authenticated browser account, which must own the station.

    Returns:
      settings: The stored session campaign.

    """
    require_browser(identity)
    async with engine_of(request).acquire() as conn:
        conversation_id = await conn.fetchval(
            "SELECT station->>'conversation_id' FROM visual_workspaces "
            "WHERE id = $1 AND user_id = $2",
            workspace_id,
            identity.user_id,
        )
    if conversation_id is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    chat_id = uuid.UUID(from_plain(conversation_id, str))
    head = await read_head(
        engine_of(request),
        assistant=get_assistant(request),
        caller=identity,
        conversation_id=chat_id,
        reader=session_reader(request, identity),
    )
    if head is None:
        raise HTTPException(status_code=404, detail="Chat session not found")
    if not await set_session_campaign(
        get_store(request),
        session_id=head.session_id,
        email=identity.email,
        campaign=(
            None
            if body.campaign is None
            else body.campaign.model_dump(mode="json", exclude_none=True)
        ),
    ):
        raise HTTPException(status_code=404, detail="Chat session not found")
    return body


@router.post("/api/me/settings-artifact", response_model=SettingsArtifact)
async def open_personal_settings_route(
    request: Request,
    *,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> SettingsArtifact:
    """Get or create the principal's pinned personal settings Artifact."""
    require_browser(identity)
    artifact_id = await open_personal_settings(get_store(request), email=identity.email)
    if artifact_id is None:
        raise HTTPException(status_code=404, detail="User not found")
    return SettingsArtifact(artifact_id=artifact_id)


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
            reader=session_reader(request, identity),
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
    to every station of the owner. A ``mark``, ``remove_mark`` or
    ``clear_marks`` operation (red boxes) commutes with every other write, so it
    takes the next revision whatever revision it expects; ``remove_mark`` takes any
    one box, ``clear_marks`` every box or one page's, and one already gone is no
    change. A replay of an applied key returns the original state and publishes
    nothing.

    A ``provider`` operation, which only the owner's browser sends, chooses who
    answers the station's Chat: ``assistant``, ``claude`` or ``codex``. A provider
    the owner cannot run now (no confirmed online machine of theirs lists the CLI) is
    409 ``provider_unavailable`` with the reason. Choosing ``claude`` or ``codex``
    gives the station a chat, and the chat a session, when it has none, so the
    partner names a session for a run to attach to.

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
            placed_by=None if identity.api_key_id is None else identity.email,
            author=identity.email,
            reader=session_reader(request, identity),
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
    except ProviderUnavailableError as error:
        raise HTTPException(
            status_code=409,
            detail={"code": "provider_unavailable", "message": str(error)},
        ) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except WorkspaceKeyRefusedError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    if applied is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    state = applied.state
    if isinstance(body.operation, ChooseProvider):
        # Also on a replay: a retry finishes a choice whose session was not opened.
        state = await _with_chat_session(request, identity, state=state)
    if not applied.replayed:
        hub = get_hub(request)
        if isinstance(body.operation, Navigate):
            if applied.restored_page:
                hub.publish(workspace_id, frame=WorkspaceFrame(state=applied.state))
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
            hub.publish(workspace_id, frame=WorkspaceFrame(state=state))
    return state


# Its own transaction after the station's: the station's provider is stored first, so a
# session that could not be opened is opened by the next choice, or its retry. The
# assistant's lease on the chat outlives the drains refused it from now on, and would
# read as the run's: revoked once the provider is stored, it does not come back, and a
# drain it held from before returns without the run's lines.
async def _with_chat_session(
    request: Request,
    identity: AuthIdentity,
    *,
    state: WorkspaceState,
) -> WorkspaceState:
    """Give a CLI station's chat its session, and return the station as it is then."""
    if state.provider == "assistant":
        return state
    assistant = get_assistant(request)
    released = False
    if assistant is not None and state.conversation_id is not None:
        inbound = get_inbound(request)
        async with engine_of(request).acquire() as conn:
            while held := await live_chat_session(
                conn,
                inbound=inbound,
                opener=assistant.email,
                conversation_id=state.conversation_id,
            ):
                inbound.forget_poller(held)
                released = True
    if state.partner is None or state.partner.session_id is None:
        _ = await open_chat_session(
            get_store(request),
            user_id=identity.user_id,
            workspace_id=state.id,
            assistant=assistant,
        )
    elif not released:
        return state
    return await _current_state(
        request,
        user_id=identity.user_id,
        workspace_id=state.id,
        reader=session_reader(request, identity),
    )


async def _current_state(
    request: Request,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    reader: SessionReader,
) -> WorkspaceState:
    """Read the owner's canvas with its partner, or 404."""
    state = await read_workspace(
        engine_of(request),
        user_id=user_id,
        workspace_id=workspace_id,
        inbound=get_inbound(request),
        assistant=get_assistant(request),
        reader=reader,
    )
    if state is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return state


async def _user_id_for_email(request: Request, *, email: str) -> uuid.UUID:
    """Look up an active user's id after ``resolve_asker`` validated the account."""
    async with engine_of(request).acquire() as conn:
        user_id = await conn.fetchval(
            "SELECT id FROM users WHERE email = $1 AND status = 'active'",
            email.lower(),
        )
    if user_id is None:
        raise HTTPException(status_code=404, detail="User not found")
    return from_plain(user_id, uuid.UUID)


async def _owner_is_active(request: Request, *, user_id: uuid.UUID) -> bool:
    """Say whether the canvas's owner is still an active user."""
    async with engine_of(request).acquire() as conn:
        active = await conn.fetchval(
            "SELECT 1 FROM users WHERE id = $1 AND status = 'active'",
            user_id,
        )
    return active is not None
