"""Event sinks for ``trax run``: local-file (default) and Trackinizer.

The session runner produces one :class:`Event` per captured turn and hands
it to a :class:`Sink`. Four exist -- two that record, two that wrap:

- :class:`FileSink` -- the default. Writes one JSON line per event to a
  local JSONL file, exactly as the harness always has. No network.
- :class:`TrackinizerSink` -- opt-in via ``--sync``. Opens a Session row
  on the server, batches events to the ingest API (retry + idempotency
  handled by the client), and closes the session on exit.
- :class:`ResilientSink` -- wraps a primary sink (the Trackinizer one) and
  degrades to a local :class:`FileSink` on its first failure, so a server
  outage never crashes the drain thread or corrupts the wrapped terminal.
- :class:`LockedSink` -- wraps any sink in one lock so the runner's drain,
  inbound-poll, and main threads serialize their ``emit`` / ``flush`` /
  ``session_id`` / ``close`` calls instead of racing (R2R-024).

The sink mints each event's monotonic per-session ``seq`` (the counter
lives with the session, not the run), since the CLIs carry no reliable
counter and the ``(session_id, seq)`` pair is the server-side dedup key.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Protocol, cast, override
from uuid import UUID

import json
import sys
import threading
import time

from trackinizer.client.client import Client
from trackinizer.lib.custom_json import JSON
from trackinizer.trax.run.custom_types import Event
from trackinizer.wire.wire_sessions import (
    EventBody,
    SessionEnd,
    SessionStart,
)


class Sink(Protocol):
    """Where captured events go.

    The sink mints each event's ``seq``: ``seq`` is per-session and is the
    server dedup key with the session id, so the counter must live with the
    session, not with the run (a run-wide counter would mis-number a sink that
    ever opened a second session). The runner just hands over parsed events.

    Every implementation below SUBCLASSES this rather than matching it
    structurally, and marks each method ``@override``. Structural conformance
    is silent when it lapses: the degrade seam reaches ``drain_pending`` by
    name, so a rename on one sink leaves the others satisfying the Protocol
    while that one strands its buffer. Declaring the base makes the same
    mistake a type error.
    """

    @property
    def session_id(self) -> UUID | None:
        """The server session id once opened, else None (no server / not yet).

        The inbound poller needs it to drain messages for this session; a
        local-file sink never opens one, so it is None there.
        """
        ...

    def open(self) -> str | None:
        """Open the session eagerly; return the granted routing handle (or None).

        Lets the runner open before fork so the server-granted handle is in the
        child env from the start. A local-file sink has no server session and
        returns None.
        """
        ...

    def set_cli_session_id(self, cli_session_id: str) -> None:
        """Record the CLI's own session id, backfilled to the server at close.

        Lets a fresh run become resumable on its next ``--resume``. A local-file
        sink has no server session and ignores it.
        """
        ...

    def emit(self, adapter_name: str, event: Event) -> None:
        """Record one captured event, assigning it the next per-session ``seq``."""
        ...

    def flush(self) -> None:
        """Push any buffered events now; idempotent and safe on an empty buffer.

        Called periodically by the drain loop so a quiet session still
        streams to the server between bursts, rather than withholding events
        until ``close``.
        """
        ...

    def drain_pending(self) -> list[EventBody]:
        """Remove and return events buffered but not yet delivered.

        The degrade seam: when a primary sink fails, ``ResilientSink``
        replays these seq-stamped bodies into the local fallback so a flush
        failure loses nothing (REV-02). On the Protocol -- not reached via
        ``getattr`` -- so a rename is a type error instead of a silent
        buffer loss. A sink that delivers synchronously returns ``[]``.
        """
        ...

    def close(self) -> None:
        """Flush and release resources; idempotent."""
        ...


def _event_body(seq: int, event: Event) -> EventBody:
    """The wire body for one captured turn: typed message + envelope."""
    return EventBody(
        seq=seq,
        kind=event.kind,
        timestamp=event.timestamp,
        model=event.model,
        message=event.message.to_json(),
    )


class FileSink(Sink):
    """Write one JSON line per event to a local JSONL file."""

    def __init__(self, handle: IO[str]) -> None:
        self._handle = handle
        self._closed = False
        self._next_seq = 0

    @property
    @override
    def session_id(self) -> UUID | None:
        # A local-file sink opens no server session; nothing to poll inbound for.
        return None

    @override
    def open(self) -> str | None:
        # No server session, so no granted handle; the requested name stands.
        return None

    @override
    def set_cli_session_id(self, cli_session_id: str) -> None:
        # No server session to backfill; nothing to record locally.
        del cli_session_id

    @override
    def emit(self, adapter_name: str, event: Event) -> None:
        seq = self._next_seq
        self._next_seq += 1
        self._write_record(adapter_name, _event_body(seq, event))

    @override
    def drain_pending(self) -> list[EventBody]:
        # Every emit writes through immediately; nothing is ever buffered.
        return []

    def write_body(self, adapter_name: str, body: EventBody) -> None:
        """Write a pre-built :class:`EventBody`, preserving its own ``seq``.

        Used to replay events stranded in a degrading server sink (its bodies
        are already seq-stamped), so the local file keeps their original
        ordinals rather than re-minting from this sink's counter. Advancing
        ``_next_seq`` past the replayed seq is load-bearing: a later live
        ``emit`` would otherwise re-mint from 0 and collide with the replayed
        ordinals against the server's ``(session_id, seq)`` key (REV-001).
        """
        self._next_seq = max(self._next_seq, body.seq + 1)
        self._write_record(adapter_name, body)

    def _write_record(self, adapter_name: str, body: EventBody) -> None:
        record = cast(JSON, {"adapter": adapter_name, **body.model_dump(mode="json")})
        self._handle.write(json.dumps(record) + "\n")

    @override
    def flush(self) -> None:
        # The handle is opened line-buffered, so each event already reaches
        # disk on its newline; a flush is a cheap no-op kept for the Protocol.
        if not self._closed:
            self._handle.flush()

    @override
    def close(self) -> None:
        if not self._closed:
            self._handle.close()
            self._closed = True


class TrackinizerSink(Sink):
    """Open a Session, batch events to the ingest API, close on exit.

    The session opens on whichever comes first: an explicit :meth:`open` or
    the first event. The runner calls ``open`` before forking the child, so a
    live run opens eagerly -- the server-granted routing handle has to be in
    the child's environment from the start (#453). Nothing else does, so a
    caller that captures no events still leaves no empty Session row. Events
    flush on two triggers: the
    buffer reaching ``batch_size`` (a busy burst), or :meth:`flush` finding
    the buffer older than ``flush_interval_sec`` (a quiet session the drain
    loop ticks). ``close`` flushes the remainder and ends the session. Retry
    and idempotency live in the :class:`Client`.

    Without the time trigger a short session would withhold every event until
    ``close`` (Ctrl-D), so the live viewer saw nothing stream; the interval
    flush is what makes a 3-turn run appear within ~1s of each turn.
    """

    def __init__(
        self,
        client: Client,
        cli: str,
        *,
        actor: str | None = None,
        rooms: tuple[str, ...] = (),
        batch_size: int = 50,
        flush_interval_sec: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._cli = cli
        self._actor = actor
        self._rooms = rooms
        self._batch_size = batch_size
        self._flush_interval_sec = flush_interval_sec
        self._clock = clock
        self._session_id: UUID | None = None
        self._granted_actor: str | None = None
        # The CLI's own session id, discovered mid-run and backfilled at close
        # so a fresh run becomes resumable on its next ``--resume``.
        self._cli_session_id: str | None = None
        self._buffer: list[EventBody] = []
        self._next_seq = 0
        self._oldest_buffered_at: float | None = None
        self._closed = False

    @property
    def granted_actor(self) -> str | None:
        """The routing name the server granted this session (after collision
        renegotiation), or ``None`` before the session opens.
        """
        return self._granted_actor

    @property
    @override
    def session_id(self) -> UUID | None:
        """The server session id once opened (lazily, on first event)."""
        return self._session_id

    @override
    def emit(self, adapter_name: str, event: Event) -> None:
        del adapter_name  # the session already names its CLI
        self._ensure_session()
        seq = self._next_seq
        self._next_seq += 1
        if not self._buffer:
            self._oldest_buffered_at = self._clock()
        self._buffer.append(_event_body(seq, event))
        if len(self._buffer) >= self._batch_size:
            self._flush()

    @override
    def flush(self) -> None:
        # Time-driven partial flush: send a non-empty buffer once it has aged
        # past the interval. The drain loop calls this each poll, so a session
        # that paused mid-run still streams instead of waiting for ``close``.
        if not self._buffer or self._oldest_buffered_at is None:
            return
        if self._clock() - self._oldest_buffered_at >= self._flush_interval_sec:
            self._flush()

    @override
    def open(self) -> str | None:
        """Open the session now (eagerly) and return the granted routing handle.

        Lets the runner open before fork so the server-granted handle is in the
        child env from the start (#453). Idempotent -- a later first ``emit``
        sees the session already open. Returns the granted ``@actor`` name (or
        ``None`` if the session was somehow not named).
        """
        self._ensure_session()
        return self._granted_actor

    @override
    def set_cli_session_id(self, cli_session_id: str) -> None:
        """Record the CLI's own session id (discovered mid-run).

        Backfilled to the server at :meth:`close` so the session becomes
        correlatable on a later ``--resume``. The first non-empty id wins -- a
        CLI session id is stable for the run, so later files of the same run
        carry the same id.
        """
        if cli_session_id and self._cli_session_id is None:
            self._cli_session_id = cli_session_id

    def _ensure_session(self) -> None:
        if self._session_id is not None:
            return
        resp = self._client.session_start(
            SessionStart(
                cli=self._cli,
                actor=self._actor,
                rooms=list(self._rooms) or None,
                started=datetime.now(UTC),
            )
        )
        self._session_id = resp.id
        # Continue the event log where the server says: 0 for a fresh session,
        # ``max(seq)+1`` for a resumed one. Seeding here is load-bearing -- a
        # resumed run that re-minted seq 0 would collide every event against the
        # ``(session_id, seq)`` PK and the server would silently drop the log.
        self._next_seq = resp.seq
        # Adopt the granted routing name: the server may have suffixed it on a
        # collision (``scientist`` -> ``scientist#2``).
        self._granted_actor = resp.actor
        if resp.actor and resp.actor != self._actor:
            sys.stderr.write(
                f"[trax run] routing name '{self._actor}' was taken; "
                f"using '{resp.actor}'\n"
            )

    @override
    def drain_pending(self) -> list[EventBody]:
        """Remove and return events buffered but not yet sent to the server.

        The escape hatch for :class:`ResilientSink`: when this sink fails and
        the wrapper degrades to a local file, the already-buffered (but
        unflushed) bodies would otherwise be lost. They are seq-stamped, so
        the fallback replays them verbatim.
        """
        pending = self._buffer
        self._buffer = []
        self._oldest_buffered_at = None
        return pending

    def _flush(self) -> None:
        if not self._buffer or self._session_id is None:
            return
        self._client.append_events(self._session_id, self._buffer)
        self._buffer = []
        self._oldest_buffered_at = None

    @override
    def close(self) -> None:
        if self._closed:
            return
        # Flush and end *before* marking closed: a failure here must leave the
        # buffer intact so a retried ``close`` re-sends it, rather than a bare
        # ``_closed=True`` swallowing the events. ``session_end`` runs in the
        # ``finally``: a flush failure must NOT skip it, or ``ended`` stays
        # NULL forever -- ``resolve_live_sessions`` then returns the session
        # as live and the subscriber push keeps feeding a queue nobody drains
        # (a phantom session). In production ``ResilientSink.close`` catches
        # the flush failure and drains the buffer to the local fallback, so
        # ending the session here loses nothing.
        try:
            self._flush()
        finally:
            if self._session_id is not None:
                self._client.session_end(
                    self._session_id,
                    SessionEnd(
                        ended=datetime.now(UTC),
                        cli_session_id=self._cli_session_id,
                    ),
                )
        self._closed = True


class ResilientSink(Sink):
    """Wrap a primary sink and degrade to a local file on its first failure.

    Sync runs in the drain thread, so an unhandled sink error there crashes
    the thread, dumps a traceback into the wrapped CLI's live terminal, and
    silently stops all capture. This wrapper makes a sync failure non-fatal:
    the first exception from the primary sink swaps in a local
    :class:`FileSink`, warns once on stderr, and every later event flows to
    the file. The wrapped CLI never sees the failure.
    """

    def __init__(self, primary: Sink, *, fallback_path: Path) -> None:
        self._primary: Sink | None = primary
        self._fallback_path = fallback_path
        self._fallback: FileSink | None = None
        # Last adapter name seen, to label replayed buffer events on degrade
        # (the primary's buffered bodies don't carry the adapter name).
        self._adapter_for_fallback = ""

    @property
    @override
    def session_id(self) -> UUID | None:
        # The poller wants the live server session: only the primary
        # (TrackinizerSink) has one. Once degraded to the local fallback there
        # is no session, so inbound polling correctly stops.
        return self._primary.session_id if self._primary is not None else None

    @override
    def open(self) -> str | None:
        # Eager open routes through the primary server sink; the local fallback
        # has no session, so a degraded sink returns None. The runner opens the
        # sink before spawning the child CLI, so an open failure (server
        # unreachable) must degrade like emit/flush rather than abort the run.
        if self._primary is not None:
            try:
                return self._primary.open()
            except Exception as err:  # noqa: BLE001 -- an open failure degrades like an emit failure.
                self._degrade(err)
        return None

    @override
    def set_cli_session_id(self, cli_session_id: str) -> None:
        # Guarded like every other primary call: the runner invokes this from
        # the drain thread on each captured line, so an unguarded raise here
        # ends capture -- the exact failure this wrapper exists to prevent.
        if self._primary is not None:
            try:
                self._primary.set_cli_session_id(cli_session_id)
            except Exception as err:  # noqa: BLE001 -- a backfill failure degrades like an emit failure.
                self._degrade(err)

    @override
    def emit(self, adapter_name: str, event: Event) -> None:
        self._adapter_for_fallback = adapter_name
        if self._primary is not None:
            try:
                self._primary.emit(adapter_name, event)
                return
            except Exception as err:  # noqa: BLE001 -- any sink failure must degrade, not crash the drain thread.
                replayed = self._degrade(err)
            # A primary that buffers (``TrackinizerSink``) has already taken
            # this event, so ``_degrade`` just replayed it into the fallback;
            # writing it again here would record one turn twice, under two
            # seqs. A primary that raised before buffering replays nothing, so
            # this is the event's only chance to be recorded.
            if replayed:
                return
        self._ensure_fallback().emit(adapter_name, event)

    @override
    def flush(self) -> None:
        if self._primary is not None:
            try:
                self._primary.flush()
                return
            except Exception as err:  # noqa: BLE001 -- a flush failure degrades like an emit failure.
                self._degrade(err)
        if self._fallback is not None:
            self._fallback.flush()

    @override
    def drain_pending(self) -> list[EventBody]:
        # The primary's buffer is replayed into the fallback on degrade, and
        # the fallback writes through; nothing is ever pending at this layer.
        return []

    @override
    def close(self) -> None:
        if self._primary is not None:
            try:
                self._primary.close()
            except Exception as err:  # noqa: BLE001 -- close failures degrade like emit failures.
                self._degrade(err)
        if self._fallback is not None:
            self._fallback.close()

    def _degrade(self, err: Exception) -> bool:
        """Abandon the primary sink and route the rest to a local file.

        Any events the primary had buffered but not yet sent are replayed into
        the fallback first, in order, so a flush failure loses nothing (REV-02).

        Returns:
          replayed: Whether anything was replayed. ``emit`` needs this to tell
            a primary that already took the failing event (buffered, so
            replayed just now) from one that raised before taking it.

        """
        primary, self._primary = self._primary, None
        assert primary is not None, "degrade is only reachable with a live primary"
        sys.stderr.write(
            f"[trax run] sync failed ({err}); "
            f"falling back to local capture at {self._fallback_path}\n"
        )
        fallback = self._ensure_fallback()
        pending = primary.drain_pending()
        for body in pending:
            fallback.write_body(self._adapter_for_fallback, body)
        return bool(pending)

    def _ensure_fallback(self) -> FileSink:
        if self._fallback is None:
            self._fallback_path.parent.mkdir(parents=True, exist_ok=True)
            handle = self._fallback_path.open("a", buffering=1, encoding="utf-8")
            self._fallback = FileSink(handle)
        return self._fallback


class LockedSink(Sink):
    """Serialize all access to an inner sink with one lock (thread-safety).

    The runner touches one sink from three threads: the drain thread (``emit``
    / ``flush``), the inbound-poll thread (``session_id``), and the main thread
    (``close``). The :class:`Sink` Protocol is silent on thread-safety, and the
    runner's ``join(timeout=...)`` makes drain-thread ownership non-binding, so
    ``close`` (a blocking ``session_end``) could run concurrently with an
    in-flight ``emit`` / ``flush`` -- corrupting the inner sink's state or its
    one non-thread-safe ``httpx`` client (R2R-024, and the same race class as
    R2R-028/029/013). Wrapping every Protocol method in one lock makes them
    mutually exclusive, so the runner's threads serialize on the sink boundary.

    The lock is re-entrant so a method that internally calls another locked
    method (none do today) cannot self-deadlock.
    """

    # Bound on ``close``'s wait for the lock. The runner calls ``close`` after a
    # bounded watchdog join that can return with a worker still wedged inside a
    # locked ``emit`` / ``flush`` (a permanently hung server POST). Blocking on
    # the lock there would hang ``trax run`` forever; the process is exiting
    # anyway, so a close that cannot acquire the lock in time skips the locked
    # teardown rather than deadlocking.
    _CLOSE_LOCK_TIMEOUT_SEC: float = 5.0

    def __init__(self, inner: Sink) -> None:
        self._inner = inner
        self._lock = threading.RLock()

    @property
    @override
    def session_id(self) -> UUID | None:
        with self._lock:
            return self._inner.session_id

    @override
    def open(self) -> str | None:
        with self._lock:
            return self._inner.open()

    @override
    def set_cli_session_id(self, cli_session_id: str) -> None:
        with self._lock:
            self._inner.set_cli_session_id(cli_session_id)

    @override
    def emit(self, adapter_name: str, event: Event) -> None:
        with self._lock:
            self._inner.emit(adapter_name, event)

    @override
    def flush(self) -> None:
        with self._lock:
            self._inner.flush()

    @override
    def drain_pending(self) -> list[EventBody]:
        with self._lock:
            return self._inner.drain_pending()

    @override
    def close(self) -> None:
        # Non-blocking teardown: a worker wedged inside a locked ``emit`` /
        # ``flush`` (a hung server POST that outlived the join watchdog) still
        # holds the lock. Acquire with a short bound and, on failure, skip the
        # locked close rather than deadlock the exiting process.
        if not self._lock.acquire(timeout=self._CLOSE_LOCK_TIMEOUT_SEC):
            sys.stderr.write(
                "[trax run] sink close could not acquire the lock within "
                f"{self._CLOSE_LOCK_TIMEOUT_SEC:.0f}s (a worker is wedged); "
                "skipping locked teardown\n"
            )
            return
        try:
            self._inner.close()
        finally:
            self._lock.release()
