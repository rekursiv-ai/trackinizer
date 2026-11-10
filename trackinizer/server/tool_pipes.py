"""The hub that pairs an agent with a machine's host over a pipe for sagent's tools.

sagent already does reconnect, resume, acks, held results, grants and cancel end to end,
so a pipe is thin. It has no ring, no buffer and no reattach, and it never takes a direct
link. Either leg dropping ends it, and sagent reconnects with a new pipe.

An agent calls :meth:`PipeHub.join`, which queues an offer for the machine's host. The
host holds :meth:`PipeHub.wait`, is handed the offer at once, and dials back to
:meth:`PipeHub.attach` within the hub's ``dial_back_sec``. Each leg's route then runs its ``serve``
method until the pipe ends. A frame is read from a leg only once the frame before it was
sent on, so a slow reader slows its sender and nothing piles up here. The host's frames
are relayed as they came. A copy is read for whether the last ended a line, as a line
``Inbound`` answers the agent with must not land inside one of the host's, and for the
calls its results answer.

A pipe can last as long as sagent keeps it, so what let it open is asked again: each
:meth:`PipeHub.sweep` ends a pipe whose rule no longer holds.

The hub keeps whether each machine has a call running, and when it last had a call or a
result (:meth:`PipeHub.idle_sec`). A call is running from when the route lets it
through (:meth:`PipeHub.called`) until the host's ``Result`` for it passes, which the
hub reads with :class:`~trackinizer.server.tool_lines.ResultIds`, or the agent
acks it (:meth:`PipeHub.acked`), or its pipe ends, or ``call_sec`` has gone by. A ping
or a pipe held open with no call marks nothing.

A pipe ends with one close code and a reason saying what happened, sent on both legs,
and logs one line saying so: which leg or rule ended it, the code, the reason, the host
instance that attached and how long it lasted. Each offer handed to a host logs a line
too. The hub never touches the database, and the clock and sleep are
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
    "Pipe",
    "PipeBusyError",
    "PipeEndError",
    "PipeHub",
    "PipeRevokedError",
    "PipeSocket",
    "close_reason",
]

_logger = logging.getLogger(__name__)

type Inbound = Callable[[Pipe, bytes], Awaitable[tuple[bytes, bytes]]]
"""Takes the pipe and what its agent sent, and returns what goes on:
``(to_host, to_agent)``.

It may raise :class:`PipeEndError` to refuse the pipe saying why; any other error ends
it as failed."""

type _Read = Callable[[], Awaitable[bytes | None]]
"""Reads a leg's next frame; ``None`` when its peer left."""


class PipeBusyError(Exception):
    """The machine already holds as many pipes as it may: ``limit``."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"the machine already holds its limit of {limit} pipes")
        self.limit = limit


class PipeEndError(Exception):
    """Raised by an :data:`Inbound` to refuse the pipe saying why, passing nothing on."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.code = wire_terminals.CLOSE_REFUSED
        self.reason = reason


class PipeRevokedError(Exception):
    """The machine was revoked, or joined again, while its host waited."""


class PipeSocket(Socket, Protocol):
    """A leg of a pipe, which closes saying why."""

    @override
    async def close(self, code: int, reason: str = "") -> None:
        """Close with ``code`` and ``reason``; closing a closed socket is a no-op."""
        ...


@dataclass(slots=True, kw_only=True, eq=False)
class Pipe:
    """One agent, and the host that dials back for it."""

    id: UUID
    machine_id: UUID
    agent: PipeSocket
    asker: str
    """The person the assistant opened this pipe for."""

    api_key_id: UUID
    """The assistant's key that opened it."""

    inbound: Inbound
    opened: float
    """When it opened, on the hub's clock."""

    results: ResultIds
    """Reads which calls the host's results answer."""

    running: dict[str, float] = field(default_factory=dict[str, float])
    """The calls let through whose result has not passed yet: when each was, by id."""

    instance: UUID | None = None
    """The host instance that dialed back; ``None`` until one has."""

    attached: asyncio.Future[PipeSocket] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future(),
    )
    """The host's leg, once it has dialed back."""

    ended: asyncio.Future[tuple[int, str]] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future(),
    )
    """The code and reason the pipe ended with, which each leg is closed with."""

    agent_turn: asyncio.Condition = field(default_factory=asyncio.Condition)
    """Taken to send to the agent; signalled when the host's frames end a line."""

    host_mid_line: bool = False
    """Whether the host's last frame stopped inside a line."""


