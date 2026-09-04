"""Run the CLI on a PTY we own, tail its session log, and write Events.

Three things run side by side:

1. The CLI, spawned on a pseudo-terminal whose master fd the wrapper holds
   (:class:`~trackinizer.lib.posix.relay.ThreadedRelay`). Owning the master is what
   lets the server splice messages into the live session; the relay mirrors
   bytes both ways so the human still drives the native TUI (byte-transparent,
   though not literally inherited fds).

2. A drain thread that watches the adapter's session directories and emits
   events for each new log line, coping with rotation and new files.

3. When syncing, an inbound-poll thread that drains server-queued messages
   and types them into the CLI via the relay.

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
from functools import partial
from pathlib import Path
from typing import ClassVar, Final, cast

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
import sys
import threading
import time

from trackinizer.client.client import Client
from trackinizer.lib.posix.follow import follow_dir, follow_tree
from trackinizer.lib.posix.relay import ThreadedRelay
from trackinizer.lib.userdirs import state_dir
from trackinizer.trax.profile import LOCALHOST_FALLBACK_URL
from trackinizer.trax.run.adapters.claude import ClaudeAdapter
from trackinizer.trax.run.adapters.codex import CodexAdapter
from trackinizer.trax.run.adapters.custom_types import Adapter, StreamAdapter
from trackinizer.trax.run.adapters.gemini import GeminiAdapter
from trackinizer.trax.run.adapters.iostream import IOStreamAdapter, LineCapture
from trackinizer.trax.run.sink import (
    FileSink,
    LockedSink,
    ResilientSink,
    Sink,
    TrackinizerSink,
)
from trackinizer.trax.run.slash import SlashCommand, SlashCommandDetector


_logger = logging.getLogger(__name__)

# Cap on stream events buffered between the relay's IO path and the drain
# thread. At the 16KB/line clamp this bounds worst-case retention to ~128MB;
# in practice lines are small and 8k events is minutes of typical output.
_STREAM_QUEUE_MAX: Final = 8_192

# How long to wait before rebuilding a session-log watch that failed. The
# causes are external and transient (an inotify instance or watch limit hit
# while another process churns directories), so retrying is right -- but a
# persistent refusal must not become a hot loop.
_WATCH_REARM_SEC: Final = 0.2

# How long the runner waits for the drain thread to arm its watch before
# spawning the CLI anyway. Arming is a walk plus one syscall per directory, so
# this is orders of magnitude of headroom; it exists so a pathological tree
# cannot wedge the launch outright.
_ARM_TIMEOUT_SEC: Final = 10.0

# How long a slash-command or stream event may sit before the drain emits it.
# The FILES need no interval -- the kernel names them -- but these two queues
# are filled by other threads and carry no wakeup of their own.
_QUEUE_DRAIN_SEC: Final = 0.05

# How long each inbound request asks the server to hold. Must not exceed the
# route's own ceiling, or the server returns first and the extra is wasted.
# Longer means fewer re-arms; it does not affect delivery latency, which is
# whenever the message is enqueued.
_INBOUND_WAIT_SEC: Final = 25.0

# Total time the worker threads get to stop before the runner proceeds to
# ``sink.close``. Shared across every join rather than granted per thread: two
# sequential 30s budgets plus the sink's own 5s lock timeout made a wedged exit
# take 65s, which reads as a hang. One deadline bounds the whole teardown.
#
# It has to exceed ``_INBOUND_WAIT_SEC``: the poll thread only re-checks
# ``stop`` between requests, so on a perfectly healthy exit it can still be
# parked in one for that long, and a shorter budget would warn every time.
_JOIN_DEADLINE_SEC: Final = 30.0


# The part name a PTY scrape is stored under. A stream adapter follows no
# file, but every record still belongs to a part and the server resolves one
# from a basename -- so the stream names itself. A constant rather than a
# per-run name: a resumed scrape must append to the part it already has.
_STREAM_PART: Final = Path("stream")


# Adapter *factories*, not singletons: each run builds a fresh adapter so
# per-run parse state (e.g. codex's last ``turn_context.model``) never leaks
# into a second run in the same process (tests, a future supervisor).
_ADAPTERS: dict[str, Callable[[], Adapter]] = {
    ClaudeAdapter.name: ClaudeAdapter,
    GeminiAdapter.name: GeminiAdapter,
    CodexAdapter.name: CodexAdapter,
    IOStreamAdapter.name: IOStreamAdapter,
}


@dataclass(frozen=True, slots=True, kw_only=True)
class _Captured:
    """One line read off a session file, and how it was produced.

    ``restart`` marks the first line after the file was REPLACED rather than
    appended to -- a claude compaction rewrites the transcript, keeping the
    turns it did not summarize away. The record stored at a given position may
    therefore have CHANGED, so such a batch overwrites rather than skipping.
    """

    path: Path
    raw: bytes
    restart: bool = False


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


@dataclass(frozen=True, slots=True, kw_only=True)
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
    """JSONL output file; ``None`` defaults to the `usersdirs.state_dir()`."""

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

    resume_path: Path | None = None
    """A materialized transcript this run continues, exempt from ``baseline``.

    A resumed run's file is written BEFORE the CLI starts, so the pre-spawn
    snapshot would classify it as a previous session's and never capture it --
    and the follower would seed it at EOF, skipping every turn it already
    holds. Naming it here exempts it from both: it is followed from offset 0,
    so the stored records re-derive to the keys they already have and the
    append is idempotent.
    """

    cli_session_id: str | None = None
    """The CLI's own id for a resumed run, known before the CLI writes.

    A fresh run learns this from the file the CLI mints; a resumed one MINTS
    it, stamps the server with it, and passes it to ``--resume``. Given here,
    the sink backfills it at open rather than waiting to observe it.
    """

    quiesce_seconds: float = 1.0
    """Seconds to keep draining after the CLI exits."""

    SUPPORTED_CLIS: ClassVar[tuple[str, ...]] = tuple(_ADAPTERS)

    @property
    def syncing(self) -> bool:
        """Whether this run opens a server session (vs local-file capture).

        The single spelling of the three-term predicate: ``_open_sink``'s
        server-vs-local choice and ``main``'s client construction both read
        it, so the two call sites cannot drift.
        """
        return self.sync and self.out_path is None and not self.dry_run


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
    """Default JSONL path under ``state_dir() / "rekursiv-ai" / "trax" / "run"``.

    One file per invocation; the name carries the adapter and start timestamp
    so recent captures are easy to find. The caller creates the parent and
    writes the file.

    The pid disambiguates. The timestamp resolves to the second, and the
    sinks open with ``"a"`` -- two runs of the same CLI launched together (a
    swarm, a script) then interleave their events into ONE file, each minting
    ``seq`` from 0, and neither transcript can be recovered from the result.
    """
    stamp = time.strftime("%Y%m%dT%H%M%S")
    base = state_dir() / "rekursiv-ai" / "trax" / "run"
    return base / f"{stamp}-{os.getpid()}-{adapter_name}.jsonl"


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
    if config.syncing:
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

    The CLI runs on a pseudo-terminal the wrapper owns (the relay), so the
    server can splice messages in while the human drives the native TUI. The
    drain thread reads the filesystem directly so it catches session files
    created after the wrapper starts; the live path needs no ``tail``
    subprocess. When syncing, an inbound-poll thread injects server-queued
    messages through the relay.

    To avoid sweeping in unrelated concurrent sessions (these CLIs share one
    session root), we snapshot the files that exist *before* spawning and
    drain only files this run creates afterward.
    """
    _prepare_session_dirs(adapter)
    baseline = _existing_session_files(adapter, config)
    stop = threading.Event()

    # Slash-commands the human types (``/exit``) are handled inside the CLI and
    # never logged, so the drain thread can't see them. The relay tees the
    # human's keystrokes into a detector (main thread); detected commands queue
    # here and the drain thread -- the single sink writer -- emits them, so they
    # serialize with file-sourced events rather than racing the sink.
    slash_queue: deque[tuple[SlashCommand, datetime]] = deque()
    detector = SlashCommandDetector(
        lambda command, at: slash_queue.append((command, at))
    )

    # Lines framed off the PTY stream (IO-stream runs) land here from the
    # relay's IO path; the drain thread -- the single sink writer -- empties
    # it each tick, exactly like ``slash_queue``. RAW, not normalized: the
    # sink owns one normalizer per part, so normalizing on the relay's thread
    # would advance that reader's position from two threads at once. Always
    # constructed (cheap) so the drain signature stays uniform across adapter
    # shapes. BOUNDED: a chatty child (thousands of lines/sec) with a stalled
    # sink (a slow-but-alive server can hold the drain thread ~90s before
    # ResilientSink degrades) would otherwise grow this without limit.
    # ``maxlen`` drops OLDEST on overflow -- capture prefers a visible gap
    # over runner OOM, matching the inbound queue's stance.
    stream_queue: deque[bytes] = deque(maxlen=_STREAM_QUEUE_MAX)
    # Set once the kernel watch is armed. ``Thread.start`` returns when the
    # thread is SCHEDULED, not when it has run, and the relay forks the CLI on
    # the next statement -- so without waiting here the child can write its
    # first record into an unwatched directory. Codex does exactly that
    # (``session_meta`` at launch), and no later event ever names it.
    armed = threading.Event()
    drain_thread = threading.Thread(
        target=partial(
            _drain_filesystem_loop,
            adapter,
            sink,
            stats,
            config,
            stop,
            baseline=baseline,
            slash_queue=slash_queue,
            stream_queue=stream_queue,
            armed=armed,
        ),
        daemon=True,
    )
    drain_thread.start()
    if not armed.wait(_ARM_TIMEOUT_SEC):
        # Spawn anyway: a run that captures nothing still beats one that never
        # starts. Say so, since the alternative is a silently empty transcript.
        sys.stderr.write(
            f"[trax run] session-log watch not ready within {_ARM_TIMEOUT_SEC:.0f}s; "
            "starting the CLI anyway (early output may not be captured)\n"
        )

    # A stream adapter names no binary of its own: the command is the ``--``
    # args verbatim (``trax run sh -- bash -c '...'``). Its capture source is
    # the PTY stream, not a session log, so a line-framing observer queues
    # each completed line. Lines go into ``stream_queue`` -- NOT straight
    # into the sink: framing runs on the relay's IO path, where a blocking
    # ``sink.feed`` (a slow server POST) would stall terminal mirroring and
    # input delivery.
    stream_capture: LineCapture | None = None
    if isinstance(adapter, StreamAdapter):
        if not config.cli_args:
            stop.set()
            drain_thread.join(timeout=1.0)
            raise SystemExit(
                f"trax run {adapter.name}: no command given; "
                f"usage: trax run {adapter.name} -- CMD [ARGS...]"
            )
        argv = list(config.cli_args)
        stream_capture = LineCapture(partial(_enqueue_stream_line, stream_queue, stats))
    else:
        argv = [adapter.cli_binary, *config.cli_args]

    # Resolve the binary before forking: after ``pty.fork`` a missing binary
    # would fail in the child's ``execvp``, not here, so the parent could not
    # turn it into a clean ``SystemExit``.
    if shutil.which(argv[0]) is None:
        stop.set()
        drain_thread.join(timeout=1.0)
        raise SystemExit(f"trax run: {argv[0]} not found in PATH")
    # Open the session eagerly (before fork) so the server-granted routing
    # handle is in the child env from the start: an agent inside must know its
    # real address (``scientist#2`` on a collision), not the requested name
    # (#453). A local-file / dry-run sink has no server session and returns
    # None, so the requested ``config.actor`` is exported unchanged.
    #
    # A resumed run KNOWS its CLI session id before the CLI writes anything --
    # it minted the id and materialized the transcript under it -- and handing
    # it over BEFORE the session opens is what makes the server re-attach the
    # existing AgentSession instead of forking a second one.
    if config.cli_session_id is not None:
        sink.set_cli_session_id(config.cli_session_id)
    granted_actor = sink.open()
    # Run the CLI on a PTY we own rather than letting it inherit the terminal:
    # owning the master fd is what lets the server splice messages into the
    # live session (the human still drives the native TUI). Byte-transparent,
    # so display fidelity matches running the CLI directly. Export the
    # session's routing identity so an agent inside can address peers
    # (``trax send @other``) and know which rooms it is reachable in.
    # A plain line-reading child (IO-stream run) gets newline-terminated
    # injection: the TUI bracketed-paste protocol would deliver its escape
    # sentinels as literal bytes to a canonical-mode ``read``.
    relay = ThreadedRelay(
        argv,
        env=_routing_env(config, granted_actor=granted_actor),
        on_input=detector.feed,
        on_output=None if stream_capture is None else stream_capture.feed,
        bracketed_paste=stream_capture is None,
    )

    # Poll the server for inbound messages and inject them into the live CLI.
    # Only when syncing: a local-file run has no server session to poll.
    poll_thread: threading.Thread | None = None
    if config.syncing and config.client is not None:
        poll_thread = threading.Thread(
            target=partial(
                _inbound_poll_loop,
                config.client,
                sink,
                relay,
                stop,
                stream=stream_capture is not None,
            ),
            daemon=True,
        )
        poll_thread.start()

    # The teardown runs even when ``relay.run`` raises. Skipping it would leave
    # ``stop`` unset and both daemon threads live, and ``run``'s own ``finally``
    # would then close the sink underneath a drain still emitting into it --
    # exactly the race ``LockedSink`` and the joins exist to rule out.
    try:
        rc = relay.run()
    finally:
        if stream_capture is not None:
            # Flush a trailing unterminated line so a child that exited mid-line
            # still gets its last words captured. The sink is a LockedSink, so
            # this emit serializes with the drain thread's.
            stream_capture.close()

        # Let the drain thread finish pending reads before stopping it.
        time.sleep(config.quiesce_seconds)
        stop.set()
        # One deadline for the whole teardown: the workers normally stop well
        # inside it, so a slow drain (a retrying server POST) finishes rather
        # than racing ``sink.close`` (R2R-024), while a permanently wedged POST
        # cannot hang the process. The LockedSink's ``close`` then acquires its
        # lock with a short timeout and skips locked teardown rather than
        # deadlocking against a straggler that outlived the deadline.
        deadline = time.monotonic() + _JOIN_DEADLINE_SEC
        _join_with_watchdog(drain_thread, "drain", deadline=deadline)
        if poll_thread is not None:
            _join_with_watchdog(poll_thread, "inbound poll", deadline=deadline)
    return rc


