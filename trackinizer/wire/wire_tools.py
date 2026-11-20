"""Wire contract for tool connections: an assistant's tool calls on a connected computer.

The configured assistant reaches the tools of a computer its asker connected over a
tool connection the server pairs from two sockets:

- The agent opens a websocket on :data:`TOOLS_WS_PATH`.
- A mux daemon registers one outbound websocket at :data:`HOST_TOOLS_MUX_PATH`.
  Each agent then gets one logical stream with one frame of credit per direction.
- The server reads each line, logs each call, then relays the line between the sockets.

The old wait and dial-back paths remain in the SDK for callers targeting older servers;
new servers do not register them.

A socket is refused after its handshake, and a tool connection ends, with a close code of
``wire_terminals`` and a reason saying what happened: nothing acts on which refusal it
was, so they share one code. Frames are binary.

This package is part of the publishable client distribution, so it must
not import ``server`` / ``trax`` / fastapi (see ``import_purity_test``).
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Final
from uuid import UUID

import asyncio
import uuid

from pydantic import BaseModel, ConfigDict


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


__all__ = [
    "FRAME_BYTES",
    "HOST_TOOLS_MUX_PATH",
    "HOST_TOOLS_WAIT_PATH",
    "HOST_TOOLS_WS_PATH",
    "MUX_CALLS",
    "MUX_FRAME_BYTES",
    "MUX_STREAMS",
    "TOOLS_WS_PATH",
    "WAIT_MAX_SEC",
    "MuxStream",
    "ToolMux",
    "ToolOffer",
]

TOOLS_WS_PATH: Final = "/api/hosts/{name}/tools/ws"
"""``?asker=EMAIL``: the agent's socket of a tool connection."""

HOST_TOOLS_WAIT_PATH: Final = "/api/hosts/{host_id}/tools/wait"
"""``GET ?instance=ID``, held up to :data:`WAIT_MAX_SEC`: the daemon's next
:class:`ToolOffer`, or ``null`` when none came."""

HOST_TOOLS_WS_PATH: Final = "/api/hosts/{host_id}/tools/{connection_id}/ws"
"""``?instance=ID``: the daemon's socket of a tool connection, dialed back for an offer."""

WAIT_MAX_SEC: Final = 25
"""The longest the server holds a daemon's wait."""

FRAME_BYTES: Final = 65_536
"""The largest websocket frame a socket sends."""


class ToolOffer(BaseModel):
    """A waiting agent, as the daemon receives it."""

    # A field a newer server adds must not stop an older daemon, as the tool connection's own wire
    # reads past one too.
    model_config = ConfigDict(extra="ignore", frozen=True)

    connection_id: uuid.UUID
    """The id the daemon dials back under."""


# The old experimental mux accepted only 32 streams and disconnects on the 33rd.
HOST_TOOLS_MUX_PATH: Final = "/api/hosts/{host_id}/tool-mux/v1/ws"
HEADER_BYTES: Final = 17
MUX_FRAME_BYTES: Final = HEADER_BYTES + FRAME_BYTES
MUX_STREAMS: Final = 256
"""The hard stream limit understood by both ends of a daemon socket."""

MUX_CALLS: Final = 32

_OPEN: Final = b"O"
_DATA: Final = b"D"
_CREDIT: Final = b"A"
_CLOSE: Final = b"C"


class MuxStream:
    """One bidirectional stream; both the relay and the daemon adapt this to their socket API."""

    def __init__(self, mux: ToolMux, stream_id: UUID) -> None:
        self._mux = mux
        self.id = stream_id
        self._inbox: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=2)
        self._credit = asyncio.Event()
        self._credit.set()
        self._closed = asyncio.Event()
        self._write = asyncio.Lock()

    async def send(self, message: bytes, /) -> None:
        """Send one frame after the remote reader consumes its predecessor.

        Args:
          message: One binary payload no larger than the tool frame limit.

        """
        async with self._write:
            await self._credit.wait()
            if self._closed.is_set():
                raise ConnectionError("the tool stream closed")
            self._credit.clear()
            await self._mux.send(_DATA, self.id, message)

    async def read(self) -> bytes | None:
        """Return one frame and replenish its credit.

        Returns:
          data: The next binary payload, or None at EOF.

        """
        if self._closed.is_set() and self._inbox.empty():
            return None
        data = await self._inbox.get()
        if data is not None and not self._closed.is_set():
            await self._mux.send(_CREDIT, self.id)
        return data

    async def close(self) -> None:
        """Stop this stream without stopping the other streams."""
        if not self._closed.is_set():
            self.end()
            with suppress(OSError):
                await self._mux.send(_CLOSE, self.id)

    async def wait_closed(self) -> None:
        """Wait for either end to close the stream."""
        await self._closed.wait()

    def end(self) -> None:
        """Detach an active stream, preserving any pending frame ahead of EOF."""
        self._mux.forget(self.id)
        self._closed.set()
        self._credit.set()
        self._inbox.put_nowait(None)

    def receive(self, kind: bytes, data: bytes) -> None:
        """Apply one transport frame without blocking other streams.

        Args:
          kind: The validated data, credit, or close tag.
          data: The data payload, or empty bytes for control frames.

        """
        if kind == _DATA:
            if not self._inbox.empty():
                raise ValueError("data exceeds the tool stream's credit")
            self._inbox.put_nowait(data)
        elif kind == _CREDIT:
            self._credit.set()
        else:
            self.end()

    def __aiter__(self) -> MuxStream:
        return self

    async def __anext__(self) -> bytes:
        item = await self.read()
        if item is None:
            raise StopAsyncIteration
        return item


