"""The mux tool relay, with an in-memory transport and a fake clock."""

from __future__ import annotations

from typing import TYPE_CHECKING, override
from unittest.mock import Mock

import asyncio
import logging
import uuid

import pytest
import pytest_asyncio

from trackinizer.server import tool_relay
from trackinizer.server.terminals import SocketClosedError, sweep_loop
from trackinizer.server.tool_relay import (
    ToolConnectionBusyError,
    ToolConnectionEndError,
    ToolConnectionUnavailableError,
    ToolRelay,
)
from trackinizer.wire import wire_terminals, wire_tools
from trackinizer.wire.wire_tools import MuxStream, ToolMux


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine

    from trackinizer.server.tool_relay import Inbound, ToolConnection


pytestmark = pytest.mark.asyncio

_HOST = uuid.UUID(int=1)
_OTHER = uuid.UUID(int=2)
_ASKER = "ada@x.y"
_KEY = uuid.UUID(int=3)
_INSTANCE = uuid.UUID(int=4)
_LINE_BYTES = 256
_HELLO = b'{"py/object": "Hello", "session": "a"}\n'
_WELCOME = b'{"py/object": "Welcome", "resumed": false}\n'
_PING = b'{"py/object": "Ping"}\n'


async def _pass_through(
    connection: ToolConnection,
    frame: bytes,
) -> tuple[bytes, bytes]:
    del connection
    return frame, b""


class _Time:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    async def advance(self, seconds: float) -> None:
        self.now += seconds
        await _settle()


async def _settle() -> None:
    for _ in range(30):
        await asyncio.sleep(0)


class _Socket:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.closed: list[tuple[int, str]] = []
        self.reads = 0
        self.gone = False
        self.open = asyncio.Event()
        self.open.set()
        self._frames: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def send_bytes(self, data: bytes) -> None:
        await self.open.wait()
        if self.gone:
            raise SocketClosedError
        self.sent.append(data)

    async def close(self, code: int, reason: str = "") -> None:
        self.closed.append((code, reason))

    async def read(self) -> bytes | None:
        self.reads += 1
        return await self._frames.get()

    def push(self, *frames: bytes) -> None:
        for frame in frames:
            self._frames.put_nowait(frame)

    def drop(self) -> None:
        self.gone = True
        self._frames.put_nowait(None)


class _Stream(MuxStream):
    def __init__(self, mux: ToolMux, stream_id: uuid.UUID) -> None:
        super().__init__(mux, stream_id)
        self.peer = _Socket()
        self.closes = 0

    @override
    async def send(self, message: bytes, /) -> None:
        try:
            await self.peer.send_bytes(message)
        except SocketClosedError as error:
            raise ConnectionError("stream left") from error

    @override
    async def read(self) -> bytes | None:
        return await self.peer.read()

    @override
    async def close(self) -> None:
        self.closes += 1
        self.peer.drop()


class _Mux(ToolMux):
    """The test controls greetings, stream capacity, and when the reader unwinds."""

    def __init__(self) -> None:
        self.transport = _Socket()
        super().__init__(self.transport.send_bytes)
        self.capacity = 32
        self.stops = 0
        self.streams: list[_Stream] = []
        self.greeting: tuple[bytes, ...] | None = (_WELCOME,)
        self.open_error: Exception | None = None

    @property
    @override
    def has_capacity(self) -> bool:
        return (
            not self.stops
            and sum(not stream.closes for stream in self.streams) < self.capacity
        )

    @override
    async def open(self, stream_id: uuid.UUID) -> _Stream:
        if self.open_error is not None:
            raise self.open_error
        assert self.has_capacity
        stream = _Stream(self, stream_id)
        self.streams.append(stream)
        if self.greeting is not None:
            stream.peer.push(*self.greeting)
        return stream

    @override
    def stop(self) -> None:
        self.stops += 1

    @override
    async def run(
        self,
        read: Callable[[], Awaitable[bytes | None]],
        *,
        accept: Callable[[MuxStream], None] | None = None,
    ) -> None:
        assert accept is None
        assert await read() is None


