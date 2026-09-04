"""Event sinks for ``trax run``: local-file (default) and Trackinizer.

The session runner hands each captured chunk to a :class:`Sink`, which
normalizes it into IR records. Four exist -- two that record, two that wrap:

- :class:`FileSink` -- the default. Writes one JSON line per record to a
  local JSONL file, exactly as the harness always has. No network.
- :class:`TrackinizerSink` -- opt-in via ``--sync``. Opens a Session row
  on the server, batches records to the ingest API (retry + idempotency
  handled by the client), and closes the session on exit.
- :class:`ResilientSink` -- wraps a primary sink (the Trackinizer one) and
  degrades to a local :class:`FileSink` on its first failure, so a server
  outage never crashes the drain thread or corrupts the wrapped terminal.
- :class:`LockedSink` -- wraps any sink in one lock so the runner's drain,
  inbound-poll, and main threads serialize their ``emit`` / ``flush`` /
  ``session_id`` / ``close`` calls instead of racing (R2R-024).

A record's key is its POSITION in its file's normalized stream, not a
counter: the runner re-feeds a file the CLI rewrote (a claude compaction
does exactly that), and a derived position lands each record back where it
already was rather than appending a second copy. So the sink tracks one
counter per FILE, reset when that file restarts.

A slash command is the exception with no position at all -- it is typed
into the TUI and never written to the log -- so it travels its own way
(:meth:`Sink.emit_slash_command`) and the server numbers it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Protocol, cast, override
from uuid import UUID, uuid4

import json
import sys
import threading
import time

from trackinizer.client.client import Client
from trackinizer.lib.custom_json import JSON, json_freeze
from trackinizer.trax.run.adapters.custom_types import Adapter
from trackinizer.trax.run.adapters.tail import Tail
from trackinizer.trax.run.custom_types import Event
from trackinizer.trax.run.slash import SlashCommand
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.wire_session_ir import (
    ManifestBody,
    RecordBody,
    SlashCommandBody,
)
from trackinizer.wire.wire_sessions import SessionEnd, SessionStart


class Sink(Protocol):
    """Where captured records go.

    The runner hands over raw chunks; :meth:`feed` normalizes them and each
    record's position within its file becomes its key. That position is
    per-FILE, so a session spanning several files (claude splits on
    compaction, codex forks) numbers each from zero.

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
        """Record one captured record at its position in its part."""
        ...

    def emit_slash_command(self, command: SlashCommand, at: datetime) -> None:
        """Record one slash command the human typed.

        NOT an :class:`Event`: a command is handled inside the CLI's TUI and
        never reaches the session log, so it cannot be re-derived and holds no
        position in any part. It is stored beside the records, not among them.

        Args:
          command: The parsed verb and its arguments.
          at: The submit-time clock the keystroke detector stamped; a typed
            command has no CLI-recorded time.

        """
        ...

    def feed(
        self,
        adapter: Adapter,
        path: Path,
        raw: bytes,
        *,
        restart: bool = False,
    ) -> list[str]:
        """Normalize one captured chunk into records and record each.

        Concrete on the Protocol because normalization is identical for every
        sink -- what differs is where the records GO. One reader per PATH,
        built on first sight: a session spans several files (claude splits on
        compaction, codex forks), and a shared reader would number the second
        file's records after the first's.

        The reader comes from the ADAPTER rather than a name lookup here: the
        adapter already declares ``reader()``, so a table in this module would
        be a second registry to keep in step with ``_ADAPTERS``.

        Args:
          adapter: The CLI whose dialect ``raw`` is in; supplies the reader.
          path: The file the chunk came from; keys the normalizer.
          raw: One log line, or a whole document for a rewriting CLI.
          restart: Whether the file was REPLACED immediately before this
            chunk, which discards the reader's position -- the records that
            follow re-derive the part from its start.

        Returns:
          kinds: The record kind names this chunk produced, for the run's
            end-of-run tally.

        """
        if restart:
            # The file was rewritten, so the reader's position describes bytes
            # that no longer exist. A fresh reader re-derives from offset 0 and
            # each record lands back on the key it already held.
            _ = self.readers.pop(path, None)
        reader = self.readers.get(path)
        if reader is None:
            reader = adapter.reader()
            self.readers[path] = reader
        kinds: list[str] = []
        for record in reader.feed(raw.decode(errors="replace")):
            self.emit(adapter.name, Event(record=record, path=path, restart=restart))
            kinds.append(type(record).__name__)
        return kinds

    @property
    def readers(self) -> dict[Path, Tail]:
        """Per-file readers, one per source file this sink has seen.

        A PROPERTY over a lazily built dict rather than an attribute every
        implementation must remember to initialize: ``feed`` is concrete here
        and each sink writes its own ``__init__``, so an attribute would be a
        contract enforced only by whichever sink happened to be tested.
        """
        built = self._readers
        if built is None:
            built = {}
            self._readers = built
        return built

    _readers: dict[Path, Tail] | None = None
    """Backing store for :attr:`readers`; ``None`` until first use.

    Declared on the Protocol with a class-level default so the lazy build
    needs no ``getattr`` and every sink inherits it. The default is immutable,
    so no instance shares another's readers.
    """

    def flush(self) -> None:
        """Push any buffered events now; idempotent and safe on an empty buffer.

        Called periodically by the drain loop so a quiet session still
        streams to the server between bursts, rather than withholding events
        until ``close``.
        """
        ...

    def drain_pending(self) -> list[tuple[Path, RecordBody]]:
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


