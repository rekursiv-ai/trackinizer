"""Run the CLI on a PTY we own, tail its session log, and write Events.

Three things run side by side:

1. The CLI, spawned on a pseudo-terminal whose master fd the wrapper holds
   (:class:`~trax.run.pty_pump.PtyPump`). Owning the master is what lets the
   server splice messages into the live session; the pump mirrors bytes both
   ways so the human still drives the native TUI (byte-transparent, though
   not literally inherited fds).

2. A drain thread that watches the adapter's session directories and emits
   events for each new log line, coping with rotation and new files.

3. When syncing, an inbound-poll thread that drains server-queued messages
   and injects them into the CLI via the pump.

When the CLI exits, the wrapper drains pending lines for a short quiesce
window, closes the sink, and exits with the CLI's status.

``--dry-run`` skips spawning the CLI and replays existing session files
(the same per-shape drain as the live path), useful for adapter development
or replaying a finished session.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import ClassVar

import argparse
import logging
import os
import shutil
import sys
import threading
import time

from trackinizer.client.client import Client
from trackinizer.lib.userdirs import state_dir
from trackinizer.trax.profile import LOCALHOST_FALLBACK_URL
from trackinizer.trax.run.adapters.base import Adapter, Event
from trackinizer.trax.run.adapters.claude import ClaudeAdapter
from trackinizer.trax.run.adapters.codex import CodexAdapter
from trackinizer.trax.run.adapters.gemini import GeminiAdapter
from trackinizer.trax.run.pty_pump import PtyPump
from trackinizer.trax.run.sink import (
    FileSink,
    LockedSink,
    ResilientSink,
    Sink,
    TrackinizerSink,
)
from trackinizer.trax.run.slash import SlashCommandDetector
from trackinizer.types.agent_session_events import SlashCommand


_logger = logging.getLogger(__name__)


# Adapter *factories*, not singletons: each run builds a fresh adapter so
# per-run parse state (e.g. codex's last ``turn_context.model``) never leaks
# into a second run in the same process (tests, a future supervisor).
_ADAPTERS: dict[str, Callable[[], Adapter]] = {
    ClaudeAdapter.name: ClaudeAdapter,
    GeminiAdapter.name: GeminiAdapter,
    CodexAdapter.name: CodexAdapter,
}


@dataclass(slots=True, kw_only=True)
class _Stats:
    """Counts events per kind for the end-of-run summary.

    Only counts: ``seq`` is minted by the sink (it is per-session, and the
    sink owns the session), so the run keeps no sequence of its own.
    """

    counts: dict[str, int] = field(default_factory=dict)

    def record(self, kind: str) -> None:
        """Tally one event of ``kind`` for the end-of-run summary."""
        self.counts[kind] = self.counts.get(kind, 0) + 1

    def render(self) -> str:
        if not self.counts:
            return "(no events captured)"
        return ", ".join(f"{k}={v}" for k, v in sorted(self.counts.items()))


@dataclass(slots=True, kw_only=True)
class RunConfig:
    """Settings for one ``trax run`` invocation."""

    cli_name: str
    """``claude`` / ``gemini`` / ``codex``; selects the adapter."""

    cli_args: tuple[str, ...] = ()
    """Passed verbatim to the wrapped CLI binary."""

    actor: str | None = None
    """Routing name for this session (``--as``); the session ``owner``.

    Defaults (in :func:`main`) to ``$AGENTNAME`` or ``Agent``. The server
    makes it unique among live sessions, so two concurrent ``--as scientist``
    runs become ``scientist`` and ``scientist#2``."""

    rooms: tuple[str, ...] = ()
    """Initial room membership (``--room``, repeatable); namespaces the session
    can be addressed within. See :attr:`AgentSession.rooms`."""

    out_path: Path | None = None
    """JSONL output file; ``None`` defaults to the XDG state dir."""

    verbose: bool = False
    """Also print one line per parsed event to stderr."""

    dry_run: bool = False
    """Skip spawning the CLI; just tail existing session files."""

    sync: bool = True
    """Forward events to a Trackinizer server instead of a local file.

    On by default: the server (URL plus auth) comes from the active trax
    profile via ``client``. Set ``False`` (CLI ``--no-sync``) to capture to
    a local JSONL file with no network.
    """

    client: Client | None = None
    """Resolved Trackinizer client for ``sync``; carries URL, actor, token.

    Built by the CLI from the active profile so ``trax run`` reaches the
    same server as every other ``trax`` verb. ``None`` means no profile was
    resolved; a ``sync`` run then falls back to the localhost default.
    """

    quiesce_seconds: float = 1.0
    """Seconds to keep draining after the CLI exits."""

    SUPPORTED_CLIS: ClassVar[tuple[str, ...]] = tuple(_ADAPTERS)