class _World:
    def __init__(self) -> None:
        self.time = _Time()
        self.relay = ToolRelay(
            alive=self._alive,
            line_bytes=_LINE_BYTES,
            clock=self.time,
        )
        self.tasks: list[asyncio.Task[object]] = []
        self.dead: set[uuid.UUID] = set()
        self.unknown: set[uuid.UUID] = set()
        self.asked: list[tuple[str, uuid.UUID | None, uuid.UUID]] = []

    async def _alive(self, connection: ToolConnection) -> bool:
        self.asked.append(
            (connection.asker, connection.api_key_id, connection.instance),
        )
        if connection.id in self.unknown:
            raise OSError("the database is down")
        return connection.id not in self.dead

    def run(self, work: Coroutine[object, object, object]) -> asyncio.Task[object]:
        task = asyncio.ensure_future(work)
        self.tasks.append(task)
        return task

    async def register(
        self,
        host_id: uuid.UUID = _HOST,
        instance: uuid.UUID = _INSTANCE,
    ) -> tuple[_Mux, asyncio.Task[object]]:
        mux = _Mux()
        serving = self.run(
            self.relay.serve_mux(host_id, instance, mux, mux.transport.read),
        )
        await _settle()
        assert not serving.done()
        assert mux.transport.reads == 1
        return mux, serving

    def join(
        self,
        host_id: uuid.UUID = _HOST,
        *,
        inbound: Inbound = _pass_through,
    ) -> tuple[ToolConnection, _Socket]:
        socket = _Socket()
        connection = self.relay.join(
            host_id,
            agent=socket,
            asker=_ASKER,
            api_key_id=_KEY,
            inbound=inbound,
        )
        return connection, socket

    async def agent(
        self,
        host_id: uuid.UUID = _HOST,
        *,
        inbound: Inbound = _pass_through,
    ) -> tuple[ToolConnection, _Socket]:
        connection, socket = self.join(host_id, inbound=inbound)
        _ = self.run(self.relay.serve_agent(connection, socket.read))
        await _settle()
        return connection, socket

    async def live(self, mux: _Mux) -> tuple[ToolConnection, _Socket, _Stream]:
        connection, agent = await self.agent()
        agent.push(_HELLO)
        await _settle()
        stream = mux.streams[-1]
        assert connection.stream is stream
        assert stream.peer.sent == [_HELLO]
        assert agent.sent == [_WELCOME]
        return connection, agent, stream


@pytest_asyncio.fixture
async def world() -> AsyncIterator[_World]:
    found = _World()
    yield found
    for task in found.tasks:
        _ = task.cancel()
    _ = await asyncio.gather(*found.tasks, return_exceptions=True)


@pytest_asyncio.fixture
async def mux(world: _World) -> _Mux:
    found, _ = await world.register()
    return found


async def test_mux_is_required_and_duplicate_does_not_replace_it(
    world: _World,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=tool_relay.__name__)
    with pytest.raises(ToolConnectionUnavailableError):
        _ = world.join()
    mux, serving = await world.register()
    duplicate = _Mux()
    duplicate.transport.drop()
    assert (
        await world.relay.serve_mux(
            _HOST,
            _INSTANCE,
            duplicate,
            duplicate.transport.read,
        )
        is False
    )
    assert duplicate.transport.reads == 0
    _, agent, _ = await world.live(mux)
    _, parked = await world.agent()
    with pytest.raises(ToolConnectionUnavailableError):
        _ = world.join(_OTHER)

    mux.transport.drop()
    await _settle()

    assert serving.done()
    assert serving.result() is True
    assert (
        agent.closed
        == parked.closed
        == [(wire_terminals.CLOSE_DAEMON_LOST, "the daemon left")]
    )
    assert [
        line for line in caplog.messages if line.startswith("daemon tool mux ")
    ] == [
        f"daemon tool mux opened: host_id={_HOST} instance={_INSTANCE}",
        f"daemon tool mux ended: host_id={_HOST} instance={_INSTANCE}",
    ]
    with pytest.raises(ToolConnectionUnavailableError):
        _ = world.join()


async def test_mux_reader_failure_unregisters_the_daemon(world: _World) -> None:
    async def broken_read() -> bytes | None:
        raise ValueError("broken mux reader")

    with pytest.raises(ValueError, match="broken mux reader"):
        _ = await world.relay.serve_mux(_HOST, _INSTANCE, _Mux(), broken_read)
    _ = await world.register()


