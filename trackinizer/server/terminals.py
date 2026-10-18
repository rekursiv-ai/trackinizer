"""The hub that pairs a browser with a host and relays a terminal's bytes.

A terminal lives only while its sockets do, so its state is in memory, in this one
process, like the inbound queue's (``server/inbound.py``). The hub never touches the
database or a real socket: the routes hand it :class:`Socket` adapters and an
:class:`Observer`, and the clock and sleep are injected, so every limit is tested
without waiting for it.

A terminal is ``pending`` from the open until its host dials in. The host learns of
it from :meth:`TerminalHub.wait`, a held request that returns the moment an open is
queued. Output the host sends goes to a ring (so a browser that drops can re-attach
from its last offset), to the observer (the recording), and to the attached browser.
Every send is awaited, so a slow browser slows the host's reads rather than growing
memory.
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
from trackinizer.wire import wire_terminals


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


__all__ = [
    "Observer",
    "Opening",
    "Ring",
    "Socket",
    "SocketClosedError",
    "StationLiveError",
    "Terminal",
    "TerminalHub",
    "TerminalLimitError",
    "decode_attach",
    "sweep_loop",
]

_logger = logging.getLogger(__name__)


class SocketClosedError(Exception):
    """A send found its peer gone; the hub treats that peer as detached."""


class TerminalLimitError(Exception):
    """An open would pass one of the live-terminal caps or the open rate."""


class StationLiveError(Exception):
    """The caller already has a live terminal for the station."""

    def __init__(self, terminal: Terminal) -> None:
        super().__init__(str(terminal.id))
        self.terminal = terminal


class Socket(Protocol):
    """One WebSocket leg, as the hub uses it."""

    async def send_bytes(self, data: bytes) -> None:
        """Send one binary message; raise :class:`SocketClosedError` if the peer left."""
        ...

    async def close(self, code: int) -> None:
        """Close with a code; closing a closed socket is a no-op."""
        ...


class Observer(Protocol):
    """Hears a terminal's output and its end; the recorder."""

    async def output(self, terminal: Terminal, data: bytes) -> None:
        """Take bytes the shell wrote."""
        ...

    async def ended(self, terminal: Terminal, *, status: int | None) -> None:
        """Take the end of the terminal, once, with the shell's exit status if known."""
        ...