def _join_with_watchdog(
    thread: threading.Thread, name: str, *, deadline: float
) -> None:
    """Join ``thread`` until ``deadline``, warning if it outlives it.

    A too-short ``join`` made thread ownership non-binding, so ``sink.close``
    could race an in-flight ``emit`` / ``flush`` (R2R-024). Waiting lets the
    worker normally stop first; a thread still alive at the deadline (a wedged
    blocking call) is logged and left rather than hanging ``trax run`` forever,
    and ``LockedSink.close`` then declines to block on the lock that straggler
    still holds.

    Args:
      thread: The worker to join.
      name: Its name, for the warning.
      deadline: A ``time.monotonic`` instant. Shared by every join in one
        teardown, so the total wait is bounded rather than per-thread.

    """
    thread.join(timeout=max(0.0, deadline - time.monotonic()))
    if thread.is_alive():
        sys.stderr.write(
            f"[trax run] {name} thread did not stop before the teardown "
            "deadline; proceeding to close\n"
        )


def _enqueue_stream_line(queue: deque[bytes], stats: _Stats, raw: bytes) -> None:
    """Queue one framed stream line for the drain thread, overflow VISIBLE.

    ``deque(maxlen=...)`` evicts the oldest silently; a full queue here means
    the sink has stalled while the child keeps printing, and each eviction is
    a captured-line loss. Count it (surfaces in the end-of-run stats line as
    ``StreamEventDropped=N``) and WARN once per run on the first drop so the
    loss is diagnosable rather than a quiet gap in the transcript.
    """
    if len(queue) == queue.maxlen:
        if not stats.counts.get("StreamEventDropped"):
            _logger.warning(
                "stream capture queue full (%d); dropping oldest lines until "
                "the sink drains",
                queue.maxlen,
            )
        stats.record("StreamEventDropped")
    queue.append(raw)


