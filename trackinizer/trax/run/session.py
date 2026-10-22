"""Run the CLI on a PTY we own, tail its session log, and write Events.

Three things run side by side, and for claude a fourth:

1. The CLI, spawned on a pseudo-terminal whose master fd the wrapper holds
   (:class:`~trackinizer.lib.posix.relay.ThreadedRelay`). Owning the master is what
   lets the server splice messages into the live session; the relay mirrors
   bytes both ways so the human still drives the native TUI (byte-transparent,
   though not literally inherited fds).

2. A drain thread that watches the adapter's session directories and emits
   events for each new log line, coping with rotation and new files.

3. When syncing, an inbound-poll thread that drains server-queued messages
   and types them into the CLI via the relay, once the CLI is ready for them
   (:class:`InboundState`).

4. For a synced claude run, a prompt bridge
   (:mod:`~trackinizer.trax.run.prompt_bridge`) that carries the permission
   and question prompts claude asks to Chat, and Chat's answers back.

A run a chat's host starts (``--detach --attach``) drives claude or codex headless
instead: no PTY, but a driver (:mod:`~trackinizer.trax.run.headless`) that sends
the CLI Chat's lines and carries its asks to Chat as cards. The drain thread and the
inbound poll are the same; the transcript is still the CLI's own session file. A person
who runs ``trax run attach`` takes the CLI over in its own terminal.

When the CLI exits, the wrapper drains pending lines for a short quiesce
window, reads the session files to their ends, closes the sink, and exits
with the CLI's status.

``--dry-run`` skips spawning the CLI and replays existing session files
(the same per-shape drain as the live path), useful for adapter development
or replaying a finished session.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Final, Literal, Protocol, cast

import argparse
import asyncio
import contextlib
import hashlib
import logging
import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid

from treekle import PlainTree, from_plain, json_dumps, json_loads

from trackinizer.client.client import Client
from trackinizer.client.errors import ClientError
from trackinizer.lib.agent.types.sessions import (
    ContextCompaction,
    ContextState,
    SystemMessage,
    UserMessage,
)
from trackinizer.lib.posix.follow import follow_dir, follow_tree
from trackinizer.lib.posix.host import HostSpec
from trackinizer.lib.posix.relay import ThreadedRelay
from trackinizer.lib.userdirs import state_dir
from trackinizer.trax.machines import host_name
from trackinizer.trax.profile import LOCALHOST_FALLBACK_URL
from trackinizer.trax.run import detach
from trackinizer.trax.run.adapters.claude import ClaudeAdapter
from trackinizer.trax.run.adapters.codex import CodexAdapter
from trackinizer.trax.run.adapters.custom_types import Adapter, StreamAdapter
from trackinizer.trax.run.adapters.gemini import GeminiAdapter
from trackinizer.trax.run.adapters.iostream import IOStreamAdapter
from trackinizer.trax.run.asks import answer_from_payload
from trackinizer.trax.run.claude_stream import ClaudeStreamDriver
from trackinizer.trax.run.codex_app import (
    ApprovalPolicy,
    CodexAppDriver,
    Reviewer,
    SandboxMode,
)
from trackinizer.trax.run.commands import (
    GoalWatch,
    command_record,
    parse_command,
    replaces_goal,
)
from trackinizer.trax.run.custom_types import Event
from trackinizer.trax.run.errors import CiphertextDroppedError, NotResumableError
from trackinizer.trax.run.headless import (
    HeadlessRun,
    RunEndedError,
    StateReporter,
    TakenOverError,
)
from trackinizer.trax.run.inbound import (
    CANVAS_COMMANDS,
    Disclosure,
    render_inbound,
    settings_from_payload,
)
from trackinizer.trax.run.placement import place_run, placement_env
from trackinizer.trax.run.prompt_bridge import PromptBridge
from trackinizer.trax.run.redact import Redactor, redactor_from_environ
from trackinizer.trax.run.sink import (
    FileSink,
    LockedSink,
    PrivateNotHeldError,
    ResilientSink,
    Sink,
    TrackinizerSink,
)
from trackinizer.trax.run.slash import SlashCommand, SlashCommandDetector
from trackinizer.trax.run.trust import (
    TrustGate,
    TrustScreen,
    claude_remembers,
    claude_trusted,
    codex_trusted,
)
from trackinizer.trax.run.worktree_diff import WorktreeDiff, open_worktree_diff
from trackinizer.types.line_capture import LineCapture
from trackinizer.wire.wire_sessions import (
    RUN_ENDING_REFUSALS,
    InboundDrainItem,
    SessionRunState,
    WorkspaceMessageContext,
    session_run_state_path,
)


if TYPE_CHECKING:
    from collections.abc import (
        Sequence,
        Set as AbstractSet,
    )

    from trackinizer.trax import machine_host
    from trackinizer.trax.run.asks import Driver
    from trackinizer.trax.run.box_hook import hook
    from trackinizer.trax.run.headless import HostState
    from trackinizer.trax.run.resume import prepare_resume
    from trackinizer.types.streams import TraxRecord
    from trackinizer.wire.wire_sessions import (
        InboundKind,
    )
else:
    from wrapt import lazy_import

    # Only a claude run touches the box hook; any other run loads none of it
    # (0.4 ms measured, so this is about scope, not speed).
    hook = lazy_import("trackinizer.trax.run.box_hook.hook")
    # Only spawning a CLI asks whether the person declared this computer a sandbox.
    machine_host = lazy_import("trackinizer.trax.machine_host")
    # Only a run attached to a chat loads its conversation, and the loader brings the
    # readers and writers of every CLI's format.
    prepare_resume = lazy_import("trackinizer.trax.run.resume", "prepare_resume")


_logger = logging.getLogger(__name__)

# Cap on stream events buffered between the relay's IO path and the drain
# thread. At the 16KB/line clamp this bounds worst-case retention to ~128MB;
# in practice lines are small and 8k events is minutes of typical output.
_STREAM_QUEUE_MAX: Final = 8_192

# A slow sink stops the filesystem follower at this many pending chunks. The
# watched files retain unread bytes, so waiting for capacity preserves lines
# without retaining an unbounded second copy in the runner.
_FILE_QUEUE_MAX: Final = 256

_CODEX_SESSION_BANNER: Final = re.compile(
    rb"Session ID: ([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})",
)

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
# whenever the message is enqueued, nor exit latency, since ending the session
# ends the request.
_INBOUND_WAIT_SEC: Final = 25.0

# Total time the worker threads get to stop at teardown. Shared across every
# join rather than granted per thread: two sequential 30s budgets plus the
# sink's own 5s lock timeout made a wedged exit take 65s, which reads as a hang.
# One deadline bounds the whole teardown.
_JOIN_DEADLINE_SEC: Final = 30.0

# How long a claude run waits for its ``SessionStart`` hook before taking inbound
# anyway. The hook is missing when the person's own ``--settings`` could not be merged,
# or from a claude that predates it; past this, a line can still land in a dialog.
_HOOK_READY_SEC: Final = 20.0

# How long any other CLI's run waits for the CLI's first record before taking inbound
# anyway: a CLI that writes nothing until its first turn would otherwise never hear.
_RECORD_READY_SEC: Final = 15.0

# How many typed lines wait for the record the CLI writes of them. A line typed while
# a turn runs is folded into that turn and never gets a record of its own, so the
# oldest are dropped rather than kept for the life of the run.
_TYPED_REMEMBERED: Final = 32


# The CLIs a chat's run drives headless: the two with a driver. Any other CLI a chat
# attaches keeps its terminal.
_HEADLESS_CLIS: Final = ("claude", "codex")


# What a CLI placed on a station is told of it. Its own product may have a canvas too,
# so "the canvas" is named as the station's. One line with no double quote or
# backslash, so it is a TOML string as it stands, for codex's ``-c``.
_CANVAS_NOTE: Final = (
    "You run under trax run, placed on a Trackinizer station: when the person says "
    "the canvas, they mean that station's, which $TRAX_WORKSPACE names "
    "(the workstream is $TRAX_WORKSTREAM), not one of your own product's. "
    "Read it with `trax workspace`; change it with `trax workspace show trax.subgraph "
    "--record UUID` (or trax.timeline), `trax workspace navigate ROUTE`, "
    "`trax workspace highlight UUID,UUID` and `trax workspace hide ID`; "
    "`trax workspace --help` lists the rest. "
    "This conversation is already logged to trax as the chat's AgentSession; "
    "file each result to trax with the `trax` CLI as a row, with `--as` set to "
    "$TRAX_ACTOR (`trax help` lists the grammar). "
    f"{CANVAS_COMMANDS}"
)


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
class _Prefix:
    """The history a resume wrote before the CLI started, which is not uploaded again.

    The session already holds it, in the parts it was loaded from. The file is still
    read from its start, so each record keeps its position in the file and the first
    one the CLI writes is uploaded where the loaded records end.
    """

    path: Path
    size: int
    read: int = 0
    """The file's byte position after the last line seen."""

    def covers(self, captured: _Captured) -> bool:
        """Whether ``captured``'s line starts inside the prefix.

        Args:
          captured: The next line the follower read, from any file; every line,
            blank ones too, since each moves the file's position on.

        Returns:
          covered: Whether the line is of ``path`` and starts inside the prefix.

        """
        if captured.path != self.path:
            return False
        if captured.restart:
            self.read = 0
        start = self.read
        # The follower hands each line over without its newline.
        self.read += len(captured.raw) + 1
        return start < self.size


@dataclass(slots=True, kw_only=True)
class _SessionOwner:
    """The one CLI session ID whose files this wrapper may capture."""

    session_id: str | None = None
    minted: bool = False
    """Whether this run made ``session_id`` up, so must hand it to the CLI."""

    child_pid: int | None = None
    _output_tail: bytes = b""

    def started(self, pid: int) -> None:
        """Record the wrapped child's PID for Codex rollout discovery."""
        self.child_pid = pid

    def output(self, raw: bytes) -> None:
        """Retain the terminal's end for a clean-exit session ID banner."""
        self._output_tail = (self._output_tail + raw)[-512:]

    def adopt(self, session_id: str) -> None:
        """Capture the session a headless driver reports, which it names before any file."""
        self.session_id = session_id

    def finish_output(self) -> None:
        """Use the final Codex banner if descriptor discovery missed the file."""
        if self.session_id is not None:
            return
        found = list(_CODEX_SESSION_BANNER.finditer(self._output_tail))
        if found and not self._output_tail[found[-1].end() :].strip():
            self.session_id = found[-1][1].decode()

    def owns(self, adapter: Adapter, path: Path) -> bool:
        """Claim a Codex ID from its writer, then require it on every file.

        Args:
          adapter: CLI adapter extracting the ID from the file path.
          path: Candidate transcript file.

        Returns:
          owned: Whether this run owns the file.

        """
        candidate_id = adapter.session_id_from_path(path)
        if candidate_id is None:
            return False
        if self.session_id is None:
            if self.child_pid is None or not _path_held_by_child(self.child_pid, path):
                return False
            self.session_id = candidate_id
        return candidate_id == self.session_id


def _path_held_by_child(pid: int, path: Path) -> bool:
    """Return whether the wrapped process or one of its descendants holds ``path``."""
    held = _path_held_in_proc if Path("/proc/self/fd").is_dir() else _path_held_by_lsof
    return held(pid, path) or any(held(child, path) for child in _descendant_pids(pid))


def _descendant_pids(pid: int) -> list[int]:
    """Read the process tree, including wrappers whose worker owns the rollout."""
    try:
        result = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid="],
            capture_output=True,
            text=True,
            check=False,
            timeout=1.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    children: dict[int, list[int]] = {}
    for line in result.stdout.splitlines():
        child, parent = map(int, line.split())
        children.setdefault(parent, []).append(child)
    pending = list(children.get(pid, ()))
    found: list[int] = []
    seen = {pid}
    while pending:
        child = pending.pop()
        if child not in seen:
            seen.add(child)
            found.append(child)
            pending.extend(children.get(child, ()))
    return found


def _path_held_in_proc(pid: int, path: Path) -> bool:
    """Read the child's live file descriptors on procfs."""
    try:
        descriptors = os.scandir(f"/proc/{pid}/fd")
    except OSError:
        return False
    with descriptors:
        return any(_same_open_file(Path(item.path), path) for item in descriptors)


def _same_open_file(descriptor: Path, path: Path) -> bool:
    """Compare a descriptor symlink to a candidate file despite path aliases."""
    try:
        target = descriptor.readlink()
        return target.is_absolute() and target.samefile(path)
    except OSError:
        return False