class Ring:
    """The last ``capacity`` bytes of a stream, addressed by absolute offset."""

    def __init__(self, capacity: int = wire_terminals.RING_BYTES) -> None:
        self._capacity = capacity
        self._data = bytearray()
        self.end = 0
        """Offset one past the last byte appended."""

    @property
    def start(self) -> int:
        """Offset of the oldest byte kept."""
        return self.end - len(self._data)

    def append(self, data: bytes) -> None:
        """Append ``data``, dropping the oldest bytes past the capacity."""
        self._data.extend(data)
        self.end += len(data)
        del self._data[: max(0, len(self._data) - self._capacity)]

    def since(self, offset: int) -> tuple[bytes, int, bool]:
        """Return the bytes from ``offset``, where they start, and whether some were lost.

        Args:
          offset: The stream position the caller has reached.

        Returns:
          data: The bytes from the later of ``offset`` and the ring's start.
          resume: The offset ``data`` starts at.
          lost: Whether ``offset`` had fallen out of the ring.

        """
        offset = min(offset, self.end)
        lost = offset < self.start
        offset = max(offset, self.start)
        return bytes(self._data[offset - self.start :]), offset, lost


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
    """One terminal: who opened it, where it runs, and its two legs."""

    id: UUID
    user: str
    machine_id: UUID
    machine: str
    session_id: UUID
    rows: int
    cols: int
    opened_at: float
    workspace: UUID | None = None
    workstream: UUID | None = None
    requested_by: str | None = None
    detached_at: float | None = None
    """When no browser was attached since; ``None`` while one is."""

    last_input_at: float = 0.0
    offered: bool = False
    """Whether a host's wait already returned this open."""

    host: Socket | None = None
    browser: Socket | None = None
    ended: bool = False
    replaying: list[tuple[Frame, bytes]] | None = None
    """Frames that arrived while a browser is being caught up; sent right after."""

    pre_input: bytearray = field(default_factory=bytearray)
    """Keystrokes typed before the host dialled in, up to one frame's worth."""

    pre_resize: bytes | None = None
    """The latest geometry sent before the host dialled in."""

    ring: Ring = field(default_factory=Ring)
    input_rate: _Rate = field(
        default_factory=lambda: _Rate(wire_terminals.MAX_INPUT_BYTES_PER_SEC),
    )
    output_rate: _Rate = field(
        default_factory=lambda: _Rate(wire_terminals.MAX_OUTPUT_BYTES_PER_SEC),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class Opening:
    """What a terminal is opened with; the hub adds its id, clock and legs."""

    user: str
    machine_id: UUID
    machine: str
    session_id: UUID
    rows: int
    cols: int
    workspace: UUID | None = None
    workstream: UUID | None = None
    requested_by: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class _Ticket:
    user: str
    terminal_id: UUID
    expires: float


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


class TerminalHub:
    """Every live terminal in this process, and the rules that bound them.

    Args:
      observer: Hears output and the end of each terminal.
      alive: Whether a terminal's machine credential is live and its owner active;
        asked by the sweep.
      clock: Monotonic seconds.
      sleep: Waits the given seconds; only the rate limits use it.

    """

    def __init__(
        self,
        *,
        observer: Observer,
        alive: Callable[[Terminal], Awaitable[bool]],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._observer = observer
        self._alive = alive
        self._clock = clock
        self._sleep = sleep
        self._terminals: dict[UUID, Terminal] = {}
        self._pending: dict[UUID, deque[Terminal]] = {}
        self._waiters: dict[UUID, list[asyncio.Future[None]]] = {}
        self._waited: dict[UUID, float] = {}
        self._tickets: dict[str, _Ticket] = {}
        self._opens: dict[str, deque[float]] = {}

    def ready(self, machine_id: UUID) -> bool:
        """Return whether the machine's host waited for an open recently enough."""
        waited = self._waited.get(machine_id)
        return (
            waited is not None
            and self._clock() - waited <= wire_terminals.WAIT_LEASE_SEC
        )

    def terminal(self, terminal_id: UUID) -> Terminal | None:
        """Return the live terminal with this id, or ``None``."""
        return self._terminals.get(terminal_id)

    def live_for(self, user: str, workspace: UUID) -> Terminal | None:
        """Return the user's live terminal opened from this station, or ``None``.

        Args:
          user: The opener's email.
          workspace: The station.

        Returns:
          terminal: The live terminal, if there is one.

        """
        return next(
            (
                term
                for term in self._terminals.values()
                if term.user == user and term.workspace == workspace
            ),
            None,
        )

    def check(self, *, user: str, machine_id: UUID, workspace: UUID | None) -> None:
        """Refuse an open that would pass a cap, the open rate, or a station's one.

        Args:
          user: The opener's email.
          machine_id: The machine to open on.
          workspace: The station the open is from.

        Raises:
          StationLiveError: The user has a live terminal for the station.
          TerminalLimitError: A cap or the open rate is reached.

        """
        if workspace is not None and (live := self.live_for(user, workspace)):
            raise StationLiveError(live)
        terms = self._terminals.values()
        opens = self._opens.get(user, deque())
        now = self._clock()
        if (
            sum(term.user == user for term in terms) >= wire_terminals.MAX_LIVE_PER_USER
            or sum(term.machine_id == machine_id for term in terms)
            >= wire_terminals.MAX_LIVE_PER_MACHINE
            or len(self._terminals) >= wire_terminals.MAX_LIVE_PER_SERVER
            or sum(now - at < 60.0 for at in opens)
            >= wire_terminals.MAX_OPENS_PER_MIN_PER_USER
        ):
            raise TerminalLimitError

    def open(self, opening: Opening) -> Terminal:
        """Queue a terminal for the machine's host, which a held wait returns at once.

        Args:
          opening: What the terminal is opened with.

        Returns:
          terminal: The queued terminal.

        Raises:
          StationLiveError: The user has a live terminal for the station.
          TerminalLimitError: A cap or the open rate is reached.

        """
        self.check(
            user=opening.user,
            machine_id=opening.machine_id,
            workspace=opening.workspace,
        )
        now = self._clock()
        user = opening.user
        machine_id = opening.machine_id
        term = Terminal(
            id=uuid4(),
            user=user,
            machine_id=machine_id,
            machine=opening.machine,
            session_id=opening.session_id,
            rows=opening.rows,
            cols=opening.cols,
            opened_at=now,
            workspace=opening.workspace,
            workstream=opening.workstream,
            requested_by=opening.requested_by,
            detached_at=now,
            last_input_at=now,
        )
        self._terminals[term.id] = term
        self._pending.setdefault(machine_id, deque()).append(term)
        opens = self._opens.setdefault(user, deque())
        while opens and now - opens[0] >= 60.0:
            opens.popleft()
        opens.append(now)
        for waiter in self._waiters.get(machine_id, []):
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

    async def wait(self, machine_id: UUID, *, wait_sec: float) -> Terminal | None:
        """Return the machine's next open, holding up to ``wait_sec`` for one.

        Args:
          machine_id: The machine whose host is asking.
          wait_sec: How long to hold.

        Returns:
          terminal: The next open, or ``None`` when none came in time.

        """
        loop = asyncio.get_running_loop()
        deadline = self._clock() + wait_sec
        self._waited[machine_id] = self._clock()
        try:
            while True:
                if (term := self._take(machine_id)) is not None:
                    return term
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return None
                waiter = loop.create_future()
                self._waiters.setdefault(machine_id, []).append(waiter)
                try:
                    await asyncio.wait_for(waiter, timeout=remaining)
                except TimeoutError:
                    return None
                finally:
                    self._waiters[machine_id].remove(waiter)
        finally:
            self._waited[machine_id] = self._clock()

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
        # The ring is read and the socket published with no await between, and output
        # that arrives while the replay goes out is queued behind it. A send yields, so
        # otherwise a chunk could reach the new socket before its ACK and again in the
        # replay.
        previous, term.browser = term.browser, socket
        term.detached_at = None
        data, resume, lost = term.ring.since(offset)
        backlog: list[tuple[Frame, bytes]] = []
        term.replaying = backlog
        try:
            if previous is not None:
                await self._close(previous, wire_terminals.CLOSE_TAKEN_OVER)
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
            while backlog:
                kind, payload = backlog.pop(0)
                await self._to_browser(term, socket, kind, payload)
        finally:
            if term.replaying is backlog:
                term.replaying = None

    def detach_browser(self, term: Terminal, socket: Socket) -> None:
        """Note that ``socket`` left; a taken-over browser's departure changes nothing."""
        if term.browser is socket:
            term.browser = None
            term.detached_at = self._clock()

    async def host_connected(self, term: Terminal, socket: Socket) -> bool:
        """Claim the terminal for its host, handing it what was typed before it came.

        Args:
          term: The terminal.
          socket: The host's socket.

        Returns:
          claimed: False when another host already holds the terminal or it ended.

        """
        if term.host is not None or term.ended:
            return False
        term.host = socket
        resize, term.pre_resize = term.pre_resize, None
        typed = bytes(term.pre_input)
        term.pre_input.clear()
        if resize is not None:
            await self._to_host(term, Frame.RESIZE, resize)
        if typed:
            await self._to_host(term, Frame.INPUT, typed)
        return True

    async def host_lost(
        self,
        term: Terminal,
        socket: Socket,
        *,
        code: int = wire_terminals.CLOSE_HOST_LOST,
    ) -> None:
        """End the terminal when its host's socket dropped.

        Args:
          term: The terminal.
          socket: The host socket that dropped.
          code: What the browser is told: 4004, or the host's own 4429 refusal.

        """
        if term.host is socket and not term.ended:
            await self.end(term, code=code)

    async def from_host(self, term: Terminal, kind: Frame, payload: bytes) -> None:
        """Take one frame from the host: output, or the shell's exit.

        Args:
          term: The terminal.
          kind: What the frame carries.
          payload: Its bytes.

        """
        if term.ended:
            return
        if kind is Frame.OUTPUT:
            delay = term.output_rate.delay(len(payload), now=self._clock())
            if delay:
                await self._sleep(delay)
            term.ring.append(payload)
            await self._observer.output(term, payload)
            await self._live(term, Frame.OUTPUT, payload)
        elif kind is Frame.EXIT:
            status = int.from_bytes(payload)
            await self._live(term, Frame.EXIT, payload)
            await self.end(term, code=wire_terminals.CLOSE_ENDED, status=status)

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
            await self._to_host(term, Frame.INPUT, payload)
        elif kind is Frame.RESIZE:
            try:
                rows, cols = decode_winsize(payload)
            except ValueError:
                return False
            await self._to_host(
                term,
                Frame.RESIZE,
                encode_winsize(
                    min(max(rows, 1), wire_terminals.MAX_ROWS),
                    min(max(cols, 1), wire_terminals.MAX_COLS),
                ),
            )
        elif kind is Frame.STOP:
            await self.stop(term)
        else:
            return False
        return True

    async def stop(self, term: Terminal) -> None:
        """Tell the shell to stop; the host's ``EXIT`` then ends the terminal."""
        if term.host is None:
            await self.end(term, code=wire_terminals.CLOSE_ENDED)
        else:
            await self._to_host(term, Frame.STOP, b"")

    async def end(
        self,
        term: Terminal,
        *,
        code: int,
        status: int | None = None,
    ) -> None:
        """End the terminal once: close both legs with ``code`` and tell the observer.

        Args:
          term: The terminal.
          code: The close code both legs receive.
          status: The shell's exit status, if it exited.

        """
        if term.ended:
            return
        term.ended = True
        self._terminals.pop(term.id, None)
        # Every step runs whatever the one before it did: a peer that vanished makes
        # its close raise, and the other leg and the recording must still be ended.
        for socket in (term.host, term.browser):
            if socket is not None:
                await self._close(socket, code)
        try:
            await self._observer.ended(term, status=status)
        except Exception:
            _logger.warning(
                "terminal %s: recording did not end",
                term.id,
                exc_info=True,
            )

    async def close_machine(self, machine_id: UUID) -> None:
        """End every terminal on a revoked machine with 4410."""
        for term in [t for t in self._terminals.values() if t.machine_id == machine_id]:
            try:
                await self.end(term, code=wire_terminals.CLOSE_MACHINE_REVOKED)
            except Exception:
                _logger.warning("terminal %s: could not end", term.id, exc_info=True)

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
                    code = wire_terminals.CLOSE_MACHINE_REVOKED
                if code is not None:
                    await self._to_host(term, Frame.STOP, b"")
                    await self.end(term, code=code)
            except Exception:
                _logger.warning("terminal %s: sweep failed", term.id, exc_info=True)

    async def _still_alive(self, term: Terminal) -> bool:
        """Ask whether the credential and owner stand; a failed question says yes."""
        try:
            return await self._alive(term)
        except Exception:
            _logger.warning("terminal %s: liveness unknown", term.id, exc_info=True)
            return True

    async def _close(self, socket: Socket, code: int) -> None:
        """Close a leg; a peer that is already gone is not an error."""
        try:
            await socket.close(code)
        except Exception:
            _logger.debug("close of a vanished peer", exc_info=True)

    async def _live(self, term: Terminal, kind: Frame, payload: bytes) -> None:
        """Send a frame to the attached browser, or queue it while it is caught up."""
        if term.replaying is not None:
            term.replaying.append((kind, payload))
        elif term.browser is not None:
            await self._to_browser(term, term.browser, kind, payload)

    def _expired(self, term: Terminal, *, now: float) -> int | None:
        """Return the close code for a terminal past a time limit, else ``None``."""
        if term.host is None and now - term.opened_at > wire_terminals.OPEN_TTL_SEC:
            return wire_terminals.CLOSE_HOST_LOST
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

    def _take(self, machine_id: UUID) -> Terminal | None:
        """Pop the machine's oldest open that is still pending and unoffered."""
        queue = self._pending.get(machine_id)
        if queue is None:
            return None
        while queue:
            term = queue.popleft()
            if not term.ended and not term.offered:
                term.offered = True
                return term
        return None

    async def _to_host(self, term: Terminal, kind: Frame, payload: bytes) -> None:
        """Send a frame to the host; a vanished host ends the terminal."""
        if term.host is None:
            # The host has not dialled in yet: keep the geometry and a frame of typing.
            if kind is Frame.RESIZE:
                term.pre_resize = payload
            elif (
                kind is Frame.INPUT
                and len(term.pre_input) + len(payload)
                <= wire_terminals.MAX_INPUT_FRAME_BYTES
            ):
                term.pre_input.extend(payload)
            return
        try:
            await term.host.send_bytes(encode_frame(kind, payload))
        except SocketClosedError:
            await self.end(term, code=wire_terminals.CLOSE_HOST_LOST)

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


async def sweep_loop(
    hub: TerminalHub,
    *,
    touch: Callable[[], Awaitable[None]],
    interval_sec: float = wire_terminals.SWEEP_SEC,
) -> None:
    """Keep the terminals' sessions alive and sweep the hub, one pass per interval.

    Args:
      hub: The hub to sweep.
      touch: Notes that the live terminals' sessions are still alive.
      interval_sec: Seconds between passes.

    """
    while True:
        await asyncio.sleep(interval_sec)
        # Each step has its own guard: a liveness write that always fails must not
        # switch off every timer and cap the sweep enforces.
        try:
            await touch()
        except Exception:
            _logger.exception("terminal liveness touch failed; retrying next cycle")
        try:
            await hub.sweep()
        except Exception:
            _logger.exception("terminal sweep failed; retrying next cycle")