def _record_body(idx: int, event: Event) -> RecordBody:
    """The wire body for one captured record at position ``idx``.

    Built through :class:`SessionRecordRow` rather than field by field, so the
    ciphertext split and the search projection are computed in ONE place and
    the stored row cannot disagree with what the wire carried.
    """
    # ``session_id`` is the server's to assign; the row type needs one, and
    # only its projections are read here.
    row = SessionRecordRow.of(
        session_id=UUID(int=0), part=0, idx=idx, record=event.record
    )
    return RecordBody.of(row)


class FileSink(Sink):
    """Write one JSON line per record to a local JSONL file."""

    def __init__(self, handle: IO[str]) -> None:
        self._handle = handle
        self._closed = False
        # Positions per FILE, not per run: a session spans several files and
        # each is stored as its own part, numbered from zero.
        self._next_idx: dict[Path, int] = {}
        # Files whose CURRENT chunk re-derived the part from its start. The
        # restart flag rides every record of that chunk, and the reset is one
        # per chunk, so this remembers that it already happened.
        self._restarted: set[Path] = set()

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
        if event.restart and event.path not in self._restarted:
            # ONCE per rewritten chunk, not once per record. The flag rides
            # every record the chunk produced -- a restart re-reads the file,
            # so its first line yields the opening context and clear as well as
            # its turn -- and resetting on each of them numbered all three 0.
            self._restarted.add(event.path)
            self._next_idx[event.path] = 0
        elif not event.restart:
            self._restarted.discard(event.path)
        idx = self._next_idx.get(event.path, 0)
        self._next_idx[event.path] = idx + 1
        self._write_record(adapter_name, event.path, _record_body(idx, event))

    @override
    def emit_slash_command(self, command: SlashCommand, at: datetime) -> None:
        # Its own line shape, tagged so a reader can tell it from a record: it
        # belongs to no part and carries no idx.
        self._handle.write(
            json.dumps(
                {
                    "slash_command": {
                        "timestamp": at.isoformat(),
                        "command": command.command,
                        "args": command.args,
                    }
                }
            )
            + "\n"
        )

    @override
    def drain_pending(self) -> list[tuple[Path, RecordBody]]:
        # Every emit writes through immediately; nothing is ever buffered.
        return []

    def write_body(self, adapter_name: str, path: Path, body: RecordBody) -> None:
        """Write a pre-built :class:`RecordBody`, preserving its own ``idx``.

        Used to replay records stranded in a degrading server sink, so the
        local file keeps their original positions rather than re-deriving from
        this sink's counter. Advancing past the replayed ``idx`` is
        load-bearing: a later live ``emit`` would otherwise restart at 0 and
        collide with what was just replayed.
        """
        self._next_idx[path] = max(self._next_idx.get(path, 0), body.idx + 1)
        self._write_record(adapter_name, path, body)

    def _write_record(self, adapter_name: str, path: Path, body: RecordBody) -> None:
        record = cast(
            JSON,
            {
                "adapter": adapter_name,
                # The BASENAME, matching how the server resolves a part: the
                # absolute path differs across machines, so a capture replayed
                # elsewhere must still name the same file.
                "part_name": path.name,
                **body.model_dump(mode="json"),
            },
        )
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
        self._buffer: list[tuple[Path, RecordBody]] = []
        # Typed commands awaiting the next flush. Not in ``_buffer``: they
        # belong to the session rather than to any file, so they cannot be
        # grouped by path.
        self._slash: list[SlashCommandBody] = []
        self._next_idx: dict[Path, int] = {}
        # Files whose current batch re-derived the part from its start, so the
        # append overwrites rather than skipping what is stored.
        self._restarted: set[Path] = set()
        # Files whose CURRENT chunk already reset its numbering. Separate from
        # ``_restarted``, which a flush clears: the reset is once per chunk,
        # and a chunk can span flushes.
        self._renumbered: set[Path] = set()
        # One IR id per file, minted on first send: the manifest records what
        # the file declared, and a fresh id per BATCH would rewrite it.
        self._ir_ids: dict[Path, UUID] = {}
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
        if event.restart:
            self._restarted.add(event.path)
        if event.restart and event.path not in self._renumbered:
            # ONCE per rewritten chunk, not once per record. The flag rides
            # every record the chunk produced -- a restart re-reads the file,
            # so its first line yields the opening context and clear as well as
            # its turn -- and resetting on each of them numbered all three 0.
            #
            # Its own set, not ``_restarted``: that one is cleared by a flush,
            # which a chunk larger than the batch triggers mid-chunk.
            self._renumbered.add(event.path)
            self._next_idx[event.path] = 0
        elif not event.restart:
            self._renumbered.discard(event.path)
        idx = self._next_idx.get(event.path, 0)
        self._next_idx[event.path] = idx + 1
        if not self._buffer:
            self._oldest_buffered_at = self._clock()
        self._buffer.append((event.path, _record_body(idx, event)))
        if len(self._buffer) >= self._batch_size:
            self._flush()

    @override
    def emit_slash_command(self, command: SlashCommand, at: datetime) -> None:
        self._ensure_session()
        if not self._buffer and not self._slash:
            self._oldest_buffered_at = self._clock()
        self._slash.append(
            SlashCommandBody(timestamp=at, command=command.command, args=command.args)
        )

    @override
    def flush(self) -> None:
        # Time-driven partial flush: send a non-empty buffer once it has aged
        # past the interval. The drain loop calls this each poll, so a session
        # that paused mid-run still streams instead of waiting for ``close``.
        if (not self._buffer and not self._slash) or self._oldest_buffered_at is None:
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
                # Sent when already known, which is the RESUME case: the
                # server re-attaches the AgentSession whose stored id matches
                # rather than minting a second one. A fresh run has none yet
                # (the CLI has not written its file), so this is null there
                # and the id is backfilled at close instead.
                cli_session_id=self._cli_session_id,
            )
        )
        self._session_id = resp.id
        # ``resp.seq`` is deliberately unread. It continued the legacy event
        # log, which a resumed run had to seed from; a record's key is derived
        # from its position in its file, so a resumed run re-derives the same
        # keys and needs no continuation point.
        #
        # Adopt the granted routing name: the server may have suffixed it on a
        # collision (``scientist`` -> ``scientist#2``).
        self._granted_actor = resp.actor
        if resp.actor and resp.actor != self._actor:
            sys.stderr.write(
                f"[trax run] routing name '{self._actor}' was taken; "
                f"using '{resp.actor}'\n"
            )

    @override
    def drain_pending(self) -> list[tuple[Path, RecordBody]]:
        """Remove and return records buffered but not yet sent to the server.

        The escape hatch for :class:`ResilientSink`: when this sink fails and
        the wrapper degrades to a local file, the already-buffered (but
        unflushed) bodies would otherwise be lost. Each carries its own
        position, so the fallback replays them verbatim.
        """
        pending = self._buffer
        self._buffer = []
        self._oldest_buffered_at = None
        return pending

    def _flush(self) -> None:
        """Send each file's records as its own batch.

        One part per request: the server resolves a part from the file's
        basename, so records from two files cannot share a request without
        one of them landing under the wrong part.

        Buffered slash commands ride the FIRST request, so they commit in the
        same transaction as the turns around them. A batch with no records at
        all still sends them, naming no file -- a command typed before the CLI
        has written anything belongs to no part.
        """
        if self._session_id is None or not (self._buffer or self._slash):
            return
        by_path: dict[Path, list[RecordBody]] = {}
        for path, body in self._buffer:
            by_path.setdefault(path, []).append(body)
        for path, bodies in by_path.items():
            self._client.append_records(
                self._session_id,
                name=path.name,
                manifest=ManifestBody(
                    name=path.name,
                    metadata=self._metadata_for(path),
                    ir_id=self._ir_ids.setdefault(path, uuid4()),
                    format=self._cli,
                    records=self._next_idx.get(path, 0),
                ),
                records=bodies,
                restart=path in self._restarted,
                slash_commands=self._slash,
            )
            # Cleared only once the request RETURNED. A command's ``seq`` is
            # server-assigned, so a resend after a failure partway through
            # would store a second copy rather than collide -- unlike a
            # record, whose derived key makes a retry a no-op.
            self._slash = []
            self._restarted.discard(path)
            # Sent with the first part only; the rest carry records alone.
            self._buffer = [entry for entry in self._buffer if entry[0] != path]
        if self._slash:
            self._client.append_records(self._session_id, slash_commands=self._slash)
            self._slash = []
        self._buffer = []
        self._oldest_buffered_at = None

    def _metadata_for(self, path: Path) -> JSON:
        r"""How the file SPELLS its bytes, as its own reader has read it so far.

        Not decoration, and not derivable server-side: claude's ascii-escaping
        convention (a majority flag plus its exception bitmap) rides on the
        ``TurnContext`` in force, and a rewrite without it writes raw UTF-8
        where the CLI wrote ``\\u00e9``. Every record still matches, so nothing
        downstream notices -- but the bytes differ, and a provider handed a
        transcript it did not write is entitled to reject it.

        Read from the per-file reader at FLUSH time rather than captured once:
        ``ascii_escaped`` is a majority over the lines consumed, so the value
        correct for one batch may be wrong for the next, and the reader
        restates it when it moves. That is the same reason the manifest is
        re-sent per batch at all.

        A path with no reader yet -- a body replayed into a degraded sink,
        which carries positions but no reader -- declares nothing, which the
        empty default already says.
        """
        reader = self.readers.get(path)
        if reader is None:
            return json_freeze({})
        return json_freeze(reader.encoding)

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
    def emit_slash_command(self, command: SlashCommand, at: datetime) -> None:
        if self._primary is not None:
            try:
                self._primary.emit_slash_command(command, at)
                return
            except Exception as err:  # noqa: BLE001 -- any sink failure must degrade, not crash the drain thread.
                self._degrade(err)
        self._ensure_fallback().emit_slash_command(command, at)

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
    def drain_pending(self) -> list[tuple[Path, RecordBody]]:
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
        for path, body in pending:
            fallback.write_body(self._adapter_for_fallback, path, body)
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
    one non-thread-safe ``httpx2`` client (R2R-024, and the same race class as
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
    def emit_slash_command(self, command: SlashCommand, at: datetime) -> None:
        with self._lock:
            self._inner.emit_slash_command(command, at)

    @override
    def feed(
        self,
        adapter: Adapter,
        path: Path,
        raw: bytes,
        *,
        restart: bool = False,
    ) -> list[str]:
        # Held across the WHOLE chunk, not per record: the inner sink's
        # per-file position advances once per record, so another thread
        # emitting between two of them would interleave positions.
        with self._lock:
            return self._inner.feed(adapter, path, raw, restart=restart)

    @override
    def flush(self) -> None:
        with self._lock:
            self._inner.flush()

    @override
    def drain_pending(self) -> list[tuple[Path, RecordBody]]:
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
