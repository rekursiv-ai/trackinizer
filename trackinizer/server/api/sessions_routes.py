"""Agent-session lifecycle and messaging routes: ``start`` / ``end`` / inbound.

The server side of the ``trax run`` capture-and-sync layer. A run opens a
session (minting an ``AgentSession`` inquiry row, server-assigned id) and
later closes it; the captured records themselves flow through the separate
``.../records`` routes in :mod:`server.api.session_ir_routes`.

Capture lifecycle routes permit a viewer to open and close sessions with its
own API key. Messaging routes require ``writer``. Tenant scope is derived by
joining to ``inquiries``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import partial
from typing import Annotated, cast
from uuid import UUID

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from treekle import from_plain

from trackinizer.server.api._deps import (
    get_assistant,
    get_hub,
    get_inbound,
    get_replays,
    get_store,
)
from trackinizer.server.api.session_access import (
    granted_audience,
    is_a_science_chat,
    require_attributable,
    require_chat_writer,
    require_session,
    require_session_access,
    require_session_id_change,
    require_session_write_access,
    resolve_asker,
)
from trackinizer.server.auth import (
    AuthIdentity,
    assert_account_active,
    require_role,
)
from trackinizer.server.inbound import Inbound, InboundQueue
from trackinizer.server.session_reaper import revive_if_reaped
from trackinizer.server.visuals.partners import (
    is_assistant_session,
    is_chat_session,
    opener_email,
    shared_partner,
)
from trackinizer.server.visuals.science_chats import read_first_session
from trackinizer.server.visuals.workspace_store import read_chat_provider
from trackinizer.types.inquiries import AgentSession
from trackinizer.wire.audience import OPEN
from trackinizer.wire.bodies import SubmitAgentSession
from trackinizer.wire.wire_machines import NAME_PATTERN
from trackinizer.wire.wire_science_chat import (
    CHAT_SESSION_PREFIX,
    CHAT_TITLE,
    ChatProvider,
    chat_conversation_id,
)
from trackinizer.wire.wire_sessions import (
    DrainInboundResponse,
    InboundDrainItem,
    InboundEnqueueRequest,
    InboundEnqueueResponse,
    SendMessage,
    SendMessageResponse,
    SessionEnd,
    SessionEndResponse,
    SessionStart,
    SessionStartResponse,
)


_logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/sessions/start", status_code=201)
async def session_start_route(
    body: SessionStart,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> SessionStartResponse:
    """Open a session: mint an ``AgentSession`` row and return its server id.

    The session ``owner`` doubles as the routing name (``--as``). It is made
    unique among live sessions (suffix on collision); the granted name is the
    owner stamped on the row and is echoed in the response so ``trax run``
    adopts whatever it was given.

    The session opens into the audience the caller's key is pinned to, else
    ``open``, or ``private`` when ``private`` asks; a pin the caller no longer holds
    is 403 ``audience_not_held``, and so is ``private`` under a pin to ``staff``. A
    resume asked to be ``private`` hides the ``open`` session it re-attaches, as the
    key that opened it may, and is 409, leaving it as it was, for a ``staff`` one. A
    session under the id of a science chat that is not ``open`` takes the chat's
    audience instead, so a chat stays ``private`` or ``staff`` whichever key opens
    its next session. A ``cli_session_id`` of a deleted science chat
    (``chat:<id>``) is 410 ``chat_deleted``. A science chat's session is titled
    ``Science chat`` whatever ``title`` says, since every key reads the row and only
    the chat's members read its lines.

    ``attach`` re-opens that session by its id instead: it is live again if it had
    ended, its ``cli_session_id`` stays, and the run's file becomes its next part. The
    response names it as a fresh start's would. A caller who may not write the
    session is 403, and so is one a science chat takes no run from: only its opening
    key, the assistant's or its starter's attach to a chat. An unknown id is 404.

    Args:
      body: Session metadata (title, cli, cli_session_id, attach, started, rooms,
        account, idempotency_key, private).
      request: Starlette request object.
      identity: Authenticated user (email, api_key_id, role).

    Returns:
      response: SessionStartResponse with session id, next event seq (0 for
        new, or max(seq)+1 for resumed), cli_session_id, granted actor name and
        audience.

    """
    store = get_store(request)
    if identity.role == "viewer" and (
        identity.api_key_id is None
        or (body.account is not None and body.account != identity.email)
    ):
        raise HTTPException(
            status_code=403,
            detail="Own session key and account required",
        )
    # Refused before anything changes: an attach re-opens an ended session.
    attached = (
        None
        if body.attach is None
        else await _require_attach(request, identity, session_id=body.attach)
    )
    audience = await _chat_audience(
        request,
        cli_session_id=body.cli_session_id
        if attached is None
        else attached.cli_session_id,
    ) or granted_audience(
        identity,
        private=body.private,
    )
    # An AgentSession is an inquiry like any other, so it carries the same
    # account attribution: default to the authenticated creator, validate it is
    # an active user before minting the row (the gate the submit / edit routes
    # run). Without this the row's account would be the unvalidated routing
    # handle the Store defaults to.
    account = body.account or identity.email
    require_attributable(identity, account)
    await assert_account_active(store.engine, account)
    # ``start_session`` reserves the routing name and inserts the row, retrying
    # on the live-owner unique index so two concurrent starts for one actor get
    # distinct ``#N`` names (the reserve-then-insert window is otherwise racy).
    # ``start_session`` returns the granted routing name too, but the response
    # reads it back off the persisted row (``row.owner``) below.
    session_id, _granted_actor, next_seq = await store.start_session(
        SubmitAgentSession(
            # A partner titles a chat by its first line, and every key reads the row.
            title=CHAT_TITLE
            if (body.cli_session_id or "").startswith(CHAT_SESSION_PREFIX)
            else body.title or f"{body.cli} session",
            cli=body.cli,
            cli_session_id=body.cli_session_id,
            started=body.started,
            rooms=body.rooms,
            account=account,
            idempotency_key=body.idempotency_key,
        ),
        requested_actor=_actor(identity, body.actor),
        audience=audience,
        api_key_id=identity.api_key_id,
        attach=body.attach,
    )
    row = await require_session(store, session_id)
    require_session_write_access(identity, row)
    get_hub(request).nudge()
    return SessionStartResponse(
        id=session_id,
        # The event log's continuation seq: 0 for a fresh session, ``max(seq)+1``
        # for a resumed one, so the client seeds its sequence and appends.
        seq=next_seq,
        cli_session_id=row.cli_session_id,
        actor=row.owner,
        audience=row.audience,
    )


@router.post("/api/sessions/{session_id}/inbound")
async def session_inbound_enqueue_route(
    session_id: UUID,
    body: InboundEnqueueRequest,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> InboundEnqueueResponse:
    """Enqueue a message to inject into a live session's input.

    The sender is **attested by the route**, never trusted from the body: the
    stored ``source`` is always the authenticated principal, so an agent
    cannot forge another sender. The request body (:class:`InboundEnqueueRequest`)
    carries no ``source`` at all -- a client that sends one gets 422, not a
    silently-ignored field. The session must exist and have a recent inbound
    poll; an ended or stale session is rejected (409) because no poller will
    drain it. A session whose audience the sender does not read is 403
    ``session_audience``, unless the sender is the configured assistant sending for
    an ``asker`` who is that session's account or a writer who holds its audience.

    Args:
      session_id: UUID of the session to message.
      body: Message text to inject, and the user the assistant sends it for.
      request: Starlette request object.
      identity: Authenticated user (sender is attested, not from body).

    Returns:
      response: InboundEnqueueResponse with list of (session_id,
        message_count) pairs of sessions enqueued (always the one target).

    """
    session = await require_session(get_store(request), session_id)
    asker = await resolve_asker(request, identity, email=body.asker)
    await require_session_access(
        request,
        identity,
        session=session,
        mode="input",
        asker=asker,
    )
    if session.ended is not None:
        raise HTTPException(
            status_code=409,
            detail=f"session {session_id} has ended; cannot enqueue",
        )
    if is_a_science_chat(session):
        raise _no_direct_line()
    inbound = get_inbound(request)
    # A detached campaign's first line is queued before its machine opens the run.
    # Only the configured assistant's opening key may seed that asker's labelled
    # campaign session without a poller; every ordinary enqueue still needs one.
    prestart_campaign = (
        asker is not None
        and asker.role in {"writer", "admin"}
        and session.account == asker.email
        and identity.api_key_id is not None
        and identity.api_key_id == session.opened_by_api_key_id
        and (session.cli_session_id or "").startswith("campaign:")
        and "campaign" in (session.labels or ())
        and body.kind == "line"
    )
    if not inbound.has_poller(session_id) and not prestart_campaign:
        raise HTTPException(status_code=409, detail="No active inbound poller")
    # One dedup path: route through ``send_once`` (same primitive as the
    # ``/api/messages`` send) so a retry reusing the ``Idempotency-Key`` is a
    # no-op instead of a double-injection. The receipt reports the current
    # queue depth -- unchanged on a replay because nothing was re-enqueued.
    inbound.send_once(
        _idempotency_key(request),
        [
            (
                session_id,
                Inbound(
                    text=body.text,
                    kind=body.kind,
                    payload=body.payload,
                    source=identity.email,
                    source_role=identity.role,
                ),
            ),
        ],
    )
    return InboundEnqueueResponse(queued=inbound.pending(session_id))


@router.post("/api/messages")
async def send_message_route(
    body: SendMessage,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> SendMessageResponse:
    """Resolve a routing name to live sessions and enqueue the message.

    The ergonomic front door for ``trax send``: the server resolves ``actor``
    (optionally scoped to ``room``) to the live sessions that match and
    enqueues the text to each, so the caller never needs a session id. Sender
    is route-attested (the principal), never the body. Delivery is
    drop-if-absent -- the response lists the sessions enqueued; an empty list
    means no live session matched.

    Policy: any ``writer`` may message the agent of any session whose audience it
    reads. The configured assistant may message one it does not read only for an
    ``asker`` it names, who is that session's account or a writer who holds its
    audience: it relays a user's line and passes on that user's rights, not its own.
    Sessions are a shared workspace and cross-agent messaging is the feature, so there
    is no per-opener ownership gate here (or on the direct ``/inbound`` route). The
    sender is always truthfully attested. When any live session the name resolves to
    is one the sender may not message, the whole send is 403 ``session_audience`` and
    nothing is queued, so a send never reports a delivery that did not happen.

    A science chat is reached through its station, so a browser's send to one is
    refused with 409 ("Open its station to talk to this chat"), whether or not the
    chat is being drained; an agent key's send to a chat being drained is refused
    with 409 too.

    A bare ``actor`` (no ``room``) is rejected when the matched session belongs
    to more than one room: a single PTY interleaves every room's messages, so
    the caller must name the room (``@actor:room``) to give the agent context.
    Retries that reuse the request's ``Idempotency-Key`` return the original
    receipt without re-enqueuing (the send path writes no ``change_log`` row to
    dedup against), but only a delivery that reached at least one live session
    is recorded: an empty send leaves the key unrecorded so a later retry --
    once the session is live -- still delivers instead of replaying nothing.

    Args:
      body: Message text and optional room scope to address by actor name, and the
        user the assistant sends it for.
      request: Starlette request object.
      identity: Authenticated user (sender is attested, not from body).

    Returns:
      response: SendMessageResponse with list of (session_id,
        message_count) pairs of all sessions enqueued (may be empty if no
        live session matched).

    """
    inbound = get_inbound(request)
    idempotency_key = _idempotency_key(request)

    store = get_store(request)
    # Once for every session the name reaches: the asker's rights do not vary by session.
    asker = await resolve_asker(request, identity, email=body.asker)
    # Resolve liveness BEFORE the in-memory critical section: the DB resolve
    # (and the per-session liveness re-check below) is async and must not run
    # while ``send_once`` holds the queue lock.
    matches = await store.resolve_live_sessions(body.actor, room=body.room)
    if body.room is None:
        for _, rooms in matches:
            if len(rooms) > 1:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"@{body.actor} is in rooms {sorted(rooms)}; "
                        f"address one explicitly (e.g. @{body.actor}:{min(rooms)})"
                    ),
                )

    targets: list[tuple[UUID, Inbound]] = []
    for session_id, rooms in matches:
        # Re-check liveness immediately before building the target: ``resolve``
        # and the enqueue are separate steps, and a concurrent ``end`` between
        # them would otherwise drop the message into a queue nobody drains.
        session = await store.get_inquiry(session_id)
        if session is not None:
            # Before the liveness skip: the send is refused whole, so a caller
            # cannot read which hidden sessions are live from what was delivered.
            await require_session_access(
                request,
                identity,
                session=cast(AgentSession, session),
                mode="input",
                asker=asker,
            )
        if session is None or cast(AgentSession, session).ended is not None:
            continue
        # A browser reaches a chat through its station, whether or not anything is
        # draining the chat now; an agent key's send to an idle chat is dropped, as
        # it is to any session no one drains.
        if identity.api_key_id is None and is_a_science_chat(
            cast(AgentSession, session),
        ):
            raise HTTPException(
                status_code=409,
                detail="Open its station to talk to this chat",
            )
        if not inbound.has_poller(session_id):
            continue
        if is_a_science_chat(cast(AgentSession, session)):
            raise _no_direct_line()
        scoped_room = body.room or (rooms[0] if rooms else None)
        targets.append(
            (
                session_id,
                Inbound(
                    text=body.text,
                    source=identity.email,
                    source_role=identity.role,
                    room=scoped_room,
                ),
            ),
        )

    # One atomic critical section dedups, enqueues every target, and records
    # the receipt -- so two concurrent same-key sends can't both pass the
    # dedup check and double-enqueue. A replay returns the original receipt;
    # an empty non-replayed delivery is not recorded (the retry stays a real
    # send once a session comes live).
    return SendMessageResponse(delivered=inbound.send_once(idempotency_key, targets))


@router.get(
    "/api/sessions/{session_id}/inbound",
)
async def session_inbound_drain_route(
    session_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    *,
    wait_sec: float = 0.0,
    # The rule ``trax run --detach`` names its host by, which the server cannot import:
    # a stricter one here would refuse every drain of a host it allowed.
    run: Annotated[
        str | None,
        Query(pattern=r"^[A-Za-z0-9][A-Za-z0-9._@#+=:-]{0,127}$"),
    ] = None,
    machine: Annotated[str | None, Query(pattern=NAME_PATTERN)] = None,
    cwd: Annotated[
        str | None,
        Query(max_length=1_024, pattern=r"^[^\x00-\x1f\x7f-\x9f]*$"),
    ] = None,
) -> DrainInboundResponse:
    """Drain all pending inbound messages for a session (oldest first).

    Read by the session's ``trax run`` to inject queued messages into the CLI.
    A viewer key may drain only the session it opened. Unknown session -> 404.

    ``wait_sec`` holds the request open until a message arrives, so the caller
    waits instead of asking repeatedly: one held request per session rather
    than one round trip per interval, and delivery on arrival rather than up
    to an interval late. Zero (the default) returns whatever is pending now,
    preserving the original contract for any caller that does not want to
    block. The ceiling keeps a held request shorter than the idle timeout of
    a proxy that would otherwise cut it. Ending the session ends the hold, so
    the run that ends it as it exits is not kept waiting on its own poller.

    The drain is 403 ``session_audience`` unless the caller opened the session or reads
    its audience.

    Writers retain shared access to the drains of ordinary sessions, but a science
    chat and the assistant's service session are drained by their opening key alone
    (403 otherwise). Viewer access is scoped to the opening API key so reading chat
    does not grant graph-write permission.

    A science chat's queue has one drainer, the provider its starter's station names.
    For ``claude`` or ``codex`` a drain by the configured assistant's key is 409
    ``provider_is_cli``, and the run that drains it may be under any key a chat takes
    a run from (its opening key or its starter's; 403 otherwise). For ``assistant`` a
    drain by its opening key that is not the assistant's is 409
    ``provider_is_assistant``. Either holds from the moment the station switches.

    ``run``, ``machine`` and ``cwd`` say where the draining run is, and the lease the
    drain takes keeps them for Chat to show (a malformed one is 422). They are the
    caller's own word about its own run, shown and never trusted; a run that names
    none is shown as it was before runs named themselves.

    Args:
      session_id: UUID of the session to drain messages from.
      request: Starlette request object.
      identity: Authenticated reader and, for viewers, the opening key.
      wait_sec: Seconds to hold the request open for a message to arrive
        (clamped to [0, 30]; 0 returns queued messages immediately).
      run: The ``trax run --detach`` host the run is, which ``trax run attach``
        takes; absent for a run in a terminal of its own.
      machine: The run's machine, as ``trax machine connect`` names it.
      cwd: The run's working directory, ``~/...`` under its home: at most 1,024
        characters, none of them a control character.

    Returns:
      response: DrainInboundResponse with list of pending messages (text, kind,
        payload, source, room), oldest first, or empty list if none are queued.

    """
    store = get_store(request)
    session = await require_session(store, session_id)
    await require_session_access(request, identity, session=session, mode="drain")
    provider = await _chat_provider_of(request, session=session)
    if provider is not None and provider != "assistant":
        _refuse_the_other_provider(request, identity, provider=provider)
        # The run is the starter's own, under whichever of their keys its machine has.
        await require_chat_writer(request, identity, session=session)
    # Its queue holds every user's Chat, and a science chat's queue holds its posters'
    # lines, so no one but the opening key reads either: a drain by another writer
    # would take the owner's lines.
    elif (
        identity.api_key_id != session.opened_by_api_key_id
        and await _takes_chat_lines(
            request,
            session=session,
        )
    ):
        raise HTTPException(
            status_code=403,
            detail="A chat session is drained by its opening key",
        )
    else:
        _refuse_the_other_provider(request, identity, provider=provider)
    if identity.role == "viewer":
        async with store.engine.acquire() as conn:
            owned = await conn.fetchval(
                "SELECT 1 FROM inquiries AS sess "
                "JOIN api_keys AS credential "
                "ON credential.id = sess.agentsession_opened_by_api_key_id "
                "WHERE sess.id = $1 AND credential.id = $2 "
                "AND credential.user_id = $3 AND credential.revoked_at IS NULL",
                session_id,
                identity.api_key_id,
                identity.user_id,
            )
        if not owned:
            raise HTTPException(status_code=403, detail="Own session key required")
    inbound = get_inbound(request)
    # A run closed for silence that polls again was alive all along -- cut off,
    # not dead -- so it gets its session back before this poll is served.
    if session.ended is not None and await revive_if_reaped(
        store,
        session_id=session_id,
    ):
        session = await require_session(store, session_id)
    polling = session.status == "active" and session.ended is None
    if polling:
        if not inbound.has_poller(session_id):
            get_hub(request).nudge()
        inbound.mark_poller(session_id, run=run, machine=machine, cwd=cwd)
        await store.record_session_seen(
            session_id,
            at=datetime.now(UTC),
            polled=True,
        )
    # Ceiling on how long the inbound drain holds a request open. Long enough that
    # a waiting caller re-arms rarely, short enough to stay under the idle timeout
    # of an intermediary that would otherwise cut the connection mid-wait.
    held = min(max(wait_sec, 0.0), 30.0)
    key = _idempotency_key(request)
    drained = await get_replays(request).inbound.run(
        None if key is None else (identity.user_id, identity.api_key_id, key),
        fingerprint=str(session_id),
        operation=partial(
            _drain_inbound,
            inbound,
            session_id=session_id,
            wait_sec=held,
            taker=identity.api_key_id,
        ),
    )
    return DrainInboundResponse(
        messages=[
            InboundDrainItem(
                text=m.text,
                kind=m.kind,
                payload=m.payload,
                source=m.source,
                source_role=m.source_role,
                room=m.room,
                context=m.context,
            )
            for m in drained
        ],
    )


@router.post("/api/sessions/{session_id}/end")
async def session_end_route(
    session_id: UUID,
    body: SessionEnd,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> SessionEndResponse:
    """Close a session atomically and release its inbound queue.

    ``end_session`` stamps ``ended`` + ``status`` (and backfills
    ``cli_session_id`` when given) in one transaction, so a partial
    failure can never leave a zombie session -- ``ended`` set yet
    ``status`` non-terminal -- and the inbound queue is drained only after
    the close commits, so a failed end never silently discards undelivered
    steering messages. A second end on an already-closed session is a 409. A
    session whose audience the caller neither reads nor opened is 403
    ``session_audience``. A science chat is closed by a key that may run on it: its
    opening key, the assistant's or its starter's (403 otherwise).

    Args:
      session_id: UUID of the session to close.
      body: Session status and optional cli_session_id backfill.
      request: Starlette request object.
      identity: Authenticated user (audit actor for the change).

    Returns:
      response: SessionEndResponse with session id and committed ended
        timestamp (original value on idempotent replay).

    """
    store = get_store(request)
    session = await require_session(store, session_id)
    require_session_write_access(identity, session)
    await require_session_access(request, identity, session=session, mode="write")
    await require_chat_writer(request, identity, session=session)
    # Ordinary vendor-ID finalization keeps shared writer access; only a chat
    # membership change uses the stricter ID-edit guard.
    if (
        body.cli_session_id is not None
        and body.cli_session_id != session.cli_session_id
        and (
            is_a_science_chat(session)
            or body.cli_session_id.startswith(CHAT_SESSION_PREFIX)
        )
    ):
        await require_session_id_change(
            request,
            identity,
            session_id,
            value=body.cli_session_id,
        )
    actor = _actor(identity, body.actor)
    # Always stamp a real ``ended``: ``ended IS NULL`` is the "live" predicate
    # (the active/previous split), so a complete session must never leave it
    # NULL even when the client omitted a timestamp.
    requested_ended = body.ended or datetime.now(UTC)
    # ``end_session`` returns the COMMITTED ``ended`` -- the value just stamped,
    # or the originally-stored one on an idempotent replay -- so two same-key
    # /end calls echo the identical receipt, not two fresh ``now()`` values.
    committed_ended = await store.end_session(
        session_id,
        ended=requested_ended,
        cli_session_id=body.cli_session_id,
        api_key_id=identity.api_key_id,
        actor=actor,
    )
    # Release any inbound messages queued for a now-dead session (its poller
    # is gone; holding them only leaks the process-local buffer). Only after
    # a clean close, so a failed end doesn't discard undelivered messages.
    inbound = get_inbound(request)
    inbound.forget_poller(session_id)
    service = await _is_a_chat_service(request, session=session)
    # A service session that names itself is resumed by its next start, which
    # drains what was posted meanwhile; releasing it here would lose lines that
    # were answered 200 and that no chat session holds yet.
    if not (service and session.cli_session_id is not None):
        await _release_unread(
            request,
            session_id=session_id,
            session=session,
            service=service,
        )
    get_hub(request).nudge()
    return SessionEndResponse(id=session_id, ended=committed_ended)


def _no_direct_line() -> HTTPException:
    """Refuse a line sent straight to a science chat's session."""
    return HTTPException(
        status_code=409,
        detail=(
            "A science chat takes lines through POST /api/chats, which names the "
            "poster and the conversation; a line sent to its session directly has "
            "neither"
        ),
    )


def _actor(identity: AuthIdentity, supplied: str | None) -> str:
    """Return the audit actor: the client-supplied value, else the principal email."""
    return supplied or identity.email


async def _chat_provider_of(
    request: Request,
    *,
    session: AgentSession,
) -> ChatProvider | None:
    """Return who answers the science chat ``session`` holds; None for another session."""
    conversation_id = chat_conversation_id(session.cli_session_id)
    if conversation_id is None:
        return None
    async with get_store(request).engine.acquire() as conn:
        return await read_chat_provider(
            conn,
            assistant=get_assistant(request),
            conversation_id=conversation_id,
        )


# A switch takes hold at once: the provider's queue may already hold lines for the new
# drainer, and a drain by the old one would take them. An assistant turn already running
# is not stopped; its answer is recorded under its key, which this does not touch.
def _refuse_the_other_provider(
    request: Request,
    identity: AuthIdentity,
    *,
    provider: ChatProvider | None,
) -> None:
    """Refuse a chat's drain by a key its station's provider is not."""
    assistant = get_assistant(request)
    if provider is None or assistant is None:
        return
    by_assistant = identity.email.lower() == assistant.email.lower()
    if provider != "assistant" and by_assistant:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "provider_is_cli",
                "message": f"This chat's station answers it with {provider}",
            },
        )
    if provider == "assistant" and not by_assistant:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "provider_is_assistant",
                "message": "This chat's station answers it with the assistant",
            },
        )