def _inbound_poll_loop(
    client: Client,
    sink: Sink,
    relay: ThreadedRelay,
    stop: threading.Event,
    *,
    poll_interval: float = 0.5,
    wait_sec: float = _INBOUND_WAIT_SEC,
    stream: bool = False,
) -> None:
    """Wait on server-queued inbound messages and type them into the CLI.

    The server HOLDS each request until a message arrives, so this is one
    parked request per session rather than one round trip every interval, and
    a message reaches the CLI when it is sent rather than up to an interval
    later. The request returns empty at its own ceiling; this re-arms.

    ``poll_interval`` is no longer the delivery latency -- only the gap before
    re-arming after a FAILURE, and the wait for a session id that has not been
    minted yet (it appears on the first captured event). ``stream`` says what
    kind of child consumes the submissions (see :func:`_render_inbound`'s
    envelope shaping).

    Server errors are swallowed: a flaky back-channel must not crash the run
    or corrupt the terminal, exactly like the capture sink's resilience.
    """
    warned = False
    while not stop.is_set():
        session_id = sink.session_id
        if session_id is None:
            # No session yet; nothing to wait on. Re-check on the interval.
            if stop.wait(poll_interval):
                break
            continue
        try:
            batch = client.drain_inbound(session_id, wait_sec=wait_sec)
            # Each submission is guarded on its own. ``drain_inbound`` CONSUMES
            # server-side, so the batch is already gone from the queue: one
            # message that cannot be typed must not take its siblings with it,
            # since no later drain will ever return them again.
            for text, source, room in batch:
                try:
                    relay.submit(_render_inbound(text, source, room, stream=stream))
                except Exception:
                    _logger.warning(
                        "trax run: could not deliver an inbound message; "
                        "continuing with the rest of the batch",
                        exc_info=True,
                    )
            warned = False  # recovered; allow a fresh warning next outage
            # No sleep: the request itself was the wait, so re-arming
            # immediately is what keeps the channel continuously parked.
            continue
        except Exception:
            # Warn once per outage on stderr (mirrors the capture sink's
            # degrade banner) so a token expiry / network drop is visible,
            # not a silent stop; repeats stay at DEBUG to avoid flooding.
            if not warned:
                sys.stderr.write(
                    "[trax run] inbound delivery failed; messages will not be "
                    "delivered until it recovers\n"
                )
                warned = True
            _logger.debug("inbound wait failed", exc_info=True)
        # Only a failure lands here: back off before re-arming so a persistent
        # outage is not a hot retry loop.
        if stop.wait(poll_interval):
            break


