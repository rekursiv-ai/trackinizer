"""Agent-session ingest routes: ``start`` / ``events`` / ``end``.

The server side of the ``trax run`` capture-and-sync layer. A run opens a
session (minting an ``AgentSession`` inquiry row, server-assigned id),
streams batches of turn-grained events into ``agent_session_events``, then
closes it. ``GET .../events`` reads them back, paginated.

The mutating routes require the ``writer`` role; the read requires
``viewer``. Tenant scope is derived by joining to ``inquiries``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from trackinizer.server.api._deps import get_inbound, get_store
from trackinizer.server.api._routes_shared import (
    parse_seq_ranges,
)
from trackinizer.server.auth import (
    AuthIdentity,
    assert_account_active,
    require_role,
)
from trackinizer.server.inbound import Inbound
from trackinizer.server.store.core import Store
from trackinizer.types.inquiries import AgentSession
from trackinizer.wire.bodies import SubmitAgentSession
from trackinizer.wire.routes import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
)
from trackinizer.wire.wire_sessions import (
    KINDS,
    AppendEventsRequest,
    AppendEventsResponse,
    DrainInboundResponse,
    InboundDrainItem,
    InboundEnqueueRequest,
    InboundEnqueueResponse,
    ReadEventsResponse,
    SendMessage,
    SendMessageResponse,
    SessionEnd,
    SessionEndResponse,
    SessionStart,
    SessionStartResponse,
)


router = APIRouter()


def _actor(identity: AuthIdentity, supplied: str | None) -> str:
    """The audit actor: the client-supplied value, else the principal email."""
    return supplied or identity.email


async def _require_session(store: Store, session_id: UUID) -> AgentSession:
    """Fetch an AgentSession row or raise 404; reject non-session ids.

    Discriminate by ``type(row).__name__`` rather than ``isinstance``: under
    some test import paths the dataclass module is loaded twice, so the
    class object differs by identity while the kind is the same.
    """
    row = await store.get_inquiry(session_id)
    if row is None or type(row).__name__ != "AgentSession":
        raise HTTPException(status_code=404, detail=f"unknown session {session_id}")
    return cast(AgentSession, row)


@router.post("/api/sessions/start", status_code=201)
async def session_start_route(
    body: SessionStart,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> SessionStartResponse:
    """Open a session: mint an ``AgentSession`` row and return its server id.

    The session ``owner`` doubles as the routing name (``--as``). It is made
    unique among live sessions (suffix on collision); the granted name is the
    owner stamped on the row and is echoed in the response so ``trax run``
    adopts whatever it was given.
    """
    store = get_store(request)
    # An AgentSession is an inquiry like any other, so it carries the same
    # account attribution: default to the authenticated creator, validate it is
    # an active user before minting the row (the gate the submit / edit routes
    # run). Without this the row's account would be the unvalidated routing
    # handle the Store defaults to.
    account = body.account or identity.email
    await assert_account_active(store.engine, account)
    # ``start_session`` reserves the routing name and inserts the row, retrying
    # on the live-owner unique index so two concurrent starts for one actor get
    # distinct ``#N`` names (the reserve-then-insert window is otherwise racy).
    # ``start_session`` returns the granted routing name too, but the response
    # reads it back off the persisted row (``row.owner``) below.
    session_id, _granted_actor, next_seq = await store.start_session(
        SubmitAgentSession(
            title=body.title or f"{body.cli} session",
            cli=body.cli,
            cli_session_id=body.cli_session_id,
            started=body.started,
            rooms=body.rooms,
            account=account,
            idempotency_key=body.idempotency_key,
        ),
        requested_actor=_actor(identity, body.actor),
        api_key_id=identity.api_key_id,
    )
    row = await _require_session(store, session_id)
    return SessionStartResponse(
        id=session_id,
        # The event log's continuation seq: 0 for a fresh session, ``max(seq)+1``
        # for a resumed one, so the client seeds its sequence and appends.
        seq=next_seq,
        cli_session_id=row.cli_session_id,
        actor=row.owner,
    )


@router.post(
    "/api/sessions/{session_id}/events",
    dependencies=[Depends(require_role("writer"))],
)
async def session_events_route(
    session_id: UUID,
    body: AppendEventsRequest,
    request: Request,
) -> AppendEventsResponse:
    """Batch-append events to an open session; idempotent on ``(id, seq)``.

    Writer-gated, not owner-gated: AgentSessions are a shared workspace (design
    step 5 -- mutations ``writer``, reads ``viewer``), so any writer may append.
    ``opened_by_api_key_id`` is attribution + resume-correlation, not an access
    boundary.
    """
    store = get_store(request)
    await _require_session(store, session_id)
    appended, skipped = await store.append_events(session_id, body.events)
    return AppendEventsResponse(appended=appended, skipped=skipped)


@router.get(
    "/api/sessions/{session_id}/events",
    dependencies=[Depends(require_role("viewer"))],
)
async def read_session_events_route(
    session_id: UUID,
    request: Request,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
    seq_range: Annotated[list[str] | None, Query()] = None,
    kind: str | None = None,
) -> ReadEventsResponse:
    """Read one page of a session's events in ``seq`` order.

    Paginated so a caller never pulls a whole large session at once;
    mirrors the inquiry-list grammar (api.md 4.3), including the repeated
    ``seq_range=a..b`` union param. Event ``seq`` starts at 0
    (harness-assigned), so a bound accepts 0 here, unlike the inquiry
    ``seq`` which starts at 1.
    """
    if limit < 1 or limit > MAX_LIST_LIMIT:
        raise HTTPException(
            status_code=400, detail=f"limit must be in [1, {MAX_LIST_LIMIT}]"
        )
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0")
    # Event ``seq`` starts at 0 (harness-assigned).
    seq_ranges = parse_seq_ranges(seq_range, min_seq=0)
    if kind is not None and kind not in KINDS:
        raise HTTPException(status_code=400, detail=f"unknown event kind {kind!r}")
    store = get_store(request)
    await _require_session(store, session_id)
    events = await store.read_session_events(
        session_id,
        limit=limit,
        offset=offset,
        seq_ranges=seq_ranges,
        kind=kind,
    )
    return ReadEventsResponse(events=events)


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
    silently-ignored field. The session must exist; whether its ``trax run`` is
    still polling is not checked -- delivery is drop-if-absent and the receipt
    only reports the queue depth. An *ended* session is rejected (409): no
    poller will ever drain it.
    """
    session = await _require_session(get_store(request), session_id)
    if session.ended is not None:
        raise HTTPException(
            status_code=409, detail=f"session {session_id} has ended; cannot enqueue"
        )
    inbound = get_inbound(request)
    # One dedup path: route through ``send_once`` (same primitive as the
    # ``/api/messages`` send) so a retry reusing the ``Idempotency-Key`` is a
    # no-op instead of a double-injection. The receipt reports the current
    # queue depth -- unchanged on a replay because nothing was re-enqueued.
    inbound.send_once(
        _idempotency_key(request),
        [(session_id, Inbound(text=body.text, source=identity.email))],
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

    Policy: anyone (any ``writer``) may message anyone's agent -- this is
    intentional. Sessions are a shared workspace and cross-agent messaging is
    the feature, so there is no per-opener ownership gate here (or on the
    direct ``/inbound`` route). The sender is always truthfully attested.

    A bare ``actor`` (no ``room``) is rejected when the matched session belongs
    to more than one room: a single PTY interleaves every room's messages, so
    the caller must name the room (``@actor:room``) to give the agent context.
    Retries that reuse the request's ``Idempotency-Key`` return the original
    receipt without re-enqueuing (the send path writes no ``change_log`` row to
    dedup against), but only a delivery that reached at least one live session
    is recorded: an empty send leaves the key unrecorded so a later retry --
    once the session is live -- still delivers instead of replaying nothing.
    """
    inbound = get_inbound(request)
    idempotency_key = _idempotency_key(request)

    store = get_store(request)
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
                        f"address one explicitly (e.g. @{body.actor}:{sorted(rooms)[0]})"
                    ),
                )

    targets: list[tuple[UUID, Inbound]] = []
    for session_id, rooms in matches:
        # Re-check liveness immediately before building the target: ``resolve``
        # and the enqueue are separate steps, and a concurrent ``end`` between
        # them would otherwise drop the message into a queue nobody drains.
        session = await store.get_inquiry(session_id)
        if session is None or cast(AgentSession, session).ended is not None:
            continue
        scoped_room = body.room or (rooms[0] if rooms else None)
        targets.append(
            (
                session_id,
                Inbound(text=body.text, source=identity.email, room=scoped_room),
            )
        )

    # One atomic critical section dedups, enqueues every target, and records
    # the receipt -- so two concurrent same-key sends can't both pass the
    # dedup check and double-enqueue. A replay returns the original receipt;
    # an empty non-replayed delivery is not recorded (the retry stays a real
    # send once a session comes live).
    delivered = inbound.send_once(idempotency_key, targets)
    return SendMessageResponse(delivered=delivered)


def _idempotency_key(request: Request) -> UUID | None:
    """Return the request's already-parsed ``Idempotency-Key``, or ``None``.

    ``ChangeIdMiddleware`` parses the header once (rejecting a malformed key
    with 400) and stashes the validated UUID on ``request.state``; this just
    reads it, so the parse + its failure branch live in exactly one place.
    ``getattr`` default covers a request that never passed through the
    middleware (a bare test app), leaving the send un-deduped -- the safe
    default.
    """
    return getattr(request.state, "idempotency_key", None)


@router.get(
    "/api/sessions/{session_id}/inbound",
    dependencies=[Depends(require_role("writer"))],
)
async def session_inbound_drain_route(
    session_id: UUID,
    request: Request,
) -> DrainInboundResponse:
    """Drain all pending inbound messages for a session (oldest first).

    Polled by the session's ``trax run`` to inject queued messages into the
    CLI. ``writer`` like enqueue. Unknown session -> 404.

    Writer-gated, not owner-gated: AgentSessions are a shared workspace, so any
    writer may drain. In practice the session's own ``trax run`` is the only
    poller, but the route enforces no per-opener restriction.
    """
    await _require_session(get_store(request), session_id)
    drained = get_inbound(request).drain(session_id)
    return DrainInboundResponse(
        messages=[
            InboundDrainItem(text=m.text, source=m.source, room=m.room) for m in drained
        ]
    )


@router.post("/api/sessions/{session_id}/end")
async def session_end_route(
    session_id: UUID,
    body: SessionEnd,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> SessionEndResponse:
    """Close a session atomically and release its inbound queue.

    ``end_session`` stamps ``ended`` + ``status`` (and backfills
    ``cli_session_id`` when given) in one transaction, so a partial
    failure can never leave a zombie session -- ``ended`` set yet
    ``status`` non-terminal -- and the inbound queue is drained only after
    the close commits, so a failed end never silently discards undelivered
    steering messages. A second end on an already-closed session is a 409.
    """
    store = get_store(request)
    await _require_session(store, session_id)
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
    get_inbound(request).drain(session_id)
    return SessionEndResponse(id=session_id, ended=committed_ended)