def run(config: RunConfig) -> int:
    """Run end-to-end and return the wrapped CLI's exit code.

    Blocks until the CLI exits; dry-run blocks until SIGINT and returns 0.
    """
    factory = _ADAPTERS.get(config.cli_name)
    if factory is None:
        raise ValueError(
            f"unsupported CLI {config.cli_name!r}; choose one of {sorted(_ADAPTERS)}"
        )
    adapter = factory()

    sink = _open_sink(config, adapter)
    stats = _Stats()
    try:
        if config.dry_run:
            rc = _dry_run_drain(config, adapter, sink, stats)
        else:
            rc = _spawn_and_drain(config, adapter, sink, stats)
    finally:
        sink.close()
        # Close the sync client this run owns: the sink flushes and ends the
        # session but never owns the transport, so without this the run's
        # ``httpx.Client`` (and its pooled sockets) would leak. ``close`` is
        # idempotent, so a client shared in by a test or reused elsewhere is
        # not harmed beyond this run, which is the client's last user.
        if config.client is not None:
            config.client.close()

    sys.stderr.write(f"\n[trax run] {stats.render()}\n")
    return rc


def _open_trackinizer_sink(config: RunConfig, adapter: Adapter) -> Sink:
    """Build a fault-tolerant :class:`TrackinizerSink` for ``sync`` runs.

    The client carries the active profile's URL, actor, and token, so the
    sync reaches the same authenticated server as every other ``trax`` verb.
    A run without a resolved profile falls back to the localhost default.

    The sink is wrapped in a :class:`ResilientSink`: a server failure must
    not crash the drain thread or corrupt the wrapped CLI's terminal, so the
    run degrades to a local JSONL file instead.
    """
    client = config.client or Client(base_url=LOCALHOST_FALLBACK_URL)
    sys.stderr.write(f"[trax run] syncing events to {client.base_url}\n")
    return ResilientSink(
        TrackinizerSink(client, adapter.name, actor=config.actor, rooms=config.rooms),
        fallback_path=_default_out_path(adapter.name),
    )


def _default_out_path(adapter_name: str) -> Path:
    """Default JSONL path under ``$XDG_STATE_HOME/trax/run/``.

    One file per invocation; the name carries the adapter and start
    timestamp so concurrent runs don't collide and recent captures are
    easy to find. The caller creates the parent and writes the file.
    """
    stamp = time.strftime("%Y%m%dT%H%M%S")
    base = state_dir("trax") / "run"
    return base / f"{stamp}-{adapter_name}.jsonl"


def _open_sink(config: RunConfig, adapter: Adapter) -> Sink:
    """Open the run's sink: Trackinizer when syncing, else local JSONL.

    Sync is the default. ``--out`` (explicit local file) and ``--dry-run``
    (offline replay) force local capture, so neither needs a reachable
    server.
    """
    # Every run's sink is touched by the drain, inbound-poll, and main threads,
    # so wrap it in a LockedSink to serialize their access (R2R-024). The
    # dry-run / local-file paths run single-threaded today, but the lock keeps
    # the sink boundary uniformly thread-safe and is effectively free there.
    if config.sync and config.out_path is None and not config.dry_run:
        return LockedSink(_open_trackinizer_sink(config, adapter))
    out_path = config.out_path or _default_out_path(adapter.name)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Line-buffered so an interrupted run still leaves parseable lines.
    handle = out_path.open("a", buffering=1, encoding="utf-8")
    if config.out_path is None:
        sys.stderr.write(f"[trax run] capturing events to {out_path}\n")
    return LockedSink(FileSink(handle))