class PipeHub:
    """Every pipe waiting on or relaying through a host in this process, and its limits.

    Args:
      alive: Whether the whole rule that let a pipe open still holds; asked by the sweep.
      pipes_per_machine: The most pipes one machine holds at once.
      dial_back_sec: How long an offered pipe waits for its host to dial back.
      line_bytes: The longest host line read for which call it answers; a longer
        result is known by the agent's ack of it.
      call_sec: The longest one call keeps its machine in use, as one never answered
        would forever: an hour, six times sagent's longest ``Bash`` timeout.
      clock: Monotonic seconds.
      sleep: Waits the given seconds; every deadline in the hub runs on it.

    """

    def __init__(
        self,
        *,
        alive: Callable[[Pipe], Awaitable[bool]],
        pipes_per_machine: int,
        dial_back_sec: float,
        line_bytes: int,
        call_sec: float = 3_600.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._alive = alive
        self._pipes_per_machine = pipes_per_machine
        self._dial_back_sec = dial_back_sec
        self._line_bytes = line_bytes
        self._call_sec = call_sec
        self._clock = clock
        self._sleep = sleep
        self._pipes: dict[UUID, Pipe] = {}
        self._pending: dict[UUID, deque[Pipe]] = {}
        self._waiters: dict[UUID, list[asyncio.Future[bool]]] = {}
        self._started = clock()
        self._used: dict[UUID, float] = {}

    def called(self, pipe: Pipe, call_id: str) -> None:
        """Record a call let through to the pipe's machine: it runs until answered.

        Args:
          pipe: The pipe it came on.
          call_id: The call's ``id``.

        """
        pipe.running[call_id] = self._clock()
        self._touch(pipe.machine_id)

    def acked(self, pipe: Pipe, call_id: str) -> None:
        """Record that the agent holds call ``call_id``'s result: it no longer runs.

        Args:
          pipe: The pipe the ack came on.
          call_id: The call's ``id``.

        """
        if pipe.running.pop(call_id, None) is not None:
            self._touch(pipe.machine_id)

    # Held in memory only, as every pipe is: after a restart a machine's clock starts at
    # the restart, so an idle rule built on it acts at most that much late.
    def idle_sec(self, machine_id: UUID) -> float:
        """Return how long the machine has had no call running, no call and no result.

        Args:
          machine_id: The machine.

        Returns:
          idle_sec: Zero while a call runs on it, for ``call_sec`` at most; else
            seconds on the hub's clock since its last call or result, or a call's
            ``call_sec`` ran out, or since the hub started for a machine with none.

        """
        now = self._clock()
        last = self._used.get(machine_id, self._started)
        for pipe in self._pipes.values():
            if pipe.machine_id != machine_id:
                continue
            for since in pipe.running.values():
                if now - since < self._call_sec:
                    return 0.0
                last = max(last, since + self._call_sec)
        return now - last

    def join(
        self,
        machine_id: UUID,
        *,
        agent: PipeSocket,
        asker: str,
        api_key_id: UUID,
        inbound: Inbound,
    ) -> Pipe:
        """Queue an offer of a new pipe for the machine's host.

        The caller goes straight on to :meth:`serve_agent`, which holds the slot this
        takes and gives it back.

        Args:
          machine_id: The machine the agent asks for.
          agent: The agent's leg.
          asker: The person the assistant opens it for.
          api_key_id: The assistant's key that opens it.
          inbound: Checks what the agent sends.

        Returns:
          pipe: The new pipe.

        Raises:
          PipeBusyError: The machine holds its limit of pipes.

        """
        if (
            sum(pipe.machine_id == machine_id for pipe in self._pipes.values())
            >= self._pipes_per_machine
        ):
            raise PipeBusyError(self._pipes_per_machine)
        pipe = Pipe(
            id=uuid4(),
            machine_id=machine_id,
            agent=agent,
            asker=asker,
            api_key_id=api_key_id,
            inbound=inbound,
            opened=self._clock(),
            results=ResultIds(line_bytes=self._line_bytes),
        )
        self._pipes[pipe.id] = pipe
        self._pending.setdefault(machine_id, deque()).append(pipe)
        # Every held wait is woken, as the first may have gone before it could take
        # the offer; those that lose it to another wait go back to waiting.
        for waiter in self._waiters.get(machine_id, []):
            if not waiter.done():
                waiter.set_result(False)
        return pipe

    # A host another has taken the machine from may still claim an offer while it waits:
    # its dial-back is refused, as ``attach`` is told only a current instance, and the
    # agent's redial offers the pipe again, at the cost of ``dial_back_sec``.
    async def wait(self, machine_id: UUID, *, wait_sec: float) -> ToolOffer | None:
        """Return the machine's next offer, holding up to ``wait_sec`` for one.

        Args:
          machine_id: The machine whose host is asking.
          wait_sec: How long to hold.

        Returns:
          offer: The oldest queued offer, or ``None`` when none came in time.

        Raises:
          PipeRevokedError: The machine was revoked while the host waited.

        """
        deadline = self._clock() + wait_sec
        while True:
            if (found := self._take(machine_id)) is not None:
                return found
            remaining = deadline - self._clock()
            if remaining <= 0:
                return None
            if not await self._park(machine_id, seconds=remaining):
                return self._take(machine_id)

    async def hold(self, machine_id: UUID, *, wait_sec: float) -> None:
        """Hold a wait that takes nothing, for a host from before tool pipes.

        Answering it at once would have that host ask again at once, in a loop.

        Args:
          machine_id: The machine whose host is asking.
          wait_sec: How long to hold.

        Raises:
          PipeRevokedError: The machine was revoked while the host waited.

        """
        deadline = self._clock() + wait_sec
        while (remaining := deadline - self._clock()) > 0:
            _ = await self._park(machine_id, seconds=remaining)

    def attach(
        self,
        machine_id: UUID,
        pipe_id: UUID,
        host: PipeSocket,
        *,
        instance: UUID,
    ) -> Pipe | None:
        """Give an offered pipe its host's leg.

        Args:
          machine_id: The machine the host has authenticated as.
          pipe_id: The pipe, from the host's offer.
          host: The host's leg.
          instance: The host instance, already checked to be the machine's current one.

        Returns:
          pipe: The pipe; ``None`` when it is unknown, is another machine's, has ended,
            or has its host already. Its id is learned only from its offer.

        """
        pipe = self._pipes.get(pipe_id)
        if pipe is None or pipe.machine_id != machine_id or pipe.attached.done():
            return None
        pipe.instance = instance
        pipe.attached.set_result(host)
        return pipe

    async def serve_agent(self, pipe: Pipe, read: _Read) -> None:
        """Relay the agent's frames until the pipe ends, then close the agent's leg.

        Args:
          pipe: The pipe, from :meth:`join`.
          read: Reads the agent's next frame.

        """
        await self._serve(
            pipe,
            pipe.agent,
            ("agent", self._from_agent(pipe, read)),
            ("dial-back", self._dial_back(pipe)),
        )

    async def serve_host(self, pipe: Pipe, read: _Read) -> None:
        """Relay the host's frames until the pipe ends, then close the host's leg.

        Args:
          pipe: The pipe, from :meth:`attach`.
          read: Reads the host's next frame.

        """
        await self._serve(
            pipe,
            pipe.attached.result(),
            ("host", self._from_host(pipe, read)),
        )

    def close_machine(self, machine_id: UUID) -> None:
        """End every pipe and wait of a revoked machine.

        Args:
          machine_id: The machine whose credential was revoked or replaced.

        """
        for pipe in [p for p in self._pipes.values() if p.machine_id == machine_id]:
            self._end(
                pipe,
                code=wire_terminals.CLOSE_MACHINE_REVOKED,
                reason="the machine was revoked, or joined again",
                by="revoke",
            )
        for waiter in self._waiters.get(machine_id, []):
            if not waiter.done():
                waiter.set_result(True)

    async def sweep(self) -> None:
        """End every pipe whose rule no longer holds.

        It closes as a refused leg is, so the agent's reconnect hears the true answer. A
        failing question leaves that pipe up and asks about the rest.
        """
        for pipe in list(self._pipes.values()):
            if not await self._still_alive(pipe):
                self._end(
                    pipe,
                    code=wire_terminals.CLOSE_REFUSED,
                    reason="no longer allowed: what opened this pipe no longer holds",
                    by="sweep",
                )

    async def _still_alive(self, pipe: Pipe) -> bool:
        """Ask whether the pipe's rule still holds; a failed question says yes."""
        try:
            return await self._alive(pipe)
        except Exception:
            _logger.warning("tool pipe %s: liveness unknown", pipe.id, exc_info=True)
            return True

    # Each work is named for the leg or the limit that ends the pipe when it fails.
    async def _serve(
        self,
        pipe: Pipe,
        leg: PipeSocket,
        *works: tuple[str, Coroutine[object, object, None]],
    ) -> None:
        """Run a leg's work until the pipe ends, however it ends, then close the leg."""
        tasks = [
            asyncio.ensure_future(self._ending(pipe, work, by=by)) for by, work in works
        ]
        try:
            _ = await asyncio.wait({pipe.ended})
        finally:
            for task in tasks:
                _ = task.cancel()
            _ = await asyncio.gather(*tasks, return_exceptions=True)
            # The route itself was cancelled, as at shutdown: the other leg still ends.
            self._end(
                pipe,
                code=wire_terminals.CLOSE_ENDED,
                reason="the server is stopping",
                by="server",
            )
            await leg.close(*pipe.ended.result())

    async def _ending(
        self,
        pipe: Pipe,
        work: Coroutine[object, object, None],
        *,
        by: str,
    ) -> None:
        """Run one leg's work; what stops it for good also ends the pipe."""
        try:
            await work
        except _LegLostError as lost:
            self._end(pipe, code=lost.code, reason=lost.reason, by=lost.by)
        except PipeEndError as refused:
            # The type only: what a line held is not logged.
            cause = refused.__cause__
            self._end(
                pipe,
                code=refused.code,
                reason=refused.reason,
                by=by,
                cause="" if cause is None else type(cause).__name__,
            )
        except Exception as error:
            # A check that failed let nothing through, and must not let later lines by.
            _logger.exception("tool pipe failed: pipe=%s", pipe.id)
            self._end(
                pipe,
                code=wire_terminals.CLOSE_REFUSED,
                reason="the server could not go on relaying this pipe",
                by="server",
                cause=type(error).__name__,
            )

    async def _from_agent(self, pipe: Pipe, read: _Read) -> None:
        """Send the agent's frames on, until it leaves."""
        while (frame := await read()) is not None:
            # A pipe holds no buffer, so what the agent sends first waits right here.
            _ = await asyncio.wait({pipe.attached})
            to_host, to_agent = await pipe.inbound(pipe, frame)
            await _send(pipe.attached.result(), to_host, lost=_HOST_LEFT)
            await _answer_agent(pipe, to_agent)
        self._end(pipe, code=_AGENT_LEFT[0], reason=_AGENT_LEFT[1], by=_AGENT_LEFT[2])

    async def _from_host(self, pipe: Pipe, read: _Read) -> None:
        """Send the host's frames on to the agent unread, until it leaves."""
        while (frame := await read()) is not None:
            if answered := pipe.results.feed(frame):
                for call_id in answered:
                    _ = pipe.running.pop(call_id, None)
                self._touch(pipe.machine_id)
            if frame:
                async with pipe.agent_turn:
                    await _send(pipe.agent, frame, lost=_AGENT_LEFT)
                    pipe.host_mid_line = not frame.endswith(b"\n")
                    pipe.agent_turn.notify_all()
        self._end(pipe, code=_HOST_LEFT[0], reason=_HOST_LEFT[1], by=_HOST_LEFT[2])

    async def _dial_back(self, pipe: Pipe) -> None:
        """End a pipe whose host does not dial back in time."""
        if not await self._until(pipe.attached, seconds=self._dial_back_sec):
            _logger.warning(
                "tool pipe dial-back timed out: pipe=%s machine_id=%s wait_sec=%g",
                pipe.id,
                pipe.machine_id,
                self._dial_back_sec,
            )
            self._end(
                pipe,
                code=wire_terminals.CLOSE_HOST_LOST,
                reason="the host did not dial back in time",
                by="dial-back",
            )

    # The first end stands, so each pipe logs one line. ``by`` names the leg or the rule
    # that ended it: ``agent``, ``host``, ``dial-back``, ``sweep``, ``revoke`` or
    # ``server``; ``cause`` is the type of an error that did, never its text.
    def _end(
        self,
        pipe: Pipe,
        *,
        code: int,
        reason: str,
        by: str,
        cause: str = "",
    ) -> None:
        """End a pipe with ``code`` and ``reason``, and free its slot."""
        if pipe.ended.done():
            return
        pipe.ended.set_result((code, close_reason(reason)))
        _logger.info(
            "tool pipe ended: pipe=%s machine_id=%s by=%s code=%d reason=%r "
            "cause=%s instance=%s duration_sec=%.3f",
            pipe.id,
            pipe.machine_id,
            by,
            code,
            reason,
            cause or "-",
            pipe.instance or "-",
            self._clock() - pipe.opened,
        )
        del self._pipes[pipe.id]
        # Its running calls end with it, so the machine's idle time starts now.
        if pipe.running:
            pipe.running.clear()
            self._touch(pipe.machine_id)
        if (queue := self._pending.get(pipe.machine_id)) is not None:
            with contextlib.suppress(ValueError):
                queue.remove(pipe)
            if not queue:
                del self._pending[pipe.machine_id]

    def _touch(self, machine_id: UUID) -> None:
        """Record that the machine had a call or a result just now."""
        self._used[machine_id] = self._clock()

    async def _park(self, machine_id: UUID, *, seconds: float) -> bool:
        """Hold until the machine has news or the time is up; return if it has news."""
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(machine_id, []).append(waiter)
        try:
            woken = await self._until(waiter, seconds=seconds)
        finally:
            self._forget_waiter(machine_id, waiter)
        if woken and waiter.result():
            raise PipeRevokedError
        return woken

    def _forget_waiter(self, machine_id: UUID, waiter: asyncio.Future[bool]) -> None:
        """Drop a finished wait, and its machine's list once it is empty."""
        waiters = self._waiters[machine_id]
        waiters.remove(waiter)
        if not waiters:
            del self._waiters[machine_id]

    def _take(self, machine_id: UUID) -> ToolOffer | None:
        """Pop the machine's oldest queued offer, if it still has one."""
        queue = self._pending.get(machine_id)
        if queue is None:
            return None
        pipe = queue.popleft()
        if not queue:
            del self._pending[machine_id]
        _logger.info("tool pipe offered: pipe=%s machine_id=%s", pipe.id, machine_id)
        return ToolOffer(pipe_id=pipe.id)

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
_HOST_LEFT: Final = (wire_terminals.CLOSE_HOST_LOST, "the host left", "host")
"""How a leg's leaving ends the pipe: the code, the reason, and the leg."""


async def _send(socket: Socket, data: bytes, *, lost: tuple[int, str, str]) -> None:
    """Send ``data`` in frames, each awaited; a peer gone ends the pipe with ``lost``."""
    try:
        for start in range(0, len(data), wire_tools.FRAME_BYTES):
            await socket.send_bytes(data[start : start + wire_tools.FRAME_BYTES])
    except SocketClosedError as error:
        raise _LegLostError(*lost) from error


async def _answer_agent(pipe: Pipe, line: bytes) -> None:
    """Answer the agent between the host's lines, which go on arriving meanwhile."""
    if line:
        async with pipe.agent_turn:
            _ = await pipe.agent_turn.wait_for(lambda: not pipe.host_mid_line)
            await _send(pipe.agent, line, lost=_AGENT_LEFT)


class _LegLostError(Exception):
    """A send found its peer gone, which ends the pipe with ``code`` and ``reason``."""

    def __init__(self, code: int, reason: str, by: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.by = by
