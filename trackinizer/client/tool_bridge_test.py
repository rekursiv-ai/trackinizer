"""Tests for the bridge from a tool pipe's websocket to asyncio streams."""

from __future__ import annotations

from functools import partial
from http import HTTPStatus
from typing import TYPE_CHECKING, Final, cast

import asyncio
import contextlib

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosedError, InvalidHandshake
from websockets.frames import Close

import pytest

from trackinizer.client import tool_bridge
from trackinizer.client.tool_bridge import (
    ToolsRefusedError,
    bridged,
    dial_tools,
)
from trackinizer.sagent.agent.team import Team
from trackinizer.sagent.host import wire
from trackinizer.sagent.host.wire import Welcome, encoded
from trackinizer.sagent.providers.catalog import ModelCatalog
from trackinizer.sagent.tools.remote import Endpoint, Link
from trackinizer.server.config import Config
from trackinizer.wire.wire_tools import FRAME_BYTES


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from websockets.http11 import Request, Response


_SETTLE_SEC: Final = 2.0
_LIMIT: Final = 1 << 20
_MOST_WRITTEN: Final = 256 << 20
"""More than any kernel's socket buffers hold, which a stalled writer stops short of."""


class _FakeWebsocket:
    """A websocket whose incoming messages the test feeds and whose sends it gates."""

    def __init__(self, *, fail_sends: bool = False) -> None:
        self.sent: list[bytes] = []
        self.started_bytes = 0
        self.closed = False
        self.gate = asyncio.Event()
        self.gate.set()
        self.received = asyncio.Event()
        self.want_bytes = 0
        self._fail_sends = fail_sends
        self._incoming: asyncio.Queue[bytes | Exception | None] = asyncio.Queue()

    def feed(self, message: bytes | None) -> None:
        self._incoming.put_nowait(message)

    def feed_close(self, code: int | None, reason: str = "") -> None:
        """End the messages as a peer closing with ``code`` does; ``None`` is a drop."""
        frame = None if code is None else Close(code, reason)
        self._incoming.put_nowait(ConnectionClosedError(frame, None))

    async def send(self, message: bytes, /) -> None:
        self.started_bytes += len(message)
        await self.gate.wait()
        if self._fail_sends:
            raise ConnectionClosedError(Close(1006, ""), None)
        self.sent.append(message)
        if sum(map(len, self.sent)) >= self.want_bytes:
            self.received.set()

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self) -> AsyncIterator[str | bytes]:
        return self._messages()

    async def _messages(self) -> AsyncIterator[str | bytes]:
        while (message := await self._incoming.get()) is not None:
            if isinstance(message, Exception):
                raise message
            yield message


async def _settled(before: set[asyncio.Task[object]]) -> None:
    """Wait until every task the bridge started has finished."""
    pending = asyncio.all_tasks() - before - {asyncio.current_task()}
    async with asyncio.timeout(_SETTLE_SEC):
        await asyncio.gather(*pending, return_exceptions=True)
    assert not (asyncio.all_tasks() - before - {asyncio.current_task()})


async def _read_exactly(reader: asyncio.StreamReader, count: int) -> bytes:
    async with asyncio.timeout(_SETTLE_SEC):
        return await reader.readexactly(count)


@pytest.mark.asyncio
async def test_bytes_cross_both_ways_in_frames_of_at_most_65536() -> None:
    before = asyncio.all_tasks()
    fake = _FakeWebsocket()
    reader, writer = await bridged(fake, limit=_LIMIT)
    payload = bytes(range(256)) * 1_000

    fake.feed(b"hello ")
    fake.feed(b"world")
    assert await _read_exactly(reader, 11) == b"hello world"

    fake.want_bytes = len(payload)
    writer.write(payload)
    await writer.drain()
    async with asyncio.timeout(_SETTLE_SEC):
        await fake.received.wait()
    assert max(map(len, fake.sent)) <= FRAME_BYTES
    assert len(fake.sent) > 1
    assert b"".join(fake.sent) == payload

    writer.close()
    await writer.wait_closed()
    await _settled(before)


