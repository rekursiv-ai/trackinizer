"""An agent process's socket, against a small relay that records frames and sends inputs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Final, cast
from unittest.mock import Mock
from uuid import UUID

import asyncio
import contextlib
import itertools

from websockets.asyncio.client import connect
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

import pytest

from trackinizer.client import session_socket
from trackinizer.client.session_socket import (
    SessionSocket,
    SocketClosedError,
    retry_delays,
)
from trackinizer.wire.wire_session_relay import (
    MAX_FRAME_BYTES,
    AgentState,
    EventLine,
    Hello,
    Input,
    Setting,
    SettingsLine,
    StateLine,
)
from trackinizer.wire.wire_terminals import (
    CLOSE_ENDED,
    CLOSE_HOST_REVOKED,
    CLOSE_REFUSED,
    CLOSE_TAKEN_OVER,
)


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from websockets.http11 import Request, Response


_SESSION: Final = UUID(int=7)
_HELLO: Final = Hello(process_id=UUID(int=8), version="1.2.3", host="laptop")
_STATE: Final = StateLine(
    state=AgentState(line="[1s]", busy=False, since=datetime(2026, 1, 1, tzinfo=UTC)),
)
_SETTINGS: Final = SettingsLine(settings=(Setting(name=("model",), help_short="m"),))


def _event(n: int) -> EventLine:
    return EventLine(kind="ModelResponsePartial", payload={"text": str(n)})


@dataclass(frozen=True, slots=True, kw_only=True)
class _Client:
    base_url: str
    api_key: str = "process-key"


@dataclass(slots=True, kw_only=True)
class _Relay:
    """A stand-in for Trackinizer's process socket: it keeps each connection's frames."""

    port: int = 0
    connections: list[list[str]] = field(default_factory=list[list[str]])
    paths: list[str] = field(default_factory=list[str])
    keys: list[str] = field(default_factory=list[str])
    refusing: HTTPStatus | None = None
    refused: asyncio.Event = field(default_factory=asyncio.Event)
    """Set once a dial was refused: the socket is down by then, and dialing."""

    live: ServerConnection | None = None
    arrived: asyncio.Condition = field(default_factory=asyncio.Condition)

    async def handle(self, connection: ServerConnection) -> None:
        frames: list[str] = []
        self.connections.append(frames)
        assert connection.request is not None
        self.paths.append(connection.request.path)
        self.keys.append(connection.request.headers.get("Authorization", ""))
        self.live = connection
        with contextlib.suppress(ConnectionClosed):
            async for message in connection:
                assert isinstance(message, str)
                async with self.arrived:
                    frames.append(message)
                    self.arrived.notify_all()

    def refuse(self, connection: ServerConnection, request: Request) -> Response | None:
        del request
        if self.refusing is None:
            return None
        self.refused.set()
        return connection.respond(self.refusing, "refused\n")

    async def until(self, count: int, *, connection: int = -1) -> list[str]:
        """Return a connection's frames once it holds ``count``."""
        async with asyncio.timeout(5), self.arrived:
            await self.arrived.wait_for(
                lambda: (
                    len(self.connections) > (max(connection, 0))
                    and len(self.connections[connection]) >= count
                ),
            )
        return self.connections[connection]

    def client(self) -> _Client:
        return _Client(base_url=f"http://127.0.0.1:{self.port}")


@contextlib.asynccontextmanager
async def _relay() -> AsyncGenerator[_Relay]:
    relay = _Relay()
    async with serve(
        relay.handle,
        "127.0.0.1",
        0,
        process_request=relay.refuse,
        # As the server takes a frame as large as the wire allows.
        max_size=MAX_FRAME_BYTES,
    ) as server:
        relay.port = server.sockets[0].getsockname()[1]
        yield relay


def _socket(relay: _Relay, *, give_up_sec: float | None = None) -> SessionSocket:
    return SessionSocket(
        relay.client(),
        session_id=_SESSION,
        hello=_HELLO,
        give_up_sec=give_up_sec,
    )


@pytest.mark.asyncio
async def test_it_dials_the_sessions_socket_with_its_key_and_says_hello_first() -> None:
    async with _relay() as relay, _socket(relay):
        frames = await relay.until(1)

    assert relay.paths == [f"/api/sessions/{_SESSION}/process/ws"]
    assert relay.keys == ["Bearer process-key"]
    assert [Hello.model_validate_json(f) for f in frames] == [_HELLO]


@pytest.mark.asyncio
async def test_it_pings_every_twenty_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    dial = Mock(wraps=connect)
    monkeypatch.setattr(session_socket, "connect", dial)
    async with _relay() as relay, _socket(relay):
        _ = await relay.until(1)

    assert [
        cast("int", call.kwargs["ping_interval"]) for call in dial.call_args_list
    ] == [
        20,
    ]


