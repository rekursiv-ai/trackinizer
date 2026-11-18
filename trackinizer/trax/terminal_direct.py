"""The host's half of a direct link: where a terminal's output goes, and from whom its input comes.

A terminal starts on the server's relay. The browser may offer a WebRTC data channel
over the relay (``SIGNAL`` frames); when it is up the browser says so on the channel
(``ROUTE``) and output goes direct, so a keystroke's echo no longer crosses the server.
This module owns that choice for one terminal:

- The host, not the server, numbers the output. It keeps the last
  :data:`~trackinizer.wire.wire_terminals.RING_BYTES` so either path can resume
  from an offset, and a browser that asks for the relay again (its channel dropped, or a
  new window attached) is replayed from there.
- While output goes direct the server sees none of it, so the router sends the server a
  ``KEEPALIVE`` every few seconds saying how long ago input was last seen.
- Input is applied from either path, in the order it arrives on that path. The browser
  fences the switch so the two paths never interleave.

The WebRTC library is behind :class:`DirectPeer`: nothing here imports it, and a host
without one still answers an offer, with ``abandon``, so the browser stays on the relay.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import asyncio
import contextlib
import functools
import logging
import time

from trackinizer.lib.posix.host import Frame, decode_frame, decode_winsize, encode_frame
from trackinizer.lib.posix.ring import Ring
from trackinizer.wire import wire_terminals
from trackinizer.wire.wire_terminals import DirectSignal


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


__all__ = ["DirectPeer", "DirectRouter", "PeerFactory", "Shell"]

_logger = logging.getLogger(__name__)


class DirectPeer(Protocol):
    """One WebRTC peer connection that answers the browser's offer."""

    @property
    def is_open(self) -> bool:
        """Whether the data channel is open."""
        ...

    async def accept(
        self,
        offer_sdp: str,
        *,
        ice_servers: tuple[str, ...],
        on_message: Callable[[bytes], None],
        on_close: Callable[[], None],
    ) -> str:
        """Take the browser's offer and return the answer, once candidates are gathered.

        Args:
          offer_sdp: The browser's session description.
          ice_servers: STUN URLs; none means host candidates only.
          on_message: Called with each binary message the channel receives.
          on_close: Called once when the channel or connection ends.

        Returns:
          sdp: The answer's session description.

        """
        ...

    def send(self, data: bytes) -> None:
        """Send one message on the channel."""
        ...

    async def close(self) -> None:
        """Close the connection; closing a closed one is a no-op."""
        ...


class Shell(Protocol):
    """The part of the pty a router drives."""

    async def write(self, data: bytes) -> bool:
        """Send keystrokes to the shell; False once it is gone."""
        ...

    def set_winsize(self, rows: int, cols: int) -> None:
        """Resize the pty."""
        ...


type PeerFactory = Callable[[], DirectPeer]


