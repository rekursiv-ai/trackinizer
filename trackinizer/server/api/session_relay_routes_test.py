"""The live session's routes: what each takes, and the pieces they are built of.

Who may do what, and a process's life, are ``session_relay_routes_pglite_test``'s,
against a real database.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, Final, TypedDict, cast, override
from unittest.mock import ANY, AsyncMock
from uuid import UUID

import asyncio

from fastapi import FastAPI, HTTPException
from starlette.websockets import WebSocketDisconnect
from treekle import from_plain

import pytest

from trackinizer.conftest import ExchangeEngine, Says, executed_sql, make_store
from trackinizer.server import auth
from trackinizer.server.api import session_relay_routes
from trackinizer.server.api._deps import Later
from trackinizer.server.api.app import ROUTERS, app
from trackinizer.server.api.conftest import live_session, patch_state
from trackinizer.server.auth import PROCESS_KEY_SQL, AuthIdentity, ProcessKey
from trackinizer.server.config import Config, RemoteAgentService
from trackinizer.server.notify import SSE_PING
from trackinizer.server.session_relay import BAD_FRAME, SessionRelay
from trackinizer.server.store.session import RunPlace
from trackinizer.server.values import vetted_sql
from trackinizer.wire.audience import SESSION_CONTENT_ROUTES
from trackinizer.wire.wire_session_relay import (
    HOST_SESSIONS_ENDED_PATH,
    HOST_SESSIONS_WAIT_PATH,
    SESSION_INPUTS_PATH,
    SESSION_LINES_PATH,
    SESSION_PROCESS_PATH,
    AgentState,
    Hello,
    Opened,
    ProcessSpec,
    StateLine,
)
from trackinizer.wire.wire_terminals import CLOSE_REFUSED


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from fastapi import Request, WebSocket
    from fastapi.testclient import TestClient
    from starlette.requests import HTTPConnection
    from starlette.types import Message

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.auth import Role
    from trackinizer.server.store.core import Store
    from trackinizer.wire.wire_session_relay import ViewerFrame


class _Components(TypedDict):
    schemas: dict[str, object]


class _Schema(TypedDict):
    paths: dict[str, object]
    components: _Components


_SESSION = UUID(int=1)
_CALLERS: Final = UUID(int=10)
_ROUTES: Final[tuple[tuple[str, str], ...]] = (
    ("GET", SESSION_LINES_PATH),
    ("POST", SESSION_INPUTS_PATH),
    ("PUT", SESSION_PROCESS_PATH),
    ("GET", SESSION_PROCESS_PATH),
    ("DELETE", SESSION_PROCESS_PATH),
)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", SESSION_INPUTS_PATH, {"kind": "Quit", "payload": {}}),
        ("POST", SESSION_INPUTS_PATH, {"kind": "Command", "payload": {"line": "hi"}}),
        ("PUT", SESSION_PROCESS_PATH, {"host": "lab-box", "cwd": "relative"}),
        ("PUT", SESSION_PROCESS_PATH, {"provider": "rai"}),
    ],
    ids=[
        "an input no person may send",
        "a command that is no slash line",
        "a folder that is not absolute",
        "a field this wire does not have",
    ],
)
def test_a_body_the_wire_refuses_is_refused_before_the_route_runs(
    route_client: tuple[TestClient, Store, FakeEngine],
    method: str,
    path: str,
    body: dict[str, object],
) -> None:
    client, _, _ = route_client

    response = client.request(method, path.format(session_id=_SESSION), json=body)

    assert response.status_code == 422


def test_the_app_serves_the_routes_and_registers_them_as_session_content() -> None:
    assert session_relay_routes.router in ROUTERS
    assert set(_ROUTES) <= SESSION_CONTENT_ROUTES


def test_the_schema_names_every_frame_a_viewer_reads_and_no_daemon_route() -> None:
    # A fresh app: the module-level one is shared, and caches the schema it builds.
    served = FastAPI()
    served.include_router(session_relay_routes.router)

    schema = from_plain(served.openapi(), _Schema)

    assert {
        "Opened",
        "EventLine",
        "StateLine",
        "SettingsLine",
        "Setting",
        "RecordLine",
        "ProcessLine",
        "AgentState",
        "AgentProcess",
        "NewInput",
        "InputSent",
        "ProcessSpec",
    } <= set(schema["components"]["schemas"])
    assert not {HOST_SESSIONS_WAIT_PATH, HOST_SESSIONS_ENDED_PATH} & set(
        schema["paths"],
    )


@pytest.mark.asyncio
async def test_each_frame_is_one_data_event_and_a_silence_a_ping() -> None:
    go = asyncio.Event()
    closed: list[bool] = []

    async def frames() -> AsyncGenerator[ViewerFrame]:
        try:
            yield Opened()
            _ = await go.wait()
            yield Opened(settings=())
        finally:
            closed.append(True)

    events = session_relay_routes._sse_frames(frames(), ping_sec=0.001)

    assert await anext(events) == f"data: {Opened().model_dump_json()}\n\n".encode()
    assert await anext(events) == SSE_PING
    go.set()
    rest = [event async for event in events if event != SSE_PING]

    assert rest == [f"data: {Opened().model_dump_json()}\n\n".encode()]
    assert closed == [True]


@pytest.mark.asyncio
async def test_a_stream_closed_by_its_reader_closes_the_viewers_frames() -> None:
    closed: list[bool] = []

    async def frames() -> AsyncGenerator[ViewerFrame]:
        try:
            yield Opened()
            _ = await asyncio.Event().wait()
        finally:
            closed.append(True)

    events = session_relay_routes._sse_frames(frames(), ping_sec=0.001)
    _ = await anext(events)
    # Past the ping, a read of the frames is under way: closing must end it first.
    assert await anext(events) == SSE_PING

    async with asyncio.timeout(1):
        await events.aclose()

    assert closed == [True]


class _Starlette:
    """The few methods of a Starlette WebSocket the socket's adapter calls."""

    def __init__(self, *received: Message | BaseException) -> None:
        self.received = list(received)
        self.closed: list[tuple[int, str]] = []
        self.sent: list[str] = []
        self.close_raises: BaseException | None = None
        self.reads_dropped = 0
        """How many reads of a silent socket were cancelled."""

    async def receive(self) -> Message:
        if not self.received:
            try:
                _ = await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.reads_dropped += 1
                raise
        each = self.received.pop(0)
        if isinstance(each, BaseException):
            raise each
        return each

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.close_raises is not None:
            raise self.close_raises
        self.closed.append((code, reason))