def _spawn_and_drain(
    config: RunConfig,
    adapter: Adapter,
    sink: Sink,
    stats: _Stats,
) -> int:
    """Run the CLI on a PTY while daemon threads drain logs and inject inbound.

    The CLI runs on a pseudo-terminal the wrapper owns (the pump), so the
    server can splice messages in while the human drives the native TUI. The
    drain thread reads the filesystem directly so it catches session files
    created after the wrapper starts; the live path needs no ``tail``
    subprocess. When syncing, an inbound-poll thread injects server-queued
    messages through the pump.

    To avoid sweeping in unrelated concurrent sessions (these CLIs share one
    session root), we snapshot the files that exist *before* spawning and
    drain only files this run creates afterward.
    """
    baseline = _existing_session_files(adapter)
    # Captured right after the pre-fork baseline snapshot: any session file
    # whose mtime predates this is an earlier run's, so the drain skips it even
    # if it raced past the baseline (#283 cross-pollination).
    spawn_time = time.time()
    stop = threading.Event()

    # Slash-commands the human types (``/exit``) are handled inside the CLI and
    # never logged, so the drain thread can't see them. The pump tees the
    # human's keystrokes into a detector (main thread); detected commands queue
    # here and the drain thread -- the single sink writer -- emits them, so they
    # serialize with file-sourced events rather than racing the sink.
    slash_queue: deque[tuple[SlashCommand, datetime]] = deque()
    detector = SlashCommandDetector(
        lambda command, at: slash_queue.append((command, at))
    )

    def _drain_loop() -> None:
        _drain_filesystem_loop(
            adapter,
            sink,
            stats,
            config,
            stop,
            baseline=baseline,
            slash_queue=slash_queue,
            spawn_time=spawn_time,
        )

    drain_thread = threading.Thread(target=_drain_loop, daemon=True)
    drain_thread.start()

    # Resolve the binary before forking: after ``pty.fork`` a missing binary
    # would fail in the child's ``execvp``, not here, so the parent could not
    # turn it into a clean ``SystemExit``.
    if shutil.which(adapter.cli_binary) is None:
        stop.set()
        drain_thread.join(timeout=1.0)
        raise SystemExit(f"trax run: {adapter.cli_binary} not found in PATH")

    argv: list[str] = [adapter.cli_binary, *config.cli_args]
    # Open the session eagerly (before fork) so the server-granted routing
    # handle is in the child env from the start: an agent inside must know its
    # real address (``scientist#2`` on a collision), not the requested name
    # (#453). A local-file / dry-run sink has no server session and returns
    # None, so the requested ``config.actor`` is exported unchanged.
    granted_actor = sink.open()
    # Run the CLI on a PTY we own rather than letting it inherit the terminal:
    # owning the master fd is what lets the server splice messages into the
    # live session (the human still drives the native TUI). Byte-transparent,
    # so display fidelity matches running the CLI directly. Export the
    # session's routing identity so an agent inside can address peers
    # (``trax send @other``) and know which rooms it is reachable in.
    pump = PtyPump(
        argv,
        env=_routing_env(config, granted_actor=granted_actor),
        on_input=detector.feed,
    )

    # Poll the server for inbound messages and inject them into the live CLI.
    # Only when syncing: a local-file run has no server session to poll.
    poll_thread: threading.Thread | None = None
    if config.sync and config.client is not None:
        poll_thread = threading.Thread(
            target=_inbound_poll_loop,
            args=(config.client, sink, pump, stop),
            daemon=True,
        )
        poll_thread.start()

    rc = pump.run()

    # Let the drain thread finish pending reads before stopping it.
    time.sleep(config.quiesce_seconds)
    stop.set()
    # Join with a generous bound so the worker threads normally stop before
    # ``run`` calls ``sink.close``: an unbounded-feeling wait lets a slow drain
    # (a retrying server POST) finish rather than racing ``close`` (R2R-024).
    # The watchdog caps the worst case -- a permanently wedged POST -- so the
    # process can never hang forever; the LockedSink's ``close`` then acquires
    # its lock with a short timeout and skips locked teardown rather than
    # deadlocking against a straggler that outlived the watchdog.
    _join_with_watchdog(drain_thread, "drain")
    if poll_thread is not None:
        _join_with_watchdog(poll_thread, "inbound poll")
    return rc