@pytest.mark.asyncio
async def test_a_line_past_the_limit_is_refused() -> None:
    socket = _FakeWebsocket()
    reader, writer = await bridged(socket, limit=8)
    socket.feed(b"x" * 64 + b"\n")
    with pytest.raises(ValueError, match="limit"):
        _ = await asyncio.wait_for(reader.readline(), _SETTLE_SEC)
    writer.close()
    socket.feed(None)


@pytest.mark.asyncio
async def test_a_closed_socket_ends_the_reader() -> None:
    before = asyncio.all_tasks()
    fake = _FakeWebsocket()
    reader, writer = await bridged(fake, limit=_LIMIT)

    fake.feed(b"last words")
    fake.feed(None)

    async with asyncio.timeout(_SETTLE_SEC):
        assert await reader.read() == b"last words"
    assert reader.at_eof()
    await _settled(before)
    assert fake.closed
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_a_close_with_a_code_is_a_refusal_after_the_bytes_sent() -> None:
    before = asyncio.all_tasks()
    fake = _FakeWebsocket()
    reader, writer = await bridged(fake, limit=_LIMIT)

    fake.feed(b"one\npart")
    fake.feed_close(4403, "update the host")

    async with asyncio.timeout(_SETTLE_SEC):
        assert await reader.readline() == b"one\n"
        assert await reader.readline() == b"part"
        with pytest.raises(ToolsRefusedError, match="4403: update the host") as refused:
            _ = await reader.readline()
    assert isinstance(refused.value, OSError)
    await _settled(before)
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [1000, 1001, None])
async def test_a_clean_or_codeless_close_is_a_plain_end_of_file(
    code: int | None,
) -> None:
    fake = _FakeWebsocket()
    reader, writer = await bridged(fake, limit=_LIMIT)

    fake.feed(b"bye\n")
    fake.feed_close(code)

    async with asyncio.timeout(_SETTLE_SEC):
        assert await reader.readline() == b"bye\n"
        assert await reader.readline() == b""
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_a_closed_writer_closes_the_socket() -> None:
    before = asyncio.all_tasks()
    fake = _FakeWebsocket()
    _, writer = await bridged(fake, limit=_LIMIT)

    writer.close()
    await writer.wait_closed()

    await _settled(before)
    assert fake.closed


@pytest.mark.asyncio
async def test_a_failing_send_ends_everything() -> None:
    before = asyncio.all_tasks()
    fake = _FakeWebsocket(fail_sends=True)
    reader, writer = await bridged(fake, limit=_LIMIT)

    writer.write(b"into the void")
    await writer.drain()

    async with asyncio.timeout(_SETTLE_SEC):
        assert await reader.read() == b""
    await _settled(before)
    assert fake.closed
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_a_socket_that_cannot_take_the_bytes_ends_the_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = asyncio.all_tasks()
    fake = _FakeWebsocket()
    reader, writer = await bridged(fake, limit=_LIMIT)

    async def broken(*_: object) -> None:
        raise BrokenPipeError

    monkeypatch.setattr(asyncio.get_running_loop(), "sock_sendall", broken)
    fake.feed(b"never delivered")

    async with asyncio.timeout(_SETTLE_SEC):
        assert await reader.read() == b""
    await _settled(before)
    assert fake.closed
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_the_agents_leg_reads_a_line_past_the_relays_limit_and_refuses_past_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket = _FakeWebsocket()

    async def dial(*args: object, **kwargs: object) -> _FakeWebsocket:
        del args, kwargs
        return socket

    monkeypatch.setattr(tool_bridge, "connect", dial)
    reader, writer = await dial_tools(
        "http://h",
        api_key="k",
        machine="m",
        asker="a@b.c",
        limit=wire.MAX_LINE_BYTES,
    )
    # A result larger than the relay's own cap, which it never reads.
    line = b"x" * (Config().tool_line_bytes + 1) + b"\n"
    socket.feed(line)
    assert await asyncio.wait_for(reader.readline(), _SETTLE_SEC * 10) == line
    writer.close()
    socket.feed(None)

    socket = _FakeWebsocket()
    reader, writer = await dial_tools(
        "http://h",
        api_key="k",
        machine="m",
        asker="a@b.c",
        limit=8,
    )
    socket.feed(b"x" * 64 + b"\n")
    with pytest.raises(ValueError, match="limit"):
        _ = await asyncio.wait_for(reader.readline(), _SETTLE_SEC)
    writer.close()
    socket.feed(None)