def _socket(fake: _Starlette) -> session_relay_routes._ProcessWebSocket:
    return session_relay_routes._ProcessWebSocket(cast("WebSocket", fake))


class _Dialed(_Starlette):
    """A process's socket as its route takes it: on the app, waiting to be accepted."""

    def __init__(self, *received: Message | BaseException) -> None:
        super().__init__(*received)
        self.app = app
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True


def _caller(
    monkeypatch: pytest.MonkeyPatch,
    connection: object,
    *,
    role: Role,
    key: UUID,
) -> None:
    """Have ``connection``'s credential, and no other, authenticate as ada's ``key``."""
    caller = AuthIdentity(
        user_id=UUID(int=9),
        api_key_id=key,
        email="ada@x.y",
        role=role,
    )

    async def authenticated(asked: object) -> AuthIdentity:
        if asked is not connection:
            raise HTTPException(status_code=401)
        return caller

    monkeypatch.setattr(auth, "request_user", authenticated)


def _opened_by(monkeypatch: pytest.MonkeyPatch, store: Store, *, key: UUID) -> None:
    """Have the store hold ``_SESSION`` alone, opened by ``key``, which so may write it."""
    held = {_SESSION: live_session(opened_by_api_key_id=key)}
    monkeypatch.setattr(store, "get_inquiry", AsyncMock(side_effect=held.get))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "key", "code"),
    [
        ("viewer", _CALLERS, CLOSE_REFUSED),
        ("writer", UUID(int=11), CLOSE_REFUSED),
        ("writer", _CALLERS, BAD_FRAME),
    ],
    ids=[
        "refused below the writer role",
        "refused to a key that may not write the session",
        "a writer's key of the writer role goes on to its Hello",
    ],
)
async def test_a_socket_serves_a_writers_key_of_the_writer_role_alone(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
    role: Role,
    key: UUID,
    code: int,
) -> None:
    """Below it a key would take every input and write the lines every viewer reads."""
    _, store, _ = route_client
    # A first frame that is no Hello, which only a caller the rule lets through reads.
    dialed = _Dialed({"type": "websocket.receive", "text": "{}"})
    _caller(monkeypatch, dialed, role=role, key=key)
    _opened_by(monkeypatch, store, key=_CALLERS)
    patch_state(monkeypatch, "later", Later())

    await session_relay_routes.process_socket(cast("WebSocket", dialed), _SESSION)

    assert dialed.accepted
    assert [closed for closed, _ in dialed.closed] == [code]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "shown", "reads"),
    [(_CALLERS, None, True), (UUID(int=11), True, True), (UUID(int=11), None, False)],
    ids=[
        "the key that opened it",
        "one the transcript rule shows it to",
        "one it no longer shows it to",
    ],
)
async def test_a_stream_is_asked_again_of_its_caller_as_it_authenticates_now(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
    key: UUID,
    shown: bool | None,
    reads: bool,
) -> None:
    """The lowest role reads, where the session's opener or the database says so."""
    _, store, engine = route_client
    request = cast("Request", SimpleNamespace(app=app))
    _caller(monkeypatch, request, role="viewer", key=key)
    _opened_by(monkeypatch, store, key=_CALLERS)
    engine.conn.fetchval.return_value = shown

    assert (
        await session_relay_routes._answers(
            partial(session_relay_routes._reader, request, _SESSION),
        )
        is reads
    )