def _path_held_by_lsof(pid: int, path: Path) -> bool:
    """Read open files on systems without procfs, including macOS."""
    binary = shutil.which("lsof")
    if binary is None:
        return False
    try:
        result = subprocess.run(  # noqa: S603 -- fixed lsof binary and argv; no shell.
            [binary, "-a", "-p", str(pid), "-Fn", str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=1.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return any(line == f"n{path}" for line in result.stdout.splitlines())


@dataclass(slots=True, kw_only=True)
class _Stats:
    """Counts events per kind for the end-of-run summary.

    Only counts: ``seq`` is minted by the sink (it is per-session, and the
    sink owns the session), so the run keeps no sequence of its own.
    """

    counts: dict[str, int] = field(default_factory=dict)
    switches: list[str] = field(default_factory=list)
    """Typed slash commands that can move the CLI into a session this run
    does not capture (``Adapter.session_switch_commands``), in order."""

    def record(self, kind: str) -> None:
        """Tally one event of ``kind`` for the end-of-run summary.

        Args:
          kind: Kind.

        """
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

    private: bool = False
    """Start the session hidden (``--private``), or hide the one a resume re-attaches:
    its transcript is read by its owner and the people who oversee alone. A server that
    would not hold it so leaves it as it was, or ends one it opened afresh, and the
    run stops, exit status 2, before the CLI starts."""

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
    """A transcript this run continues, exempt from ``baseline`` and scope.

    Materialized by ``trax agentsession <seq> run``, or found by the id a
    resume typed after ``--`` names (``claude --resume <id>``, ``codex resume
    <id>``) -- under another project directory too, which claude keeps
    appending to. Either way it exists BEFORE the CLI starts, so the pre-spawn
    snapshot would classify it as a previous session's and never capture it --
    and the follower would seed it at EOF, skipping every turn it already
    holds. Naming it here exempts it from both: it is followed from offset 0,
    so the stored records re-derive to the keys they already have and the
    append is idempotent.
    """

    cli_session_id: str | None = None
    """The CLI's own id for the session ``resume_path`` holds.

    Named before the CLI writes anything, so the sink hands it to the server
    at open and the server re-attaches the AgentSession that captured it,
    rather than opening a second one.
    """

    attach: uuid.UUID | None = None
    """An existing AgentSession this run re-opens (``--attach``), writing its files as
    new parts of it. The session keeps its own CLI session id -- a chat's is
    ``chat:<id>`` for its whole life -- so the run neither names one at open nor
    backfills one at close."""

    prefix_bytes: int = 0
    """How many bytes at the start of ``resume_path`` hold history the session already
    has, loaded from its earlier parts. Records from lines starting inside them are
    read but not uploaded; each record keeps its position in the file, so the first
    one uploaded sits where the loaded records end."""

    compact_after_load: bool = False
    """Whether the loaded history is past what the CLI holds comfortably, so the run
    types ``/compact`` once the CLI is ready, before any line: the CLI's own compaction
    summarizes it."""

    profile: str | None = None
    """The ``--profile`` the run was started with; the box hook reads the station
    through it."""

    quiesce_seconds: float = 1.0
    """Seconds to keep draining after the CLI exits."""

    host: HostSpec | None = None
    """Serve the CLI to attachable viewers instead of this process's terminal.

    Set by ``--detach``, in the background process it launches; see
    :mod:`~trackinizer.trax.run.detach`. Capture, sync, and inbound
    delivery are unchanged -- only who sees the terminal differs.
    """

    run_name: str | None = None
    """The ``--detach`` host's name, which ``trax run attach`` takes; ``None`` for a run
    in this terminal. Each inbound drain names it, so Chat can say how to attach."""

    instance: str = field(default_factory=lambda: uuid.uuid4().hex)
    """This run's id, minted once per run. Its drains, its state reports and its
    session end all name it, so the server can tell a run it replaced from the one that
    holds the chat."""

    headless: bool = False
    """Drive the CLI through its driver instead of a terminal: a chat's run, started by
    a machine's terminal host (``--detach --attach``) for claude or codex. The CLI's
    own terminal is hosted only while a person has taken the run over."""

    diff: bool = False
    """Show the session's net ``git diff`` of the files it edits on its Trax
    page, refreshed each turn. Off by default; a no-op when the run is not in a
    git work tree. See :mod:`~trackinizer.trax.run.worktree_diff`."""

    SUPPORTED_CLIS: ClassVar[tuple[str, ...]] = tuple(_ADAPTERS)

    @property
    def syncing(self) -> bool:
        """Whether this run opens a server session (vs local-file capture).

        The single spelling of the three-term predicate: ``_open_sink``'s
        server-vs-local choice and ``main``'s client construction both read
        it, so the two call sites cannot drift.
        """
        return self.sync and self.out_path is None and not self.dry_run


@dataclass(slots=True, kw_only=True)
class InboundState:
    """When a run may take messages off its server queue, and the lines it typed.

    The server's drain consumes, so a message drained before the CLI can take it is
    lost: typed while a startup dialog is open, a line is swallowed and its Enter picks
    the highlighted option, which at claude's trust dialog is "No, exit". So nothing is
    drained until the CLI is ready: claude says so through its ``SessionStart`` hook,
    which writes :attr:`marker`; another CLI by recording its first record. A CLI that
    never says is taken as ready after :attr:`fallback_sec`. While
    :attr:`prompt_pending` is set the drain goes on, since an answer to the prompt or
    an interrupt must still arrive, but the lines it brings wait for the prompt to
    close: one typed in would answer the prompt's dialog.

    A CLI asking to trust its folder is not ready and will not say so until it is
    answered, so while :attr:`trust_pending` is set the drain opens anyway (the card
    can then be answered from Chat) and the fallback does not count down: taken as
    ready, the CLI would get a line whose Enter answers the trust dialog.

    It also remembers each line it typed, so the record the CLI writes of it is
    stamped with who sent it (:meth:`stamp`). The sender comes from the route that
    delivered the line, never from its text: a person typing ``bob@x: hi`` at the
    terminal is not bob. Every record it is shown also feeds :attr:`goal`.

    It keeps what it has told the CLI of the canvas and of who speaks, so a Chat line
    reads as one typed at the CLI (:meth:`disclosure`): the context only when it
    changed, the sender only when it says something. A compaction (:meth:`stamp`) may
    have cost the CLI its start note and the last context, so the next line repeats
    both.
    """

    marker: Path | None = None
    """The file claude's ``SessionStart`` hook writes; ``None`` when there is none."""

    by_record: bool = False
    """Whether the CLI's first recorded record says it is ready."""

    fallback_sec: float = _RECORD_READY_SEC
    """How long after this is built the CLI is taken as ready whatever else is known."""

    clock: Callable[[], float] = time.monotonic
    """Monotonic seconds, for :attr:`fallback_sec`."""

    prompt_pending: threading.Event = field(default_factory=threading.Event)
    """Set while the CLI waits on a prompt (a permission, a question) that a typed
    line would answer; the prompt bridge sets and clears it."""

    trust_pending: threading.Event = field(default_factory=threading.Event)
    """Set while the CLI asks to trust its folder and Chat has a card for it; the
    run's trust gate sets and clears it."""

    goal: GoalWatch = field(default_factory=GoalWatch)
    """Whether the CLI follows a goal, as the records it writes say."""

    noted: bool = True
    """Whether the CLI's start note holds the canvas commands; if not, the first line
    carries them."""

    _ready: threading.Event = field(default_factory=threading.Event, init=False)
    _deadline: float = field(default=0.0, init=False)
    _typed: deque[tuple[str, Mapping[str, PlainTree], _Told]] = field(
        default_factory=lambda: deque[tuple[str, Mapping[str, PlainTree], _Told]](
            maxlen=_TYPED_REMEMBERED,
        ),
        init=False,
    )
    _told: _Told = field(init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __post_init__(self) -> None:
        self._deadline = self.clock() + self.fallback_sec
        self._told = _Told(stale=not self.noted)

    def ready(self) -> bool:
        """Whether the CLI can take a typed line; once true, true for the run.

        Returns:
          ready: Whether its hook or first record said so, or the fallback passed
            while no trust card was open.

        """
        if not self._ready.is_set() and (
            (self.marker is not None and self.marker.exists())
            or (not self.trust_pending.is_set() and self.clock() >= self._deadline)
        ):
            self._ready.set()
        return self._ready.is_set()

    def rearm(self) -> None:
        """Restart the fallback from now: the CLI starts over once trusted from Chat."""
        self._deadline = self.clock() + self.fallback_sec

    def record_seen(self) -> None:
        """Note that the CLI recorded a record, which readies a CLI ready by them."""
        if self.by_record:
            self._ready.set()

    def wait_open(self, stop: threading.Event, *, poll_sec: float) -> bool:
        """Block until messages may be drained; return False when ``stop`` came first.

        Args:
          stop: Ends the wait.
          poll_sec: How often readiness is looked at again.

        Returns:
          open: Whether messages may be drained now: the CLI is ready, or a trust
            card waits for its answer.

        """
        while not (self.ready() or self.trust_pending.is_set()):
            if stop.wait(poll_sec):
                return False
        return True

    def disclosure(
        self,
        *,
        source: str | None,
        context: WorkspaceMessageContext | None,
    ) -> Disclosure:
        """Return what a Chat line from ``source`` has to say that the CLI lacks.

        The context goes with the run's first line, then only when it changed since
        the last line (or a compaction came); marks go with their line every time, and
        with the context, which the readers of a transcript cut them off by. The
        sender is dropped for the run's own person while nobody else has spoken, and
        the canvas commands repeat only after a compaction.

        Args:
          source: The route-attested sender of the line.
          context: The canvas the line was sent from.

        Returns:
          disclosure: What the line carries; the default for a line that is no
            person's Chat line (no context, or a console relay).

        """
        if context is None or context.relay is not None:
            return Disclosure()
        with self._lock:
            told = self._told
        return Disclosure(
            sender=told.names(source),
            context=(
                told.stale or bool(context.marks) or told.context != _unmarked(context)
            ),
            canvas=told.stale,
        )

    def typed(
        self,
        text: str,
        *,
        source: str | None,
        room: str | None,
        kind: InboundKind,
        context: WorkspaceMessageContext | None = None,
    ) -> None:
        """Remember a line about to be typed, with who sent it, for :meth:`stamp`.

        What the line told the CLI is remembered with it, for :meth:`disclosure`.

        Args:
          text: The line as typed.
          source: The route-attested sender.
          room: The room it was sent to.
          kind: What the message is.
          context: The canvas it was sent from, if a Chat line.

        """
        with self._lock:
            before = self._told
            self._told = before.after(source=source, context=context)
            self._typed.append(
                (
                    _spaced(text),
                    {"source": source, "room": room, "kind": kind},
                    before,
                ),
            )

    def withdraw(self, text: str) -> None:
        """Forget the newest remembered line that reads ``text``, and what it told.

        Args:
          text: A line :meth:`typed` remembered that never reached the CLI.

        """
        key = _spaced(text)
        with self._lock:
            for index in reversed(range(len(self._typed))):
                if self._typed[index][0] == key:
                    self._told = self._typed[index][2]
                    del self._typed[index]
                    return

    def stamp(self, record: TraxRecord) -> TraxRecord:
        """Return ``record`` stamped with its sender when it is a line this run typed.

        Each remembered line stamps one record, the first that reads the same, and
        is then forgotten. Whitespace is compared collapsed, on both sides. A line
        typed while claude is mid-turn is queued, and claude records it as a
        ``queued_command`` ``ContextState`` instead of a ``UserMessage``; that stamps
        the same way, while one no line was typed for (a task notification) does not.

        Args:
          record: A record the CLI wrote.

        Returns:
          stamped: ``record``, with ``extra["$inbound"]`` naming the sender, room and
            kind when it is a ``UserMessage`` or a ``queued_command`` ``ContextState``
            this run typed; otherwise unchanged.

        """
        self.goal.observe(record)
        if isinstance(record, ContextCompaction):
            with self._lock:
                self._told = replace(self._told, stale=True)
            return record
        if isinstance(record, ContextState) and record.kind != "queued_command":
            return record
        if not isinstance(record, UserMessage | ContextState) or record.content is None:
            return record
        key = _spaced(record.content)
        with self._lock:
            found = next(
                (
                    index
                    for index, (text, _, _) in enumerate(self._typed)
                    if text == key
                ),
                None,
            )
            if found is None:
                return record
            _, sent, _ = self._typed[found]
            del self._typed[found]
        return replace(record, extra={**record.extra, "$inbound": sent})


# The one sender that is no one's voice: the server speaking for itself. It is named on
# every line and neither is nor makes anyone "the other person".
_TRAX_SENDER: Final = "trackinizer"


@dataclass(frozen=True, slots=True, kw_only=True)
class _Told:
    """What a run's Chat lines have told its CLI so far."""

    context: WorkspaceMessageContext | None = None
    """The context the last line carried, as the model, without the marks."""

    owner: str | None = None
    """The run's own person: whoever sent its first Chat line. The server attests the
    sender but names no owner to the run, and a run starts from its owner's first line."""

    others: bool = False
    """Whether anyone but :attr:`owner` has sent a line."""

    stale: bool = False
    """Whether the CLI may have lost the start note and the last context."""

    def names(self, source: str | None) -> bool:
        """Whether a line from ``source`` must name its sender."""
        return (
            source in (None, _TRAX_SENDER)
            or self.others
            or source != (self.owner or source)
        )

    def after(
        self,
        *,
        source: str | None,
        context: WorkspaceMessageContext | None,
    ) -> _Told:
        """Return what the CLI has been told once a line from ``source`` is typed.

        Args:
          source: The route-attested sender of the line.
          context: The canvas it was sent from, if a Chat line.

        Returns:
          told: This, or what is told once the line is in.

        """
        if context is not None and context.relay is not None:
            return self
        owner, others = self.owner, self.others
        if source not in (None, _TRAX_SENDER):
            if owner is None and context is not None:
                owner = source
            elif source != owner:
                others = True
        if context is None:
            return replace(self, owner=owner, others=others)
        return _Told(
            context=_unmarked(context),
            owner=owner,
            others=others,
        )


def _unmarked(context: WorkspaceMessageContext) -> WorkspaceMessageContext:
    """Return ``context`` without its marks, which go with their line alone."""
    return context.model_copy(update={"marks": []})


def run(config: RunConfig) -> int:
    """Run end-to-end and return the wrapped CLI's exit code.

    Blocks until the CLI exits; dry-run blocks until SIGINT and returns 0.

    Args:
      config: Trax run configuration with CLI name, paths, and options.

    Returns:
      rc: Exit code from the wrapped CLI or 0 for dry-run.

    """
    factory = _ADAPTERS.get(config.cli_name)
    if factory is None:
        raise ValueError(
            f"unsupported CLI {config.cli_name!r}; choose one of {sorted(_ADAPTERS)}",
        )
    adapter = factory()

    if config.syncing and config.client is None:
        config = replace(config, client=Client(base_url=LOCALHOST_FALLBACK_URL))

    sink = _open_sink(config, adapter)
    stats = _Stats()
    try:
        if config.dry_run:
            rc = _dry_run_drain(config, adapter, sink, stats)
        else:
            rc = _spawn_and_drain(config, adapter, sink, stats)
    except PrivateNotHeldError as err:
        sys.stderr.write(f"[trax run] {err}\n")
        return 2
    finally:
        sink.close()

    sys.stderr.write(f"\n[trax run] {stats.render()}\n")
    if stats.switches:
        typed = ", ".join(f"/{command}" for command in dict.fromkeys(stats.switches))
        sys.stderr.write(
            f"[trax run] {typed} can move {adapter.name} into another session, whose"
            " turns this run does not capture; resume a captured session with"
            f" trax agentsession <seq> run {adapter.name}\n",
        )
    return rc


def build_parser() -> argparse.ArgumentParser:
    """Argument parser for ``trax run``.

    Returns:
      parser: Configured ArgumentParser with all subcommands.

    """
    parser = argparse.ArgumentParser(
        prog="trax run",
        description=(
            "Wrap an agent CLI and tail its session log. "
            f"Supported: {', '.join(RunConfig.SUPPORTED_CLIS)}."
        ),
        epilog=(
            "A --detach host is reached by name: trax run ls | attach NAME | "
            "log NAME [-f] | send NAME TEXT | stop NAME."
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
    parser.add_argument(
        "--private",
        action="store_true",
        help=(
            "Start the session hidden: only you, staff and admins read it. Stops "
            "before the CLI starts if the server will not hide it."
        ),
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help=(
            "Show a live git diff of the files this session edits on its Trax "
            "page, refreshed each turn. No effect outside a git work tree."
        ),
    )
    parser.add_argument(
        "--detach",
        action="store_true",
        help=(
            "Run in a background host that outlives this terminal; return once "
            "it is up. Reach it with trax run attach|log|send|stop NAME."
        ),
    )
    parser.add_argument(
        "--name",
        default=None,
        help="The --detach host's name. Defaults to the --as name, else the CLI.",
    )
    parser.add_argument(
        "--attach",
        type=uuid.UUID,
        default=None,
        metavar="UUID",
        help=(
            "Write this run into an existing AgentSession, such as a chat's, as a "
            "new part of it, rather than opening a session of its own."
        ),
    )
    return parser


def resume_argv(cli_name: str, cli_session_id: str | None) -> tuple[str, ...]:
    """Return the tokens that tell ``cli_name`` to continue ``cli_session_id``.

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
    if cli_session_id is None:
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
    profile: str | None = None,
    attach: uuid.UUID | None = None,
    prefix_bytes: int = 0,
    compact_after_load: bool = False,
) -> int:
    """Entry point for ``trax run``, called from ``trax/cli.py``.

    Args:
      argv: ``trax run`` arguments; everything after ``--`` goes to the CLI.
      client_factory: Builds the Trackinizer client from the active profile.
        Invoked only when the run actually syncs, so a ``--no-sync`` or
        ``--out`` capture never resolves a profile or opens a socket.
      resume_path: A materialized transcript this run continues. Not a flag:
        the file is written by the resume tail moments before, so there is no
        spelling of it a human would type. A resume typed after ``--`` is
        found here instead.
      cli_session_id: The id that transcript was materialized under, which
        ``--resume`` takes and which re-attaches the run to its stored
        AgentSession.
      profile: The top-level ``--profile``, if given.
      attach: The AgentSession the run writes into, as ``--attach`` names one; this
        wins over the flag. See :attr:`RunConfig.attach`.
      prefix_bytes: How much of ``resume_path`` is history the session already
        holds; see :attr:`RunConfig.prefix_bytes`.
      compact_after_load: Whether that history is past what the CLI holds
        comfortably; see :attr:`RunConfig.compact_after_load`.

    Returns:
      exit_code: The wrapped CLI's exit status; for ``--detach``, whether the
        host came up.

    """
    if argv and argv[0] in detach.HOST_COMMANDS:
        return detach.main(argv)
    if "--" in argv:
        idx = argv.index("--")
        trax_argv = argv[:idx]
        cli_argv = tuple(argv[idx + 1 :])
    else:
        trax_argv = argv
        cli_argv = ()
    parser = build_parser()
    flags = cast(_Flags, parser.parse_args(trax_argv))
    if flags.name is not None and not flags.detach:
        parser.error("--name names a --detach host")
    if flags.detach and flags.dry_run:
        parser.error("--detach hosts a live CLI, and --dry-run spawns none")
    # Only the flag is refused locally: a resume attaches every run it starts, and one
    # asked not to sync still resumes, uploading nothing.
    if flags.attach is not None and (not flags.sync or flags.out is not None):
        parser.error("--attach writes into a server session, and this run syncs none")
    attach = attach or flags.attach
    run_name = (flags.name or flags.actor or flags.cli) if flags.detach else None
    start = partial(
        _run_flags,
        flags=flags,
        cli_argv=cli_argv,
        client_factory=client_factory,
        resume_path=resume_path,
        cli_session_id=cli_session_id,
        profile=profile,
        attach=attach,
        prefix_bytes=prefix_bytes,
        compact_after_load=compact_after_load,
        run_name=run_name,
    )
    if run_name is not None:
        return detach.launch(
            run_name,
            command=shlex.join((flags.cli, *cli_argv)),
            start=start,
        )
    return start(None)


# A run attached to a chat starts from the chat's conversation, as a switch to its CLI
# would: the CLI resumes a file written from every part of the session, and only what it
# writes after that is uploaded. Lossy, because a CLI that cannot express one record is
# still better off with the rest than with none. Loaded in the process that runs the
# CLI, after a ``--detach`` host forks: the launcher repeats the host's log up to its
# start, so the line still lands on the person's terminal. The client is not closed,
# since the factory may hand out one it keeps.
def _load_window(
    client: Client,
    session_id: uuid.UUID,
    cli: str,
) -> tuple[Path | None, str | None, int, bool]:
    """Write the chat's conversation for ``cli``; return its file, id, size and fit."""
    try:
        written = prepare_resume(client, session_id, cli, lossy=True)
    except (NotResumableError, CiphertextDroppedError, ClientError) as err:
        sys.stderr.write(f"[trax run] {cli} starts without the chat's history: {err}\n")
        return None, None, 0, False
    sys.stderr.write(
        f"[trax run] loaded {written.loaded_records:,} records "
        f"(~{written.estimated_tokens:,} tokens) into {cli}; "
        f"{written.skipped:,} earlier not loaded\n",
    )
    return (
        written.path,
        str(written.cli_session_id),
        written.prefix_bytes,
        written.compact_after_load,
    )


# Decided in the process that runs the CLI: a ``--detach`` host's warning lands in its
# log, which the launcher repeats to the person once the host is up.
#
# A transcript that exists before the CLI starts is in the pre-spawn ``baseline`` and
# would be skipped as an earlier run's, so a resume typed after ``--`` stored nothing.
# Found by its id and named as ``resume_path``, it gets what ``trax agentsession <seq>
# run`` gives the file it materializes. A fork of either copies the transcript into a
# NEW one and never writes the old, so it follows nothing and the run mints the new id.
def _continued_transcript(
    adapter: Adapter,
    cli_args: tuple[str, ...],
    *,
    resume_path: Path | None,
    cli_session_id: str | None,
) -> tuple[Path | None, str | None]:
    """Return the transcript ``cli_args`` continue and its id, warning if unknown."""
    resumed = adapter.resumed_session(cli_args)
    if resumed is None:
        return None, None
    if resume_path is not None:
        return resume_path, cli_session_id
    found = _session_file(adapter, resumed) if resumed else None
    if found is None:
        sys.stderr.write(
            f"[trax run] this run cannot tell which {adapter.name} session it"
            " continues, so it captures none of it; resume a captured session"
            f" with trax agentsession <seq> run {adapter.name}, or name the"
            " session id\n",
        )
        return None, None
    return found, resumed


# Searched for by its id rather than built from the cwd: claude resumes a session filed
# under ANOTHER project directory -- a worktree resuming its main checkout's session --
# and keeps appending to that file, so a path derived from this cwd names nothing.
def _session_file(adapter: Adapter, session_id: str) -> Path | None:
    """Return the transcript ``adapter`` keeps for ``session_id``, if there is one."""
    return next(
        (
            path
            for root in adapter.session_dirs()
            for path in root.rglob(f"*{session_id}*")
            if adapter.matches_session_file(path)
            and adapter.session_id_from_path(path) == session_id
        ),
        None,
    )


# The client is built HERE, inside whichever process runs the CLI, and so is the chat's
# history: a ``--detach`` host calls this after forking, so the launcher opens no
# connection before the fork. One it opened would leave the host a half-used socket
# and, on macOS, the system frameworks the TLS trust check loads, which a forked child
# may not use: the host's first file watch then crashes it.
def _run_flags(
    host: HostSpec | None,
    *,
    flags: _Flags,
    cli_argv: tuple[str, ...],
    client_factory: Callable[[], Client] | None,
    resume_path: Path | None,
    cli_session_id: str | None,
    profile: str | None,
    attach: uuid.UUID | None,
    prefix_bytes: int,
    compact_after_load: bool,
    run_name: str | None,
) -> int:
    """Run the parsed ``trax run`` invocation, served at ``host`` when given."""
    # A chat's run that a person took over has a session its terminal went on with, and
    # the run that replaces it goes on with that one: the turns typed there are in it.
    if (
        flags.attach is not None
        and resume_path is None
        and isinstance(host, detach.HeadlessSpec)
        and host.resume
    ):
        taken = _session_file(_ADAPTERS[flags.cli](), host.resume)
        if taken is not None:
            resume_path, cli_session_id = taken, host.resume
            prefix_bytes = taken.stat().st_size
    if (
        flags.attach is not None
        and resume_path is None
        and not flags.dry_run
        and client_factory is not None
        and _ADAPTERS[flags.cli]().resumed_session(cli_argv) is None
    ):
        resume_path, cli_session_id, prefix_bytes, compact_after_load = _load_window(
            client_factory(),
            flags.attach,
            flags.cli,
        )
    # Prepended, so a caller's own ``--`` args still win over it.
    resuming = (*resume_argv(flags.cli, cli_session_id), *cli_argv)
    if not flags.dry_run:
        resume_path, cli_session_id = _continued_transcript(
            _ADAPTERS[flags.cli](),
            resuming,
            resume_path=resume_path,
            cli_session_id=cli_session_id,
        )
    # A chat's run is hosted: a person's own terminal run keeps its TUI. A driver
    # resumes by the id it is handed, so the resume argument is not its argument.
    headless = (
        flags.attach is not None
        and host is not None
        and flags.cli in _HEADLESS_CLIS
        and not flags.dry_run
    )
    # ``syncing`` off the parsed flags, not off a half-built config: the client
    # is then an ordinary constructor argument and ``RunConfig`` stays frozen.
    syncing = flags.sync and flags.out is None and not flags.dry_run
    return run(
        RunConfig(
            cli_name=flags.cli,
            cli_args=cli_argv if headless else resuming,
            actor=flags.actor or os.environ.get("AGENTNAME", "Agent"),
            rooms=tuple(flags.rooms or ()),
            private=flags.private,
            diff=flags.diff,
            out_path=flags.out,
            verbose=flags.verbose,
            dry_run=flags.dry_run,
            sync=flags.sync,
            client=client_factory() if syncing and client_factory else None,
            resume_path=resume_path,
            cli_session_id=cli_session_id,
            attach=attach,
            prefix_bytes=prefix_bytes,
            compact_after_load=compact_after_load,
            profile=profile,
            host=host,
            run_name=run_name,
            headless=headless,
        ),
    )


# The client carries the active profile's URL, actor, and token, so the sync reaches the
# same authenticated server as every other ``trax`` verb. A run without a resolved
# profile falls back to the localhost default.
#
# The sink is wrapped in a :class:`ResilientSink`: a server failure must not crash the
# drain thread or corrupt the wrapped CLI's terminal, so the run degrades to a local
# JSONL file instead.
def _open_trackinizer_sink(
    config: RunConfig,
    adapter: Adapter,
    *,
    redactor: Redactor | None,
) -> Sink:
    """Build a fault-tolerant :class:`TrackinizerSink` for ``sync`` runs."""
    client = config.client or Client(base_url=LOCALHOST_FALLBACK_URL)
    sys.stderr.write(f"[trax run] syncing events to {client.base_url}\n")
    return ResilientSink(
        TrackinizerSink(
            client,
            adapter.name,
            actor=config.actor,
            rooms=config.rooms,
            private=config.private,
            attach=config.attach,
            redactor=redactor,
            instance=config.instance,
        ),
        fallback_path=_default_out_path(adapter.name),
        redactor=redactor,
    )


# One file per invocation; the name carries the adapter and start timestamp so recent
# captures are easy to find. The caller creates the parent and writes the file.
#
# The pid disambiguates. The timestamp resolves to the second, and the sinks open with
# ``"a"`` -- two runs of the same CLI launched together (a swarm, a script) then
# interleave their events into ONE file, each minting ``seq`` from 0, and neither
# transcript can be recovered from the result.
def _default_out_path(adapter_name: str) -> Path:
    """Default JSONL path under ``state_dir() / "rekursiv-ai" / "trax" / "run"``."""
    stamp = time.strftime("%Y%m%dT%H%M%S")
    base = state_dir() / "rekursiv-ai" / "trax" / "run"
    return base / f"{stamp}-{os.getpid()}-{adapter_name}.jsonl"


# Sync is the default. ``--out`` (explicit local file) and ``--dry-run`` (offline
# replay) force local capture, so neither needs a reachable server.
def _open_sink(config: RunConfig, adapter: Adapter) -> Sink:
    """Open the run's sink: Trackinizer when syncing, else local JSONL."""
    # Every run's sink is touched by the drain, inbound-poll, and main threads,
    # so wrap it in a LockedSink to serialize their access (R2R-024). The
    # dry-run / local-file paths run single-threaded today, but the lock keeps
    # the sink boundary uniformly thread-safe and is effectively free there.
    # Built before any file opens, so a named secret the environment lacks stops the
    # run before it records anything. Redaction is in the sinks, not in
    # ``LockedSink``: ``LockedSink.feed`` calls the inner sink's ``feed``, so a mask
    # there would miss every fed chunk.
    redactor = redactor_from_environ(os.environ)
    if config.syncing:
        return LockedSink(_open_trackinizer_sink(config, adapter, redactor=redactor))
    out_path = config.out_path or _default_out_path(adapter.name)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Line-buffered so an interrupted run still leaves parseable lines.
    handle = out_path.open("a", buffering=1, encoding="utf-8")
    if config.out_path is None:
        sys.stderr.write(f"[trax run] capturing events to {out_path}\n")
    return LockedSink(FileSink(handle, redactor=redactor))


# The CLI runs on a pseudo-terminal the wrapper owns (the relay), so the server can
# splice messages in while the human drives the native TUI. The drain thread reads the
# filesystem directly so it catches session files created after the wrapper starts; the
# live path needs no ``tail`` subprocess. When syncing, an inbound-poll thread injects
# server-queued messages through the relay.
#
# The pre-spawn snapshot excludes old files; the CLI's own session ID excludes
# concurrent new files, including ones created in the same workspace.
def _spawn_and_drain(
    config: RunConfig,
    adapter: Adapter,
    sink: Sink,
    stats: _Stats,
) -> int:
    """Run the CLI on a PTY while daemon threads drain logs and inject inbound."""
    if config.headless:
        return _headless_and_drain(config, adapter, sink, stats)
    owner = _session_owner(adapter, config)
    # A stream adapter names no binary of its own: the command is the ``--``
    # args verbatim (``trax run sh -- bash -c '...'``).
    if isinstance(adapter, StreamAdapter) and not config.cli_args:
        raise SystemExit(
            f"trax run {adapter.name}: no command given; "
            f"usage: trax run {adapter.name} -- CMD [ARGS...]",
        )
    argv = (
        list(config.cli_args)
        if isinstance(adapter, StreamAdapter)
        else _cli_argv(adapter, config, owner)
    )
    # Resolve the binary before forking: after ``pty.fork`` a missing binary
    # would fail in the child's ``execvp``, not here, so the parent could not
    # turn it into a clean ``SystemExit``. Checked before any worker starts, so
    # refusing leaves nothing to stop.
    if shutil.which(argv[0]) is None:
        raise SystemExit(f"trax run: {argv[0]} not found in PATH")
    _prepare_session_dirs(adapter)
    baseline = _existing_session_files(adapter, config)
    stop = threading.Event()
    # Shared by the drain, which sees the CLI's records, and the inbound poller,
    # which waits on them; built before either starts.
    inbound = _inbound_state(adapter)

    # The net worktree diff of the files this run edits, when asked for and when
    # the cwd is in a git work tree; ``None`` otherwise, and the drain loop then
    # does nothing for it.
    worktree_diff = open_worktree_diff(Path.cwd()) if config.diff else None

    # Slash-commands the human types (``/exit``) are handled inside the CLI and
    # never logged, so the drain thread can't see them. The relay tees the
    # human's keystrokes into a detector (main thread); detected commands queue
    # here and the drain thread -- the single sink writer -- emits them, so they
    # serialize with file-sourced events rather than racing the sink.
    slash_queue: deque[tuple[SlashCommand, datetime]] = deque()
    detector = SlashCommandDetector(
        lambda command, at: slash_queue.append((command, at)),
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
    # A stream run's capture source is the PTY stream, not a session log, so a
    # line-framing observer queues each completed line. Lines go into
    # ``stream_queue`` -- NOT straight into the sink: framing runs on the
    # relay's IO path, where a blocking ``sink.feed`` (a slow server POST)
    # would stall terminal mirroring and input delivery.
    stream_capture = (
        LineCapture(partial(_enqueue_stream_line, stream_queue, stats))
        if isinstance(adapter, StreamAdapter)
        else None
    )
    # Records the run writes itself -- the prompts its prompt bridge sees and their
    # ends, the commands it types, its trust card -- queue here from the threads that
    # make them, and the drain thread, the single sink writer, empties it each tick: a
    # hook must never wait on a server POST. All of them go on ONE part of the run's
    # own: an attached run writes into a session that earlier runs wrote too, and a
    # part named alike would land on their positions.
    run_records: deque[Event] = deque()
    record = partial(
        _queue_run_record,
        run_records,
        Path(f"prompts-{uuid.uuid4().hex}"),
    )
    drain_thread = _start_drain(
        adapter,
        sink,
        stats,
        config,
        stop,
        baseline=baseline,
        slash_queue=slash_queue,
        stream_queue=stream_queue,
        run_records=run_records,
        owner=owner,
        worktree_diff=worktree_diff,
        inbound=inbound,
    )

    # Open the session eagerly (before fork) so the server-granted routing
    # handle is in the child env from the start: an agent inside must know its
    # real address (``scientist#2`` on a collision), not the requested name
    # (#453). A local-file / dry-run sink has no server session and returns
    # None, so the requested ``config.actor`` is exported unchanged.
    #
    # Any CLI session id the run knows before the CLI writes -- a resume names it, a
    # fresh claude run mints it -- goes over BEFORE the session opens. A resume then
    # re-attaches the AgentSession stamped with it instead of forking a second one,
    # and a fresh run is stamped at open: backfilled only at close, a run killed
    # before it was reaped unstamped, and its next ``--resume`` stored it all twice.
    # An attached run's sink keeps none: the session it re-opens keeps its own id.
    named = owner.session_id if owner is not None else config.cli_session_id
    granted_actor = _open_session(
        sink,
        named=named,
        stop=stop,
        drain_thread=drain_thread,
    )
    # Placed before the fork, like the routing name, so the child's env holds its
    # workstream and station from the start. A failure leaves it unplaced.
    placement = (
        place_run(
            config.client,
            session_id=sink.session_id,
            environ=os.environ,
            cwd=Path.cwd(),
        )
        if config.syncing and config.client is not None and sink.session_id is not None
        else None
    )
    argv, hook_files = _with_box_hook(
        argv,
        adapter=adapter,
        placed=placement_env(placement),
        profile=config.profile,
        bridge=config.syncing,
    )
    if hook_files is not None:
        # Set before the poller starts, which is the only reader of it.
        inbound.marker = hook_files.ready
    machine = _machine_name()
    trust = (
        _trust_gate(adapter, record=record, inbound=inbound, machine=machine)
        if config.syncing
        else None
    )
    gate, screen = (None, None) if trust is None else trust
    bridge = _start_bridge(
        None if hook_files is None else hook_files.socket,
        emit=record,
        pending=inbound.prompt_pending,
    )
    argv = _with_canvas_note(argv, adapter=adapter, placed=placement_env(placement))
    # Run the CLI on a PTY we own rather than letting it inherit the terminal:
    # owning the master fd is what lets the server splice messages into the
    # live session (the human still drives the native TUI). Byte-transparent,
    # so display fidelity matches running the CLI directly. Export the
    # session's routing identity so an agent inside can address peers
    # (``trax send @other``) and know which rooms it is reachable in.
    # A plain line-reading child (IO-stream run) gets newline-terminated
    # injection: the TUI bracketed-paste protocol would deliver its escape
    # sentinels as literal bytes to a canonical-mode ``read``.
    #
    # The CLI is a new top-level session, so whatever names the LAUNCHING one
    # stays behind: the adapter's markers (see ``parent_session_env``) and our
    # own routing pair -- a run with no rooms would otherwise inherit its
    # launcher's ``TRAX_ROOMS`` and believe it is reachable where it is not.
    #
    # Codex alone prints its session id as it exits, so its owner alone reads
    # the terminal -- and alone looks for that banner once the CLI is gone.
    banner_reader = owner if adapter.name == "codex" else None
    relay = ThreadedRelay(
        argv,
        env=_routing_env(config, granted_actor=granted_actor)
        | placement_env(placement),
        drop_env=_dropped_env(adapter),
        host=config.host,
        on_input=detector.feed,
        on_output=_observers(
            None if stream_capture is None else stream_capture.feed,
            None if banner_reader is None else banner_reader.output,
            None if screen is None else screen.output,
        ),
        on_started=None if owner is None else owner.started,
        bracketed_paste=stream_capture is None,
    )
    if gate is not None:
        gate.relay = relay

    # Poll the server for inbound messages and inject them into the live CLI.
    # Only when syncing: a local-file run has no server session to poll.
    poll_thread: threading.Thread | None = None
    if config.syncing and config.client is not None:
        poll_thread = threading.Thread(
            name="inbound poll",
            target=partial(
                _inbound_poll_loop,
                config.client,
                sink,
                relay,
                stop,
                stream=stream_capture is not None,
                inbound=inbound,
                bridge=bridge,
                run_name=config.run_name,
                machine=machine,
                cwd=_shown_cwd(Path.cwd()),
                cli=adapter.name,
                record=record,
                gate=gate,
                instance=config.instance,
                compact=(
                    f"the loaded conversation is past what {adapter.name} holds "
                    "comfortably"
                    if config.compact_after_load
                    else None
                ),
            ),
            daemon=True,
        )
        poll_thread.start()

    # The teardown runs even when ``relay.run`` raises. Skipping it would leave
    # ``stop`` unset and both daemon threads live, and ``run``'s own ``finally``
    # would then close the sink underneath a drain still emitting into it --
    # exactly the race ``LockedSink`` and the joins exist to rule out.
    rc: int | None = None
    try:
        rc = relay.run()
        if rc == 0 and banner_reader is not None:
            banner_reader.finish_output()
    finally:
        # Before the quiesce, so the ends of the prompts it lets go of are drained.
        if bridge is not None:
            bridge.close()
        # A CLI that exited with its trust card still open quit at the dialog.
        if gate is not None:
            gate.close(cli_exited=rc is not None)
        if hook_files is not None:
            shutil.rmtree(hook_files.settings.parent, ignore_errors=True)
        if stream_capture is not None:
            # Flush a trailing unterminated line so a child that exited mid-line
            # still gets its last words captured. The sink is a LockedSink, so
            # this emit serializes with the drain thread's.
            stream_capture.close()

        _wind_down(config, stop, drain_thread, poll_thread, sink)
    return rc


# Set once the kernel watch is armed. ``Thread.start`` returns when the thread is
# SCHEDULED, not when it has run, and the CLI is spawned on the caller's next
# statement -- so without waiting here the child can write its first record into an
# unwatched directory. Codex does exactly that (``session_meta`` at launch), and no later
# event ever names it.
def _start_drain(
    adapter: Adapter,
    sink: Sink,
    stats: _Stats,
    config: RunConfig,
    stop: threading.Event,
    *,
    baseline: frozenset[Path],
    slash_queue: deque[tuple[SlashCommand, datetime]],
    stream_queue: deque[bytes] | None,
    run_records: deque[Event],
    owner: _SessionOwner | None,
    worktree_diff: WorktreeDiff | None,
    inbound: InboundState,
) -> threading.Thread:
    """Start the drain thread and wait until its watch is armed; return the thread."""
    armed = threading.Event()
    drain_thread = threading.Thread(
        name="drain",
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
            run_records=run_records,
            armed=armed,
            owner=owner,
            worktree_diff=worktree_diff,
            inbound=inbound,
        ),
        daemon=True,
    )
    drain_thread.start()
    if not armed.wait(_ARM_TIMEOUT_SEC):
        # Spawn anyway: a run that captures nothing still beats one that never
        # starts. Say so, since the alternative is a silently empty transcript.
        sys.stderr.write(
            f"[trax run] session-log watch not ready within {_ARM_TIMEOUT_SEC:.0f}s; "
            "starting the CLI anyway (early output may not be captured)\n",
        )
    return drain_thread


def _open_session(
    sink: Sink,
    *,
    named: str | None,
    stop: threading.Event,
    drain_thread: threading.Thread,
) -> str | None:
    """Open the run's session; return the routing handle the server granted, if any."""
    if named is not None:
        sink.set_cli_session_id(named)
    try:
        return sink.open()
    except PrivateNotHeldError:
        # Nothing has spawned, so the drain has nothing to finish.
        stop.set()
        _join_with_watchdog(
            drain_thread,
            deadline=time.monotonic() + _JOIN_DEADLINE_SEC,
        )
        raise


def _wind_down(
    config: RunConfig,
    stop: threading.Event,
    drain_thread: threading.Thread,
    poll_thread: threading.Thread | None,
    sink: Sink,
) -> None:
    """Stop the workers once the CLI is gone, and close the sink under them."""
    # Let the drain thread finish pending reads before stopping it.
    time.sleep(config.quiesce_seconds)
    stop.set()
    # One deadline for the whole teardown: the workers normally stop well
    # inside it, so a slow drain (a retrying server POST) finishes rather than
    # racing ``sink.close`` (R2R-024), while a permanently wedged POST cannot
    # hang the process. The LockedSink's ``close`` then acquires its lock with a
    # short timeout and skips locked teardown rather than deadlocking against a
    # straggler that outlived the deadline.
    deadline = time.monotonic() + _JOIN_DEADLINE_SEC
    _join_with_watchdog(drain_thread, deadline=deadline)
    # Closed BEFORE the poller is joined: it is parked in a request the server
    # holds until a message arrives or the session ends, and this close ends the
    # session. Joined first, every exit would wait out the whole hold. ``run``
    # closes again on every path; the second close is a no-op.
    sink.close()
    # A poller whose run never came up was never started, and there is nothing to join.
    if poll_thread is not None and poll_thread.ident is not None:
        _join_with_watchdog(poll_thread, deadline=deadline)


# The CLI is a new top-level session, so whatever names the LAUNCHING one stays behind:
# the adapter's markers (see ``parent_session_env``) and our own routing pair -- a run
# with no rooms would otherwise inherit its launcher's ``TRAX_ROOMS`` and believe it is
# reachable where it is not.
def _dropped_env(adapter: Adapter) -> frozenset[str]:
    """Return the inherited variables the CLI must not see."""
    return adapter.parent_session_env | {
        "TRAX_ACTOR",
        "TRAX_ROOMS",
        "TRAX_WORKSTREAM",
        "TRAX_WORKSPACE",
    }


# A chat's run has no terminal. The CLI runs through its driver, which hears Chat's
# lines and reports the asks Chat answers, and the same drain and poll threads capture
# and feed it as for a terminal run. The pieces a terminal needs are gone: no trust
# screen (neither CLI asks in this mode), no prompt bridge (the driver is the channel
# that carries an ask), no keystroke detector, no readiness wait (the driver is up
# before the poller starts).
def _headless_and_drain(
    config: RunConfig,
    adapter: Adapter,
    sink: Sink,
    stats: _Stats,
) -> int:
    """Drive the CLI through a driver while daemon threads drain logs and feed lines."""
    # A computer the person declared a sandbox (``trax machine connect --sandbox``) is
    # one where agents act freely, so a headless run there asks nothing.
    sandboxed = machine_host.is_sandbox()
    # A permission the run cannot start with ends it here, before any worker starts,
    # not when the driver is built; the policy is read again, unchanged, then.
    if adapter.name == "codex":
        _ = _codex_policy(config.cli_args, sandboxed=sandboxed)
    owner = _SessionOwner(session_id=config.cli_session_id)
    _prepare_session_dirs(adapter)
    baseline = _existing_session_files(adapter, config)
    stop = threading.Event()
    inbound = InboundState(fallback_sec=0.0)
    worktree_diff = open_worktree_diff(Path.cwd()) if config.diff else None
    run_records: deque[Event] = deque()
    record = partial(
        _queue_run_record,
        run_records,
        Path(f"prompts-{uuid.uuid4().hex}"),
    )
    drain_thread = _start_drain(
        adapter,
        sink,
        stats,
        config,
        stop,
        baseline=baseline,
        slash_queue=deque(),
        stream_queue=None,
        run_records=run_records,
        owner=owner,
        worktree_diff=worktree_diff,
        inbound=inbound,
    )
    granted_actor = _open_session(
        sink,
        named=owner.session_id,
        stop=stop,
        drain_thread=drain_thread,
    )
    placement = (
        place_run(
            config.client,
            session_id=sink.session_id,
            environ=os.environ,
            cwd=Path.cwd(),
        )
        if config.syncing and config.client is not None and sink.session_id is not None
        else None
    )
    placed = placement_env(placement)
    env = _routing_env(config, granted_actor=granted_actor) | placed
    argv, hook_files = _with_box_hook(
        [
            adapter.cli_binary,
            *(
                _claude_permission_args(config.cli_args)
                if adapter.name == "claude"
                else ()
            ),
        ],
        adapter=adapter,
        placed=placed,
        profile=config.profile,
    )
    argv = _with_canvas_note(argv, adapter=adapter, placed=placed)
    model, effort = _model_and_effort(config.cli_args)
    host = config.host
    instance = config.instance
    reporter = (
        None
        if config.client is None
        else StateReporter(partial(_report_state, config.client, sink, instance))
    )
    run = HeadlessRun(
        cli=adapter.name,
        cwd=Path.cwd(),
        resume=config.cli_session_id or "",
        model=model,
        effort=effort,
        make_driver=partial(
            _make_driver,
            config,
            adapter,
            argv=argv,
            env=env,
            sandboxed=sandboxed,
        ),
        make_tui=partial(_make_tui, config, adapter, env=env),
        record=record,
        on_session=owner.adopt,
        on_listening=None if host is None else host.on_listening,
        on_state=None if reporter is None else reporter.report,
        on_takeover=host.on_takeover if isinstance(host, detach.HeadlessSpec) else None,
        control=host.control if isinstance(host, detach.HeadlessSpec) else None,
    )
    poll_thread = (
        threading.Thread(
            name="inbound poll",
            target=partial(
                _inbound_poll_loop,
                config.client,
                sink,
                run,
                stop,
                inbound=inbound,
                run_name=config.run_name,
                machine=_machine_name(),
                cwd=_shown_cwd(Path.cwd()),
                cli=adapter.name,
                record=record,
                instance=instance,
                compact=(
                    f"the loaded conversation is past what {adapter.name} holds "
                    "comfortably"
                    if config.compact_after_load
                    else None
                ),
            ),
            daemon=True,
        )
        if config.syncing and config.client is not None
        else None
    )
    if reporter is not None:
        reporter.start()
    # The teardown runs even when the run raises, for the reason a terminal run's does.
    try:
        return run.run(on_started=None if poll_thread is None else poll_thread.start)
    finally:
        if hook_files is not None:
            shutil.rmtree(hook_files.settings.parent, ignore_errors=True)
        # Before the sink closes the session, which a report to it would follow.
        if reporter is not None:
            reporter.close()
        _wind_down(config, stop, drain_thread, poll_thread, sink)


# The server shows it, and never trusts it: a report from any run but the one holding the
# session's lease changes nothing there, so a report made before the first drain is lost
# and harmless.
def _report_state(client: Client, sink: Sink, instance: str, state: HostState) -> None:
    """Tell the server where the run stands."""
    session_id = sink.session_id
    if session_id is None:
        return
    body = SessionRunState(state=state, instance=instance)
    _ = client.post(session_run_state_path(session_id), body=body.model_dump())


# Claude and codex each have a flag for the model; only claude has one for the effort.
def _model_and_effort(cli_args: tuple[str, ...]) -> tuple[str, str]:
    """Return the model and effort the person passed after ``--``; empty for neither."""
    model = _cli_arg_value(cli_args, "--model") or _cli_arg_value(cli_args, "-m")
    return model or "", _cli_arg_value(cli_args, "--effort") or ""


# Built on demand, once per start: a driver runs once, so a terminal that cannot start
# asks for another.
# Neither driver changes the person's own CLI config: they are a layer over a CLI the
# person logged in to. On a computer declared a sandbox, nothing asks.
def _make_driver(
    config: RunConfig,
    adapter: Adapter,
    *,
    argv: Sequence[str],
    env: Mapping[str, str],
    sandboxed: bool,
) -> Driver:
    """Build the driver that runs ``adapter``'s CLI headless."""
    dropped = _dropped_env(adapter)
    if adapter.name == "claude":
        return ClaudeStreamDriver(
            argv=_env_argv(argv, env={}, drop=dropped - env.keys()),
            env=env,
            permission_mode=_claude_mode(config.cli_args, sandboxed=sandboxed),
        )
    approval_policy, sandbox = _codex_policy(config.cli_args, sandboxed=sandboxed)
    return CodexAppDriver(
        argv=_codex_argv(
            _env_argv(argv, env=env, drop=dropped),
            cli_args=config.cli_args,
        ),
        approval_policy=approval_policy,
        sandbox=sandbox,
        reviewer=_codex_reviewer(config.cli_args),
    )


def _claude_mode(cli_args: tuple[str, ...], *, sandboxed: bool) -> str:
    """Default to automatic permission review, preserving explicit modes."""
    given = _cli_arg_value(cli_args, "--permission-mode")
    if given is not None:
        return given
    if sandboxed or "--dangerously-skip-permissions" in cli_args:
        return "bypassPermissions"
    return "auto"


def _codex_reviewer(cli_args: tuple[str, ...]) -> Reviewer:
    """Default to Approve for me, preserving an explicit reviewer."""
    if "--approve-for-me" in cli_args:
        return "auto_review"
    configured = _codex_config_pairs(cli_args)
    match configured.get("approvals_reviewer", "auto_review").strip("\"' "):
        case "user" | "auto_review" as reviewer:
            return reviewer
        case value:
            raise SystemExit(
                f"trax run codex: unknown approvals reviewer {value!r}; "
                "use user or auto_review",
            )


# The person's own permission rules reach a Chat run of claude as they reach a terminal
# one, even on a sandbox, where claude still refuses what they forbade. The rest of the
# argv is the driver's: the session, the model and the mode it sets itself, and a
# prompt it sends as a line. Each flag here takes this many values, as claude reads it.
_CLAUDE_PERMISSION_FLAGS: Final = {
    "--add-dir": sys.maxsize,
    "--allow-dangerously-skip-permissions": 0,
    "--allowed-tools": sys.maxsize,
    "--allowedTools": sys.maxsize,
    "--disallowed-tools": sys.maxsize,
    "--disallowedTools": sys.maxsize,
    "--settings": 1,
    "--tools": sys.maxsize,
}


def _claude_permission_args(cli_args: tuple[str, ...]) -> list[str]:
    """Return the permission flags in ``cli_args``, each with its values."""
    kept: list[str] = []
    room = 0
    for arg in cli_args:
        if arg.startswith("-"):
            flag, equals, _ = arg.partition("=")
            if flag in _CLAUDE_PERMISSION_FLAGS:
                kept.append(arg)
            room = 0 if equals else _CLAUDE_PERMISSION_FLAGS.get(flag, 0)
        elif room:
            kept.append(arg)
            room -= 1
    return kept


# The person's own permission flags rule a Chat run as they rule a terminal one; the
# computer's default fills only a half they left unset. Codex gives the flag and the
# ``-c`` key the same meaning, and the flag wins when both are given.
def _codex_policy(
    cli_args: tuple[str, ...],
    *,
    sandboxed: bool,
) -> tuple[ApprovalPolicy, SandboxMode]:
    """Return when codex asks and what it may touch unasked."""
    if _BYPASS_FLAGS.intersection(cli_args):
        return "never", "danger-full-access"
    if "--full-auto" in cli_args:
        return "on-request", "workspace-write"
    configured = _codex_config_pairs(cli_args)
    approval = _cli_arg_value(cli_args, "--ask-for-approval") or _cli_arg_value(
        cli_args,
        "-a",
    )
    sandbox = _cli_arg_value(cli_args, "--sandbox") or _cli_arg_value(cli_args, "-s")
    asked = approval or configured.get("approval_policy")
    confined = sandbox or configured.get("sandbox_mode")
    unasked: ApprovalPolicy = "never" if sandboxed else "on-request"
    open_to: SandboxMode = "danger-full-access" if sandboxed else "workspace-write"
    return (
        unasked if asked is None else _approval_policy(asked),
        open_to if confined is None else _sandbox_mode(confined),
    )


# A word outside the ones the driver knows (codex's deprecated ``on-failure``, a typo)
# ends the run before anything starts: running another policy than the one asked for
# would hide the mistake. A ``-c`` value may arrive quoted, as TOML.
def _approval_policy(raw: str) -> ApprovalPolicy:
    """Return ``raw`` as a codex approval policy, or refuse the run."""
    match raw.strip("\"' "):
        case "untrusted" | "on-request" | "never" as word:
            return word
        case _:
            raise SystemExit(
                f"trax run codex: a Chat run cannot start with approval policy {raw!r}; "
                "use untrusted, on-request or never",
            )


def _sandbox_mode(raw: str) -> SandboxMode:
    """Return ``raw`` as a codex sandbox mode, or refuse the run."""
    match raw.strip("\"' "):
        case "read-only" | "workspace-write" | "danger-full-access" as word:
            return word
        case _:
            raise SystemExit(
                f"trax run codex: a Chat run cannot start with sandbox {raw!r}; "
                "use read-only, workspace-write or danger-full-access",
            )


_BYPASS_FLAGS: Final = frozenset(
    {"--dangerously-bypass-approvals-and-sandbox", "--yolo"},
)


# A driver only adds to the environment the run inherited, so what names the launching
# session is unset by ``env`` on the way in, as a terminal run drops it. ``env`` unsets
# before it sets. The driver gives codex's app-server no environment of its own, so the
# run's variables ride here too.
def _env_argv(
    prefix: Sequence[str],
    *,
    env: Mapping[str, str],
    drop: AbstractSet[str],
) -> list[str]:
    """Return ``prefix`` started with ``drop`` unset and ``env`` set."""
    return [
        "env",
        *(arg for name in sorted(drop) for arg in ("-u", name)),
        *(f"{name}={value}" for name, value in env.items()),
        *prefix,
    ]


# The person's own ``-c`` overrides and feature toggles reach the app-server as they
# reach a terminal run. Goals are turned on for the run as they are for a terminal one,
# unless the person said otherwise.
def _codex_argv(prefix: Sequence[str], *, cli_args: tuple[str, ...]) -> list[str]:
    """Return the command that starts codex's app-server."""
    return [
        *prefix,
        "app-server",
        *_codex_overrides(cli_args),
        *([] if _sets_codex_goals(cli_args) else ["--enable", "goals"]),
    ]


# The CLI's own terminal, on the session the driver was on, for a person who took the run
# over. A claude session with no turn yet has no file to resume, so it starts by its id;
# a codex thread with none cannot be resumed at all.
def _make_tui(
    config: RunConfig,
    adapter: Adapter,
    *,
    env: Mapping[str, str],
    session_id: str,
    listening: Callable[[], None],
) -> ThreadedRelay:
    """Build the terminal that continues ``session_id`` in ``adapter``'s own CLI."""
    host = config.host
    if host is None:
        raise RuntimeError("this run has no terminal host to serve a terminal from")
    if _session_file(adapter, session_id) is not None:
        resume = resume_argv(adapter.name, session_id)
    elif adapter.name == "claude":
        resume = ("--session-id", session_id)
    else:
        raise RuntimeError(
            f"{adapter.name} has no turn in this chat yet, so there is nothing to take over",
        )
    argv = _cli_argv(
        adapter,
        replace(config, cli_args=(*resume, *config.cli_args)),
        None,
    )
    return ThreadedRelay(
        _with_canvas_note(argv, adapter=adapter, placed=env),
        env=env,
        drop_env=_dropped_env(adapter),
        host=replace(host, on_listening=listening),
    )


# Claude names its transcript after its session id, so knowing the id up front is what
# tells this run's file from a concurrent run's in the same project. A run that names
# none -- no ``--session-id``, no resumed transcript -- and starts a new session gets
# one minted here and handed to claude; a fork counts, since it writes a NEW file. A
# resume claude picks itself (``--continue``, the picker, a search term) owns nothing:
# claude refuses ``--session-id`` beside an unforked resume, and the file it continues
# is in ``baseline``, so this run captures none of it (``main`` warned).
# Codex mints its own, so only a resumed codex run knows it in advance.
def _session_owner(adapter: Adapter, config: RunConfig) -> _SessionOwner | None:
    """Return the CLI session this run captures, or None when it cannot say."""
    if adapter.name == "claude":
        named = config.cli_session_id or _cli_arg_value(config.cli_args, "--session-id")
        if named is None and adapter.resumed_session(config.cli_args) is None:
            return _SessionOwner(session_id=str(uuid.uuid4()), minted=True)
        return _SessionOwner(session_id=named)
    if adapter.name == "codex":
        return _SessionOwner(session_id=config.cli_session_id)
    return None


# Claude says it is ready through its ``SessionStart`` hook, whose marker is set once
# the hook files exist; its records do not count, since it writes none before the trust
# dialog is answered. A stream child reads lines with no dialog in the way, so it is
# ready from the start.
def _inbound_state(adapter: Adapter) -> InboundState:
    """Return when ``adapter``'s CLI is ready for inbound, before it is spawned."""
    if isinstance(adapter, StreamAdapter):
        return InboundState(fallback_sec=0.0)
    if adapter.name == "claude":
        return InboundState(fallback_sec=_HOOK_READY_SEC)
    return InboundState(
        by_record=True,
        fallback_sec=_RECORD_READY_SEC,
        noted=adapter.name == "codex",
    )


# A paste can come back with its whitespace changed -- line endings, a trailing newline
# -- and a one-line paste was measured stored verbatim; multi-line text was not.
def _spaced(text: str) -> str:
    """Return ``text`` with every run of whitespace collapsed to one space."""
    return " ".join(text.split())


def _cli_argv(
    adapter: Adapter,
    config: RunConfig,
    owner: _SessionOwner | None,
) -> list[str]:
    """Return the argv that spawns a file adapter's CLI."""
    argv = [adapter.cli_binary, *config.cli_args]
    if adapter.name == "claude" and owner is not None and owner.minted:
        assert isinstance(owner.session_id, str)
        argv[1:1] = ["--session-id", owner.session_id]
    elif adapter.name == "codex":
        argv[1:1] = [
            *(() if "--no-daemon" in config.cli_args else ("--no-daemon",)),
            *(() if _sets_codex_goals(config.cli_args) else ("--enable", "goals")),
        ]
    approval = _approval_args(
        adapter.name,
        cli_args=config.cli_args,
        attached=config.attach is not None,
        sandbox=machine_host.is_sandbox(),
    )
    if "--settings" in approval:
        # Ours carries the person's, merged, so theirs leaves the argv.
        argv = [argv[0], *_without_flag(argv[1:], "--settings")]
    argv[1:1] = approval
    return argv


# A Chat run's asks go to Chat, but a CLI left to its own dialog stops there with
# nobody watching, so an attached run gets the interim auto review until one ask path
# ships (JP, 2026-10-21). A computer the person declared a sandbox, with
# ``trax machine connect --sandbox``, is one where agents may act freely, so every run
# there gets no restriction: codex skips its approvals and its own sandbox, and claude
# gets bypassPermissions. The person's own permission flags win over all of it: a flag
# they set is never replaced. ``--yolo`` is codex's hidden alias of its bypass flag, and
# codex refuses the two together (exit 2, 0.163.0).
def _approval_args(
    cli_name: str,
    *,
    cli_args: tuple[str, ...],
    attached: bool,
    sandbox: bool,
) -> list[str]:
    """Return the approval flags to hand ``cli_name``, placed before its own args."""
    given = {arg.partition("=")[0] for arg in cli_args}
    if cli_name == "claude":
        if given & {
            "--dangerously-skip-permissions",
            "--permission-mode",
            "--permission-prompt-tool",
        }:
            return []
        # Allowing bypass picks no mode, so off a sandbox a Chat run still takes auto.
        if sandbox:
            allowed = "--allow-dangerously-skip-permissions" in given
            return [] if allowed else _claude_bypass_args(cli_args)
        return ["--permission-mode", "auto"] if attached else []
    if cli_name == "codex":
        keys = {
            value.partition("=")[0].strip() for value in _codex_config_values(cli_args)
        }
        if keys & {"approval_policy", "approvals_reviewer", "sandbox_mode"} or given & {
            "--approve-for-me",
            "--ask-for-approval",
            "--dangerously-bypass-approvals-and-sandbox",
            "--full-auto",
            "--sandbox",
            "--yolo",
            "-a",
            "-s",
        }:
            return []
        if sandbox:
            return ["--dangerously-bypass-approvals-and-sandbox"]
        return ["-c", 'approvals_reviewer="auto_review"'] if attached else []
    return []


# Bypass mode opens a startup dialog, "WARNING: Claude Code running in Bypass
# Permissions mode", whose default is "No, exit" on a computer that never accepted it,
# and a run nobody watches cannot answer it. The inline setting skips the dialog for
# this launch only: measured on claude 2.1.306, a launch with it opened straight to the
# prompt and left ~/.claude/settings.json and ~/.claude.json untouched. Claude's help
# does not say what two ``--settings`` do, so the person's own inline JSON is merged
# into ours and theirs leaves the argv. Their ``--settings`` as a file, or as JSON that
# is no object, cannot be merged here, and the run takes the auto mode, which opens no
# dialog, rather than risk a bypass that exits at once. Our key wins over theirs: with
# it false the dialog returns and the run exits.
def _claude_bypass_args(cli_args: tuple[str, ...]) -> list[str]:
    """Return the flags that start claude in bypass mode with no startup dialog."""
    given = _cli_arg_value(cli_args, "--settings")
    try:
        settings = {} if given is None else json_loads(given, dict[str, PlainTree])
    except ValueError:
        return ["--permission-mode", "auto"]
    settings["skipDangerousModePermissionPrompt"] = True
    return [
        "--permission-mode",
        "bypassPermissions",
        "--settings",
        json_dumps(settings, separators=(",", ":")),
    ]


# Codex drops ``/goal`` without a word when its goals feature is off, so a run turns it
# on for itself -- an override for this run, never a config write -- unless the person
# said otherwise.
def _sets_codex_goals(args: tuple[str, ...]) -> bool:
    """Whether ``args`` already turn codex's goals feature on or off."""
    toggled = [
        arg.partition("=")[2] if "=" in arg else args[index + 1]
        for index, arg in enumerate(args)
        if arg.startswith(("--enable=", "--disable="))
        or (arg in {"--enable", "--disable"} and index + 1 < len(args))
    ]
    return "goals" in toggled or any(
        value.startswith("features.goals") for value in _codex_config_values(args)
    )


# Every claude run gets the ``SessionStart`` hook, which says when inbound may be typed
# in. The box hook is told about boxes on the station the CLI was placed on, so it runs
# only when the child's environment names one. Claude takes one ``--settings``, so a
# person's own is merged into ours and theirs leaves the argv; one that cannot be read
# is left as they gave it, and the run then waits out the fallback for readiness.
# ``bridge`` adds the hooks that hand claude's prompts to the run's prompt bridge: only
# a run that syncs has a Chat to answer them.
def _with_box_hook(
    argv: list[str],
    *,
    adapter: Adapter,
    placed: Mapping[str, str],
    profile: str | None,
    bridge: bool = False,
) -> tuple[list[str], hook.HookFiles | None]:
    """Return ``argv`` handing claude the run's hooks, and their files, if any."""
    if adapter.name != "claude":
        return argv, None
    given = _cli_arg_value(tuple(argv), "--settings")
    user = None if given is None else hook.read_settings(given)
    if given is not None and user is None:
        return argv, None
    files = hook.prepare(
        profile=profile,
        boxes="TRAX_WORKSPACE" in placed,
        user=user,
        bridge=bridge,
    )
    rest = _without_flag(argv[1:], "--settings")
    return [argv[0], "--settings", str(files.settings), *rest], files


# A bridge that cannot listen leaves each hook unable to connect, which prints nothing,
# so claude asks in its TUI alone, as with no hook.
def _start_bridge(
    socket: Path | None,
    *,
    emit: Callable[[SystemMessage], None],
    pending: threading.Event,
) -> PromptBridge | None:
    """Start the prompt bridge on ``socket``; ``None`` when there is none to start."""
    if socket is None:
        return None
    bridge = PromptBridge(socket, emit=emit, pending=pending)
    try:
        bridge.start()
    except OSError:
        _logger.warning("trax run: the prompt bridge cannot listen", exc_info=True)
        return None
    return bridge


def _queue_run_record(
    run_records: deque[Event],
    part: Path,
    record: SystemMessage,
) -> None:
    """Queue a record the run writes itself, on the run's own part."""
    run_records.append(Event(record=record, path=part))


# Read before the CLI starts, read-only: trust is the person's decision, and the CLI
# saves it when they accept. Claude's read is exact, so its card opens at once. Codex's
# read is not -- whether it inherits a parent folder's trust is unverified -- so it only
# arms a watch, and the card opens when codex's screen asks. The line is printed here,
# before the CLI draws its screen, which a line written later would tear.
def _trust_gate(
    adapter: Adapter,
    *,
    record: Callable[[SystemMessage], None],
    inbound: InboundState,
    machine: str | None,
) -> tuple[TrustGate, TrustScreen | None] | None:
    """Return the gate for a CLI not trusted in this folder, and the screen it awaits."""
    cwd = Path.cwd()
    # By $HOME, as each CLI finds its own config, not by the account's home folder.
    home = Path.home()  # noqa: TID251 -- vendor fixed path, not ours (AGENTS.md rule 3)  # house-ignore[xdg-literal] -- Vendor CLI's fixed home path, not ours (AGENTS.md rule 3).
    match adapter.name:
        case "claude":
            trusted = claude_trusted(cwd, environ=os.environ, home=home)
            remembered = claude_remembers(cwd, home=home)
        case "codex":
            trusted = codex_trusted(cwd, environ=os.environ, home=home)
            remembered = True
        case _:
            return None
    if trusted:
        return None
    folder = _shown_cwd(cwd) or str(cwd)
    gate = TrustGate(
        emit=record,
        pending=inbound.trust_pending,
        ready=inbound.ready,
        rearm=inbound.rearm,
    )
    sys.stderr.write(
        f"[trax run] {adapter.name} is not trusted in {folder} yet: answer its prompt "
        "here, or in Chat.\n",
    )
    card = partial(
        gate.open,
        adapter.name,
        folder,
        machine=machine,
        remembered=remembered,
    )
    if adapter.name == "claude":
        card()
        return gate, None
    return gate, TrustScreen(card)


def _observers(
    *observers: Callable[[bytes], None] | None,
) -> Callable[[bytes], None] | None:
    """Return one observer of the CLI's output calling each given, or ``None``."""
    present = tuple(each for each in observers if each is not None)
    if len(present) > 1:
        return partial(_observe_each, present)
    return next(iter(present), None)


def _observe_each(observers: tuple[Callable[[bytes], None], ...], raw: bytes) -> None:
    """Hand one chunk of the CLI's output to each observer, in order."""
    for observe in observers:
        observe(raw)


def _without_flag(args: list[str], flag: str) -> list[str]:
    """Return ``args`` without ``flag`` and its value, in split or equals form."""
    kept: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg == flag:
            skip = True
        elif not arg.startswith(f"{flag}="):
            kept.append(arg)
    return kept


# A placed agent has a canvas only its environment names, and one never told of it looks
# for its own product's: asked for "the canvas", codex built a ChatGPT page. Each CLI
# hears of it through its own instruction flag, and one the person passed wins: theirs
# is deliberate, and a codex ``-c`` of the same key would replace it.
def _with_canvas_note(
    argv: list[str],
    *,
    adapter: Adapter,
    placed: Mapping[str, str],
) -> list[str]:
    """Return ``argv`` telling the CLI of the station it was placed on, if any."""
    if "TRAX_WORKSPACE" not in placed:
        return argv
    given = tuple(argv[1:])
    if adapter.name == "claude" and not any(
        _cli_arg_value(given, flag=flag) is not None
        for flag in ("--append-system-prompt", "--append-system-prompt-file")
    ):
        return [argv[0], "--append-system-prompt", _CANVAS_NOTE, *argv[1:]]
    if adapter.name == "codex" and not any(
        value.startswith("developer_instructions")
        for value in _codex_config_values(given)
    ):
        return [argv[0], "-c", f'developer_instructions="{_CANVAS_NOTE}"', *argv[1:]]
    return argv


def _codex_config_values(args: tuple[str, ...]) -> list[str]:
    """Return the value of each ``-c``/``--config`` override in ``args``."""
    return [
        arg.partition("=")[2] if arg.startswith("--config=") else args[index + 1]
        for index, arg in enumerate(args)
        if arg.startswith("--config=")
        or (arg in {"-c", "--config"} and index + 1 < len(args))
    ]


def _codex_config_pairs(args: tuple[str, ...]) -> dict[str, str]:
    """Return each ``-c key=value`` override in ``args`` by key; the last one wins."""
    pairs = (value.partition("=") for value in _codex_config_values(args))
    return {key.strip(): raw.strip() for key, _, raw in pairs}


def _codex_overrides(args: tuple[str, ...]) -> list[str]:
    """Return ``args``' config overrides and feature toggles, each as a flag and value."""
    kept: list[str] = []
    for index, arg in enumerate(args):
        flag, equals, value = arg.partition("=")
        if flag not in {"-c", "--config", "--enable", "--disable"}:
            continue
        if equals:
            kept += ["-c" if flag == "--config" else flag, value]
        elif index + 1 < len(args):
            kept += ["-c" if flag == "--config" else flag, args[index + 1]]
    return kept


def _cli_arg_value(args: tuple[str, ...], flag: str) -> str | None:
    """Return a CLI flag's value in split or equals form, if supplied."""
    for index, arg in enumerate(args):
        if arg == flag and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith(f"{flag}="):
            return arg.partition("=")[2]
    return None


# A too-short ``join`` made thread ownership non-binding, so ``sink.close`` could race
# an in-flight ``emit`` / ``flush`` (R2R-024). Waiting lets the worker normally stop
# first; a thread still alive at the deadline (a wedged blocking call) is logged and
# left rather than hanging ``trax run`` forever, and ``LockedSink.close`` then declines
# to block on the lock that straggler still holds.
# The warning names the thread by its own ``name`` (``drain`` / ``inbound poll``), which
# also lets ``py-spy`` and tracebacks identify a straggler.
def _join_with_watchdog(
    thread: threading.Thread,
    *,
    deadline: float,
) -> None:
    """Join ``thread`` until ``deadline``, warning if it outlives it."""
    thread.join(timeout=max(0.0, deadline - time.monotonic()))
    if thread.is_alive():
        sys.stderr.write(
            f"[trax run] {thread.name} thread did not stop before the teardown "
            "deadline; proceeding to close\n",
        )


# ``deque(maxlen=...)`` evicts the oldest silently; a full queue here means the sink has
# stalled while the child keeps printing, and each eviction is a captured-line loss.
# Count it (surfaces in the end-of-run stats line as ``StreamEventDropped=N``) and WARN
# once per run on the first drop so the loss is diagnosable rather than a quiet gap in
# the transcript.
def _enqueue_stream_line(queue: deque[bytes], stats: _Stats, raw: bytes) -> None:
    """Queue one framed stream line for the drain thread, overflow VISIBLE."""
    if len(queue) == queue.maxlen:
        if stats.counts.get("StreamEventDropped", 0) == 0:
            _logger.warning(
                "stream capture queue full (%d); dropping oldest lines until "
                "the sink drains",
                queue.maxlen,
            )
        stats.record("StreamEventDropped")
    queue.append(raw)


# A hostname that makes no machine name, one ``trax machine connect`` refuses too, leaves
# the run unnamed rather than stopping it.
def _machine_name() -> str | None:
    """Return this computer's machine name, as ``trax machine connect`` names it."""
    try:
        return host_name()
    except ClientError:
        return None


# The server refuses a drain that names a longer folder or one with a control character
# in it, and a refused drain delivers nothing: such a folder goes unsaid instead. So does
# one whose name is not UTF-8 (a surrogate from ``os.fsdecode``), which no request can
# carry: the client would fail every drain. The home directory is the account's, as the
# terminal host starts its shells in.
def _shown_cwd(cwd: Path) -> str | None:
    """Return ``cwd`` as Chat shows it: ``~/...`` under the home directory."""
    try:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except KeyError:
        home = None
    shown = (
        "/".join(("~", *cwd.relative_to(home).parts))
        if home is not None and cwd.is_relative_to(home)
        else str(cwd)
    )
    if len(shown) > 1_024 or any(
        ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F or 0xD800 <= ord(c) <= 0xDFFF
        for c in shown
    ):
        return None
    return shown


# The server HOLDS each request until a message arrives, so this is one parked request
# per session rather than one round trip every interval, and a message reaches the CLI
# when it is sent rather than up to an interval later. The request returns empty at its
# own ceiling; this re-arms.
#
# ``poll_interval`` is no longer the delivery latency -- only the gap before re-arming
# after a FAILURE, the wait for a session id that has not been minted yet (it appears
# on the first captured event), and how often a CLI not yet ready is looked at again.
# ``stream`` says what kind of child consumes the submissions (see ``render_inbound``'s
# envelope shaping).
#
# Not one message is drained until ``inbound`` says the CLI can take it: the drain
# consumes on the server, so holding the TYPING would not be enough. ``None`` drains at
# once, for a caller with no CLI startup to wait out.
#
# While a prompt or a trust card is open the drain goes on, since its answer comes this
# way, but a line or a command it brings is held here: typed in, it would answer the
# dialog. A trust card opens the drain before the CLI is ready, so lines are held until
# it is. Once nothing is open and the CLI is ready, the held ones go first, in order,
# and while any is held or a card is open the drain asks the server for no longer than
# ``poll_interval``, so they go soon after and the card sees the terminal's answer.
#
# ``compact`` is why the run types ``/compact`` before anything else: the history it
# loaded is past what the CLI holds comfortably, and the CLI's own compaction
# summarizes it.
#
# Server errors are swallowed: a flaky back-channel must not crash the run or corrupt
# the terminal, exactly like the capture sink's resilience.
#
# Every drain says where the run is -- its ``--detach`` host, machine and folder -- so
# Chat shows it for as long as the lease the drain renews holds. It also names the CLI
# this run is and an id the run minted for itself: the server routes and judges liveness
# by that CLI, not by what the run wrote, and refuses a second run of one chat.
#
# A drain the chat refuses for good -- another provider now answers it, or another run
# holds it -- ends the run: the poll would be refused again, and a CLI left running that
# no line can reach is a second run in all but name.
#
# A headless run takes its messages by kind from the run itself rather than typing them:
# a line goes to its driver, an answer to the ask it names. It has no dialog to hold a
# line back from, so it holds none.
def _inbound_poll_loop(
    client: Client,
    sink: Sink,
    relay: ThreadedRelay | HeadlessRun,
    stop: threading.Event,
    *,
    poll_interval: float = 0.5,
    wait_sec: float = _INBOUND_WAIT_SEC,
    stream: bool = False,
    inbound: InboundState | None = None,
    bridge: PromptBridge | None = None,
    run_name: str | None = None,
    machine: str | None = None,
    cwd: str | None = None,
    cli: str = "",
    record: Callable[[SystemMessage], None] | None = None,
    gate: TrustGate | None = None,
    compact: str | None = None,
    instance: str | None = None,
) -> None:
    """Wait on server-queued inbound messages and type them into the CLI."""
    warned = False
    instance = instance or uuid.uuid4().hex
    held: deque[InboundDrainItem] = deque()
    deliver = (
        partial(_deliver_headless, relay, inbound=inbound, record=record)
        if isinstance(relay, HeadlessRun)
        else partial(
            _deliver_one,
            relay,
            stream=stream,
            inbound=inbound,
            bridge=bridge,
            cli=cli,
            record=record,
            gate=gate,
        )
    )
    while not stop.is_set():
        session_id = sink.session_id
        if session_id is None:
            # No session yet; nothing to wait on. Re-check on the interval.
            if stop.wait(poll_interval):
                break
            continue
        if inbound is not None and not inbound.wait_open(stop, poll_sec=poll_interval):
            break
        if gate is not None and inbound is not None:
            gate.check(ready=inbound.ready())
        if compact is not None and not _holding(inbound):
            command = InboundDrainItem(
                text="/compact",
                kind="command",
                source="trax run",
            )
            if isinstance(relay, HeadlessRun):
                _deliver_headless_command(
                    relay,
                    command,
                    inbound=inbound,
                    record=record,
                    reason=compact,
                )
            else:
                _deliver_command(
                    relay,
                    command,
                    cli=cli,
                    inbound=inbound,
                    record=record,
                    reason=compact,
                )
            compact = None
        while held and not _holding(inbound):
            deliver(held.popleft())
        try:
            batch = client.drain_inbound(
                session_id,
                wait_sec=poll_interval if held or _trusting(inbound) else wait_sec,
                run=run_name,
                machine=machine,
                cwd=cwd,
                cli=cli or None,
                instance=instance,
            )
            # Each submission is guarded on its own. ``drain_inbound`` CONSUMES
            # server-side, so the batch is already gone from the queue: one
            # message that cannot be typed must not take its siblings with it,
            # since no later drain will ever return them again.
            for item in batch:
                if item.kind in {"line", "command"} and (held or _holding(inbound)):
                    held.append(item)
                else:
                    deliver(item)
            warned = False  # Recovered; allow a fresh warning next outage.
            # No sleep: the request itself was the wait, so re-arming
            # immediately is what keeps the channel continuously parked.
            continue
        except Exception as error:
            if isinstance(error, ClientError) and (
                error.status_code == 409 and error.code in RUN_ENDING_REFUSALS
            ):
                sys.stderr.write(
                    "[trax run] this chat no longer takes lines from this run "
                    f"({error.code}); stopping the run\n",
                )
                # The session is the chat's, shared with the run it keeps: this
                # run's exit must not end it, or it takes that run's lines with it.
                sink.keep_session_open()
                relay.terminate()
                return
            # Warn once per outage on stderr (mirrors the capture sink's
            # degrade banner) so a token expiry / network drop is visible,
            # not a silent stop; repeats stay at DEBUG to avoid flooding.
            if not warned:
                sys.stderr.write(
                    "[trax run] inbound delivery failed; messages will not be "
                    "delivered until it recovers\n",
                )
                warned = True
            _logger.debug("inbound wait failed", exc_info=True)
        # Only a failure lands here: back off before re-arming so a persistent
        # outage is not a hot retry loop.
        if stop.wait(poll_interval):
            break


def _holding(inbound: InboundState | None) -> bool:
    """Whether a typed line must wait: a dialog would take it, or the CLI is not up."""
    return inbound is not None and (
        inbound.prompt_pending.is_set()
        or inbound.trust_pending.is_set()
        or not inbound.ready()
    )


def _trusting(inbound: InboundState | None) -> bool:
    """Whether a trust card waits for its answer."""
    return inbound is not None and inbound.trust_pending.is_set()


# What is typed is remembered BEFORE it is typed: the CLI can write its record of the
# line before ``submit`` returns, and the drain thread would then meet a line it had not
# been told of.
#
# An answer is never typed: the trust gate or the prompt bridge takes it, whichever
# holds the card it names. Typed, its Enter would pick whatever the dialog highlighted.
def _deliver_one(
    relay: ThreadedRelay,
    item: InboundDrainItem,
    *,
    stream: bool,
    cli: str,
    inbound: InboundState | None = None,
    bridge: PromptBridge | None = None,
    record: Callable[[SystemMessage], None] | None = None,
    gate: TrustGate | None = None,
) -> None:
    """Deliver one inbound message as its kind says; a failure is logged."""
    if item.kind == "answer":
        payload = item.payload or {}
        if gate is not None and gate.holds(payload.get("prompt_id")):
            _ = gate.answer(payload)
        elif bridge is None or not bridge.answer(payload):
            _logger.info("trax run: an answer names no open prompt: %s", item.payload)
        return
    if item.kind == "command":
        _deliver_command(relay, item, cli=cli, inbound=inbound, record=record)
        return
    rendered = render_inbound(
        item.text,
        item.source,
        item.room,
        context=item.context,
        stream=stream,
        kind=item.kind,
        disclosure=_disclosure(item, inbound=inbound),
    )
    if rendered is None:
        _logger.info("trax run: a %s message is not typed: %s", item.kind, item.payload)
        return
    try:
        if isinstance(rendered, bytes):
            relay.write(rendered)
            return
        if inbound is not None:
            inbound.typed(
                rendered,
                source=item.source,
                room=item.room,
                kind=item.kind,
                context=item.context,
            )
        relay.submit(rendered)
    except Exception as error:
        if inbound is not None and isinstance(rendered, str):
            inbound.withdraw(rendered)
        _record_delivery_failure(item, error, record=record)
        _logger.warning(
            "trax run: could not deliver an inbound message; "
            "continuing with the rest of the batch",
            exc_info=True,
        )


def _disclosure(item: InboundDrainItem, *, inbound: InboundState | None) -> Disclosure:
    """Return what the line says of itself: all but the canvas commands, with no run."""
    if inbound is None:
        return Disclosure()
    return inbound.disclosure(source=item.source, context=item.context)


# A command is typed bare, never as a person's line: with a sender before it the CLI
# reads ``/goal x`` as a prompt. It is not remembered for stamping either: no CLI writes
# a user message of a command it ran, so a remembered one would stamp a later identical
# line typed at the terminal. The run writes its own record instead, typed or refused.
#
# Codex asks "Replace goal?" in a modal when a goal is set over an open one, and the
# next line typed would answer it, so that ``/goal`` is refused. Codex logs no clear,
# so a typed ``/goal clear`` closes the watch here.
def _deliver_command(
    relay: ThreadedRelay,
    item: InboundDrainItem,
    *,
    cli: str,
    inbound: InboundState | None,
    record: Callable[[SystemMessage], None] | None,
    reason: str | None = None,
) -> None:
    """Type one command into the CLI, or refuse it; record which, if a record is kept."""
    command = parse_command(item.text)
    goal = None if inbound is None else inbound.goal
    refused = (
        cli == "codex" and goal is not None and goal.open and replaces_goal(command)
    )
    if refused:
        reason = "codex is following a goal: send /goal clear, then the new goal"
    else:
        try:
            relay.submit(item.text)
        except Exception as error:
            _record_delivery_failure(item, error, record=record)
            _logger.warning(
                "trax run: could not type a command; continuing with the rest",
                exc_info=True,
            )
            return
        if goal is not None and command == parse_command("/goal clear"):
            goal.cleared()
    if record is not None:
        record(
            command_record(
                item.text,
                source=item.source,
                room=item.room,
                state="refused" if refused else "typed",
                reason=reason,
            ),
        )


# What is sent is remembered BEFORE it is sent, as a typed line is: the CLI can write its
# record of the line before the driver's call returns. A line a terminal has taken the CLI
# from is refused, and forgotten again.
def _deliver_headless(
    run: HeadlessRun,
    item: InboundDrainItem,
    *,
    inbound: InboundState | None,
    record: Callable[[SystemMessage], None] | None,
) -> None:
    """Hand one inbound message to the headless run as its kind says; a failure is logged."""
    try:
        match item.kind:
            case "answer":
                payload = from_plain(item.payload or {}, dict[str, PlainTree])
                answer = answer_from_payload(payload)
                run.answer(replace(answer, who=item.source or ""))
            case "interrupt":
                run.interrupt()
            case "settings":
                model, effort = settings_from_payload(item.payload)
                run.settings(model=model, effort=effort)
            case "command":
                _deliver_headless_command(run, item, inbound=inbound, record=record)
            case "line":
                _deliver_headless_line(run, item, inbound=inbound)
    except (TakenOverError, RunEndedError) as error:
        _record_delivery_failure(item, error, record=record, refused=True)
        _logger.info("trax run: a %s message was not taken: %s", item.kind, error)
    except Exception as error:
        _record_delivery_failure(item, error, record=record)
        _logger.warning(
            "trax run: could not deliver an inbound message; "
            "continuing with the rest of the batch",
            exc_info=True,
        )


def _deliver_headless_line(
    run: HeadlessRun,
    item: InboundDrainItem,
    *,
    inbound: InboundState | None,
) -> None:
    """Send a person's line to the driver, remembering who sent it."""
    rendered = render_inbound(
        item.text,
        item.source,
        item.room,
        context=item.context,
        disclosure=_disclosure(item, inbound=inbound),
    )
    if inbound is not None:
        inbound.typed(
            rendered,
            source=item.source,
            room=item.room,
            kind="line",
            context=item.context,
        )
    try:
        run.send_line(rendered)
    except Exception:
        if inbound is not None:
            inbound.withdraw(rendered)
        raise


# Not remembered for stamping, as a typed command is not: no CLI writes a user message of
# a command it ran. The run writes its own record instead, typed or refused, which is how
# Chat settles the line it sent. A driver runs a goal over an open one, where codex's
# terminal asks first, so no command is refused for that.
def _deliver_headless_command(
    run: HeadlessRun,
    item: InboundDrainItem,
    *,
    inbound: InboundState | None,
    record: Callable[[SystemMessage], None] | None,
    reason: str | None = None,
) -> None:
    """Run one command on the driver, or refuse it; record which, if a record is kept."""
    command = parse_command(item.text)
    state: Literal["typed", "refused"] = "typed"
    try:
        run.command(command.command, command.args)
    except (TakenOverError, RunEndedError, ValueError) as error:
        state = "refused"
        reason = str(error)
    else:
        if inbound is not None and command == parse_command("/goal clear"):
            inbound.goal.cleared()
    if record is not None:
        record(
            command_record(
                item.text,
                source=item.source,
                room=item.room,
                state=state,
                reason=reason,
            ),
        )


def _record_delivery_failure(
    item: InboundDrainItem,
    error: Exception,
    *,
    record: Callable[[SystemMessage], None] | None,
    refused: bool = False,
) -> None:
    """Report a drained message's failure without claiming an uncertain send was refused."""
    if record is None:
        return
    record(
        SystemMessage(
            timestamp=datetime.now(UTC).isoformat(),
            subtype="delivery_failed",
            content=item.text,
            extra={
                "$delivery": {
                    "text": item.text,
                    "kind": item.kind,
                    "source": item.source,
                    "refused": refused,
                    "reason": (str(error).splitlines() or [type(error).__name__])[0][
                        :500
                    ],
                },
            },
        ),
    )


# ``TRAX_ACTOR`` is the session's granted routing handle and ``TRAX_ROOMS`` its comma-
# joined rooms, so an agent inside the session can address peers (``trax send @other``)
# and tell them its real address.
#
# ``granted_actor`` is the server-granted handle, known on the sync path because the
# session opens eagerly (before fork). On a collision the server routes the session as
# ``scientist#2``; the child must see that, not the requested ``scientist`` (#453). When
# ``None`` -- a ``--no-sync`` / ``--out`` / ``--dry-run`` run with no collision arbiter
# -- the requested ``config.actor`` is exported as-is.
def _routing_env(
    config: RunConfig,
    *,
    granted_actor: str | None = None,
) -> dict[str, str]:
    """Return the routing identity to export into the wrapped CLI's environment."""
    env: dict[str, str] = {}
    actor = granted_actor or config.actor or ""
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


# Files in this set existed before the child started. The follower ignores them
# unless this run is resuming one (``RunConfig.resume_path``).
#
# A ``resume_path`` is EXEMPT: this run is about to continue it, so excluding it would
# drop the very transcript being resumed.
def _existing_session_files(adapter: Adapter, config: RunConfig) -> frozenset[Path]:
    """Snapshot the matching session files present before the run starts."""
    found: set[Path] = set()
    for session_dir in adapter.session_dirs():
        if not session_dir.is_dir():
            continue
        for path in session_dir.rglob("*"):
            if path.is_file() and adapter.matches_session_file(path):
                found.add(path)
    return frozenset(found - {config.resume_path})


# Wake-driven, not polled. The cost of polling was never the tick -- it was the
# DISCOVERY inside it: claude's ``session_dirs()`` returned every project directory the
# CLI had ever used, and re-walking them cost 86ms of every 0.2s pass. The directories
# are walked ONCE here, to arm a watch; the kernel then names each changed file.
#
# ``baseline`` skips files present at startup; the owned session ID distinguishes
# simultaneous new files. There is no mtime floor: the watch is armed before spawn.
#
# ``slash_queue`` carries slash-commands the relay's keystroke detector produced on the
# main thread; ``stream_queue`` carries events a stream adapter's LineCapture parsed on
# the relay's IO path. This loop owns the sink, so both drain and emit here rather than
# have another thread touch the sink (or block the relay on a slow server POST). Neither
# has a filesystem event to ride on, so a short wait bounds how long a queued item can
# sit -- that wait is for the QUEUES, never for the files.
#
# ``armed`` is set once the kernel watch exists, so the caller can hold the child's
# spawn until then; a run with no filesystem source sets it too, since there is nothing
# to wait for.
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
    run_records: deque[Event] | None = None,
    armed: threading.Event | None = None,
    owner: _SessionOwner | None = None,
    worktree_diff: WorktreeDiff | None = None,
    inbound: InboundState | None = None,
) -> None:
    """Watch the adapter's session dirs; emit records as lines are appended."""
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
            run_records=run_records,
            armed=armed,
            owner=owner,
            worktree_diff=worktree_diff,
            inbound=inbound,
        ),
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
    run_records: deque[Event] | None = None,
    replay: bool = False,
    armed: threading.Event | None = None,
    owner: _SessionOwner | None = None,
    worktree_diff: WorktreeDiff | None = None,
    inbound: InboundState | None = None,
) -> None:
    """Follow the session logs and drain the queues until ``stop``."""
    emit = partial(
        _emit_line,
        adapter,
        sink,
        stats,
        config,
        worktree_diff=worktree_diff,
        prefix=(
            _Prefix(path=config.resume_path, size=config.prefix_bytes)
            if config.resume_path is not None and config.prefix_bytes
            else None
        ),
        inbound=inbound,
    )
    watched = tuple(d for d in adapter.session_dirs() if d.is_dir())
    stream_part = Path(f"stream-{uuid.uuid4().hex}")
    lines: asyncio.Queue[_Captured] = asyncio.Queue(maxsize=_FILE_QUEUE_MAX)
    finish = asyncio.Event()
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
                until=finish,
                owner=owner,
            ),
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
                stream_part=stream_part,
                run_records=run_records,
            )
            await _drain_lines(lines, emit=emit)
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
            #
            # A timeout is a quiet gap -- the turn's end, near enough -- so the
            # worktree diff refreshes there (cheap unless an edit made it due).
            try:
                captured = await asyncio.wait_for(lines.get(), _QUEUE_DRAIN_SEC)
            except TimeoutError:
                _refresh_diff(worktree_diff, sink, force=False)
            else:
                emit(captured)
        # The CLI has exited (or the replay is done), so its files are final:
        # the follower reads them to their ends and returns. Cancelling it
        # instead lost any file the kernel had not named yet -- on macOS a new
        # file waits on FSEvents, measured up to 4.6s late under load and
        # sometimes not within 10s, so a CLI that wrote and exited inside that
        # window left an empty transcript.
        finish.set()
        if follower is not None:
            while not follower.done() or not lines.empty():
                with contextlib.suppress(TimeoutError):
                    captured = await asyncio.wait_for(lines.get(), _QUEUE_DRAIN_SEC)
                    emit(captured)
            await follower
        await _drain_queues(
            adapter,
            sink,
            stats,
            config,
            slash_queue=slash_queue,
            stream_queue=stream_queue,
            stream_part=stream_part,
            run_records=run_records,
        )
        await _drain_lines(lines, emit=emit)
        _flush(sink)
        # The last turn's edits may have arrived with no quiet gap after them;
        # force a final refresh while the session is still live (before close).
        _refresh_diff(worktree_diff, sink, force=True)
    finally:
        if follower is not None:
            _ = follower.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await follower


