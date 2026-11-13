"""Routes of a live session: a viewer's stream, a person's inputs, its process, the daemon's.

The relay behind them (``server/session_relay.py``) holds no store; these routes check
who may do what, mint and revoke the process's key, and call it.

Who may do what is one question, asked in one place each: may this caller write the
session (:func:`_require_writer`)? It answers the process routes and the process's
socket, and a plain session's inputs; a science chat's inputs ask who may post a line
into it instead. Nothing here gives the person who started a session a right of its
own.

- ``GET .../lines`` and ``GET .../process``: whoever may read the session.
- ``POST .../inputs``: a writer of the session; for a chat, whoever may post into it.
- ``PUT`` and ``DELETE .../process``: a writer of the session, with the writer role.
- The process's socket: a key of a writer of the session, with the writer role; never
  a cookie, which a handshake from another site's page would carry.
- The daemon's wait and its report: the host's own credential.

A viewer's stream and a process's socket outlive the check that opened them, so each
hands the relay its route's rule, and the relay's sweep asks it again of the caller as
its credential authenticates then.

A start finds its place before anything is minted: the host it runs on, which takes
it on its daemon's held wait, and the host whose login it uses. Either answers 409
``no_place`` when it cannot serve the start. The process's key is minted under the
caller's account, capped at the caller's role and at ``writer``, bound to the host and
named for the process and its session (``auth.process_key_name``). It reaches only the
routes an agent process calls (``wire_process_keys.PROCESS_KEY_ROUTES``), each for
its own process's session alone, and is refused 403 on every other before the route
acts (``auth.request_user``): a key that could do more would reach every session and
host of its account. Of the routes here that leaves its session's socket, which it
opens for the process it was minted for alone; elsewhere its session's parts and
records, its account's hosts and their tools, and a message sent as an agent of its
session (``sessions_routes``). It is revoked by its
name: when its daemon reports the process ended, a minute after a stop, 30 seconds
after its daemon took the start if it neither attached nor was reported by then, as
long after the start as a host takes to read offline if no daemon took it by then,
and with its host. A server with authentication off answers every route here 403, as the
tool wait does: a start mints a key, and its stand-in caller may own none.

A WebSocket cannot answer 403 after its handshake, so the process's socket accepts and
then closes with a code (``wire_session_relay``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Annotated, Final, cast
from uuid import UUID, uuid4

import asyncio
import time

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
)
from fastapi.responses import StreamingResponse
from starlette.websockets import WebSocketDisconnect
from treekle import from_plain

from trackinizer.server import auth
from trackinizer.server.api._deps import (
    auth_is_disabled,
    get_default_remote_agent_service,
    get_later,
    get_remote_agent_services,
    get_session_relay,
    get_store,
)
from trackinizer.server.api._routes_shared import (
    refusal,
    request_engine,
    require_instance,
)
from trackinizer.server.api.session_access import require_session_access
from trackinizer.server.api.session_identity import require_session
from trackinizer.server.audience import person_audiences
from trackinizer.server.auth import (
    AuthIdentity,
    create_api_key,
    effective_role,
    live_process_keys,
    process_key_minted,
    process_key_name,
    require_role,
    revoke_process_key,
)
from trackinizer.server.host_auth import current_host, host_revoked
from trackinizer.server.notify import SSE_PING, notify_after_commit, tx
from trackinizer.server.session_relay import (
    BAD_FRAME,
    HostRevokedError,
    InputRefusedError,
    NotLiveError,
    SessionRelay,
    process_frame,
)
from trackinizer.server.store.core import Store
from trackinizer.server.store.session import RunPlace
from trackinizer.wire.audience import STAFF
from trackinizer.wire.wire_daemon import OFFLINE_AFTER_SEC
from trackinizer.wire.wire_session_relay import (
    HOST_SESSIONS_ENDED_PATH,
    HOST_SESSIONS_WAIT_PATH,
    INPUT_REFUSED,
    LINES_PING_SEC,
    LOGINS_FACT,
    NO_PLACE,
    NOT_LIVE,
    REMOTE_AGENTS_FACT,
    SESSION_INPUTS_PATH,
    SESSION_LINES_PATH,
    SESSION_PROCESS_PATH,
    SESSION_PROCESS_WS_PATH,
    SESSIONS_FACT,
    AgentProcess,
    Hello,
    InputSent,
    NewInput,
    ProcessEnded,
    ProcessSpec,
    ServeSession,
    ViewerFrame,
)
from trackinizer.wire.wire_terminals import CLOSE_REFUSED
from trackinizer.wire.wire_tools import WAIT_MAX_SEC


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Mapping

    from starlette.requests import HTTPConnection

    from trackinizer.lib.postgres import Conn
    from trackinizer.server.api._deps import Sleep
    from trackinizer.server.config import RemoteAgentService
    from trackinizer.types.inquiries import AgentSession


__all__ = [
    "ProcessKeySweep",
    "process_socket",
    "read_process_route",
    "router",
    "send_input_route",
    "session_lines_route",
    "sessions_ended_route",
    "sessions_wait_route",
    "start_process_route",
    "stop_process",
    "stop_process_route",
]


router = APIRouter()

_REVOKE_AFTER_SEC: Final = 60.0
"""How long a stopped process keeps its key, so it can upload its last records."""

_HELLO_SEC: Final = 10.0
"""How long a process's socket may go without its first frame."""

