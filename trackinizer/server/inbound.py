"""In-process inbound-message queue: world -> a live session's input.

A message addressed to a session (web chat box, another agent) is enqueued
here and drained by that session's ``trax run`` poller, which injects it into
the CLI. This is the routing seam for the messaging channel; it is
deliberately **separate from** ``session_records`` (capture): an inbound
message is transient routing, not a recorded turn. When the agent consumes
it, the capture path logs it as a normal turn.

Transport is a process-local in-memory queue: one trackinizer process,
drop-if-absent semantics. The server runs single-process for exactly this
reason -- the queues, the dedup receipts, and the waiters below all live in
memory, so a second process would neither see a send enqueued by the first
nor its receipts. The two functions :meth:`InboundQueue.enqueue` /
:meth:`InboundQueue.drain` are the seam: a durable-inbox or multi-process
upgrade (NOTIFY, a table, Redis) replaces them without touching routes or
the client. ``docs/private/workers.md`` specs that upgrade -- this class is
the only per-process state standing between the server and ``--workers N``.
"""

from __future__ import annotations

from collections import OrderedDict, defaultdict, deque
from contextlib import suppress
from dataclasses import dataclass, field
from uuid import UUID

import asyncio
import logging
import threading


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class Inbound:
    """One queued inbound message: text, attested sender, and routed room."""

    text: str
    source: str | None = None
    room: str | None = None
    """The room a routed send was scoped to; threads into the ``[room]
    sender:`` injection prefix. ``None`` for a direct (session-id) enqueue."""


@dataclass(slots=True, kw_only=True)
class _Waiter:
    """One caller awaiting a message, with the loop that must wake it."""

    loop: asyncio.AbstractEventLoop
    event: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(slots=True, kw_only=True)
class InboundQueue:
    """Thread-safe per-session FIFO of pending inbound messages.

    Bounded per session so a session whose poller has gone (drop-if-absent)
    cannot grow memory without limit: the oldest messages are dropped once
    the cap is reached, matching the live-channel "stale steering is worse
    than dropping" stance.
    """

    max_per_session: int = 256
    max_seen_keys: int = 4_096
    _queues: dict[UUID, deque[Inbound]] = field(
        default_factory=lambda: defaultdict(deque)
    )
    _seen_sends: OrderedDict[UUID, list[UUID]] = field(default_factory=OrderedDict)
    # Callers blocked in ``await_messages``, by session. A list, not one event
    # per session: two waiters must both wake, or the second hangs to its
    # timeout because the first consumed the only wakeup.
    _waiters: dict[UUID, list[_Waiter]] = field(
        default_factory=lambda: defaultdict(list)
    )
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def send_once(
        self,
        key: UUID | None,
        targets: list[tuple[UUID, Inbound]],
    ) -> list[UUID]:
        """Atomically dedup, enqueue, and record one idempotent send.

        The dedup-check, the per-target enqueues, and the receipt record run
        under one lock acquisition, so two concurrent same-key sends cannot
        both pass the "unseen" check and double-enqueue: the loser sees the
        winner's recorded receipt and enqueues nothing. Liveness must be
        resolved by the caller *before* this call -- ``targets`` is the
        already-resolved ``(session_id, message)`` list -- so the async DB
        resolve never runs while the in-memory lock is held.

        A ``None`` key has no dedup record (the caller had no
        ``Idempotency-Key``): every target is enqueued and the delivered ids
        returned, but nothing is remembered. An empty non-replayed delivery
        is not recorded, so a retry once a session is live still delivers
        instead of replaying an empty receipt.

        Returns:
          delivered: the session ids enqueued to (or the original receipt on
            a key replay).

        """
        with self._lock:
            if key is not None:
                seen = self._seen_sends.get(key)
                if seen is not None:
                    return seen
            delivered: list[UUID] = []
            for session_id, message in targets:
                self._append_capped(session_id, message)
                delivered.append(session_id)
            if key is not None and delivered:
                self._seen_sends[key] = delivered
                while len(self._seen_sends) > self.max_seen_keys:
                    self._seen_sends.popitem(last=False)
            return delivered

    def enqueue(self, session_id: UUID, message: Inbound) -> int:
        """Append ``message`` for ``session_id``; return the pending count.

        Drops the oldest message when the per-session cap is exceeded so an
        unattended session cannot leak memory.
        """
        with self._lock:
            self._append_capped(session_id, message)
            return len(self._queues[session_id])

    def _append_capped(self, session_id: UUID, message: Inbound) -> None:
        """Append ``message``, dropping the oldest over the per-session cap.

        Each overflow drop loses an undelivered inbound message (the session's
        poller is behind or gone), so WARN on it: a silent drop hides a stuck
        or absent ``trax run`` poller. Caller holds ``self._lock``.
        """
        queue = self._queues[session_id]
        queue.append(message)
        self._wake(session_id)
        while len(queue) > self.max_per_session:
            queue.popleft()
            _LOGGER.warning(
                "inbound queue for session %s over cap (%d); dropped oldest message",
                session_id,
                self.max_per_session,
            )

    def drain(self, session_id: UUID) -> list[Inbound]:
        """Remove and return all pending messages for ``session_id``, oldest first."""
        with self._lock:
            queue = self._queues.pop(session_id, None)
            return list(queue) if queue else []

    async def await_messages(
        self, session_id: UUID, *, timeout_sec: float
    ) -> list[Inbound]:
        """Drain ``session_id``, waiting up to ``timeout_sec`` for a message.

        What a poller replaces: asking every half-second costs one request per
        session per interval whether or not anything was sent, and still
        delivers up to that interval late. Waiting costs one held request and
        returns the moment a message is enqueued.

        Returns empty at the timeout rather than holding forever -- a proxy
        will cut an idle connection anyway, and the caller needs a turn to
        notice it should stop.

        Args:
          session_id: Session to drain.
          timeout_sec: How long to wait when nothing is pending.

        Returns:
          messages: Everything queued, oldest first; empty on timeout.

        """
        pending = self.drain(session_id)
        if pending:
            return pending
        waiter = _Waiter(loop=asyncio.get_running_loop())
        with self._lock:
            self._waiters[session_id].append(waiter)
        try:
            await asyncio.wait_for(waiter.event.wait(), timeout_sec)
        except TimeoutError:
            return []
        finally:
            self._release(session_id, waiter)
        return self.drain(session_id)

    def _release(self, session_id: UUID, waiter: _Waiter) -> None:
        """Drop a waiter that has woken or timed out."""
        with self._lock:
            waiters = self._waiters.get(session_id)
            if waiters is None:
                return
            with suppress(ValueError):
                waiters.remove(waiter)
            if not waiters:
                del self._waiters[session_id]

    def _wake(self, session_id: UUID) -> None:
        """Wake everything waiting on ``session_id``. Caller holds ``_lock``.

        Each waiter is woken through its OWN loop: an enqueue arrives on
        whatever thread served that request, and ``Event.set`` is not
        thread-safe against the loop awaiting it.
        """
        for waiter in self._waiters.get(session_id, ()):
            if waiter.loop.is_closed():
                continue
            with suppress(RuntimeError):
                waiter.loop.call_soon_threadsafe(waiter.event.set)

    def pending(self, session_id: UUID) -> int:
        """How many messages are queued for ``session_id`` (test/inspection)."""
        with self._lock:
            queue = self._queues.get(session_id)
            return len(queue) if queue else 0