def _join_with_watchdog(
    thread: threading.Thread, name: str, *, watchdog_sec: float = 30.0
) -> None:
    """Join ``thread`` with a generous bound, warning if it outlives the watchdog.

    A too-short ``join`` made thread ownership non-binding, so ``sink.close``
    could race an in-flight ``emit`` / ``flush`` (R2R-024). This waits up to
    ``watchdog_sec`` so the worker normally stops first, then logs and returns if
    the thread is still alive (a wedged blocking call) rather than hanging ``trax
    run`` forever; ``LockedSink.close`` then declines to block on the lock that
    straggler still holds. The default is generous enough that a normal in-flight
    flush (a retrying client POST) completes, but bounded so a permanently wedged
    network call cannot hang the process.
    """
    thread.join(timeout=watchdog_sec)
    if thread.is_alive():
        sys.stderr.write(
            f"[trax run] {name} thread did not stop within "
            f"{watchdog_sec:.0f}s; proceeding to close\n"
        )


def _inbound_poll_loop(
    client: Client,
    sink: Sink,
    pump: PtyPump,
    stop: threading.Event,
    *,
    poll_interval: float = 0.5,
) -> None:
    """Drain server-queued inbound messages and inject them into the CLI.

    Waits for the sink to open the session (its id is minted lazily on the
    first captured event), then polls ``drain_inbound`` and feeds each message
    to the pump. Server errors are swallowed -- a flaky back-channel must not
    crash the run or corrupt the terminal, exactly like the capture sink's
    resilience.
    """
    warned = False
    while not stop.is_set():
        try:
            session_id = sink.session_id
            if session_id is not None:
                for text, source, room in client.drain_inbound(session_id):
                    pump.inject(_render_inbound(text, source, room))
                warned = False  # recovered; allow a fresh warning next outage
        except Exception:
            # Warn once per outage on stderr (mirrors the capture sink's
            # degrade banner) so a token expiry / network drop is visible,
            # not a silent stop; repeats stay at DEBUG to avoid flooding.
            if not warned:
                sys.stderr.write(
                    "[trax run] inbound polling failed; messages will not be "
                    "delivered until it recovers\n"
                )
                warned = True
            _logger.debug("inbound poll failed", exc_info=True)
        if stop.wait(poll_interval):
            break


def _render_inbound(text: str, source: str | None, room: str | None) -> str:
    """Decorate an inbound message with its routing context for injection.

    A single PTY interleaves every room's messages into one input stream, so
    the agent needs the room and sender to know who is steering it. Renders
    ``[room] sender: text`` (dropping whichever of room/sender is absent), so a
    direct session-id enqueue with no attested sender injects the bare text.
    """
    prefix = ""
    if room:
        prefix += f"[{room}] "
    if source:
        prefix += f"{source}: "
    return f"{prefix}{text}"


def _routing_env(
    config: RunConfig, *, granted_actor: str | None = None
) -> dict[str, str]:
    """The routing identity to export into the wrapped CLI's environment.

    ``TRAX_ACTOR`` is the session's granted routing handle and ``TRAX_ROOMS``
    its comma-joined rooms, so an agent inside the session can address peers
    (``trax send @other``) and tell them its real address.

    ``granted_actor`` is the server-granted handle, known on the sync path
    because the session opens eagerly (before fork). On a collision the server
    routes the session as ``scientist#2``; the child must see that, not the
    requested ``scientist`` (#453). When ``None`` -- a ``--no-sync`` / ``--out``
    / ``--dry-run`` run with no collision arbiter -- the requested
    ``config.actor`` is exported as-is.
    """
    env: dict[str, str] = {}
    actor = granted_actor or config.actor
    if actor:
        env["TRAX_ACTOR"] = actor
    if config.rooms:
        env["TRAX_ROOMS"] = ",".join(config.rooms)
    return env