class ToolMux:
    """Demultiplex frames on one socket. Only Trackinizer initiates streams."""

    def __init__(self, send: Callable[[bytes], Awaitable[None]]) -> None:
        self._send = send
        self._write = asyncio.Lock()
        self._streams: dict[UUID, MuxStream] = {}
        self._reader: asyncio.Task[None] | None = None
        self._closed = False  # pragma: no mutate[truthiness]

    async def send(self, kind: bytes, stream_id: UUID, data: bytes = b"") -> None:
        """Write an envelope without concurrent writes to the physical socket."""
        async with self._write:
            await self._send_frame(kind, stream_id, data)

    async def _send_frame(
        self,
        kind: bytes,
        stream_id: UUID,
        data: bytes = b"",
    ) -> None:
        if self._closed:
            raise ConnectionError("the daemon tools socket closed")
        await self._send(kind + stream_id.bytes + data)

    def forget(self, stream_id: UUID) -> None:
        """Drop a closed stream."""
        self._streams.pop(stream_id, None)

    @property
    def has_capacity(self) -> bool:
        """Whether this socket can accept another stream."""
        return not self._closed and len(self._streams) < MUX_STREAMS

    def _add(self, stream_id: UUID) -> MuxStream:
        if stream_id in self._streams or not self.has_capacity:
            raise ValueError("duplicate or excess tool stream")
        stream = self._streams[stream_id] = MuxStream(self, stream_id)
        return stream

    async def open(self, stream_id: UUID) -> MuxStream:
        """Open the daemon end for one authorized agent socket.

        Args:
          stream_id: The id assigned by the relay.

        Returns:
          stream: A new transport stream.

        """
        stream = self._add(stream_id)
        try:
            async with self._write:
                await self._send_open(stream)
        except BaseException:
            if self._streams.get(stream_id) is stream:
                stream.end()
            raise
        return stream

    async def _send_open(self, stream: MuxStream) -> None:
        try:
            await self._send_frame(_OPEN, stream.id)
        except BaseException:
            # OPEN may have arrived; write CLOSE before releasing the lock.
            if self._streams.get(stream.id) is stream:
                with suppress(Exception):
                    await self._send_frame(_CLOSE, stream.id)
            raise

    def stop(self) -> None:
        """Fence the socket and wake all streams when the host leaves or is revoked."""
        self._closed = True
        for stream in tuple(self._streams.values()):
            stream.end()
        if self._reader is not None:
            self._reader.cancel()

    async def run(
        self,
        read: Callable[[], Awaitable[bytes | None]],
        *,
        accept: Callable[[MuxStream], None] | None = None,
    ) -> None:
        """Route frames until the outbound connection ends.

        Args:
          read: Reads the next frame, or returns None at EOF.
          accept: On the daemon, hands a newly opened stream to sagent's adapter.

        """
        if self._closed:
            return
        reader = self._reader = asyncio.create_task(self._read(read, accept))
        try:
            await asyncio.wait({reader})
            if not reader.cancelled():
                reader.result()
        finally:
            self.stop()
            await asyncio.gather(reader, return_exceptions=True)

    async def _read(
        self,
        read: Callable[[], Awaitable[bytes | None]],
        accept: Callable[[MuxStream], None] | None,
    ) -> None:
        while (frame := await read()) is not None:
            if len(frame) < HEADER_BYTES or len(frame) > MUX_FRAME_BYTES:
                raise ValueError("invalid daemon tool frame length")
            kind = frame[:1]
            stream_id = UUID(bytes=frame[1:HEADER_BYTES])
            data = frame[HEADER_BYTES:]
            if kind == _OPEN and accept is not None and not data:
                accept(self._add(stream_id))
            elif (kind == _DATA and data) or (kind in (_CREDIT, _CLOSE) and not data):
                if (stream := self._streams.get(stream_id)) is not None:
                    stream.receive(kind, data)
            else:
                raise ValueError("invalid daemon tool frame kind")
