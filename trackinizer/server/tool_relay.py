"""The relay that pairs an agent with a daemon over a tool connection for sagent's tools.

sagent already does reconnect, resume, acks, held results, grants and cancel end to end,
so a tool connection is thin. It has no ring, no buffer and no reattach, and it never takes a direct
link. Either socket dropping ends it, and sagent reconnects with a new tool connection.

An agent calls :meth:`ToolRelay.join`, which queues an offer for the daemon. The
daemon holds :meth:`ToolRelay.wait`, is handed the offer at once, and dials back to
:meth:`ToolRelay.attach` within the relay's ``dial_back_sec``. Each socket's route then runs its ``serve``
method until the tool connection ends. A frame is read from a socket only once the frame before it was
sent on, so a slow reader slows its sender and nothing piles up here. The daemon's frames
are relayed as they came. A copy is read for whether the last ended a line, as a line
``Inbound`` answers the agent with must not land inside one of the daemon's, and for the
calls its results answer.

A tool connection can last as long as sagent keeps it, so what let it open is asked again: each
:meth:`ToolRelay.sweep` ends a tool connection whose rule no longer holds.

The relay keeps whether each host has a call running, and when it last had a call or a
result (:meth:`ToolRelay.idle_sec`). A call is running from when the route lets it
through (:meth:`ToolRelay.called`) until the daemon's ``Result`` for it passes, which the
relay reads with :class:`~trackinizer.server.tool_lines.ResultIds`, or the agent
acks it (:meth:`ToolRelay.acked`), or its tool connection ends, or ``call_sec`` has gone by. A ping
or a tool connection held open with no call marks nothing.

A tool connection ends with one close code and a reason saying what happened, sent on both sockets,
and logs one line saying so: which socket or rule ended it, the code, the reason, the daemon
instance that attached and how long it lasted. Each offer handed to a daemon logs a line
too. The relay never touches the database, and the clock and sleep are
injected, so every limit is tested without waiting for it.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Protocol, override
from uuid import UUID, uuid4

import asyncio
import contextlib
import logging
import time

from trackinizer.server.terminals import Socket, SocketClosedError
from trackinizer.server.tool_lines import ResultIds
from trackinizer.wire import wire_terminals, wire_tools
from trackinizer.wire.wire_tools import ToolOffer


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine


__all__ = [
    "Inbound",
    "ToolConnection",
    "ToolConnectionBusyError",
    "ToolConnectionEndError",
    "ToolConnectionRevokedError",
    "ToolRelay",
    "ToolSocket",
    "close_reason",
]

_logger = logging.getLogger(__name__)

type Inbound = Callable[[ToolConnection, bytes], Awaitable[tuple[bytes, bytes]]]
"""Takes the tool connection and what its agent sent, and returns what goes on:
``(to_daemon, to_agent)``.

It may raise :class:`ToolConnectionEndError` to refuse the tool connection saying why; any other error ends
it as failed."""

type _Read = Callable[[], Awaitable[bytes | None]]
"""Reads a socket's next frame; ``None`` when its peer left."""


class ToolConnectionBusyError(Exception):
    """The host already holds as many tool connections as it may: ``limit``."""

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"the host already holds its limit of {limit} tool connections",
        )
        self.limit = limit


class ToolConnectionEndError(Exception):
    """Raised by an :data:`Inbound` to refuse the tool connection saying why, passing nothing on."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.code = wire_terminals.CLOSE_REFUSED
        self.reason = reason


class ToolConnectionRevokedError(Exception):
    """The daemon was revoked, or joined again, while its daemon waited."""


class ToolSocket(Socket, Protocol):
    """A socket of a tool connection, which closes saying why."""

    @override
    async def close(self, code: int, reason: str = "") -> None:
        """Close with ``code`` and ``reason``; closing a closed socket is a no-op."""
        ...


@dataclass(slots=True, kw_only=True, eq=False)
class ToolConnection:
    """One agent, and the daemon that dials back for it."""

    id: UUID
    host_id: UUID
    agent: ToolSocket
    asker: str
    """The person the assistant opened this tool connection for."""

    api_key_id: UUID
    """The assistant's key that opened it."""

    inbound: Inbound
    opened: float
    """When it opened, on the relay's clock."""

    results: ResultIds
    """Reads which calls the daemon's results answer."""

    running: dict[str, float] = field(default_factory=dict[str, float])
    """The calls let through whose result has not passed yet: when each was, by id."""

    instance: UUID | None = None
    """The daemon instance that dialed back; ``None`` until one has."""

    attached: asyncio.Future[ToolSocket] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future(),
    )
    """The daemon's socket, once it has dialed back."""

    ended: asyncio.Future[tuple[int, str]] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future(),
    )
    """The code and reason the tool connection ended with, which each socket is closed with."""

    agent_turn: asyncio.Condition = field(default_factory=asyncio.Condition)
    """Taken to send to the agent; signalled when the daemon's frames end a line."""

    daemon_mid_line: bool = False
    """Whether the daemon's last frame stopped inside a line."""