# Errors are logged and the watch is REBUILT rather than abandoned: the causes are
# transient and external (an inotify instance or watch limit hit while another process
# churns directories), and a follower that returned would leave the drain loop ticking
# against a queue nothing fills -- capture dead for the rest of the run, with one log
# line an hour earlier as the only sign. It returns once ``until`` is set and the files
# have been read to their ends.
async def _follow_session_files(
    adapter: Adapter,
    watched: tuple[Path, ...],
    baseline: frozenset[Path],
    lines: asyncio.Queue[_Captured],
    *,
    until: asyncio.Event,
    replay: bool = False,
    resume: frozenset[Path] = frozenset(),
    armed: threading.Event | None = None,
    owner: _SessionOwner | None = None,
) -> None:
    """Feed every line this run's session files gain onto ``lines``."""
    # The subtree this run's own files land in, when the CLI derives one from
    # the working directory. Resolved ONCE: a wrapped CLI cannot change the
    # runner's cwd, and re-deriving per candidate would pay a ``resolve`` on
    # every event.
    mine = partial(
        _owned,
        adapter=adapter,
        baseline=baseline,
        scope=adapter.session_scope(),
        resume=resume,
        owner=owner,
    )
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
            until=until,
            replay=replay or rearming,
            resume=resume,
            armed=armed,
        )
        if until.is_set():
            return
        rearming = True
        # Before ``until``, only a failure returns. Back off so a persistent
        # refusal is not a hot retry loop.
        await asyncio.sleep(_WATCH_REARM_SEC)


