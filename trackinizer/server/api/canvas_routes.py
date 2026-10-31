"""Shared canvas operations for browser and authorized agent clients."""

from __future__ import annotations

from functools import partial
from typing import Annotated

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from treekle import from_plain

from trackinizer.server.api._deps import (
    get_assistant,
    get_hub,
    get_inbound,
    get_store,
    get_terminal_hub,
)
from trackinizer.server.api._routes_shared import request_engine, require_browser
from trackinizer.server.api.session_access import resolve_asker, session_reader
from trackinizer.server.api.visuals_routes import visual_catalog
from trackinizer.server.audience import SessionReader, TranscriptPings
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.chat_hub import (
    CanvasFrame,
    ChatFrame,
    HighlightFrame,
    NavigateFrame,
    PreferencesFrame,
    iter_canvas_events,
)
from trackinizer.server.notify import iter_changed_ids
from trackinizer.server.visuals.canvas_store import (
    CanvasKeyRefusedError,
    CanvasNameTakenError,
    ProviderUnavailableError,
    ReplayConflictError,
    RevisionConflictError,
    apply_canvas_operation,
    canvas_for_chat,
    canvases_of_key,
    clear_chat_canvas,
    list_canvases,
    name_unnamed_canvas,
    new_canvas,
    open_chat_session,
    open_latest_canvas,
    open_personal_settings,
    read_canvas,
    rename_canvas,
    set_canvas_flags,
    set_canvas_layout,
    set_canvas_opens_as,
    set_canvas_page,
    set_canvas_workspace,
    set_session_campaign,
    settings_refs_for_chat,
    suggest_workspace_for_chat,
    view_canvas,
)
from trackinizer.server.visuals.canvases import (
    ApplyCanvasOperation,
    CanvasConflict,
    CanvasLayout,
    CanvasOfKey,
    CanvasSettingsRefs,
    CanvasState,
    CanvasSummary,
    CanvasView,
    CanvasViewRefused,
    ChatWindow,
    ChooseProvider,
    Highlight,
    NameChat,
    Navigate,
    NewCanvas,
    SessionCampaign,
    SetPage,
    SetPreferences,
    SettingsArtifact,
    UpdateCanvas,
    WorkspaceSuggestion,
)
from trackinizer.server.visuals.partners import live_chat_session
from trackinizer.server.visuals.science_chats import read_head, read_starter
from trackinizer.server.visuals.session_placement import move_canvas_sessions
from trackinizer.server.visuals.workspaces import (
    SetCanvasWorkspace,
    check_workspace,
    open_default_workspace,
)
from trackinizer.wire.wire_canvas_keys import CANVAS_KEY_PATTERN
from trackinizer.wire.wire_science_chat import chat_session_id


router = APIRouter()


