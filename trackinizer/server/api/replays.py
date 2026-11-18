"""Replay the result of a consuming request after an interrupted response."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING
from uuid import UUID

import asyncio
import time

from trackinizer.server.execs import MayHaveRun
from trackinizer.server.inbound import Inbound
from trackinizer.wire.wire_exec import ExecResult, ExecWait
from trackinizer.wire.wire_terminals import TerminalWait


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Hashable

    from fastapi import HTTPException
else:
    from wrapt import lazy_import

    HTTPException = lazy_import("fastapi", "HTTPException")  # ~120 ms.


class Replay[T]:
    """Retain consuming responses on one event loop for fifteen minutes.

    Authorization stays with the route and runs on every attempt, before this
    cache is consulted. Keys include the authenticated principal. Empty polls and
    failed operations retain no entry, so ordinary idle polling cannot exhaust
    its 4,096 slots. Pending operations outlive a disconnected waiter; otherwise a
    cancellation could discard a message already removed from its queue.
    """

    def __init__(
        self,
        *,
        max_entries: int = 4_096,
        retention_sec: float = 15 * 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_entries = max_entries
        self._retention_sec = retention_sec
        self._clock = clock
        self._receipts: dict[Hashable, _Receipt[T]] = {}
        # Each kept receipt, in the order it expires: one retention for all, stamped
        # from a monotonic clock as each finishes, so the order it finished in.
        self._expiring: deque[tuple[float, Hashable]] = deque()

    async def run(
        self,
        key: Hashable | None,
        *,
        fingerprint: str,
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        """Serve or replay a consuming operation after the route authorizes it.

        Args:
          key: Principal-scoped request identity; None retains legacy delivery.
          fingerprint: Target and parameters the identity was first used for.
          operation: The consuming operation, called only on a cache miss.

        Returns:
          result: The original operation's result.

        Raises:
          HTTPException: 409 on identity reuse with different parameters, or 429
            before consumption when every receipt slot is occupied.

        """
        if key is None:
            return await operation()
        self._expire(self._clock())
        receipt = self._receipts.get(key)
        if receipt is not None and receipt.fingerprint != fingerprint:
            raise HTTPException(409, "Idempotency-Key names a different request")
        if receipt is None:
            if len(self._receipts) >= self._max_entries:
                raise HTTPException(
                    429,
                    "Delivery receipt capacity reached; retry later",
                )
            pending = asyncio.ensure_future(operation())
            receipt = _Receipt(fingerprint=fingerprint, pending=pending)
            self._receipts[key] = receipt
            pending.add_done_callback(partial(self._finished, key))
        return await asyncio.shield(receipt.pending)

    # A poll per request: dropping only the receipts that are due keeps it from walking
    # all 4,096 of them each time.
    def _expire(self, now: float) -> None:
        """Forget every kept receipt whose retention ended by ``now``."""
        # A key is in the queue only while its kept receipt is the one held under it:
        # the receipt leaves the two together, here.
        while self._expiring and self._expiring[0][0] <= now:
            _, key = self._expiring.popleft()
            del self._receipts[key]

    def _finished(self, key: Hashable, pending: asyncio.Future[T]) -> None:
        receipt = self._receipts.get(key)
        if receipt is None or receipt.pending is not pending:
            return
        if (
            pending.cancelled()
            or pending.exception() is not None
            or not pending.result()
        ):
            del self._receipts[key]
        else:
            self._expiring.append((self._clock() + self._retention_sec, key))


@dataclass(frozen=True, slots=True, kw_only=True)
class Replays:
    """The independently typed receipts for each consuming API operation."""

    inbound: Replay[list[Inbound]] = field(default_factory=Replay)
    terminal_waits: Replay[TerminalWait | None] = field(default_factory=Replay)
    terminal_opens: Replay[UUID] = field(default_factory=Replay)
    # A receipt holds a whole command, up to 1.4 MB of stdin among it, or a result of
    # up to two 1 MiB streams, so the default 4,096 slots kept 15 minutes would let
    # one user's retries pin gigabytes. A minute holds the 60 a user may start in it,
    # and a retry follows a lost response within seconds.
    exec_waits: Replay[ExecWait | None] = field(
        default_factory=lambda: Replay(max_entries=256, retention_sec=60),
    )
    exec_runs: Replay[ExecResult | MayHaveRun] = field(
        default_factory=lambda: Replay(max_entries=256, retention_sec=60),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class _Receipt[T]:
    fingerprint: str
    pending: asyncio.Future[T]