@pytest.mark.asyncio
async def test_lines_go_up_in_order_as_one_frame_each() -> None:
    async with _relay() as relay, _socket(relay) as socket:
        for n in range(3):
            socket.send(_event(n))
        frames = await relay.until(4)

    assert [EventLine.model_validate_json(f) for f in frames[1:]] == [
        _event(n) for n in range(3)
    ]


@pytest.mark.asyncio
async def test_a_line_over_the_frame_limit_is_not_sent() -> None:
    big = EventLine(
        kind="ModelResponsePartial",
        payload={"text": "x" * MAX_FRAME_BYTES},
    )
    async with _relay() as relay, _socket(relay) as socket:
        socket.send(big)
        socket.send(_event(1))
        frames = await relay.until(2)

    assert [EventLine.model_validate_json(f) for f in frames[1:]] == [_event(1)]


@pytest.mark.asyncio
async def test_a_new_connection_says_hello_then_the_last_state_and_settings() -> None:
    async with _relay() as relay, _socket(relay) as socket:
        socket.send(_STATE)
        socket.send(_SETTINGS)
        socket.send(_event(1))
        _ = await relay.until(4)
        assert relay.live is not None
        await relay.live.close(code=1011)
        again = await relay.until(3, connection=1)

    assert again == [
        _HELLO.model_dump_json(),
        _STATE.model_dump_json(),
        _SETTINGS.model_dump_json(),
    ]


@pytest.mark.asyncio
async def test_a_line_of_the_frame_limit_exactly_is_sent() -> None:
    line = _event(0)
    padding = "x" * (MAX_FRAME_BYTES - len(line.model_dump_json().encode()) + 1)
    whole = EventLine(kind=line.kind, payload={"text": padding})
    async with _relay() as relay, _socket(relay) as socket:
        socket.send(whole)
        frames = await relay.until(2)

    assert len(frames[1].encode()) == MAX_FRAME_BYTES


@pytest.mark.asyncio
async def test_a_kept_state_over_the_frame_limit_is_not_sent_on_a_new_connection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    big = StateLine(
        state=AgentState(
            line="x" * MAX_FRAME_BYTES,
            busy=False,
            since=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    )
    size = len(big.model_dump_json().encode())
    async with _relay() as relay, _socket(relay) as socket:
        socket.send(big)
        socket.send(_SETTINGS)
        _ = await relay.until(2)
        assert relay.live is not None
        await relay.live.close(code=1011)
        again = await relay.until(2, connection=1)

    assert again == [_HELLO.model_dump_json(), _SETTINGS.model_dump_json()]
    assert [record.getMessage() for record in caplog.records] == [
        f"dropped a line of {size} bytes",
    ] * 2


@pytest.mark.asyncio
async def test_lines_sent_while_it_is_down_are_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_socket, "retry_delays", lambda: itertools.repeat(0.05))
    async with _relay() as relay:
        socket = _socket(relay)
        async with socket:
            _ = await relay.until(1)
            relay.refusing = HTTPStatus.SERVICE_UNAVAILABLE
            assert relay.live is not None
            await relay.live.close(code=1011)
            async with asyncio.timeout(5):
                _ = await relay.refused.wait()
            socket.send(_event(1))
            relay.refusing = None
            frames = await relay.until(1, connection=1)
            socket.send(_event(2))
            frames = await relay.until(2, connection=1)

    assert frames == [_HELLO.model_dump_json(), _event(2).model_dump_json()]


@pytest.mark.asyncio
async def test_inputs_come_down_and_a_frame_that_is_not_one_is_passed_over() -> None:
    sent = Input(
        id=UUID(int=3),
        author="ada@x.y",
        kind="Halt",
        payload={"py/object": "Halt"},
    )
    async with _relay() as relay, _socket(relay) as socket:
        _ = await relay.until(1)
        assert relay.live is not None
        await relay.live.send('{"type": "input"}')
        await relay.live.send(sent.model_dump_json())
        received = await anext(aiter(socket.inputs()))

    assert received == sent


@pytest.mark.parametrize(
    "code",
    [CLOSE_ENDED, CLOSE_TAKEN_OVER, CLOSE_REFUSED, CLOSE_HOST_REVOKED],
)
@pytest.mark.asyncio
async def test_one_of_the_four_codes_closes_it_for_good(code: int) -> None:
    async with _relay() as relay, _socket(relay) as socket:
        _ = await relay.until(1)
        assert relay.live is not None
        await relay.live.close(code=code, reason="why")
        async with asyncio.timeout(5):
            closed = await socket.closed

    assert (closed, socket.reason) == (code, "why")
    assert len(relay.connections) == 1


@pytest.mark.parametrize("close", ["1000", "1011", "dropped"])
@pytest.mark.asyncio
async def test_any_other_close_reconnects_with_the_same_key(close: str) -> None:
    async with _relay() as relay, _socket(relay) as socket:
        _ = await relay.until(1)
        assert relay.live is not None
        if close == "dropped":
            relay.live.transport.abort()
        else:
            await relay.live.close(code=int(close))
        _ = await relay.until(1, connection=1)
        reconnected = not socket.closed.done()

    assert reconnected
    assert relay.keys == ["Bearer process-key"] * 2


