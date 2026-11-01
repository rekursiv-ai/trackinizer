"""Agent-session lifecycle and messaging routes: ``start`` / ``end`` / inbound.

The server side of the agent runner's capture-and-sync layer. A run opens a
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
    get_inbound,
    get_replays,
    get_store,
    nudge_streams,
)
from trackinizer.server.api.session_access import (
    granted_audience,
    request_principal,
    require_attributable,
    require_session,
    require_session_access,
    require_session_write_access,
    resolve_asker,
)
from trackinizer.server.asks import release_open_asks
from trackinizer.server.auth import (
    AuthIdentity,
    assert_account_active,
    require_role,
)
from trackinizer.server.inbound import Inbound, InboundQueue
from trackinizer.server.session_reaper import revive_if_reaped
from trackinizer.server.store.session import RunPlace
from trackinizer.types.inquiries import AgentSession
from trackinizer.wire.bodies import SubmitAgentSession
from trackinizer.wire.wire_machines import NAME_PATTERN
from trackinizer.wire.wire_sessions import (
    RUN_NAME_PATTERN,
    RUN_STOPPED,
    DrainInboundResponse,
    InboundDrainItem,
    InboundEnqueueRequest,
    InboundEnqueueResponse,
    SendMessage,
    SendMessageResponse,
    SessionEnd,
    SessionEndResponse,
    SessionRunState,
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
    owner stamped on the row and is echoed in the response so the run
    adopts whatever it was given.

    The session opens into the audience the caller's key is pinned to, else
    ``open``, or ``private`` when ``private`` asks; a pin the caller no longer holds
    is 403 ``audience_not_held``, and so is ``private`` under a pin to ``staff``. A
    resume asked to be ``private`` hides the ``open`` session it re-attaches, as the
    key that opened it may, and is 409, leaving it as it was, for a ``staff`` one. A
    session under the id of a science chat that is not ``open`` takes the chat's
    audience instead, so a chat stays ``private`` or ``staff`` whichever key opens
    its next session; a chat's first session takes ``private`` when the canvas that
    holds the chat chose it (its ``opens_as``). A ``cli_session_id`` of a deleted
    science chat (``chat:<id>``) is 410 ``chat_deleted``. A science chat's session is
    titled ``Science chat`` whatever ``title`` says, since every key reads the row and
    only the chat's members read its lines.

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
    (
        None
        if body.attach is None
        else await _require_attach(request, identity, session_id=body.attach)
    )
    audience: str | None = None
    title = body.title or f"{body.cli} session"
    audience = audience or granted_audience(identity, private=body.private)
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
            title=title,
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
        principal=await request_principal(request, identity),
    )
    row = await require_session(store, session_id)
    require_session_write_access(identity, row)
    nudge_streams(request)
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
    Sessions are a shared canvas and cross-agent messaging is the feature, so there
    is no per-opener ownership gate here (or on the direct ``/inbound`` route). The
    sender is always truthfully attested. When any live session the name resolves to
    is one the sender may not message, the whole send is 403 ``session_audience`` and
    nothing is queued, so a send never reports a delivery that did not happen.

    A science chat is reached through its canvas, so a browser's send to one is
    refused with 409 ("Open its canvas to talk to this chat"), whether or not the
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
        if not inbound.has_poller(session_id):
            continue
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
    run: Annotated[str | None, Query(pattern=RUN_NAME_PATTERN)] = None,
    machine: Annotated[str | None, Query(pattern=NAME_PATTERN)] = None,
    cwd: Annotated[
        str | None,
        Query(max_length=1_024, pattern=r"^[^\x00-\x1f\x7f-\x9f]*$"),
    ] = None,
    cli: Annotated[str | None, Query(pattern=r"^[a-z][a-z0-9_-]{0,31}$")] = None,
    instance: Annotated[str | None, Query(pattern=r"^[0-9a-f]{32}$")] = None,
    runs: bool = False,
) -> DrainInboundResponse:
    """Drain all pending inbound messages for a session (oldest first).

    Read by the session's run to inject queued messages into the CLI.
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

    A science chat's queue has one drainer, the provider its starter's canvas names,
    and one live run. For ``claude`` or ``codex`` a drain by the configured assistant's
    key is 409 ``provider_is_cli``, and the run that drains it may be under any key a
    chat takes a run from (its opening key or its starter's; 403 otherwise). For
    ``assistant`` a drain by its opening key that is not the assistant's is 409
    ``provider_is_assistant``. Either holds from the moment the canvas switches.

    A chat answered by a CLI is drained by a run that names its ``cli``: a drain that
    names none, as a ``trax`` older than the name sends, is 426 ``cli_required`` and
    takes no lease, since the server cannot tell which CLI polls. A drain by the other
    CLI than the canvas's is 409 ``provider_is_other_cli``. The lease records the
    named CLI, and the chat is live for a provider only while a poller that named it
    holds the lease. ``instance`` is an id the run minted for itself; a drain whose
    ``instance`` differs from that of the unexpired lease of the same CLI is 409
    ``chat_run_live``, so one chat never has two runs draining it. So is a drain by
    a run the server replaced when it reattached the chat (``POST .../reattach``).

    A drain of a CLI chat's session that has ended, as when the person closed the chat,
    is 409 ``session_ended`` and the run stops; a session the reaper closed for silence
    is reopened by the drain instead.

    ``run``, ``machine`` and ``cwd`` say where the draining run is, and the lease the
    drain takes keeps them for Chat to show (a malformed one is 422). They are the
    caller's own word about its own run, shown and never trusted; a run that names
    none is shown as it was before runs named themselves.

    ``runs`` says the run takes ``run`` messages, the commands a chat's starter clicks
    Run on; a chat queues one only while such a run holds the lease, since an older run
    would fail to read the drain that carries it and lose every message in it.

    Args:
      session_id: UUID of the session to drain messages from.
      request: Starlette request object.
      identity: Authenticated reader and, for viewers, the opening key.
      wait_sec: Seconds to hold the request open for a message to arrive
        (clamped to [0, 30]; 0 returns queued messages immediately).
      run: The name of the detached run, which an attach takes; absent for a run in
        a terminal of its own.
      machine: The run's machine, as the machine host names it.
      cwd: The run's working directory, ``~/...`` under its home: at most 1,024
        characters, none of them a control character.
      cli: The CLI the draining run is, as the run names it: a lowercase word of
        at most 32 characters.
      instance: The run's own id, 32 lowercase hexadecimal digits, the same on each of
        its drains.
      runs: Whether the run takes ``run`` messages.

    Returns:
      response: DrainInboundResponse with list of pending messages (text, kind,
        payload, source, room), oldest first, or empty list if none are queued.

    """
    store = get_store(request)
    inbound = get_inbound(request)
    try:
        polling = await _admit_drain(
            request,
            identity,
            session_id=session_id,
            instance=instance,
        )
    except HTTPException as refused:
        await _remember_refusal(
            request,
            identity,
            session_id=session_id,
            refused=refused,
        )
        raise
    if polling:
        # No await since the admission's lease test: two runs that start together
        # cannot both pass it.
        if not inbound.has_poller(session_id):
            nudge_streams(request)
        inbound.mark_poller(
            session_id,
            run=run,
            machine=machine,
            cwd=cwd,
            cli=cli,
            instance=instance,
            runs=runs,
            account=identity.email,
        )
        await store.record_session_seen(
            session_id,
            at=datetime.now(UTC),
            polled=True,
            place=RunPlace(machine=machine, run=run, cli=cli, cwd=cwd),
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


@router.post("/api/sessions/{session_id}/run-state", status_code=204)
async def session_run_state_route(
    session_id: UUID,
    body: SessionRunState,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> None:
    """Take what a run says it is doing, for Chat to show.

    The run reports between its drains, so a change shows at once, not after the
    drain it is parked in. The state stays on the session's lease: it lapses with it,
    and a drain of the same run leaves it as it is, so a run says a state other than
    idle again while it holds; only a change rechecks open canvases. It is the run's own
    word about its
    own run, shown and never trusted. A report from a run other than the one holding
    the lease, or while none does, changes nothing, and is answered the same.

    ``taken_over`` is the report that matters: Chat then refuses the chat's lines with
    409 ``chat_taken_over``, until a reattach starts a headless run again.

    The caller is held to the rules of the session's drain: its opening key, or for a
    chat answered by a CLI its starter's run (403 otherwise), and never the
    assistant's (409 ``provider_is_cli``).

    Args:
      session_id: The session the run drains.
      body: The state, and the run's own id.
      request: Starlette request object.
      identity: Authenticated run.

    """
    _ = await authorize_drain(
        request,
        identity,
        session_id=session_id,
    )
    inbound = get_inbound(request)
    # A run says its state again while it holds, which changes nothing a stream shows.
    before = inbound.poller_run(session_id)
    kept = inbound.report_state(session_id, instance=body.instance, state=body.state)
    if kept and (before is None or before.state != body.state):
        nudge_streams(request)


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
    opening key, the assistant's or its starter's (403 otherwise); its starter may also
    close it from a browser, which has no key. An end that names
    a ``cli`` other than the chat's provider is a 200 that changes nothing: it is the
    exit of a run a switch stopped, and the session is the chat's, now drained by the
    provider its canvas chose. An end that names a ``cli`` and an ``instance`` is
    the same 200 that changes nothing when the run it names was replaced, or another
    run of that CLI holds the session's lease: it is the exit of a run a reattach
    took over, and the run holding the chat goes on. Any other end first releases
    the asks the run left open: an ``$interaction_resolved`` record with
    ``by: released`` for each, which the
    closed session would take no more. A chat's run that ended before it asked for its
    lines says why (``failed``), and the chat's canvas says it as its ``refused``.

    Args:
      session_id: UUID of the session to close.
      body: Session status, optional cli_session_id backfill, and the ending run's
        CLI and instance.
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
    # A reattach replaces a run that may quit before its drain is turned away: its end
    # would close the chat the new run holds, take that run's lease and release its asks.
    if (
        body.cli is not None
        and body.instance is not None
        and get_inbound(request).held_by_another(
            session_id,
            cli=body.cli,
            instance=body.instance,
        )
    ):
        return SessionEndResponse(id=session_id, ended=session.ended)
    actor = _actor(identity, body.actor)
    # Before the close, which takes records no more: an ask the run left open can be
    # answered by no one once it is gone.
    _ = await release_open_asks(store, session_id=session_id, at=datetime.now(UTC))
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
    _ = inbound.drain(session_id)
    nudge_streams(request)
    return SessionEndResponse(id=session_id, ended=committed_ended)


# The run is its owner's own, under whichever of their keys its machine has.
#
# A run's report of its own state is held to the same rule, since it speaks for the same
# run; it names no CLI (``names_cli``), the lease it reports to already does.
async def authorize_drain(
    request: Request,
    identity: AuthIdentity,
    *,
    session_id: UUID,
) -> AgentSession:
    """Refuse a caller who may not drain a session; return the session.

    Args:
      request: The drain or report, which carries the store.
      identity: The caller.
      session_id: The session.
      cli: The CLI the drain names, if any.
      names_cli: Whether the caller is a drain, which names its CLI.

    Returns:
      session: The session, once the caller may drain it.

    Raises:
      HTTPException: 403 when the caller may not drain it, or is a viewer whose key
        did not open it.

    """
    store = get_store(request)
    session = await require_session(store, session_id)
    await require_session_access(request, identity, session=session, mode="drain")
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
    return session


async def _admit_drain(
    request: Request,
    identity: AuthIdentity,
    *,
    session_id: UUID,
    instance: str | None,
) -> bool:
    """Refuse a drain the session may not take; return whether it renews the lease."""
    # A 403, which every run ever shipped takes as the end of its run, where it
    # retries a 409 code it does not know.
    if instance is not None and get_inbound(request).stopped(
        session_id,
        instance=instance,
    ):
        raise HTTPException(
            status_code=403,
            detail={"code": RUN_STOPPED, "message": "This run was stopped"},
        )
    store = get_store(request)
    session = await authorize_drain(
        request,
        identity,
        session_id=session_id,
    )
    # A run closed for silence that polls again was alive all along -- cut off,
    # not dead -- so it gets its session back before this poll is served.
    if session.ended is not None and await revive_if_reaped(
        store,
        session_id=session_id,
    ):
        session = await require_session(store, session_id)
    return session.status == "active" and session.ended is None


# Only a run of the chat's own account: a stranger's refused drain would otherwise tell
# the person why someone else was turned away from their chat.
async def _remember_refusal(
    request: Request,
    identity: AuthIdentity,
    *,
    session_id: UUID,
    refused: HTTPException,
) -> None:
    """Keep why a drain by a run of the session's own account was refused."""
    try:
        session = await require_session(get_store(request), session_id)
    except HTTPException:
        return
    if session.account.lower() != identity.email.lower():
        return
    detail: object = refused.detail
    message = (
        from_plain(detail, dict[str, object]).get("message")
        if isinstance(detail, dict)
        else detail
    )
    get_inbound(request).refuse(session_id, reason=str(message))


def _actor(identity: AuthIdentity, supplied: str | None) -> str:
    """Return the audit actor: the client-supplied value, else the principal email."""
    return supplied or identity.email


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
    return session


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