def _existing_session_files(adapter: Adapter) -> frozenset[Path]:
    """Snapshot the matching session files present before the run starts.

    Files in this set belong to earlier or concurrent sessions; the drain
    loop ignores them so this run captures only the session it spawned.
    """
    found: set[Path] = set()
    for session_dir in adapter.session_dirs():
        if not session_dir.is_dir():
            continue
        for path in session_dir.rglob("*"):
            if path.is_file() and adapter.matches_session_file(path):
                found.add(path)
    return frozenset(found)


def _drain_filesystem_loop(
    adapter: Adapter,
    sink: Sink,
    stats: _Stats,
    config: RunConfig,
    stop: threading.Event,
    *,
    baseline: frozenset[Path],
    slash_queue: deque[tuple[SlashCommand, datetime]],
    spawn_time: float,
) -> None:
    """Poll the adapter's session dirs and emit events for newly-appended lines.

    Rescanning each poll picks up files created after the wrapper started,
    the common case since these CLIs make a fresh session file per run. Each
    tracked file keeps a byte offset; each poll reads the new slice and feeds
    completed lines to the adapter. ``baseline`` (files present at startup)
    is skipped so concurrent unrelated sessions are not swept in. After
    ``stop`` is set, one final sweep flushes anything written between the
    last poll and exit.

    ``slash_queue`` carries slash-commands the pump's keystroke detector
    produced on the main thread; this loop owns the sink, so it drains and
    emits them here rather than have the pump thread touch the sink. Queued
    commands are drained *before* the file scan each tick: the human typed them
    before the CLI wrote whatever the scan now sees, so emitting them first
    keeps the captured order matching the real order (a ``/model`` precedes the
    turns it reconfigures).
    """
    offsets: dict[Path, int] = {}
    buffers: dict[Path, bytearray] = {}
    stamps: dict[Path, tuple[int, int]] = {}
    poll_interval = 0.2

    while not stop.is_set():
        _drain_tick(
            adapter,
            sink,
            stats,
            config,
            slash_queue,
            offsets=offsets,
            buffers=buffers,
            baseline=baseline,
            stamps=stamps,
            spawn_time=spawn_time,
        )
        if stop.wait(poll_interval):
            break
    _drain_tick(
        adapter,
        sink,
        stats,
        config,
        slash_queue,
        offsets=offsets,
        buffers=buffers,
        baseline=baseline,
        stamps=stamps,
        spawn_time=spawn_time,
    )


def _drain_tick(
    adapter: Adapter,
    sink: Sink,
    stats: _Stats,
    config: RunConfig,
    slash_queue: deque[tuple[SlashCommand, datetime]],
    *,
    offsets: dict[Path, int],
    buffers: dict[Path, bytearray],
    baseline: frozenset[Path],
    stamps: dict[Path, tuple[int, int]],
    spawn_time: float,
) -> None:
    """Run one drain pass, swallowing any unhandled error to survive (R-57).

    The drain runs on a daemon thread; an unhandled error here (a transient
    sink flush failure, a filesystem stat race) would kill the thread and
    silently stop all capture for the rest of the run. Catching, logging with
    a traceback, and returning keeps the loop polling so a later turn still
    lands. Each pass emits queued slash-commands before the file scan so the
    captured order matches the real order, then flushes so a quiet session
    still streams between bursts.
    """
    try:
        _emit_slash_commands(adapter, sink, stats, config, slash_queue)
        _scan_and_read(
            adapter,
            sink,
            stats,
            config,
            offsets,
            buffers=buffers,
            baseline=baseline,
            stamps=stamps,
            spawn_time=spawn_time,
        )
        sink.flush()
    except Exception:
        _logger.warning("trax run: drain pass failed; continuing", exc_info=True)


