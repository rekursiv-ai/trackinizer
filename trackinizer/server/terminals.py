"""The relay that pairs a browser with a host's daemon and relays a terminal's bytes.

A terminal lives only while its sockets do, so its state is in memory, in this one
process, like the inbound queue's (``server/inbound.py``). The relay never touches the
database or a real socket: the routes hand it :class:`Socket` adapters and an
:class:`Observer`, and the clock and sleep are injected, so every limit is tested
without waiting for it.

A terminal is ``pending`` from the open until its daemon dials in. The daemon learns of
it from :meth:`TerminalRelay.wait`, a held request that returns the moment an open is
queued. Output the daemon sends goes to a ring (so a browser that drops can re-attach
from its last offset) and to the attached browser. Nothing is stored: a terminal's
bytes live in the ring only as long as the terminal does. Every send is awaited, so a
slow browser slows the daemon's reads rather than growing memory.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol
from uuid import UUID, uuid4

import asyncio
import logging
import secrets
import time

from trackinizer.lib.posix.host import (
    Frame,
    decode_winsize,
    encode_frame,
    encode_winsize,
)
from trackinizer.lib.posix.ring import Ring
from trackinizer.wire import wire_terminals


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


__all__ = [
    "Opening",
    "Socket",
    "SocketClosedError",
    "Terminal",
    "TerminalLimitError",
    "TerminalRelay",
    "decode_attach",
    "sweep_loop",
]

_logger = logging.getLogger(__name__)


class SocketClosedError(Exception):
    """A send found its peer gone; the relay treats that peer as detached."""


class TerminalLimitError(Exception):
    """An open would pass the open rate."""


class Socket(Protocol):
    """One WebSocket socket, as the relay uses it."""

    async def send_bytes(self, data: bytes) -> None:
        """Send one binary message; raise :class:`SocketClosedError` if the peer left."""
        ...

    async def close(self, code: int) -> None:
        """Close with a code; closing a closed socket is a no-op."""
        ...


class _Rate:
    """Bytes allowed per second; :meth:`delay` says how long to wait to stay under."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._start = 0.0
        self._used = 0

    def delay(self, size: int, *, now: float) -> float:
        """Count ``size`` bytes at ``now`` and return the seconds to wait, or 0."""
        if now - self._start >= 1.0:
            self._start = now
            self._used = 0
        self._used += size
        return 0.0 if self._used <= self._limit else self._start + 1.0 - now