class DirectRouter:
    """Sends one terminal's output down the relay or the data channel, and takes its input.

    Args:
      relay: Sends one encoded frame to the server.
      shell: The pty the input is written to.
      factory: Makes a peer for an offer; ``None`` when no WebRTC library is installed.
      ice_servers: The STUN URLs the server named for this terminal.
      clock: Monotonic seconds.
      sleep: Waits the given seconds; only the keepalive uses it.

    """

    def __init__(
        self,
        *,
        relay: Callable[[bytes], Awaitable[None]],
        shell: Shell,
        factory: PeerFactory | None,
        ice_servers: tuple[str, ...] = (),
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._relay = relay
        self._shell = shell
        self._factory = factory
        self._ice_servers = ice_servers
        self._clock = clock
        self._sleep = sleep
        self._ring = Ring(capacity=wire_terminals.RING_BYTES)
        self._route = wire_terminals.ROUTE_RELAY
        self._sent = 0
        """Output up to here has gone to the browser by the current route."""

        self._send_lock = asyncio.Lock()
        self._peer: DirectPeer | None = None
        self._gen = 0
        self._fenced = False
        """The browser's relay fence came before it switched the route."""

        self._last_input = clock()
        self._window_start = 0.0
        self._window_bytes = 0
        self._inbox: asyncio.Queue[tuple[DirectPeer, bytes]] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._keepalive: asyncio.Task[None] | None = None
        self._answering: set[asyncio.Task[None]] = set()

    async def output(self, chunk: bytes) -> None:
        """Take what the shell wrote and send it by the current route.

        Args:
          chunk: Bytes the shell wrote, unmodified.

        """
        self._ring.append(chunk)
        if self._route == wire_terminals.ROUTE_RELAY:
            await self._flush_relay()
        else:
            self._flush_direct()

    async def from_relay(self, kind: Frame, payload: bytes) -> None:
        """Take one frame the server forwarded from the browser.

        Args:
          kind: What the frame carries; ``STOP`` is the caller's.
          payload: Its bytes.

        """
        if kind is Frame.INPUT:
            await self._input(payload)
        elif kind is Frame.RESIZE:
            self._resize(payload)
        elif kind is Frame.SIGNAL:
            await self._signal(payload)
        elif kind is Frame.ROUTE:
            await self._fence_or_return(payload)

    async def exit(self, status: int) -> None:
        """Tell the browser the shell exited, on the channel and on the relay.

        Args:
          status: The shell's exit status.

        """
        frame = encode_frame(Frame.EXIT, status.to_bytes(4))
        if self._peer is not None and self._peer.is_open:
            self._peer.send(frame)
        await self._relay(frame)

    async def close(self) -> None:
        """Stop every task and close the peer."""
        tasks = [self._worker, self._keepalive, *self._answering]
        for task in tasks:
            if task is not None:
                _ = task.cancel()
        for task in tasks:
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await self._drop_peer()

    async def _flush_relay(self) -> None:
        async with self._send_lock:
            if self._route != wire_terminals.ROUTE_RELAY:
                return
            data, _, _ = self._ring.since(self._sent)
            self._sent = self._ring.end
            if data:
                await self._relay(encode_frame(Frame.OUTPUT, data))

    def _flush_direct(self) -> None:
        peer = self._peer
        if peer is None or not peer.is_open:
            return
        data, _, _ = self._ring.since(self._sent)
        self._sent = self._ring.end
        self._send_output(peer, data)

    def _send_output(self, peer: DirectPeer, data: bytes) -> None:
        step = wire_terminals.DIRECT_FRAME_BYTES
        for at in range(0, len(data), step):
            peer.send(encode_frame(Frame.OUTPUT, data[at : at + step]))

    async def _input(self, data: bytes) -> None:
        self._last_input = self._clock()
        _ = await self._shell.write(data)

    def _resize(self, payload: bytes) -> None:
        try:
            rows, cols = decode_winsize(payload)
        except ValueError:
            return
        self._shell.set_winsize(
            min(max(rows, 1), wire_terminals.MAX_ROWS),
            min(max(cols, 1), wire_terminals.MAX_COLS),
        )

    async def _signal(self, payload: bytes) -> None:
        try:
            signal = DirectSignal.model_validate_json(payload)
        except ValueError:
            return
        if signal.type != "offer" or signal.gen <= self._gen:
            return
        self._gen = signal.gen
        if self._factory is None:
            await self._signal_out(DirectSignal(gen=signal.gen, type="abandon"))
            return
        await self._drop_peer()
        peer = self._factory()
        self._peer = peer
        if self._worker is None:
            self._worker = asyncio.create_task(self._work())
        task = asyncio.create_task(self._answer(peer, signal))
        self._answering.add(task)
        task.add_done_callback(self._answering.discard)

    async def _answer(self, peer: DirectPeer, signal: DirectSignal) -> None:
        try:
            sdp = await peer.accept(
                signal.sdp,
                ice_servers=self._ice_servers,
                on_message=functools.partial(self._on_message, peer),
                on_close=functools.partial(self._on_close, peer),
            )
        except Exception:
            _logger.warning("direct link: could not answer an offer", exc_info=True)
            if self._peer is peer:
                self._peer = None
                await self._signal_out(DirectSignal(gen=signal.gen, type="abandon"))
            await peer.close()
            return
        if self._peer is peer:
            await self._signal_out(DirectSignal(gen=signal.gen, type="answer", sdp=sdp))
        else:
            await peer.close()

    async def _signal_out(self, signal: DirectSignal) -> None:
        await self._relay(
            encode_frame(Frame.SIGNAL, signal.model_dump_json().encode()),
        )

    def _on_message(self, peer: DirectPeer, data: bytes) -> None:
        if peer is self._peer:
            self._inbox.put_nowait((peer, data))

    def _on_close(self, peer: DirectPeer) -> None:
        if peer is self._peer:
            self._peer = None

    async def _work(self) -> None:
        """Apply the channel's frames one at a time, in the order they arrived."""
        while True:
            peer, data = await self._inbox.get()
            if peer is not self._peer:
                continue
            try:
                kind, payload = decode_frame(data)
            except ValueError:
                await self._drop_peer()
                continue
            await self._from_peer(kind, payload)

    async def _from_peer(self, kind: Frame, payload: bytes) -> None:
        if kind is Frame.ROUTE:
            try:
                route, offset = wire_terminals.decode_route(payload)
            except ValueError:
                return
            if route == wire_terminals.ROUTE_DIRECT:
                self._switch_to_direct(offset)
        elif kind is Frame.INPUT:
            if self._within_rate(len(payload)):
                await self._input(payload)
            else:
                await self._drop_peer()
        elif kind is Frame.RESIZE:
            self._resize(payload)

    def _within_rate(self, size: int) -> bool:
        now = self._clock()
        if now - self._window_start >= 1.0:
            self._window_start = now
            self._window_bytes = 0
        self._window_bytes += size
        return self._window_bytes <= wire_terminals.MAX_INPUT_BYTES_PER_SEC

    def _switch_to_direct(self, offset: int) -> None:
        """Send the ring from ``offset`` on the channel, then every later byte."""
        peer = self._peer
        if peer is None or not peer.is_open:
            return
        data, resume, _ = self._ring.since(offset)
        self._route = wire_terminals.ROUTE_DIRECT
        self._sent = self._ring.end
        peer.send(encode_frame(Frame.ACK, resume.to_bytes(8)))
        self._send_output(peer, data)
        if self._fenced:
            self._fenced = False
            peer.send(self._fence())
        if self._keepalive is None or self._keepalive.done():
            self._keepalive = asyncio.create_task(self._report_idle())

    def _fence(self) -> bytes:
        route = wire_terminals.encode_route(
            wire_terminals.ROUTE_DIRECT,
            self._ring.end,
        )
        return encode_frame(Frame.ROUTE, route)

    async def _fence_or_return(self, payload: bytes) -> None:
        """Take the browser's ``ROUTE`` over the relay: its input fence, or a return."""
        try:
            route, offset = wire_terminals.decode_route(payload)
        except ValueError:
            return
        if route == wire_terminals.ROUTE_RELAY:
            await self._return_to_relay(offset)
        elif self._route == wire_terminals.ROUTE_DIRECT and self._peer is not None:
            if self._peer.is_open:
                self._peer.send(self._fence())
        else:
            self._fenced = True

    async def _return_to_relay(self, offset: int) -> None:
        """Replay the ring from ``offset`` on the relay and send output there again."""
        async with self._send_lock:
            self._gen = 0
            self._fenced = False
            await self._drop_peer()
            if self._route != wire_terminals.ROUTE_DIRECT:
                return
            self._route = wire_terminals.ROUTE_RELAY
            data, resume, _ = self._ring.since(offset)
            self._sent = self._ring.end
            await self._relay(encode_frame(Frame.ACK, resume.to_bytes(8)))
            if data:
                await self._relay(encode_frame(Frame.OUTPUT, data))

    async def _drop_peer(self) -> None:
        peer, self._peer = self._peer, None
        if peer is not None:
            await peer.close()

    async def _report_idle(self) -> None:
        """Tell the server how long input has been idle, while output goes direct."""
        while self._route == wire_terminals.ROUTE_DIRECT:
            idle_ms = int((self._clock() - self._last_input) * 1_000)
            await self._relay(
                encode_frame(Frame.KEEPALIVE, wire_terminals.encode_keepalive(idle_ms)),
            )
            await self._sleep(wire_terminals.KEEPALIVE_SEC)