@pytest.mark.asyncio
async def test_a_server_url_with_no_host_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="hostname"):
        _ = await dial_tools(
            "http://:80",
            api_key="k",
            machine="m",
            asker="a@b.c",
            limit=wire.MAX_LINE_BYTES,
        )


@pytest.mark.asyncio
async def test_a_slow_socket_stalls_the_writer_without_a_growing_buffer() -> None:
    before = asyncio.all_tasks()
    fake = _FakeWebsocket()
    fake.gate.clear()
    _, writer = await bridged(fake, limit=_LIMIT)

    # However much a kernel's socket buffers hold, writing on fills them, and then a
    # drain waits. What the writer holds does not grow with each write: at most the one
    # it waits on, and what the kernel left of the write before it.
    chunk = b"x" * (1 << 20)
    stalled: asyncio.Task[None] | None = None
    for _ in range(_MOST_WRITTEN >> 20):
        writer.write(chunk)
        drained = asyncio.create_task(writer.drain())
        done, _ = await asyncio.wait({drained}, timeout=0.3)
        if not done:
            stalled = drained
            break
    assert stalled is not None, "the writer never stalled"
    assert writer.transport.get_write_buffer_size() < 2 * len(chunk)
    # The pump holds one frame in flight and reads no further.
    assert fake.started_bytes <= FRAME_BYTES

    fake.gate.set()
    async with asyncio.timeout(_SETTLE_SEC * 10):
        await stalled
    writer.close()
    await writer.wait_closed()
    await _settled(before)
    assert fake.closed


@pytest.mark.asyncio
async def test_dial_tools_reaches_a_real_server_and_refusal_is_an_oserror() -> None:
    before = asyncio.all_tasks()
    seen: list[tuple[str, str | None]] = []

    def refuse_bad_keys(
        connection: ServerConnection,
        request: Request,
    ) -> Response | None:
        seen.append((request.path, request.headers.get("Authorization")))
        if request.headers.get("Authorization") != "Bearer secret":
            return connection.respond(HTTPStatus.FORBIDDEN, "no\n")
        return None

    async def echo(connection: ServerConnection) -> None:
        async for message in connection:
            await connection.send(message)

    async with serve(
        echo,
        "127.0.0.1",
        0,
        process_request=refuse_bad_keys,
        max_size=None,
    ) as server:
        port = cast(int, server.sockets[0].getsockname()[1])
        url = f"http://127.0.0.1:{port}"
        reader, writer = await dial_tools(
            url,
            api_key="secret",
            machine="my box/1",
            asker="a+b@example.com",
            limit=wire.MAX_LINE_BYTES,
        )
        payload = b"z" * 300_000
        writer.write(payload)
        await writer.drain()
        assert await _read_exactly(reader, len(payload)) == payload
        writer.close()
        await writer.wait_closed()

        with pytest.raises(ToolsRefusedError, match="403") as refused:
            await dial_tools(
                url,
                api_key="wrong",
                machine="m",
                asker="x@example.com",
                limit=wire.MAX_LINE_BYTES,
            )
        assert isinstance(refused.value, OSError)
        with pytest.raises(ValueError, match=r"http://"):
            await dial_tools(
                "ws://x",
                api_key="k",
                machine="m",
                asker="x@y.z",
                limit=wire.MAX_LINE_BYTES,
            )

    assert seen[0] == (
        "/api/machines/my%20box%2F1/tools/ws?asker=a%2Bb%40example.com",
        "Bearer secret",
    )
    await _settled(before)