async def _takes_chat_lines(request: Request, *, session: AgentSession) -> bool:
    """Say whether ``session`` queues chat lines: a chat or a chat's service session."""
    return is_a_science_chat(session) or await _is_a_chat_service(
        request,
        session=session,
    )


async def _is_a_chat_service(request: Request, *, session: AgentSession) -> bool:
    """Say whether ``session`` is the assistant's service session."""
    assistant = get_assistant(request)
    if assistant is None:
        return False
    async with get_store(request).engine.acquire() as conn:
        opener = await opener_email(conn, api_key_id=session.opened_by_api_key_id)
    return is_assistant_session(
        assistant,
        actor=session.owner or "",
        email=opener or "",
    )


async def _is_the_assistants_chat(
    request: Request,
    *,
    session: AgentSession,
) -> bool:
    """Say whether ``session`` is a science chat the configured assistant opened."""
    assistant = get_assistant(request)
    if assistant is None:
        return False
    async with get_store(request).engine.acquire() as conn:
        opener = await opener_email(conn, api_key_id=session.opened_by_api_key_id)
    return is_chat_session(
        assistant,
        cli_session_id=session.cli_session_id,
        email=opener or "",
    )


# The chat's audience is taken without the hold check ``granted_audience`` makes: a
# ``private`` or ``staff`` chat was made so by its starter or by a staff read, and the
# key that opens its next session only joins it.
async def _chat_audience(request: Request, *, cli_session_id: str | None) -> str | None:
    """Return the audience of the chat a session opens under, when it is not open."""
    conversation_id = chat_conversation_id(cli_session_id)
    if conversation_id is None:
        return None
    async with get_store(request).engine.acquire() as conn:
        first = await read_first_session(
            conn,
            assistant=get_assistant(request),
            conversation_id=conversation_id,
        )
        audience = from_plain(
            await conn.fetchval(
                "SELECT agentsession_audience FROM inquiries WHERE id = $1",
                first,
            ),
            str,
            default=None,
        )
    return None if audience == OPEN else audience