@router.get("/api/canvases/{canvas_id}/events")
async def canvas_events_route(
    canvas_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> StreamingResponse:
    """Stream a canvas's changes to its owner's browser as server-sent events.

    Each frame is ``data: <json>`` with ``t``, the server's epoch milliseconds:
    ``canvas`` carries the whole canvas on open, after every change by a
    browser or an agent, and when its partner changes; ``navigate`` an agent's
    move of the page; ``highlight`` the inquiries it points at; ``chat`` whether it
    hid (``hidden``) the tab's Chat window; and ``changed`` an
    inquiry id, as ``/api/web/subscribe`` relays it, so a tab needs this one stream.
    A Chat conversation is an AgentSession, so a line added to it reaches every
    viewer who reads the session as the session's ``changed`` id; no frame is Chat's
    own. The stream opens with a comment and sends another after 25 s without
    a frame. A subscriber more than 256 frames behind is dropped; it reconnects from
    the ``canvas`` frame.

    Args:
      canvas_id: Canvas to follow.
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
        canvas_id=canvas_id,
        reader=session_reader(request, identity),
    )
    await read_state()
    return StreamingResponse(
        iter_canvas_events(
            get_hub(request),
            canvas_id=canvas_id,
            read_state=read_state,
            is_active=partial(_owner_is_active, request, user_id=identity.user_id),
            changes=iter_changed_ids(
                request_engine(request),
                hears=TranscriptPings(
                    engine=request_engine(request),
                    reader=session_reader(request, identity),
                ).hears,
            ),
        ),
        media_type="text/event-stream",
    )


@router.post("/api/canvases", response_model=CanvasState)
async def create_canvas_route(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> CanvasState:
    """Open the canvas the principal changed last, making the first if none.

    The first lands in the principal's default workspace.

    Args:
      request: Request carrying the database engine.
      identity: Authenticated browser account.

    Returns:
      state: The principal's canvas changed last.

    """
    require_browser(identity)
    return await open_latest_canvas(
        request_engine(request),
        store=get_store(request),
        user_id=identity.user_id,
        email=identity.email,
        catalog=visual_catalog(request).catalog(),
        inbound=get_inbound(request),
        assistant=get_assistant(request),
    )


@router.get("/api/canvases", response_model=list[CanvasSummary])
async def list_canvases_route(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> list[CanvasSummary]:
    """List the principal's canvases, the one changed last first.

    Args:
      request: Request carrying the database engine.
      identity: Authenticated browser account.

    Returns:
      canvases: Their ids, names, chats, pages and times, and in ``state`` what each
        chat needs of them: ``requires_action`` while an ask is open, ``taken_over``
        after its CLI was opened in a terminal.

    """
    require_browser(identity)
    return await list_canvases(
        request_engine(request),
        user_id=identity.user_id,
        inbound=get_inbound(request),
        assistant=get_assistant(request),
    )


@router.post("/api/canvases/new", response_model=CanvasState)
async def new_canvas_route(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    body: NewCanvas | None = None,
) -> CanvasState:
    """Make a canvas named with the lowest free ``unnamed-canvas#N``.

    It starts on the ``provider`` the body names, else on the caller's
    ``chat_provider`` preference. A provider with no machine to run on is taken all
    the same: nothing runs until a machine is picked. A ``claude`` or ``codex``
    canvas is given its chat and the chat's session at once, which its partner
    names, so a run can be started attached to it before any line.

    Args:
      request: Request carrying the database engine.
      identity: Authenticated browser account.
      body: Optional workspace and provider; an unset workspace selects the default
        workspace.

    Returns:
      state: The new canvas.

    """
    require_browser(identity)
    engine = request_engine(request)
    workspace_id = body.workspace_id if body is not None else None
    if workspace_id is None:
        workspace_id = await open_default_workspace(
            engine,
            store=get_store(request),
            user_id=identity.user_id,
            email=identity.email,
        )
    elif not await check_workspace(
        engine,
        email=identity.email,
        workspace_id=workspace_id,
    ):
        raise HTTPException(status_code=404, detail="Workspace not found")
    state = await new_canvas(
        engine,
        user_id=identity.user_id,
        catalog=visual_catalog(request).catalog(),
        inbound=get_inbound(request),
        assistant=get_assistant(request),
        workspace_id=workspace_id,
        provider=None if body is None else body.provider,
    )
    return await _with_chat_session(request, identity, state=state)


@router.post("/api/canvases/{canvas_id}/clear-chat", response_model=CanvasState)
async def clear_chat_route(
    canvas_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")],
) -> CanvasState:
    """Copy a canvas's canvas into a fresh, chatless canvas.

    Args:
      canvas_id: Canvas to clear.
      request: Request carrying the database engine.
      identity: Authenticated browser account, which must own the canvas.
      key: Retry-safe idempotency key.

    Returns:
      state: The new canvas, or the same canvas for a retry.

    """
    require_browser(identity)
    state = await clear_chat_canvas(
        request_engine(request),
        user_id=identity.user_id,
        canvas_id=canvas_id,
        key=key,
        inbound=get_inbound(request),
        assistant=get_assistant(request),
    )
    if state is None:
        raise HTTPException(status_code=404, detail="Canvas not found")
    return state


@router.post(
    "/api/canvases/for-chat/{conversation_id}",
    response_model=CanvasState,
)
async def canvas_for_chat_route(
    conversation_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> CanvasState:
    """Find the principal's canvas that holds a chat, or make one bound to it.

    A canvas made for it lands in the principal's default workspace. A chat that was
    deleted is 410 ``chat_deleted``, and no canvas is bound to it.

    Args:
      conversation_id: The science chat to continue.
      request: Request carrying the database engine.
      identity: Authenticated browser account.

    Returns:
      state: The canvas that holds the chat.

    """
    require_browser(identity)
    return await canvas_for_chat(
        request_engine(request),
        store=get_store(request),
        user_id=identity.user_id,
        email=identity.email,
        conversation_id=conversation_id,
        catalog=visual_catalog(request).catalog(),
        inbound=get_inbound(request),
        assistant=get_assistant(request),
    )


@router.post(
    "/api/canvases/for-chat/{conversation_id}/name",
    response_model=CanvasState,
)
async def name_for_chat_route(
    conversation_id: uuid.UUID,
    body: NameChat,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    asker: str | None = None,
) -> CanvasState:
    """Name an unnamed canvas, or return its existing name unchanged.

    Only the configured assistant may name for an ``asker``. A user's name wins
    over a late suggestion; the server returns that canvas unchanged.

    Args:
      conversation_id: The science chat the canvas holds.
      body: Suggested name, trimmed and bounded to 120 characters.
      request: Request carrying the database engine and event hub.
      identity: The assistant's agent key.
      asker: The account the assistant names for.

    Returns:
      state: The named canvas, or its unchanged state when already named.

    """
    found = await resolve_asker(request, identity=identity, email=asker)
    if found is None:
        raise HTTPException(
            status_code=403,
            detail="Only the assistant may name a canvas for an asker.",
        )
    state = await name_unnamed_canvas(
        request_engine(request),
        email=found.email,
        conversation_id=conversation_id,
        name=body.name,
        inbound=get_inbound(request),
        assistant=get_assistant(request),
    )
    if state is None:
        raise HTTPException(status_code=404, detail="Canvas for chat not found")
    get_hub(request).publish(state.id, frame=CanvasFrame(state=state))
    return state


@router.post(
    "/api/canvases/for-chat/{conversation_id}/suggestion",
    response_model=CanvasState,
)
async def suggest_workspace_for_chat_route(
    conversation_id: uuid.UUID,
    body: WorkspaceSuggestion,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    asker: str | None = None,
) -> CanvasState:
    """Store the assistant's workspace suggestion on the asker's chat canvas.

    Args:
      conversation_id: The science chat the canvas holds.
      body: Suggested title and optional owned workspace id.
      request: Request carrying the database engine and event hub.
      identity: The assistant's agent key.
      asker: The account the assistant suggests for.

    Returns:
      state: The updated canvas.

    """
    found = await resolve_asker(request, identity=identity, email=asker)
    if found is None:
        raise HTTPException(
            status_code=403,
            detail="Only the assistant may suggest a workspace for an asker.",
        )
    state = await suggest_workspace_for_chat(
        request_engine(request),
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
            detail="Canvas or workspace for chat not found",
        )
    get_hub(request).publish(state.id, frame=CanvasFrame(state=state))
    return state


@router.put(
    "/api/canvases/{canvas_id}/workspace",
    response_model=CanvasState,
)
async def set_canvas_workspace_route(
    canvas_id: uuid.UUID,
    body: SetCanvasWorkspace,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> CanvasState:
    """Move an owned canvas to one of the caller's workspaces.

    Args:
      canvas_id: Canvas to move.
      body: Destination workspace Issue id.
      request: Request carrying the database engine.
      identity: Authenticated browser account.

    Returns:
      state: The canvas after it moves.

    """
    require_browser(identity)
    state = await set_canvas_workspace(
        request_engine(request),
        user_id=identity.user_id,
        email=identity.email,
        canvas_id=canvas_id,
        workspace_id=body.workspace_id,
        inbound=get_inbound(request),
        assistant=get_assistant(request),
    )
    if state is None:
        raise HTTPException(status_code=404, detail="Canvas or workspace not found")
    await move_canvas_sessions(
        request_engine(request),
        store=get_store(request),
        canvas_id=canvas_id,
        email=identity.email,
        api_key_id=identity.api_key_id,
        workspace_id=body.workspace_id,
    )
    return state


@router.get(
    "/api/canvases/for-chat/{conversation_id}/settings",
    response_model=CanvasSettingsRefs,
)
async def settings_refs_for_chat_route(
    conversation_id: uuid.UUID,
    *,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    asker: str | None = None,
) -> CanvasSettingsRefs:
    """Resolve the asker's settings rows and head session for a chat.

    Only the configured assistant may read for an ``asker``, and it reads the asker's
    own canvas, so one writer's setup never governs another's launch from a shared
    chat. A launch takes this setup where the asker's line leaves a field unset.

    Args:
      conversation_id: The science chat the canvas holds.
      request: Request carrying the database engine and the configuration.
      identity: The assistant's agent key.
      asker: The account the assistant reads for.

    Returns:
      setup: The canvas's setup, or a null ``campaign`` when there is none.

    """
    found = await resolve_asker(request, identity=identity, email=asker)
    if found is None:
        raise HTTPException(
            status_code=403,
            detail="Only the assistant may read a campaign setup for an asker.",
        )
    head = await read_head(
        request_engine(request),
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
    "/api/canvases/{canvas_id}",
    response_model=CanvasState,
    responses={409: {"description": "The principal has a canvas of that name."}},
)
async def rename_canvas_route(
    canvas_id: uuid.UUID,
    body: UpdateCanvas,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> CanvasState:
    """Rename, change flags on, or set ``opens_as`` of one of the principal's canvases.

    ``opens_as`` is the access, ``open`` or ``private``, that the canvas's next new
    chat's first session opens with; a chat whose first session exists keeps its own.
    No field changes the revision.

    Args:
      canvas_id: The canvas.
      body: A new name, the hidden and pinned flags, and ``opens_as``; any of them.
      request: Request carrying the database engine.
      identity: Authenticated browser account, which must own the canvas.

    Returns:
      state: The updated canvas.

    """
    require_browser(identity)
    inbound = get_inbound(request)
    assistant = get_assistant(request)
    try:
        state = None
        if body.name is not None:
            state = await rename_canvas(
                request_engine(request),
                user_id=identity.user_id,
                canvas_id=canvas_id,
                name=body.name,
                inbound=inbound,
                assistant=assistant,
            )
            if state is None:
                raise HTTPException(status_code=404, detail="Canvas not found")
        if body.hidden is not None or body.pinned is not None:
            state = await set_canvas_flags(
                request_engine(request),
                user_id=identity.user_id,
                canvas_id=canvas_id,
                hidden=body.hidden,
                pinned=body.pinned,
                inbound=inbound,
                assistant=assistant,
            )
            if state is None:
                raise HTTPException(status_code=404, detail="Canvas not found")
        if body.opens_as is not None:
            state = await set_canvas_opens_as(
                request_engine(request),
                user_id=identity.user_id,
                canvas_id=canvas_id,
                opens_as=body.opens_as,
                inbound=inbound,
                assistant=assistant,
            )
        if state is None:
            raise HTTPException(status_code=404, detail="Canvas not found")
    except CanvasNameTakenError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return state


@router.put("/api/canvases/{canvas_id}/page", status_code=204)
async def set_canvas_page_route(
    canvas_id: uuid.UUID,
    body: SetPage,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> Response:
    """Remember the page a canvas is on, so switching back can restore it.

    No revision changes and nothing is published.

    Args:
      canvas_id: The canvas.
      body: The `#/...` page.
      request: Request carrying the database engine.
      identity: Authenticated browser account, which must own the canvas.

    Returns:
      response: Empty.

    """
    require_browser(identity)
    if not await set_canvas_page(
        request_engine(request),
        user_id=identity.user_id,
        canvas_id=canvas_id,
        route=body.route,
    ):
        raise HTTPException(status_code=404, detail="Canvas not found")
    return Response(status_code=204)


@router.put("/api/canvases/{canvas_id}/layout", status_code=204)
async def set_canvas_layout_route(
    canvas_id: uuid.UUID,
    body: CanvasLayout,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> Response:
    """Remember how a canvas's columns and windows were left.

    Each key in the body replaces the stored key; the rest stay. No revision
    changes and nothing is published.

    Args:
      canvas_id: The canvas.
      body: The layout keys to store, as fractions of the stage.
      request: Request carrying the database engine.
      identity: Authenticated browser account, which must own the canvas.

    Returns:
      response: Empty.

    """
    require_browser(identity)
    if not await set_canvas_layout(
        request_engine(request),
        user_id=identity.user_id,
        canvas_id=canvas_id,
        layout=body,
    ):
        raise HTTPException(status_code=404, detail="Canvas not found")
    return Response(status_code=204)


@router.put(
    "/api/canvases/{canvas_id}/session-settings",
    response_model=SessionCampaign,
)
async def set_session_settings_route(
    canvas_id: uuid.UUID,
    *,
    body: SessionCampaign,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> SessionCampaign:
    """Store campaign settings on the current head session of an owned canvas.

    Only the canvas owner may write. Other session app keys survive this update.

    Args:
      canvas_id: The canvas.
      body: The campaign setup to store, or ``None`` to clear it.
      request: Request carrying the database engine.
      identity: Authenticated browser account, which must own the canvas.

    Returns:
      settings: The stored session campaign.

    """
    require_browser(identity)
    async with request_engine(request).acquire() as conn:
        conversation_id = await conn.fetchval(
            "SELECT metadata->>'conversation_id' FROM visual_canvases "
            "WHERE id = $1 AND user_id = $2",
            canvas_id,
            identity.user_id,
        )
    if conversation_id is None:
        raise HTTPException(status_code=404, detail="Canvas not found")
    chat_id = uuid.UUID(from_plain(conversation_id, str))
    head = await read_head(
        request_engine(request),
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


@router.get(
    "/api/canvases/key/{key}",
    response_model=CanvasOfKey,
    responses={
        404: {"description": "No canvas has the key."},
        409: {"description": "Several canvases have the key."},
    },
    dependencies=[Depends(require_role("viewer"))],
)
async def canvas_of_key_route(
    key: Annotated[str, Path(pattern=CANVAS_KEY_PATTERN)],
    request: Request,
) -> CanvasOfKey:
    """Find the canvas an address names by its key, for anyone signed in.

    An id grants nothing: whether the caller may read the canvas is for the routes
    that read it to say, as they do for a link that names the whole id.

    Args:
      key: The canvas key, its id's first 12 hex digits (``wire_canvas_keys.py``).
      request: Request carrying the database engine.

    Returns:
      canvas: The canvas's id.

    """
    async with request_engine(request).acquire() as conn:
        found = await canvases_of_key(conn, key=key)
    if not found:
        raise HTTPException(status_code=404, detail="Canvas not found")
    if len(found) > 1:
        raise HTTPException(status_code=409, detail="Several canvases have this key")
    return CanvasOfKey(id=found[0])


@router.get("/api/canvases/{canvas_id}", response_model=CanvasState)
async def read_canvas_route(
    canvas_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> CanvasState:
    """Read the principal's own canvas, or an assistant's partner canvas.

    Args:
      canvas_id: Requested canvas.
      request: FastAPI request with a database engine.
      identity: Authenticated canvas owner, or the assistant's agent key.

    Returns:
      state: The canvas and its current revision.

    """
    try:
        state = await read_canvas(
            request_engine(request),
            user_id=identity.user_id,
            canvas_id=canvas_id,
            inbound=get_inbound(request),
            assistant=get_assistant(request),
            agent_api_key_id=identity.api_key_id,
            reader=session_reader(request, identity),
        )
    except CanvasKeyRefusedError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    if state is None:
        raise HTTPException(status_code=404, detail="Canvas not found")
    return state


@router.get(
    "/api/canvases/{canvas_id}/view",
    response_model=CanvasView,
    responses={403: {"model": CanvasViewRefused}, 404: {"description": "No canvas."}},
)
async def view_canvas_route(
    canvas_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> CanvasView | JSONResponse:
    """Show a canvas by its own URL: to its owner, and read only to whom its chat lets in.

    Anyone else is refused (403) with the owner's email, to ask for access, and
    nothing of the canvas. Only the owner changes it: every write stays theirs.

    Args:
      canvas_id: The canvas.
      request: Request carrying the database engine.
      identity: Authenticated browser account.

    Returns:
      view: The canvas and its owner, or the refusal.

    """
    require_browser(identity)
    view = await _view(request, identity=identity, canvas_id=canvas_id)
    if view is None:
        raise HTTPException(status_code=404, detail="Canvas not found")
    if isinstance(view, CanvasViewRefused):
        return JSONResponse(status_code=403, content=view.model_dump(mode="json"))
    return view


@router.get("/api/canvases/{canvas_id}/view/events")
async def view_canvas_events_route(
    canvas_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> StreamingResponse:
    """Stream a viewed canvas's changes, as ``/events`` streams its owner's.

    A viewer hears the canvas frames alone, each read as they may see it, and the
    ``changed`` ids of what they read; the owner's tab's own frames are not theirs.
    The stream ends at a keepalive once the canvas's chat no longer lets them in.

    Args:
      canvas_id: The canvas.
      request: Request carrying the database engine and the event hub.
      identity: Authenticated browser account its chat lets in.

    Returns:
      stream: Server-sent frames.

    """
    require_browser(identity)
    read_state = partial(_viewed_state, request, identity=identity, canvas_id=canvas_id)
    await read_state()
    return StreamingResponse(
        iter_canvas_events(
            get_hub(request),
            canvas_id=canvas_id,
            read_state=read_state,
            is_active=partial(
                _still_viewed,
                request,
                identity=identity,
                canvas_id=canvas_id,
            ),
            changes=iter_changed_ids(
                request_engine(request),
                hears=TranscriptPings(
                    engine=request_engine(request),
                    reader=session_reader(request, identity),
                ).hears,
            ),
            viewer=True,
        ),
        media_type="text/event-stream",
    )


@router.post(
    "/api/canvases/{canvas_id}/operations",
    response_model=CanvasState,
    responses={409: {"model": CanvasConflict}},
)
async def canvas_operation_route(
    canvas_id: uuid.UUID,
    body: ApplyCanvasOperation,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")],
) -> CanvasState | JSONResponse:
    """Apply one revisioned operation and return the updated canvas.

    A ``navigate``, ``highlight``, ``chat`` or ``preferences`` operation, which only an
    agent key may send, changes neither a visual nor the revision: it pushes a
    ``navigate``, ``highlight`` or ``chat`` frame to this canvas, or, for
    ``preferences``, which changes the owner's preferences, a ``preferences`` frame
    to every canvas of the owner. A ``mark``, ``remove_mark`` or
    ``clear_marks`` operation (red boxes) commutes with every other write, so it
    takes the next revision whatever revision it expects; ``remove_mark`` takes any
    one box, ``clear_marks`` every box or one page's, and one already gone is no
    change. A replay of an applied key returns the original state and publishes
    nothing.

    A ``provider`` operation, which only the owner's browser sends, chooses who
    answers the canvas's Chat: ``assistant``, ``claude`` or ``codex``. A provider
    the owner cannot run now (no confirmed online machine of theirs lists the CLI) is
    409 ``provider_unavailable`` with the reason. Choosing ``claude`` or ``codex``
    gives the canvas a chat, and the chat a session, when it has none, so the
    partner names a session for a run to attach to. A switch on a canvas that holds a
    chat someone else started ends none of that chat's runs: its starter's canvas
    says who answers it.

    Args:
      canvas_id: Canvas to change.
      body: Expected revision and visual operation.
      request: Request carrying the database engine.
      identity: Authenticated canvas owner.
      key: Retry-safe idempotency key.

    Returns:
      state: Updated canvas, or a conflict with the current revision.

    """
    try:
        applied = await apply_canvas_operation(
            get_store(request),
            user_id=identity.user_id,
            canvas_id=canvas_id,
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
            content=CanvasConflict(
                detail="stale canvas revision",
                current=error.current,
            ).model_dump(mode="json"),
        )
    except ReplayConflictError as error:
        return JSONResponse(
            status_code=409,
            content=CanvasConflict(
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
    except CanvasKeyRefusedError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    if applied is None:
        raise HTTPException(status_code=404, detail="Canvas not found")
    state = applied.state
    if isinstance(body.operation, ChooseProvider):
        # Also on a replay: a retry finishes a choice whose session was not opened.
        state = await _with_chat_session(request, identity, state=state)
    if not applied.replayed:
        hub = get_hub(request)
        if isinstance(body.operation, Navigate):
            if applied.restored_page:
                hub.publish(canvas_id, frame=CanvasFrame(state=applied.state))
            hub.publish(canvas_id, frame=NavigateFrame(route=body.operation.route))
        elif isinstance(body.operation, Highlight):
            hub.publish(canvas_id, frame=HighlightFrame(ids=body.operation.ids))
        elif isinstance(body.operation, ChatWindow):
            hub.publish(canvas_id, frame=ChatFrame(hidden=body.operation.hidden))
        elif isinstance(body.operation, SetPreferences):
            # The stream is the owner's browser alone, so their tabs get every key.
            if applied.preferences is not None:
                for canvas in applied.canvases:
                    hub.publish(
                        canvas,
                        frame=PreferencesFrame(preferences=applied.preferences),
                    )
        else:
            hub.publish(canvas_id, frame=CanvasFrame(state=state))
    return state


# Its own transaction after the canvas's: the canvas's provider is stored first, so a
# session that could not be opened is opened by the next choice, or its retry. The
# assistant's lease on the chat outlives the drains refused it from now on, and would
# read as the run's: revoked once the provider is stored, it does not come back, and a
# drain it held from before returns without the run's lines.
async def _with_chat_session(
    request: Request,
    identity: AuthIdentity,
    *,
    state: CanvasState,
) -> CanvasState:
    """Give a CLI canvas's chat its session, and return the canvas as it is then."""
    if await _started_by_another(request, identity, state=state):
        return state
    await _end_other_runs(request, identity, state=state)
    if state.provider == "assistant":
        return state
    assistant = get_assistant(request)
    released = False
    if assistant is not None and state.conversation_id is not None:
        inbound = get_inbound(request)
        async with request_engine(request).acquire() as conn:
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
            canvas_id=state.id,
            assistant=assistant,
        )
    elif not released:
        return state
    return await _current_state(
        request,
        user_id=identity.user_id,
        canvas_id=state.id,
        reader=session_reader(request, identity),
    )


