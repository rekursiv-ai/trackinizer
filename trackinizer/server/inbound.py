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
the client. This class is
the only per-process state standing between the server and ``--workers N``.
"""

from __future__ import annotations

from collections import OrderedDict, defaultdict, deque
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from uuid import UUID

import asyncio
import logging
import threading
import time

from trackinizer.server.auth import Role
from trackinizer.wire.wire_sessions import InboundKind, WorkspaceMessageContext


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class Inbound:
    """One queued inbound message: text, attested sender, and routed room."""

    text: str

    kind: InboundKind = "line"
    """What the message is, as its sender named it; the drain hands it on."""

    payload: dict[str, object] | None = None
    """The kind's structured data, as :data:`InboundKind` documents it."""

    source: str | None = None

    source_role: Role | None = None
    """The sender's role as the server saw it, beside the attested ``source``; ``None``
    for a message the server itself generates."""

    room: str | None = None
    """The room a routed send was scoped to; threads into the ``[room]
    sender:`` injection prefix. ``None`` for a direct (session-id) enqueue."""

    context: WorkspaceMessageContext | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PollerRun:
    """Where a session's poller says its run is: shown in Chat, never a grant.

    The run reports it on each drain, under the key that opened the session, so it is
    the owner's own word about their own machine.
    """

    run: str | None = None
    """The detached host's name, which ``trax run attach`` takes; ``None`` for a run in
    a terminal of its own."""

    machine: str | None = None
    """The run's machine, named as ``trax machine connect`` names it."""

    cwd: str | None = None
    """The run's working directory, ``~/...`` under its home directory."""


@dataclass(frozen=True, slots=True, kw_only=True)
class _Lease:
    """Until when a session counts as polled, and where its poller said it runs."""

    expires: float
    where: PollerRun


@dataclass(frozen=True, slots=True, kw_only=True)
class _Handed:
    """Who sent one chat line a poller took, and until when it counts as posted."""

    source: str | None
    until: float


class IdempotencyReuseError(Exception):
    """An idempotency key that sent one message was used to send another."""


@dataclass(frozen=True, slots=True, kw_only=True)
class _SendReceipt:
    """Original send result retained for retry-safe responses."""

    delivered: tuple[UUID, ...]
    fingerprint: str = ""
    source: str | None = None