async def _require_attach(
    request: Request,
    identity: AuthIdentity,
    *,
    session_id: UUID,
) -> AgentSession:
    """Return the session a run re-opens, refusing a caller who may not run on it."""
    session = await require_session(get_store(request), session_id)
    require_session_write_access(identity, session)
    await require_session_access(request, identity, session=session, mode="write")
    await require_chat_writer(request, identity, session=session)
    return session


async def _release_unread(
    request: Request,
    *,
    session_id: UUID,
    session: AgentSession,
    service: bool,
) -> None:
    """Take a closed session's unread lines off its queue, and say where they went."""
    unread = get_inbound(request).drain(session_id)
    if unread and await _is_the_assistants_chat(request, session=session):
        await _hand_to_assistant(request, unread)
    elif unread and service:
        _logger.warning(
            "dropped %d chat line(s) from %s: their service session ended and "
            "cannot be resumed",
            len(unread),
            _sources(unread),
        )


# The lines posted into a chat while its assistant was closing it were taken from the
# chat's queue by the close. The assistant's service session hears them instead, as it
# hears any line for a chat it does not have open, and reopens the chat; a post is not
# answered 200 and then lost.
async def _hand_to_assistant(request: Request, unread: list[Inbound]) -> None:
    """Queue ``unread`` for the assistant's live service session, if it has one."""
    inbound = get_inbound(request)
    async with get_store(request).engine.acquire() as conn:
        partner = await shared_partner(
            conn,
            inbound=inbound,
            assistant=get_assistant(request),
        )
    if partner is None or partner.session_id is None:
        _logger.warning(
            "dropped %d chat line(s) from %s: the assistant has no live session",
            len(unread),
            _sources(unread),
        )
        return
    for message in unread:
        _ = inbound.enqueue(partner.session_id, message)


