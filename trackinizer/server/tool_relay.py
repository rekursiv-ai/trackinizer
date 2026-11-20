"""Relay authenticated tool clients over one outbound WebSocket per daemon.

The first bytes passed by the line gate open one native daemon stream. It stays
attached for the lifetime of the agent socket, including idle periods. If the
host's stream budget is full at activation, the relay refuses the new socket
instead of waiting for room or closing an existing stream.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Protocol, override
from uuid import UUID, uuid4

import asyncio
import logging
import time

from trackinizer.server.config import (
    DEFAULT_TOOL_AGENT_SOCKETS,
    DEFAULT_TOOL_STREAMS,
)
from trackinizer.server.terminals import Socket, SocketClosedError
from trackinizer.server.tool_lines import ResultIds
from trackinizer.wire import wire_terminals, wire_tools
from trackinizer.wire.wire_tools import MuxStream, ToolMux


if TYPE_CHECKING:
    from collections.abc import Coroutine


__all__ = [
    "Inbound",
    "ToolConnection",
    "ToolConnectionBusyError",
    "ToolConnectionEndError",
    "ToolConnectionUnavailableError",
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


class ToolConnectionUnavailableError(Exception):
    """The host has no connected mux daemon."""


class ToolSocket(Socket, Protocol):
    """A socket of a tool connection, which closes saying why."""

    @override
    async def close(self, code: int, reason: str = "") -> None:
        """Close with ``code`` and ``reason``; closing a closed socket is a no-op."""
        ...


@dataclass(slots=True, kw_only=True, eq=False)
class ToolConnection:
    """An agent WebSocket and its optional live daemon stream."""

    id: UUID
    host_id: UUID
    agent: ToolSocket
    asker: str
    """The person the assistant opened this tool connection for."""

    api_key_id: UUID | None
    """The key that opened it; ``None`` for the asker's own browser, signed in."""

    inbound: Inbound
    opened: float
    """When it opened, on the relay's clock."""

    results: ResultIds
    """Reads which calls the daemon's results answer."""

    running: dict[str, float] = field(default_factory=dict[str, float])
    """Calls awaiting a result, by id; not an admission counter."""

    mux: tuple[UUID, ToolMux]
    reserved: bool = False
    stream: MuxStream | None = None
    pump: asyncio.Task[None] | None = None

    @property
    def instance(self) -> UUID:
        """The daemon instance which owns the stream."""
        return self.mux[0]

    ended: asyncio.Future[tuple[int, str]] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future(),
    )
    """The code and reason the tool connection ended with, which each socket is closed with."""

    agent_turn: asyncio.Condition = field(default_factory=asyncio.Condition)
    """Taken to send to the agent; signalled when the daemon's frames end a line."""

    daemon_mid_line: bool = False
    """Whether the daemon's last frame stopped inside a line."""

    signed_in: Callable[[], Awaitable[bool]] | None = None
    """For one the asker's own browser opened: asks again whether that browser is
    still signed in as the asker, from this server's pages; ``None`` for a key's.
    Its route sets it as the connection is joined."""