def _render_inbound(
    text: str,
    source: str | None,
    room: str | None,
    *,
    stream: bool = False,
) -> str:
    """Decorate an inbound message with its routing context for injection.

    A single PTY interleaves every room's messages into one input stream, so
    the agent needs the room and sender to know who is steering it. Renders
    ``[room] sender: text`` (dropping whichever of room/sender is absent), so a
    direct session-id enqueue with no attested sender injects the bare text.

    Change envelopes are shaped per consumer HERE, at the client -- the
    server pushes one uniform JSON envelope to every session. A model-CLI
    session (``stream=False``) receives only the envelope's
    ``agent_message`` line: the remaining fields would spend the model's
    context on metadata it can fetch on demand (the line itself names the
    ``trax`` command). An IO-stream session (``stream=True``) receives the
    whole envelope to parse itself -- behind the same room/sender prefix as
    any other message, since a line-reading child needs to know who sent it
    just as much. Only the route-attested ``trackinizer``
    sender unwraps -- ``source`` is stamped server-side from the principal,
    so another sender's JSON-looking text renders as a plain message.
    """
    if source == "trackinizer" and not stream:
        agent_message = _envelope_agent_message(text)
        if agent_message is not None:
            return agent_message
    prefix = ""
    if room:
        prefix += f"[{room}] "
    if source:
        prefix += f"{source}: "
    return f"{prefix}{text}"


def _envelope_agent_message(text: str) -> str | None:
    """The ``agent_message`` line of a change envelope, or None if not one."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    message = cast(dict[str, object], payload).get("agent_message")
    return message if isinstance(message, str) else None


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


def _prepare_session_dirs(adapter: Adapter) -> None:
    """Create a file adapter's watch roots before the wrapped CLI starts."""
    if isinstance(adapter, StreamAdapter):
        return
    for session_dir in adapter.session_dirs():
        session_dir.mkdir(parents=True, exist_ok=True)