@dataclass(slots=True, kw_only=True)
class _Waiter:
    """One caller awaiting a message, with the loop that must wake it."""

    loop: asyncio.AbstractEventLoop

    event: asyncio.Event = field(default_factory=asyncio.Event)

    released: bool = False
    """Set when its session's lease is revoked: the hold ends taking nothing."""


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

    max_handed_chats: int = 10_000

    handed_ttl_sec: float = 600.0
    """How long a taken chat line counts as posted, unrecorded. The assistant records a
    line as it hands it to its agent, before the turn, so seconds are the norm."""

    max_turns: int = 10_000

    turn_ttl_sec: float = 3_600.0
    """How long the key that took a chat line holds its poster's turn: the line may wait
    behind a turn of its chat, then run a turn of its own."""

    poller_ttl_sec: float = 45.0

    _clock: Callable[[], float] = field(default=time.monotonic, repr=False)

    _leases: dict[UUID, _Lease] = field(default_factory=dict)

    _queues: dict[UUID, deque[Inbound]] = field(
        default_factory=lambda: defaultdict(deque),
    )

    _seen_sends: OrderedDict[UUID, _SendReceipt] = field(default_factory=OrderedDict)

    # Callers blocked in ``await_messages``, by session. A list, not one event
    # per session: two waiters must both wake, or the second hangs to its
    # timeout because the first consumed the only wakeup.
    _waiters: dict[UUID, list[_Waiter]] = field(
        default_factory=lambda: defaultdict(list),
    )

    # Who sent each chat line a poller took and its chat does not yet record, by
    # conversation, the one handed a line longest ago first: a delete counts these as
    # posted, as it counts the queued ones.
    _handed: OrderedDict[UUID, list[_Handed]] = field(default_factory=OrderedDict)

    # Until when each key that took a chat line holds its poster's turn, by key and
    # lowercased poster, the one taken longest ago first. Unlike ``_handed``, the chat
    # recording the line does not end it: the turn runs after that.
    _turns: OrderedDict[tuple[UUID, str], float] = field(default_factory=OrderedDict)

    _lock: threading.Lock = field(default_factory=threading.Lock)

    # Each poll replaces what the last one said: a run that stops saying where it is
    # (an older trax resumed in its place) is not shown where the last one was.
    def mark_poller(
        self,
        session_id: UUID,
        *,
        run: str | None = None,
        machine: str | None = None,
        cwd: str | None = None,
    ) -> None:
        """Renew a session's short lease after an authorized inbound poll.

        The rest is where the poller says its run is, kept while the lease holds
        (:class:`PollerRun`).

        Args:
          session_id: Session whose inbound poll was authorized.
          run: The detached host's name, if the run is one.
          machine: The run's machine.
          cwd: The run's working directory.

        """
        with self._lock:
            now = self._clock()
            self._leases[session_id] = _Lease(
                expires=now + self.poller_ttl_sec,
                where=PollerRun(run=run, machine=machine, cwd=cwd),
            )
            if len(self._leases) > 1_024:
                self._sweep(now)

    def _sweep(self, now: float) -> None:
        """Drop every lease expired by ``now``. Caller holds ``_lock``."""
        self._leases = {
            sid: lease for sid, lease in self._leases.items() if lease.expires > now
        }

    def has_poller(self, session_id: UUID) -> bool:
        """Return whether an inbound poll renewed this session's lease recently.

        Args:
          session_id: Session to check.

        Returns:
          active: Whether the session's lease is unexpired.

        """
        return self.poller_run(session_id) is not None

    def poller_run(self, session_id: UUID) -> PollerRun | None:
        """Say where a session's poller runs, while its lease holds.

        Args:
          session_id: Session to check.

        Returns:
          where: What its last poll said of its run, empty when it said nothing;
            ``None`` when the lease has expired or was revoked.

        """
        with self._lock:
            lease = self._leases.get(session_id)
            if lease is None or lease.expires <= self._clock():
                _ = self._leases.pop(session_id, None)
                return None
            return lease.where

    def active_poller_ids(self) -> list[UUID]:
        """Snapshot unexpired poller sessions for database candidate filtering.

        Returns:
          session_ids: Session ids whose leases are still unexpired.

        """
        with self._lock:
            self._sweep(self._clock())
            return list(self._leases)

    # A lease is revoked as its session ends, and that session's poller is then parked
    # in a held request. Left parked, the hold keeps the exiting run -- which ends its
    # session before it stops its poller -- waiting out the whole hold.
    def forget_poller(self, session_id: UUID) -> None:
        """Revoke a session's lease and end every hold parked on it."""
        with self._lock:
            _ = self._leases.pop(session_id, None)
            for waiter in self._waiters.get(session_id, ()):
                waiter.released = True
            self._wake(session_id)

    def send_once(
        self,
        key: UUID | None,
        targets: list[tuple[UUID, Inbound]],
        *,
        fingerprint: str = "",
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

        Args:
          key: Idempotency key (UUID or None).
          targets: (session_id, message) pairs to enqueue.
          fingerprint: Names what the send says. A key that is replayed with
            another fingerprint is a different send under a used key, which is
            refused; callers that name none replay whatever the body.

        Returns:
          delivered: the session ids enqueued to (or the original receipt on
            a key replay).

        Raises:
          IdempotencyReuseError: ``key`` already sent a message with another
            fingerprint.

        """
        with self._lock:
            if key is not None:
                seen = self._seen_sends.get(key)
                if seen is not None:
                    if seen.fingerprint != fingerprint:
                        raise IdempotencyReuseError(
                            "Idempotency-Key already used for another message",
                        )
                    return list(seen.delivered)
            delivered: list[UUID] = []
            for session_id, message in targets:
                self._append_capped(session_id, message)
                delivered.append(session_id)
            if key is not None and delivered:
                self._remember(
                    key,
                    receipt=_SendReceipt(
                        delivered=tuple(delivered),
                        fingerprint=fingerprint,
                        source=targets[0][1].source,
                    ),
                )
            return delivered

    def sender_of(self, key: UUID) -> str | None:
        """Return who sent the message that first used ``key``, while it is remembered.

        Args:
          key: An idempotency key.

        Returns:
          source: The attested sender of the send ``key`` delivered; ``None`` for a
            key never used, forgotten, or used by a message without a sender.

        """
        with self._lock:
            seen = self._seen_sends.get(key)
            return None if seen is None else seen.source

    def _remember(self, key: UUID, *, receipt: _SendReceipt) -> None:
        """Retain a bounded receipt under the caller's queue lock."""
        self._seen_sends[key] = receipt
        while len(self._seen_sends) > self.max_seen_keys:
            self._seen_sends.popitem(last=False)

    def enqueue(self, session_id: UUID, message: Inbound) -> int:
        """Append ``message`` for ``session_id``; return the pending count.

        Drops the oldest message when the per-session cap is exceeded so an
        unattended session cannot leak memory.

        Args:
          session_id: Session to queue on.
          message: Inbound message (ServerMessage or similar).

        Returns:
          result: Pending message count for this session.

        """
        with self._lock:
            self._append_capped(session_id, message)
            return len(self._queues[session_id])

    # Each overflow drop loses an undelivered inbound message (the session's poller is
    # behind or gone), so WARN on it: a silent drop hides a stuck or absent ``trax run``
    # poller. Caller holds ``self._lock``.
    def _append_capped(self, session_id: UUID, message: Inbound) -> None:
        """Append ``message``, dropping the oldest over the per-session cap."""
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
        """Remove and return all pending messages for ``session_id``, oldest first.

        Args:
          session_id: Session id.

        Returns:
          result: The list[Inbound].

        """
        with self._lock:
            return list(self._queues.pop(session_id, ()))

    async def await_messages(
        self,
        session_id: UUID,
        *,
        timeout_sec: float,
    ) -> list[Inbound]:
        """Drain ``session_id``, waiting up to ``timeout_sec`` for a message.

        What a poller replaces: asking every half-second costs one request per
        session per interval whether or not anything was sent, and still
        delivers up to that interval late. Waiting costs one held request and
        returns the moment a message is enqueued.

        Returns empty at the timeout rather than holding forever -- a proxy
        will cut an idle connection anyway, and the caller needs a turn to
        notice it should stop. Returns empty at once when the session's lease
        is revoked (:meth:`forget_poller`).

        Args:
          session_id: Session to drain.
          timeout_sec: How long to wait when nothing is pending.

        Returns:
          messages: Everything queued, oldest first; empty on timeout or release.

        """
        waiter = _Waiter(loop=asyncio.get_running_loop())
        with self._lock:
            self._waiters[session_id].append(waiter)
        try:
            # Register first so an enqueue after the drain cannot miss this poll.
            pending = self.drain(session_id)
            if pending:
                return pending
            try:
                await asyncio.wait_for(waiter.event.wait(), timeout_sec)
            except TimeoutError:
                return []
        finally:
            self._release(session_id, waiter)
        # A released poller is leaving: a message drained for it now would be typed
        # into a CLI that has exited, so it stays queued instead.
        if waiter.released:
            return []
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

    # Each waiter is woken through its OWN loop: an enqueue arrives on whatever thread
    # served that request, and ``Event.set`` is not thread-safe against the loop
    # awaiting it.
    def _wake(self, session_id: UUID) -> None:
        """Wake everything waiting on ``session_id``. Caller holds ``_lock``."""
        for waiter in self._waiters.get(session_id, ()):
            if waiter.loop.is_closed():
                continue
            with suppress(RuntimeError):
                waiter.loop.call_soon_threadsafe(waiter.event.set)

    def pending(self, session_id: UUID) -> int:
        """How many messages are queued for ``session_id`` (test/inspection).

        Args:
          session_id: Session id.

        Returns:
          result: The int.

        """
        with self._lock:
            return len(self._queues.get(session_id, ()))

    def hand_out(self, messages: Sequence[Inbound], *, taker: UUID | None) -> None:
        """Remember who sent each chat line a poller took, and which key took it.

        A drained line is in no queue and not yet in its chat's records, so for the
        moment between, only this says it was posted. It is kept in memory alone: a
        restart forgets it, and so does the chat's delete. One not recorded within
        ``handed_ttl_sec`` no longer counts: its run may have stopped mid-turn, or let
        its drain go, and it would hold the chat's delete off until a restart. Over
        ``max_handed_chats`` chats, the one handed a line longest ago is forgotten.

        The key that took a chat line holds its poster's turn for ``turn_ttl_sec``
        (:meth:`took_line`), whether or not the chat has recorded it since; over
        ``max_turns``, the turn taken longest ago is forgotten.

        Args:
          messages: What one drain gave its poller.
          taker: The API key that drained them; ``None`` for a browser session, which
            holds no turn.

        """
        with self._lock:
            now = self._clock()
            for message in messages:
                context = message.context
                if context is None or context.conversation_id is None:
                    continue
                handed = self._handed.setdefault(context.conversation_id, [])
                self._handed.move_to_end(context.conversation_id)
                handed.append(
                    _Handed(source=message.source, until=now + self.handed_ttl_sec),
                )
                del handed[: -self.max_per_session]
                if taker is not None and message.source is not None:
                    turn = (taker, message.source.lower())
                    self._turns[turn] = now + self.turn_ttl_sec
                    self._turns.move_to_end(turn)
            while len(self._handed) > self.max_handed_chats:
                forgotten, _ = self._handed.popitem(last=False)
                _LOGGER.warning(
                    "taken chat lines over cap (%d chats); forgot those of chat %s",
                    self.max_handed_chats,
                    forgotten,
                )
            while len(self._turns) > self.max_turns:
                _ = self._turns.popitem(last=False)

    def took_line(self, taker: UUID, *, poster: str) -> bool:
        """Say whether ``taker`` took a chat line ``poster`` sent, within the turn TTL.

        Args:
          taker: An API key.
          poster: An attested sender, in any case.

        Returns:
          held: Whether that key drained a chat line of theirs in the last
            ``turn_ttl_sec``.

        """
        with self._lock:
            return self._turns.get((taker, poster.lower()), 0.0) > self._clock()

    def recorded(self, conversation_id: UUID, *, senders: Sequence[str]) -> None:
        """Forget one handed-out line of each sender, now its chat records it.

        Args:
          conversation_id: The science chat.
          senders: The poster of each line its session was just given.

        """
        with self._lock:
            handed = self._handed.get(conversation_id, [])
            for sender in senders:
                taken = next(
                    (
                        at
                        for at, line in enumerate(handed)
                        if (line.source or "").lower() == sender.lower()
                    ),
                    None,
                )
                if taken is not None:
                    del handed[taken]
            if not handed:
                _ = self._handed.pop(conversation_id, None)

    def conversation_senders(self, conversation_id: UUID) -> list[str | None]:
        """Name the sender of each line on its way into a conversation.

        That is each line still queued for it, on any session, and each one a poller
        took within ``handed_ttl_sec`` that its chat does not yet record.

        Args:
          conversation_id: The science chat.

        Returns:
          senders: Each such line's attested ``source``: the queued ones, oldest
            first per session, then the handed-out ones.

        """
        with self._lock:
            now = self._clock()
            return [
                *(
                    message.source
                    for queue in self._queues.values()
                    for message in queue
                    if message.context is not None
                    and message.context.conversation_id == conversation_id
                ),
                *(
                    line.source
                    for line in self._handed.get(conversation_id, ())
                    if line.until > now
                ),
            ]

    def drop_conversation(self, conversation_id: UUID) -> int:
        """Remove every line still queued for a conversation, and forget handed ones.

        Args:
          conversation_id: The science chat.

        Returns:
          dropped: How many queued lines were removed.

        """
        dropped = 0
        with self._lock:
            _ = self._handed.pop(conversation_id, None)
            for session_id in list(self._queues):
                queue = self._queues[session_id]
                kept = deque(
                    message
                    for message in queue
                    if message.context is None
                    or message.context.conversation_id != conversation_id
                )
                dropped += len(queue) - len(kept)
                self._queues[session_id] = kept
        return dropped

    def is_full(self, session_id: UUID) -> bool:
        """Say whether one more message would evict the oldest waiting one.

        Args:
          session_id: Session id.

        Returns:
          full: Whether the session's queue is at its cap.

        """
        return self.pending(session_id) >= self.max_per_session