@pytest.mark.asyncio
async def test_a_rule_asked_again_says_no_for_a_refusal_alone() -> None:
    async def refused() -> None:
        raise HTTPException(status_code=401)

    async def unknown() -> None:
        raise ConnectionError

    assert await session_relay_routes._answers(AsyncMock(return_value=None)) is True
    assert await session_relay_routes._answers(refused) is False
    # Any other failure is the relay's to treat as an answer it does not have.
    with pytest.raises(ConnectionError):
        _ = await session_relay_routes._answers(unknown)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("received", "read"),
    [
        ({"type": "websocket.receive", "text": "{}"}, "{}"),
        ({"type": "websocket.receive", "bytes": b"{}"}, b"{}"),
        ({"type": "websocket.receive"}, b""),
        ({"type": "websocket.disconnect", "code": 1000}, 1000),
        ({"type": "websocket.disconnect"}, 1006),
        (WebSocketDisconnect(1001), 1006),
        (RuntimeError("closed"), 1006),
    ],
    ids=["text", "binary", "empty", "closed", "closed with no code", "dropped", "gone"],
)
async def test_the_socket_reads_a_text_frame_a_binary_one_or_how_it_closed(
    received: Message | BaseException,
    read: str | bytes | int,
) -> None:
    assert await _socket(_Starlette(received)).receive() == read


@pytest.mark.asyncio
async def test_the_socket_sends_text_and_closes_once_whatever_the_peer_did() -> None:
    fake = _Starlette()
    socket = _socket(fake)

    await socket.send_text("line")
    await socket.close(4000, "stopped")
    fake.close_raises = RuntimeError("closed")
    await socket.close()

    assert (fake.sent, fake.closed) == (["line"], [(4000, "stopped")])


@pytest.mark.asyncio
async def test_a_hello_is_read_whole_and_in_time() -> None:
    hello = Hello(process_id=UUID(int=2), version="1", host="lab")
    later = asyncio.Event()
    asked: list[float] = []
    timers_dropped: list[float] = []

    async def forever(seconds: float) -> None:
        asked.append(seconds)
        try:
            _ = await later.wait()
        except asyncio.CancelledError:
            timers_dropped.append(seconds)
            raise

    async def at_once(seconds: float) -> None:
        del seconds

    def first(text: str) -> session_relay_routes._ProcessWebSocket:
        return _socket(_Starlette({"type": "websocket.receive", "text": text}))

    assert (
        await session_relay_routes._hello(first(hello.model_dump_json()), sleep=forever)
        == hello
    )
    # One turn of the loop, for the timer it no longer needs to take its cancel.
    await asyncio.sleep(0)
    assert (asked, timers_dropped) == ([10.0], [10.0])
    assert await session_relay_routes._hello(first("{}"), sleep=forever) is None
    # A frame the wire allows a process, but not first.
    state = StateLine(
        state=AgentState(line="", busy=False, since=datetime(2026, 11, 10, tzinfo=UTC)),
    )
    assert (
        await session_relay_routes._hello(first(state.model_dump_json()), sleep=forever)
        is None
    )
    silent = _Starlette()
    assert await session_relay_routes._hello(_socket(silent), sleep=at_once) is None
    # Likewise for the read of a socket that said nothing in time.
    await asyncio.sleep(0)
    assert silent.reads_dropped == 1