def _existing_session_files(
    adapter: Adapter, config: RunConfig | None = None
) -> frozenset[Path]:
    """Snapshot the matching session files present before the run starts.

    Files in this set belong to earlier or concurrent sessions; the follower
    ignores them so this run captures only the session it spawned.

    A ``resume_path`` is EXEMPT: this run materialized it moments ago and is
    about to continue it, so excluding it would drop the very transcript being
    resumed -- every turn the CLI appends would be captured while the ones it
    was handed were not.
    """
    found: set[Path] = set()
    for session_dir in adapter.session_dirs():
        if not session_dir.is_dir():
            continue
        for path in session_dir.rglob("*"):
            if path.is_file() and adapter.matches_session_file(path):
                found.add(path)
    resume = config.resume_path if config is not None else None
    return frozenset(found - {resume} if resume is not None else found)


def _drain_filesystem_loop(
    adapter: Adapter,
    sink: Sink,
    stats: _Stats,
    config: RunConfig,
    stop: threading.Event,
    *,
    baseline: frozenset[Path],
    slash_queue: deque[tuple[SlashCommand, datetime]],
    stream_queue: deque[bytes] | None = None,
    armed: threading.Event | None = None,
) -> None:
    """Watch the adapter's session dirs; emit records as lines are appended.

    Wake-driven, not polled. The cost of polling was never the tick -- it was
    the DISCOVERY inside it: claude's ``session_dirs()`` returned every project
    directory the CLI had ever used, and re-walking them cost 86ms of every
    0.2s pass. The directories are walked ONCE here, to arm a watch; the
    kernel then names each changed file.

    ``baseline`` (files present at startup) is skipped so a concurrent
    session's log is never swept in. There is no mtime floor: it existed to
    reject a file the pre-fork snapshot raced past, and a watch armed before
    the spawn reports only what happens after it.

    ``slash_queue`` carries slash-commands the relay's keystroke detector
    produced on the main thread; ``stream_queue`` carries events a stream
    adapter's LineCapture parsed on the relay's IO path. This loop owns the
    sink, so both drain and emit here rather than have another thread touch
    the sink (or block the relay on a slow server POST). Neither has a
    filesystem event to ride on, so a short wait bounds how long a queued
    item can sit -- that wait is for the QUEUES, never for the files.

    ``armed`` is set once the kernel watch exists, so the caller can hold the
    child's spawn until then; a run with no filesystem source sets it too,
    since there is nothing to wait for.
    """
    asyncio.run(
        _drain_until_stopped(
            adapter,
            sink,
            stats,
            config,
            stop,
            baseline=baseline,
            slash_queue=slash_queue,
            stream_queue=stream_queue,
            armed=armed,
        )
    )


async def _drain_until_stopped(
    adapter: Adapter,
    sink: Sink,
    stats: _Stats,
    config: RunConfig,
    stop: threading.Event,
    *,
    baseline: frozenset[Path],
    slash_queue: deque[tuple[SlashCommand, datetime]],
    stream_queue: deque[bytes] | None,
    replay: bool = False,
    armed: threading.Event | None = None,
) -> None:
    """Follow the session logs and drain the queues until ``stop``."""
    watched = tuple(d for d in adapter.session_dirs() if d.is_dir())
    lines: asyncio.Queue[_Captured] = asyncio.Queue()
    follower = (
        asyncio.create_task(
            _follow_session_files(
                adapter,
                watched,
                baseline,
                lines,
                replay=replay,
                resume=(
                    frozenset({config.resume_path})
                    if config.resume_path is not None
                    else frozenset()
                ),
                armed=armed,
            )
        )
        if watched
        else None
    )
    if follower is None and armed is not None:
        # No filesystem source (a stream adapter, or no directory exists):
        # nothing will ever arm, so release the caller rather than make it
        # wait out the timeout before every such run.
        armed.set()
    try:
        while not stop.is_set():
            await _drain_queues(
                adapter,
                sink,
                stats,
                config,
                slash_queue=slash_queue,
                stream_queue=stream_queue,
            )
            await _drain_lines(adapter, sink, stats, config, lines)
            _flush(sink)
            # Wait on the LINE queue, not a clock: a captured line wakes this
            # the moment the follower delivers it. The timeout is the ceiling
            # for the two thread-fed queues, which carry no wakeup of their
            # own -- it never delays a line.
            #
            # The woken item is emitted HERE rather than pushed back: a
            # ``put_nowait`` appends, so re-queueing it would move it BEHIND
            # every line the follower delivered while this waited, and the
            # transcript would record a later turn before an earlier one.
            with contextlib.suppress(TimeoutError):
                captured = await asyncio.wait_for(lines.get(), _QUEUE_DRAIN_SEC)
                _emit_line(adapter, sink, stats, config, captured)
        # A final pass: the CLI's last write may land between the last wake
        # and ``stop``, and a session-end record often does exactly that.
        # The wait lets the follower run at least once -- a caller that set
        # ``stop`` before entering (a replay, a test) skipped the loop body
        # entirely, so nothing has been delivered yet.
        with contextlib.suppress(TimeoutError):
            captured = await asyncio.wait_for(lines.get(), _QUEUE_DRAIN_SEC)
            _emit_line(adapter, sink, stats, config, captured)
        await _drain_queues(
            adapter,
            sink,
            stats,
            config,
            slash_queue=slash_queue,
            stream_queue=stream_queue,
        )
        await _drain_lines(adapter, sink, stats, config, lines)
        _flush(sink)
    finally:
        if follower is not None:
            _ = follower.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await follower