# The starter's own canvas says who answers a chat (``read_chat_provider``), so another
# person's canvas that holds it changes nothing of it: a switch there ends no run of the
# chat, the assistant's included, and the chat already has the session it began with.
async def _started_by_another(
    request: Request,
    identity: AuthIdentity,
    *,
    state: CanvasState,
) -> bool:
    """Say whether the canvas holds a chat someone other than ``identity`` started."""
    if state.conversation_id is None:
        return False
    async with request_engine(request).acquire() as conn:
        starter = await read_starter(
            conn,
            assistant=get_assistant(request),
            conversation_id=state.conversation_id,
        )
    return starter is not None and starter.lower() != identity.email.lower()


# A chat has one live run at a time, and a switch is where a second would begin: the new
# provider's run may start the moment the canvas answers. The old run's lease is
# revoked, which ends the poll it is parked in, and a run the server opened for the
# owner is told to stop; one the owner started by hand is stopped by the refusal its
# next drain gets (``provider_is_other_cli``), since the server cannot reach its process.
async def _end_other_runs(
    request: Request,
    identity: AuthIdentity,
    *,
    state: CanvasState,
) -> None:
    """Stop the chat's runs of any CLI its canvas no longer chooses."""
    if state.conversation_id is None:
        return
    inbound = get_inbound(request)
    hub = get_terminal_hub(request)
    async with request_engine(request).acquire() as conn:
        rows = await conn.fetch(
            "SELECT id FROM inquiries WHERE kind = 'AgentSession' "
            "AND agentsession_cli_session_id = $1",
            chat_session_id(state.conversation_id),
        )
    for row in rows:
        session_id = from_plain(row["id"], uuid.UUID)
        held = inbound.poller_run(session_id)
        if held is not None and held.cli is not None and held.cli != state.provider:
            inbound.forget_poller(session_id)
        for term in hub.attached_to(session_id):
            if (
                term.user.lower() == identity.email.lower()
                and term.command != state.provider
            ):
                await hub.stop(term)