def _emit_slash_commands(
    adapter: Adapter,
    sink: Sink,
    stats: _Stats,
    config: RunConfig,
    slash_queue: deque[tuple[SlashCommand, datetime]],
) -> None:
    """Drain queued slash-commands into the sink as captured turns.

    Runs on the drain thread (the single sink writer). ``popleft`` on a
    ``deque`` is atomic, so it needs no lock against the pump thread's
    ``append``; draining until ``IndexError`` empties whatever the human typed
    since the last tick. Each command carries the submit-time clock the
    detector stamped, since a typed command has no CLI-recorded timestamp.
    """
    while True:
        try:
            command, submitted_at = slash_queue.popleft()
        except IndexError:
            break
        event = Event(message=command, timestamp=submitted_at)
        stats.record(event.kind)
        sink.emit(adapter.name, event)
        if config.verbose:
            sys.stderr.write(f"[trax run] {adapter.name}: {event.kind}\n")


def _scan_and_read(
    adapter: Adapter,
    sink: Sink,
    stats: _Stats,
    config: RunConfig,
    offsets: dict[Path, int],
    *,
    buffers: dict[Path, bytearray],
    baseline: frozenset[Path],
    stamps: dict[Path, tuple[int, int]] | None = None,
    spawn_time: float = 0.0,
    mtime_grace_sec: float = 2.0,
) -> None:
    """Walk the session dirs once, draining each file this run created.

    Two filters identify *this run's* file, positively (#283), so a concurrent
    run's session is never swept in:

    * ``baseline`` -- files present before spawn are skipped (earlier/concurrent
      sessions).
    * ``spawn_time`` -- a file whose mtime predates this run's spawn belongs to
      an earlier run, even if the pre-fork ``baseline`` snapshot raced past it
      (the cross-pollination window: run B snapshots before run A's file is
      written, so A is absent from B's baseline; A's mtime still predates B's
      spawn because A started first). ``0.0`` disables the floor (legacy
      callers / tests that only exercise the baseline path).

    ``stamps`` carries the per-file ``(size, mtime_ns)`` change-detection state
    for whole-file adapters; ``offsets`` carries the byte offset for line
    adapters. A run uses exactly one (``adapter.whole_file`` is fixed), so the
    unused dict stays empty.
    """
    if stamps is None:
        stamps = {}
    for session_dir in adapter.session_dirs():
        if not session_dir.is_dir():
            continue
        for path in session_dir.rglob("*"):
            if path in baseline:
                continue
            if not path.is_file() or not adapter.matches_session_file(path):
                continue
            # Positive per-run id: a file older than this run's spawn belongs to
            # an earlier run the baseline snapshot raced past. Skip it. The
            # grace margin absorbs filesystem mtime granularity (some report
            # whole-second mtimes, truncating a just-created file below the
            # sub-second wall clock) and minor clock skew; an earlier run's file
            # is reliably many seconds older (CLI boot takes seconds), so the
            # grace never re-opens the cross-pollination window.
            # ``mtime_grace_sec`` absorbs whole-second filesystem mtime
            # granularity and minor clock skew, so a just-created file is never
            # wrongly rejected; an earlier run's file is many seconds older (CLI
            # boot takes seconds), so the grace never re-opens the window.
            if spawn_time and path.stat().st_mtime < spawn_time - mtime_grace_sec:
                continue
            # Backfill the CLI's own session id (the file names it) so a fresh
            # run becomes resumable on its next ``--resume``. First non-empty
            # id wins; the sink ignores it on a local-file run.
            cli_session_id = adapter.session_id_from_path(path)
            if cli_session_id is not None:
                sink.set_cli_session_id(cli_session_id)
            _drain_file(
                path,
                adapter,
                sink,
                stats,
                config,
                offsets=offsets,
                buffers=buffers,
                stamps=stamps,
            )