def _endpoint(url: str) -> Endpoint:
    return Endpoint(
        Link(
            dial=partial(
                dial_tools,
                url,
                api_key="k",
                machine="gx10",
                asker="ada@x.y",
                limit=wire.MAX_LINE_BYTES,
            ),
            token="",
            name="gx10",
            where="gx10",
        ),
        session_id="s1",
        client="here",
        team=Team(catalog=ModelCatalog(factories={})),
        retry_sec=0.05,
        ping_sec=20.0,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "reason"),
    [
        (4403, "gx10 serves no tools: update its host, or drop --no-exec"),
        (4403, "the server will not relay a line: Expecting value"),
        (4410, "the machine was revoked, or joined again"),
    ],
)
async def test_an_endpoint_reports_why_the_server_closed_it(
    code: int,
    reason: str,
) -> None:
    dials = 0

    async def refuse(connection: ServerConnection) -> None:
        nonlocal dials
        dials += 1
        await connection.close(code, reason)

    async with serve(refuse, "127.0.0.1", 0) as server:
        port = cast(int, server.sockets[0].getsockname()[1])
        endpoint = _endpoint(f"http://127.0.0.1:{port}")
        task = asyncio.create_task(endpoint.run())
        try:
            async with asyncio.timeout(_SETTLE_SEC):
                await endpoint.until(lambda e: e.tried and dials >= 3)
            assert str(code) in endpoint.error
            assert reason in endpoint.error
            with pytest.raises(ValueError, match=reason):
                _ = await endpoint.call(agent="a", window="w", tool="t", arguments={})
        finally:
            _ = task.cancel()
            _ = await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_an_endpoint_redials_a_refusing_server_and_recovers() -> None:
    refusing = True
    dials = 0

    async def serve_one(connection: ServerConnection) -> None:
        nonlocal dials
        dials += 1
        if refusing:
            await connection.close(4403, "serves no tools")
            return
        _ = await connection.recv()  # The Hello.
        await connection.send(
            encoded(Welcome(name="gx10", resumed=False, calls=(), tools={})),
        )
        with contextlib.suppress(Exception):
            async for _message in connection:
                pass

    async with serve(serve_one, "127.0.0.1", 0) as server:
        port = cast(int, server.sockets[0].getsockname()[1])
        endpoint = _endpoint(f"http://127.0.0.1:{port}")
        task = asyncio.create_task(endpoint.run())
        try:
            async with asyncio.timeout(_SETTLE_SEC):
                await endpoint.until(lambda e: dials >= 2 and "4403" in e.error)
            refusing = False
            async with asyncio.timeout(_SETTLE_SEC):
                await endpoint.until(lambda e: e.up)
            assert endpoint.error == ""
        finally:
            _ = task.cancel()
            _ = await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_dial_tools_dials_what_its_arguments_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialed: list[tuple[str, dict[str, object]]] = []

    async def refuse(url: str, **kwargs: object) -> None:
        dialed.append((url, kwargs))
        raise InvalidHandshake("no upgrade")

    monkeypatch.setattr(tool_bridge, "connect", refuse)
    with pytest.raises(
        ToolsRefusedError,
        match=r"^tools websocket refused: no upgrade$",
    ):
        # A host that spells ``http`` again, and ends as the strip does not.
        await dial_tools(
            "https://http.boX/",
            api_key="k",
            machine="my box/1",
            asker="a+b@c.d",
            limit=wire.MAX_LINE_BYTES,
        )
    assert dialed == [
        (
            "wss://http.boX/api/machines/my%20box%2F1/tools/ws?asker=a%2Bb%40c.d",
            {
                "additional_headers": {"Authorization": "Bearer k"},
                "max_size": None,
                "ping_interval": 20,
                "close_timeout": 1,
            },
        ),
    ]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