@pytest.mark.parametrize("ended", [False, True], ids=["live", "ended"])
@pytest.mark.asyncio
async def test_the_seen_loop_tells_the_reaper_each_minute_until_cancelled(
    monkeypatch: pytest.MonkeyPatch,
    ended: bool,
) -> None:
    """Seen as it attaches, reopened if it ended, then seen after each minute."""
    store, engine = make_store()
    seen, reopened = AsyncMock(), AsyncMock()
    monkeypatch.setattr(store, "record_session_seen", seen)
    monkeypatch.setattr(store, "reopen_session", reopened)
    ticks: list[float] = []
    stop = asyncio.Event()

    async def sleep(seconds: float) -> None:
        ticks.append(seconds)
        if len(ticks) > 2:
            _ = await stop.wait()

    session = live_session()
    if ended:
        session = replace(session, ended=datetime.now(UTC))
    place = RunPlace(host="lab", cli="sagent")
    before = datetime.now(UTC)
    loop = asyncio.ensure_future(
        session_relay_routes._seen_while_open(
            store,
            session,
            place=place,
            actor="ada@x.y",
            sleep=sleep,
        ),
    )
    await asyncio.sleep(0)
    _ = loop.cancel()
    _ = await asyncio.gather(loop, return_exceptions=True)

    assert ticks == [60.0] * 3
    # Once as it attaches, then after each minute that passed.
    assert [call.args for call in seen.await_args_list] == [(session.id,)] * 3
    assert all(
        (call.kwargs["polled"], call.kwargs["place"]) == (True, place)
        and cast("datetime", call.kwargs["at"]) >= before
        for call in seen.await_args_list
    )
    if not ended:
        reopened.assert_not_awaited()
        return
    reopened.assert_awaited_once_with(session.id, conn=engine.conn, actor="ada@x.y")
    assert executed_sql(engine.conn) == ["BEGIN", "COMMIT"]