async def _follow_session_files(
    adapter: Adapter,
    watched: tuple[Path, ...],
    baseline: frozenset[Path],
    lines: asyncio.Queue[_Captured],
    *,
    replay: bool = False,
    resume: frozenset[Path] = frozenset(),
    armed: threading.Event | None = None,
) -> None:
    """Feed every line this run's session files gain onto ``lines``.

    Errors are logged and the watch is REBUILT rather than abandoned: the
    causes are transient and external (an inotify instance or watch limit hit
    while another process churns directories), and a follower that returned
    would leave the drain loop ticking against a queue nothing fills -- capture
    dead for the rest of the run, with one log line an hour earlier as the only
    sign.
    """
    # The subtree this run's own files land in, when the CLI derives one from
    # the working directory. Resolved ONCE: a wrapped CLI cannot change the
    # runner's cwd, and re-deriving per candidate would pay a ``resolve`` on
    # every event.
    scope = adapter.session_scope()

    def mine(path: Path) -> bool:
        """Whether this run should capture ``path``.

        Three conditions, and the third is what makes ownership more than a
        timing accident. ``baseline`` excludes what already existed and
        ``matches_session_file`` excludes what is not a transcript, but
        between them any NEW match qualifies -- including one a concurrent run
        in another workspace just created, since both appear under the same
        watched root. ``scope`` is the CLI's own answer to "which of these is
        mine"; adapters that cannot tell return ``None`` and this stays off.
        """
        if path in baseline or not adapter.matches_session_file(path):
            return False
        return scope is None or scope in path.parents

    # Digest of the body last queued per whole-file session, so one rewrite
    # delivered as two wakes is not read as two turns (see ``_queue_body``).
    # Held ACROSS rearms: a rebuilt watch re-reads bodies it already queued,
    # and the digests are what keep those from becoming duplicate turns.
    bodies: dict[Path, str] = {}
    rearming = False
    while True:
        # A REARM always replays. A fresh watch starts each file it finds at
        # its current end, so this run's own session file -- written during the
        # outage, and excluded from ``baseline`` -- would resume past every
        # line it already holds and those turns would be lost with nothing
        # said. Replaying re-reads them, and re-reading is free: a record's key
        # is its POSITION in the file's normalized stream, so a replayed line
        # lands back on the key it already had. ``mine`` still excludes
        # ``baseline``, so a replay never reaches another session's transcript.
        await _watch_session_files(
            adapter,
            watched,
            lines,
            bodies,
            mine,
            replay=replay or rearming,
            resume=resume,
            armed=armed,
        )
        rearming = True
        # Only a failure returns; a healthy watch iterates until cancelled.
        # Back off so a persistent refusal is not a hot retry loop.
        await asyncio.sleep(_WATCH_REARM_SEC)


