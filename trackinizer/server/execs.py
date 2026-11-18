"""The hub that hands a command to a machine's host and carries its result back.

A command is held in memory, in this one process, only while its caller waits for it,
like a terminal (``server/terminals.py``). The assistant's request calls :meth:`ExecHub.run`
and waits; the machine's host holds :meth:`ExecHub.wait` and is handed the command at
once; its result arrives through :meth:`ExecHub.deliver` and ends the caller's wait. The
hub never touches the database or a socket, and the clock and sleep are injected, so
every limit is tested without waiting for it.

A command no host takes, or whose host never reports, ends the caller's wait with an
error, and a caller that goes away takes a command no host has taken with it. A command
the host was handed keeps its slot on the machine after its caller goes, until the host
reports or its timeout and grace are up: the process may still be running there.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import asyncio
import contextlib
import time

from trackinizer.wire import wire_exec
from trackinizer.wire.wire_exec import ExecRequest, ExecResult


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


__all__ = [
    "Exec",
    "ExecBusyError",
    "ExecHub",
    "ExecLimitError",
    "ExecLostError",
    "ExecRevokedError",
    "ExecStaleError",
    "MayHaveRun",
    "Submission",
    "always_current",
]


class ExecBusyError(Exception):
    """The machine already holds as many commands as it may."""


class ExecLimitError(Exception):
    """The user started too many commands in the last minute."""


class ExecRevokedError(Exception):
    """The machine was revoked, or joined again, while the command waited.

    Args:
      taken: Whether the host was handed the command, so it may have run.

    """

    def __init__(self, *, taken: bool = False) -> None:
        super().__init__("the machine was revoked")
        self.taken = taken


class ExecStaleError(Exception):
    """The host asking for a command is no longer the machine's current instance."""


class ExecLostError(Exception):
    """The host did not take the command, or took it and did not report in time.

    Args:
      taken: Whether the host was handed the command, so it may have run.

    """

    def __init__(self, *, taken: bool) -> None:
        super().__init__(
            "the host took the command but did not report"
            if taken
            else "the host did not take the command",
        )
        self.taken = taken


@dataclass(frozen=True, slots=True, kw_only=True)
class MayHaveRun:
    """How a command ended after its host was handed it: it may have run.

    A receipt keeps this, where it keeps no error, so a retry under the same key is
    told the same and does not run the command again.

    Args:
      revoked: Whether the machine was revoked, rather than the host lost.

    """

    revoked: bool

    def error(self) -> ExecLostError | ExecRevokedError:
        """Return the error the command ended with."""
        if self.revoked:
            return ExecRevokedError(taken=True)
        return ExecLostError(taken=True)


@dataclass(frozen=True, slots=True, kw_only=True)
class Submission:
    """What a command is run with; the hub adds its id and its two waits."""

    user: str
    machine_id: UUID
    machine: str
    request: ExecRequest


@dataclass(slots=True, kw_only=True, eq=False)
class Exec:
    """One command: who asked, where it runs, and the two things its caller awaits."""

    id: UUID
    user: str
    machine_id: UUID
    machine: str
    request: ExecRequest
    taken: asyncio.Future[None] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future(),
    )
    """Done once a host was handed the command, or the machine was revoked."""

    outcome: asyncio.Future[ExecResult | None] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future(),
    )
    """The host's result; ``None`` when the machine was revoked first."""

    handed_at: float | None = None
    """The hub's clock when a host was handed the command; ``None`` before that."""


async def always_current() -> bool:
    """Say yes: the check of a caller that holds no host instance to verify."""
    return True