@pytest.mark.asyncio
async def test_a_socket_hands_the_relay_the_key_it_was_opened_with(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A message that key posts may then name the session as its sender's."""
    _, store, _ = route_client
    hello = Hello(process_id=UUID(int=2), version="1", host="lab")
    dialed = _Dialed({"type": "websocket.receive", "text": hello.model_dump_json()})
    _caller(monkeypatch, dialed, role="writer", key=_CALLERS)
    _opened_by(monkeypatch, store, key=_CALLERS)
    monkeypatch.setattr(store, "record_session_seen", AsyncMock())
    monkeypatch.setattr(
        session_relay_routes,
        "_process_host_id",
        AsyncMock(return_value=None),
    )
    relay = SessionRelay()
    patch_state(monkeypatch, "session_relay", relay)
    served = AsyncMock()
    monkeypatch.setattr(relay, "serve_process", served)
    patch_state(monkeypatch, "later", Later())

    await session_relay_routes.process_socket(cast("WebSocket", dialed), _SESSION)

    assert served.await_args is not None
    assert served.await_args.kwargs["key"] == _CALLERS


_MINTED: Final = vetted_sql(
    "SELECT 1 FROM api_keys AS credential WHERE starts_with(credential.name, $1) AND ",
    PROCESS_KEY_SQL,
)
_OWN_HOST: Final = "SELECT id FROM hosts WHERE name = $1 AND owner = $2"
_HELLO: Final = Hello(process_id=UUID(int=2), version="1", host="laptop")
_KEYS_HOST: Final = UUID(int=12)


async def _process_host_id(
    monkeypatch: pytest.MonkeyPatch,
    *steps: Says,
    process: ProcessKey | None = None,
) -> UUID | None:
    """Return the host a writer's key knows ``_HELLO``'s process by, over ``steps``."""
    engine = ExchangeEngine(*steps)
    patch_state(monkeypatch, "engine", engine)
    caller = AuthIdentity(
        user_id=UUID(int=9),
        api_key_id=_CALLERS,
        email="ada@x.y",
        role="writer",
        process=process,
    )
    known_by = await session_relay_routes._process_host_id(
        cast("HTTPConnection", SimpleNamespace(app=app)),
        caller,
        hello=_HELLO,
    )
    assert engine.done
    return known_by


def _minted(*, minted: bool) -> Says:
    """Answer whether a start minted a key for ``_HELLO``'s process."""
    return Says(
        method="fetchval",
        sql=_MINTED,
        args=(f"agent process {_HELLO.process_id}",),
        answer=1 if minted else None,
    )


@pytest.mark.asyncio
async def test_a_process_key_serves_its_own_process_known_by_its_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not by its ``Hello``'s host: a remote agent's host is not its account's."""
    own = ProcessKey(
        process_id=_HELLO.process_id,
        session_id=_SESSION,
        host_id=_KEYS_HOST,
    )

    assert await _process_host_id(monkeypatch, process=own) == _KEYS_HOST


@pytest.mark.asyncio
async def test_a_process_key_serves_no_process_but_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process no key was minted for is a person's own, and a process key no person's."""
    other = ProcessKey(process_id=UUID(int=3), session_id=_SESSION, host_id=_KEYS_HOST)

    with pytest.raises(HTTPException) as refused:
        _ = await _process_host_id(monkeypatch, process=other)

    assert refused.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host_id",
    [UUID(int=13), None],
    ids=["an enrolled host of the caller's", "a computer not enrolled"],
)
async def test_a_process_no_key_was_minted_for_is_known_by_the_callers_host_named(
    monkeypatch: pytest.MonkeyPatch,
    host_id: UUID | None,
) -> None:
    own = Says(
        method="fetchval",
        sql=_OWN_HOST,
        args=("laptop", "ada@x.y"),
        answer=None if host_id is None else str(host_id),
    )

    assert await _process_host_id(monkeypatch, _minted(minted=False), own) == host_id


@pytest.mark.asyncio
async def test_a_process_a_key_was_minted_for_is_no_persons_to_serve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its stop would revoke the key minted for it, another account's among them."""
    with pytest.raises(HTTPException) as refused:
        _ = await _process_host_id(monkeypatch, _minted(minted=True))

    assert refused.value.status_code == 403


class _Holding(SessionRelay):
    """A relay that holds the processes a test names, and counts its sweeps."""

    def __init__(self) -> None:
        super().__init__()
        self.held: frozenset[UUID] = frozenset()
        self.swept = 0
        self.keyed: list[frozenset[UUID]] = []

    @override
    def live_process_ids(self) -> frozenset[UUID]:
        return self.held

    @override
    async def sweep(self) -> None:
        self.swept += 1

    @override
    def forget_idle(self, *, keyed: frozenset[UUID]) -> None:
        self.keyed.append(keyed)


@pytest.mark.asyncio
async def test_a_process_key_sweep_revokes_a_key_unheld_for_its_grace_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counted from the first sweep that found it unheld; held again, it starts over."""
    store, _ = make_store()
    unheld, held, comes_back = UUID(int=1), UUID(int=2), UUID(int=3)
    live = [unheld, held, comes_back]
    found = AsyncMock(return_value=live)
    monkeypatch.setattr(session_relay_routes, "live_process_keys", found)
    revoked: list[UUID] = []

    async def revoke(given: object, *, process_id: UUID) -> None:
        assert given is store
        revoked.append(process_id)

    monkeypatch.setattr(session_relay_routes, "_revoke", revoke)
    relay = _Holding()
    relay.held = frozenset({held})
    now = [0.0]
    sweep = session_relay_routes.ProcessKeySweep(
        relay=relay,
        store=store,
        clock=lambda: now[0],
        unheld_sec=10.0,
    )

    await sweep.sweep()
    now[0] = 5.0
    relay.held = frozenset({held, comes_back})
    await sweep.sweep()
    relay.held = frozenset({held})
    now[0] = 9.9
    await sweep.sweep()
    before = list(revoked)
    now[0] = 10.0
    await sweep.sweep()
    at_grace = list(revoked)
    live.remove(unheld)
    now[0] = 19.9
    await sweep.sweep()
    now[0] = 20.0
    await sweep.sweep()

    assert (before, at_grace) == ([], [unheld])
    # Unheld again from 9.9, so its grace ends at 19.9; revoked once.
    assert revoked == [unheld, comes_back]
    assert relay.swept == 6
    # The relay forgets each idle session whose process's key no longer lives: the
    # keys read live, less those the sweep revoked.
    assert relay.keyed == [
        frozenset({unheld, held, comes_back}),
        frozenset({unheld, held, comes_back}),
        frozenset({unheld, held, comes_back}),
        frozenset({held, comes_back}),
        frozenset({held, comes_back}),
        frozenset({held}),
    ]
    # Each sweep reads the keys on a connection of the store's.
    assert [call.args for call in found.await_args_list] == [(ANY,)] * 6
    assert None not in [call.args[0] for call in found.await_args_list]


@pytest.mark.asyncio
async def test_a_key_still_in_its_grace_holds_back_no_other_key_s_revoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _ = make_store()
    young, old = UUID(int=1), UUID(int=2)
    live = [old]
    monkeypatch.setattr(
        session_relay_routes,
        "live_process_keys",
        AsyncMock(return_value=live),
    )
    revoked: list[UUID] = []

    async def revoke(given: object, *, process_id: UUID) -> None:
        del given
        revoked.append(process_id)

    monkeypatch.setattr(session_relay_routes, "_revoke", revoke)
    now = [0.0]
    sweep = session_relay_routes.ProcessKeySweep(
        relay=_Holding(),
        store=store,
        clock=lambda: now[0],
        unheld_sec=10.0,
    )
    await sweep.sweep()
    # Listed first, unheld since 5: its grace runs on as the old one's ends.
    live.insert(0, young)
    now[0] = 5.0
    await sweep.sweep()
    now[0] = 10.0
    await sweep.sweep()

    assert revoked == [old]


@pytest.mark.asyncio
async def test_a_key_whose_process_attaches_as_the_sweep_reads_the_keys_stays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past its grace as the sweep began, held by the time it would be revoked."""
    store, _ = make_store()
    process = UUID(int=1)
    reads: list[None] = []
    reading, released = asyncio.Event(), asyncio.Event()

    # The second read waits on the test, as a database answering slowly would.
    async def found(conn: object) -> list[UUID]:
        del conn
        reads.append(None)
        if len(reads) > 1:
            reading.set()
            _ = await released.wait()
        return [process]

    monkeypatch.setattr(session_relay_routes, "live_process_keys", found)
    revoked: list[UUID] = []

    async def revoke(given: object, *, process_id: UUID) -> None:
        del given
        revoked.append(process_id)

    monkeypatch.setattr(session_relay_routes, "_revoke", revoke)
    relay = _Holding()
    now = [0.0]
    sweep = session_relay_routes.ProcessKeySweep(
        relay=relay,
        store=store,
        clock=lambda: now[0],
        unheld_sec=10.0,
    )
    await sweep.sweep()
    now[0] = 10.0

    sweeping = asyncio.ensure_future(sweep.sweep())
    _ = await asyncio.wait_for(reading.wait(), 1)
    relay.held = frozenset({process})
    released.set()
    await asyncio.wait_for(sweeping, 1)

    assert revoked == []


@pytest.mark.asyncio
async def test_a_key_whose_process_attaches_as_another_is_revoked_stays_and_starts_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each revoke waits on the database; a process may attach meanwhile."""
    store, _ = make_store()
    first, second = UUID(int=1), UUID(int=2)
    live = [first, second]
    monkeypatch.setattr(
        session_relay_routes,
        "live_process_keys",
        AsyncMock(return_value=live),
    )
    relay = _Holding()
    revoked: list[UUID] = []

    async def revoke(given: object, *, process_id: UUID) -> None:
        del given
        revoked.append(process_id)
        live.remove(process_id)
        relay.held = frozenset({second})

    monkeypatch.setattr(session_relay_routes, "_revoke", revoke)
    now = [0.0]
    sweep = session_relay_routes.ProcessKeySweep(
        relay=relay,
        store=store,
        clock=lambda: now[0],
        unheld_sec=10.0,
    )
    await sweep.sweep()
    now[0] = 10.0
    await sweep.sweep()
    # Its count starts over from the next sweep that finds it unheld again.
    relay.held = frozenset()
    now[0] = 19.9
    await sweep.sweep()
    after_a_grace_less_a_moment = list(revoked)
    now[0] = 29.9
    await sweep.sweep()

    assert after_a_grace_less_a_moment == [first]
    assert revoked == [first, second]


@pytest.mark.asyncio
async def test_a_revoked_process_key_stops_being_honoured_at_once(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verified-bearer cache would otherwise honour each for up to a minute more."""
    _, store, engine = route_client
    keys = [UUID(int=7), UUID(int=8)]
    engine.conn.fetch.return_value = [{"id": key} for key in keys]
    forgotten: list[UUID] = []
    monkeypatch.setattr(store, "forget_bearer_identity_for_key", forgotten.append)
    process, host = UUID(int=2), UUID(int=3)

    await session_relay_routes._revoke(
        store,
        process_id=process,
        host_id=host,
    )

    revoked = engine.conn.fetch.call_args
    assert revoked is not None
    assert revoked.args[1:] == (f"agent process {process}", host)
    assert forgotten == keys


_AGENTS: Final = "agents@service.invalid"
_SERVICE_HOST: Final = UUID(int=20)
_HERS: Final = UUID(int=21)
_RUNS: Final = {"sessions": True}
_REMOTE: Final = {"sessions": True, "remote_agents": True}
_LOGINS: Final = {"logins": True}


def _found(name: str, owner: str, facts: dict[str, bool], host: UUID | None) -> Says:
    """Answer the lookup of ``owner``'s host ``name`` reporting ``facts`` with ``host``."""
    return Says(
        method="fetchval",
        sql=session_relay_routes._PLACE,
        args=(name, owner, facts),
        answer=None if host is None else str(host),
    )


async def _placed(
    monkeypatch: pytest.MonkeyPatch,
    spec: ProcessSpec,
    *steps: Says,
    admits: tuple[str, ...] = ("ada@x.y",),
    default: str = "remote-agents",
    hosted_domain: str | None = None,
) -> session_relay_routes._Place:
    """Return where ada's start ``spec`` runs, over the database's ``steps``."""
    engine = ExchangeEngine(*steps)
    patch_state(monkeypatch, "engine", engine)
    patch_state(
        monkeypatch,
        "config",
        Config(
            remote_agent_services=(
                RemoteAgentService(
                    name="remote-agents",
                    account=_AGENTS,
                    admits=admits,
                ),
            ),
            default_remote_agent_service=default,
        ),
    )
    ada = AuthIdentity(
        user_id=UUID(int=9),
        api_key_id=_CALLERS,
        email="ada@x.y",
        role="writer",
        hosted_domain=hosted_domain,
    )
    try:
        return await session_relay_routes._start_place(
            cast("Request", SimpleNamespace(app=app)),
            ada,
            spec=spec,
        )
    finally:
        assert engine.done, engine.pending


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("spec", "steps", "remote"),
    [
        (
            ProcessSpec(),
            (_found("remote-agents", _AGENTS, _REMOTE, _SERVICE_HOST),),
            True,
        ),
        (ProcessSpec(host="lab"), (_found("lab", "ada@x.y", _RUNS, _HERS),), False),
        (
            ProcessSpec(host="remote-agents"),
            (
                _found("remote-agents", "ada@x.y", _RUNS, None),
                _found("remote-agents", _AGENTS, _REMOTE, _SERVICE_HOST),
            ),
            True,
        ),
        (
            ProcessSpec(host="lab", login_host="laptop"),
            (
                _found("lab", "ada@x.y", _RUNS, _HERS),
                _found("laptop", "ada@x.y", _LOGINS, UUID(int=22)),
            ),
            False,
        ),
    ],
    ids=["the default service", "a host of hers", "the service by name", "her login"],
)
async def test_a_start_runs_on_her_own_host_first_else_a_service_that_admits_her(
    monkeypatch: pytest.MonkeyPatch,
    *,
    spec: ProcessSpec,
    steps: tuple[Says, ...],
    remote: bool,
) -> None:
    placed = await _placed(monkeypatch, spec, *steps)

    assert placed == session_relay_routes._Place(
        host_id=_SERVICE_HOST if remote else _HERS,
        host=spec.host or "remote-agents",
        remote=remote,
    )


