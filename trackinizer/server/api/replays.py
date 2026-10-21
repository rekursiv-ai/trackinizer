"""Replay the result of a consuming request after an interrupted response."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING
from uuid import UUID

import asyncio
import time

from fastapi import HTTPException

from trackinizer.server.inbound import Inbound
from trackinizer.wire.wire_terminals import TerminalWait


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Hashable


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
        now = self._clock()
        self._receipts = {
            key: receipt
            for key, receipt in self._receipts.items()
            if receipt.expires is None or receipt.expires > now
        }
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
            receipt.expires = self._clock() + self._retention_sec


@dataclass(frozen=True, slots=True, kw_only=True)
class Replays:
    """The independently typed receipts for each consuming API operation."""

    inbound: Replay[list[Inbound]] = field(default_factory=Replay)
    terminal_waits: Replay[TerminalWait | None] = field(default_factory=Replay)
    terminal_opens: Replay[UUID] = field(default_factory=Replay)


@dataclass(slots=True, kw_only=True)
class _Receipt[T]:
    fingerprint: str
    pending: asyncio.Future[T]
    expires: float | None = None