def _drain_file(
    path: Path,
    adapter: Adapter,
    sink: Sink,
    stats: _Stats,
    config: RunConfig,
    *,
    offsets: dict[Path, int],
    buffers: dict[Path, bytearray],
    stamps: dict[Path, tuple[int, int]],
) -> None:
    """Emit events for one file's new content, by the adapter's drain shape.

    Whole-file adapters (gemini rewrites one JSON object in place) get the
    entire body re-read on each change; line adapters (claude / codex append
    JSONL) follow a byte offset and emit per newline-terminated line.
    """
    if adapter.whole_file:
        _drain_whole_file(path, adapter, sink, stats, config, stamps=stamps)
        return
    _drain_appended_lines(
        path, adapter, sink, stats, config, offsets=offsets, buffers=buffers
    )


def _drain_whole_file(
    path: Path,
    adapter: Adapter,
    sink: Sink,
    stats: _Stats,
    config: RunConfig,
    *,
    stamps: dict[Path, tuple[int, int]],
) -> None:
    """Re-read a whole-file session and parse its full body when it changes.

    ``stamps`` records the last seen ``(size, mtime_ns)`` per file; an
    unchanged stamp skips the re-read so an idle file is not reparsed every
    poll. Tracking mtime alongside size is what catches a same-length rewrite
    (a gemini in-place edit to identical byte size), which a size-only check
    would silently drop.
    """
    try:
        info = path.stat()
    except OSError:
        return
    stamp = (info.st_size, info.st_mtime_ns)
    if stamp == stamps.get(path):
        return
    try:
        body = path.read_bytes()
    except OSError:
        return
    stamps[path] = stamp
    if body.strip():
        _process_chunk(body, adapter, sink, stats, config, whole_file=True)


def _drain_appended_lines(
    path: Path,
    adapter: Adapter,
    sink: Sink,
    stats: _Stats,
    config: RunConfig,
    *,
    offsets: dict[Path, int],
    buffers: dict[Path, bytearray],
) -> None:
    """Read newly-appended bytes from one file and emit per newline line."""
    try:
        size = path.stat().st_size
    except OSError:
        return
    last = offsets.get(path, 0)
    if size < last:
        # Shrunk means rotated or truncated; restart from the new end.
        last = 0
    if size == last:
        return
    try:
        with path.open("rb") as fh:
            fh.seek(last)
            chunk = fh.read(size - last)
    except OSError:
        return
    offsets[path] = last + len(chunk)
    buf = buffers.setdefault(path, bytearray())
    buf.extend(chunk)
    while True:
        nl = buf.find(b"\n")
        if nl < 0:
            return
        line = bytes(buf[:nl])
        del buf[: nl + 1]
        if not line.strip():
            continue
        _process_chunk(line, adapter, sink, stats, config, whole_file=False)


def _process_chunk(
    raw: bytes,
    adapter: Adapter,
    sink: Sink,
    stats: _Stats,
    config: RunConfig,
    *,
    whole_file: bool,
) -> None:
    """Parse one chunk into events and emit each; never let a parser bug abort.

    ``parse`` runs untrusted CLI bytes through adapter code in a daemon drain
    thread. An unguarded raise (e.g. a malformed turn failing an
    ``AssistantMessage`` invariant) would kill the thread and silently stop
    capture, so a parser failure logs and skips the chunk instead.
    """
    try:
        events = tuple(adapter.parse(raw, whole_file=whole_file))
    except Exception:
        # ``exc_info`` so the swallowed failure carries a traceback: without it
        # a malformed-output loss is an opaque one-liner with no clue which
        # adapter path raised (K6-004).
        _logger.warning(
            "trax run: %s adapter failed to parse a chunk",
            adapter.name,
            exc_info=True,
        )
        return
    for event in events:
        # The sink mints the per-session seq and owns serialization (the
        # frozen payload is unfrozen at the sink boundary); the run only tallies.
        stats.record(event.kind)
        sink.emit(adapter.name, event)
        if config.verbose:
            sys.stderr.write(f"[trax run] {adapter.name}: {event.kind}\n")