@pytest.mark.asyncio
async def test_a_service_may_answer_the_model_calls_of_an_agent_on_her_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its login is asked of the service only by the fact a login reports."""
    placed = await _placed(
        monkeypatch,
        ProcessSpec(host="lab", login_host="remote-agents"),
        _found("lab", "ada@x.y", _RUNS, _HERS),
        _found("remote-agents", "ada@x.y", _LOGINS, None),
        _found("remote-agents", _AGENTS, _LOGINS, _SERVICE_HOST),
    )

    assert (placed.host, placed.remote) == ("lab", False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("hosted_domain", "admitted"),
    [("x.y", True), (None, False)],
    ids=["Google names her domain", "no domain claim"],
)
async def test_a_service_that_admits_a_domain_admits_one_google_says_is_of_it(
    monkeypatch: pytest.MonkeyPatch,
    *,
    hosted_domain: str | None,
    admitted: bool,
) -> None:
    """A domain pattern is matched as staff's is: by the ``hd`` claim alone."""
    service = _found("remote-agents", _AGENTS, _REMOTE, _SERVICE_HOST)
    placing = _placed(
        monkeypatch,
        ProcessSpec(),
        *((service,) if admitted else ()),
        admits=("*@x.y",),
        hosted_domain=hosted_domain,
    )

    if admitted:
        assert (await placing).remote
        return
    with pytest.raises(HTTPException):
        _ = await placing


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("spec", "steps", "admits", "default", "says"),
    [
        (
            ProcessSpec(),
            (),
            ("ada@x.y",),
            "",
            "this server names no host to run agents on; name one",
        ),
        (
            ProcessSpec(host="bobs"),
            (_found("bobs", "ada@x.y", _RUNS, None),),
            ("ada@x.y",),
            "remote-agents",
            "no host bobs runs agents for you",
        ),
        (
            ProcessSpec(),
            (),
            (),
            "remote-agents",
            "no host remote-agents runs agents for you",
        ),
        (
            ProcessSpec(),
            (_found("remote-agents", _AGENTS, _REMOTE, None),),
            ("ada@x.y",),
            "remote-agents",
            "no host remote-agents runs agents for you",
        ),
        (
            ProcessSpec(args=("--model", "opus")),
            (_found("remote-agents", _AGENTS, _REMOTE, _SERVICE_HOST),),
            ("ada@x.y",),
            "remote-agents",
            "remote-agents runs agents for others, and takes no sagent flags",
        ),
        (
            ProcessSpec(host="lab", login_host="laptop"),
            (
                _found("lab", "ada@x.y", _RUNS, _HERS),
                _found("laptop", "ada@x.y", _LOGINS, None),
            ),
            ("ada@x.y",),
            "remote-agents",
            "no host laptop answers model calls for you",
        ),
    ],
    ids=[
        "no host and no default",
        "another's host",
        "a service that admits her not",
        "a service not enrolled",
        "flags for a service",
        "a login not hers",
    ],
)
async def test_a_start_no_place_serves_is_refused_as_no_place(
    monkeypatch: pytest.MonkeyPatch,
    *,
    spec: ProcessSpec,
    steps: tuple[Says, ...],
    admits: tuple[str, ...],
    default: str,
    says: str,
) -> None:
    with pytest.raises(HTTPException) as refused:
        _ = await _placed(monkeypatch, spec, *steps, admits=admits, default=default)

    assert (refused.value.status_code, refused.value.detail) == (
        409,
        {"code": "no_place", "message": says},
    )