class ToolRelay:
    """Track agent sockets and lazily attach their bound daemon streams."""

    def __init__(
        self,
        *,
        alive: Callable[[ToolConnection], Awaitable[bool]],
        line_bytes: int,
        agent_sockets_per_host: int = DEFAULT_TOOL_AGENT_SOCKETS,
        streams_per_host: int = DEFAULT_TOOL_STREAMS,
        call_sec: float = 3_600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._alive = alive
        self._line_bytes = line_bytes
        self._agent_sockets = agent_sockets_per_host
        self._streams = streams_per_host
        self._call_sec = call_sec
        self._clock = clock
        self._connections: dict[UUID, ToolConnection] = {}
        self._socket_count: dict[UUID, int] = {}
        self._mux: dict[UUID, tuple[UUID, ToolMux]] = {}
        self._started = clock()
        self._used: dict[UUID, float] = {}
        self._detached_until: dict[UUID, float] = {}

    def called(self, connection: ToolConnection, call_id: str) -> None:
        """Record a call let through to the tool connection's host: it runs until answered.

        Args:
          connection: The tool connection it came on.
          call_id: The call's ``id``.

        """
        connection.running[call_id] = self._clock()
        self._touch(connection.host_id)

    def acked(self, connection: ToolConnection, call_id: str) -> None:
        """Record receipt of a call's result, including results too large to inspect.

        Args:
          connection: The agent that received the result.
          call_id: The wire id of the call.

        """
        if connection.running.pop(call_id, None) is not None:
            self._touch(connection.host_id)

    def idle_sec(self, host_id: UUID) -> float:
        """Return the time a host has been idle, independent of open sockets.

        Args:
          host_id: The host to measure.

        Returns:
          idle_sec: Seconds since activity; zero while work may be in flight.

        """
        now = self._clock()
        last = max(
            self._used.get(host_id, self._started),
            self._detached_until.get(host_id, self._started),
        )
        for connection in self._connections.values():
            if connection.host_id == host_id:
                for since in connection.running.values():
                    last = max(last, since + self._call_sec)
        return max(0.0, now - last)

    def join(
        self,
        host_id: UUID,
        *,
        agent: ToolSocket,
        asker: str,
        api_key_id: UUID | None,
        inbound: Inbound,
    ) -> ToolConnection:
        """Queue an offer of a new tool connection for the daemon.

        The caller goes straight on to :meth:`serve_agent`, which holds the slot this
        takes and gives it back.

        Args:
          host_id: The host the agent asks for.
          agent: The agent's socket.
          asker: The person the assistant opens it for.
          api_key_id: The key that opens it; ``None`` for the asker's own browser,
            signed in.
          inbound: Checks what the agent sends.

        Returns:
          connection: The new tool connection.

        Raises:
          ToolConnectionBusyError: The host holds its limit of tool connections.

        """
        mux = self._mux.get(host_id)
        if mux is None:
            raise ToolConnectionUnavailableError
        count = self._socket_count.get(host_id, 0)
        if count >= self._agent_sockets:
            raise ToolConnectionBusyError(self._agent_sockets)
        now = self._clock()
        connection = ToolConnection(
            id=uuid4(),
            host_id=host_id,
            agent=agent,
            asker=asker,
            api_key_id=api_key_id,
            inbound=inbound,
            opened=now,
            results=ResultIds(line_bytes=self._line_bytes),
            mux=mux,
        )
        self._connections[connection.id] = connection
        self._socket_count[host_id] = count + 1
        return connection

    async def serve_agent(self, connection: ToolConnection, read: _Read) -> None:
        """Relay one agent until it or the owning daemon leaves.

        Args:
          connection: An authenticated agent socket admitted by join.
          read: Reads the next agent frame or EOF.

        """
        task = asyncio.create_task(
            self._ending(connection, self._from_agent(connection, read)),
        )
        try:
            _ = await asyncio.wait({connection.ended})
        finally:
            _ = task.cancel()
            _ = await asyncio.gather(task, return_exceptions=True)
            self._end(
                connection,
                code=wire_terminals.CLOSE_ENDED,
                reason="the server is stopping",
                by="server",
            )
            try:
                await connection.agent.close(*connection.ended.result())
            finally:
                await self._detach(connection)

    async def serve_mux(
        self,
        host_id: UUID,
        instance: UUID,
        mux: ToolMux,
        read: _Read,
    ) -> bool:
        """Register one tools socket for this host until it goes away.

        Args:
          host_id: The authenticated host.
          instance: The registered daemon instance.
          mux: The transport for the outbound tools socket.
          read: Reads the socket's next binary frame or EOF.

        Returns:
          served: False if the host already had a mux socket, true after serving.

        """
        if host_id in self._mux:
            return False
        entry = self._mux[host_id] = (instance, mux)
        _logger.info(
            "daemon tool mux opened: host_id=%s instance=%s",
            host_id,
            instance,
        )
        try:
            await mux.run(read)
        finally:
            if self._mux.get(host_id) is entry:
                del self._mux[host_id]
            self._close_connections(host_id, entry, _DAEMON_LEFT)
            _logger.info(
                "daemon tool mux ended: host_id=%s instance=%s",
                host_id,
                instance,
            )
        return True

    def close_host(self, host_id: UUID) -> None:
        """Stop a revoked daemon and all connections attached to it.

        Args:
          host_id: The revoked or replaced host.

        """
        if (entry := self._mux.pop(host_id, None)) is not None:
            entry[1].stop()
            self._close_connections(
                host_id,
                entry,
                (
                    wire_terminals.CLOSE_HOST_REVOKED,
                    "the host was revoked, or joined again",
                    "revoke",
                ),
            )

    def _close_connections(
        self,
        host_id: UUID,
        entry: tuple[UUID, ToolMux],
        ending: tuple[int, str, str],
    ) -> None:
        for connection in tuple(self._connections.values()):
            if connection.host_id == host_id and connection.mux is entry:
                self._end(connection, code=ending[0], reason=ending[1], by=ending[2])

    async def sweep(self) -> None:
        """End every tool connection whose rule no longer holds.

        It closes as a refused socket is, so the agent's reconnect hears the true answer. A
        failing question leaves that tool connection up and asks about the rest.
        """
        for connection in tuple(self._connections.values()):
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

    async def _ending(
        self,
        connection: ToolConnection,
        work: Coroutine[object, object, None],
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
                by="agent",
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
        while (frame := await read()) is not None:
            to_daemon, to_agent = await connection.inbound(connection, frame)
            if to_daemon:
                stream = connection.stream or await self._open_stream(connection)
                await _send(_MuxSocket(stream=stream), to_daemon, lost=_DAEMON_LEFT)
            await _answer_agent(connection, to_agent)
        self._end(
            connection,
            code=_AGENT_LEFT[0],
            reason=_AGENT_LEFT[1],
            by=_AGENT_LEFT[2],
        )

    async def _open_stream(self, connection: ToolConnection) -> MuxStream:
        if self._mux.get(connection.host_id) is not connection.mux:
            raise _SocketLostError(*_DAEMON_LEFT)
        mux = connection.mux[1]
        if (
            not mux.has_capacity
            or sum(
                other.reserved
                for other in self._connections.values()
                if other.mux is connection.mux
            )
            >= self._streams
        ):
            raise ToolConnectionEndError("the host has reached its tool stream limit")
        connection.reserved = True
        try:
            async with asyncio.timeout(10.0):
                stream = await mux.open(uuid4())
        except (OSError, ValueError) as error:
            raise _SocketLostError(*_DAEMON_LEFT) from error
        connection.stream = stream
        connection.pump = asyncio.create_task(
            self._ending(connection, self._from_daemon(connection, stream)),
        )
        return stream

    async def _detach(self, connection: ToolConnection) -> None:
        stream, pump = connection.stream, connection.pump
        connection.stream = connection.pump = None
        if pump is not None:
            _ = pump.cancel()
            _ = await asyncio.gather(pump, return_exceptions=True)
        if stream is not None:
            await stream.close()

    async def _from_daemon(self, connection: ToolConnection, stream: MuxStream) -> None:
        while (frame := await stream.read()) is not None:
            if frame:
                async with connection.agent_turn:
                    await _send(connection.agent, frame, lost=_AGENT_LEFT)
                    connection.daemon_mid_line = not frame.endswith(b"\n")
                    for call_id in connection.results.feed(frame):
                        self.acked(connection, call_id)
                    connection.agent_turn.notify_all()
        raise _SocketLostError(*_DAEMON_LEFT)

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
        host = connection.host_id
        if remaining := self._socket_count[host] - 1:
            self._socket_count[host] = remaining
        else:
            del self._socket_count[host]
        if connection.running:
            deadline = max(connection.running.values()) + self._call_sec
            self._detached_until[host] = max(
                self._detached_until.get(host, self._started),
                deadline,
            )

    def _touch(self, host_id: UUID) -> None:
        self._used[host_id] = self._clock()


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


async def _send(
    socket: ToolSocket | _MuxSocket,
    data: bytes,
    *,
    lost: tuple[int, str, str],
) -> None:
    """Send frames in order, ending the connection if the peer leaves."""
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


@dataclass(slots=True, kw_only=True)
class _MuxSocket:
    stream: MuxStream

    async def send_bytes(self, data: bytes) -> None:
        try:
            await self.stream.send(data)
        except OSError as error:
            raise SocketClosedError from error


class _SocketLostError(Exception):
    """A send found its peer gone, which ends the tool connection with ``code`` and ``reason``."""

    def __init__(self, code: int, reason: str, by: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.by = by