def _sources(messages: list[Inbound]) -> str:
    """Name the senders of ``messages``, once each."""
    return ", ".join(sorted({message.source or "unknown" for message in messages}))


# ``ChangeIdMiddleware`` parses the header once (rejecting a malformed key with 400) and
# stashes the validated UUID on ``request.state``; this just reads it, so the parse +
# its failure branch live in exactly one place. ``getattr`` default covers a request
# that never passed through the middleware (a bare test app), leaving the send un-
# deduped -- the safe default.
def _idempotency_key(request: Request) -> UUID | None:
    """Return the request's already-parsed ``Idempotency-Key``, or ``None``."""
    key = getattr(request.state, "idempotency_key", None)
    if key is not None and not isinstance(key, UUID):
        raise ValueError("Expected key is None or isinstance(key, UUID).")
    return key


async def _drain_inbound(
    inbound: InboundQueue,
    *,
    session_id: UUID,
    wait_sec: float,
    taker: UUID | None,
) -> list[Inbound]:
    """Consume and attest a batch once, before its receipt can be replayed."""
    # Receipt work starts on a task: an end may have revoked the lease since the
    # route marked it. Keep this check beside waiter registration, with no yield
    # between them, or shutdown can leave a new hold parked for its full timeout.
    drained = (
        await inbound.await_messages(session_id, timeout_sec=wait_sec)
        if wait_sec and inbound.has_poller(session_id)
        else inbound.drain(session_id)
    )
    inbound.hand_out(drained, taker=taker)
    return drained