class ToolRelay:
    """Every tool connection waiting on or relaying through a host in this process, and its limits.

    Args:
      alive: Whether the whole rule that let a tool connection open still holds; asked by the sweep.
      connections_per_host: The most tool connections one host holds at once.
      dial_back_sec: How long an offered tool connection waits for its daemon to dial back.
      line_bytes: The longest daemon line read for which call it answers; a longer
        result is known by the agent's ack of it.
      call_sec: The longest one call keeps its host in use, as one never answered
        would forever: an hour, six times sagent's longest ``Bash`` timeout.
      clock: Monotonic seconds.
      sleep: Waits the given seconds; every deadline in the relay runs on it.

    """

    def __init__(
        self,
        *,
        alive: Callable[[ToolConnection], Awaitable[bool]],
        connections_per_host: int,
        dial_back_sec: float,
        line_bytes: int,
        call_sec: float = 3_600.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._alive = alive
        self._connections_per_host = connections_per_host
        self._dial_back_sec = dial_back_sec
        self._line_bytes = line_bytes
        self._call_sec = call_sec
        self._clock = clock
        self._sleep = sleep
        self._connections: dict[UUID, ToolConnection] = {}
        self._pending: dict[UUID, deque[ToolConnection]] = {}
        self._waiters: dict[UUID, list[asyncio.Future[bool]]] = {}
        self._started = clock()
        self._used: dict[UUID, float] = {}

    def called(self, connection: ToolConnection, call_id: str) -> None:
        """Record a call let through to the tool connection's host: it runs until answered.

        Args:
          connection: The tool connection it came on.
          call_id: The call's ``id``.

        """
        connection.running[call_id] = self._clock()
        self._touch(connection.host_id)

    def acked(self, connection: ToolConnection, call_id: str) -> None:
        """Record that the agent holds call ``call_id``'s result: it no longer runs.

        Args:
          connection: The tool connection the ack came on.
          call_id: The call's ``id``.

        """
        if connection.running.pop(call_id, None) is not None:
            self._touch(connection.host_id)

    # Held in memory only, as every tool connection is: after a restart a host's clock starts at
    # the restart, so an idle rule built on it acts at most that much late.
    def idle_sec(self, host_id: UUID) -> float:
        """Return how long the host has had no call running, no call and no result.

        Args:
          host_id: The host.

        Returns:
          idle_sec: Zero while a call runs on it, for ``call_sec`` at most; else
            seconds on the relay's clock since its last call or result, or a call's
            ``call_sec`` ran out, or since the relay started for a host with none.

        """
        now = self._clock()
        last = self._used.get(host_id, self._started)
        for connection in self._connections.values():
            if connection.host_id != host_id:
                continue
            for since in connection.running.values():
                if now - since < self._call_sec:
                    return 0.0
                last = max(last, since + self._call_sec)
        return now - last

    def join(
        self,
        host_id: UUID,
        *,
        agent: ToolSocket,
        asker: str,
        api_key_id: UUID,
        inbound: Inbound,
    ) -> ToolConnection:
        """Queue an offer of a new tool connection for the daemon.

        The caller goes straight on to :meth:`serve_agent`, which holds the slot this
        takes and gives it back.

        Args:
          host_id: The host the agent asks for.
          agent: The agent's socket.
          asker: The person the assistant opens it for.
          api_key_id: The assistant's key that opens it.
          inbound: Checks what the agent sends.

        Returns:
          connection: The new tool connection.

        Raises:
          ToolConnectionBusyError: The host holds its limit of tool connections.

        """
        if (
            sum(
                connection.host_id == host_id
                for connection in self._connections.values()
            )
            >= self._connections_per_host
        ):
            raise ToolConnectionBusyError(self._connections_per_host)
        connection = ToolConnection(
            id=uuid4(),
            host_id=host_id,
            agent=agent,
            asker=asker,
            api_key_id=api_key_id,
            inbound=inbound,
            opened=self._clock(),
            results=ResultIds(line_bytes=self._line_bytes),
        )
        self._connections[connection.id] = connection
        self._pending.setdefault(host_id, deque()).append(connection)
        # Every held wait is woken, as the first may have gone before it could take
        # the offer; those that lose it to another wait go back to waiting.
        for waiter in self._waiters.get(host_id, []):
            if not waiter.done():
                waiter.set_result(False)
        return connection

    # A daemon another has taken the host from may still claim an offer while it waits:
    # its dial-back is refused, as ``attach`` is told only a current instance, and the
    # agent's redial offers the tool connection again, at the cost of ``dial_back_sec``.
    async def wait(self, host_id: UUID, *, wait_sec: float) -> ToolOffer | None:
        """Return the host's next offer, holding up to ``wait_sec`` for one.

        Args:
          host_id: The host whose daemon is asking.
          wait_sec: How long to hold.

        Returns:
          offer: The oldest queued offer, or ``None`` when none came in time.

        Raises:
          ToolConnectionRevokedError: The host was revoked while its daemon waited.

        """
        deadline = self._clock() + wait_sec
        while True:
            if (found := self._take(host_id)) is not None:
                return found
            remaining = deadline - self._clock()
            if remaining <= 0:
                return None
            if not await self._park(host_id, seconds=remaining):
                return self._take(host_id)

    async def hold(self, host_id: UUID, *, wait_sec: float) -> None:
        """Hold a wait that takes nothing, for a daemon from before tool connections.

        Answering it at once would have that daemon ask again at once, in a loop.

        Args:
          host_id: The host whose daemon is asking.
          wait_sec: How long to hold.

        Raises:
          ToolConnectionRevokedError: The host was revoked while its daemon waited.

        """
        deadline = self._clock() + wait_sec
        while (remaining := deadline - self._clock()) > 0:
            _ = await self._park(host_id, seconds=remaining)

    def attach(
        self,
        host_id: UUID,
        connection_id: UUID,
        daemon: ToolSocket,
        *,
        instance: UUID,
    ) -> ToolConnection | None:
        """Give an offered tool connection its daemon's socket.

        Args:
          host_id: The host the daemon has authenticated as.
          connection_id: The tool connection, from the daemon's offer.
          daemon: The daemon's socket.
          instance: The daemon instance, already checked to be the host's current one.

        Returns:
          connection: The tool connection; ``None`` when it is unknown, is another host's, has ended,
            or has its daemon already. Its id is learned only from its offer.

        """
        connection = self._connections.get(connection_id)
        if (
            connection is None
            or connection.host_id != host_id
            or connection.attached.done()
        ):
            return None
        connection.instance = instance
        connection.attached.set_result(daemon)
        return connection

    async def serve_agent(self, connection: ToolConnection, read: _Read) -> None:
        """Relay the agent's frames until the tool connection ends, then close the agent's socket.

        Args:
          connection: The tool connection, from :meth:`join`.
          read: Reads the agent's next frame.

        """
        await self._serve(
            connection,
            connection.agent,
            ("agent", self._from_agent(connection, read)),
            ("dial-back", self._dial_back(connection)),
        )

    async def serve_daemon(self, connection: ToolConnection, read: _Read) -> None:
        """Relay the daemon's frames until the tool connection ends, then close the daemon's socket.

        Args:
          connection: The tool connection, from :meth:`attach`.
          read: Reads the daemon's next frame.

        """
        await self._serve(
            connection,
            connection.attached.result(),
            ("daemon", self._from_daemon(connection, read)),
        )

    def close_host(self, host_id: UUID) -> None:
        """End every tool connection and wait of a revoked host.

        Args:
          host_id: The host whose credential was revoked or replaced.

        """
        for connection in [
            p for p in self._connections.values() if p.host_id == host_id
        ]:
            self._end(
                connection,
                code=wire_terminals.CLOSE_HOST_REVOKED,
                reason="the host was revoked, or joined again",
                by="revoke",
            )
        for waiter in self._waiters.get(host_id, []):
            if not waiter.done():
                waiter.set_result(True)

    async def sweep(self) -> None:
        """End every tool connection whose rule no longer holds.

        It closes as a refused socket is, so the agent's reconnect hears the true answer. A
        failing question leaves that tool connection up and asks about the rest.
        """
        for connection in list(self._connections.values()):
            if not await self._still_alive(connection):
                self._end(
                    connection,
                    code=wire_terminals.CLOSE_REFUSED,
                    reason="no longer allowed: what opened this tool connection no longer holds",
                    by="sweep",
                )

    async def _still_alive(self, connection: ToolConnection) -> bool:
        """Ask whether the tool connection's rule still holds; a failed question says yes."""
        try:
            return await self._alive(connection)
        except Exception:
            _logger.warning(
                "tool connection %s: liveness unknown",
                connection.id,
                exc_info=True,
            )
            return True

    # Each work is named for the socket or the limit that ends the tool connection when it fails.
    async def _serve(
        self,
        connection: ToolConnection,
        socket: ToolSocket,
        *works: tuple[str, Coroutine[object, object, None]],
    ) -> None:
        """Run a socket's work until the tool connection ends, however it ends, then close the socket."""
        tasks = [
            asyncio.ensure_future(self._ending(connection, work, by=by))
            for by, work in works
        ]
        try:
            _ = await asyncio.wait({connection.ended})
        finally:
            for task in tasks:
                _ = task.cancel()
            _ = await asyncio.gather(*tasks, return_exceptions=True)
            # The route itself was cancelled, as at shutdown: the other socket still ends.
            self._end(
                connection,
                code=wire_terminals.CLOSE_ENDED,
                reason="the server is stopping",
                by="server",
            )
            await socket.close(*connection.ended.result())

    async def _ending(
        self,
        connection: ToolConnection,
        work: Coroutine[object, object, None],
        *,
        by: str,
    ) -> None:
        """Run one socket's work; what stops it for good also ends the tool connection."""
        try:
            await work
        except _SocketLostError as lost:
            self._end(connection, code=lost.code, reason=lost.reason, by=lost.by)
        except ToolConnectionEndError as refused:
            # The type only: what a line held is not logged.
            cause = refused.__cause__
            self._end(
                connection,
                code=refused.code,
                reason=refused.reason,
                by=by,
                cause="" if cause is None else type(cause).__name__,
            )
        except Exception as error:
            # A check that failed let nothing through, and must not let later lines by.
            _logger.exception("tool connection failed: connection=%s", connection.id)
            self._end(
                connection,
                code=wire_terminals.CLOSE_REFUSED,
                reason="the server could not go on relaying this tool connection",
                by="server",
                cause=type(error).__name__,
            )

    async def _from_agent(self, connection: ToolConnection, read: _Read) -> None:
        """Send the agent's frames on, until it leaves."""
        while (frame := await read()) is not None:
            # A tool connection holds no buffer, so what the agent sends first waits right here.
            _ = await asyncio.wait({connection.attached})
            to_daemon, to_agent = await connection.inbound(connection, frame)
            await _send(connection.attached.result(), to_daemon, lost=_DAEMON_LEFT)
            await _answer_agent(connection, to_agent)
        self._end(
            connection,
            code=_AGENT_LEFT[0],
            reason=_AGENT_LEFT[1],
            by=_AGENT_LEFT[2],
        )

    async def _from_daemon(self, connection: ToolConnection, read: _Read) -> None:
        """Send the daemon's frames on to the agent unread, until it leaves."""
        while (frame := await read()) is not None:
            if answered := connection.results.feed(frame):
                for call_id in answered:
                    _ = connection.running.pop(call_id, None)
                self._touch(connection.host_id)
            if frame:
                async with connection.agent_turn:
                    await _send(connection.agent, frame, lost=_AGENT_LEFT)
                    connection.daemon_mid_line = not frame.endswith(b"\n")
                    connection.agent_turn.notify_all()
        self._end(
            connection,
            code=_DAEMON_LEFT[0],
            reason=_DAEMON_LEFT[1],
            by=_DAEMON_LEFT[2],
        )

    async def _dial_back(self, connection: ToolConnection) -> None:
        """End a tool connection whose daemon does not dial back in time."""
        if not await self._until(connection.attached, seconds=self._dial_back_sec):
            _logger.warning(
                "tool connection dial-back timed out: connection=%s host_id=%s wait_sec=%g",
                connection.id,
                connection.host_id,
                self._dial_back_sec,
            )
            self._end(
                connection,
                code=wire_terminals.CLOSE_DAEMON_LOST,
                reason="the daemon did not dial back in time",
                by="dial-back",
            )

    # The first end stands, so each tool connection logs one line. ``by`` names the socket or the rule
    # that ended it: ``agent``, ``daemon``, ``dial-back``, ``sweep``, ``revoke`` or
    # ``server``; ``cause`` is the type of an error that did, never its text.
    def _end(
        self,
        connection: ToolConnection,
        *,
        code: int,
        reason: str,
        by: str,
        cause: str = "",
    ) -> None:
        """End a tool connection with ``code`` and ``reason``, and free its slot."""
        if connection.ended.done():
            return
        connection.ended.set_result((code, close_reason(reason)))
        _logger.info(
            "tool connection ended: connection=%s host_id=%s by=%s code=%d reason=%r "
            "cause=%s instance=%s duration_sec=%.3f",
            connection.id,
            connection.host_id,
            by,
            code,
            reason,
            cause or "-",
            connection.instance or "-",
            self._clock() - connection.opened,
        )
        del self._connections[connection.id]
        # Its running calls end with it, so the host's idle time starts now.
        if connection.running:
            connection.running.clear()
            self._touch(connection.host_id)
        if (queue := self._pending.get(connection.host_id)) is not None:
            with contextlib.suppress(ValueError):
                queue.remove(connection)
            if not queue:
                del self._pending[connection.host_id]

    def _touch(self, host_id: UUID) -> None:
        """Record that the host had a call or a result just now."""
        self._used[host_id] = self._clock()

    async def _park(self, host_id: UUID, *, seconds: float) -> bool:
        """Hold until the host has news or the time is up; return if it has news."""
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(host_id, []).append(waiter)
        try:
            woken = await self._until(waiter, seconds=seconds)
        finally:
            self._forget_waiter(host_id, waiter)
        if woken and waiter.result():
            raise ToolConnectionRevokedError
        return woken

    def _forget_waiter(self, host_id: UUID, waiter: asyncio.Future[bool]) -> None:
        """Drop a finished wait, and its host's list once it is empty."""
        waiters = self._waiters[host_id]
        waiters.remove(waiter)
        if not waiters:
            del self._waiters[host_id]

    def _take(self, host_id: UUID) -> ToolOffer | None:
        """Pop the host's oldest queued offer, if it still has one."""
        queue = self._pending.get(host_id)
        if queue is None:
            return None
        connection = queue.popleft()
        if not queue:
            del self._pending[host_id]
        _logger.info(
            "tool connection offered: connection=%s host_id=%s",
            connection.id,
            host_id,
        )
        return ToolOffer(connection_id=connection.id)

    async def _until[T](self, future: asyncio.Future[T], *, seconds: float) -> bool:
        """Wait for ``future`` or for ``seconds`` on the injected sleep; return if done."""
        timer = asyncio.ensure_future(self._sleep(seconds))
        try:
            _ = await asyncio.wait({future, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            _ = timer.cancel()
        return future.done()


def close_reason(text: str) -> str:
    """Return ``text`` cut to what a close frame carries, whole characters only.

    Args:
      text: What happened.

    Returns:
      reason: At most :data:`_REASON_BYTES` of it, in UTF-8.

    """
    return text.encode()[:_REASON_BYTES].decode(errors="ignore")


_REASON_BYTES: Final = 123
"""The most a websocket close frame's reason holds."""

_AGENT_LEFT: Final = (wire_terminals.CLOSE_ENDED, "the agent left", "agent")
_DAEMON_LEFT: Final = (wire_terminals.CLOSE_DAEMON_LOST, "the daemon left", "daemon")
"""How a socket's leaving ends the tool connection: the code, the reason, and the socket."""


async def _send(socket: Socket, data: bytes, *, lost: tuple[int, str, str]) -> None:
    """Send ``data`` in frames, each awaited; a peer gone ends the tool connection with ``lost``."""
    try:
        for start in range(0, len(data), wire_tools.FRAME_BYTES):
            await socket.send_bytes(data[start : start + wire_tools.FRAME_BYTES])
    except SocketClosedError as error:
        raise _SocketLostError(*lost) from error


async def _answer_agent(connection: ToolConnection, line: bytes) -> None:
    """Answer the agent between the daemon's lines, which go on arriving meanwhile."""
    if line:
        async with connection.agent_turn:
            _ = await connection.agent_turn.wait_for(
                lambda: not connection.daemon_mid_line,
            )
            await _send(connection.agent, line, lost=_AGENT_LEFT)


class _SocketLostError(Exception):
    """A send found its peer gone, which ends the tool connection with ``code`` and ``reason``."""

    def __init__(self, code: int, reason: str, by: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.by = by