# The baseline excludes old files; the adapter identifies transcripts; the scope
# narrows workspace-based layouts; and the owned session ID distinguishes new files from
# simultaneous runs in the same workspace. A resumed transcript is exempt from the scope
# as it is from the baseline: claude keeps appending to one filed under ANOTHER
# project's directory when that is where the session started.
def _owned(
    path: Path,
    *,
    adapter: Adapter,
    baseline: frozenset[Path],
    scope: Path | None,
    resume: frozenset[Path],
    owner: _SessionOwner | None,
) -> bool:
    """Return whether this run should capture ``path``."""
    if path in baseline or not adapter.matches_session_file(path):
        return False
    if scope is not None and scope not in path.parents and path not in resume:
        return False
    return owner is None or owner.owns(adapter, path)


async def _watch_session_files(
    adapter: Adapter,
    watched: tuple[Path, ...],
    lines: asyncio.Queue[_Captured],
    bodies: dict[Path, str],
    mine: Callable[[Path], bool],
    *,
    until: asyncio.Event,
    replay: bool,
    resume: frozenset[Path] = frozenset(),
    armed: threading.Event | None = None,
) -> None:
    """Arm one watch and feed it until it fails or ``until`` ends it."""
    try:
        if adapter.whole_file:
            # Gemini rewrites ONE json object in place, so a line is a fragment
            # of it and parses as nothing. Its body is re-read whole on each
            # change instead; the watch says which file changed, which is all
            # the old poll's ``(size, mtime)`` stamp was determining.
            async with follow_dir(*watched, until=until) as changed:
                if armed is not None:
                    armed.set()
                if replay:
                    # A dry run replays what is already on disk. Nothing is
                    # going to change those files, so waiting for an event
                    # would capture nothing at all.
                    for directory in watched:
                        for path in sorted(directory.rglob("*")):
                            if path.is_file() and mine(path):
                                await _queue_body(path, lines, bodies)
                async for paths in changed:
                    for path in sorted(p for p in paths if mine(p)):
                        await _queue_body(path, lines, bodies)
            return
        async for followed in follow_tree(
            *watched,
            match=mine,
            replay=replay,
            resume=resume,
            on_armed=None if armed is None else armed.set,
            until=until,
        ):
            await lines.put(
                _Captured(
                    path=followed.path,
                    raw=followed.text.encode(),
                    restart=followed.restart,
                ),
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


# ONE rewrite is not one wake: ``write_text`` truncates and then writes, so the kernel
# queues two events for it, and a read landing between them wakes the follower twice.
# The second wake re-reads a body already queued, and the adapter turns it into the same
# turn again -- a duplicated last message in the transcript. The line path needs no
# equivalent: its cursor advances past what it read, so a second wake with nothing
# appended yields nothing.
#
# Identity is the body's digest rather than ``(size, mtime)``: gemini rewrites one JSON
# object in place, so a same-length edit within a timestamp's granularity leaves both
# unchanged while the turn is new. Only the digest is kept -- a session file grows to
# megabytes, and every one of them would otherwise be held for the life of the run.
async def _queue_body(
    path: Path,
    lines: asyncio.Queue[_Captured],
    bodies: dict[Path, str],
) -> None:
    """Queue a whole-file session's body, skipping empty, unreadable, or unchanged."""
    body = _read_bytes(path)
    if not body.strip():
        return
    digest = hashlib.sha256(body).hexdigest()
    if bodies.get(path) == digest:
        return
    await lines.put(_Captured(path=path, raw=body, restart=True))
    bodies[path] = digest


# A read racing the writer's rewrite is routine, not an error: the next change wakes
# another read.
def _read_bytes(path: Path) -> bytes:
    """Return the whole file, or empty when it cannot be read."""
    try:
        return path.read_bytes()
    except OSError:
        return b""


async def _drain_lines(
    lines: asyncio.Queue[_Captured],
    *,
    emit: Callable[[_Captured], None],
) -> None:
    """Emit every line the follower has delivered so far."""
    while True:
        try:
            captured = lines.get_nowait()
        except asyncio.QueueEmpty:
            return
        emit(captured)


# No line-level dedup. A record's stored key is its POSITION in the file's normalized
# stream, so re-feeding a file the CLI rewrote lands each record back where it already
# was. Identity by record id (what this did before) could only ever suppress a
# duplicate, never correct one that CHANGED -- and a compaction is a replacement, so a
# re-derived record legitimately differs from the stored one.
#
# ``prefix`` is told of every line, blank ones too, since each one moves the file's
# byte position on.
def _emit_line(
    adapter: Adapter,
    sink: Sink,
    stats: _Stats,
    config: RunConfig,
    captured: _Captured,
    *,
    worktree_diff: WorktreeDiff | None = None,
    prefix: _Prefix | None = None,
    inbound: InboundState | None = None,
) -> None:
    """Emit one captured line."""
    # Backfill the CLI's own session id (the file names it) so a fresh run
    # becomes resumable on its next ``--resume``. The sink keeps the first
    # non-empty id and ignores it on a local-file run.
    cli_session_id = adapter.session_id_from_path(captured.path)
    if cli_session_id is not None:
        sink.set_cli_session_id(cli_session_id)
    upload = prefix is None or not prefix.covers(captured)
    if not captured.raw.strip():
        return
    _process_chunk(
        captured,
        adapter,
        sink,
        stats,
        config,
        worktree_diff=worktree_diff,
        upload=upload,
        inbound=inbound,
    )


# Guarded like the file path: this runs on the drain's own loop, where an escaping error
# would end capture for the rest of the run.
async def _drain_queues(
    adapter: Adapter,
    sink: Sink,
    stats: _Stats,
    config: RunConfig,
    *,
    slash_queue: deque[tuple[SlashCommand, datetime]],
    stream_queue: deque[bytes] | None,
    stream_part: Path,
    run_records: deque[Event] | None = None,
) -> None:
    """Emit whatever the relay's and the prompt bridge's threads have queued."""
    try:
        _emit_slash_commands(adapter, sink, stats, config, slash_queue)
        _emit_stream_lines(adapter, sink, stats, stream_queue, part=stream_part)
        _emit_run_records(sink, stats, run_records)
    except Exception:
        _logger.warning("trax run: queue drain failed; continuing", exc_info=True)


def _emit_run_records(
    sink: Sink,
    stats: _Stats,
    run_records: deque[Event] | None,
) -> None:
    """Drain the records the run wrote itself into the sink, oldest first."""
    if run_records is None:
        return
    while run_records:
        event = run_records.popleft()
        sink.emit_run_record(event)
        stats.record(event.kind)


# A flush hiccup must cost one pass, not the whole run's capture.
def _flush(sink: Sink) -> None:
    """Flush the sink, surviving a transient failure (R-57)."""
    try:
        sink.flush()
    except Exception:
        _logger.warning("trax run: sink flush failed; continuing", exc_info=True)


# Guarded like capture: a git or sink failure here must cost one refresh, not the
# run. Runs on the drain thread (the single sink writer); the git it may run is
# bounded, and at idle there is no line waiting for it to block.
def _refresh_diff(
    worktree_diff: WorktreeDiff | None,
    sink: Sink,
    *,
    force: bool,
) -> None:
    """Refresh the worktree diff when one is tracked, surviving a failure."""
    if worktree_diff is None:
        return
    try:
        worktree_diff.refresh(
            sink.emit_worktree_diff,
            now=time.monotonic(),
            force=force,
        )
    except Exception:
        _logger.warning(
            "trax run: worktree diff refresh failed; continuing",
            exc_info=True,
        )


# NOT as records: a command is handled inside the TUI and never written to the session
# log, so it has no position in any part and cannot be re-derived. It goes down its own
# path and the server numbers it.
#
# Runs on the drain thread (the single sink writer). ``popleft`` on a ``deque`` is
# atomic, so it needs no lock against the relay thread's ``append``; draining until
# ``IndexError`` empties whatever the human typed since the last tick. Each command
# carries the submit-time clock the detector stamped, since a typed command has no CLI-
# recorded timestamp.
def _emit_slash_commands(
    adapter: Adapter,
    sink: Sink,
    stats: _Stats,
    config: RunConfig,
    slash_queue: deque[tuple[SlashCommand, datetime]],
) -> None:
    """Drain queued slash-commands into the sink."""
    while True:
        try:
            command, submitted_at = slash_queue.popleft()
        except IndexError:
            break
        sink.emit_slash_command(command, submitted_at)
        stats.record("SlashCommand")
        if command.command in adapter.session_switch_commands:
            stats.switches.append(command.command)
        if config.verbose:
            sys.stderr.write(f"[trax run] {adapter.name}: SlashCommand\n")


# Runs on the drain thread. The relay's IO path only appends to the deque (atomic), so
# framing never blocks on a slow ``sink.feed``.
#
# Every line goes to one part per run. A resumed scrape starts its own positions
# at zero, so reusing a basename would overwrite or hide the preceding run's lines.
def _emit_stream_lines(
    adapter: Adapter,
    sink: Sink,
    stats: _Stats,
    stream_queue: deque[bytes] | None,
    *,
    part: Path,
) -> None:
    """Drain stream-captured lines into the sink (single sink writer)."""
    if stream_queue is None:
        return
    while True:
        try:
            raw = stream_queue.popleft()
        except IndexError:
            break
        for kind in sink.feed(adapter, part, raw):
            stats.record(kind)


# Both halves run untrusted work in a daemon drain thread, and an escaping raise there
# kills the thread and silently ends capture for the rest of the run. Normalizing runs
# adapter code over CLI bytes (a malformed line failing a record invariant); the sink
# can fail at the bottom of its own fallback chain -- ``ResilientSink`` degrades to a
# local ``FileSink``, whose write raises on a full disk with nothing left to catch it.
# One guard covers both, so a failure costs one chunk rather than the run.
def _process_chunk(
    captured: _Captured,
    adapter: Adapter,
    sink: Sink,
    stats: _Stats,
    config: RunConfig,
    *,
    worktree_diff: WorktreeDiff | None = None,
    upload: bool = True,
    inbound: InboundState | None = None,
) -> None:
    """Feed one chunk to the sink; never let a bug abort capture."""
    try:
        emitted = sink.feed(
            adapter,
            captured.path,
            captured.raw,
            restart=captured.restart,
            on_record=None if worktree_diff is None else worktree_diff.note,
            upload=upload,
            annotate=None if inbound is None else inbound.stamp,
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
    if emitted and inbound is not None:
        inbound.record_seen()
    for kind in emitted:
        stats.record(kind)
        if config.verbose:
            sys.stderr.write(f"[trax run] {adapter.name}: {kind}\n")


# The same watch the live drain uses, with two differences that are the point of a dry
# run: nothing is excluded (there is no ``baseline`` -- the existing files ARE the
# subject), and the follower replays what they already hold rather than only what they
# gain.
#
# ``stop`` ends the loop (tests inject it); in a real run it is ``None`` and a
# ``KeyboardInterrupt`` (Ctrl-C) ends it instead.
def _dry_run_drain(
    config: RunConfig,
    adapter: Adapter,
    sink: Sink,
    stats: _Stats,
    *,
    stop: threading.Event | None = None,
) -> int:
    """Return the ``--dry-run`` loop: replay existing session files until Ctrl-C."""
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
            ),
        )
    return 0


class _Flags(Protocol):
    """Parsed ``trax run`` flags."""

    cli: str
    out: Path | None
    verbose: bool
    dry_run: bool
    sync: bool
    actor: str | None
    rooms: list[str] | None
    private: bool
    diff: bool
    detach: bool
    name: str | None
    attach: uuid.UUID | None