async def test_revocation_closes_only_its_agents_and_old_cleanup_keeps_replacement(
    world: _World,
) -> None:
    first, serving = await world.register()
    other, other_serving = await world.register(_OTHER)
    _, agent, stream = await world.live(first)
    _, parked = await world.agent()
    _, unrelated = await world.agent(_OTHER)

    world.relay.close_host(_HOST)
    world.relay.close_host(_HOST)
    await _settle()

    revoked = (
        wire_terminals.CLOSE_HOST_REVOKED,
        "the host was revoked, or joined again",
    )
    assert agent.closed == parked.closed == [revoked]
    assert stream.closes == first.stops == 1
    assert unrelated.closed == []
    assert other.stops == 0
    assert not other_serving.done()
    instance = uuid.UUID(int=5)
    replacement, _ = await world.register(instance=instance)
    first.transport.drop()
    await _settle()
    assert serving.done()
    assert serving.result() is True
    fresh, _, _ = await world.live(replacement)
    assert fresh.instance == instance


async def test_parked_socket_limit_is_independent_of_stream_limit_and_other_hosts(
    world: _World,
    mux: _Mux,
) -> None:
    _ = await world.register(_OTHER)
    _, agent = await world.agent()
    for _ in range(1023):
        _ = world.join()
    assert mux.streams == []
    with pytest.raises(ToolConnectionBusyError) as busy:
        _ = world.join()
    assert busy.value.limit == 1024
    _ = world.join(_OTHER)

    agent.drop()
    await _settle()
    _ = world.join()
    with pytest.raises(ToolConnectionBusyError):
        _ = world.join()


async def test_sweep_keeps_the_idle_stream_and_never_replays_a_hello(
    world: _World,
    mux: _Mux,
) -> None:
    _, agent, stream = await world.live(mux)
    await world.time.advance(60)
    await world.relay.sweep()

    assert stream.closes == 0
    agent.push(_PING + b"request\n")
    await _settle()
    assert mux.streams == [stream]
    assert b"".join(stream.peer.sent) == _HELLO + _PING + b"request\n"
    assert agent.sent == [_WELCOME]
    assert agent.closed == []


async def test_first_forwarded_bytes_open_one_stream_and_pass_the_native_greeting(
    world: _World,
    mux: _Mux,
) -> None:
    refusal = b'{"py/object": "Refused", "reason": "All workers are busy"}\n'
    mux.greeting = (refusal[:8], refusal[8:])
    _, agent = await world.agent()
    agent.push(b"")
    await _settle()
    assert mux.streams == []
    agent.push(_HELLO)
    await _settle()
    [stream] = mux.streams
    assert stream.peer.sent == [_HELLO]
    assert b"".join(agent.sent) == refusal
    stream.peer.drop()
    await _settle()
    assert agent.closed == [(wire_terminals.CLOSE_DAEMON_LOST, "the daemon left")]
    assert stream.closes == 1


async def test_at_capacity_refuses_new_agent_without_displacing_idle_agent(
    world: _World,
    mux: _Mux,
) -> None:
    mux.capacity = 1
    _, first, stream = await world.live(mux)
    _, second = await world.agent()
    second.push(_HELLO)
    await _settle()

    assert stream.closes == 0
    assert mux.streams == [stream]
    [(code, reason)] = second.closed
    assert code == wire_terminals.CLOSE_REFUSED
    assert "stream limit" in reason
    assert second.sent == []
    assert first.closed == []
    first.push(b"request\n")
    await _settle()
    assert stream.peer.sent == [_HELLO, b"request\n"]

    first.drop()
    await _settle()
    _, _, fresh = await world.live(mux)
    assert mux.streams == [stream, fresh]
    assert fresh.id != stream.id