@pytest.mark.asyncio
async def test_a_close_after_its_waiter_left_is_kept_to_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    told = asyncio.Event()
    close = SessionSocket._close

    def closing(self: SessionSocket, code: int, reason: str) -> None:
        close(self, code, reason)
        told.set()

    monkeypatch.setattr(SessionSocket, "_close", closing)
    async with _relay() as relay, _socket(relay) as socket:
        _ = await relay.until(1)
        _ = socket.closed.cancel()
        assert relay.live is not None
        await relay.live.close(code=CLOSE_ENDED, reason="why")
        async with asyncio.timeout(5):
            _ = await told.wait()

    assert socket.reason == ""


@pytest.mark.asyncio
async def test_a_dial_that_fails_outright_fails_the_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(url: str, **kwargs: object) -> None:
        del url, kwargs
        raise TypeError("no such dial")

    monkeypatch.setattr(session_socket, "connect", broken)
    async with _relay() as relay:
        with pytest.raises(TypeError, match="no such dial"):
            async with _socket(relay):
                pass


@pytest.mark.parametrize("status", [HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN])
@pytest.mark.asyncio
async def test_a_refused_handshake_is_a_refusal(status: HTTPStatus) -> None:
    async with _relay() as relay:
        relay.refusing = status
        with pytest.raises(SocketClosedError) as raised:
            async with _socket(relay):
                pass

    assert (raised.value.code, raised.value.reason) == (
        CLOSE_REFUSED,
        f"the server refused the key: HTTP {status.value}",
    )


@pytest.mark.asyncio
async def test_a_key_refused_on_reconnecting_closes_it_as_refused() -> None:
    async with _relay() as relay, _socket(relay) as socket:
        _ = await relay.until(1)
        relay.refusing = HTTPStatus.UNAUTHORIZED
        assert relay.live is not None
        await relay.live.close(code=1011)
        async with asyncio.timeout(5):
            closed = await socket.closed

    assert closed == CLOSE_REFUSED


@pytest.mark.asyncio
async def test_down_past_its_bound_it_gives_up() -> None:
    async with _relay() as relay, _socket(relay, give_up_sec=0.05) as socket:
        _ = await relay.until(1)
        relay.refusing = HTTPStatus.SERVICE_UNAVAILABLE
        assert relay.live is not None
        await relay.live.close(code=1011)
        async with asyncio.timeout(5):
            closed = await socket.closed

    assert closed == 1006


@pytest.mark.asyncio
async def test_a_server_never_reached_within_its_bound_fails_the_start() -> None:
    async with _relay() as relay:
        relay.refusing = HTTPStatus.SERVICE_UNAVAILABLE
        with pytest.raises(SocketClosedError) as raised:
            async with _socket(relay, give_up_sec=0.05):
                pass

    assert raised.value.code == 1006


@pytest.mark.asyncio
async def test_a_process_that_ends_itself_closes_with_1000() -> None:
    codes: list[int | None] = []
    async with _relay() as relay:
        async with _socket(relay):
            _ = await relay.until(1)
            live = relay.live
        assert live is not None
        await live.wait_closed()
        codes.append(live.close_code)

    assert codes == [1000]


class _Yielding:
    """A connection whose every send yields to the loop, as a socket's write may."""

    def __init__(self, sent: Callable[[str], None]) -> None:
        self.frames: list[str] = []
        self._sent = sent
        self._closed = asyncio.Event()

    async def send(self, text: str) -> None:
        self.frames.append(text)
        self._sent(text)
        await asyncio.sleep(0)

    def __aiter__(self) -> _Yielding:
        return self

    async def __anext__(self) -> str:
        _ = await self._closed.wait()
        raise StopAsyncIteration

    async def close(self) -> None:
        self._closed.set()


@pytest.mark.asyncio
async def test_a_state_sent_while_it_says_hello_again_goes_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sent between the kept lines of a new connection, it is the one a viewer holds."""
    socket = _socket(_Relay())
    busy = _STATE.model_copy(
        update={"state": _STATE.state.model_copy(update={"busy": True})},
    )

    def sent(text: str) -> None:
        if text == _STATE.model_dump_json():
            socket.send(busy)

    connection = _Yielding(sent)

    async def dialed(*args: object, **kwargs: object) -> AsyncGenerator[_Yielding]:
        del args, kwargs
        yield connection
        _ = await asyncio.Event().wait()

    monkeypatch.setattr(session_socket, "connect", dialed)
    socket.send(_STATE)
    async with socket:
        pass

    assert connection.frames == [
        _HELLO.model_dump_json(),
        _STATE.model_dump_json(),
        busy.model_dump_json(),
    ]


def test_a_retry_waits_a_second_doubling_to_thirty() -> None:
    assert list(itertools.islice(retry_delays(), 8)) == [1, 2, 4, 8, 16, 30, 30, 30]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
