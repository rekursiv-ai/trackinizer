"""Agent-session lifecycle and messaging routes: ``start``, ``end`` and ``messages``.

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
from typing import TYPE_CHECKING, Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from trackinizer.server.api._deps import (
    get_replays,
    get_session_relay,
    get_store,
)
from trackinizer.server.api._routes_shared import refusal
from trackinizer.server.api.session_access import (
    granted_audience,
    require_attributable,
    require_session_access,
    require_session_write_access,
    resolve_asker,
)
from trackinizer.server.api.session_identity import (
    request_principal,
    require_session,
)
from trackinizer.server.api.session_relay_routes import stop_process
from trackinizer.server.auth import (
    AuthIdentity,
    assert_account_active,
    require_role,
)
from trackinizer.server.session_relay import InputRefusedError, NotLiveError
from trackinizer.types.inquiries import AgentSession
from trackinizer.wire.bodies import SubmitAgentSession
from trackinizer.wire.wire_session_relay import NewInput
from trackinizer.wire.wire_sessions import (
    SendMessage,
    SendMessageResponse,
    SessionEnd,
    SessionEndResponse,
    SessionStart,
    SessionStartResponse,
)


if TYPE_CHECKING:
    from collections.abc import Sequence


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
    key that opened it may, and is 409, leaving it as it was, for a ``staff`` one;
    with ``keep_audience`` it leaves the session in the audience it has, so ``private``
    hides only a session the start mints. A session under the id of a science chat that is not ``open`` takes the chat's
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
        account, idempotency_key, private, keep_audience).
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
        keep_audience=body.keep_audience,
        principal=await request_principal(request, identity),
    )
    row = await require_session(store, session_id)
    require_session_write_access(identity, row)
    return SessionStartResponse(
        id=session_id,
        # The event log's continuation seq: 0 for a fresh session, ``max(seq)+1``
        # for a resumed one, so the client seeds its sequence and appends.
        seq=next_seq,
        cli_session_id=row.cli_session_id,
        actor=row.owner,
        account=row.account,
        audience=row.audience,
    )


@router.post("/api/messages")
async def send_message_route(
    body: SendMessage,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> SendMessageResponse:
    """Send a message to one agent of a session, or to the live sessions of a name.

    The front door of ``trax send`` and of AgentSend across sessions. The target is
    ``to``, one agent of one session by the session's number or UUID, or ``actor``
    (optionally scoped to ``room``), a routing name the server resolves to the live
    sessions that match. Each live session is sent the text through the agent process
    serving it, which holds it while it starts. Delivery is drop-if-absent -- the
    response lists the sessions sent to; an empty list means no agent process serves
    a live session the target names now.

    The sender is written by the route, never read from the body: the caller's email,
    or ``label#number`` for an agent of another session. A body that names
    ``from_session`` must come with a key that session's attached agent process holds
    its socket with, else 403 ``not_its_process``; the session's number then follows
    ``from_label``. A key minted for an agent process must name it, and is 403
    ``not_its_process`` without. An unknown number or UUID in ``to`` is 404
    ``no_session``.

    Policy: only the session's account or opening key may send input; a science
    chat takes a message from whoever may post a line into it, as
    ``POST .../inputs`` does. The configured
    assistant may relay for a writer ``asker`` whose account holds the session, on
    that user's rights alone. Reading a session, sharing an organisation or being
    an admin grants no input rights. Same-account cross-agent messaging remains.
    When any live session the target resolves to is one the sender may not message,
    the whole send is 403 ``session_audience`` and nothing is sent, so a send never
    reports a delivery that did not happen.

    A browser reaches a science chat through its canvas, so its send to one is refused
    with 409 ("Open its canvas to talk to this chat"); a key's send reaches the chat's
    agent process as it reaches any session's.

    A bare ``actor`` (no ``room``) is rejected when the matched session belongs
    to more than one room: a single PTY interleaves every room's messages, so
    the caller must name the room (``@actor:room``) to give the agent context.
    Retries that reuse the request's ``Idempotency-Key`` return the original
    receipt without sending again (the send path writes no ``change_log`` row to
    dedup against), but only a delivery that reached at least one live session
    is recorded: an empty send leaves the key unrecorded so a later retry --
    once the session is live -- still delivers instead of replaying nothing.

    Args:
      body: The target, the text, the user the assistant sends it for, and an
        agent's own session, label and booking.
      request: Starlette request object.
      identity: Authenticated user (sender is attested, not from body).

    Returns:
      response: The ids of the sessions sent to (empty if none takes messages).

    """
    store = get_store(request)
    relay = get_session_relay(request)
    author = await _author(request, identity, body=body)
    # Once for every session the name reaches: the asker's rights do not vary by session.
    asker = await resolve_asker(request, identity, email=body.asker)
    targets: list[UUID] = []
    for session_id in await _sessions(request, body):
        # Re-check liveness immediately before building the target: ``resolve``
        # and the send are separate steps, and a concurrent ``end`` between
        # them would otherwise drop the message where nothing takes it.
        session = await store.get_inquiry(session_id)
        if session is not None:
            # Before the liveness skip: the send is refused whole, so a caller
            # cannot read which hidden sessions are live from what was delivered.
            await require_session_access(
                request,
                identity,
                session=cast(AgentSession, session),
                mode="post",
                asker=asker,
            )
        if session is None or cast(AgentSession, session).ended is not None:
            continue
        process = relay.process(session_id)
        if process is None or process.state == "detached":
            continue
        targets.append(session_id)
    key = _idempotency_key(request)
    # One receipt per key: a replay answers what the first send answered, and two
    # concurrent sends under one key send once.
    return SendMessageResponse(
        delivered=await get_replays(request).messages.run(
            None if key is None else (identity.user_id, identity.api_key_id, key),
            fingerprint="",
            operation=partial(_sent, request, targets, body=body, author=author),
        ),
    )


@router.post("/api/sessions/{session_id}/end")
async def session_end_route(
    session_id: UUID,
    body: SessionEnd,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> SessionEndResponse:
    """Close a session atomically, and stop the agent process serving it.

    ``end_session`` stamps ``ended`` + ``status`` (and backfills
    ``cli_session_id`` when given) in one transaction, so a partial
    failure can never leave a zombie session -- ``ended`` set yet
    ``status`` non-terminal. Then its agent process, if one serves it, is stopped as
    ``DELETE .../process`` stops one: its socket closes and its key is revoked a
    minute later, so an ended session keeps no agent, tools or key. A process that
    attaches to it again reopens it. A second end on an already-closed session is a 409. A
    session the caller neither opened nor holds the account of is 403
    ``session_audience``. A science chat is closed by a key that may run on it: its
    opening key, the assistant's or its starter's (403 otherwise); its starter may also
    close it from a browser, which has no key.

    Args:
      session_id: UUID of the session to close.
      body: When the session ended, an optional cli_session_id backfill and the
        audit actor.
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
    # After the end commits: a start racing it then attaches to an ended session,
    # which reopens it, rather than to a live one this end then strands.
    _ = await stop_process(request, session_id)
    return SessionEndResponse(id=session_id, ended=committed_ended)


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


