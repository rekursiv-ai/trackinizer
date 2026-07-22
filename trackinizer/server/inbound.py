"""In-process inbound-message queue: world -> a live session's input.

A message addressed to a session (web chat box, another agent) is enqueued
here and drained by that session's ``trax run`` poller, which injects it into
the CLI. This is the routing seam for the messaging channel; it is
deliberately **separate from** ``agent_session_events`` (capture): an inbound
message is transient routing, not a recorded turn. When the agent consumes
it, the capture path logs it as a normal turn.

Phase-2a transport is a process-local in-memory queue (one trackinizer
process; drop-if-absent semantics). The two functions
:meth:`InboundQueue.enqueue` / :meth:`InboundQueue.drain` are the seam: a
durable-inbox or multi-process upgrade (NOTIFY, a table, Redis) replaces them
without touching routes or the client.
"""

from __future__ import annotations

from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from uuid import UUID

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

    def pending(self, session_id: UUID) -> int:
        """How many messages are queued for ``session_id`` (test/inspection)."""
        with self._lock:
            queue = self._queues.get(session_id)
            return len(queue) if queue else 0