class _Later(Later):
    """The app's delayed work, kept for the test to run instead of waited for."""

    def __init__(self) -> None:
        super().__init__()
        self.asked: list[tuple[float, Callable[[], Awaitable[None]]]] = []

    @override
    def run(
        self,
        delay_sec: float,
        work: Callable[[], Awaitable[None]],
    ) -> asyncio.Task[None]:
        self.asked.append((delay_sec, work))
        return asyncio.ensure_future(_nothing())


async def _nothing() -> None:
    """Do nothing, in place of work the test runs itself."""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("taken", "given_up_as_taken", "reason"),
    [
        (True, True, "not started"),
        (False, False, "its host did not take it"),
        (False, True, ""),
        (True, False, ""),
    ],
    ids=[
        "taken, given up as taken",
        "still queued, given up as queued",
        "still queued, given up as taken",
        "taken, given up as queued",
    ],
)
async def test_a_give_up_revokes_the_key_of_the_start_it_detached_alone(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
    *,
    taken: bool,
    given_up_as_taken: bool,
    reason: str,
) -> None:
    _, store, engine = route_client
    relay = SessionRelay()
    patch_state(monkeypatch, "session_relay", relay)
    process_id, host_id = UUID(int=2), UUID(int=3)
    _ = relay.start(
        _SESSION,
        ProcessSpec(),
        process_id=process_id,
        host_id=host_id,
        host="lab",
        token=f"key-{process_id.hex}",
        remote=False,
    )
    if taken:
        _ = await relay.wait(host_id, wait_sec=0, dropped=AsyncMock(return_value=False))
    engine.conn.fetch.return_value = [{"id": UUID(int=7)}]
    forgotten: list[UUID] = []
    monkeypatch.setattr(store, "forget_bearer_identity_for_key", forgotten.append)

    await session_relay_routes._give_up(
        cast("Request", SimpleNamespace(app=app)),
        session_id=_SESSION,
        process_id=process_id,
        taken=given_up_as_taken,
    )

    process = relay.process(_SESSION)
    assert process is not None
    if not reason:
        assert process.state == "starting"
        engine.conn.fetch.assert_not_called()
        return
    assert (process.state, process.reason) == ("detached", reason)
    revoked = engine.conn.fetch.call_args
    assert revoked is not None
    assert revoked.args[1:] == (f"agent process {process_id}", None)
    assert forgotten == [UUID(int=7)]


