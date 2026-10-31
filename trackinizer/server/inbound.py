"""In-process inbound-message queue: world -> a live session's input.

A message addressed to a session (web chat box, another agent) is enqueued
here and drained by that session's run's poller, which injects it into
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
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import asyncio
import logging
import threading
import time

from trackinizer.server.auth import Role
from trackinizer.wire.wire_sessions import (
    CanvasMessageContext,
    InboundKind,
    RunState,
)


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

    context: CanvasMessageContext | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PollerRun:
    """What a session's poller says of its run: where it is and which CLI it runs.

    The run reports it on each drain, under the key that opened the session, so it is
    the owner's own word about their own run. Where it is is shown in Chat, never a
    grant. Which CLI it runs decides whether a chat's provider counts it live, after
    the drain's access checks, so it widens no one's access either.
    """

    run: str | None = None
    """The detached host's name, which an attach takes; ``None`` for a run in
    a terminal of its own."""

    machine: str | None = None
    """The run's machine, named as the machine host names it."""

    cwd: str | None = None
    """The run's working directory, ``~/...`` under its home directory."""

    cli: str | None = None
    """The CLI the run is, as the run names it (``claude`` or ``codex``); ``None``
    for a poller that names none: the assistant's, or a ``trax`` older than the name."""

    instance: str | None = None
    """An id the run minted for itself, the same on each of its drains, which tells one
    run from another that drains the same session; ``None`` for a poller that has none."""

    runs: bool = False
    """Whether the run takes ``run`` messages: one that does not fails to read a drain
    that carries one, and loses every message in it."""

    state: RunState = "idle"
    """What the run last reported of itself (:meth:`InboundQueue.report_state`). It
    stays through the run's later drains, and starts again at ``idle`` for a run that
    reports nothing, or a lease that lapsed."""


@dataclass(frozen=True, slots=True, kw_only=True)
class DrainRefusal:
    """Why the server last refused a drain by a run of the session's own account."""

    reason: str
    """The refusal's message, as the run was told it."""

    at: datetime
    """When; a later refusal moves it, so a reader tells a fresh one from one it saw."""