async def test_configured_stream_budget_is_independent_of_wire_capacity(
    world: _World,
    mux: _Mux,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(world.relay, "_streams", 1)
    _, first, stream = await world.live(mux)
    assert mux.has_capacity
    _, second = await world.agent()
    second.push(_HELLO)
    await _settle()
    assert second.closed == [
        (wire_terminals.CLOSE_REFUSED, "the host has reached its tool stream limit"),
    ]
    assert first.closed == []
    assert stream.closes == 0


async def test_daemon_disconnect_also_closes_agents_that_have_not_sent_hello(
    world: _World,
) -> None:
    mux, serving = await world.register()
    _, first, stream = await world.live(mux)
    _, second = await world.agent()
    mux.transport.drop()
    await _settle()
    assert serving.result() is True
    assert mux.streams == [stream]
    assert stream.closes == 1
    assert (
        first.closed
        == second.closed
        == [(wire_terminals.CLOSE_DAEMON_LOST, "the daemon left")]
    )


async def test_silent_mux_open_expires_without_leaving_a_slot(
    world: _World,
    mux: _Mux,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def stalled(stream_id: uuid.UUID) -> _Stream:
        del stream_id
        return await asyncio.Future[_Stream]()

    def immediately(seconds: float) -> asyncio.Timeout:
        assert seconds == 10.0
        return asyncio.timeout(0)

    with monkeypatch.context() as patch:
        patch.setattr(mux, "open", stalled)
        patch.setattr(tool_relay, "asyncio", Mock(wraps=asyncio, timeout=immediately))
        _, agent = await world.agent()
        agent.push(_HELLO)
        await _settle()
        assert agent.closed == [(wire_terminals.CLOSE_DAEMON_LOST, "the daemon left")]
    assert mux.streams == []
    _, fresh, _ = await world.live(mux)
    assert fresh.closed == []


@pytest.mark.parametrize("error", [ConnectionError, ValueError])
async def test_open_failure_closes_agent_as_daemon_lost(
    world: _World,
    mux: _Mux,
    error: type[Exception],
) -> None:
    mux.open_error = error("failed stream open")
    connection, agent = await world.agent()
    agent.push(_HELLO)
    await _settle()
    assert mux.streams == []
    assert connection.stream is None
    assert agent.closed == [(wire_terminals.CLOSE_DAEMON_LOST, "the daemon left")]


async def test_ping_does_not_count_as_host_activity(world: _World, mux: _Mux) -> None:
    _, agent, stream = await world.live(mux)
    await world.time.advance(29)
    agent.push(_PING)
    await _settle()
    assert stream.peer.sent == [_HELLO, _PING]
    assert world.relay.idle_sec(_HOST) == 29


async def test_sweep_skips_an_agent_blocked_on_a_daemon_send(
    world: _World,
    mux: _Mux,
) -> None:
    _, agent, stream = await world.live(mux)
    await world.time.advance(30)
    agent.open.clear()
    stream.peer.push(b"blocked\n")
    await _settle()
    sweeping = world.run(world.relay.sweep())
    await _settle()
    assert sweeping.done()
    assert sweeping.result() is None
    assert stream.closes == 0
    assert agent.sent == [_WELCOME]

    agent.open.set()
    await _settle()
    await world.relay.sweep()
    assert agent.sent == [_WELCOME, b"blocked\n"]
    assert stream.closes == 0
    assert agent.closed == []


async def test_revoking_during_inbound_does_not_open_a_stale_stream(
    world: _World,
    mux: _Mux,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def revoke(connection: ToolConnection, frame: bytes) -> tuple[bytes, bytes]:
        world.relay.close_host(connection.host_id)
        return frame, b""

    _, agent = await world.agent(inbound=revoke)
    agent.push(_HELLO)
    await _settle()
    assert mux.streams == []
    assert agent.closed == [
        (wire_terminals.CLOSE_HOST_REVOKED, "the host was revoked, or joined again"),
    ]
    assert "tool connection failed" not in caplog.text


async def test_active_calls_keep_host_busy_until_result_or_ack(
    world: _World,
    mux: _Mux,
) -> None:
    connection, agent, stream = await world.live(mux)
    await world.time.advance(30)
    assert world.relay.idle_sec(_HOST) == world.relay.idle_sec(_OTHER) == 30
    world.relay.called(connection, "c1")
    world.relay.called(connection, "c2")
    await world.time.advance(3_000)
    await world.relay.sweep()
    assert world.relay.idle_sec(_HOST) == 0
    assert world.relay.idle_sec(_OTHER) == 3_030
    assert stream.closes == 0

    head = b'{"py/object": "Res'
    tail = b'ult", "id": "c1"}\n'
    long = (
        b'{"py/object": "Result", "id": "c2", "record": "'
        + b"x" * _LINE_BYTES
        + b'"}\n'
    )
    stream.peer.push(head, tail, long)
    await _settle()
    assert agent.sent == [_WELCOME, head, tail, long]
    assert set(connection.running) == {"c2"}
    assert world.relay.idle_sec(_HOST) == 0
    world.relay.acked(connection, "c2")
    await world.time.advance(7)
    world.relay.acked(connection, "not-a-call")
    assert connection.running == {}
    assert world.relay.idle_sec(_HOST) == 7
    await world.relay.sweep()
    assert stream.closes == 0
    assert agent.closed == []


async def test_dropped_pending_calls_keep_host_busy_until_the_latest_deadline(
    world: _World,
    mux: _Mux,
) -> None:
    first, first_agent, _ = await world.live(mux)
    second, second_agent, _ = await world.live(mux)
    world.relay.called(first, "a")
    await world.time.advance(8)
    world.relay.called(second, "b")
    await world.time.advance(1)
    second_agent.drop()
    await _settle()
    first_agent.drop()
    await _settle()
    assert first.ended.done()
    assert second.ended.done()
    await world.time.advance(3_598)
    assert world.relay.idle_sec(_HOST) == 0
    assert world.relay.idle_sec(_OTHER) == 3_607
    await world.time.advance(5)
    assert world.relay.idle_sec(_HOST) == 4


async def test_a_live_call_never_answered_only_keeps_host_busy_for_an_hour(
    world: _World,
    mux: _Mux,
) -> None:
    connection, _, _ = await world.live(mux)
    world.relay.called(connection, "hung")
    await world.time.advance(3_599)
    assert world.relay.idle_sec(_HOST) == 0
    await world.time.advance(2)
    assert world.relay.idle_sec(_HOST) == 1


async def test_relay_preserves_bytes_and_backpressure_in_both_directions(
    world: _World,
    mux: _Mux,
) -> None:
    _, agent, stream = await world.live(mux)
    peer = stream.peer
    agent.open.clear()
    peer.open.clear()
    reads = agent.reads, peer.reads
    agent.push(b"agent \xff\n", b"next agent\n")
    peer.push(b"daemon \xfe\n", b"next daemon\n")
    await _settle()
    assert (agent.reads, peer.reads) == reads
    assert (agent.sent, peer.sent) == ([_WELCOME], [_HELLO])
    agent.open.set()
    peer.open.set()
    await _settle()
    assert agent.sent == [_WELCOME, b"daemon \xfe\n", b"next daemon\n"]
    assert peer.sent == [_HELLO, b"agent \xff\n", b"next agent\n"]


async def test_writes_are_split_at_the_frame_limit_and_empty_data_is_not_sent(
    world: _World,
    mux: _Mux,
) -> None:
    _, agent, stream = await world.live(mux)
    size = wire_tools.FRAME_BYTES
    agent.push(b"", b"a" * (size + 1))
    stream.peer.push(b"", b"b" * (2 * size))
    await _settle()
    assert stream.peer.sent == [_HELLO, b"a" * size, b"a"]
    assert agent.sent == [_WELCOME, b"b" * size, b"b" * size]


@pytest.mark.parametrize(
    ("ending", "code", "reason", "by"),
    [
        ("agent-eof", wire_terminals.CLOSE_ENDED, "the agent left", "agent"),
        ("agent-send", wire_terminals.CLOSE_ENDED, "the agent left", "agent"),
        ("daemon-eof", wire_terminals.CLOSE_DAEMON_LOST, "the daemon left", "daemon"),
        ("daemon-send", wire_terminals.CLOSE_DAEMON_LOST, "the daemon left", "daemon"),
        ("cancel", wire_terminals.CLOSE_ENDED, "the server is stopping", "server"),
    ],
)
async def test_first_disconnect_closes_stream_and_logs_once(
    world: _World,
    mux: _Mux,
    caplog: pytest.LogCaptureFixture,
    *,
    ending: str,
    code: int,
    reason: str,
    by: str,
) -> None:
    caplog.set_level(logging.INFO, logger=tool_relay.__name__)
    connection, agent, stream = await world.live(mux)
    await world.time.advance(3)
    if ending == "agent-eof":
        agent.open.clear()
        stream.peer.push(b"blocked")
        await _settle()
        agent.drop()
    elif ending == "agent-send":
        agent.gone = True
        stream.peer.push(b"undeliverable")
    elif ending == "daemon-eof":
        stream.peer.drop()
    elif ending == "daemon-send":
        stream.peer.gone = True
        agent.push(b"undeliverable")
    else:
        _ = world.tasks[-1].cancel()
    await _settle()
    world.relay.close_host(_HOST)
    await _settle()

    assert agent.closed == [(code, reason)]
    assert stream.closes == 1
    assert [
        line for line in caplog.messages if line.startswith("tool connection ended:")
    ] == [
        (
            f"tool connection ended: connection={connection.id} host_id={_HOST} by={by} "
            f"code={code} reason={reason!r} cause=- instance={_INSTANCE} duration_sec=3.000"
        ),
    ]


async def test_sweep_rechecks_live_and_parked_clients_with_their_daemon_identity(
    world: _World,
    mux: _Mux,
    caplog: pytest.LogCaptureFixture,
) -> None:
    unknown, surviving, stream = await world.live(mux)
    dead, parked = await world.agent()
    world.unknown.add(unknown.id)
    world.dead.add(dead.id)

    looping = world.run(sweep_loop(world.relay, interval_sec=0))
    await _settle()

    assert world.asked[:2] == [(_ASKER, _KEY, _INSTANCE), (_ASKER, _KEY, _INSTANCE)]
    assert surviving.closed == []
    assert stream.closes == 0
    assert parked.closed == [
        (
            wire_terminals.CLOSE_REFUSED,
            "no longer allowed: what opened this tool connection no longer holds",
        ),
    ]
    assert "liveness unknown" in caplog.text
    assert not looping.done()


async def test_inbound_rewrites_requests_and_places_its_reply_between_daemon_lines(
    world: _World,
    mux: _Mux,
) -> None:
    shown: list[bytes] = []

    async def inbound(connection: ToolConnection, frame: bytes) -> tuple[bytes, bytes]:
        del connection
        shown.append(frame)
        if frame == b"denied\n":
            return b"", b"refused\n"
        if frame == b"allowed\n":
            return b"rewritten\n", b""
        return frame, b""

    _, agent = await world.agent(inbound=inbound)
    agent.push(_HELLO)
    await _settle()
    stream = mux.streams[0]
    stream.peer.push(b"half a li")
    await _settle()
    agent.push(b"denied\n", b"allowed\n")
    await _settle()
    assert agent.sent == [_WELCOME, b"half a li"]
    assert shown == [_HELLO, b"denied\n"]
    stream.peer.push(b"ne\n")
    await _settle()
    assert agent.sent == [_WELCOME, b"half a li", b"ne\n", b"refused\n"]
    assert shown == [_HELLO, b"denied\n", b"allowed\n"]
    assert stream.peer.sent == [_HELLO, b"rewritten\n"]


@pytest.mark.parametrize("kind", ["failure", "refusal", "caused-refusal"])
async def test_line_check_errors_refuse_without_forwarding_or_logging_the_payload(
    world: _World,
    mux: _Mux,
    caplog: pytest.LogCaptureFixture,
    kind: str,
) -> None:
    caplog.set_level(logging.INFO, logger=tool_relay.__name__)

    async def inbound(connection: ToolConnection, frame: bytes) -> tuple[bytes, bytes]:
        del connection
        if frame == _HELLO:
            return frame, b""
        if kind == "failure":
            raise ValueError("the line check broke")
        if kind == "caused-refusal":
            raise ToolConnectionEndError("é" * 100) from ValueError("invalid line")
        raise ToolConnectionEndError("é" * 100)

    _, agent = await world.agent(inbound=inbound)
    agent.push(_HELLO, b"secret request\n")
    await _settle()
    assert mux.streams[0].peer.sent == [_HELLO]
    assert mux.streams[0].closes == 1
    if kind == "failure":
        reason = "the server could not go on relaying this tool connection"
        assert "by=server" in caplog.text
    else:
        reason = "é" * 61
        assert "by=agent" in caplog.text
    assert agent.closed == [(wire_terminals.CLOSE_REFUSED, reason)]
    assert f"cause={'-' if kind == 'refusal' else 'ValueError'}" in caplog.text
    assert "secret request" not in caplog.text


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