async def _watch_session_files(
    adapter: Adapter,
    watched: tuple[Path, ...],
    lines: asyncio.Queue[_Captured],
    bodies: dict[Path, str],
    mine: Callable[[Path], bool],
    *,
    replay: bool,
    resume: frozenset[Path] = frozenset(),
    armed: threading.Event | None = None,
) -> None:
    """Arm one watch and feed it until it fails; the caller rearms."""
    try:
        if adapter.whole_file:
            # Gemini rewrites ONE json object in place, so a line is a fragment
            # of it and parses as nothing. Its body is re-read whole on each
            # change instead; the watch says which file changed, which is all
            # the old poll's ``(size, mtime)`` stamp was determining.
            async with follow_dir(*watched) as changed:
                if armed is not None:
                    armed.set()
                if replay:
                    # A dry run replays what is already on disk. Nothing is
                    # going to change those files, so waiting for an event
                    # would capture nothing at all.
                    for directory in watched:
                        for path in sorted(directory.rglob("*")):
                            if path.is_file() and mine(path):
                                _queue_body(path, lines, bodies)
                async for paths in changed:
                    for path in sorted(p for p in paths if mine(p)):
                        _queue_body(path, lines, bodies)
            return
        async for followed in follow_tree(
            *watched,
            match=mine,
            replay=replay,
            resume=resume,
            on_armed=None if armed is None else armed.set,
        ):
            lines.put_nowait(
                _Captured(
                    path=followed.path,
                    raw=followed.text.encode(),
                    restart=followed.restart,
                )
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _logger.warning("trax run: session-log watch failed", exc_info=True)
    finally:
        # A watch that never armed must not leave the spawn waiting out the
        # full timeout: the failure is already logged, and the run proceeds
        # uncaptured either way.
        if armed is not None:
            armed.set()


def _queue_body(
    path: Path, lines: asyncio.Queue[_Captured], bodies: dict[Path, str]
) -> None:
    """Queue a whole-file session's body, skipping empty, unreadable, or unchanged.

    ONE rewrite is not one wake: ``write_text`` truncates and then writes, so
    the kernel queues two events for it, and a read landing between them wakes
    the follower twice. The second wake re-reads a body already queued, and the
    adapter turns it into the same turn again -- a duplicated last message in
    the transcript. The line path needs no equivalent: its cursor advances past
    what it read, so a second wake with nothing appended yields nothing.

    Identity is the body's digest rather than ``(size, mtime)``: gemini rewrites
    one JSON object in place, so a same-length edit within a timestamp's
    granularity leaves both unchanged while the turn is new. Only the digest is
    kept -- a session file grows to megabytes, and every one of them would
    otherwise be held for the life of the run.
    """
    body = _read_bytes(path)
    if not body.strip():
        return
    digest = hashlib.sha256(body).hexdigest()
    if bodies.get(path) == digest:
        return
    bodies[path] = digest
    lines.put_nowait(_Captured(path=path, raw=body, restart=True))


def _read_bytes(path: Path) -> bytes:
    """The whole file, or empty when it cannot be read.

    A read racing the writer's rewrite is routine, not an error: the next
    change wakes another read.
    """
    try:
        return path.read_bytes()
    except OSError:
        return b""


async def _drain_lines(
    adapter: Adapter,
    sink: Sink,
    stats: _Stats,
    config: RunConfig,
    lines: asyncio.Queue[_Captured],
) -> None:
    """Emit every line the follower has delivered so far."""
    while True:
        try:
            captured = lines.get_nowait()
        except asyncio.QueueEmpty:
            return
        _emit_line(adapter, sink, stats, config, captured)


def _emit_line(
    adapter: Adapter,
    sink: Sink,
    stats: _Stats,
    config: RunConfig,
    captured: _Captured,
) -> None:
    """Emit one captured line.

    No line-level dedup. A record's stored key is its POSITION in the file's
    normalized stream, so re-feeding a file the CLI rewrote lands each record
    back where it already was. Identity by record id (what this did before)
    could only ever suppress a duplicate, never correct one that CHANGED --
    and a compaction is a replacement, so a re-derived record legitimately
    differs from the stored one.
    """
    # Backfill the CLI's own session id (the file names it) so a fresh run
    # becomes resumable on its next ``--resume``. The sink keeps the first
    # non-empty id and ignores it on a local-file run.
    cli_session_id = adapter.session_id_from_path(captured.path)
    if cli_session_id is not None:
        sink.set_cli_session_id(cli_session_id)
    if not captured.raw.strip():
        return
    _process_chunk(captured, adapter, sink, stats, config)


async def _drain_queues(
    adapter: Adapter,
    sink: Sink,
    stats: _Stats,
    config: RunConfig,
    *,
    slash_queue: deque[tuple[SlashCommand, datetime]],
    stream_queue: deque[bytes] | None,
) -> None:
    """Emit whatever the relay's threads have queued, oldest first.

    Guarded like the file path: this runs on the drain's own loop, where an
    escaping error would end capture for the rest of the run.
    """
    try:
        _emit_slash_commands(adapter, sink, stats, config, slash_queue)
        _emit_stream_lines(adapter, sink, stats, stream_queue)
    except Exception:
        _logger.warning("trax run: queue drain failed; continuing", exc_info=True)


def _flush(sink: Sink) -> None:
    """Flush the sink, surviving a transient failure (R-57).

    A flush hiccup must cost one pass, not the whole run's capture.
    """
    try:
        sink.flush()
    except Exception:
        _logger.warning("trax run: sink flush failed; continuing", exc_info=True)


def _emit_slash_commands(
    adapter: Adapter,
    sink: Sink,
    stats: _Stats,
    config: RunConfig,
    slash_queue: deque[tuple[SlashCommand, datetime]],
) -> None:
    """Drain queued slash-commands into the sink.

    NOT as records: a command is handled inside the TUI and never written to
    the session log, so it has no position in any part and cannot be
    re-derived. It goes down its own path and the server numbers it.

    Runs on the drain thread (the single sink writer). ``popleft`` on a
    ``deque`` is atomic, so it needs no lock against the relay thread's
    ``append``; draining until ``IndexError`` empties whatever the human typed
    since the last tick. Each command carries the submit-time clock the
    detector stamped, since a typed command has no CLI-recorded timestamp.
    """
    while True:
        try:
            command, submitted_at = slash_queue.popleft()
        except IndexError:
            break
        sink.emit_slash_command(command, submitted_at)
        stats.record("SlashCommand")
        if config.verbose:
            sys.stderr.write(f"[trax run] {adapter.name}: SlashCommand\n")


def _emit_stream_lines(
    adapter: Adapter,
    sink: Sink,
    stats: _Stats,
    stream_queue: deque[bytes] | None,
) -> None:
    """Drain stream-captured lines into the sink (single sink writer).

    Runs on the drain thread. The relay's IO path only appends to the deque
    (atomic), so framing never blocks on a slow ``sink.feed``.

    Every line goes to ONE part: a scrape follows no file, but a record still
    needs one, so the stream names itself (:data:`_STREAM_PART`) and the
    positions run from zero across the whole run.
    """
    if stream_queue is None:
        return
    while True:
        try:
            raw = stream_queue.popleft()
        except IndexError:
            break
        for kind in sink.feed(adapter, _STREAM_PART, raw):
            stats.record(kind)


def _process_chunk(
    captured: _Captured,
    adapter: Adapter,
    sink: Sink,
    stats: _Stats,
    config: RunConfig,
) -> None:
    """Feed one chunk to the sink; never let a bug abort capture.

    Both halves run untrusted work in a daemon drain thread, and an escaping
    raise there kills the thread and silently ends capture for the rest of the
    run. Normalizing runs adapter code over CLI bytes (a malformed line failing
    a record invariant); the sink can fail at the bottom of its own fallback
    chain -- ``ResilientSink`` degrades to a local ``FileSink``, whose write
    raises on a full disk with nothing left to catch it. One guard covers both,
    so a failure costs one chunk rather than the run.
    """
    try:
        emitted = sink.feed(
            adapter,
            captured.path,
            captured.raw,
            restart=captured.restart,
        )
    except Exception:
        # ``exc_info`` so the swallowed failure carries a traceback: without it
        # a malformed-output loss is an opaque one-liner with no clue which
        # adapter path raised (K6-004).
        _logger.warning(
            "trax run: %s failed to capture a chunk",
            adapter.name,
            exc_info=True,
        )
        return
    for kind in emitted:
        stats.record(kind)
        if config.verbose:
            sys.stderr.write(f"[trax run] {adapter.name}: {kind}\n")


def _dry_run_drain(
    config: RunConfig,
    adapter: Adapter,
    sink: Sink,
    stats: _Stats,
    *,
    stop: threading.Event | None = None,
) -> int:
    """The ``--dry-run`` loop: replay existing session files until Ctrl-C.

    The same watch the live drain uses, with two differences that are the
    point of a dry run: nothing is excluded (there is no ``baseline`` -- the
    existing files ARE the subject), and the follower replays what they
    already hold rather than only what they gain.

    ``stop`` ends the loop (tests inject it); in a real run it is ``None`` and
    a ``KeyboardInterrupt`` (Ctrl-C) ends it instead.
    """
    stop = stop or threading.Event()
    sys.stderr.write("[trax run] dry-run: replaying session files; Ctrl-C to stop\n")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(
            _drain_until_stopped(
                adapter,
                sink,
                stats,
                config,
                stop,
                baseline=frozenset(),
                slash_queue=deque(),
                stream_queue=None,
                replay=True,
            )
        )
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
        help=(
            "Capture to this local JSONL file instead of syncing to the "
            "server (the default)."
        ),
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


def resume_argv(cli_name: str, cli_session_id: str | None) -> tuple[str, ...]:
    """The tokens that tell ``cli_name`` to continue ``cli_session_id``.

    Per-CLI because the spelling is: claude takes a FLAG (``--resume <uuid>``)
    and codex a SUBCOMMAND (``codex resume <SESSION_ID>``), both verified
    against the installed binaries' help. Handing codex claude's spelling
    makes it reject the argument, so a resume that materialized a perfectly
    good rollout never starts.

    Args:
      cli_name: Which CLI is being spawned.
      cli_session_id: The id it should re-enter, or ``None`` for a fresh run.

    Returns:
      argv: Tokens to prepend to the CLI's own arguments; empty for a fresh
        run, and for a CLI with no resume spelling at all.

    """
    if not cli_session_id:
        return ()
    if cli_name == "codex":
        return ("resume", cli_session_id)
    return ("--resume", cli_session_id)


def main(
    argv: Sequence[str],
    *,
    client_factory: Callable[[], Client] | None = None,
    resume_path: Path | None = None,
    cli_session_id: str | None = None,
) -> int:
    """Entry point for ``trax run``, called from ``trax/cli.py``.

    Args:
      argv: ``trax run`` arguments; everything after ``--`` goes to the CLI.
      client_factory: Builds the Trackinizer client from the active profile.
        Invoked only when the run actually syncs, so a ``--no-sync`` or
        ``--out`` capture never resolves a profile or opens a socket.
      resume_path: A materialized transcript this run continues. Not a flag:
        the file is written by the resume tail moments before, so there is no
        spelling of it a human would type.
      cli_session_id: The id that transcript was materialized under, which
        ``--resume`` takes and which re-attaches the run to its stored
        AgentSession.

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
    # ``syncing`` off the parsed args, not off a half-built config: the client
    # is then an ordinary constructor argument and ``RunConfig`` stays frozen.
    syncing = args.sync and args.out is None and not args.dry_run
    return run(
        RunConfig(
            cli_name=args.cli,
            # Prepended, so a caller's own ``--`` args still win over it.
            cli_args=(*resume_argv(args.cli, cli_session_id), *cli_argv),
            actor=args.actor or os.environ.get("AGENTNAME", "Agent"),
            rooms=tuple(args.rooms or ()),
            out_path=args.out,
            verbose=args.verbose,
            dry_run=args.dry_run,
            sync=args.sync,
            client=client_factory() if syncing and client_factory else None,
            resume_path=resume_path,
            cli_session_id=cli_session_id,
        )
    )