@dataclass(frozen=True, slots=True, kw_only=True)
class _Lease:
    """Until when a session counts as polled, and where its poller said it runs."""

    expires: float
    where: PollerRun

    account: str | None = None
    """Whose key drained, as the server attested it: the machine a run names is the
    run's own word, so its owner's list holds only the runs of the owner's account."""


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

    max_gone: int = 1_024
    """Most sessions whose lease ended that wait to be taken (:meth:`take_gone`); the
    one that ended longest ago is forgotten past it."""

    max_retired: int = 4_096
    """Most sessions that remember the run they replaced (:meth:`retire_run`)."""

    max_stopped: int = 4_096
    """Most sessions that remember the run their owner stopped (:meth:`stop_run`)."""

    max_places: int = 4_096
    """Most sessions that remember where their run last said it ran."""

    max_refusals: int = 4_096
    """Most sessions that remember why their run's last drain was refused."""

    _clock: Callable[[], float] = field(default=time.monotonic, repr=False)

    _leases: dict[UUID, _Lease] = field(default_factory=dict)

    # The sessions whose lease ended, by lapsing or by being revoked, since the last
    # ``take_gone``: their runs are offline, and whatever they asked is released.
    _gone: dict[UUID, None] = field(default_factory=dict)

    # The one run each session turned away on purpose: it is held off when it drains
    # again, though no lease names it.
    _retired: OrderedDict[UUID, str] = field(default_factory=OrderedDict)

    # The one run each session's owner stopped: every drain it makes is refused.
    _stopped: OrderedDict[UUID, str] = field(default_factory=OrderedDict)

    # Where each session's run last said it ran, which outlives its lease.
    _places: OrderedDict[UUID, PollerRun] = field(default_factory=OrderedDict)

    # Why each session's own run was last turned away, until a drain is let in: a run
    # refused at its first drain never goes live, and the canvas says why.
    _refusals: OrderedDict[UUID, DrainRefusal] = field(default_factory=OrderedDict)

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
    # (an older trax resumed in its place) is not shown where the last one was. The one
    # thing kept is the state a run reported, while the same run polls on.
    def mark_poller(
        self,
        session_id: UUID,
        *,
        run: str | None = None,
        machine: str | None = None,
        cwd: str | None = None,
        cli: str | None = None,
        instance: str | None = None,
        runs: bool = False,
        account: str | None = None,
    ) -> None:
        """Renew a session's short lease after an authorized inbound poll.

        The rest is what the poller says of its run, kept while the lease holds
        (:class:`PollerRun`). A lease that had lapsed before this renewal counts as
        gone (:meth:`take_gone`): the run was offline meanwhile.

        Args:
          session_id: Session whose inbound poll was authorized.
          run: The detached host's name, if the run is one.
          machine: The run's machine.
          cwd: The run's working directory.
          cli: The CLI the run is.
          instance: The run's own id.
          runs: Whether the run takes ``run`` messages.
          account: The email of the account whose key polled.

        """
        with self._lock:
            _ = self._refusals.pop(session_id, None)
            now = self._clock()
            held = self._live(session_id)
            where = PollerRun(
                run=run,
                machine=machine,
                cwd=cwd,
                cli=cli,
                instance=instance,
                runs=runs,
                state="idle"
                if held is None or instance is None or held.where.instance != instance
                else held.where.state,
            )
            self._leases[session_id] = _Lease(
                expires=now + self.poller_ttl_sec,
                where=where,
                account=account,
            )
            if run is not None or machine is not None or cwd is not None:
                self._places[session_id] = where
                self._places.move_to_end(session_id)
                while len(self._places) > self.max_places:
                    _ = self._places.popitem(last=False)
            if len(self._leases) > 1_024:
                self._sweep(now)

    # Caller holds ``_lock``.
    def _live(self, session_id: UUID) -> _Lease | None:
        """Return a session's unexpired lease; an expired one is dropped and gone."""
        lease = self._leases.get(session_id)
        if lease is None:
            return None
        if lease.expires <= self._clock():
            del self._leases[session_id]
            self._lapse(session_id, lease)
            return None
        return lease

    # Only a CLI's run is noted: the assistant polls only when it is ready for input, so
    # its lease lapses during a long tool call, while it is alive and its cards are
    # open. It releases its own cards when it closes a chat.
    def _lapse(self, session_id: UUID, lease: _Lease) -> None:
        """Note that a CLI's run went offline. Caller holds ``_lock``."""
        if lease.where.cli is None:
            return
        _ = self._gone.pop(session_id, None)
        self._gone[session_id] = None
        while len(self._gone) > self.max_gone:
            del self._gone[next(iter(self._gone))]

    def _sweep(self, now: float) -> None:
        """Drop every lease expired by ``now``, noting each gone. Caller holds ``_lock``."""
        for session_id in [
            sid for sid, lease in self._leases.items() if lease.expires <= now
        ]:
            self._lapse(session_id, self._leases.pop(session_id))

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
            lease = self._live(session_id)
            return None if lease is None else lease.where

    def last_run(self, session_id: UUID) -> PollerRun | None:
        """Say where a session's run last said it ran, though its lease is gone.

        Args:
          session_id: Session to check.

        Returns:
          where: What the last poll that named a machine, a folder or a host said;
            ``None`` when no run of the session ever did, or since a restart.

        """
        with self._lock:
            return self._places.get(session_id)

    def refuse(self, session_id: UUID, *, reason: str) -> None:
        """Keep why a drain by the session's own run was refused, until one is let in.

        Args:
          session_id: The session the run drains.
          reason: The refusal's message.

        """
        with self._lock:
            at = datetime.now(UTC)
            # Strictly later, so two refusals within one clock tick still differ.
            if (last := self._refusals.get(session_id)) is not None and at <= last.at:
                at = last.at + timedelta(microseconds=1)
            self._refusals[session_id] = DrainRefusal(reason=reason, at=at)
            self._refusals.move_to_end(session_id)
            while len(self._refusals) > self.max_refusals:
                _ = self._refusals.popitem(last=False)

    def refusal(self, session_id: UUID) -> DrainRefusal | None:
        """Say why the session's own run was last refused; ``None`` once one drained."""
        with self._lock:
            return self._refusals.get(session_id)

    def report_state(self, session_id: UUID, *, instance: str, state: RunState) -> bool:
        """Keep what a run says it is doing, while it holds its session's lease.

        Args:
          session_id: The run's session.
          instance: The run's own id, as its drains name it.
          state: What it is doing.

        Returns:
          kept: False when the session has no unexpired lease or it is another
            run's, whose report this is not.

        """
        with self._lock:
            lease = self._live(session_id)
            if lease is None or lease.where.instance != instance:
                return False
            self._leases[session_id] = replace(
                lease,
                where=replace(lease.where, state=state),
            )
            return True

    def take_gone(self) -> list[UUID]:
        """Take the sessions whose lease ended since the last call, oldest first.

        A lease ends by lapsing, which this notices for every session, or by being
        revoked. Whoever takes them releases what the runs had asked.

        Returns:
          session_ids: The sessions whose runs went offline.

        """
        with self._lock:
            self._sweep(self._clock())
            gone = list(self._gone)
            self._gone.clear()
            return gone

    def active_poller_ids(self, *, cli: str | None = None) -> list[UUID]:
        """Snapshot unexpired poller sessions for database candidate filtering.

        Args:
          cli: Keep only the sessions whose poller named this CLI; all by default.

        Returns:
          session_ids: Session ids whose leases are still unexpired.

        """
        with self._lock:
            self._sweep(self._clock())
            return [
                session_id
                for session_id, lease in self._leases.items()
                if cli is None or lease.where.cli == cli
            ]

    def held_by_another(
        self,
        session_id: UUID,
        *,
        cli: str,
        instance: str,
    ) -> bool:
        """Say whether another run of ``cli`` holds this session's lease.

        A chat has one live run at a time, so a drain by a run other than the one
        whose lease is unexpired must be refused. A lease that names no run, or
        another CLI (a run the chat no longer takes lines from), holds no one off.
        A run the server replaced (:meth:`retire_run`) is held off too, whatever
        lease the session holds now.

        Args:
          session_id: Session being drained.
          cli: The CLI of the run asking.
          instance: The id of the run asking.

        Returns:
          held: Whether an unexpired lease names the same CLI and another run, or
            the run asking was replaced.

        """
        with self._lock:
            if self._retired.get(session_id) == instance:
                return True
            held = self._live(session_id)
        return (
            held is not None
            and held.where.cli == cli
            and held.where.instance is not None
            and held.where.instance != instance
        )

    # A lease is revoked as its session ends, and that session's poller is then parked
    # in a held request. Left parked, the hold keeps the exiting run -- which ends its
    # session before it stops its poller -- waiting out the whole hold.
    def forget_poller(self, session_id: UUID) -> None:
        """Revoke a session's lease and end every hold parked on it."""
        with self._lock:
            self._revoke(session_id)

    def retire_run(self, session_id: UUID) -> None:
        """Replace a session's run: revoke its lease and turn the run away for good.

        The run keeps draining until it is told to stop, and a bare revocation would
        let its next drain take the lease again before the run that replaces it
        starts. Its id is remembered, so :meth:`held_by_another` refuses it however
        the lease stands. A session whose lease already lapsed retires the run it last
        said it ran (:meth:`last_run`), which may be alive behind a dropped network. A
        session no run ever placed retires no one.

        Args:
          session_id: The session whose run is replaced.

        """
        with self._lock:
            lease = self._leases.get(session_id)
            where = self._places.get(session_id) if lease is None else lease.where
            if where is not None and where.instance is not None:
                self._retired[session_id] = where.instance
                self._retired.move_to_end(session_id)
                while len(self._retired) > self.max_retired:
                    _ = self._retired.popitem(last=False)
            self._revoke(session_id)

    def runs_on(self, machine: str, *, account: str) -> dict[UUID, PollerRun]:
        """Return the live runs of a CLI that name ``machine``, polled by ``account``.

        Args:
          machine: The machine's name, as the runs name it.
          account: The email of the machine's owner, matched in any case.

        Returns:
          runs: Each run's session and what its last poll said of it.

        """
        with self._lock:
            self._sweep(self._clock())
            return {
                session_id: lease.where
                for session_id, lease in self._leases.items()
                if lease.where.cli is not None
                and lease.where.machine == machine
                and lease.account is not None
                and lease.account.lower() == account.lower()
            }

    # Unlike a replaced run, which only a chat's next run holds off, a stopped run is
    # refused on every session, so the drain checks it before the session's kind.
    def stop_run(self, session_id: UUID) -> None:
        """Stop a session's live run: revoke its lease and refuse its drains for good.

        Args:
          session_id: The session whose run its owner stopped.

        """
        with self._lock:
            lease = self._leases.get(session_id)
            if lease is not None and lease.where.instance is not None:
                self._stopped[session_id] = lease.where.instance
                self._stopped.move_to_end(session_id)
                while len(self._stopped) > self.max_stopped:
                    _ = self._stopped.popitem(last=False)
            self._revoke(session_id)

    def stopped(self, session_id: UUID, *, instance: str) -> bool:
        """Say whether the session's owner stopped the run of this ``instance``."""
        with self._lock:
            return self._stopped.get(session_id) == instance

    def _revoke(self, session_id: UUID) -> None:
        """Drop a session's lease, note it gone and release its holds."""
        if (lease := self._leases.pop(session_id, None)) is not None:
            self._lapse(session_id, lease)
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

    def key_sender(self, key: UUID) -> str | None:
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
    # behind or gone), so WARN on it: a silent drop hides a stuck or absent run's
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