@dataclass(slots=True, kw_only=True, eq=False)
class Terminal:
    """One terminal: who opened it, where it runs, and its two sockets."""

    id: UUID
    user: str
    host_id: UUID
    host: str
    rows: int
    cols: int
    opened_at: float
    canvas: UUID | None = None
    workspace: UUID | None = None
    requested_by: str | None = None
    surface: str | None = None
    command: wire_terminals.TerminalCommand | None = None
    """The CLI the host runs, as a detached agent run."""

    attach: UUID | None = None
    """The AgentSession the detached run re-opens."""

    run_name: str | None = None
    """The detached run's name; set whenever ``command`` is."""

    cwd: str | None = None
    """The folder the run starts in, as the browser named it; the host resolves it."""

    kind: wire_terminals.TerminalKind = "shell"
    """A shell, or the daemon's answers to file requests."""

    detached_at: float | None = None
    """When no browser was attached since; ``None`` while one is."""

    last_input_at: float = 0.0
    offered: bool = False
    """Whether a daemon's wait already returned this open."""

    daemon: Socket | None = None
    """The daemon's socket, once it dialled in."""

    starting: bool = False
    """The daemon has not yet confirmed receiving its startup acknowledgement."""

    browser: Socket | None = None
    ended: bool = False
    stopping: bool = False
    """Whether the shell was told to stop; the canvas no longer lists it."""

    replaying: asyncio.Event | None = None
    """Set when the current replay finishes; the daemon waits rather than buffering."""

    pre_input: bytearray = field(default_factory=bytearray)
    """Keystrokes typed before the daemon dialled in, up to one frame's worth."""

    pre_resize: bytes | None = None
    """The latest geometry sent before the daemon dialled in."""

    daemon_sending: asyncio.Lock = field(default_factory=asyncio.Lock)
    """Serialize buffered input with typing that arrives while startup flushes it."""

    ring: Ring = field(default_factory=lambda: Ring(capacity=wire_terminals.RING_BYTES))
    direct_until: float = 0.0
    """Until when the daemon's last ``KEEPALIVE`` says output goes to the browser direct."""

    signals: deque[float] = field(default_factory=deque)
    """When the browser's recent ``SIGNAL`` frames came, for the rate limit."""

    input_rate: _Rate = field(
        default_factory=lambda: _Rate(wire_terminals.MAX_INPUT_BYTES_PER_SEC),
    )
    output_rate: _Rate = field(
        default_factory=lambda: _Rate(wire_terminals.MAX_OUTPUT_BYTES_PER_SEC),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class Opening:
    """What a terminal is opened with; the relay adds its id, clock and sockets."""

    user: str
    host_id: UUID
    host: str
    rows: int
    cols: int
    canvas: UUID | None = None
    workspace: UUID | None = None
    requested_by: str | None = None
    surface: str | None = None
    command: wire_terminals.TerminalCommand | None = None
    attach: UUID | None = None
    run_name: str | None = None
    cwd: str | None = None
    kind: wire_terminals.TerminalKind = "shell"


def decode_attach(payload: bytes) -> tuple[str, int]:
    """Return the ticket and the offset an ``ATTACH`` payload carries.

    Args:
      payload: The frame's bytes.

    Returns:
      ticket: The attach ticket.
      offset: The stream offset the browser has reached.

    Raises:
      ValueError: The payload is not a ticket and a u64 offset.

    """
    if len(payload) != wire_terminals.TICKET_CHARS + 8:
        raise ValueError(f"ATTACH payload of {len(payload)} bytes")
    return (
        payload[: wire_terminals.TICKET_CHARS].decode("ascii"),
        int.from_bytes(payload[wire_terminals.TICKET_CHARS :]),
    )


class TerminalRelay:
    """Every live terminal in this process, and the rules that bound them.

    Args:
      alive: Whether a terminal's host credential is live and its owner active;
        asked by the sweep.
      clock: Monotonic seconds.
      sleep: Waits the given seconds; only the rate limits use it.
      ice_servers: STUN URLs handed to the browser and the daemon for a direct link.

    """

    def __init__(
        self,
        *,
        alive: Callable[[Terminal], Awaitable[bool]],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        ice_servers: tuple[str, ...] = (),
    ) -> None:
        self._alive = alive
        self._clock = clock
        self._sleep = sleep
        self.ice_servers = ice_servers
        """The STUN URLs the open, the attach and the daemon's wait return."""
        self._terminals: dict[UUID, Terminal] = {}
        self._pending: dict[UUID, deque[Terminal]] = {}
        self._waiters: dict[UUID, list[asyncio.Future[None]]] = {}
        self._waited: dict[UUID, float] = {}
        self._tickets: dict[str, _Ticket] = {}
        self._opens: dict[str, deque[float]] = {}
        self._started = clock()
        self._active: dict[UUID, float] = {}

    def ready(self, host_id: UUID) -> bool:
        """Return whether the daemon waited for an open recently enough."""
        waited = self._waited.get(host_id)
        return (
            waited is not None
            and self._clock() - waited <= wire_terminals.WAIT_LEASE_SEC
        )

    # Held in memory only, as every terminal is: after a restart a host's clock starts
    # at the restart, so an idle rule built on it acts at most that much late.
    def idle_sec(self, host_id: UUID) -> float:
        """Return how long the host's terminals have had no input or output.

        Input the browser sent, output the daemon relayed, and what a direct link's
        keepalive reports of either count. A host with none since this relay
        started is counted from its start.

        Args:
          host_id: The host.

        Returns:
          idle_sec: Seconds on the relay's clock.

        """
        return self._clock() - self._active.get(host_id, self._started)

    def terminal(self, terminal_id: UUID) -> Terminal | None:
        """Return the live terminal with this id, or ``None``."""
        return self._terminals.get(terminal_id)

    def live_in(self, user: str, *, canvas: UUID) -> list[Terminal]:
        """Return the user's live terminals opened from this canvas, oldest first.

        Args:
          user: The opener's email.
          canvas: The canvas.

        Returns:
          terminals: Those not told to stop.

        """
        return [
            term
            for term in self._terminals.values()
            if term.user == user and term.canvas == canvas and not term.stopping
        ]

    def attached_to(self, session_id: UUID) -> list[Terminal]:
        """Return the live sessions that re-attach this session, oldest first.

        Args:
          session_id: The AgentSession a detached run re-opens.

        Returns:
          terminals: Those opened with it as their ``attach`` and not told to stop.

        """
        return [
            term
            for term in self._terminals.values()
            if term.attach == session_id and not term.stopping
        ]

    def check(self, *, user: str) -> None:
        """Refuse an open past the open rate.

        Args:
          user: The opener's email.

        Raises:
          TerminalLimitError: The user opened too many terminals in the last minute.

        """
        opens = self._opens.get(user, deque())
        now = self._clock()
        if (
            sum(now - at < 60.0 for at in opens)
            >= wire_terminals.MAX_OPENS_PER_MIN_PER_USER
        ):
            raise TerminalLimitError

    def open(self, opening: Opening) -> Terminal:
        """Queue a terminal for the daemon, which a held wait returns at once.

        Args:
          opening: What the terminal is opened with.

        Returns:
          terminal: The queued terminal.

        Raises:
          TerminalLimitError: The open rate is reached.

        """
        self.check(user=opening.user)
        now = self._clock()
        user = opening.user
        host_id = opening.host_id
        term_id = uuid4()
        run_name = opening.run_name
        if opening.command is not None and run_name is None:
            run_name = (
                f"run-{term_id.hex[:8]}"
                if opening.attach is None
                else f"chat-{opening.attach.hex[:8]}"
            )
        term = Terminal(
            id=term_id,
            user=user,
            host_id=host_id,
            host=opening.host,
            rows=opening.rows,
            cols=opening.cols,
            opened_at=now,
            canvas=opening.canvas,
            workspace=opening.workspace,
            requested_by=opening.requested_by,
            surface=opening.surface,
            command=opening.command,
            attach=opening.attach,
            run_name=run_name,
            cwd=opening.cwd,
            kind=opening.kind,
            detached_at=now,
            last_input_at=now,
        )
        self._terminals[term.id] = term
        self._pending.setdefault(host_id, deque()).append(term)
        opens = self._opens.setdefault(user, deque())
        while opens and now - opens[0] >= 60.0:
            opens.popleft()
        opens.append(now)
        for waiter in self._waiters.get(host_id, []):
            if not waiter.done():
                waiter.set_result(None)
                break
        return term

    def mint_ticket(self, term: Terminal) -> str:
        """Return a one-use ticket for ``term``'s opener, good for 30 seconds.

        Args:
          term: The terminal to attach to.

        Returns:
          ticket: 256 random bits, url-safe.

        """
        now = self._clock()
        self._tickets = {
            key: ticket for key, ticket in self._tickets.items() if ticket.expires > now
        }
        token = secrets.token_urlsafe(32)
        self._tickets[token] = _Ticket(
            user=term.user,
            terminal_id=term.id,
            expires=now + wire_terminals.TICKET_TTL_SEC,
        )
        return token

    def redeem(self, token: str, *, user: str, terminal_id: UUID) -> bool:
        """Spend a ticket; return whether it was live and bound to user and terminal.

        Args:
          token: The ticket the browser presented.
          user: The cookie user presenting it.
          terminal_id: The terminal it must be for.

        Returns:
          valid: Whether the ticket was live, unspent and bound to both.

        """
        ticket = self._tickets.pop(token, None)
        return (
            ticket is not None
            and ticket.expires > self._clock()
            and ticket.user == user
            and ticket.terminal_id == terminal_id
        )

    async def wait(self, host_id: UUID, *, wait_sec: float) -> Terminal | None:
        """Return the host's next open, holding up to ``wait_sec`` for one.

        Args:
          host_id: The host whose daemon is asking.
          wait_sec: How long to hold.

        Returns:
          terminal: The next open, or ``None`` when none came in time.

        """
        loop = asyncio.get_running_loop()
        deadline = self._clock() + wait_sec
        self._waited[host_id] = self._clock()
        try:
            while True:
                if (term := self._take(host_id)) is not None:
                    return term
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return None
                waiter = loop.create_future()
                self._waiters.setdefault(host_id, []).append(waiter)
                try:
                    await asyncio.wait_for(waiter, timeout=remaining)
                except TimeoutError:
                    return None
                finally:
                    self._waiters[host_id].remove(waiter)
        finally:
            self._waited[host_id] = self._clock()

    async def attach_browser(
        self,
        term: Terminal,
        socket: Socket,
        *,
        offset: int,
    ) -> None:
        """Make ``socket`` the terminal's browser, closing any other with 4001.

        The browser first receives the lost-output notice if its offset fell out of the
        ring, then ``ACK`` with the offset the stream resumes at, then the ring's bytes
        from there.

        Args:
          term: The terminal.
          socket: The browser's socket.
          offset: The stream offset the browser has reached.

        """
        # Publish the replay gate before yielding: the daemon must not append to the
        # ring or send live frames until this snapshot has reached the browser.
        previous, term.browser = term.browser, socket
        term.detached_at = None
        data, resume, lost = term.ring.since(offset)
        # Each browser starts its offer generations at one. Reset the daemon's
        # negotiation even when its previous browser never reached direct mode.
        relay_from = term.ring.end
        term.direct_until = 0.0
        replayed = asyncio.Event()
        term.replaying = replayed
        try:
            if previous is not None:
                await self._close(previous, wire_terminals.CLOSE_TAKEN_OVER)
            await self._to_daemon(
                term,
                Frame.ROUTE,
                wire_terminals.encode_route(wire_terminals.ROUTE_RELAY, relay_from),
            )
            if lost:
                await self._to_browser(
                    term,
                    socket,
                    Frame.OUTPUT,
                    b"\r\n--- output lost ---\r\n",
                )
            await self._to_browser(term, socket, Frame.ACK, resume.to_bytes(8))
            if data:
                await self._to_browser(term, socket, Frame.OUTPUT, data)
        finally:
            if term.replaying is replayed:
                term.replaying = None
            replayed.set()

    def detach_browser(self, term: Terminal, socket: Socket) -> None:
        """Note that ``socket`` left; a taken-over browser's departure changes nothing."""
        if term.browser is socket:
            term.browser = None
            term.detached_at = self._clock()

    async def daemon_connected(
        self,
        term: Terminal,
        socket: Socket,
        *,
        ready: bool = False,
    ) -> bool:
        """Claim the terminal for its daemon, handing it what was typed before it came.

        Args:
          term: The terminal.
          socket: The daemon's socket.
          ready: Acknowledge startup before handing over buffered input.

        Returns:
          claimed: False when another daemon already holds the terminal or it ended.

        """
        if term.ended or (term.daemon is not None and not term.starting):
            return False
        previous = term.daemon
        term.daemon = socket
        term.starting = ready
        if previous is not None:
            await self._close(previous, wire_terminals.CLOSE_TAKEN_OVER)
        if ready:
            try:
                await socket.send_bytes(encode_frame(Frame.ACK))
            except SocketClosedError:
                if term.daemon is socket:
                    term.daemon = None
                    term.starting = False
                return False
            return True
        await self._start_daemon(term)
        return True

    async def daemon_lost(self, term: Terminal, socket: Socket) -> None:
        """End the terminal when its daemon's socket dropped.

        Args:
          term: The terminal.
          socket: The daemon socket that dropped.

        """
        if term.daemon is socket and not term.ended:
            if term.starting:
                term.daemon = None
                term.starting = False
            else:
                await self.end(term, code=wire_terminals.CLOSE_DAEMON_LOST)

    async def from_daemon(self, term: Terminal, kind: Frame, payload: bytes) -> None:
        """Take one frame from the daemon: output, the shell's exit, or a link frame.

        Args:
          term: The terminal.
          kind: What the frame carries.
          payload: Its bytes.

        """
        if term.id not in self._terminals:
            return
        if kind is Frame.ACK and payload == b"" and term.starting:
            await self._start_daemon(term)
            return
        if kind is Frame.KEEPALIVE:
            self._keepalive(term, payload)
            return
        if kind is Frame.OUTPUT:
            delay = term.output_rate.delay(len(payload), now=self._clock())
            if delay:
                await self._sleep(delay)
        while term.replaying is not None and not term.ended:
            await term.replaying.wait()
        if term.ended:
            return
        if kind is Frame.OUTPUT:
            self._touch(term.host_id, at=self._clock())
            term.ring.append(payload)
            await self._live(term, Frame.OUTPUT, payload)
        elif kind is Frame.EXIT:
            await self._live(term, Frame.EXIT, payload)
            await self.end(term, code=wire_terminals.CLOSE_ENDED)
        elif kind is Frame.SIGNAL:
            await self._live(term, Frame.SIGNAL, payload)
        elif kind is Frame.ACK and len(payload) == 8:
            # The daemon resumes on the relay at an offset the ring never saw: output went
            # direct meanwhile. Later offsets stay contiguous from here.
            term.ring.reset(int.from_bytes(payload))
            await self._live(term, Frame.ACK, payload)

    async def from_browser(self, term: Terminal, kind: Frame, payload: bytes) -> bool:
        """Take one frame from the browser; return False when it broke a rule.

        The caller closes a browser that returned False.

        Args:
          term: The terminal.
          kind: What the frame carries.
          payload: Its bytes.

        Returns:
          kept: Whether the frame was valid.

        """
        if kind is Frame.INPUT:
            if len(payload) > wire_terminals.MAX_INPUT_FRAME_BYTES:
                return False
            delay = term.input_rate.delay(len(payload), now=self._clock())
            if delay:
                await self._sleep(delay)
            term.last_input_at = self._clock()
            self._touch(term.host_id, at=term.last_input_at)
            await self._to_daemon(term, Frame.INPUT, payload)
        elif kind is Frame.RESIZE:
            try:
                rows, cols = decode_winsize(payload)
            except ValueError:
                return False
            await self._to_daemon(
                term,
                Frame.RESIZE,
                encode_winsize(
                    min(max(rows, 1), wire_terminals.MAX_ROWS),
                    min(max(cols, 1), wire_terminals.MAX_COLS),
                ),
            )
        elif kind is Frame.SIGNAL:
            if not self._signal_allowed(term, size=len(payload)):
                return False
            await self._to_daemon(term, Frame.SIGNAL, payload)
        elif kind is Frame.ROUTE:
            try:
                _ = wire_terminals.decode_route(payload)
            except ValueError:
                return False
            await self._to_daemon(term, Frame.ROUTE, payload)
        elif kind is Frame.STOP:
            await self.stop(term)
        else:
            return False
        return True

    async def stop(self, term: Terminal) -> None:
        """Tell the shell to stop; the daemon's ``EXIT`` then ends the terminal."""
        term.stopping = True
        if term.daemon is None or term.starting:
            await self.end(term, code=wire_terminals.CLOSE_ENDED)
        else:
            await self._to_daemon(term, Frame.STOP, b"")

    async def end(self, term: Terminal, *, code: int) -> None:
        """End the terminal once: close both sockets with ``code``.

        Args:
          term: The terminal.
          code: The close code both sockets receive.

        """
        if term.ended:
            return
        term.ended = True
        if term.replaying is not None:
            term.replaying.set()
        self._terminals.pop(term.id, None)
        # A peer that vanished makes its close raise, which `_close` absorbs, so the
        # other socket is still closed.
        for socket in (term.daemon, term.browser):
            if socket is not None:
                await self._close(socket, code)

    async def close_host(self, host_id: UUID) -> None:
        """End every terminal on a revoked host with 4410."""
        for term in [t for t in self._terminals.values() if t.host_id == host_id]:
            await self.end(term, code=wire_terminals.CLOSE_HOST_REVOKED)

    async def sweep(self) -> None:
        """End terminals past a limit: untaken, detached, idle, old, or orphaned.

        One terminal that fails, or one failing liveness question, never keeps the
        rest from being looked at.
        """
        now = self._clock()
        for term in list(self._terminals.values()):
            try:
                code = self._expired(term, now=now)
                if code is None and not await self._still_alive(term):
                    code = wire_terminals.CLOSE_HOST_REVOKED
                if code is not None:
                    await self._to_daemon(term, Frame.STOP, b"")
                    await self.end(term, code=code)
            except Exception:
                _logger.warning("terminal %s: sweep failed", term.id, exc_info=True)

    def _signal_allowed(self, term: Terminal, *, size: int) -> bool:
        """Count a ``SIGNAL`` from the browser; False when it is too big or too many."""
        now = self._clock()
        while term.signals and now - term.signals[0] >= 60.0:
            _ = term.signals.popleft()
        if (
            size > wire_terminals.MAX_SIGNAL_BYTES
            or len(term.signals) >= wire_terminals.MAX_SIGNALS_PER_MIN
        ):
            return False
        term.signals.append(now)
        return True

    # A daemon from before output idle time reports input alone.
    def _keepalive(self, term: Terminal, payload: bytes) -> None:
        """Take a direct link's report of how long ago input and output last were."""
        try:
            idle_ms, output_idle_ms = wire_terminals.decode_keepalive(payload)
        except ValueError:
            return
        now = self._clock()
        term.last_input_at = now - idle_ms / 1_000
        term.direct_until = now + 3 * wire_terminals.KEEPALIVE_SEC
        quiet_ms = idle_ms if output_idle_ms is None else min(idle_ms, output_idle_ms)
        self._touch(term.host_id, at=now - quiet_ms / 1_000)

    def _touch(self, host_id: UUID, *, at: float) -> None:
        """Record activity on the host at ``at``; an older report moves nothing."""
        self._active[host_id] = max(at, self._active.get(host_id, at))

    async def _still_alive(self, term: Terminal) -> bool:
        """Ask whether the credential and owner stand; a failed question says yes."""
        try:
            return await self._alive(term)
        except Exception:
            _logger.warning("terminal %s: liveness unknown", term.id, exc_info=True)
            return True

    async def _close(self, socket: Socket, code: int) -> None:
        """Close a socket; a peer that is already gone is not an error."""
        try:
            await socket.close(code)
        except Exception:
            _logger.debug("close of a vanished peer", exc_info=True)

    async def _live(self, term: Terminal, kind: Frame, payload: bytes) -> None:
        """Send a live frame once the daemon has waited for any replay."""
        if term.browser is not None:
            await self._to_browser(term, term.browser, kind, payload)

    def _expired(self, term: Terminal, *, now: float) -> int | None:
        """Return the close code for a terminal past a time limit, else ``None``."""
        if (
            term.daemon is None or term.starting
        ) and now - term.opened_at > wire_terminals.OPEN_TTL_SEC:
            return wire_terminals.CLOSE_DAEMON_LOST
        if now - term.opened_at > wire_terminals.LIFETIME_SEC:
            return wire_terminals.CLOSE_LIFETIME
        if now - term.last_input_at > wire_terminals.IDLE_SEC:
            return wire_terminals.CLOSE_IDLE
        if (
            term.detached_at is not None
            and now - term.detached_at > wire_terminals.DETACH_GRACE_SEC
        ):
            return wire_terminals.CLOSE_ENDED
        return None

    def _take(self, host_id: UUID) -> Terminal | None:
        """Pop the host's oldest open that is still pending and unoffered."""
        queue = self._pending.get(host_id)
        if queue is None:
            return None
        while queue:
            term = queue.popleft()
            if not term.ended and not term.offered:
                term.offered = True
                return term
        return None

    async def _to_daemon(self, term: Terminal, kind: Frame, payload: bytes) -> None:
        """Send a frame to the daemon; a vanished daemon ends the terminal."""
        async with term.daemon_sending:
            if term.daemon is None or term.starting:
                if kind is Frame.RESIZE:
                    term.pre_resize = payload
                elif (
                    kind is Frame.INPUT
                    and len(term.pre_input) + len(payload)
                    <= wire_terminals.MAX_INPUT_FRAME_BYTES
                ):
                    term.pre_input.extend(payload)
                return
            await self._send_to_daemon(term, kind, payload)

    async def _start_daemon(self, term: Terminal) -> None:
        """Flush startup input before allowing subsequent frames to reach the daemon."""
        async with term.daemon_sending:
            term.starting = False
            resize, term.pre_resize = term.pre_resize, None
            typed = bytes(term.pre_input)
            term.pre_input.clear()
            if resize is not None:
                await self._send_to_daemon(term, Frame.RESIZE, resize)
            if typed:
                await self._send_to_daemon(term, Frame.INPUT, typed)

    async def _send_to_daemon(
        self,
        term: Terminal,
        kind: Frame,
        payload: bytes,
    ) -> None:
        if term.daemon is None or term.ended:
            return
        try:
            await term.daemon.send_bytes(encode_frame(kind, payload))
        except SocketClosedError:
            await self.end(term, code=wire_terminals.CLOSE_DAEMON_LOST)

    async def _to_browser(
        self,
        term: Terminal,
        socket: Socket,
        kind: Frame,
        payload: bytes,
    ) -> None:
        """Send a frame to a browser; a vanished one only detaches."""
        try:
            await socket.send_bytes(encode_frame(kind, payload))
        except SocketClosedError:
            self.detach_browser(term, socket)


class _Swept(Protocol):
    """A relay with limits to enforce on a timer."""

    async def sweep(self) -> None:
        """End what has passed a limit."""
        ...


async def sweep_loop(
    relay: _Swept,
    *,
    interval_sec: float = wire_terminals.SWEEP_SEC,
) -> None:
    """Sweep the relay, one pass per interval.

    Args:
      relay: The relay to sweep.
      interval_sec: Seconds between passes.

    """
    while True:
        await asyncio.sleep(interval_sec)
        try:
            await relay.sweep()
        except Exception:
            _logger.exception("relay sweep failed; retrying next cycle")


@dataclass(frozen=True, slots=True, kw_only=True)
class _Ticket:
    user: str
    terminal_id: UUID
    expires: float