async def _current_state(
    request: Request,
    *,
    user_id: uuid.UUID,
    canvas_id: uuid.UUID,
    reader: SessionReader,
) -> CanvasState:
    """Read the owner's canvas with its partner, or 404."""
    state = await read_canvas(
        request_engine(request),
        user_id=user_id,
        canvas_id=canvas_id,
        inbound=get_inbound(request),
        assistant=get_assistant(request),
        reader=reader,
    )
    if state is None:
        raise HTTPException(status_code=404, detail="Canvas not found")
    return state


async def _view(
    request: Request,
    *,
    identity: AuthIdentity,
    canvas_id: uuid.UUID,
) -> CanvasView | CanvasViewRefused | None:
    """Show a canvas to ``identity`` as :func:`view_canvas` does."""
    return await view_canvas(
        request_engine(request),
        user_id=identity.user_id,
        canvas_id=canvas_id,
        inbound=get_inbound(request),
        assistant=get_assistant(request),
        reader=session_reader(request, identity),
    )


async def _viewed_state(
    request: Request,
    *,
    identity: AuthIdentity,
    canvas_id: uuid.UUID,
) -> CanvasState:
    """Read a canvas as ``identity`` views it, or 404 for none and 403 for a refusal."""
    view = await _view(request, identity=identity, canvas_id=canvas_id)
    if view is None:
        raise HTTPException(status_code=404, detail="Canvas not found")
    if isinstance(view, CanvasViewRefused):
        raise HTTPException(status_code=403, detail=view.detail)
    return view.canvas


async def _still_viewed(
    request: Request,
    *,
    identity: AuthIdentity,
    canvas_id: uuid.UUID,
) -> bool:
    """Say whether the canvas's chat still lets ``identity`` in."""
    return isinstance(
        await _view(request, identity=identity, canvas_id=canvas_id),
        CanvasView,
    )


async def _user_id_for_email(request: Request, *, email: str) -> uuid.UUID:
    """Look up an active user's id after ``resolve_asker`` validated the account."""
    async with request_engine(request).acquire() as conn:
        user_id = await conn.fetchval(
            "SELECT id FROM users WHERE email = $1 AND status = 'active'",
            email.lower(),
        )
    if user_id is None:
        raise HTTPException(status_code=404, detail="User not found")
    return from_plain(user_id, uuid.UUID)


async def _owner_is_active(request: Request, *, user_id: uuid.UUID) -> bool:
    """Say whether the canvas's owner is still an active user."""
    async with request_engine(request).acquire() as conn:
        active = await conn.fetchval(
            "SELECT 1 FROM users WHERE id = $1 AND status = 'active'",
            user_id,
        )
    return active is not None