_STARTED_SEC: Final = 30.0
"""How long a start its daemon took may go without its process's ``Hello`` or its
daemon's report before it is given up on: a start takes a few seconds."""

_SEEN_EVERY_SEC: Final = 60.0
"""How often a process's open socket tells the reaper its session is alive."""

_UNHELD_SEC: Final = 900.0
"""How long a process key may be held by no process ``starting`` or ``attached``
before a sweep revokes it. Longer than each window a key outlives its socket on
purpose: a stopped process's minute to upload (``_REVOKE_AFTER_SEC``), a start's
wait for its daemon to take it (``OFFLINE_AFTER_SEC``) and then to attach
(``_STARTED_SEC``), and the ten minutes a headless agent process redials a dropped
socket before it ends itself."""


@router.get(
    SESSION_LINES_PATH,
    response_class=StreamingResponse,
    responses={200: {"model": ViewerFrame}},
)
async def session_lines_route(
    session_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> StreamingResponse:
    """Stream a session's lines to a viewer as server-sent frames.

    Each frame is ``data: <json>``, one :data:`ViewerFrame` told apart by its
    ``type``: an ``Opened`` first, then the event lines of a reply still being written,
    if one is, then each line as it arrives. A ``ping`` event keeps a quiet stream
    open. A viewer that falls too far behind is closed, and opens again; so is one that
    may no longer read the session, and its next open is refused.

    Args:
      session_id: The session to watch.
      request: Request carrying the relay.
      identity: A viewer who may read the session.

    Returns:
      stream: Server-sent frames.

    Raises:
      HTTPException: 403 for a caller who may not read the session; 404 for no
        session.

    """
    await _require_reader(request, identity, session_id=session_id)
    return StreamingResponse(
        _sse_frames(
            get_session_relay(request).lines(
                session_id,
                allowed=partial(_answers, partial(_reader, request, session_id)),
            ),
        ),
        media_type="text/event-stream",
    )


@router.post(SESSION_INPUTS_PATH, response_model=InputSent)
async def send_input_route(
    session_id: UUID,
    body: NewInput,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> InputSent:
    """Send a session's root agent a line, a stop or a slash line, under the caller's name.

    Args:
      session_id: The session whose agent process takes it.
      body: What to send.
      request: Request carrying the relay.
      identity: Whoever may send the session an input.

    Returns:
      sent: The id the input's effect is told back under.

    Raises:
      HTTPException: 403 for a caller who may not write the session, or post into
        its chat; 404 for no session; 409 ``not_live`` when no process serves it,
        and ``input_refused`` for an input larger than a frame or past what its
        process holds unread.

    """
    session = await _session(request, session_id)
    await require_session_access(request, identity, session=session, mode="post")
    try:
        sent = get_session_relay(request).send_input(
            session_id,
            body,
            author=identity.email,
        )
    except NotLiveError as error:
        raise refusal(
            409,
            code=NOT_LIVE,
            message="no agent process serves this session; start one",
        ) from error
    except InputRefusedError as error:
        raise refusal(409, code=INPUT_REFUSED, message=str(error)) from error
    return InputSent(id=sent)


@router.put(SESSION_PROCESS_PATH, response_model=AgentProcess)
async def start_process_route(
    session_id: UUID,
    body: ProcessSpec,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> AgentProcess:
    """Bring up a session's agent process: on any host, with any host's login.

    The host's daemon is handed the start on its held wait, with a key minted for the
    process alone. A start no daemon takes within the 180 seconds a host takes to read
    offline is detached as ``its host did not take it``, and its key revoked.

    Args:
      session_id: The session to serve.
      body: The agent's host, its login's host, and the agent's flags.
      request: Request carrying the relay.
      identity: A writer of the session.

    Returns:
      process: The process, ``starting``; the one already serving when there is one.

    Raises:
      HTTPException: 403 for a caller who may not write the session; 404 for no
        session; 409 ``no_place`` when a place it names cannot serve the start.

    """
    session = await _session(request, session_id)
    await _require_writer(request, identity, session=session)
    relay = get_session_relay(request)
    current = relay.process(session_id)
    if current is not None and current.state != "detached":
        return current
    place = await _start_place(request, identity, spec=body)
    process_id = uuid4()
    async with request_engine(request).acquire() as conn:
        _, token, _, _ = await create_api_key(
            conn,
            user_id=identity.user_id,
            name=process_key_name(process_id, session_id=session_id),
            # Never admin: an agent manages no account, whoever started it.
            ceiling=effective_role(identity.role, "writer"),
            minter=identity,
            host_id=place.host_id,
        )
    process = relay.start(
        session_id,
        body,
        process_id=process_id,
        host_id=place.host_id,
        host=place.host,
        token=token,
        remote=place.remote,
    )
    if process.process_id != process_id:
        # Another start won while this one minted: its key would serve nothing.
        await _revoke(get_store(request), process_id=process_id)
        return process
    _ = get_later(request).run(
        OFFLINE_AFTER_SEC,
        partial(
            _give_up,
            request,
            session_id=session_id,
            process_id=process_id,
            taken=False,
        ),
    )
    return process


@router.get(SESSION_PROCESS_PATH, response_model=AgentProcess | None)
async def read_process_route(
    session_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> AgentProcess | None:
    """Return the agent process serving a session, or the last that did.

    Args:
      session_id: The session.
      request: Request carrying the relay.
      identity: A viewer who may read the session.

    Returns:
      process: The process; ``None`` for a session that has had none.

    Raises:
      HTTPException: 403 for a caller who may not read the session; 404 for no
        session.

    """
    session = await _session(request, session_id)
    await require_session_access(request, identity, session=session, mode="read")
    return get_session_relay(request).process(session_id)


@router.delete(SESSION_PROCESS_PATH, response_model=AgentProcess | None)
async def stop_process_route(
    session_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> AgentProcess | None:
    """Stop a session's agent process; its key is revoked a minute later.

    The minute lets a stopped process upload its last records.

    Args:
      session_id: The session.
      request: Request carrying the relay.
      identity: A writer of the session.

    Returns:
      process: The process, ``detached``; ``None`` for a session that has had none.

    Raises:
      HTTPException: 403 for a caller who may not write the session; 404 for no
        session.

    """
    session = await _session(request, session_id)
    await _require_writer(request, identity, session=session)
    return await stop_process(request, session_id)


async def stop_process(request: Request, session_id: UUID) -> AgentProcess | None:
    """Stop a session's agent process, and revoke its key a minute later.

    The one way a process is stopped: its session's route, and its host owner's, both
    stop it here, once the caller is let through.

    Args:
      request: Request carrying the relay, the database engine and the delayed work.
      session_id: The session.

    Returns:
      process: The process, ``detached``; ``None`` for a session that has had none.

    """
    process = await get_session_relay(request).stop(session_id)
    if process is not None:
        _ = get_later(request).run(
            _REVOKE_AFTER_SEC,
            partial(_revoke, get_store(request), process_id=process.process_id),
        )
    return process


@dataclass(slots=True, kw_only=True)
class ProcessKeySweep:
    """The session relay's timed sweep, then the revoke of process keys no process holds.

    A stop's revoke and a start's give-up wait in this server's memory (``Later``), so
    a restart inside their window leaves a key live with no process, and so does a
    process that ends while no daemon reports it. Each sweep finds every live key a
    start minted (``auth.PROCESS_KEY_SQL``); one whose process the relay holds
    neither ``starting`` nor ``attached`` for ``unheld_sec``, counted from the first
    sweep that found it so, is revoked. After a restart the relay holds nothing until
    each process attaches again, which a running one does at once. Last, the relay
    forgets each session it holds nothing live of whose process's key is dead too.

    Args:
      relay: The session relay, swept first.
      store: Where the keys are, and the bearer cache a revoke clears.
      clock: Seconds on a monotonic clock.
      unheld_sec: How long a key may go unheld.

    """

    relay: SessionRelay
    store: Store
    clock: Callable[[], float] = time.monotonic
    unheld_sec: float = _UNHELD_SEC
    _unheld: dict[UUID, float] = field(default_factory=dict[UUID, float])

    async def sweep(self) -> None:
        """Sweep the relay, revoke each process key unheld for too long, then forget idle sessions."""
        await self.relay.sweep()
        async with self.store.engine.acquire() as conn:
            live = await live_process_keys(conn)
        now = self.clock()
        # Asked after each wait, never before one: a process attaches meanwhile.
        serving = self.relay.live_process_ids()
        self._unheld = {
            process_id: self._unheld.get(process_id, now)
            for process_id in live
            if process_id not in serving
        }
        revoked: set[UUID] = set()
        for process_id, since in list(self._unheld.items()):
            if now - since < self.unheld_sec:
                continue
            if process_id not in self.relay.live_process_ids():
                await _revoke(self.store, process_id=process_id)
                revoked.add(process_id)
            del self._unheld[process_id]
        # A key read live and minted since is a ``starting`` process's, which no
        # forgetting reaches; one revoked since it was read is forgotten next sweep.
        self.relay.forget_idle(keyed=frozenset(live) - revoked)


@router.websocket(SESSION_PROCESS_WS_PATH)
async def process_socket(websocket: WebSocket, session_id: UUID) -> None:
    """Serve an agent process's socket: its lines up, its inputs down, until it closes.

    The first frame must be a ``Hello``, within 10 seconds. The key must be one of a
    writer of the session, with the writer role. While the socket is open the session
    counts as alive, and a message that key posts may name the session as its sender's.

    Args:
      websocket: The process's socket; it carries its key as a bearer.
      session_id: The session it serves.

    """
    await websocket.accept()
    socket = _ProcessWebSocket(websocket)
    if auth_is_disabled(websocket):
        await socket.close(CLOSE_REFUSED, "no agent process serves here")
        return
    serves = partial(_server, websocket, session_id)
    try:
        identity, session = await serves()
    except HTTPException:
        await socket.close(CLOSE_REFUSED, "this key may not serve it")
        return
    sleep = get_later(websocket).sleep
    hello = await _hello(socket, sleep=sleep)
    if hello is None:
        await socket.close(BAD_FRAME, "the first frame is a Hello")
        return
    try:
        host_id = await _process_host_id(websocket, identity, hello=hello)
    except HTTPException:
        await socket.close(CLOSE_REFUSED, "another key serves this process")
        return
    relay = get_session_relay(websocket)
    current = relay.process(session_id)
    # Where the process runs once the relay takes it: a known process keeps its host.
    place = RunPlace(
        host=current.host
        if current is not None and current.process_id == hello.process_id
        else hello.host,
        cli="sagent",
    )
    await relay.serve_process(
        session_id,
        socket,
        hello,
        host_id=host_id,
        allowed=partial(_answers, serves),
        key=identity.api_key_id,
        attached=partial(
            _seen_while_open,
            get_store(websocket),
            session,
            place=place,
            actor=identity.email,
            sleep=sleep,
        ),
    )


@router.get(HOST_SESSIONS_WAIT_PATH, include_in_schema=False)
async def sessions_wait_route(
    host_id: UUID,
    request: Request,
    instance: UUID,
    wait_sec: Annotated[float, Query(ge=0)] = WAIT_MAX_SEC,
) -> ServeSession | None:
    """Hold until a session asks to start on this host, then return it; else ``null``.

    The host's own credential authenticates, as for a heartbeat. A start is handed out
    once, and never to a caller that has gone. One whose answer is lost all the same,
    or whose daemon dies before running it, is given up on 30 seconds after it was
    handed out unless its process attached or its daemon reported it: it is detached
    as ``not started`` and its key revoked.

    Args:
      host_id: The host.
      request: Request carrying the database engine and the relay.
      instance: The daemon instance, which must be the host's current one.
      wait_sec: How long to hold, clamped to 25 seconds.

    Returns:
      serve: The start to run, or ``None`` when none came in time.

    Raises:
      HTTPException: 403 on a server with authentication off, or when another daemon
        took the host over; 410 when the host is revoked, including while its daemon
        waits.

    """
    if auth_is_disabled(request):
        raise _forbidden()
    await current_host(request, host_id)
    await require_instance(request, host_id, instance=instance)
    try:
        serve = await get_session_relay(request).wait(
            host_id,
            wait_sec=min(wait_sec, WAIT_MAX_SEC),
            dropped=request.is_disconnected,
        )
    except HostRevokedError as error:
        raise host_revoked() from error
    if serve is not None:
        _ = get_later(request).run(
            _STARTED_SEC,
            partial(
                _give_up,
                request,
                session_id=serve.session_id,
                process_id=serve.process_id,
                taken=True,
            ),
        )
    return serve


@router.post(HOST_SESSIONS_ENDED_PATH, status_code=204, include_in_schema=False)
async def sessions_ended_route(
    host_id: UUID,
    body: ProcessEnded,
    request: Request,
) -> Response:
    """Take a daemon's report that a process it started has ended, and revoke its key.

    It carries no instance: a daemon that restarted still reports a child it started.

    Args:
      host_id: The host.
      body: The process and why it ended.
      request: Request carrying the database engine and the relay.

    Returns:
      empty: 204 with no body.

    Raises:
      HTTPException: 403 on a server with authentication off; 401 or 410 for a
        credential that is unknown or revoked.

    """
    if auth_is_disabled(request):
        raise _forbidden()
    await current_host(request, host_id)
    await _revoke(get_store(request), process_id=body.process_id, host_id=host_id)
    get_session_relay(request).ended(body, host_id=host_id)
    return Response(status_code=204)


# One read of ``frames`` stays pending across pings: a timeout that cancelled it would
# end the viewer's stream.
async def _sse_frames(
    frames: AsyncGenerator[ViewerFrame],
    *,
    ping_sec: float = LINES_PING_SEC,
) -> AsyncGenerator[bytes]:
    """Write each frame as a server-sent ``data:`` event, and a ``ping`` in a silence."""
    pending = asyncio.ensure_future(anext(frames))
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=ping_sec)
            if not done:
                yield SSE_PING
                continue
            try:
                frame = pending.result()
            except StopAsyncIteration:
                return
            yield f"data: {frame.model_dump_json()}\n\n".encode()
            pending = asyncio.ensure_future(anext(frames))
    finally:
        _ = pending.cancel()
        _ = await asyncio.wait({pending})
        await frames.aclose()


class _ProcessWebSocket:
    """A Starlette WebSocket, as the session relay reads, writes and closes it."""

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket

    async def receive(self) -> str | bytes | int:
        """Read the socket's next message.

        Returns:
          received: A text frame, a binary frame, or the code the peer closed with.

        """
        try:
            message = await self._websocket.receive()
        except (WebSocketDisconnect, RuntimeError):
            return _GONE
        if message["type"] == "websocket.disconnect":
            code = cast(object, message.get("code"))
            return code if isinstance(code, int) else _GONE
        text = cast(object, message.get("text"))
        if isinstance(text, str):
            return text
        data = cast(object, message.get("bytes"))
        return data if isinstance(data, bytes) else b""

    async def send_text(self, text: str) -> None:
        """Send one text frame; raise if the peer has gone."""
        await self._websocket.send_text(text)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """Close with ``code`` and ``reason``; a socket already closed is left alone."""
        # Uvicorn raises WebSocketDisconnect from a close on a peer that has dropped.
        try:
            await self._websocket.close(code, reason)
        except (WebSocketDisconnect, RuntimeError, OSError):
            return


_GONE: Final = 1006
"""The code of a socket whose peer left without a close of its own."""


@dataclass(frozen=True, slots=True, kw_only=True)
class _Place:
    """A host a start may run on, or use the login of."""

    host_id: UUID
    host: str
    remote: bool
    """Whether it is a remote agent service, which serves accounts other than its
    own, by the configuration."""


async def _session(connection: HTTPConnection, session_id: UUID) -> AgentSession:
    """Return the session a route names, refusing a server with authentication off."""
    if auth_is_disabled(connection):
        raise _forbidden()
    return await require_session(get_store(connection), session_id)


async def _require_reader(
    request: Request,
    identity: AuthIdentity,
    *,
    session_id: UUID,
) -> None:
    """Refuse a caller who may not read the session's lines."""
    session = await _session(request, session_id)
    await require_session_access(request, identity, session=session, mode="read")


# Asked by each sweep of a stream long open: the lines route's whole rule, its role and
# then its access check, of the caller as its credential authenticates then.
async def _reader(request: Request, session_id: UUID) -> None:
    """Refuse the caller, authenticated again, unless it may still read the session."""
    identity = await require_role("viewer")(await auth.request_user(request))
    await _require_reader(request, identity, session_id=session_id)


# The process socket's whole rule, asked as it opens and again by each sweep. The key's
# role must be the floor ``PUT``, ``DELETE`` and ``POST .../inputs`` ask: the process
# takes every person's inputs and writes the lines every viewer reads.
async def _server(
    websocket: WebSocket,
    session_id: UUID,
) -> tuple[AuthIdentity, AgentSession]:
    """Return the caller and the session when its key may serve it; else refuse."""
    identity = await require_role("writer")(await auth.request_user(websocket))
    # An agent process always holds a key. A handshake asks no ``Origin`` and no write
    # rule (``_origin``), so a cookie here would let another site's page, in a
    # signed-in writer's browser, take the session's inputs and write its lines.
    if identity.api_key_id is None:
        raise HTTPException(status_code=403, detail="an agent process serves by key")
    session = await _session(websocket, session_id)
    await _require_writer(websocket, identity, session=session)
    return identity, session


async def _answers(rule: Callable[[], Awaitable[object]]) -> bool:
    """Say whether a route's rule, asked again, still lets its caller through."""
    try:
        _ = await rule()
    except HTTPException:
        return False
    return True


# The one check of who may write a session: send it input, and start or stop its
# agent. Part 9 changes who a writer is here, and nowhere else. Mode ``input`` reads
# nothing of the request but its app, which a socket has too.
async def _require_writer(
    connection: HTTPConnection,
    identity: AuthIdentity,
    *,
    session: AgentSession,
) -> None:
    """Refuse a caller who may not write the session."""
    await require_session_access(
        cast("Request", connection),
        identity,
        session=session,
        mode="input",
    )


async def _start_place(
    request: Request,
    identity: AuthIdentity,
    *,
    spec: ProcessSpec,
) -> _Place:
    """Find the host a start runs on, and check its login's host; refuse what cannot."""
    name = spec.host or get_default_remote_agent_service(request)
    if not name:
        raise _no_place("this server names no host to run agents on; name one")
    services = get_remote_agent_services(request)
    async with request_engine(request).acquire() as conn:
        # The default the server names is a remote agent service; a host named is
        # the caller's own first. A service must also say it confines each process.
        runs = await _place(
            conn,
            identity,
            name=name,
            own=bool(spec.host),
            facts={SESSIONS_FACT: True},
            service_facts={SESSIONS_FACT: True, REMOTE_AGENTS_FACT: True},
            services=services,
        )
        signs_in = not spec.login_host or await _place(
            conn,
            identity,
            name=spec.login_host,
            own=True,
            facts={LOGINS_FACT: True},
            service_facts={LOGINS_FACT: True},
            services=services,
        )
    if runs is None:
        raise _no_place(f"no host {name} runs agents for you")
    if runs.remote and spec.args:
        raise _no_place(f"{name} runs agents for others, and takes no sagent flags")
    if not signs_in:
        raise _no_place(f"no host {spec.login_host} answers model calls for you")
    return runs


# A remote agent service is one the configuration names, admits the caller, and
# reports ``service_facts``.
async def _place(
    conn: Conn,
    identity: AuthIdentity,
    *,
    name: str,
    own: bool,
    facts: Mapping[str, bool],
    service_facts: Mapping[str, bool],
    services: tuple[RemoteAgentService, ...],
) -> _Place | None:
    """Find the host ``name``: the caller's own reporting ``facts``, else a service."""
    if own and (
        found := await _host_id(conn, name=name, owner=identity.email, facts=facts)
    ):
        return _Place(host_id=found, host=name, remote=False)
    service = next((each for each in services if each.name == name), None)
    # Its admits are patterns of the kind staff holds, matched as staff is.
    if service is None or STAFF not in person_audiences(
        identity.email,
        hosted_domain=identity.hosted_domain,
        staff=service.admits,
    ):
        return None
    found = await _host_id(conn, name=name, owner=service.account, facts=service_facts)
    return None if found is None else _Place(host_id=found, host=name, remote=True)


# A place as the tools' rule asks of a host (``tool_access``): its credential is live,
# it is confirmed and its owner is an active writer or admin. Its daemon need not be
# attached: the start waits for it.
_PLACE: Final = (
    "SELECT m.id FROM hosts m JOIN users u ON u.email = m.owner "
    "WHERE m.name = $1 AND m.owner = $2 AND m.facts @> $3 "
    "AND m.confirmed_at IS NOT NULL "
    "AND u.status = 'active' AND u.role IN ('writer', 'admin') "
    "AND EXISTS (SELECT 1 FROM host_credentials c "
    "WHERE c.host_id = m.id AND c.revoked_at IS NULL)"
)


async def _host_id(
    conn: Conn,
    *,
    name: str,
    owner: str,
    facts: Mapping[str, bool],
) -> UUID | None:
    """Return the id of ``owner``'s host ``name`` that is a place reporting ``facts``."""
    return from_plain(
        await conn.fetchval(_PLACE, name, owner, dict(facts)),
        UUID,
        default=None,
    )


# A process a key was minted for is served under that key alone, and that key serves no
# other, whatever ``Hello`` a socket sends: under a person's key it would be that
# caller's process, whose stop then revokes the minted key, another account's among
# them; and a process key is no person's, so a process it names that none was minted
# for is not its to serve. A minted process is known by the host its key is bound to.
# One no key was minted for is a process a person started with a key of their own,
# known by the caller's host its ``Hello`` names.
async def _process_host_id(
    connection: HTTPConnection,
    identity: AuthIdentity,
    *,
    hello: Hello,
) -> UUID | None:
    """Return the host a process is known by; refuse a key that does not serve it."""
    process = identity.process
    if process is not None:
        if hello.process_id != process.process_id:
            raise HTTPException(status_code=403)
        return process.host_id
    async with request_engine(connection).acquire() as conn:
        minted = await process_key_minted(conn, process_id=hello.process_id)
    if minted:
        raise HTTPException(status_code=403)
    return await _own_host_id(connection, owner=identity.email, name=hello.host)


async def _own_host_id(
    connection: HTTPConnection,
    *,
    owner: str,
    name: str,
) -> UUID | None:
    """Return the id of ``owner``'s enrolled host ``name``; ``None`` for none."""
    async with request_engine(connection).acquire() as conn:
        return from_plain(
            await conn.fetchval(
                "SELECT id FROM hosts WHERE name = $1 AND owner = $2",
                name,
                owner,
            ),
            UUID,
            default=None,
        )


async def _hello(socket: _ProcessWebSocket, *, sleep: Sleep) -> Hello | None:
    """Return the socket's first frame if it is a ``Hello`` sent in time; else ``None``."""
    reading = asyncio.ensure_future(socket.receive())
    timer = asyncio.ensure_future(sleep(_HELLO_SEC))
    try:
        _ = await asyncio.wait({reading, timer}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        _ = timer.cancel()
        _ = reading.cancel()
    if not reading.done() or reading.cancelled():
        return None
    frame = process_frame(reading.result())
    return frame if isinstance(frame, Hello) else None


# Run by the relay once it has taken the socket, so a socket it refuses neither moves
# the session's host nor reopens it. Whoever ended the session -- the reaper for its
# silence, its own run as it quit, an upload, a person's stop -- a process that attaches
# serves it from now on: left ended, each record it saves would be refused as a write
# to an ended session for as long as it ran. So an ended session and a reaped one are
# one case here, as an attach of ``POST /api/sessions/start`` made them. An upload
# alone reopens a reaped one only (``session_ir_routes``): a stopped process's last
# upload must not undo the stop.
async def _seen_while_open(
    store: Store,
    session: AgentSession,
    *,
    place: RunPlace,
    actor: str,
    sleep: Sleep,
) -> None:
    """Start the reaper's watch, reopening an ended session; then say it lives each minute."""
    await store.record_session_seen(
        session.id,
        at=datetime.now(UTC),
        polled=True,
        place=place,
    )
    if session.ended is not None:
        async with notify_after_commit(), store.engine.acquire() as conn, tx(conn):
            await store.reopen_session(session.id, conn=conn, actor=actor)
    while True:
        await sleep(_SEEN_EVERY_SEC)
        await store.record_session_seen(
            session.id,
            at=datetime.now(UTC),
            polled=True,
            place=place,
        )


async def _give_up(
    request: Request,
    *,
    session_id: UUID,
    process_id: UUID,
    taken: bool,
) -> None:
    """Detach a start no daemon took, or no process answered, in time; revoke its key."""
    relay = get_session_relay(request)
    if relay.give_up(session_id, process_id=process_id, taken=taken):
        await _revoke(get_store(request), process_id=process_id)


async def _revoke(
    store: Store,
    *,
    process_id: UUID,
    host_id: UUID | None = None,
) -> None:
    """Revoke the key minted for a process, if any, and stop honouring it at once."""
    async with store.engine.acquire() as conn:
        revoked = await revoke_process_key(conn, process_id=process_id, host_id=host_id)
    for key_id in revoked:
        store.forget_bearer_identity_for_key(key_id)


def _no_place(message: str) -> HTTPException:
    """Return the refusal of a start that a place it names cannot serve."""
    return refusal(409, code=NO_PLACE, message=message)


def _forbidden() -> HTTPException:
    """Return the one refusal of every route here on a server with authentication off."""
    return refusal(
        403,
        code="sessions_forbidden",
        message="live sessions are not available here",
    )