class ExecHub:
    """Every command waiting on or running at a host in this process, and its limits.

    Args:
      clock: Monotonic seconds.
      sleep: Waits the given seconds; every deadline in the hub runs on it.
      delivered_keep_sec: How long a delivered command's id is remembered, so that a
        result sent again, after a lost response, is accepted.

    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        delivered_keep_sec: float = 60.0,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._delivered_keep_sec = delivered_keep_sec
        self._execs: dict[UUID, Exec] = {}
        self._pending: dict[UUID, deque[Exec]] = {}
        self._waiters: dict[UUID, list[asyncio.Future[bool]]] = {}
        self._starts: dict[str, deque[float]] = {}
        # Handed to a host whose caller has gone, and so still running as far as the
        # hub knows: each holds its slot on the machine until it is reported or due.
        self._held: dict[UUID, _Mark] = {}
        self._delivered: dict[UUID, _Mark] = {}

    async def run(self, submission: Submission) -> ExecResult:
        """Queue a command for the machine's host and wait for its result.

        Args:
          submission: The command, who it runs for, and the machine.

        Returns:
          result: What the host reported.

        Raises:
          ExecBusyError: The machine holds its limit of commands.
          ExecLimitError: The user started too many commands in the last minute.
          ExecLostError: No host took the command, or the host did not report in time.
          ExecRevokedError: The machine was revoked while the command waited.

        """
        found = self._queue(submission)
        try:
            return await self._result(found)
        finally:
            self._release(found)

    async def wait(
        self,
        machine_id: UUID,
        *,
        wait_sec: float,
        is_current: Callable[[], Awaitable[bool]] = always_current,
    ) -> Exec | None:
        """Return the machine's next command, holding up to ``wait_sec`` for one.

        Args:
          machine_id: The machine whose host is asking.
          wait_sec: How long to hold.
          is_current: Says whether the asking host is still the machine's current
            instance; asked just before a command is handed out.

        Returns:
          found: The oldest queued command, or ``None`` when none came in time.

        Raises:
          ExecRevokedError: The machine was revoked while the host waited.
          ExecStaleError: The host stopped being the machine's current instance.

        """
        loop = asyncio.get_running_loop()
        deadline = self._clock() + wait_sec
        while True:
            if (found := await self._claim(machine_id, is_current)) is not None:
                return found
            remaining = deadline - self._clock()
            if remaining <= 0:
                return None
            waiter = loop.create_future()
            self._waiters.setdefault(machine_id, []).append(waiter)
            try:
                woken = await self._until(waiter, seconds=remaining)
            finally:
                self._forget_waiter(machine_id, waiter)
            if not woken:
                return await self._claim(machine_id, is_current)
            if waiter.result():
                raise ExecRevokedError

    def deliver(self, machine_id: UUID, exec_id: UUID, result: ExecResult) -> bool:
        """Hand the host's result to the command's caller.

        A result sent again for a command already delivered is accepted and changes
        nothing, since the host cannot tell a lost response from a lost request.

        Args:
          machine_id: The machine that reports.
          exec_id: The command it was handed.
          result: What running it produced.

        Returns:
          accepted: False when the command is unknown, is another machine's, was not
            handed out, or ended another way than by a result.

        """
        now = self._clock()
        self._sweep(now)
        if (seen := self._delivered.get(exec_id)) and seen.machine_id == machine_id:
            return True
        if (found := self._execs.get(exec_id)) is not None:
            if (
                found.machine_id != machine_id
                or found.handed_at is None
                or found.outcome.done()
            ):
                return False
            found.outcome.set_result(result)
        elif (held := self._held.get(exec_id)) and held.machine_id == machine_id:
            del self._held[exec_id]
        else:
            return False
        self._delivered[exec_id] = _Mark(
            machine_id=machine_id,
            until=now + self._delivered_keep_sec,
        )
        return True

    def close_machine(self, machine_id: UUID) -> None:
        """End every command and wait of a revoked machine.

        Args:
          machine_id: The machine whose credential was revoked or replaced.

        """
        for found in [e for e in self._execs.values() if e.machine_id == machine_id]:
            if not found.taken.done():
                found.taken.set_result(None)
            if not found.outcome.done():
                found.outcome.set_result(None)
        _ = self._pending.pop(machine_id, None)
        for marks in (self._held, self._delivered):
            for exec_id in [i for i, m in marks.items() if m.machine_id == machine_id]:
                del marks[exec_id]
        for waiter in self._waiters.get(machine_id, []):
            if not waiter.done():
                waiter.set_result(True)

    def _queue(self, submission: Submission) -> Exec:
        """Queue a command for its machine's host, or refuse it at a limit."""
        now = self._clock()
        self._sweep(now)
        starts = self._starts.get(submission.user, deque())
        if len(starts) >= wire_exec.MAX_EXECS_PER_MIN_PER_USER:
            raise ExecLimitError
        machine_id = submission.machine_id
        if (
            sum(found.machine_id == machine_id for found in self._execs.values())
            + sum(held.machine_id == machine_id for held in self._held.values())
            >= wire_exec.MAX_RUNNING_PER_MACHINE
        ):
            raise ExecBusyError
        found = Exec(
            id=uuid4(),
            user=submission.user,
            machine_id=machine_id,
            machine=submission.machine,
            request=submission.request,
        )
        starts.append(now)
        self._starts[submission.user] = starts
        self._execs[found.id] = found
        self._pending.setdefault(machine_id, deque()).append(found)
        # Every held wait is woken, as the first may have gone before it could take
        # the command; those that lose it to another wait go back to waiting.
        for waiter in self._waiters.get(machine_id, []):
            if not waiter.done():
                waiter.set_result(False)
        return found

    async def _result(self, found: Exec) -> ExecResult:
        """Wait for a host to take the command, then for it to report."""
        if not await self._until(found.taken, seconds=wire_exec.OFFER_TTL_SEC):
            raise ExecLostError(taken=False)
        if not await self._until(
            found.outcome,
            seconds=found.request.timeout_sec + wire_exec.RESULT_GRACE_SEC,
        ):
            raise ExecLostError(taken=True)
        if (result := found.outcome.result()) is None:
            raise ExecRevokedError(taken=found.handed_at is not None)
        return result

    def _release(self, found: Exec) -> None:
        """Drop a command its caller has left; keep its slot if a host may run it."""
        _ = self._execs.pop(found.id, None)
        if (queue := self._pending.get(found.machine_id)) is not None:
            with contextlib.suppress(ValueError):
                queue.remove(found)
            if not queue:
                del self._pending[found.machine_id]
        if found.handed_at is None or found.outcome.done():
            return
        until = found.handed_at + found.request.timeout_sec + wire_exec.RESULT_GRACE_SEC
        if self._clock() < until:
            self._held[found.id] = _Mark(machine_id=found.machine_id, until=until)

    def _sweep(self, now: float) -> None:
        """Forget what has run out: held slots, delivered ids and old starts."""
        for marks in (self._held, self._delivered):
            for exec_id in [i for i, mark in marks.items() if mark.until <= now]:
                del marks[exec_id]
        for user, starts in list(self._starts.items()):
            while starts and now - starts[0] >= 60.0:
                _ = starts.popleft()
            if not starts:
                del self._starts[user]

    def _forget_waiter(self, machine_id: UUID, waiter: asyncio.Future[bool]) -> None:
        """Drop a finished wait, and its machine's list once it is empty."""
        waiters = self._waiters[machine_id]
        waiters.remove(waiter)
        if not waiters:
            del self._waiters[machine_id]

    async def _claim(
        self,
        machine_id: UUID,
        is_current: Callable[[], Awaitable[bool]],
    ) -> Exec | None:
        """Hand out the machine's oldest queued command to a host that is current."""
        if machine_id not in self._pending:
            return None
        if not await is_current():
            raise ExecStaleError
        return self._take(machine_id)

    def _take(self, machine_id: UUID) -> Exec | None:
        """Pop the machine's oldest queued command not yet handed out."""
        queue = self._pending.get(machine_id, deque())
        found = None
        while queue and found is None:
            candidate = queue.popleft()
            if candidate.id in self._execs and not candidate.taken.done():
                found = candidate
        if not queue:
            _ = self._pending.pop(machine_id, None)
        if found is not None:
            found.handed_at = self._clock()
            found.taken.set_result(None)
        return found

    async def _until[T](self, future: asyncio.Future[T], *, seconds: float) -> bool:
        """Wait for ``future`` or for ``seconds`` on the injected sleep; return if done."""
        timer = asyncio.ensure_future(self._sleep(seconds))
        try:
            _ = await asyncio.wait({future, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            _ = timer.cancel()
        return future.done()


@dataclass(frozen=True, slots=True, kw_only=True)
class _Mark:
    """A command id the hub still remembers, on its machine, until a time."""

    machine_id: UUID
    until: float
