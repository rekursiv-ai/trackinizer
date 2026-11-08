"""Bridge a tool pipe's websocket to asyncio streams.

sagent's ``Link`` dials for a ``(StreamReader, StreamWriter)`` pair and its
``Server.adopt`` takes the same pair. :func:`bridged` makes that pair from a websocket
and :func:`dial_tools` opens the agent's leg of a tool pipe first, so both ends of the
pipe use one bridge.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Protocol, override
from urllib.parse import quote, urlencode

import asyncio
import contextlib
import socket

from websockets.asyncio.client import connect
from websockets.exceptions import (
    ConnectionClosed,
    InvalidHandshake,
    InvalidStatus,
    InvalidURI,
)

from trackinizer.wire.wire_tools import FRAME_BYTES, TOOLS_WS_PATH


if TYPE_CHECKING:
    from collections.abc import AsyncIterator


__all__ = [
    "Streams",
    "ToolsRefusedError",
    "Websocket",
    "bridged",
    "dial_tools",
]

type Streams = tuple[asyncio.StreamReader, asyncio.StreamWriter]
"""The pair sagent's ``Link`` dials for and ``Server.adopt`` takes."""


class Websocket(Protocol):
    """The part of a websocket the bridge uses."""

    async def send(self, message: bytes, /) -> None:
        """Send one binary frame."""
        ...

    async def close(self) -> None:
        """Close the connection."""
        ...

    def __aiter__(self) -> AsyncIterator[str | bytes]:
        """Yield each message until the peer closes."""
        ...


class ToolsRefusedError(ConnectionError):
    """The server did not accept the tools websocket, or ended it with a refusal.

    An ``OSError``, so that sagent's ``Endpoint.run`` redials and keeps its message as
    the endpoint's error.
    """


class _Reader(asyncio.StreamReader):
    """A reader that raises ``refusal``, once it has given out every byte, at the end."""

    def __init__(self, limit: int) -> None:
        super().__init__(limit=limit)
        self.refusal: ToolsRefusedError | None = None
        """Set before the end of the stream when the server closed with a code."""

    @override
    async def readline(self) -> bytes:
        line = await super().readline()
        if not line and self.refusal is not None:
            raise self.refusal
        return line


async def dial_tools(
    url: str,
    *,
    api_key: str,
    machine: str,
    asker: str,
    limit: int,
) -> Streams:
    """Open the agent's leg of ``machine``'s tool pipe, as streams.

    Args:
        url: The server's ``http://`` or ``https://`` base URL.
        api_key: Sent as a bearer token.
        machine: The machine whose tools to reach.
        asker: The email the pipe is opened for.
        limit: The longest line the reader takes: the agent's own, as a ``Read`` of a
            large image needs; the relay caps only the lines it reads, the agent's.

    Returns:
        streams: The pipe's reader and writer; they end when either side closes.

    Raises:
        ToolsRefusedError: The server refused the handshake; the message names the
            status.
        ValueError: ``url`` is not an ``http://`` or ``https://`` URL.

    """
    base = url.rstrip("/")
    if not base.startswith(("http://", "https://")):
        msg = f"server URL must start with http:// or https://: {url!r}"
        raise ValueError(msg)
    # Letters are always safe, so a mutant's ``safe`` of letters is this same one.
    path = TOOLS_WS_PATH.format(name=quote(machine, safe=""))  # pragma: no mutate
    try:
        socket_ = await connect(
            f"{base.replace('http', 'ws', 1)}{path}?{urlencode({'asker': asker})}",
            additional_headers={"Authorization": f"Bearer {api_key}"},
            max_size=None,
            ping_interval=20,
            close_timeout=1,
        )
    except InvalidURI as error:
        raise ValueError(str(error)) from error
    except InvalidStatus as error:
        status = error.response.status_code
        raise ToolsRefusedError(f"tools websocket refused: HTTP {status}") from error
    except InvalidHandshake as error:
        raise ToolsRefusedError(f"tools websocket refused: {error}") from error
    return await bridged(socket_, limit=limit)


async def bridged(websocket: Websocket, *, limit: int) -> Streams:
    """Return streams that carry bytes to and from ``websocket``.

    One end of a socket pair becomes the streams; the other is pumped to and from the
    websocket. Each send is awaited, so a slow websocket stalls the writer's
    ``drain``. When either side ends, both pumps stop, the websocket and the pair's
    far end close, and the reader sees end of file after the bytes already sent. The
    caller closes the writer, which owns the near end.

    A server that closes the socket with a code other than 1000 or 1001 has refused,
    so the reader raises :class:`ToolsRefusedError` naming the code and reason, in
    place of the end of file, after the bytes already sent. Without a code (a dropped
    connection) it is a plain end of file.

    Args:
        websocket: An open websocket to pump to and from.
        limit: The longest line the reader takes.

    Returns:
        streams: The reader and writer of the near end of a socket pair.

    """
    near, far = socket.socketpair()
    far.setblocking(False)
    loop = asyncio.get_running_loop()
    reader = _Reader(limit=limit)
    transport, protocol = await loop.create_connection(
        lambda: asyncio.StreamReaderProtocol(reader),
        sock=near,
    )
    writer = asyncio.StreamWriter(transport, protocol, reader, loop)
    task = asyncio.create_task(_run(websocket, far, reader))
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)
    return reader, writer


_CLEAN_CODES: Final = frozenset({1000, 1001})
"""The codes of a close that is no refusal."""

_RUNNING: Final[set[asyncio.Task[None]]] = set()


async def _run(websocket: Websocket, far: socket.socket, reader: _Reader) -> None:
    """Pump until either direction ends, then clean up both."""
    pumps = [
        asyncio.create_task(_into_websocket(websocket, far)),
        asyncio.create_task(_from_websocket(websocket, far, reader)),
    ]
    try:
        await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for pump in pumps:
            pump.cancel()
        await asyncio.gather(*pumps, return_exceptions=True)
        far.close()
        with contextlib.suppress(ConnectionClosed, OSError):
            await websocket.close()


async def _into_websocket(websocket: Websocket, far: socket.socket) -> None:
    """Send what the writer wrote, in frames of at most ``FRAME_BYTES``."""
    loop = asyncio.get_running_loop()
    with contextlib.suppress(ConnectionClosed, OSError):
        while chunk := await loop.sock_recv(far, FRAME_BYTES):
            await websocket.send(chunk)


async def _from_websocket(
    websocket: Websocket,
    far: socket.socket,
    reader: _Reader,
) -> None:
    """Hand each message to the reader; note a close with a code as a refusal."""
    loop = asyncio.get_running_loop()
    try:
        async for message in websocket:
            data = message.encode() if isinstance(message, str) else message
            await loop.sock_sendall(far, data)
    except ConnectionClosed as closed:
        # Set before the pump ends, so before the reader can see the end of file.
        if closed.rcvd is not None and closed.rcvd.code not in _CLEAN_CODES:
            reader.refusal = ToolsRefusedError(
                f"the server closed the tools pipe with {closed.rcvd.code}: "
                f"{closed.rcvd.reason}",
            )
    except OSError:
        return