@pytest.mark.asyncio
async def test_a_stop_detaches_the_process_and_revokes_its_key_a_minute_later(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The minute lets a stopped process upload its last records."""
    _, store, engine = route_client
    relay, later = SessionRelay(), _Later()
    patch_state(monkeypatch, "session_relay", relay)
    patch_state(monkeypatch, "later", later)
    process_id = UUID(int=2)
    _ = relay.start(
        _SESSION,
        ProcessSpec(),
        process_id=process_id,
        host_id=UUID(int=3),
        host="lab",
        token=f"key-{process_id.hex}",
        remote=False,
    )
    request = cast("Request", SimpleNamespace(app=app))

    stopped = await session_relay_routes.stop_process(request, _SESSION)
    nothing = await session_relay_routes.stop_process(request, UUID(int=4))

    assert stopped is not None
    assert stopped == relay.process(_SESSION)
    assert (stopped.state, stopped.reason) == ("detached", "stopped")
    assert nothing is None
    ((delay_sec, revoke),) = later.asked
    assert delay_sec == 60.0
    keys = [UUID(int=7)]
    engine.conn.fetch.return_value = [{"id": key} for key in keys]
    forgotten: list[UUID] = []
    monkeypatch.setattr(store, "forget_bearer_identity_for_key", forgotten.append)
    await revoke()
    revoked = engine.conn.fetch.call_args
    assert revoked is not None
    assert revoked.args[1:] == (f"agent process {process_id}", None)
    assert forgotten == keys


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