# The session is never taken from the body on its word: a key names only a session whose
# attached agent process holds its socket with that very key, so no caller sends as a
# session it does not run, and two processes on one key each send as their own. A key
# minted for an agent process sends as an agent of its process's session alone: as its
# person it would put a line in any session of theirs, signed as theirs.
async def _author(
    request: Request,
    identity: AuthIdentity,
    *,
    body: SendMessage,
) -> str:
    """Return who sends ``body``: an agent of the caller's own process, else the caller."""
    if body.from_session is None and identity.process is None:
        return identity.email
    if body.from_session is None:
        raise refusal(
            403,
            code="not_its_process",
            message="an agent process's key sends as an agent of its session; name it",
        )
    assert isinstance(body.from_label, str)
    held = get_session_relay(request).socket_key(body.from_session)
    if identity.api_key_id is None or held != identity.api_key_id:
        raise refusal(
            403,
            code="not_its_process",
            message="this key holds the socket of no agent process of that session",
        )
    sender = await require_session(get_store(request), body.from_session)
    return f"{body.from_label}#{sender.seq}"


async def _sessions(
    request: Request,
    body: SendMessage,
) -> list[UUID]:
    """Return the sessions ``body``'s target names."""
    store = get_store(request)
    if body.to is None:
        assert isinstance(body.actor, str)
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
        return [session_id for session_id, _ in matches]
    named = body.to.session
    session_id = (
        named
        if isinstance(named, UUID)
        else await store.inquiry_id("AgentSession", named)
    )
    found = None if session_id is None else await store.get_inquiry(session_id)
    if not isinstance(found, AgentSession):
        raise refusal(404, code="no_session", message=f"no session {named}")
    return [found.id]


async def _sent(
    request: Request,
    targets: Sequence[UUID],
    *,
    body: SendMessage,
    author: str,
) -> list[UUID]:
    """Send ``body``'s text to each target's agent process; return those sent to."""
    relay = get_session_relay(request)
    sent: list[UUID] = []
    for session_id in targets:
        try:
            _ = relay.send_input(
                session_id,
                NewInput(
                    kind="UserMessage",
                    payload={"py/object": "UserMessage", "content": body.text},
                ),
                author=author,
                recipient="" if body.to is None else body.to.label,
                booked=body.booked,
            )
        # Its process went, or holds all the inputs it may, since it was found.
        except (NotLiveError, InputRefusedError):
            continue
        sent.append(session_id)
    return sent


# ``ChangeIdMiddleware`` parses the header once (rejecting a malformed key with 400) and
# stashes the validated UUID on ``request.state``; this just reads it, so the parse +
# its failure branch live in exactly one place. ``getattr`` default covers a request
# that never passed through the middleware (a bare test app), leaving the send un-
# deduped -- the safe default.
def _idempotency_key(request: Request) -> UUID | None:
    """Return the request's already-parsed ``Idempotency-Key``, or ``None``."""
    key = getattr(request.state, "idempotency_key", None)
    assert isinstance(key, UUID | None)
    return key