def _dry_run_drain(
    config: RunConfig,
    adapter: Adapter,
    sink: Sink,
    stats: _Stats,
    *,
    stop: threading.Event | None = None,
) -> int:
    """The ``--dry-run`` loop: replay existing session files until Ctrl-C.

    Polls the adapter's session dirs with the same per-shape dispatch as the
    live drain (:func:`_scan_and_read`), so a whole-file adapter (gemini) is
    re-read whole and a line adapter follows byte offsets -- unlike the old
    ``tail -F`` path, which line-split every adapter and so could never parse a
    whole-file JSON body. The baseline is empty (dry-run *replays* existing
    files, the point of an offline session review).

    ``stop`` ends the loop (tests inject it); in a real run it is ``None`` and
    a ``KeyboardInterrupt`` (Ctrl-C) ends it instead. Either way a final sweep
    flushes anything written since the last poll.
    """
    stop = stop or threading.Event()
    offsets: dict[Path, int] = {}
    buffers: dict[Path, bytearray] = {}
    stamps: dict[Path, tuple[int, int]] = {}
    sys.stderr.write("[trax run] dry-run: replaying session files; Ctrl-C to stop\n")
    try:
        while not stop.is_set():
            _scan_and_read(
                adapter,
                sink,
                stats,
                config,
                offsets,
                buffers=buffers,
                baseline=frozenset(),
                stamps=stamps,
            )
            sink.flush()
            if stop.wait(0.2):
                break
    except KeyboardInterrupt:
        pass
    # Final sweep so a file written between the last poll and stop still lands.
    _scan_and_read(
        adapter,
        sink,
        stats,
        config,
        offsets,
        buffers=buffers,
        baseline=frozenset(),
        stamps=stamps,
    )
    sink.flush()
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Argument parser for ``trax run``."""
    parser = argparse.ArgumentParser(
        prog="trax run",
        description=(
            "Wrap an agent CLI and tail its session log. "
            f"Supported: {', '.join(RunConfig.SUPPORTED_CLIS)}."
        ),
    )
    parser.add_argument(
        "cli",
        choices=RunConfig.SUPPORTED_CLIS,
        help="Which CLI to wrap.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="JSONL output file. Default: parsed events go to stderr.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Also print one line per parsed event to stderr.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip spawning the CLI; replay existing session files until Ctrl-C.",
    )
    parser.add_argument(
        "--no-sync",
        dest="sync",
        action="store_false",
        help="Capture to a local JSONL file instead of syncing to the server.",
    )
    parser.add_argument(
        "--as",
        dest="actor",
        default=None,
        help=(
            "Routing name for this session (its owner). Other agents and the "
            "web UI address it as @<name>. Defaults to $AGENTNAME or 'Agent'; "
            "made unique among live sessions (suffixed on collision)."
        ),
    )
    parser.add_argument(
        "--room",
        dest="rooms",
        action="append",
        default=None,
        metavar="ROOM",
        help=(
            "Namespace this session joins; address it as @<name>:<room>. "
            "Repeatable to join several rooms."
        ),
    )
    return parser


def main(
    argv: Sequence[str], *, client_factory: Callable[[], Client] | None = None
) -> int:
    """Entry point for ``trax run``, called from ``trax/cli.py``.

    Args:
      argv: ``trax run`` arguments; everything after ``--`` goes to the CLI.
      client_factory: Builds the Trackinizer client from the active profile.
        Invoked only when the run actually syncs, so a ``--no-sync`` or
        ``--out`` capture never resolves a profile or opens a socket.

    Returns:
      exit_code: The wrapped CLI's exit status.

    """
    if "--" in argv:
        idx = argv.index("--")
        trax_argv = argv[:idx]
        cli_argv = argv[idx + 1 :]
    else:
        trax_argv = argv
        cli_argv = ()
    parser = build_parser()
    args = parser.parse_args(trax_argv)
    syncing = args.sync and args.out is None and not args.dry_run
    config = RunConfig(
        cli_name=args.cli,
        cli_args=tuple(cli_argv),
        actor=args.actor or os.environ.get("AGENTNAME", "Agent"),
        rooms=tuple(args.rooms or ()),
        out_path=args.out,
        verbose=args.verbose,
        dry_run=args.dry_run,
        sync=args.sync,
        client=client_factory() if syncing and client_factory else None,
    )
    return run(config)
