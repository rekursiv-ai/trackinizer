"""Tests for the session runner: which files a run captures, and how it runs.

The runner shares one session root with concurrent sessions, so it must
drain only the files the wrapped run creates or resumes -- never re-emit
lines from sessions that already existed when the run started.
"""

from __future__ import annotations

from collections import deque
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Callable,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Final, TextIO, cast, override

import asyncio
import io
import json
import os
import pwd
import shlex
import shutil
import sys
import threading
import time
import tomllib
import uuid

from treekle import from_plain, json_loads

import pytest

from trackinizer.client.client import Client
from trackinizer.client.errors import ClientError
from trackinizer.lib.agent.sessions import claude, codex
from trackinizer.lib.agent.sessions.tail import Tail
from trackinizer.lib.agent.types.sessions import (
    AssistantMessage,
    FileEditResult,
    IncompleteRecord,
    SessionRecord,
    SystemMessage,
    UncategorizedRecord,
    UserMessage,
)
from trackinizer.lib.posix import follow
from trackinizer.lib.posix.follow import follow_tree
from trackinizer.lib.posix.host import HostSpec
from trackinizer.lib.posix.relay import ThreadedRelay
from trackinizer.lib.posix.testing import poll_fsevents
from trackinizer.trax import machine_host
from trackinizer.trax.daemon.protocol import socket_address
from trackinizer.trax.profile import LOCALHOST_FALLBACK_URL
from trackinizer.trax.run import detach, prompt_bridge, session
from trackinizer.trax.run.adapters.claude import ClaudeAdapter
from trackinizer.trax.run.adapters.codex import CodexAdapter
from trackinizer.trax.run.adapters.gemini import GeminiAdapter
from trackinizer.trax.run.adapters.iostream import IOStreamAdapter
from trackinizer.trax.run.commands import command_record
from trackinizer.trax.run.custom_types import Event
from trackinizer.trax.run.inbound import render_inbound
from trackinizer.trax.run.prompt_bridge import PromptBridge
from trackinizer.trax.run.session import (
    _CANVAS_NOTE,
    InboundState,
    RunConfig,
    _cli_argv,
    _drain_filesystem_loop,
    _emit_slash_commands,
    _existing_session_files,
    _inbound_poll_loop,
    _open_sink,
    _process_chunk,
    _routing_env,
    _session_owner,
    _Stats,
    _with_box_hook,
    _with_canvas_note,
    resume_argv,
    run,
)
from trackinizer.trax.run.sink import (
    LockedSink,
    PrivateNotHeldError,
    ResilientSink,
    Sink,
    TrackinizerSink,
)
from trackinizer.trax.run.slash import SlashCommand
from trackinizer.trax.run.trust import TrustGate
from trackinizer.trax.run.worktree_diff import Output, WorktreeDiff, run_git
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.chat_commands import Command, classify
from trackinizer.wire.wire_session_ir import (
    AppendRecordsResponse,
    PartBody,
    RecordBody,
    SlashCommandBody,
)
from trackinizer.wire.wire_sessions import (
    InboundDrainItem,
    SessionEnd,
    SessionEndResponse,
    SessionPlacement,
    SessionStart,
    SessionStartResponse,
    WorkspaceMessageContext,
)


_CWD: Final = Path(__file__).resolve().parent

if TYPE_CHECKING:
    from treekle import PlainTree

    from trackinizer.trax.run.adapters.custom_types import Adapter
    from trackinizer.types.streams import TraxRecord


@pytest.fixture(autouse=True)
def short_queue_drain_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Preserve flush ticks and rearm backoff without production-sized waits."""
    monkeypatch.setattr(session, "_QUEUE_DRAIN_SEC", 0.005)
    monkeypatch.setattr(session, "_WATCH_REARM_SEC", 0.01)


# Writes reported: a whole-file session is rewritten in place, and the drain learns of a
# rewrite only from its write event -- it holds no kqueue per file.
@pytest.fixture(autouse=True)
def prompt_fsevents(monkeypatch: pytest.MonkeyPatch) -> None:
    """Watch on a poll rather than on ``fseventsd``'s schedule; see ``poll_fsevents``."""
    poll_fsevents(monkeypatch, writes=True)


# A synced claude run reads claude's trust config before it starts. Sandboxed, claude
# trusts every folder without reading one, so no test reads the person's own config and
# no trust card opens; a test of the card says otherwise.
@pytest.fixture(autouse=True)
def trusted_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every claude run trusted in its folder, as claude decides it."""
    monkeypatch.setenv("CLAUDE_CODE_SANDBOXED", "1")


class _RecordingSink(Sink):
    """Collects emitted records for assertions.

    SUBCLASSES ``Sink`` rather than matching it structurally -- which is the
    Protocol's own rule, and load-bearing here: ``feed`` is concrete on the
    Protocol and reaches ``_readers``, so a structural stand-in gets the
    method without the state it needs.

    Positions are per-FILE, exactly as the real sinks derive them, so a test
    can assert each part numbers its records from 0.
    """

    def __init__(self) -> None:
        self.events: list[tuple[int, str, Event]] = []
        self.slash: list[tuple[SlashCommand, datetime]] = []
        self.diffs: list[list[FileEditResult]] = []
        self.closed = False
        self.flushes = 0
        self.cli_session_ids: list[str] = []
        self._next_idx: dict[Path, int] = {}

    @property
    @override
    def session_id(self) -> uuid.UUID | None:
        return None

    @override
    def open(self) -> str | None:
        return None

    @override
    def set_cli_session_id(self, cli_session_id: str) -> None:
        self.cli_session_ids.append(cli_session_id)

    @override
    def restart(self, path: Path) -> None:
        self._next_idx[path] = 0

    @override
    def pass_over(self, path: Path) -> None:
        self._next_idx[path] = self._next_idx.get(path, 0) + 1

    @override
    def emit(self, adapter_name: str, event: Event) -> None:
        idx = self._next_idx.get(event.path, 0)
        self._next_idx[event.path] = idx + 1
        self.events.append((idx, adapter_name, event))

    @override
    def emit_slash_command(self, command: SlashCommand, at: datetime) -> None:
        self.slash.append((command, at))

    @override
    def emit_worktree_diff(self, records: Sequence[FileEditResult]) -> None:
        self.diffs.append(list(records))

    @override
    def flush(self) -> None:
        self.flushes += 1

    @override
    def pending(self) -> list[tuple[Path, RecordBody]]:
        return []

    @override
    def close(self) -> None:
        self.closed = True


# Reads the record's own field rather than a projection, so a test failure names the
# turn that went missing rather than a search string.
#
# Records with no prose are SKIPPED, not asserted against: one native line legitimately
# produces several records -- a claude user line also emits the ``TurnContext`` that
# applies to it -- and the drain tests are about which turns were captured and in what
# order, not how many records a dialect spends saying so.
def _texts(sink: _RecordingSink) -> list[str]:
    """Return the prose of each captured record that carries any, in emit order."""
    out: list[str] = []
    for _idx, _name, event in sink.events:
        match event.record:
            case IncompleteRecord() as record:
                out.append(record.text)
            case UserMessage() | AssistantMessage() as record:
                out.append(record.content or "")
            case _:
                continue
    return out


# The fake dialect the file-drain tests use: it makes "which turns were captured, in
# what order" the only thing under test, with no provider grammar in the way -- so it
# states no opening ``TurnContext`` either, which a real dialect does and which would
# show up in every count here.
def _line_records(stream: TextIO) -> Iterator[SessionRecord]:
    """One record per line, carrying the line's own text."""
    for line in stream:
        yield UserMessage(content=line.rstrip("\n"))


# STREAMING, like the real dialects: the reader runs on the ``Tail``'s own thread, and a
# generator that raises is finished -- so the tail rebuilds it and the raise is handed
# back to whoever fed the poison line. That is what keeps one bad line costing one line
# rather than the rest of the file.
def _poison_records(stream: TextIO) -> Iterator[SessionRecord]:
    """One record per line; raise on the line whose text is ``boom``."""
    for line in stream:
        text = line.rstrip("\n")
        if text == "boom":
            raise ValueError("reader blew up")
        yield UserMessage(content=text)


# Mirrors gemini, which rewrites ONE JSON object in place: the runner re-feeds the whole
# body on each change and marks the chunk a restart, so each record lands back on the
# position it already held rather than the reader having to remember what it emitted.
def _document_records(stream: TextIO) -> Iterator[SessionRecord]:
    """Every message a whole document holds, re-read from its start."""
    obj = json_loads(stream.read(), dict[str, object])
    for text in from_plain(obj.get("messages"), list[str], default=[]):
        yield UserMessage(content=text)


class _FakeAdapter:
    """Treats every ``*.jsonl`` line as one ``UserMessage`` record."""

    name: str = "fake"
    cli_binary: str = "fake"
    whole_file: bool = False
    parent_session_env: frozenset[str] = frozenset[str]()
    session_switch_commands: frozenset[str] = frozenset({"clear"})

    def __init__(self, root: Path) -> None:
        self._root = root

    def session_dirs(self) -> Iterable[Path]:
        return (self._root,)

    def matches_session_file(self, path: Path) -> bool:
        return path.suffix == ".jsonl"

    def session_scope(self) -> Path | None:
        return None

    def session_id_from_path(self, path: Path) -> str | None:
        del path
        return None

    def resumed_session(self, cli_args: tuple[str, ...]) -> str | None:
        del cli_args
        return None

    def reader(self) -> Tail[TraxRecord]:
        return Tail(_line_records)


class _WholeFileAdapter(_FakeAdapter):
    """A whole-file adapter: the runner must feed it the entire file body.

    Mirrors gemini, which rewrites one JSON object in place rather than
    appending lines.
    """

    name: str = "wholefile"
    cli_binary: str = "wholefile"
    whole_file: bool = True

    @override
    def matches_session_file(self, path: Path) -> bool:
        return path.suffix == ".json"

    @override
    def reader(self) -> Tail[TraxRecord]:
        return Tail(_document_records, whole_file=True)


# Each line is the absolute path of a file the session edited, so the diff
# capture's ``note`` sees it through ``feed``'s ``on_record`` hook.
def _edit_records(stream: TextIO) -> Iterator[SessionRecord]:
    """One empty ``FileEditResult`` per line, its path the line's text."""
    for line in stream:
        yield FileEditResult(call_id="", path=line.rstrip("\n"), edits=())


class _FakeEditAdapter(_FakeAdapter):
    """Treats every ``*.jsonl`` line as one file-edit record naming a path."""

    name: str = "fakeedit"

    @override
    def reader(self) -> Tail[TraxRecord]:
        return Tail(_edit_records)


def _unused_git(argv: Sequence[str], max_bytes: int) -> Output:
    """Fail if called at all -- ``note`` resolves paths and never runs git."""
    del argv, max_bytes
    raise AssertionError("note must not run git")


def _diff_git(argv: Sequence[str], max_bytes: int) -> Output:
    """Answer a one-line diff for ``a.py`` and nothing else."""
    del max_bytes
    if "diff" in argv and "a.py" in argv:
        return Output(returncode=0, stdout=b"@@ -1 +1 @@\n-old\n+new\n", cut=False)
    return Output(returncode=1, stdout=b"", cut=False)


class _PoisonAdapter(_FakeAdapter):
    """Raises on a line whose text is ``boom``; otherwise a ``UserMessage``."""

    name: str = "poison"
    cli_binary: str = "poison"

    @override
    def reader(self) -> Tail[TraxRecord]:
        return Tail(_poison_records)


def _always_found(
    cmd: str,
    mode: int = os.F_OK | os.X_OK,
    path: str | None = None,
) -> str:
    """Return a ``shutil.which`` that resolves anything: the binary is never exec'd."""
    del mode, path
    return f"/bin/{cmd}"


def _write(path: Path, lines: int) -> None:
    path.write_text("".join(json.dumps({"n": i}) + "\n" for i in range(lines)))


def _claude_line(text: str) -> str:
    """One claude user record carrying ``text``, newline-terminated."""
    message = {
        "type": "user",
        "uuid": text,
        "message": {"role": "user", "content": text},
    }
    return json.dumps(message) + "\n"


_CODEX_META: Final = (
    '{"type":"session_meta","payload":{"session_id":"s1","cwd":"/workspace"}}\n'
)


def _codex_line(text: str) -> str:
    """One codex user message carrying ``text``, newline-terminated."""
    content = [{"type": "input_text", "text": text}]
    payload = {"type": "message", "role": "user", "content": content}
    return json.dumps({"type": "response_item", "payload": payload}) + "\n"


def _append(path: Path, text: str, argv: Sequence[str]) -> None:
    """Act as a CLI that appends ``text`` to ``path``."""
    del argv
    with path.open("a") as log:
        _ = log.write(text)


def _ignore(argv: Sequence[str]) -> None:
    """Act as a CLI that writes nothing."""
    del argv


# Delivery is the handshake between a test's writes: a second write issued once the
# first's turn has been EMITTED cannot be folded into the same wake, which no fixed
# pause can promise. Returns rather than asserts on timeout so the caller's own
# assertion names what is missing.
def _wait_for_events(sink: _RecordingSink, count: int) -> None:
    """Block until ``sink`` holds ``count`` events, or give up after 3s."""
    deadline = time.monotonic() + 3.0
    while len(sink.events) < count and time.monotonic() < deadline:
        time.sleep(0.005)


# The drain is wake-driven, so a test cannot call one scan and inspect the result: it
# starts the loop, writes, and waits for delivery. A ``write`` that handshakes on
# delivery mid-way passes the ``sink`` it will watch.
def _drain_once(
    adapter: Adapter,
    write: Callable[[], object],
    *,
    baseline: frozenset[Path] = frozenset(),
    expected: int = 1,
    sink: _RecordingSink | None = None,
) -> tuple[_Stats, _RecordingSink]:
    """Run the real drain, perform ``write``, and collect what it captured."""
    sink = sink or _RecordingSink()
    stats = _Stats()
    stop = threading.Event()
    armed = threading.Event()

    def _run() -> None:
        _drain_filesystem_loop(
            adapter,
            sink,
            stats,
            RunConfig(cli_name=adapter.name),
            stop,
            baseline=baseline,
            slash_queue=deque(),
            armed=armed,
        )

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    try:
        assert armed.wait(5.0), "session-log watch did not arm"
        write()
        _wait_for_events(sink, expected)
    finally:
        stop.set()
        worker.join(timeout=5.0)
        assert not worker.is_alive(), "session drain did not stop"
    return stats, sink


# The trailing pause is a settle window, not a spawn guess: the test proves NOTHING
# arrives after the identical rewrite, and absence has no event to wait on. Ten drain
# intervals is ample for a duplicate to have been emitted.
def _rewrite_identically(log: Path, body: str, sink: _RecordingSink) -> None:
    """Write ``body``, wait for its turn to land, then write the same bytes."""
    log.write_text(body)
    _wait_for_events(sink, 1)
    log.write_text(body)
    time.sleep(10 * session._QUEUE_DRAIN_SEC)


class _RecordingStop(threading.Event):
    """A stop event that records each timed wait's timeout and shortens it.

    A loop under test paces itself by waiting on this event, so the timeouts
    it asks for ARE its poll interval or backoff; paying them in full would
    only make the test slow.
    """

    def __init__(self) -> None:
        super().__init__()
        self.timeouts: list[float] = []

    @override
    def wait(self, timeout: float | None = None) -> bool:
        if timeout is not None:
            self.timeouts.append(timeout)
        return super().wait(None if timeout is None else min(timeout, 0.001))


class TestSessionScoping:
    """Only this run's session files are captured."""

    def test_baseline_files_are_skipped(self, tmp_path: Path) -> None:
        old = tmp_path / "old.jsonl"
        _write(old, lines=5)
        adapter = _FakeAdapter(tmp_path)
        baseline = _existing_session_files(adapter, RunConfig(cli_name="fake"))
        assert old in baseline

        stats, sink = _drain_once(
            adapter,
            lambda: _write(tmp_path / "new.jsonl", lines=3),
            baseline=baseline,
            expected=3,
        )

        # Only the 3 lines of the new file; none of the 5 pre-existing.
        assert stats.counts == {"UserMessage": 3}
        assert [seq for seq, _, _ in sink.events] == [0, 1, 2]

    def test_no_new_file_emits_nothing(self, tmp_path: Path) -> None:
        old = tmp_path / "old.jsonl"
        _write(old, lines=4)
        adapter = _FakeAdapter(tmp_path)
        baseline = _existing_session_files(adapter, RunConfig(cli_name="fake"))

        stats, sink = _drain_once(
            adapter,
            lambda: None,
            baseline=baseline,
            expected=0,
        )

        assert stats.counts == {}
        assert sink.events == []

    def test_a_concurrent_runs_file_is_not_drained(self, tmp_path: Path) -> None:
        """Another run's session file must not be swept in (#283).

        The old drain needed an mtime floor for this, because it rescanned the
        whole tree every tick and could pick up a file its baseline snapshot
        had raced past. A watch armed before the spawn cannot: it reports only
        what happens after it, and another run's file is not written by this
        one.
        """
        adapter = _FakeAdapter(tmp_path)
        others = tmp_path / "run_a.jsonl"
        _write(others, lines=5)
        past = time.time() - 60
        os.utime(others, (past, past))

        stats, sink = _drain_once(
            adapter,
            lambda: None,
            expected=0,
        )

        assert sink.events == []
        assert stats.counts == {}

    def test_a_concurrent_run_starting_later_is_not_drained(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A sibling run's file, created AFTER the watch, must not be captured.

        ``baseline`` only excludes what already existed, so ownership rests on
        a timing accident: nothing distinguishes this run's new file from a
        concurrent run's. Two agents in different workspaces then write each
        other's turns into both transcripts. Claude names its project
        directory after the cwd, so the run's own workspace is what scopes it.
        """
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        mine = tmp_path / "projects" / "-mine"
        theirs = tmp_path / "projects" / "-theirs"
        mine.mkdir(parents=True)
        theirs.mkdir()

        def write() -> None:
            _ = (theirs / "other.jsonl").write_text(_claude_line("theirs"))
            _ = (mine / "own.jsonl").write_text(_claude_line("mine"))

        adapter = ClaudeAdapter()
        monkeypatch.setattr(adapter, "session_scope", lambda: mine)
        _stats, sink = _drain_once(adapter, write)

        texts = _texts(sink)
        assert texts == ["mine"], f"a concurrent run's file was swept in: {texts}"

    def test_simultaneous_claude_runs_in_one_cwd_stay_separate(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two wrappers armed before either file exists capture their own ID."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(shutil, "which", _always_found)
        barrier = threading.Barrier(2)
        sinks = {name: _RecordingSink() for name in ("A", "B")}
        errors: list[Exception] = []
        argv_seen: list[Sequence[str]] = []

        class _Relay:
            def __init__(self, argv: Sequence[str], **kwargs: object) -> None:
                del kwargs
                self.argv = argv

            def run(self) -> int:
                name = threading.current_thread().name
                argv_seen.append(self.argv)
                session_id = (
                    self.argv[self.argv.index("--session-id") + 1]
                    if "--session-id" in self.argv
                    else f"00000000-0000-4000-8000-00000000000{int(name == 'B') + 1}"
                )
                barrier.wait(timeout=3.0)
                scope = ClaudeAdapter().session_scope()
                assert scope is not None
                scope.mkdir(parents=True, exist_ok=True)
                (scope / f"{session_id}.jsonl").write_text(_claude_line(name))
                _wait_for_events(sinks[name], 1)
                return 0

        monkeypatch.setattr(session, "ThreadedRelay", _Relay)

        def drive(name: str) -> None:
            try:
                _ = session._spawn_and_drain(
                    RunConfig(cli_name="claude", sync=False, quiesce_seconds=0.02),
                    ClaudeAdapter(),
                    sinks[name],
                    _Stats(),
                )
            except (
                AssertionError,
                OSError,
                RuntimeError,
            ) as error:
                errors.append(error)

        workers = [
            threading.Thread(target=drive, args=(name,), name=name) for name in sinks
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5.0)
            assert not worker.is_alive()
        assert not errors
        assert {name: _texts(sink) for name, sink in sinks.items()} == {
            "A": ["A"],
            "B": ["B"],
        }
        assert all("--session-id" in argv for argv in argv_seen)

    def test_simultaneous_codex_runs_in_one_root_stay_separate(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A rollout belongs to the child holding its file, not every watcher."""
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        monkeypatch.setattr(shutil, "which", _always_found)
        barrier = threading.Barrier(2)
        sinks = {name: _RecordingSink() for name in ("A", "B")}
        errors: list[Exception] = []
        argv_seen: list[Sequence[str]] = []
        ids = {
            "A": "00000000-0000-7000-8000-000000000001",
            "B": "00000000-0000-7000-8000-000000000002",
        }
        pids = {"A": 101, "B": 102}

        def line_reader(self: CodexAdapter) -> Tail[TraxRecord]:
            del self
            return Tail(_line_records)

        def held_by_child(pid: int, path: Path) -> bool:
            return any(
                pid == pids[name] and path.stem.endswith(ids[name]) for name in ids
            )

        class _Relay:
            def __init__(self, argv: Sequence[str], **kwargs: object) -> None:
                self.argv = argv
                self.on_started = cast(
                    Callable[[int], None] | None,
                    kwargs.get("on_started"),
                )

            def run(self) -> int:
                name = threading.current_thread().name
                argv_seen.append(self.argv)
                if self.on_started is not None:
                    self.on_started(pids[name])
                barrier.wait(timeout=3.0)
                day = tmp_path / "sessions" / "2026" / "09" / "29"
                day.mkdir(parents=True, exist_ok=True)
                (day / f"rollout-2026-09-29T20-00-00-{ids[name]}.jsonl").write_text(
                    name + "\n",
                )
                _wait_for_events(sinks[name], 1)
                return 0

        monkeypatch.setattr(CodexAdapter, "reader", line_reader)
        monkeypatch.setattr(session, "ThreadedRelay", _Relay)
        monkeypatch.setattr(
            session,
            "_path_held_by_child",
            held_by_child,
            raising=False,
        )

        def drive(name: str) -> None:
            try:
                _ = session._spawn_and_drain(
                    RunConfig(cli_name="codex", sync=False, quiesce_seconds=0.02),
                    CodexAdapter(),
                    sinks[name],
                    _Stats(),
                )
            except (
                AssertionError,
                OSError,
                RuntimeError,
            ) as error:
                errors.append(error)

        workers = [
            threading.Thread(target=drive, args=(name,), name=name) for name in sinks
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5.0)
            assert not worker.is_alive()
        assert not errors
        assert {name: _texts(sink) for name, sink in sinks.items()} == {
            "A": ["A"],
            "B": ["B"],
        }
        assert all("--no-daemon" in argv for argv in argv_seen)

    def test_codex_exit_banner_recovers_a_late_file_notification(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The final scan still owns a rollout after its writer closed it."""
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        monkeypatch.setattr(shutil, "which", _always_found)
        session_id = "00000000-0000-7000-8000-000000000003"
        sink = _RecordingSink()

        def line_reader(self: CodexAdapter) -> Tail[TraxRecord]:
            del self
            return Tail(_line_records)

        class _Relay:
            def __init__(self, argv: Sequence[str], **kwargs: object) -> None:
                del argv
                self.on_output = cast(
                    Callable[[bytes], None] | None,
                    kwargs.get("on_output"),
                )

            def run(self) -> int:
                day = tmp_path / "sessions" / "2026" / "09" / "29"
                day.mkdir(parents=True, exist_ok=True)
                (day / f"rollout-2026-09-29T20-00-00-{session_id}.jsonl").write_text(
                    "final\n",
                )
                if self.on_output is not None:
                    self.on_output(b"Session ID: 00000000-0000-")
                    self.on_output(b"7000-8000-000000000003\r\n")
                return 0

        def closed_file(pid: int, path: Path) -> bool:
            del pid, path
            return False

        monkeypatch.setattr(CodexAdapter, "reader", line_reader)
        monkeypatch.setattr(session, "ThreadedRelay", _Relay)
        monkeypatch.setattr(session, "_path_held_by_child", closed_file)

        assert (
            session._spawn_and_drain(
                RunConfig(cli_name="codex", sync=False, quiesce_seconds=0.0),
                CodexAdapter(),
                sink,
                _Stats(),
            )
            == 0
        )
        assert _texts(sink) == ["final"]

    def test_codex_output_only_claims_the_final_banner(self) -> None:
        """A session ID printed during a turn cannot redirect file ownership."""
        owner = session._SessionOwner()
        owner.output(b"Session ID: 00000000-0000-7000-8000-000000000002\r\n")
        assert owner.session_id is None
        owner.output(b"Session ID: 00000000-0000-7000-8000-000000000001\r\n")
        owner.finish_output()
        assert owner.session_id == "00000000-0000-7000-8000-000000000001"

    def test_this_runs_own_file_is_drained(self, tmp_path: Path) -> None:
        """A file created after the watch is armed IS this run's."""
        adapter = _FakeAdapter(tmp_path)
        stats, _sink = _drain_once(
            adapter,
            lambda: _write(tmp_path / "mine.jsonl", lines=3),
            expected=3,
        )
        assert stats.counts == {"UserMessage": 3}


_OLD: Final = "0b3c2f7e-1d4a-4f5e-9a1b-2c3d4e5f6a7b"
_ELSEWHERE: Final = "44444444-4444-4444-8444-444444444444"
_ROLLOUT_ID: Final = "01a14f1f-cca1-7071-803c-ab02f8457212"


class _OpenWatchingSink(_RecordingSink):
    """Also notes which CLI session ids the RUNNER set before the session opened.

    Only the runner's own thread counts: the drain may read a resumed file and
    set the same id first, which says nothing about what the runner knew.
    """

    def __init__(self) -> None:
        super().__init__()
        self._runner = threading.current_thread()
        self._runner_ids: list[str] = []
        self.known_at_open: list[str] | None = None

    @override
    def set_cli_session_id(self, cli_session_id: str) -> None:
        super().set_cli_session_id(cli_session_id)
        if threading.current_thread() is self._runner:
            self._runner_ids.append(cli_session_id)

    @override
    def open(self) -> str | None:
        self.known_at_open = list(self._runner_ids)
        return None


@pytest.fixture
def claude_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return the cwd's claude project directory, holding one earlier session."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    scope = ClaudeAdapter().session_scope()
    assert scope is not None
    scope.mkdir(parents=True)
    _ = (scope / f"{_OLD}.jsonl").write_text(_claude_line("before"))
    return scope


@pytest.fixture
def codex_rollout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return an earlier codex rollout under a throwaway ``$CODEX_HOME``."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    day = tmp_path / "sessions" / "2026" / "10" / "18"
    day.mkdir(parents=True)
    rollout = day / f"rollout-2026-10-18T15-07-26-{_ROLLOUT_ID}.jsonl"
    _ = rollout.write_text(_CODEX_META + _codex_line("before"))
    return rollout


def _keep_main(
    monkeypatch: pytest.MonkeyPatch,
    argv: Sequence[str],
    *,
    resume_path: Path | None = None,
    cli_session_id: str | None = None,
) -> RunConfig:
    """Return the config ``trax run argv`` would run, running nothing."""
    ran: list[RunConfig] = []
    monkeypatch.setattr(session, "run", partial(_keep_run, ran))
    assert (
        session.main(argv, resume_path=resume_path, cli_session_id=cli_session_id) == 0
    )
    (config,) = ran
    return config


class TestAResumeTypedAfterTheDashes:
    """``trax run <cli> -- <resume>`` follows the transcript the resume names.

    The transcript existed before the CLI started, so the pre-spawn snapshot
    holds it as an earlier run's and the run stored nothing. Named as
    ``resume_path`` it gets what ``trax agentsession <seq> run`` gives the
    file it materializes: followed from its start, and its id re-attaches the
    AgentSession that captured it.
    """

    @pytest.mark.parametrize(
        "spelling",
        [
            ("--resume", _OLD),
            (f"--resume={_OLD}",),
            ("-r", _OLD),
            (f"-r{_OLD}",),
            ("-pr", _OLD),
        ],
        ids=["split", "equals", "short", "attached", "cluster"],
    )
    def test_a_claude_resume_by_id_follows_its_transcript(
        self,
        claude_scope: Path,
        monkeypatch: pytest.MonkeyPatch,
        spelling: tuple[str, ...],
    ) -> None:
        config = _keep_main(monkeypatch, ["claude", "--no-sync", "--", *spelling])

        assert config.resume_path == claude_scope / f"{_OLD}.jsonl"
        assert config.cli_session_id == _OLD
        assert config.cli_args == spelling

    def test_a_session_filed_under_another_project_is_followed_there(
        self,
        claude_scope: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Measured: claude appends to the OTHER project's file, not this cwd's.

        A worktree resuming a session its main checkout started is the
        everyday case.
        """
        elsewhere = claude_scope.parent / "-elsewhere" / f"{_ELSEWHERE}.jsonl"
        elsewhere.parent.mkdir()
        _ = elsewhere.write_text(_claude_line("before"))

        config = _keep_main(
            monkeypatch,
            ["claude", "--no-sync", "--", "--resume", _ELSEWHERE],
        )

        assert config.resume_path == elsewhere
        assert config.cli_session_id == _ELSEWHERE

    def test_a_codex_resume_by_id_follows_its_rollout(
        self,
        codex_rollout: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config = _keep_main(
            monkeypatch,
            ["codex", "--no-sync", "--", "resume", _ROLLOUT_ID],
        )

        assert config.resume_path == codex_rollout
        assert config.cli_session_id == _ROLLOUT_ID

    @pytest.mark.parametrize(
        ("cli", "spelling"),
        [
            ("claude", ("-c",)),
            ("claude", ("-pc",)),
            ("claude", ("--resume",)),
            ("claude", ("--resume", "fix bug")),
            ("claude", (f"-r={_OLD}",)),
            ("claude", ("--resume", _OLD.upper())),
            ("claude", ("--resume", _ELSEWHERE)),
            ("codex", ("resume", "--last")),
            ("codex", ("resume", _ELSEWHERE)),
        ],
        ids=[
            "continue",
            "cluster-continue",
            "picker",
            "search",
            "short-equals",
            "uppercase",
            "no-such-session",
            "codex-last",
            "codex-no-such-session",
        ],
    )
    @pytest.mark.usefixtures("claude_scope", "codex_rollout")
    def test_a_resume_the_run_cannot_name_runs_uncaptured_with_a_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        cli: str,
        spelling: tuple[str, ...],
    ) -> None:
        """The CLI still starts; the warning names the route that captures."""
        config = _keep_main(monkeypatch, [cli, "--no-sync", "--", *spelling])

        assert config.resume_path is None
        assert config.cli_session_id is None
        assert config.cli_args == spelling
        assert f"trax agentsession <seq> run {cli}" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("cli", "spelling"),
        [
            ("claude", ()),
            ("claude", ("--model", "haiku")),
            ("claude", ("--resume", _OLD, "--fork-session")),
            ("codex", ("fork", _ROLLOUT_ID)),
        ],
        ids=["fresh", "options", "fork", "codex-fork"],
    )
    @pytest.mark.usefixtures("claude_scope", "codex_rollout")
    def test_a_new_session_follows_nothing_and_warns_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        cli: str,
        spelling: tuple[str, ...],
    ) -> None:
        """A fork copies an old transcript into a NEW one, which it captures."""
        config = _keep_main(monkeypatch, [cli, "--no-sync", "--", *spelling])

        assert config.resume_path is None
        assert config.cli_session_id is None
        assert capsys.readouterr().err == ""

    def test_a_materialized_resume_keeps_its_transcript(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``trax agentsession <seq> run`` names the file; the run trusts it."""
        materialized = tmp_path / f"{_OLD}.jsonl"

        config = _keep_main(
            monkeypatch,
            ["claude", "--no-sync"],
            resume_path=materialized,
            cli_session_id=_OLD,
        )

        assert config.resume_path == materialized
        assert config.cli_session_id == _OLD
        assert config.cli_args == ("--resume", _OLD)

    def test_a_forked_materialized_resume_starts_a_new_session(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The fork writes a new file, so the materialized one is not followed.

        Kept, it pinned the run to the old id: the fork's turns were dropped
        and the old AgentSession re-attached to receive nothing new.
        """
        config = _keep_main(
            monkeypatch,
            ["claude", "--no-sync", "--", "--fork-session"],
            resume_path=tmp_path / f"{_OLD}.jsonl",
            cli_session_id=_OLD,
        )

        assert config.resume_path is None
        assert config.cli_session_id is None
        assert config.cli_args == ("--resume", _OLD, "--fork-session")

    @pytest.mark.usefixtures("claude_scope")
    def test_a_detached_run_warns_in_its_host(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The host warns before it accepts, so its launcher repeats the line."""
        ran: list[RunConfig] = []
        hosted: list[Callable[[HostSpec | None], int]] = []
        monkeypatch.setattr(session, "run", partial(_keep_run, ran))
        monkeypatch.setattr(detach, "launch", partial(_keep_host, hosted))

        assert session.main(["claude", "--detach", "--", "-c"]) == 0
        assert capsys.readouterr().err == ""

        [start] = hosted
        assert start(None) == 0
        assert "trax agentsession <seq> run claude" in capsys.readouterr().err

    @pytest.mark.usefixtures("claude_scope")
    def test_a_dry_run_resolves_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A dry run spawns no CLI, so there is no resume to warn about."""
        config = _keep_main(monkeypatch, ["claude", "--dry-run", "--", "-c"])

        assert config.resume_path is None
        assert capsys.readouterr().err == ""


class TestMainDispatch:
    """``main`` hands host verbs to ``detach`` and refuses contradictory flags."""

    def test_a_host_verb_goes_to_detach(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(detach, "main", len)
        assert session.main(["ls", "x"]) == 2

    @pytest.mark.parametrize(
        "argv",
        [["claude", "--name", "x"], ["claude", "--detach", "--dry-run"]],
        ids=["name-without-detach", "detached-dry-run"],
    )
    def test_contradictory_flags_are_refused(self, argv: list[str]) -> None:
        with pytest.raises(SystemExit):
            _ = session.main(argv)


class TestARunSaysWhereItIs:
    """Each drain names the run's detached host, machine and folder, for Chat."""

    @pytest.mark.parametrize(
        ("argv", "run_name"),
        [
            (["claude", "--detach", "--name", "chat-1a2b3c4d"], "chat-1a2b3c4d"),
            (["claude", "--detach", "--as", "scout"], "scout"),
            (["codex", "--detach"], "codex"),
            (["claude", "--as", "scout"], None),
        ],
    )
    def test_a_run_is_named_by_the_host_trax_run_attach_takes(
        self,
        monkeypatch: pytest.MonkeyPatch,
        argv: list[str],
        run_name: str | None,
    ) -> None:
        ran: list[RunConfig] = []
        hosts: list[str] = []
        monkeypatch.setattr(session, "run", partial(_keep_run, ran))
        monkeypatch.setattr(detach, "launch", partial(_start_host, hosts))

        assert session.main(argv) == 0

        [config] = ran
        assert config.run_name == run_name
        assert hosts == ([] if run_name is None else [run_name])

    def test_a_detached_runs_poller_is_handed_its_host(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        wiring, _ = _wire(
            monkeypatch,
            tmp_path,
            RunConfig(
                cli_name="claude",
                client=cast(Client, object()),
                run_name="chat-1a2b3c4d",
            ),
            ClaudeAdapter(),
        )

        *_, options = wiring.inbound
        assert cast(dict[str, object], options)["run_name"] == "chat-1a2b3c4d"

    def test_each_drain_says_where_the_run_is(self) -> None:
        stop = threading.Event()
        client = _WhereClient(stop)

        _inbound_poll_loop(
            cast(Client, client),
            cast(Sink, _SessionSink()),
            cast(ThreadedRelay, _RecordingRelay()),
            stop,
            run_name="chat-1a2b3c4d",
            machine="gx10",
            cwd="~/work",
        )

        assert client.said == [
            {"run": "chat-1a2b3c4d", "machine": "gx10", "cwd": "~/work"},
        ]

    @pytest.mark.parametrize(
        ("hostname", "machine"),
        [("GX10.lan", "gx10"), ("init", None), ("...", None)],
    )
    def test_a_hostname_names_the_machine_only_as_connect_would(
        self,
        monkeypatch: pytest.MonkeyPatch,
        hostname: str,
        machine: str | None,
    ) -> None:
        monkeypatch.setattr("socket.gethostname", lambda: hostname)

        assert session._machine_name() == machine

    @pytest.mark.parametrize(
        ("cwd", "shown"),
        [
            ("/home/ada", "~"),
            ("/home/ada/work/web", "~/work/web"),
            ("/home/adam", "/home/adam"),
            ("/srv/work", "/srv/work"),
            ("/home/ada/new\nline", None),
            ("/home/ada/\x9bcsi", None),
            # A name in no UTF-8, as the filesystem hands it (``os.fsdecode``).
            ("/home/ada/\udce9t\udce9", None),
            ("/srv/" + "d" * 1_019, "/srv/" + "d" * 1_019),
            ("/srv/" + "d" * 1_020, None),
        ],
    )
    def test_a_folder_is_shown_from_home_and_left_unsaid_when_refused(
        self,
        monkeypatch: pytest.MonkeyPatch,
        cwd: str,
        shown: str | None,
    ) -> None:
        _at_home(monkeypatch, Path("/home/ada"))

        assert session._shown_cwd(Path(cwd)) == shown

    def test_an_account_with_no_home_shows_its_folder_whole(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(pwd, "getpwuid", _no_account)

        assert session._shown_cwd(Path("/home/ada/loop")) == "/home/ada/loop"


class _WhereClient:
    """Records what each drain says of the run, and stops the loop after one."""

    def __init__(self, stop: threading.Event) -> None:
        self._stop = stop
        self.said: list[dict[str, str | None]] = []

    def drain_inbound(
        self,
        session_id: uuid.UUID,
        *,
        wait_sec: float = 0.0,
        **where: str | None,
    ) -> list[InboundDrainItem]:
        del session_id, wait_sec
        self.said.append(where)
        self._stop.set()
        return []


def _start_host(
    hosts: list[str],
    name: str,
    *,
    command: str,
    start: Callable[[HostSpec | None], int],
) -> int:
    """Start what a detached host named ``name`` would run, in this process."""
    del command
    hosts.append(name)
    return start(None)


def _at_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Make ``home`` the account's home directory, as the password database says."""
    monkeypatch.setattr(pwd, "getpwuid", partial(_account_at, home))


def _account_at(home: Path, uid: int) -> pwd.struct_passwd:
    """Return an account entry for ``uid`` whose home directory is ``home``."""
    return pwd.struct_passwd(("ada", "x", uid, uid, "", str(home), "/bin/sh"))


def _no_account(uid: int) -> pwd.struct_passwd:
    """Stand in for a uid the password database has no entry for."""
    raise KeyError(f"getpwuid(): uid not found: {uid}")


class TestAResumedTranscriptIsCaptured:
    """A run handed a ``resume_path`` captures that transcript, from its start.

    Re-reading what it already holds is safe: a record's key is its position
    in the file, so the stored turns land back on their own keys, and the
    server re-attaches the AgentSession stamped with the id instead of opening
    a second one.
    """

    @classmethod
    def _spawn(
        cls,
        monkeypatch: pytest.MonkeyPatch,
        config: RunConfig,
        act: Callable[[Sequence[str]], None],
    ) -> tuple[_OpenWatchingSink, list[str]]:
        """Run the real spawn path with a relay that calls ``act(argv)``."""
        sink = _OpenWatchingSink()
        argv_seen: list[Sequence[str]] = []

        class _Relay:
            def __init__(self, argv: Sequence[str], **kwargs: object) -> None:
                del kwargs
                self.argv = argv

            def run(self) -> int:
                argv_seen.append(self.argv)
                act(self.argv)
                return 0

        monkeypatch.setattr(session, "ThreadedRelay", _Relay)
        monkeypatch.setattr(shutil, "which", _always_found)
        rc = session._spawn_and_drain(
            replace(config, quiesce_seconds=0.0),
            session._ADAPTERS[config.cli_name](),
            sink,
            _Stats(),
        )
        assert rc == 0
        (seen,) = argv_seen
        argv = list(seen)
        if argv[0] == "claude":
            # Every claude run is handed its hooks; the rest is what is asserted.
            assert argv[1] == "--settings"
            del argv[1:3]
        return sink, argv

    def test_a_claude_resume_captures_the_file_it_appends_to(
        self,
        claude_scope: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        transcript = claude_scope / f"{_OLD}.jsonl"
        config = RunConfig(
            cli_name="claude",
            cli_args=("--resume", _OLD),
            resume_path=transcript,
            cli_session_id=_OLD,
        )

        sink, argv = self._spawn(
            monkeypatch,
            config,
            partial(_append, transcript, _claude_line("after")),
        )

        assert _texts(sink) == ["before", "after"]
        assert sink.known_at_open == [_OLD]
        assert argv == ["claude", "--resume", _OLD]

    def test_a_transcript_under_another_project_is_captured(
        self,
        claude_scope: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Outside this cwd's project directory, which scopes every other file."""
        elsewhere = claude_scope.parent / "-elsewhere" / f"{_ELSEWHERE}.jsonl"
        elsewhere.parent.mkdir()
        _ = elsewhere.write_text(_claude_line("before"))
        config = RunConfig(
            cli_name="claude",
            cli_args=("--resume", _ELSEWHERE),
            resume_path=elsewhere,
            cli_session_id=_ELSEWHERE,
        )

        sink, _argv = self._spawn(
            monkeypatch,
            config,
            partial(_append, elsewhere, _claude_line("after")),
        )

        assert _texts(sink) == ["before", "after"]

    def test_a_resumed_run_does_not_adopt_a_siblings_new_file(
        self,
        claude_scope: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Another run's session minted in the same cwd stays that run's."""
        transcript = claude_scope / f"{_OLD}.jsonl"
        sibling = claude_scope / f"{_ELSEWHERE}.jsonl"
        config = RunConfig(
            cli_name="claude",
            cli_args=("--resume", _OLD),
            resume_path=transcript,
            cli_session_id=_OLD,
        )

        def append_beside_a_sibling(argv: Sequence[str]) -> None:
            _append(sibling, _claude_line("sibling"), argv)
            _append(transcript, _claude_line("after"), argv)

        sink, _argv = self._spawn(monkeypatch, config, append_beside_a_sibling)

        assert _texts(sink) == ["before", "after"]

    def test_a_forked_resume_is_pinned_to_a_fresh_id(
        self,
        claude_scope: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--fork-session`` writes a NEW file, so the run names its id.

        ``--session-id`` is the one spelling claude accepts beside a resume,
        so the fork's file is known before claude writes it.
        """

        def fork(argv: Sequence[str]) -> None:
            minted = argv[argv.index("--session-id") + 1]
            forked = claude_scope / f"{minted}.jsonl"
            _ = forked.write_text(_claude_line("before") + _claude_line("forked"))

        sink, argv = self._spawn(
            monkeypatch,
            RunConfig(cli_name="claude", cli_args=("--resume", _OLD, "--fork-session")),
            fork,
        )

        minted = argv[argv.index("--session-id") + 1]
        assert minted != _OLD
        assert _texts(sink) == ["before", "forked"]
        assert sink.known_at_open == [minted]

    @pytest.mark.usefixtures("claude_scope")
    def test_a_fresh_run_is_stamped_with_its_minted_id_at_open(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sent at open, not only at close: a killed run stays resumable.

        Backfilled at close alone, a run that never closed was reaped with no
        id, and a later ``--resume`` opened a second AgentSession that stored
        the whole history again.
        """
        sink, argv = self._spawn(monkeypatch, RunConfig(cli_name="claude"), _ignore)

        assert sink.known_at_open == [argv[argv.index("--session-id") + 1]]

    @pytest.mark.usefixtures("claude_scope")
    def test_a_resume_claude_picks_is_not_given_a_session_id(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Measured: claude refuses ``--session-id`` beside an unforked resume."""
        sink, argv = self._spawn(
            monkeypatch,
            RunConfig(cli_name="claude", cli_args=("-pc",)),
            _ignore,
        )

        assert argv == ["claude", "-pc"]
        assert sink.known_at_open == []

    def test_a_codex_resume_captures_the_rollout_it_appends_to(
        self,
        codex_rollout: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config = RunConfig(
            cli_name="codex",
            cli_args=("resume", _ROLLOUT_ID),
            resume_path=codex_rollout,
            cli_session_id=_ROLLOUT_ID,
        )

        sink, argv = self._spawn(
            monkeypatch,
            config,
            partial(_append, codex_rollout, _codex_line("after")),
        )

        assert _texts(sink) == ["before", "after"]
        assert argv == [
            "codex",
            "--no-daemon",
            "--enable",
            "goals",
            "resume",
            _ROLLOUT_ID,
        ]


class TestWorktreeDiffWiring:
    """``trax run --diff`` wires a session's edits to its worktree diff."""

    def test_a_chunk_notes_each_edited_path(self, tmp_path: Path) -> None:
        """``feed``'s ``on_record`` hook feeds the session's edits to ``note``."""
        diff = WorktreeDiff(top=tmp_path, run=_unused_git, base="HEAD")
        sink = _RecordingSink()
        session._process_chunk(
            session._Captured(
                path=tmp_path / "s.jsonl",
                raw=f"{tmp_path / 'a.py'}\n".encode(),
            ),
            _FakeEditAdapter(tmp_path),
            sink,
            _Stats(),
            RunConfig(cli_name="fakeedit"),
            worktree_diff=diff,
        )
        assert diff.touched == {"a.py"}

    def test_refresh_uploads_the_diff_and_none_is_a_no_op(self, tmp_path: Path) -> None:
        """The teardown refresh uploads the net diff; no capture means nothing sent."""
        diff = WorktreeDiff(top=tmp_path, run=_diff_git, base="HEAD")
        diff.note(FileEditResult(call_id="", path=str(tmp_path / "a.py"), edits=()))
        sink = _RecordingSink()
        session._refresh_diff(diff, sink, force=True)
        [batch] = sink.diffs
        assert [record.path for record in batch] == ["a.py"]
        session._refresh_diff(None, sink, force=True)
        assert len(sink.diffs) == 1

    def test_refresh_swallows_a_sink_failure(self, tmp_path: Path) -> None:
        """A git or sink failure costs one refresh, never the run's capture."""
        diff = WorktreeDiff(top=tmp_path, run=_diff_git, base="HEAD")
        diff.note(FileEditResult(call_id="", path=str(tmp_path / "a.py"), edits=()))

        class _Boom(_RecordingSink):
            @override
            def emit_worktree_diff(self, records: Sequence[FileEditResult]) -> None:
                raise RuntimeError("server down")

        session._refresh_diff(diff, _Boom(), force=True)  # Must not raise.


def _rewrite(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` -- a sync helper so the async caller stays clean."""
    path.write_text(text)


async def _queue_body_thrice(source: Path) -> list[session._Captured]:
    """Queue ``source``'s body, re-read it unchanged, then queue it changed."""
    queue: asyncio.Queue[session._Captured] = asyncio.Queue()
    bodies: dict[Path, str] = {}
    _rewrite(source, '{"m": 1}')
    await session._queue_body(source, queue, bodies)
    await session._queue_body(source, queue, bodies)  # Unchanged -> skipped.
    _rewrite(source, '{"m": 22}')
    await session._queue_body(source, queue, bodies)  # Changed -> queued.
    drained: list[session._Captured] = []
    while not queue.empty():
        drained.append(queue.get_nowait())
    return drained


class TestQueueBody:
    """``_queue_body`` queues a whole-file body once, by path, as a restart."""

    def test_queues_by_path_as_a_restart_and_skips_an_unchanged_reread(
        self,
        tmp_path: Path,
    ) -> None:
        """A changed body is queued (``restart=True``); an unchanged reread is not."""
        source = tmp_path / "s.json"
        captured = asyncio.run(_queue_body_thrice(source))
        assert [(c.path, c.restart) for c in captured] == [
            (source, True),
            (source, True),
        ]
        assert captured[0].raw == b'{"m": 1}'


class TestAppendedLineDrain:
    def test_rotation_discards_the_previous_files_partial_line(
        self,
        tmp_path: Path,
    ) -> None:
        """A truncated file starts a new byte stream with an empty buffer.

        A held fragment would prepend dead bytes to the first line of the
        replacement, corrupting a turn that parsed fine on disk.

        The fragment rides behind a complete line: that line's delivery is the
        proof the follower READ the fragment before the rewrite, which is the
        only way the held-buffer hazard is exercised rather than skipped.
        """
        log = tmp_path / "session.jsonl"
        adapter = _FakeAdapter(tmp_path)
        sink = _RecordingSink()

        def write() -> None:
            log.write_bytes(b"first\nstale-partial")
            _wait_for_events(sink, 1)
            log.write_bytes(b"fresh\n")

        _drain_once(adapter, write, expected=2, sink=sink)

        texts = _texts(sink)
        assert texts == ["first", "fresh"]


class TestProjectDirectoryBornMidRun:
    """A session directory the CLI mints AFTER the watch is armed.

    Claude shards sessions per project (a hashed cwd) and gemini per project
    sha; neither directory exists before the CLI's first run in that
    workspace. The watch is armed once, before the spawn, so whatever
    ``session_dirs()`` returns has to be a tree the new directory appears
    UNDER -- a watch on today's leaves cannot adopt tomorrow's sibling, and
    the run captures nothing with no error anywhere.
    """

    def test_missing_codex_session_root_is_created_before_the_watch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A hermetic first run must have a real directory to watch."""
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
        expected = tmp_path / "codex" / "sessions"

        assert not expected.exists()
        session._prepare_session_dirs(CodexAdapter())
        assert expected.is_dir()

    def test_claude_captures_a_project_directory_created_after_the_watch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        projects = tmp_path / "projects"
        # One project from an earlier run: what ``session_dirs()`` can see at
        # arming time. The run under test happens in a DIFFERENT workspace.
        (projects / "-existing-workspace").mkdir(parents=True)
        # The directory claude will mint for THIS run's cwd -- the adapter
        # names it, so the test cannot drift from the encoding.
        fresh = ClaudeAdapter().session_scope()
        assert fresh is not None

        def write() -> None:
            fresh.mkdir()
            (fresh / "abc-123.jsonl").write_text(_claude_line("captured"))

        _stats, sink = _drain_once(ClaudeAdapter(), write)

        texts = _texts(sink)
        assert texts == ["captured"], "a new project directory captured nothing"

    def test_gemini_captures_a_project_directory_created_after_the_watch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        tmp = tmp_path / ".gemini" / "tmp"
        (tmp / "existingsha" / "chats").mkdir(parents=True)
        # The project directory gemini will hash THIS run's cwd to; asking the
        # adapter keeps the test from re-deriving the hashing scheme.
        scope = GeminiAdapter().session_scope()
        assert scope is not None

        def write() -> None:
            chats = scope / "chats"
            chats.mkdir(parents=True)
            (chats / "session-1.json").write_text(
                json.dumps(
                    {
                        "sessionId": "sess-A",
                        "messages": [{"type": "user", "content": "captured"}],
                    },
                ),
            )

        _stats, sink = _drain_once(GeminiAdapter(), write)

        texts = _texts(sink)
        assert texts == ["captured"], "a new project directory captured nothing"

    def test_a_missing_claude_projects_root_is_created_before_the_watch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``_prepare_session_dirs`` must MINT the root, not skip it.

        It iterates ``session_dirs()`` to decide what to create, and claude's
        returns ``()`` when the root is absent -- so the one case the mkdir
        exists for is the one case it cannot reach. Nothing is watched, no
        follower is armed, and the run captures nothing while logging nothing.
        """
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "fresh"))
        expected = tmp_path / "fresh" / "projects"

        assert not expected.exists()
        session._prepare_session_dirs(ClaudeAdapter())
        assert expected.is_dir()

    def test_a_missing_gemini_tmp_root_is_created_before_the_watch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Gemini has the same shape: an absent root reports no directories."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        expected = tmp_path / ".gemini" / "tmp"

        assert not expected.exists()
        session._prepare_session_dirs(GeminiAdapter())
        assert expected.is_dir()

    def test_claude_captures_when_no_project_directory_exists_yet(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A first-ever run has no project dir at all when the watch arms.

        With nothing to watch the runner arms no follower and capture is
        disabled for the whole run -- the worst shape of this bug, since it
        needs no concurrency to hit.
        """
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        (tmp_path / "projects").mkdir()
        fresh = ClaudeAdapter().session_scope()
        assert fresh is not None

        def write() -> None:
            fresh.mkdir()
            (fresh / "abc-123.jsonl").write_text(_claude_line("captured"))

        _stats, sink = _drain_once(ClaudeAdapter(), write)

        texts = _texts(sink)
        assert texts == ["captured"], "a first-ever run captured nothing"


class TestWholeFileDrain:
    """A whole-file adapter (gemini) must receive the entire file, re-read."""

    def test_in_place_rewrite_emits_event(self, tmp_path: Path) -> None:
        adapter = _WholeFileAdapter(tmp_path)
        log = tmp_path / "session-x.json"

        stats, sink = _drain_once(
            adapter,
            lambda: log.write_text(json.dumps({"messages": ["hello"]})),
        )

        assert stats.counts == {"UserMessage": 1}
        texts = _texts(sink)
        assert texts == ["hello"]

    def test_same_size_rewrite_emits_event(self, tmp_path: Path) -> None:
        """A rewrite to identical byte size still emits.

        Gemini rewrites one JSON object in place; a same-length edit leaves
        ``st_size`` unchanged, so a size-only check would drop the new turn.
        """
        adapter = _WholeFileAdapter(tmp_path)
        log = tmp_path / "session-x.json"
        sink = _RecordingSink()

        def write() -> None:
            log.write_text(json.dumps({"messages": ["a"]}))
            _wait_for_events(sink, 1)
            log.write_text(json.dumps({"messages": ["b"]}))

        _drain_once(adapter, write, expected=2, sink=sink)

        texts = _texts(sink)
        assert texts == ["a", "b"]

    def test_an_unchanged_body_is_emitted_once(self, tmp_path: Path) -> None:
        """The same bytes read twice are one turn, not two.

        ``write_text`` truncates and then writes, so ONE rewrite queues two
        inotify events. They usually drain in a single read, but a read landing
        between them wakes the drain twice for the same rewrite, and the second
        wake re-reads a body already emitted -- a duplicated last turn in the
        transcript (CI flake on ``test_same_size_rewrite_emits_event``).
        """
        adapter = _WholeFileAdapter(tmp_path)
        log = tmp_path / "session-x.json"
        body = json.dumps({"messages": ["b"]})
        sink = _RecordingSink()

        _drain_once(
            adapter,
            partial(_rewrite_identically, log, body, sink),
            sink=sink,
        )

        texts = _texts(sink)
        assert texts == ["b"]

    def test_a_body_that_returns_after_a_change_is_emitted_again(
        self,
        tmp_path: Path,
    ) -> None:
        """Only the LAST body is remembered, never every body ever seen.

        A session legitimately returns to earlier content -- a retry, an undo,
        a regenerated answer that lands identically. The only thing separating
        that from a double-wake is whether something else was written in
        between, so the guard compares against the PREVIOUS body. Remembering
        every digest instead would silently swallow the third write here, and
        nothing else in this file would notice.
        """
        adapter = _WholeFileAdapter(tmp_path)
        log = tmp_path / "session-x.json"
        sink = _RecordingSink()

        def write() -> None:
            for count, text in enumerate(("a", "b", "a"), start=1):
                log.write_text(json.dumps({"messages": [text]}))
                _wait_for_events(sink, count)

        _drain_once(adapter, write, expected=3, sink=sink)

        texts = _texts(sink)
        assert texts == ["a", "b", "a"], "a returning body was swallowed"


class TestGeminiMultiFileDrain:
    """One GeminiAdapter draining several session files keeps cursors apart.

    #498: the runner reuses ONE adapter across every matching session file. A
    per-adapter message cursor carried one file's count into the next, so a
    second gemini session file's turns were dropped.
    """

    def test_two_session_files_both_fully_drained(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # Both files belong to THIS run, so both sit under its own project
        # directory -- the adapter names it from the cwd.
        scope = GeminiAdapter().session_scope()
        assert scope is not None
        chats = scope / "chats"
        chats.mkdir(parents=True)

        def _session(name: str, session_id: str, msgs: list[str]) -> None:
            (chats / name).write_text(
                json.dumps(
                    {
                        "sessionId": session_id,
                        "messages": [{"type": "user", "content": m} for m in msgs],
                    },
                ),
            )

        def write() -> None:
            _session("session-1.json", "sess-A", ["a-q", "a-r"])
            _session("session-2.json", "sess-B", ["b-q", "b-r"])

        _stats, sink = _drain_once(GeminiAdapter(), write, expected=4)

        texts = sorted(_texts(sink))
        assert texts == ["a-q", "a-r", "b-q", "b-r"]


class TestDrainSurvivesParseError:
    """A parser exception on one line must not stop capture for the rest."""

    def test_bad_line_is_skipped_and_drain_continues(self, tmp_path: Path) -> None:
        log = tmp_path / "session.jsonl"
        adapter = _PoisonAdapter(tmp_path)

        stats, sink = _drain_once(
            adapter,
            lambda: log.write_bytes(b"alpha\nboom\nomega\n"),
            expected=2,
        )

        # The poison line raised but was swallowed; the good lines emitted.
        texts = _texts(sink)
        assert texts == ["alpha", "omega"]
        assert stats.counts == {"UserMessage": 2}

    def test_parse_failure_logs_with_traceback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The swallowed parse error must log a traceback, not an opaque line.

        K6-004: the ``_process_chunk`` except logged a bare message without
        ``exc_info``, so a malformed-output loss gave no stack trace to diagnose
        which adapter path raised. The warning must carry the exception info.
        """
        adapter = _PoisonAdapter(tmp_path)
        sink = _RecordingSink()
        calls: list[tuple[str, tuple[object, ...], bool]] = []

        def record_warning(message: str, *args: object, exc_info: bool = False) -> None:
            calls.append((message, args, exc_info))

        monkeypatch.setattr(session._logger, "warning", record_warning)

        _process_chunk(
            session._Captured(path=tmp_path / "s.jsonl", raw=b"boom"),
            adapter,
            sink,
            _Stats(),
            RunConfig(cli_name="poison"),
        )

        assert calls == [("trax run: %s failed to capture a chunk", ("poison",), True)]


class TestTheWatchIsArmedBeforeTheChildSpawns:
    """The child must not be able to write before the watch exists.

    ``drain_thread.start()`` returns once the thread is SCHEDULED, and the
    relay forks the CLI on the next statement. Between those two the watch is
    not armed, and a CLI that writes its first record immediately -- codex's
    ``session_meta`` lands at launch -- loses it with nothing said. Every other
    test here waits on ``armed`` before writing; the runner must hold the spawn
    the same way.
    """

    def test_a_write_racing_the_spawn_is_still_captured(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        scope = ClaudeAdapter().session_scope()
        assert scope is not None
        scope.mkdir(parents=True)
        sink = _RecordingSink()

        class _WritingRelay:
            """A "CLI" that writes its first record the instant it starts."""

            def __init__(self, argv: Sequence[str], **kwargs: object) -> None:
                del kwargs
                self.session_id = argv[argv.index("--session-id") + 1]

            def run(self) -> int:
                _ = (scope / f"{self.session_id}.jsonl").write_text(
                    _claude_line("first"),
                )
                return 0

        monkeypatch.setattr(session, "ThreadedRelay", _WritingRelay)
        monkeypatch.setattr(shutil, "which", _always_found)

        # No quiesce: the relay exits the instant it writes, and the drain reads
        # the files to their ends at ``stop``. A window here would only hide a
        # drain that went back to waiting for FSEvents to name the file.
        rc = session._spawn_and_drain(
            RunConfig(cli_name="claude", quiesce_seconds=0.0),
            ClaudeAdapter(),
            sink,
            _Stats(),
        )

        assert rc == 0
        texts = _texts(sink)
        assert texts == ["first"], "the CLI's first record raced the watch"


# FSEvents was measured naming a new file 0.1s to 4.6s after the write under load, and
# sometimes not within 10s. A watch that arms and never reports holds that window open,
# which no timing in a test reproduces reliably.
@pytest.fixture
def silent_kernel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Arm every watch but report no write, on either platform's backend."""
    monkeypatch.setattr(follow, "_watch_fsevents", _silent_fsevents)
    monkeypatch.setattr(follow, "_inotify_events", _silent_inotify)


@asynccontextmanager
async def _silent_fsevents(
    *directories: Path,
    queue: asyncio.Queue[Path] | None = None,
) -> AsyncGenerator[AsyncIterator[set[Path]]]:
    del directories, queue
    yield _silence()


def _silent_inotify(fd: int, watches: dict[int, Path]) -> AsyncIterator[set[Path]]:
    del fd, watches
    return _silence()


async def _silence() -> AsyncIterator[set[Path]]:
    """Neither report nor end, as a watch the kernel has not reached yet."""
    _ = await asyncio.Event().wait()
    yield set()


@pytest.mark.usefixtures("silent_kernel")
class TestAWriteNothingAnnouncedIsReadAtExit:
    """A file the kernel has not named when the CLI exits is still captured.

    On macOS a NEW session file is found only when FSEvents names it. The drain
    cancelled its follower once ``stop`` was set, so a CLI that wrote and
    exited before FSEvents caught up lost its whole transcript -- the macOS
    flake in ``test_a_write_racing_the_spawn_is_still_captured``. Once the CLI
    has exited its files are final, so the drain reads them rather than
    waiting on a notification.
    """

    def test_a_line_file_is_read(self, tmp_path: Path) -> None:
        stats, _sink = _drain_once(
            _FakeAdapter(tmp_path),
            lambda: _write(tmp_path / "mine.jsonl", lines=2),
            expected=0,
        )

        assert stats.counts == {"UserMessage": 2}, "an unannounced file was lost"

    def test_a_whole_file_is_read(self, tmp_path: Path) -> None:
        log = tmp_path / "session-x.json"

        _stats, sink = _drain_once(
            _WholeFileAdapter(tmp_path),
            lambda: log.write_text(json.dumps({"messages": ["hello"]})),
            expected=0,
        )

        assert _texts(sink) == ["hello"], "an unannounced file was lost"


class TestFollowerRearmsAfterAFailure:
    """A watch that dies must be rebuilt, not silently abandoned.

    ``_follow_session_files`` logs its exception and returns, and the drain
    loop never restarts it -- so one transient failure (an inotify limit hit
    while another process churns directories) ends capture for the whole run
    while the loop keeps ticking as though it were watching.
    """

    def test_a_watch_that_raises_once_is_rearmed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        log = tmp_path / "session.jsonl"
        adapter = _FakeAdapter(tmp_path)
        real_follow = follow_tree
        attempts: list[int] = []
        rearmed = threading.Event()

        def flaky(*directories: Path, **kwargs: object) -> object:
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("inotify instance limit reached")
            kwargs["on_armed"] = rearmed.set
            match = cast(Callable[[Path], bool], kwargs.pop("match"))
            replay = cast(bool, kwargs.pop("replay"))
            resume = cast(frozenset[Path], kwargs.pop("resume"))
            until = cast(asyncio.Event, kwargs.pop("until"))
            on_armed = kwargs.pop("on_armed")
            assert on_armed is not None

            def armed() -> None:
                assert callable(on_armed)
                on_armed()

            return real_follow(
                *directories,
                match=match,
                replay=replay,
                resume=resume,
                on_armed=armed,
                until=until,
            )

        monkeypatch.setattr(session, "follow_tree", flaky)

        def write_after_rearm() -> None:
            # A failure releases the original armed event without registering a watch.
            assert rearmed.wait(5.0), "the follower was never rearmed"
            log.write_bytes(b"recovered\n")

        _stats, sink = _drain_once(
            adapter,
            write_after_rearm,
            expected=1,
        )

        assert len(attempts) >= 2, "the follower was never rearmed"
        assert _texts(sink) == ["recovered"], (
            "capture stayed dead after one transient watch failure"
        )


class _FlakyEmitSink(_RecordingSink):
    """Records events, but the first ``emit`` raises, as a full disk would."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    @override
    def emit(self, adapter_name: str, event: Event) -> None:
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("sink write failed")
        super().emit(adapter_name, event)


class TestDrainSurvivesSinkError:
    """A sink failure costs one turn, never the rest of the run's capture.

    ``_process_chunk`` guards ``parse`` but emits OUTSIDE that guard, so a
    raise from ``sink.emit`` escapes to ``asyncio.run`` and kills the daemon
    drain thread -- silently ending capture. The wrapping sinks do not close
    this: ``ResilientSink`` degrades to a local :class:`FileSink`, whose write
    raises on a full disk with nothing left to catch it.
    """

    def test_a_failing_emit_does_not_stop_capture(self, tmp_path: Path) -> None:
        log = tmp_path / "session.jsonl"
        adapter = _FakeAdapter(tmp_path)
        sink = _FlakyEmitSink()
        stats = _Stats()
        stop = threading.Event()
        armed = threading.Event()

        def _run() -> None:
            _drain_filesystem_loop(
                adapter,
                sink,
                stats,
                RunConfig(cli_name=adapter.name),
                stop,
                baseline=frozenset(),
                slash_queue=deque(),
                armed=armed,
            )

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        try:
            assert armed.wait(5.0), "session-log watch did not arm"
            log.write_bytes(b"boom\n")
            # The failed emit records no event; its attempt is the handshake
            # that the next line is a LATER write, not the same batch.
            deadline = time.monotonic() + 3.0
            while sink.attempts < 1 and time.monotonic() < deadline:
                time.sleep(0.005)
            with log.open("ab") as handle:
                _ = handle.write(b"survived\n")
            _wait_for_events(sink, 1)
        finally:
            stop.set()
            worker.join(timeout=5.0)

        texts = _texts(sink)
        assert texts == ["survived"], "the drain thread died with the failed emit"


class _FlakyFlushSink(_RecordingSink):
    """Records events, but ``flush`` raises a transient error a fixed number of.

    Times before succeeding, to drive the drain loop's resilience.
    """

    def __init__(self, fail_times: int) -> None:
        super().__init__()
        self._fail_times = fail_times
        self.flush_attempts = 0

    @override
    def flush(self) -> None:
        self.flush_attempts += 1
        if self.flush_attempts <= self._fail_times:
            raise RuntimeError("transient flush failure")
        super().flush()


class TestStreamQueueIsBounded:
    """The pump->drain handoff must not grow without limit (runner OOM).

    A chatty child produces thousands of line events per second while a
    slow-but-alive sink can hold the drain thread for ~90s (POST timeout x
    retries) before ResilientSink degrades. The queue drops oldest past its
    cap -- capture prefers a visible gap over unbounded memory.
    """

    def test_overflow_drops_oldest_not_memory(self) -> None:
        queue: deque[bytes] = deque(maxlen=session._STREAM_QUEUE_MAX)
        overfill = session._STREAM_QUEUE_MAX + 1_000
        for i in range(overfill):
            queue.append(f"{i}\n".encode())
        assert len(queue) == session._STREAM_QUEUE_MAX
        # Oldest dropped, newest kept.
        assert queue[-1] == f"{overfill - 1}\n".encode()

    def test_spawn_constructs_a_bounded_queue(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The queue handed to the drain must carry the production cap."""
        wiring, _ = _wire(
            monkeypatch,
            tmp_path,
            RunConfig(cli_name="fake", sync=False),
            _FakeAdapter(tmp_path),
        )
        queue = cast(deque[bytes], wiring.drain["stream_queue"])
        assert queue.maxlen == session._STREAM_QUEUE_MAX

    def test_a_dropped_line_is_counted(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A loss is visible in the end-of-run stats, not a quiet gap."""
        monkeypatch.setattr(session, "_STREAM_QUEUE_MAX", 1)
        stats = _Stats()
        _ = _wire(
            monkeypatch,
            tmp_path,
            RunConfig(cli_name="sh", cli_args=("cat",), sync=False),
            IOStreamAdapter(),
            output=b"one\ntwo\n",
            stats=stats,
        )
        assert stats.counts["StreamEventDropped"] == 1


class TestFilesystemQueueBackpressure:
    """A slow sink must stop file reads before they exhaust runner memory."""

    def test_line_follower_waits_for_capacity(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        log = tmp_path / "own.jsonl"
        log.write_text("first\nsecond\n")

        async def two_lines(
            *args: object,
            **kwargs: object,
        ) -> AsyncIterator[follow.Line]:
            del args, kwargs
            yield follow.Line(path=log, text="first", restart=False)
            yield follow.Line(path=log, text="second", restart=False)

        monkeypatch.setattr(session, "follow_tree", two_lines)

        async def exercise() -> None:
            lines: asyncio.Queue[session._Captured] = asyncio.Queue(maxsize=1)
            until = asyncio.Event()
            watch = asyncio.create_task(
                session._watch_session_files(
                    _FakeAdapter(tmp_path),
                    (tmp_path,),
                    lines,
                    {},
                    lambda path: path == log,
                    until=until,
                    replay=False,
                ),
            )
            await asyncio.sleep(0)
            assert lines.qsize() == 1
            assert not watch.done(), "the follower did not wait for a full queue"
            first = await lines.get()
            await asyncio.wait_for(watch, timeout=1.0)
            second = await lines.get()
            assert [first.raw, second.raw] == [b"first", b"second"]

        asyncio.run(exercise())

    def test_overflow_is_counted_and_warned(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An eviction must be visible: stats counter + one WARN per run.

        A silent drop is a quiet gap in the transcript; the counter surfaces
        in the end-of-run stats line and the WARN names the stall.
        """
        queue: deque[bytes] = deque(maxlen=2)
        stats = _Stats()
        with caplog.at_level("WARNING"):
            for i in range(5):
                session._enqueue_stream_line(queue, stats, f"{i}\n".encode())
        assert stats.counts["StreamEventDropped"] == 3
        warns = [r for r in caplog.records if "queue full" in r.message]
        assert len(warns) == 1, "one WARN per run, not one per dropped event"
        assert "StreamEventDropped=3" in stats.render()


class TestDrainLoopSurvivesTransientError:
    """A transient error in the drain loop body must not kill capture (R-57).

    The drain runs on a daemon thread with no top-level guard, so any unhandled
    error (a flush hiccup, a stat race) killed the thread and silently stopped
    all capture for the rest of the run. The loop body must catch, log, and
    keep polling so a later turn still lands.
    """

    def test_loop_continues_after_a_flush_raises(self, tmp_path: Path) -> None:
        adapter = _FakeAdapter(tmp_path)
        sink = _FlakyFlushSink(fail_times=1)
        stats = _Stats()
        config = RunConfig(cli_name="fake")
        stop = _RecordingStop()
        slash_queue: deque[tuple[SlashCommand, datetime]] = deque()
        armed = threading.Event()

        # Written AFTER the drain arms its watch, as a real session file is:
        # the runner starts watching before it spawns the CLI, and a file that
        # predates the watch belongs to an earlier run.
        log = tmp_path / "session.jsonl"

        def _run() -> None:
            _drain_filesystem_loop(
                adapter,
                sink,
                stats,
                config,
                stop,
                baseline=frozenset(),
                slash_queue=slash_queue,
                armed=armed,
            )

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        assert armed.wait(5.0), "session-log watch did not arm"
        with log.open("ab") as handle:
            _ = handle.write(b"alpha\n")
        _wait_for_events(sink, 1)
        assert sink.events, "the first line never reached the sink"
        assert sink.flush_attempts > 0, "the failing flush never fired"

        with log.open("ab") as handle:
            _ = handle.write(b"omega\n")
        _wait_for_events(sink, 2)
        stop.set()
        worker.join(timeout=5.0)

        assert not worker.is_alive(), "drain thread died on the transient flush error"
        texts = _texts(sink)
        assert texts == ["alpha", "omega"], (
            "a transient flush error stopped capture instead of continuing"
        )


class TestDrainIsWakeDriven:
    """The drain must wake on a write, not on a timer.

    The poll cost is discovery, not the tick: claude's ``session_dirs()``
    returns every project directory it has ever used, so each 0.2s pass walked
    999 of them -- 86ms median, 43% of every tick spent in ``stat``. A watch
    replaces both the walk and the wait.
    """

    def test_a_line_arrives_without_any_timer(self, tmp_path: Path) -> None:
        """With every sleep made fatal, a written line must still be captured.

        Any timer left in the drain path fails here rather than merely being
        slow, so a reintroduced poll cannot pass by running fast enough.
        """
        adapter = _FakeAdapter(tmp_path)
        sink = _RecordingSink()
        stats = _Stats()
        config = RunConfig(cli_name="fake")
        stop = threading.Event()
        armed = threading.Event()

        drain_thread: list[int] = []
        slept_in_drain: list[float] = []
        real_sleep = time.sleep

        def watched_sleep(seconds: float) -> None:
            # Only the DRAIN's sleeps are forbidden; this test's own waits run
            # on another thread and must still work.
            if threading.get_ident() in drain_thread:
                slept_in_drain.append(seconds)
            real_sleep(seconds)

        def _run() -> None:
            drain_thread.append(threading.get_ident())
            _drain_filesystem_loop(
                adapter,
                sink,
                stats,
                config,
                stop,
                baseline=frozenset(),
                slash_queue=deque(),
                armed=armed,
            )

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(time, "sleep", watched_sleep)
            worker = threading.Thread(target=_run, daemon=True)
            worker.start()
            try:
                assert armed.wait(5.0), "session-log watch did not arm"
                (tmp_path / "session.jsonl").write_bytes(b"alpha\n")
                deadline = time.monotonic() + 5.0
                while not sink.events and time.monotonic() < deadline:
                    real_sleep(0.01)
            finally:
                stop.set()
                worker.join(timeout=5.0)

        texts = _texts(sink)
        assert texts == ["alpha"]
        assert slept_in_drain == [], (
            f"the drain slept {slept_in_drain}; it must wake on a write"
        )

    def test_does_not_walk_every_session_directory(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Discovery happens once, not per wake.

        A drain that re-walks on every event pays claude's 999-directory scan
        per captured line rather than per run.
        """
        adapter = _FakeAdapter(tmp_path)
        walks: list[int] = []
        real_session_dirs = adapter.session_dirs

        def counting_dirs() -> Iterable[Path]:
            walks.append(1)
            return real_session_dirs()

        monkeypatch.setattr(adapter, "session_dirs", counting_dirs)
        sink = _RecordingSink()
        stop = threading.Event()
        armed = threading.Event()

        def _run() -> None:
            _drain_filesystem_loop(
                adapter,
                sink,
                _Stats(),
                RunConfig(cli_name="fake"),
                stop,
                baseline=frozenset(),
                slash_queue=deque(),
                armed=armed,
            )

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        try:
            assert armed.wait(5.0), "session-log watch did not arm"
            log = tmp_path / "session.jsonl"
            # APPEND, never rewrite: an identical rewrite leaves the byte
            # length unchanged, so the cursor rightly reports nothing new.
            for count, text in enumerate((b"one\n", b"two\n", b"three\n"), start=1):
                with log.open("ab") as handle:
                    _ = handle.write(text)
                _wait_for_events(sink, count)
        finally:
            stop.set()
            worker.join(timeout=5.0)

        texts = _texts(sink)
        assert texts == ["one", "two", "three"], "not every line was captured"
        # One walk to arm the watch. The old loop walked once per 0.2s tick --
        # measured at 24 walks for these three lines.
        assert len(walks) <= 2, f"walked the session dirs {len(walks)} times"


class TestWholeFileAdapterIsDrainedWhole:
    """A whole-file adapter must receive the whole body, not one line.

    Gemini rewrites one JSON object in place rather than appending records.
    Fed line-by-line, its parser sees fragments of a pretty-printed object and
    every turn is lost -- silently, because a fragment is simply unparseable
    rather than an error.
    """

    def test_gemini_shaped_session_yields_its_turn(self, tmp_path: Path) -> None:
        adapter = _WholeFileAdapter(tmp_path)
        sink = _RecordingSink()
        stop = threading.Event()
        armed = threading.Event()
        log = tmp_path / "session-x.json"

        def _run() -> None:
            _drain_filesystem_loop(
                adapter,
                sink,
                _Stats(),
                RunConfig(cli_name="wholefile"),
                stop,
                baseline=frozenset(),
                slash_queue=deque(),
                armed=armed,
            )

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        try:
            assert armed.wait(5.0), "session-log watch did not arm"
            # Pretty-printed across several lines, as gemini writes it: no
            # single line is valid JSON on its own.
            log.write_text(json.dumps({"messages": ["hello"]}, indent=2) + "\n")
            _wait_for_events(sink, 1)
        finally:
            stop.set()
            worker.join(timeout=5.0)

        texts = _texts(sink)
        assert texts == ["hello"], "a whole-file session was drained line-by-line"


class TestCompactionReDerivesPositions:
    """A compacted transcript re-derives its part rather than appending twice.

    Claude does not append on compaction -- it REWRITES the session file,
    smaller, retaining the turns it kept. A byte cursor sees the shrink and
    re-reads the file whole, so the retained lines arrive again.

    They must not become second copies. The runner no longer dedups by record
    id (it cannot: a compaction legitimately CHANGES a record, and identity
    could only ever suppress a duplicate, never correct a change). Instead the
    rewrite is reported as a ``restart``, positions re-derive from zero, and
    the re-read records land back on the keys they already held -- an
    overwrite, which is what makes disk the truth.
    """

    def test_a_rewrite_restarts_the_parts_positions(self, tmp_path: Path) -> None:
        adapter = _UuidAdapter(tmp_path)
        sink = _RecordingSink()
        stop = threading.Event()
        armed = threading.Event()
        log = tmp_path / "session.jsonl"

        def _run() -> None:
            _drain_filesystem_loop(
                adapter,
                sink,
                _Stats(),
                RunConfig(cli_name="uuids"),
                stop,
                baseline=frozenset(),
                slash_queue=deque(),
                armed=armed,
            )

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        try:
            assert armed.wait(5.0), "session-log watch did not arm"
            with log.open("ab") as handle:
                _ = handle.write(_claude_line("a").encode())
            _wait_for_events(sink, 1)
            assert sink.events, "the first line never reached the sink"
            with log.open("ab") as handle:
                _ = handle.write((_claude_line("b") + _claude_line("c")).encode())
            _wait_for_events(sink, 3)
            captured = len(sink.events)

            # Compaction: the file is REPLACED, smaller, and keeps ``c``.
            log.write_bytes((_claude_line("c") + _claude_line("d")).encode())
            _wait_for_events(sink, captured + 1)
        finally:
            stop.set()
            worker.join(timeout=5.0)

        # The retained turn re-derives to position 0 of the rewritten file, so
        # it OVERWRITES its stored row rather than appending a duplicate.
        after = [
            (idx, text)
            for (idx, _n, _e), text in zip(sink.events, _texts(sink), strict=True)
        ]
        assert "d" in [t for _i, t in after], (
            "the post-compaction turn was never captured"
        )
        restarted = [i for i, (_idx, _n, e) in enumerate(sink.events) if e.restart]
        assert restarted, "the rewrite was never reported as a restart"
        # Everything from the restart onward re-numbers from zero.
        tail = [idx for idx, _t in after[restarted[0] :]]
        assert tail == list(range(len(tail))), (
            f"a compacted file did not re-derive its positions: {after}"
        )


def _uuid_records(stream: TextIO) -> Iterator[SessionRecord]:
    """Read the claude-shaped fixture lines ``_claude_line`` writes."""
    for line in stream:
        obj = json_loads(line, dict[str, object])
        message = from_plain(obj["message"], dict[str, object])
        yield UserMessage(content=str(message["content"]))


class _UuidAdapter(_FakeAdapter):
    """A line adapter over uuid-stamped records, like claude's."""

    name: str = "uuids"
    cli_binary: str = "uuids"

    @override
    def reader(self) -> Tail[TraxRecord]:
        return Tail(_uuid_records)


class TestAdapterRegistryFreshPerRun:
    """Each run gets a fresh adapter so per-run state never leaks across runs."""

    def test_registry_builds_a_fresh_adapter_each_call(self) -> None:
        """The codex adapter carries per-run ``_last_model`` state.

        Two runs in one process (tests, a future supervisor) must not share it. The
        registry holds a factory, so each lookup yields a distinct instance.
        """
        factory = session._ADAPTERS["codex"]
        first = factory()
        second = factory()
        assert first is not second


class TestMissingBinary:
    """A missing CLI binary fails cleanly before the PTY fork."""

    def test_run_raises_systemexit_when_binary_absent(self, tmp_path: Path) -> None:
        # An adapter whose ``cli_binary`` is not on PATH: the spawn path must
        # detect this with ``shutil.which`` and raise SystemExit, rather than
        # failing inside the forked child's ``execvp`` where the parent can't
        # turn it into a clean message.
        adapter = _FakeAdapter(tmp_path)
        adapter.cli_binary = "definitely-not-a-real-binary-xyz"
        config = RunConfig(
            cli_name=adapter.name,
            sync=False,
            out_path=tmp_path / "o.jsonl",
        )
        session._ADAPTERS[adapter.name] = lambda: adapter
        try:
            with pytest.raises(SystemExit, match="not found in PATH"):
                run(config)
        finally:
            session._ADAPTERS.pop(adapter.name, None)


class TestRoutingEnv:
    """The routing identity exported into the wrapped CLI's environment."""

    def test_actor_and_rooms_exported(self) -> None:
        env = _routing_env(
            RunConfig(cli_name="codex", actor="scientist", rooms=("sear", "lab")),
        )
        assert env == {"TRAX_ACTOR": "scientist", "TRAX_ROOMS": "sear,lab"}

    def test_omits_unset_fields(self) -> None:
        # No actor, no rooms -> nothing exported (empty env, not blank vars).
        assert _routing_env(RunConfig(cli_name="codex")) == {}

    def test_exports_granted_handle_when_known(self) -> None:
        # On the sync path the session opens eagerly, so the server-granted
        # handle (after collision suffixing) is known before fork. The child
        # must see its REAL address, not the requested name (#453).
        env = _routing_env(
            RunConfig(cli_name="codex", actor="scientist", rooms=("lab",)),
            granted_actor="scientist#2",
        )
        assert env == {"TRAX_ACTOR": "scientist#2", "TRAX_ROOMS": "lab"}

    def test_falls_back_to_requested_when_no_grant(self) -> None:
        # Local / --no-sync runs have no collision arbiter, so no granted name;
        # the requested actor is exported as-is.
        env = _routing_env(
            RunConfig(cli_name="codex", actor="scientist"),
            granted_actor=None,
        )
        assert env == {"TRAX_ACTOR": "scientist"}


class TestEmitSlashCommands:
    """Queued slash-commands become captured turns on the sink-writer thread."""

    def test_drains_queue_into_sink(self) -> None:
        sink = _RecordingSink()
        stats = _Stats()
        at = datetime(2026, 6, 1, tzinfo=UTC)
        queue: deque[tuple[SlashCommand, datetime]] = deque(
            [
                (SlashCommand(command="exit"), at),
                (SlashCommand(command="model", args="gpt-5"), at),
            ],
        )
        _emit_slash_commands(
            _FakeAdapter(Path()),
            sink,
            stats,
            RunConfig(cli_name="fake"),
            queue,
        )
        assert not queue  # Fully drained.
        # NOT records: a command is absent from the session log, so it holds no
        # position in any part and must not consume one.
        assert sink.events == []
        assert [c.command for c, _at in sink.slash] == ["exit", "model"]
        assert sink.slash[1][0].args == "gpt-5"
        # The submit-time clock the detector stamped rides along -- a typed
        # command has no CLI-recorded timestamp.
        assert [when for _c, when in sink.slash] == [at, at]
        assert stats.counts == {"SlashCommand": 2}

    def test_empty_queue_is_a_noop(self) -> None:
        sink = _RecordingSink()
        _emit_slash_commands(
            _FakeAdapter(Path()),
            sink,
            _Stats(),
            RunConfig(cli_name="fake"),
            deque(),
        )
        assert sink.events == []

    def test_a_command_that_switches_sessions_is_noted(self) -> None:
        """Only the adapter's switching commands; ``/model`` keeps the session."""
        stats = _Stats()
        at = datetime(2026, 6, 1, tzinfo=UTC)
        _emit_slash_commands(
            _FakeAdapter(Path()),
            _RecordingSink(),
            stats,
            RunConfig(cli_name="fake"),
            deque(
                [
                    (SlashCommand(command="model"), at),
                    (SlashCommand(command="clear"), at),
                ],
            ),
        )
        assert stats.switches == ["clear"]


class TestASessionSwitchIsWarnedAtExit:
    """A run that saw ``/clear`` says, as it exits, what it did not capture.

    Measured on claude 2.1.302 and codex 0.163.0: the turns after ``/clear``,
    ``/branch`` or ``/fork`` land in a session under another id, which the
    run does not own, so the transcript silently stopped there.
    """

    @pytest.mark.parametrize(("switches", "warned"), [(["clear"], True), ([], False)])
    def test_the_exit_names_the_switching_commands(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        switches: list[str],
        warned: bool,
    ) -> None:
        monkeypatch.setattr(
            session,
            "_ADAPTERS",
            {"fake": lambda: _FakeAdapter(tmp_path)},
        )
        monkeypatch.setattr(session, "_open_sink", _open_recording_sink)
        monkeypatch.setattr(
            session,
            "_spawn_and_drain",
            partial(_spawn_switching, switches),
        )

        assert run(RunConfig(cli_name="fake", sync=False)) == 0

        assert ("/clear" in capsys.readouterr().err) is warned


def _open_recording_sink(config: RunConfig, adapter: Adapter) -> Sink:
    """Open a recording sink in place of the run's real one."""
    del config, adapter
    return _RecordingSink()


def _spawn_switching(
    switches: list[str],
    config: RunConfig,
    adapter: Adapter,
    sink: Sink,
    stats: _Stats,
) -> int:
    """Stand in for a run whose human typed ``switches``."""
    del config, adapter, sink
    stats.switches.extend(switches)
    return 0


class TestDryRunDrain:
    """The dry-run replay must dispatch each adapter by its drain shape."""

    def test_whole_file_adapter_emits_existing_body(self, tmp_path: Path) -> None:
        """Dry-run on a whole-file (gemini) session re-reads and emits its turn.

        K3: dry-run used to feed every adapter through ``tail``'s line-split
        (``whole_file=False``), so a whole-file JSON body never parsed and no
        event was emitted. The dry-run replay must dispatch whole-file adapters
        to the re-read path, exactly like the live drain.
        """
        log = tmp_path / "session-x.json"
        log.write_text(json.dumps({"messages": ["replayed"]}))
        adapter = _WholeFileAdapter(tmp_path)
        sink = _RecordingSink()
        stats = _Stats()
        stop = threading.Event()
        stop.set()
        rc = session._dry_run_drain(
            RunConfig(cli_name="wholefile"),
            adapter,
            sink,
            stats,
            stop=stop,
        )
        assert rc == 0
        texts = _texts(sink)
        assert texts == ["replayed"]

    def test_returns_when_stopped(self, tmp_path: Path) -> None:
        """The dry-run loop exits promptly once ``stop`` is set (no spin)."""
        adapter = _FakeAdapter(tmp_path)
        stop = threading.Event()
        stop.set()  # Already stopped: the loop runs one final sweep and returns.
        rc = session._dry_run_drain(
            RunConfig(cli_name="fake"),
            adapter,
            _RecordingSink(),
            _Stats(),
            stop=stop,
        )
        assert rc == 0


class _BorrowedClient:
    """A shared client whose lifetime is owned outside one ``trax run``."""

    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class TestRunPreservesClient:
    """A ``trax run`` must not close the daemon's shared client."""

    def test_run_leaves_config_client_open(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The CLI cache, not one run, owns the supplied transport.

        The daemon reuses this client across requests. Closing it here leaves
        the closed instance cached, so the next invocation cannot send.
        """
        client = _BorrowedClient()
        config = RunConfig(cli_name="codex", dry_run=True, client=cast(Client, client))

        # Stub the drain so no session files are scanned and the run returns at
        # once; the client ownership boundary is the only thing under test.
        def _fake_dry_run(
            config: RunConfig,
            adapter: Adapter,
            sink: Sink,
            stats: _Stats,
            *,
            stop: threading.Event | None = None,
        ) -> int:
            del config, adapter, sink, stats, stop
            return 0

        monkeypatch.setattr(session, "_dry_run_drain", _fake_dry_run)
        rc = run(config)
        assert rc == 0
        assert client.close_calls == 0


class TestAPrivateRun:
    """``--private`` reaches the sink, in a detached host too, and stops a run early."""

    def test_private_reaches_the_run_and_a_detached_host(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ran: list[RunConfig] = []
        hosted: list[Callable[[HostSpec | None], int]] = []
        monkeypatch.setattr(session, "run", partial(_keep_run, ran))
        monkeypatch.setattr(detach, "launch", partial(_keep_host, hosted))

        assert session.main(["claude", "--private", "--no-sync"]) == 0
        assert session.main(["claude", "--detach"]) == 0
        assert session.main(["claude", "--private", "--detach"]) == 0
        for start in hosted:
            assert start(None) == 0

        assert [config.private for config in ran] == [True, False, True]

    def test_the_sink_of_a_private_run_refuses_a_server_that_does_not_hide(
        self,
    ) -> None:
        """An older server answers no audience: the run stops before the CLI starts."""
        config = RunConfig(
            cli_name="sh",
            client=cast(Client, _UploadsClient()),
            private=True,
        )

        with pytest.raises(PrivateNotHeldError):
            _ = _open_sink(config, IOStreamAdapter()).open()

    def test_a_run_stopped_so_exits_2(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        client = _BorrowedClient()
        config = RunConfig(cli_name="codex", dry_run=True, client=cast(Client, client))
        monkeypatch.setattr(session, "_dry_run_drain", _refuse_private)

        assert run(config) == 2
        assert "predates hiding a session; nothing was sent" in capsys.readouterr().err


def _keep_run(ran: list[RunConfig], config: RunConfig) -> int:
    """Keep the config a run was asked to run, and run nothing."""
    ran.append(config)
    return 0


def _keep_host(
    hosted: list[Callable[[HostSpec | None], int]],
    name: str,
    *,
    command: str,
    start: Callable[[HostSpec | None], int],
) -> int:
    """Keep what a detached host would start, and launch nothing."""
    del name, command
    hosted.append(start)
    return 0


def _refuse_private(*args: object, **kwargs: object) -> int:
    """Stand in for a run whose server would not hide its session."""
    del args, kwargs
    raise PrivateNotHeldError("the server predates hiding a session")


class TestFallbackClientInbound:
    """The implicit localhost sync client must also receive inbound messages."""

    def test_none_client_starts_the_inbound_worker(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        polling = threading.Event()
        clients: list[Client] = []

        def watch(*args: object, **kwargs: object) -> None:
            stop = args[4]
            armed = kwargs["armed"]
            assert isinstance(stop, threading.Event)
            assert isinstance(armed, threading.Event)
            armed.set()
            _ = stop.wait(2.0)

        class _Relay:
            def __init__(self, argv: object, **kwargs: object) -> None:
                del argv, kwargs

            def run(self) -> int:
                assert polling.wait(1.0), "the inbound worker never started"
                return 0

        def poll(client: Client, *args: object, **kwargs: object) -> None:
            del args, kwargs
            clients.append(client)
            polling.set()

        def open_sink(config: RunConfig, adapter: Adapter) -> Sink:
            del config, adapter
            return _RecordingSink()

        monkeypatch.setattr(
            session,
            "_ADAPTERS",
            {"fake": lambda: _FakeAdapter(tmp_path)},
        )
        monkeypatch.setattr(
            session,
            "_open_sink",
            open_sink,
        )
        monkeypatch.setattr(session, "_drain_filesystem_loop", watch)
        monkeypatch.setattr(session, "_inbound_poll_loop", poll)
        monkeypatch.setattr(session, "ThreadedRelay", _Relay)
        monkeypatch.setattr(shutil, "which", _always_found)

        assert run(RunConfig(cli_name="fake", client=None, quiesce_seconds=0.0)) == 0
        assert len(clients) == 1
        assert clients[0].base_url == LOCALHOST_FALLBACK_URL


class TestBoxHookLifetime:
    """The hook's directory belongs to the run that made it."""

    @pytest.mark.parametrize("gone_already", [False, True])
    def test_a_placed_claude_run_is_handed_the_hook_and_removes_its_dir(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        gone_already: bool,
    ) -> None:
        heard: list[Sequence[str]] = []
        texts: list[str] = []

        class _Placed(_RecordingSink):
            @property
            @override
            def session_id(self) -> uuid.UUID | None:
                return uuid.UUID(int=1)

        class _Relay:
            def __init__(self, argv: Sequence[str], **kwargs: object) -> None:
                del kwargs
                heard.append(argv)
                self.settings = argv[argv.index("--settings") + 1]
                settings = Path(argv[argv.index("--settings") + 1])
                texts.append(settings.read_text())

            def run(self) -> int:
                if gone_already:
                    shutil.rmtree(Path(self.settings).parent)
                return 0

        def watch(*args: object, **kwargs: object) -> None:
            del args
            armed = kwargs["armed"]
            assert isinstance(armed, threading.Event)
            armed.set()

        def open_sink(config: RunConfig, adapter: Adapter) -> Sink:
            del config, adapter
            return _Placed()

        def poll(*args: object, **kwargs: object) -> None:
            del args, kwargs

        def place(*args: object, **kwargs: object) -> SessionPlacement:
            del args, kwargs
            return SessionPlacement(workspace_id=uuid.UUID(int=2))

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(session, "_open_sink", open_sink)
        monkeypatch.setattr(session, "_drain_filesystem_loop", watch)
        monkeypatch.setattr(session, "_inbound_poll_loop", poll)
        monkeypatch.setattr(session, "place_run", place)
        monkeypatch.setattr(session, "ThreadedRelay", _Relay)
        monkeypatch.setattr(shutil, "which", _always_found)

        config = RunConfig(
            cli_name="claude",
            client=Client(base_url=LOCALHOST_FALLBACK_URL),
            profile="prod",
            quiesce_seconds=0.0,
        )
        assert run(config) == 0
        assert "--profile prod" in texts[0]

        [argv] = heard
        assert not Path(argv[argv.index("--settings") + 1]).parent.exists()


_RESUMED: Final = "33333333-3333-4333-8333-333333333333"


class TestSessionOwnerAndArgv:
    """Which CLI session a run claims, and how its CLI is told."""

    def test_a_fresh_claude_run_mints_and_passes_its_session_id(self) -> None:
        config = RunConfig(cli_name="claude", cli_args=("--model", "haiku"))
        owner = _session_owner(ClaudeAdapter(), config)
        assert owner is not None
        assert owner.session_id is not None
        assert str(uuid.UUID(owner.session_id)) == owner.session_id
        assert _cli_argv(ClaudeAdapter(), config, owner) == [
            "claude",
            "--session-id",
            owner.session_id,
            "--model",
            "haiku",
        ]

    @pytest.mark.parametrize(
        ("args", "named"),
        [(("--session-id", "s1"), "s1"), (("--session-id=s2",), "s2")],
    )
    def test_a_claude_session_named_on_the_command_line_is_claimed(
        self,
        args: tuple[str, ...],
        named: str,
    ) -> None:
        """Claimed, not minted -- and not passed a second time."""
        config = RunConfig(cli_name="claude", cli_args=args)
        owner = _session_owner(ClaudeAdapter(), config)
        assert owner is not None
        assert owner.session_id == named
        assert _cli_argv(ClaudeAdapter(), config, owner) == ["claude", *args]

    @pytest.mark.parametrize(
        "args",
        [
            ("--continue",),
            ("-pc",),
            ("--resume",),
            ("--resume", "fix bug"),
            ("--resume=fix bug",),
            (f"-r{_RESUMED}",),
        ],
        ids=["continue", "cluster", "picker", "search", "equals-search", "attached"],
    )
    def test_a_claude_resume_is_never_handed_a_session_id(
        self,
        args: tuple[str, ...],
    ) -> None:
        """Measured: claude refuses ``--session-id`` beside an unforked resume.

        The run owns no id here: one it can name arrives as ``cli_session_id``,
        resolved before the run, and one claude picks it cannot capture.
        """
        config = RunConfig(cli_name="claude", cli_args=args)
        owner = _session_owner(ClaudeAdapter(), config)
        assert owner is not None
        assert owner.session_id is None
        assert _cli_argv(ClaudeAdapter(), config, owner) == ["claude", *args]

    def test_a_forked_claude_resume_is_minted_a_fresh_id(self) -> None:
        """The fork writes a new file, so the run names it up front."""
        args = ("--resume", _RESUMED, "--fork-session")
        config = RunConfig(cli_name="claude", cli_args=args)
        owner = _session_owner(ClaudeAdapter(), config)
        assert owner is not None
        assert owner.session_id is not None
        assert owner.session_id != _RESUMED
        assert _cli_argv(ClaudeAdapter(), config, owner) == [
            "claude",
            "--session-id",
            owner.session_id,
            *args,
        ]

    def test_a_resumed_claude_run_claims_its_materialized_id(self) -> None:
        """The resume path names the id itself; claude must not get a second."""
        config = RunConfig(cli_name="claude", cli_session_id="r1")
        owner = _session_owner(ClaudeAdapter(), config)
        assert owner is not None
        assert owner.session_id == "r1"
        assert _cli_argv(ClaudeAdapter(), config, owner) == ["claude"]

    def test_a_claude_run_placed_on_a_station_is_handed_the_box_hook(self) -> None:
        argv, files = _with_box_hook(
            ["claude", "--model", "haiku"],
            adapter=ClaudeAdapter(),
            placed={"TRAX_WORKSPACE": "c5286865-67b6-4bd8-ab51-e06e10c326c5"},
            profile="prod",
        )

        assert files is not None
        assert argv == ["claude", "--settings", str(files.settings), "--model", "haiku"]
        text = files.settings.read_text()
        assert "--profile prod" in text
        assert "UserPromptSubmit" in text
        assert "SessionStart" in text

    def test_an_unplaced_claude_run_gets_only_the_readiness_hook(self) -> None:
        argv, files = _with_box_hook(
            ["claude"],
            adapter=ClaudeAdapter(),
            placed={},
            profile=None,
        )

        assert files is not None
        assert argv == ["claude", "--settings", str(files.settings)]
        text = files.settings.read_text()
        assert "SessionStart" in text
        assert "UserPromptSubmit" not in text

    @pytest.mark.parametrize("spelled", ["split", "equals"])
    def test_the_persons_own_settings_are_merged_into_ours(
        self,
        tmp_path: Path,
        spelled: str,
    ) -> None:
        mine = tmp_path / "mine.json"
        stop: dict[str, object] = {"hooks": [{"type": "command", "command": "ding"}]}
        _ = mine.write_text(json.dumps({"model": "opus", "hooks": {"Stop": [stop]}}))
        given = (
            ["--settings", str(mine)] if spelled == "split" else [f"--settings={mine}"]
        )

        argv, files = _with_box_hook(
            ["claude", *given, "--model", "haiku"],
            adapter=ClaudeAdapter(),
            placed={"TRAX_WORKSPACE": "w"},
            profile=None,
        )

        assert files is not None
        assert argv == ["claude", "--settings", str(files.settings), "--model", "haiku"]
        merged = json_loads(files.settings.read_text(), dict[str, object])
        hooks = from_plain(merged["hooks"], dict[str, list[object]])
        assert merged["model"] == "opus"
        assert hooks["Stop"] == [stop]
        assert {"SessionStart", "UserPromptSubmit"} <= set(hooks)

    @pytest.mark.parametrize(
        ("adapter", "args"),
        [
            (CodexAdapter(), []),
            (ClaudeAdapter(), ["--settings", "/no/such/mine.json"]),
            (ClaudeAdapter(), ["--settings=/no/such/mine.json"]),
        ],
    )
    def test_no_hook_for_another_cli_or_settings_that_cannot_be_merged(
        self,
        adapter: Adapter,
        args: list[str],
    ) -> None:
        argv = [adapter.cli_binary, *args]
        placed = {"TRAX_WORKSPACE": "w"}

        assert _with_box_hook(argv, adapter=adapter, placed=placed, profile=None) == (
            argv,
            None,
        )

    def test_a_placed_claude_or_codex_is_told_its_station_by_its_own_flag(self) -> None:
        placed = {"TRAX_WORKSPACE": "w"}
        # A codex ``-c`` of another key, and one left dangling, are not the person's.
        codex_args = ["--no-daemon", "-c", 'model="o3"', "-c"]

        claude = _with_canvas_note(
            ["claude", "--model", "haiku"],
            adapter=ClaudeAdapter(),
            placed=placed,
        )
        codex = _with_canvas_note(
            ["codex", *codex_args],
            adapter=CodexAdapter(),
            placed=placed,
        )

        assert claude == [
            "claude",
            "--append-system-prompt",
            _CANVAS_NOTE,
            "--model",
            "haiku",
        ]
        assert codex[:2] == ["codex", "-c"]
        assert codex[3:] == codex_args
        assert tomllib.loads(codex[2]) == {"developer_instructions": _CANVAS_NOTE}
        assert "$TRAX_WORKSPACE" in _CANVAS_NOTE

    def test_a_placed_cli_is_told_it_is_logged_and_how_to_log_results(self) -> None:
        """The note is a TOML string as it stands, so it holds no quote or backslash."""
        assert "already logged to trax as the chat's AgentSession" in _CANVAS_NOTE
        assert "`--as` set to $TRAX_ACTOR" in _CANVAS_NOTE
        assert "`trax help` lists the grammar" in _CANVAS_NOTE
        assert '"' not in _CANVAS_NOTE
        assert "\\" not in _CANVAS_NOTE

    @pytest.mark.parametrize(
        ("adapter", "placed", "args"),
        [
            (ClaudeAdapter(), {}, []),
            (CodexAdapter(), {"TRAX_WORKSTREAM": "s"}, []),
            (GeminiAdapter(), {"TRAX_WORKSPACE": "w"}, []),
            (ClaudeAdapter(), {"TRAX_WORKSPACE": "w"}, ["--append-system-prompt", "x"]),
            (
                ClaudeAdapter(),
                {"TRAX_WORKSPACE": "w"},
                ["--append-system-prompt-file=f"],
            ),
            (
                CodexAdapter(),
                {"TRAX_WORKSPACE": "w"},
                ["-c", 'developer_instructions="x"'],
            ),
            (
                CodexAdapter(),
                {"TRAX_WORKSPACE": "w"},
                ["--config", "developer_instructions=1"],
            ),
            (
                CodexAdapter(),
                {"TRAX_WORKSPACE": "w"},
                ['--config=developer_instructions="x"'],
            ),
        ],
    )
    def test_no_note_without_a_station_for_gemini_or_over_the_persons_own(
        self,
        adapter: Adapter,
        placed: dict[str, str],
        args: list[str],
    ) -> None:
        argv = [adapter.cli_binary, *args]

        assert _with_canvas_note(argv, adapter=adapter, placed=placed) == argv

    def test_codex_claims_only_a_resumed_id(self) -> None:
        fresh = _session_owner(CodexAdapter(), RunConfig(cli_name="codex"))
        resumed = _session_owner(
            CodexAdapter(),
            RunConfig(cli_name="codex", cli_session_id="c1"),
        )
        assert fresh is not None
        assert fresh.session_id is None
        assert resumed is not None
        assert resumed.session_id == "c1"

    def test_codex_runs_without_its_daemon_exactly_once(self) -> None:
        plain = RunConfig(cli_name="codex", cli_args=("exec", "hi"))
        explicit = RunConfig(cli_name="codex", cli_args=("--no-daemon",))
        assert _cli_argv(CodexAdapter(), plain, None) == [
            "codex",
            "--no-daemon",
            "--enable",
            "goals",
            "exec",
            "hi",
        ]
        assert _cli_argv(CodexAdapter(), explicit, None) == [
            "codex",
            "--enable",
            "goals",
            "--no-daemon",
        ]

    @pytest.mark.parametrize(
        "given",
        [
            ("--disable", "goals"),
            ("--enable=goals",),
            ("-c", "features.goals=false"),
            ("--config", "features.goals=true"),
            ("--config=features.goals=false",),
        ],
        ids=["disable", "enable-equals", "c", "config", "config-equals"],
    )
    def test_codex_goals_are_left_as_the_person_set_them(
        self,
        given: tuple[str, ...],
    ) -> None:
        """Codex drops ``/goal`` silently with goals off, so a run turns them on."""
        config = RunConfig(cli_name="codex", cli_args=(*given, "--no-daemon"))

        assert _cli_argv(CodexAdapter(), config, None) == [
            "codex",
            *given,
            "--no-daemon",
        ]

    def test_another_feature_or_a_dangling_flag_still_gets_goals(self) -> None:
        config = RunConfig(
            cli_name="codex",
            cli_args=("--no-daemon", "--enable", "web_search", "--disable"),
        )

        assert _cli_argv(CodexAdapter(), config, None)[1:3] == ["--enable", "goals"]

    def test_another_cli_claims_nothing_and_gets_its_args_verbatim(self) -> None:
        config = RunConfig(cli_name="gemini", cli_args=("-p", "x"))
        assert _session_owner(GeminiAdapter(), config) is None
        assert _cli_argv(GeminiAdapter(), config, None) == ["gemini", "-p", "x"]


_CHAT_RUN: Final = uuid.UUID("44444444-4444-4444-8444-444444444444")
"""The AgentSession a Chat run writes into, as ``--attach`` names it."""


def _spawned(
    cli: str,
    *,
    cli_args: tuple[str, ...],
    attach: uuid.UUID | None,
) -> list[str]:
    """Return the argv a run of ``cli`` spawns, with no session owner."""
    adapter = {
        "claude": ClaudeAdapter,
        "codex": CodexAdapter,
        "gemini": GeminiAdapter,
    }[cli]()
    config = RunConfig(cli_name=cli, cli_args=cli_args, attach=attach)
    return _cli_argv(adapter, config, None)


class TestChatRunApprovals:
    """What a run's CLI is told about approvals, and what the person overrides.

    A Chat run gets the interim auto review. On a computer declared a sandbox every
    run gets liberal permissions. The person's own permission flags always win.
    """

    @pytest.mark.parametrize(
        ("cli", "args", "expected"),
        [
            ("claude", (), ["claude", "--permission-mode", "auto"]),
            (
                "claude",
                ("--model", "haiku"),
                ["claude", "--permission-mode", "auto", "--model", "haiku"],
            ),
            (
                "codex",
                ("exec", "hi"),
                [
                    "codex",
                    "-c",
                    'approvals_reviewer="auto_review"',
                    "--no-daemon",
                    "--enable",
                    "goals",
                    "exec",
                    "hi",
                ],
            ),
        ],
        ids=["claude", "claude-with-args", "codex"],
    )
    def test_a_chat_run_gets_the_interim_auto_review(
        self,
        cli: str,
        args: tuple[str, ...],
        expected: list[str],
    ) -> None:
        assert _spawned(cli, cli_args=args, attach=_CHAT_RUN) == expected

    @pytest.mark.parametrize(
        ("cli", "args", "expected"),
        [
            ("claude", ("--model", "haiku"), ["claude", "--model", "haiku"]),
            (
                "codex",
                ("exec", "hi"),
                ["codex", "--no-daemon", "--enable", "goals", "exec", "hi"],
            ),
        ],
        ids=["claude", "codex"],
    )
    def test_a_run_outside_a_chat_is_unchanged(
        self,
        cli: str,
        args: tuple[str, ...],
        expected: list[str],
    ) -> None:
        assert _spawned(cli, cli_args=args, attach=None) == expected

    @pytest.mark.parametrize(
        "args",
        [
            ("--permission-mode", "plan"),
            ("--permission-mode=acceptEdits",),
            ("--dangerously-skip-permissions",),
            ("--permission-prompt-tool", "stdio"),
        ],
        ids=["mode", "mode-equals", "skip", "prompt-tool"],
    )
    def test_claude_keeps_the_permission_flags_the_person_passed(
        self,
        args: tuple[str, ...],
    ) -> None:
        assert _spawned("claude", cli_args=args, attach=_CHAT_RUN) == ["claude", *args]

    @pytest.mark.parametrize("declared", [False, True], ids=["chat", "sandbox"])
    @pytest.mark.parametrize(
        "args",
        [
            ("-a", "never"),
            ("--ask-for-approval=never",),
            ("-s", "read-only"),
            ("--sandbox=read-only",),
            ("--sandbox", "danger-full-access"),
            ("--full-auto",),
            ("--approve-for-me",),
            ("--dangerously-bypass-approvals-and-sandbox",),
            ("--yolo",),
            ("-c", 'approvals_reviewer="user"'),
            ('--config=approvals_reviewer="user"',),
            ("-c", 'approval_policy="on-request"'),
            ("--config", 'sandbox_mode="read-only"'),
            ("-c", 'sandbox_mode = "read-only"'),
        ],
        ids=[
            "ask",
            "ask-equals",
            "sandbox",
            "sandbox-equals",
            "sandbox-split",
            "full-auto",
            "approve-for-me",
            "bypass",
            "yolo",
            "reviewer",
            "reviewer-equals",
            "policy",
            "mode",
            "mode-spaced",
        ],
    )
    def test_codex_keeps_the_permission_flags_the_person_passed(
        self,
        args: tuple[str, ...],
        declared: bool,
    ) -> None:
        machine_host.set_sandbox(on=declared)
        assert _spawned("codex", cli_args=(*args, "exec", "hi"), attach=_CHAT_RUN) == [
            "codex",
            "--no-daemon",
            "--enable",
            "goals",
            *args,
            "exec",
            "hi",
        ]

    def test_other_codex_config_still_gets_the_auto_review(self) -> None:
        args = ("-c", 'model="o3"', "exec", "hi")
        assert _spawned("codex", cli_args=args, attach=_CHAT_RUN) == [
            "codex",
            "-c",
            'approvals_reviewer="auto_review"',
            "--no-daemon",
            "--enable",
            "goals",
            *args,
        ]

    @pytest.mark.parametrize(
        ("attach", "expected"),
        [
            (None, ["claude", "--permission-mode", "auto", "--model", "haiku"]),
            (_CHAT_RUN, ["claude", "--permission-mode", "auto", "--model", "haiku"]),
        ],
        ids=["plain", "chat"],
    )
    def test_claude_on_a_sandbox_runs_in_auto_mode_never_in_bypass_mode(
        self,
        attach: uuid.UUID | None,
        expected: list[str],
    ) -> None:
        # Bypass mode opens a startup dialog whose default is "No, exit" on a computer
        # that never accepted it, and nobody is watching a run that could answer it.
        machine_host.set_sandbox(on=True)
        argv = _spawned("claude", cli_args=("--model", "haiku"), attach=attach)

        assert argv == expected
        assert "--dangerously-skip-permissions" not in argv
        assert "bypassPermissions" not in argv

    @pytest.mark.parametrize("attach", [None, _CHAT_RUN], ids=["plain", "chat"])
    def test_codex_on_a_sandbox_skips_approvals(
        self,
        attach: uuid.UUID | None,
    ) -> None:
        machine_host.set_sandbox(on=True)
        assert _spawned("codex", cli_args=("exec", "hi"), attach=attach) == [
            "codex",
            "--dangerously-bypass-approvals-and-sandbox",
            "--no-daemon",
            "--enable",
            "goals",
            "exec",
            "hi",
        ]

    def test_other_codex_config_on_a_sandbox_still_gets_the_bypass(self) -> None:
        machine_host.set_sandbox(on=True)
        args = (
            "-c",
            'model="o3"',
            "--config=sandbox_workspace_write.network_access=true",
            "exec",
        )
        assert _spawned("codex", cli_args=args, attach=None) == [
            "codex",
            "--dangerously-bypass-approvals-and-sandbox",
            "--no-daemon",
            "--enable",
            "goals",
            *args,
        ]

    @pytest.mark.parametrize(
        "args",
        [
            ("--permission-mode", "plan"),
            ("--permission-mode=acceptEdits",),
            ("--dangerously-skip-permissions",),
            ("--permission-prompt-tool", "stdio"),
        ],
        ids=["mode", "mode-equals", "skip", "prompt-tool"],
    )
    def test_claude_on_a_sandbox_keeps_the_permission_flags_the_person_passed(
        self,
        args: tuple[str, ...],
    ) -> None:
        machine_host.set_sandbox(on=True)
        assert _spawned("claude", cli_args=args, attach=None) == ["claude", *args]

    def test_withdrawing_the_declaration_returns_to_the_default(self) -> None:
        machine_host.set_sandbox(on=True)
        machine_host.set_sandbox(on=False)
        assert _spawned("codex", cli_args=("exec", "hi"), attach=None) == [
            "codex",
            "--no-daemon",
            "--enable",
            "goals",
            "exec",
            "hi",
        ]
        assert _spawned("claude", cli_args=("--model", "haiku"), attach=None) == [
            "claude",
            "--model",
            "haiku",
        ]

    def test_another_cli_gets_no_approval_flags(self) -> None:
        machine_host.set_sandbox(on=True)
        assert _spawned("gemini", cli_args=("-p", "x"), attach=_CHAT_RUN) == [
            "gemini",
            "-p",
            "x",
        ]


class TestWrappedCliIsATopLevelSession:
    """What names the LAUNCHING session stays behind.

    Launched from inside a Claude Code session, an interactive ``trax run
    claude`` inherited ``CLAUDE_CODE_CHILD_SESSION``; claude then kept no
    transcript, and the run captured nothing while the model answered.
    """

    def test_the_adapters_markers_and_routing_reach_drop_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        dropped: list[object] = []

        class _CapturingRelay:
            def __init__(self, argv: object, **kwargs: object) -> None:
                del argv
                dropped.append(kwargs["drop_env"])

            def run(self) -> int:
                return 0

        def armed_drain(
            *args: object,
            armed: threading.Event,
            **kwargs: object,
        ) -> None:
            del args, kwargs
            armed.set()

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(session, "ThreadedRelay", _CapturingRelay)
        monkeypatch.setattr(shutil, "which", _always_found)
        monkeypatch.setattr(session, "_drain_filesystem_loop", armed_drain)

        _ = session._spawn_and_drain(
            RunConfig(cli_name="claude", quiesce_seconds=0.0),
            ClaudeAdapter(),
            _RecordingSink(),
            _Stats(),
        )

        assert dropped == [
            frozenset(
                {
                    "CLAUDE_CODE_CHILD_SESSION",
                    "CLAUDECODE",
                    "CLAUDE_CODE_SESSION_ID",
                    "TRAX_ACTOR",
                    "TRAX_ROOMS",
                    "TRAX_WORKSTREAM",
                    "TRAX_WORKSPACE",
                },
            ),
        ]

    def test_a_run_without_rooms_does_not_inherit_the_launchers(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A real child: the launcher's rooms are not this session's rooms."""
        monkeypatch.setenv("TRAX_ROOMS", "launcher-room")
        out = tmp_path / "run.jsonl"
        child = "import os; print('rooms=' + os.environ.get('TRAX_ROOMS', '-'))"

        rc = run(
            RunConfig(
                cli_name="sh",
                cli_args=(sys.executable, "-c", child),
                out_path=out,
                quiesce_seconds=0.0,
            ),
        )

        assert rc == 0
        assert "rooms=-" in out.read_text()
        assert "launcher-room" not in out.read_text()


@dataclass(slots=True, kw_only=True)
class _Wiring:
    """What ``_spawn_and_drain`` handed each collaborator."""

    argv: list[str] = field(default_factory=list[str])
    relay: dict[str, object] = field(default_factory=dict[str, object])
    relay_instance: object = None
    drain: dict[str, object] = field(default_factory=dict[str, object])
    inbound: list[object] = field(default_factory=list[object])
    daemons: list[bool] = field(default_factory=list[bool])


class _WiringRelay:
    """A relay that records how it was built, plays ``output``, and exits ``rc``."""

    def __init__(
        self,
        argv: list[str],
        *,
        wiring: _Wiring,
        rc: int,
        output: bytes,
        **kwargs: object,
    ) -> None:
        wiring.argv = list(argv)
        wiring.relay = kwargs
        wiring.relay_instance = self
        self._rc = rc
        self._output = output
        self._on_output = kwargs["on_output"]

    def run(self) -> int:
        if callable(self._on_output) and self._output:
            _ = self._on_output(self._output)
        return self._rc


def _wiring_drain(
    wiring: _Wiring,
    *args: object,
    armed: threading.Event,
    **kwargs: object,
) -> None:
    """Record the drain's inputs, and that it runs as a daemon; release the spawn."""
    wiring.drain = {"args": args, **kwargs}
    wiring.daemons.append(threading.current_thread().daemon)
    armed.set()


def _wiring_inbound(wiring: _Wiring, *args: object, **kwargs: object) -> None:
    """Record inbound delivery's inputs, and that it runs as a daemon."""
    wiring.inbound = [*args, kwargs]
    wiring.daemons.append(threading.current_thread().daemon)


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    config: RunConfig,
    adapter: Adapter,
    *,
    sink: Sink | None = None,
    stats: _Stats | None = None,
    rc: int = 0,
    output: bytes = b"",
    relay: type[_WiringRelay] = _WiringRelay,
    wiring: _Wiring | None = None,
) -> tuple[_Wiring, int]:
    """Run ``_spawn_and_drain`` against recording collaborators."""
    wiring = wiring or _Wiring()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(
        session,
        "ThreadedRelay",
        partial(relay, wiring=wiring, rc=rc, output=output),
    )
    monkeypatch.setattr(
        session,
        "_drain_filesystem_loop",
        partial(_wiring_drain, wiring),
    )
    monkeypatch.setattr(session, "_inbound_poll_loop", partial(_wiring_inbound, wiring))
    monkeypatch.setattr(shutil, "which", _always_found)
    status = session._spawn_and_drain(
        replace(config, quiesce_seconds=0.0),
        adapter,
        sink or _RecordingSink(),
        stats or _Stats(),
    )
    return wiring, status


class _RecordingOpener:
    """Stand in for ``open_worktree_diff``: record each cwd, return a fixed value."""

    def __init__(self, result: object) -> None:
        self.calls: list[Path] = []
        self.result = result

    def __call__(self, cwd: Path) -> object:
        self.calls.append(cwd)
        return self.result


class _RefusingSink(_RecordingSink):
    """A sink whose ``open`` refuses, like a server that will not hide a session."""

    @override
    def open(self) -> str | None:
        raise PrivateNotHeldError("the server predates hiding a session")


# The sleep outlives the microseconds between ``sink.open`` raising and the watchdog
# join, so a join that WAITS (deadline ahead) finds the thread gone while one whose
# deadline is already in the past leaves it alive.
def _sleeping_drain(
    recorded: list[threading.Thread],
    *args: object,
    armed: threading.Event,
    **kwargs: object,
) -> None:
    """Record the drain thread, arm the spawn, then briefly stay alive."""
    del args, kwargs
    recorded.append(threading.current_thread())
    armed.set()
    time.sleep(0.002)


class TestSpawnWorktreeDiffWiring:
    """``_spawn_and_drain`` builds the worktree diff only with ``--diff``."""

    def test_the_diff_flag_opens_the_capture_and_hands_it_to_the_drain(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """With ``diff=True`` the cwd's capture is opened and passed to the drain."""
        sentinel = object()
        opener = _RecordingOpener(sentinel)
        monkeypatch.setattr(session, "open_worktree_diff", opener)
        wiring, _ = _wire(
            monkeypatch,
            tmp_path,
            RunConfig(cli_name="claude", sync=False, diff=True),
            ClaudeAdapter(),
        )
        assert opener.calls == [Path.cwd()]
        assert wiring.drain["worktree_diff"] is sentinel

    def test_without_the_flag_no_capture_is_opened(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """With ``diff=False`` the capture is never opened and the drain gets None."""
        opener = _RecordingOpener(object())
        monkeypatch.setattr(session, "open_worktree_diff", opener)
        wiring, _ = _wire(
            monkeypatch,
            tmp_path,
            RunConfig(cli_name="claude", sync=False, diff=False),
            ClaudeAdapter(),
        )
        assert opener.calls == []
        assert wiring.drain["worktree_diff"] is None

    def test_a_private_refusal_joins_the_drain_then_reraises(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A refused private session joins the drain thread by name and re-raises.

        Drives the ``except PrivateNotHeldError`` branch so its
        ``_join_with_watchdog(drain_thread, ...)`` call runs: a mutant that drops
        the thread or the deadline crashes before the re-raise, so the refusal no
        longer surfaces.
        """
        with pytest.raises(PrivateNotHeldError):
            _ = _wire(
                monkeypatch,
                tmp_path,
                RunConfig(cli_name="claude", sync=False),
                ClaudeAdapter(),
                sink=_RefusingSink(),
            )

    def test_the_refusal_join_waits_for_the_drain_before_reraising(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """The refusal join's deadline is AHEAD, so it waits out the drain thread.

        With the drain thread still finishing (~2 ms) and the deadline ~80 ms
        off, the join waits and the thread is gone by the re-raise. A deadline
        flipped into the past would time out at once and leave it alive -- caught
        here without touching the warning text.
        """
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
        monkeypatch.setattr(shutil, "which", _always_found)
        monkeypatch.setattr(session, "_JOIN_DEADLINE_SEC", 0.08)
        recorded: list[threading.Thread] = []
        monkeypatch.setattr(
            session,
            "_drain_filesystem_loop",
            partial(_sleeping_drain, recorded),
        )
        with pytest.raises(PrivateNotHeldError):
            _ = session._spawn_and_drain(
                replace(RunConfig(cli_name="claude", sync=False), quiesce_seconds=0.0),
                ClaudeAdapter(),
                _RefusingSink(),
                _Stats(),
            )
        assert recorded
        assert not recorded[0].is_alive()
        assert recorded[0].name == "drain"


_CODEX_BANNER = b"Session ID: 01234567-89ab-cdef-0123-456789abcdef\r\n"


class TestSpawnWiring:
    """How ``_spawn_and_drain`` joins the CLI to capture and delivery."""

    def test_a_tui_gets_pastes_and_no_output_tee(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        wiring, status = _wire(
            monkeypatch,
            tmp_path,
            RunConfig(cli_name="claude", sync=False),
            ClaudeAdapter(),
        )
        owner = wiring.drain["owner"]
        assert isinstance(owner, session._SessionOwner)
        assert status == 0
        assert wiring.relay["bracketed_paste"] is True
        assert wiring.relay["on_output"] is None
        assert wiring.relay["on_started"] == owner.started
        assert wiring.relay["host"] is None
        assert wiring.daemons == [True]

    def test_the_child_learns_its_granted_handle_and_a_resume_id_comes_first(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """The server re-attaches a resumed session only if told before ``open``."""
        sink = _GrantingSink(granted="scientist#2")
        wiring, _ = _wire(
            monkeypatch,
            tmp_path,
            RunConfig(cli_name="claude", actor="scientist", cli_session_id="r1"),
            ClaudeAdapter(),
            sink=sink,
        )
        env = cast(dict[str, str], wiring.relay["env"])
        assert env["TRAX_ACTOR"] == "scientist#2"
        assert sink.calls[:2] == ["cli_session_id r1", "open"]

    def test_the_child_is_told_the_workstream_and_station_it_was_placed_in(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
        for argv in (
            ("init", "-q", "-b", "feature"),
            ("remote", "add", "origin", "https://github.com/o/r.git"),
        ):
            assert run_git(["git", "-C", str(tmp_path), *argv], 4096).returncode == 0
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("LC_TRAX_WORKSPACE", str(uuid.UUID(int=5)))
        client = _PlacingClient()
        wiring, _ = _wire(
            monkeypatch,
            tmp_path,
            RunConfig(cli_name="claude", client=cast(Client, client)),
            ClaudeAdapter(),
            sink=_PlacedSink(),
        )
        env = cast(dict[str, str], wiring.relay["env"])
        assert (env["TRAX_WORKSTREAM"], env["TRAX_WORKSPACE"]) == (
            str(uuid.UUID(int=7)),
            str(uuid.UUID(int=8)),
        )
        assert wiring.argv[1:3] == ["--append-system-prompt", _CANVAS_NOTE]
        assert client.asked == [
            (
                f"/api/sessions/{_SESSION}/place",
                {
                    "workspace": str(uuid.UUID(int=5)),
                    "git": {
                        "origin": "https://github.com/o/r.git",
                        "branch": "feature",
                    },
                },
            ),
        ]

    def test_a_refused_placement_warns_once_and_the_run_goes_on_unplaced(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        wiring, status = _wire(
            monkeypatch,
            tmp_path,
            RunConfig(
                cli_name="claude",
                client=cast(Client, _PlacingClient(refuse=True)),
            ),
            ClaudeAdapter(),
            sink=_PlacedSink(),
        )
        env = cast(dict[str, str], wiring.relay["env"])
        assert status == 0
        assert "TRAX_WORKSPACE" not in env
        assert capsys.readouterr().err.count("running unplaced") == 1

    def test_a_local_capture_never_asks_to_be_placed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        client = _PlacingClient()
        _ = _wire(
            monkeypatch,
            tmp_path,
            RunConfig(cli_name="claude", sync=False, client=cast(Client, client)),
            ClaudeAdapter(),
            sink=_PlacedSink(),
        )
        assert client.asked == []

    def test_a_fresh_run_names_no_session_before_open(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A fresh codex run: codex mints its own id after it starts."""
        sink = _GrantingSink(granted="scientist")
        _ = _wire(
            monkeypatch,
            tmp_path,
            RunConfig(cli_name="codex", sync=False),
            CodexAdapter(),
            sink=sink,
        )
        assert sink.calls[0] == "open"

    def test_a_fresh_claude_run_names_its_minted_session_before_open(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """The id claude is handed is the one the server is stamped with at open."""
        sink = _GrantingSink(granted="scientist")
        wiring, _status = _wire(
            monkeypatch,
            tmp_path,
            RunConfig(cli_name="claude", sync=False),
            ClaudeAdapter(),
            sink=sink,
        )
        minted = wiring.argv[wiring.argv.index("--session-id") + 1]
        assert sink.calls[:2] == [f"cli_session_id {minted}", "open"]

    def test_a_detached_run_hands_its_host_to_the_relay(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        host = HostSpec(address=tmp_path / "s", scrollback=tmp_path / "scrollback")
        wiring, _ = _wire(
            monkeypatch,
            tmp_path,
            RunConfig(cli_name="claude", sync=False, host=host),
            ClaudeAdapter(),
        )
        assert wiring.relay["host"] is host

    def test_a_watch_that_never_arms_is_reported_and_the_cli_still_starts(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        wiring = _Wiring()
        monkeypatch.setattr(session, "_ARM_TIMEOUT_SEC", 0.01)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
        monkeypatch.setattr(
            session,
            "ThreadedRelay",
            partial(_WiringRelay, wiring=wiring, rc=0, output=b""),
        )
        monkeypatch.setattr(session, "_drain_filesystem_loop", _late_arming_drain)
        monkeypatch.setattr(shutil, "which", _always_found)
        status = session._spawn_and_drain(
            RunConfig(cli_name="claude", sync=False, quiesce_seconds=0.0),
            ClaudeAdapter(),
            _RecordingSink(),
            _Stats(),
        )
        assert status == 0
        assert wiring.argv[0] == "claude"
        assert (
            "[trax run] session-log watch not ready within 0s; starting the CLI "
            "anyway (early output may not be captured)\n"
        ) in capsys.readouterr().err

    def test_a_stream_run_without_a_command_starts_no_worker(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Checked before the drain starts, a refusal leaves nothing to stop."""
        wiring = _Wiring()
        monkeypatch.setattr(
            session,
            "_drain_filesystem_loop",
            partial(_wiring_drain, wiring),
        )
        with pytest.raises(SystemExit, match="no command given"):
            _ = session._spawn_and_drain(
                RunConfig(cli_name="sh", sync=False),
                IOStreamAdapter(),
                _RecordingSink(),
                _Stats(),
            )
        assert wiring.drain == {}

    def test_a_missing_binary_starts_no_worker_and_makes_no_directory(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        wiring = _Wiring()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
        monkeypatch.setattr(
            session,
            "_drain_filesystem_loop",
            partial(_wiring_drain, wiring),
        )
        monkeypatch.setattr(shutil, "which", _never_found)
        with pytest.raises(SystemExit, match="not found in PATH"):
            _ = session._spawn_and_drain(
                RunConfig(cli_name="claude", sync=False),
                ClaudeAdapter(),
                _RecordingSink(),
                _Stats(),
            )
        assert wiring.drain == {}
        assert not (tmp_path / "claude").exists()

    def test_codex_reads_its_exit_banner_off_the_terminal(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        wiring, status = _wire(
            monkeypatch,
            tmp_path,
            RunConfig(cli_name="codex", sync=False),
            CodexAdapter(),
            output=_CODEX_BANNER,
        )
        owner = wiring.drain["owner"]
        assert isinstance(owner, session._SessionOwner)
        assert status == 0
        assert wiring.relay["on_output"] == owner.output
        assert owner.session_id == "01234567-89ab-cdef-0123-456789abcdef"

    def test_a_failed_codex_run_claims_no_banner(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        wiring, status = _wire(
            monkeypatch,
            tmp_path,
            RunConfig(cli_name="codex", sync=False),
            CodexAdapter(),
            rc=1,
            output=_CODEX_BANNER,
        )
        owner = wiring.drain["owner"]
        assert isinstance(owner, session._SessionOwner)
        assert status == 1
        assert owner.session_id is None

    def test_a_stream_run_gets_lines_rather_than_pastes(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Every output line queues for the drain, the unterminated last one too."""
        wiring, _ = _wire(
            monkeypatch,
            tmp_path,
            RunConfig(cli_name="sh", cli_args=("cat",), sync=False),
            IOStreamAdapter(),
            output=b"one\ntwo",
        )
        assert wiring.argv == ["cat"]
        assert wiring.relay["bracketed_paste"] is False
        assert wiring.relay["on_started"] is None
        assert list(cast(deque[bytes], wiring.drain["stream_queue"])) == [
            b"one\n",
            b"two",
        ]

    def test_typed_slash_commands_queue_for_the_drain(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        wiring, _ = _wire(
            monkeypatch,
            tmp_path,
            RunConfig(cli_name="claude", sync=False),
            ClaudeAdapter(),
        )
        on_input = wiring.relay["on_input"]
        assert callable(on_input)
        _ = on_input(b"/exit\r")
        queued = list(
            cast(deque[tuple[SlashCommand, datetime]], wiring.drain["slash_queue"]),
        )
        assert [command for command, _ in queued] == [SlashCommand(command="exit")]
        assert all(isinstance(at, datetime) for _, at in queued)

    @pytest.mark.parametrize(
        ("cli", "adapter", "stream"),
        [("claude", ClaudeAdapter, False), ("sh", IOStreamAdapter, True)],
    )
    def test_a_synced_run_delivers_inbound_from_a_daemon_thread(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        cli: str,
        adapter: Callable[[], Adapter],
        stream: bool,
    ) -> None:
        client = cast(Client, object())
        sink = _RecordingSink()
        monkeypatch.setattr("socket.gethostname", lambda: "gx10.lan")
        _at_home(monkeypatch, tmp_path.parent)
        monkeypatch.chdir(tmp_path)
        wiring, _ = _wire(
            monkeypatch,
            tmp_path,
            RunConfig(cli_name=cli, cli_args=("cat",), client=client),
            adapter(),
            sink=sink,
        )
        delivered_client, delivered_sink, relay, stop, options = wiring.inbound
        assert delivered_client is client
        assert delivered_sink is sink
        assert relay is wiring.relay_instance
        assert isinstance(stop, threading.Event)
        assert stop.is_set()
        named = dict(cast(dict[str, object], options))
        inbound = named.pop("inbound")
        bridge = named.pop("bridge")
        assert callable(named.pop("record"))
        # A run in this terminal names its machine and folder, and no host; a trusted
        # CLI opens no trust card, and one that loaded nothing compacts nothing.
        assert named == {
            "stream": stream,
            "run_name": None,
            "machine": "gx10",
            "cwd": f"~/{tmp_path.name}",
            "cli": cli,
            "gate": None,
            "compact": None,
        }
        # A synced claude run bridges its prompts; a stream child asks none.
        assert isinstance(bridge, PromptBridge) is not stream
        assert isinstance(wiring.drain["run_records"], deque)
        # One readiness for both threads: the drain sees records, the poller waits.
        assert isinstance(inbound, InboundState)
        assert inbound is wiring.drain["inbound"]
        assert inbound.marker is None if stream else inbound.marker is not None
        assert sorted(wiring.daemons) == [True, True]

    def test_a_local_capture_never_waits_on_the_server(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A client without a server session has nothing to deliver from."""
        wiring, _ = _wire(
            monkeypatch,
            tmp_path,
            RunConfig(
                cli_name="claude",
                client=cast(Client, object()),
                out_path=tmp_path / "local.jsonl",
            ),
            ClaudeAdapter(),
        )
        assert wiring.inbound == []

    def test_a_resumed_transcript_is_not_part_of_the_baseline(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """The file a resume materialized is this run's, not a predecessor's."""
        project = tmp_path / "claude" / "projects" / "-work"
        project.mkdir(parents=True)
        earlier = project / "earlier.jsonl"
        resumed = project / "resumed.jsonl"
        earlier.write_text("{}\n")
        resumed.write_text("{}\n")
        wiring, _ = _wire(
            monkeypatch,
            tmp_path,
            RunConfig(cli_name="claude", sync=False, resume_path=resumed),
            ClaudeAdapter(),
        )
        assert wiring.drain["baseline"] == frozenset({earlier})

    def test_workers_outliving_the_teardown_are_named(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        release = threading.Event()
        monkeypatch.setattr(session, "_JOIN_DEADLINE_SEC", 0.05)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
        monkeypatch.setattr(
            session,
            "ThreadedRelay",
            partial(_WiringRelay, wiring=_Wiring(), rc=0, output=b""),
        )
        monkeypatch.setattr(
            session,
            "_drain_filesystem_loop",
            partial(_held_drain, release),
        )
        monkeypatch.setattr(
            session,
            "_inbound_poll_loop",
            partial(_held_inbound, release),
        )
        monkeypatch.setattr(shutil, "which", _always_found)
        try:
            _ = session._spawn_and_drain(
                RunConfig(
                    cli_name="claude",
                    client=cast(Client, object()),
                    quiesce_seconds=0.0,
                ),
                ClaudeAdapter(),
                _RecordingSink(),
                _Stats(),
            )
        finally:
            release.set()
        err = capsys.readouterr().err
        assert (
            "[trax run] drain thread did not stop before the teardown deadline" in err
        )
        assert "[trax run] inbound poll thread did not stop before the teardown" in err


class _GrantingSink(_RecordingSink):
    """A sink whose server grants ``granted``; records the order it is told things."""

    def __init__(self, *, granted: str) -> None:
        super().__init__()
        self._granted = granted
        self.calls: list[str] = []

    @override
    def set_cli_session_id(self, cli_session_id: str) -> None:
        self.calls.append(f"cli_session_id {cli_session_id}")

    @override
    def open(self) -> str:
        self.calls.append("open")
        return self._granted


class _PlacedSink(_RecordingSink):
    """A sink whose server opened session ``_SESSION``."""

    @property
    @override
    def session_id(self) -> uuid.UUID:
        return _SESSION


_SESSION = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


class _PlacingClient:
    """A client whose server places a session, or refuses to; records what it was asked."""

    def __init__(self, *, refuse: bool = False) -> None:
        self.asked: list[tuple[str, object]] = []
        self._refuse = refuse

    def post(self, path: str, *, body: object = None) -> dict[str, str]:
        self.asked.append((path, body))
        if self._refuse:
            raise ClientError("404 Not Found")
        return {
            "workstream_id": str(uuid.UUID(int=7)),
            "workspace_id": str(uuid.UUID(int=8)),
        }


def _never_found(
    cmd: str,
    mode: int = os.F_OK | os.X_OK,
    path: str | None = None,
) -> None:
    """Return a ``shutil.which`` that resolves nothing."""
    del cmd, mode, path


def _late_arming_drain(*args: object, armed: threading.Event, **kwargs: object) -> None:
    """Arm only after the spawn has stopped waiting for it."""
    del args, kwargs
    time.sleep(0.2)
    armed.set()


def _held_drain(
    release: threading.Event,
    *args: object,
    armed: threading.Event,
    **kwargs: object,
) -> None:
    """Arm, then stay alive past any teardown until ``release``."""
    del args, kwargs
    armed.set()
    _ = release.wait(5.0)


def _held_inbound(release: threading.Event, *args: object, **kwargs: object) -> None:
    """Stay alive past any teardown until ``release``."""
    del args, kwargs
    _ = release.wait(5.0)


class TestTeardownRunsEvenWhenTheRelayRaises:
    """A relay failure must still stop the workers before the sink closes.

    ``run``'s ``finally`` closes the sink unconditionally. Without a matching
    guard in ``_spawn_and_drain``, a raise from ``relay.run`` skips
    ``stop.set()`` and both joins, so the daemon drain is still emitting into
    a sink being torn down -- the race the joins and ``LockedSink`` exist for.
    """

    def test_a_raising_relay_still_stops_the_drain(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _ExplodingRelay:
            def __init__(self, argv: object, **kwargs: object) -> None:
                del argv, kwargs

            def run(self) -> int:
                raise RuntimeError("pty allocation failed")

        # The drain thread observes ``stop`` and exits; that it observed the
        # set at all is the assertion. A real drain would need session files.
        observed: list[bool] = []

        def watching_drain(*args: object, **kwargs: object) -> None:
            armed = kwargs["armed"]
            assert isinstance(armed, threading.Event)
            armed.set()  # Release the spawn, as a real armed watch would.
            stop = args[4]
            assert isinstance(stop, threading.Event)
            observed.append(stop.wait(timeout=5.0))

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(session, "ThreadedRelay", _ExplodingRelay)
        monkeypatch.setattr(shutil, "which", _always_found)
        monkeypatch.setattr(session, "_drain_filesystem_loop", watching_drain)

        with pytest.raises(RuntimeError, match="pty allocation"):
            _ = session._spawn_and_drain(
                RunConfig(cli_name="claude", quiesce_seconds=0.0),
                ClaudeAdapter(),
                _RecordingSink(),
                _Stats(),
            )

        assert observed == [True], (
            "the relay raised and the drain was never told to stop"
        )


class TestExitDoesNotWaitOutTheInboundHold:
    """A run's exit ends the held request its inbound poller is parked in.

    The server holds that request until a message arrives or the session ends,
    and closing the sink is what ends the session -- so a teardown that joined
    the poller before closing waited out the whole hold on every exit.
    """

    def test_the_session_ends_while_the_poller_is_still_parked(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        server = _HoldingServer()
        client = cast(Client, server)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
        monkeypatch.setattr(session, "ThreadedRelay", partial(_ExitsOnceParked, server))
        monkeypatch.setattr(session, "_drain_filesystem_loop", _drain_until_stop)
        # No claude starts, so no hook says it is ready: wait out no fallback.
        monkeypatch.setattr(session, "_HOOK_READY_SEC", 0.0)
        monkeypatch.setattr(shutil, "which", _always_found)

        status = session._spawn_and_drain(
            RunConfig(cli_name="claude", client=client, quiesce_seconds=0.0),
            ClaudeAdapter(),
            LockedSink(TrackinizerSink(client, "claude")),
            _Stats(),
        )

        assert status == 0
        assert server.holds == ["ended"], "the hold ran out instead of being ended"
        assert "inbound poll thread did not stop" not in capsys.readouterr().err


class _HoldingServer:
    """Holds each inbound wait until the session ends, as the server route does."""

    def __init__(self) -> None:
        self.session = uuid.uuid4()
        self.parked = threading.Event()
        self.ended = threading.Event()
        self.holds: list[str] = []

    def session_start(self, body: SessionStart) -> SessionStartResponse:
        return SessionStartResponse(id=self.session, seq=0, actor=body.actor)

    def post(self, path: str, *, body: object = None) -> dict[str, str]:
        del path, body
        return {}

    def session_end(
        self,
        session_id: uuid.UUID,
        body: SessionEnd | None = None,
    ) -> SessionEndResponse:
        del body
        self.ended.set()
        return SessionEndResponse(id=session_id)

    def drain_inbound(
        self,
        session_id: uuid.UUID,
        *,
        wait_sec: float = 0.0,
        **where: str | None,
    ) -> list[InboundDrainItem]:
        del where
        del session_id
        self.parked.set()
        self.holds.append("ended" if self.ended.wait(wait_sec) else "timed out")
        return []


class _ExitsOnceParked:
    """A CLI that exits once inbound delivery is parked in its held request."""

    def __init__(self, server: _HoldingServer, argv: object, **kwargs: object) -> None:
        del argv, kwargs
        self._server = server

    def run(self) -> int:
        assert self._server.parked.wait(5.0), "inbound delivery never parked"
        return 0


def _drain_until_stop(*args: object, armed: threading.Event, **kwargs: object) -> None:
    """Arm, then capture nothing until the teardown stops the drain."""
    del kwargs
    armed.set()
    stop = args[4]
    assert isinstance(stop, threading.Event)
    _ = stop.wait(5.0)


class TestInboundIsWaitDriven:
    """Inbound delivery waits on the server, rather than asking repeatedly.

    A poller costs one request per session per interval whether or not
    anything was sent, and delivers up to an interval late. A held request
    costs one connection and delivers on arrival.
    """

    def test_asks_the_server_to_hold_the_request(self) -> None:
        """Every drain must carry a non-zero hold, or it is still a poll."""
        client = _WaitingClient(hold_sec=0.05)
        stop = threading.Event()

        worker = threading.Thread(
            target=lambda: _inbound_poll_loop(
                cast(Client, client),
                cast(Sink, _SessionSink()),
                cast(ThreadedRelay, _RecordingRelay()),
                stop,
            ),
            daemon=True,
        )
        worker.start()
        try:
            deadline = time.monotonic() + 3.0
            while not client.waits and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            stop.set()
            worker.join(timeout=5.0)

        assert client.waits, "inbound never called the server"
        assert all(w > 0 for w in client.waits), (
            f"drained without asking the server to wait: {client.waits}"
        )

    def test_does_not_sleep_between_successful_waits(self) -> None:
        """Re-arming must be immediate: the request itself was the wait.

        A sleep after a successful hold adds latency on top of a mechanism
        whose whole purpose is to remove it.
        """
        client = _WaitingClient(hold_sec=0.02)
        stop = threading.Event()
        drain_thread: list[int] = []
        slept: list[float] = []
        real_sleep = time.sleep

        def watched_sleep(seconds: float) -> None:
            if threading.get_ident() in drain_thread:
                slept.append(seconds)
            real_sleep(seconds)

        def _run() -> None:
            drain_thread.append(threading.get_ident())
            _inbound_poll_loop(
                cast(Client, client),
                cast(Sink, _SessionSink()),
                cast(ThreadedRelay, _RecordingRelay()),
                stop,
            )

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(time, "sleep", watched_sleep)
            worker = threading.Thread(target=_run, daemon=True)
            worker.start()
            try:
                deadline = time.monotonic() + 3.0
                while len(client.waits) < 3 and time.monotonic() < deadline:
                    real_sleep(0.01)
            finally:
                stop.set()
                worker.join(timeout=5.0)

        assert len(client.waits) >= 3, "did not re-arm after a successful wait"
        assert slept == [], f"slept between waits: {slept}"

    def test_a_failure_backs_off_before_re_arming(self) -> None:
        """A persistent outage must not become a hot retry loop.

        Asserted on the wait the loop ASKS for, not on how many attempts fit
        in a wall-clock window: the stop event records each timed wait and
        shortens it, so a removed backoff shows as attempts with no wait
        between them rather than as a count that depends on the host's load.
        """
        client = _FailingClient()
        stop = _RecordingStop()

        worker = threading.Thread(
            target=lambda: _inbound_poll_loop(
                cast(Client, client),
                cast(Sink, _SessionSink()),
                cast(ThreadedRelay, _RecordingRelay()),
                stop,
                poll_interval=0.05,
            ),
            daemon=True,
        )
        worker.start()
        try:
            deadline = time.monotonic() + 3.0
            while client.attempts < 3 and time.monotonic() < deadline:
                time.sleep(0.005)
        finally:
            stop.set()
            worker.join(timeout=5.0)

        assert client.attempts >= 3, "never re-armed after a failure"
        # One backoff per failure; only the last may be missing, cut off by
        # ``stop`` before the loop reached it.
        assert len(stop.timeouts) >= client.attempts - 1, (
            f"retried {client.attempts} times with {len(stop.timeouts)} backoffs"
        )
        assert set(stop.timeouts) == {0.05}, f"backed off by {stop.timeouts}"


class TestInboundBatchSurvivesOneBadMessage:
    """One undeliverable message must not discard the rest of its batch.

    ``drain_inbound`` CONSUMES server-side: the messages it returns are gone
    from the queue. A submit that raises partway then aborts the loop with the
    remaining messages already dequeued and never typed -- silently dropped
    for good, since no later drain will return them.
    """

    def test_a_failed_submit_still_delivers_the_rest(self) -> None:
        client = _BatchClient(["first", "poison", "third"])
        relay = _PickyRelay(reject="poison")
        stop = threading.Event()

        worker = threading.Thread(
            target=lambda: _inbound_poll_loop(
                cast(Client, client),
                cast(Sink, _SessionSink()),
                cast(ThreadedRelay, relay),
                stop,
                poll_interval=0.01,
            ),
            daemon=True,
        )
        worker.start()
        try:
            deadline = time.monotonic() + 3.0
            while len(relay.submitted) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            stop.set()
            worker.join(timeout=5.0)

        assert relay.submitted[:2] == ["first", "third"], (
            f"a bad message took the rest of its batch with it: {relay.submitted}"
        )


class TestDeliverOne:
    """One drained message reaches the CLI with all of its routing, or is logged."""

    def test_a_stream_child_gets_the_whole_routed_envelope(self) -> None:
        relay = _RecordingRelay()
        envelope = '{"agent_message": "go"}'
        session._deliver_one(
            cast(ThreadedRelay, relay),
            InboundDrainItem(text=envelope, source="trackinizer", room="lab"),
            stream=True,
            cli="sh",
        )
        assert relay.submitted == [f"[lab] trackinizer: {envelope}"]

    def test_workspace_context_rides_with_the_text(self) -> None:
        relay = _RecordingRelay()
        context = WorkspaceMessageContext(workspace_id=uuid.uuid4(), visible_visuals=[])
        session._deliver_one(
            cast(ThreadedRelay, relay),
            InboundDrainItem(text="look", source="alice@x", context=context),
            stream=False,
            cli="claude",
        )
        assert relay.submitted == [
            render_inbound("look", "alice@x", None, context=context),
        ]
        assert "Trackinizer context" in relay.submitted[0]

    def test_each_kind_is_delivered_the_way_the_cli_acts_on_it(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        relay = _RecordingRelay()
        items = [
            InboundDrainItem(text="/compact", kind="command", source="ann@x"),
            InboundDrainItem(
                text="Yes",
                kind="answer",
                payload={"prompt_id": "p9"},
                source="ann@x",
            ),
            InboundDrainItem(text="interrupt", kind="interrupt", source="ann@x"),
            InboundDrainItem(
                text="model",
                kind="settings",
                payload={"model": "opus"},
                source="ann@x",
            ),
        ]

        with caplog.at_level("INFO", logger=session.__name__):
            for item in items:
                session._deliver_one(
                    cast(ThreadedRelay, relay),
                    item,
                    stream=False,
                    cli="claude",
                )

        assert relay.submitted == ["/compact"]
        assert relay.written == [b"\x1b"]
        assert [
            r.getMessage() for r in caplog.records if r.name == session.__name__
        ] == [
            "trax run: an answer names no open prompt: {'prompt_id': 'p9'}",
            "trax run: a settings message is not typed: {'model': 'opus'}",
        ]

    def test_an_undeliverable_message_is_logged_with_its_cause(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The sender believes it was delivered, so the loss must be traceable."""
        with caplog.at_level("WARNING", logger=session.__name__):
            session._deliver_one(
                cast(ThreadedRelay, _PickyRelay(reject="poison")),
                InboundDrainItem(text="poison"),
                stream=False,
                cli="claude",
            )
        (record,) = [r for r in caplog.records if r.name == session.__name__]
        assert record.getMessage() == (
            "trax run: could not deliver an inbound message; "
            "continuing with the rest of the batch"
        )
        assert isinstance(record.exc_info, tuple)
        assert isinstance(record.exc_info[1], RuntimeError)


class TestInboundSurvivesACaptureDegrade:
    """A failed upload moves capture to a local file; the agent must still hear.

    Inbound needs only the server session id, which a degrade used to drop: the
    poller then idled for the rest of the run while the server, still seeing a
    live session, told every sender its message was delivered.
    """

    def test_a_degraded_run_still_receives_inbound(self, tmp_path: Path) -> None:
        client = _UploadOutageClient(["still listening"])
        fallback_path = tmp_path / "fallback.jsonl"
        sink = ResilientSink(
            TrackinizerSink(cast(Client, client), "claude", flush_interval_sec=0.0),
            fallback_path=fallback_path,
        )
        sink.emit(
            "claude",
            Event(record=UserMessage(content="hi"), path=tmp_path / "s.jsonl"),
        )
        sink.flush()
        relay = _RecordingRelay()
        stop = threading.Event()

        worker = threading.Thread(
            target=lambda: _inbound_poll_loop(
                cast(Client, client),
                sink,
                cast(ThreadedRelay, relay),
                stop,
                poll_interval=0.01,
            ),
            daemon=True,
        )
        worker.start()
        try:
            deadline = time.monotonic() + 3.0
            while not relay.submitted and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            stop.set()
            worker.join(timeout=5.0)

        assert '"hi"' in fallback_path.read_text(), "capture never degraded"
        assert relay.submitted == ["still listening"]
        assert client.drained_for == {client.session}


class TestInboundAfterALateOpen:
    """A run started while the server was down hears once its session opens."""

    def test_messages_arrive_once_the_server_comes_back(self, tmp_path: Path) -> None:
        client = _ServerComesBackClient(["now you hear me"])
        now = [0.0]
        sink = ResilientSink(
            TrackinizerSink(
                cast(Client, client),
                cli="claude",
                flush_interval_sec=0.0,
            ),
            fallback_path=tmp_path / "fallback.jsonl",
            retry_sec=30.0,
            clock=lambda: now[0],
        )
        assert sink.open() is None
        sink.emit(
            "claude",
            Event(record=UserMessage(content="hi"), path=tmp_path / "s.jsonl"),
        )
        relay = _RecordingRelay()
        stop = threading.Event()
        worker = threading.Thread(
            target=lambda: _inbound_poll_loop(
                cast(Client, client),
                sink,
                cast(ThreadedRelay, relay),
                stop,
                poll_interval=0.01,
            ),
            daemon=True,
        )
        worker.start()
        try:
            _real_pause(0.05)
            assert client.drained_for == set(), "polled a session that never opened"
            client.up = True
            now[0] = 30.0
            sink.flush()
            deadline = time.monotonic() + 3.0
            while not relay.submitted and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            stop.set()
            worker.join(timeout=5.0)

        assert relay.submitted == ["now you hear me"]
        assert client.drained_for == {client.session}


class _BatchClient:
    """Returns one batch of messages, then nothing (the queue is drained)."""

    def __init__(self, texts: list[str]) -> None:
        self._texts = texts
        self.drains = 0

    def drain_inbound(
        self,
        session_id: uuid.UUID,
        *,
        wait_sec: float = 0.0,
        **where: str | None,
    ) -> list[InboundDrainItem]:
        del where
        del session_id, wait_sec
        self.drains += 1
        if self.drains > 1:
            _real_pause(0.02)
            return []
        return [InboundDrainItem(text=text) for text in self._texts]


class _UploadOutageClient(_BatchClient):
    """Opens a session and serves its inbound queue, but every upload fails."""

    def __init__(self, texts: list[str]) -> None:
        super().__init__(texts)
        self.session = uuid.uuid4()
        self.drained_for: set[uuid.UUID] = set()

    def session_start(self, body: SessionStart) -> SessionStartResponse:
        return SessionStartResponse(id=self.session, seq=0, actor=body.actor)

    def append_records(
        self,
        session_id: uuid.UUID,
        **fields: object,
    ) -> AppendRecordsResponse:
        del session_id, fields
        raise RuntimeError("upload failed")

    @override
    def drain_inbound(
        self,
        session_id: uuid.UUID,
        *,
        wait_sec: float = 0.0,
        **where: str | None,
    ) -> list[InboundDrainItem]:
        del where
        self.drained_for.add(session_id)
        return super().drain_inbound(session_id, wait_sec=wait_sec)


class _ServerComesBackClient(_UploadOutageClient):
    """A server that refuses everything until ``up``, then accepts uploads too."""

    def __init__(self, texts: list[str]) -> None:
        super().__init__(texts)
        self.up = False

    @override
    def session_start(self, body: SessionStart) -> SessionStartResponse:
        if not self.up:
            raise RuntimeError("server down")
        return super().session_start(body)

    @override
    def append_records(
        self,
        session_id: uuid.UUID,
        **fields: object,
    ) -> AppendRecordsResponse:
        if not self.up:
            raise RuntimeError("server down")
        del fields
        return AppendRecordsResponse(part=0, written=1, skipped=0)


class _SessionSink:
    """A sink whose session is already open, so inbound has an id to use."""

    session_id = uuid.UUID("11111111-2222-3333-4444-555555555555")


class _RecordingRelay:
    """Records what would have been typed into the CLI."""

    def __init__(self) -> None:
        self.submitted: list[str] = []
        self.written: list[bytes] = []
        self.terminated = 0

    def submit(self, text: str) -> None:
        self.submitted.append(text)

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def terminate(self) -> None:
        self.terminated += 1


class _PromptWatchingRelay(_RecordingRelay):
    """Notes, for each line typed, whether a prompt was open to swallow it."""

    def __init__(self, pending: threading.Event) -> None:
        super().__init__()
        self._pending = pending
        self.typed_into_a_prompt: list[bool] = []

    @override
    def submit(self, text: str) -> None:
        self.typed_into_a_prompt.append(self._pending.is_set())
        super().submit(text)


class _PickyRelay(_RecordingRelay):
    """Rejects one message, as a wedged PTY write would."""

    def __init__(self, *, reject: str) -> None:
        super().__init__()
        self._reject = reject

    @override
    def submit(self, text: str) -> None:
        if text == self._reject:
            raise RuntimeError("pty write failed")
        super().submit(text)


def _real_pause(seconds: float) -> None:
    """Block without ``time.sleep``, so a sleep assertion stays meaningful."""
    threading.Event().wait(seconds)


class _WaitingClient:
    """A client whose drain holds, as the real long-poll route does."""

    def __init__(self, *, hold_sec: float) -> None:
        self._hold_sec = hold_sec
        self.waits: list[float] = []

    def drain_inbound(
        self,
        session_id: uuid.UUID,
        *,
        wait_sec: float = 0.0,
        **where: str | None,
    ) -> list[InboundDrainItem]:
        del where
        del session_id
        self.waits.append(wait_sec)
        # A real hold blocks in the transport, not in ``time.sleep``: a fake
        # that slept here would be indistinguishable from the loop sleeping,
        # which is the very thing the caller asserts about.
        _real_pause(self._hold_sec)
        return []


class _FailingClient:
    """A client whose drain always raises, to drive the backoff path."""

    def __init__(self) -> None:
        self.attempts = 0

    def drain_inbound(
        self,
        session_id: uuid.UUID,
        *,
        wait_sec: float = 0.0,
        **where: str | None,
    ) -> list[InboundDrainItem]:
        del where
        del session_id, wait_sec
        self.attempts += 1
        raise RuntimeError("back-channel down")


class TestResumeArgv:
    """Each CLI spells "continue this session" its own way."""

    def test_claude_takes_a_resume_flag(self) -> None:
        """``claude --resume <uuid>``, verified against ``claude --help``."""
        assert resume_argv("claude", "abc-123") == ("--resume", "abc-123")

    def test_codex_takes_a_resume_subcommand(self) -> None:
        """``codex resume <SESSION_ID>`` -- a SUBCOMMAND, not a flag.

        Verified against the installed CLI: ``codex resume --help`` reads
        "Usage: codex resume [OPTIONS] [SESSION_ID]". Passing claude's
        ``--resume`` spelling makes codex reject the argument outright, so a
        resume that materialized a perfectly good rollout still fails to
        start.
        """
        assert resume_argv("codex", "abc-123") == ("resume", "abc-123")

    def test_no_id_forwards_nothing(self) -> None:
        """A fresh run names no session, so it gets no resume tokens."""
        assert resume_argv("claude", None) == ()
        assert resume_argv("codex", None) == ()


class _UploadsClient:
    """A server that takes every upload, keeping each body it was sent as JSON."""

    base_url = "http://uploads.test"

    def __init__(self) -> None:
        self.sent: list[str] = []

    def session_start(self, body: SessionStart) -> SessionStartResponse:
        return SessionStartResponse(id=uuid.uuid4(), seq=0, actor=body.actor)

    def append_records(
        self,
        session_id: uuid.UUID,
        **fields: object,
    ) -> AppendRecordsResponse:
        del session_id
        records = cast(list[RecordBody], fields["records"])
        self.sent.extend(body.model_dump_json() for body in records)
        slash = cast(list[SlashCommandBody], fields["slash_commands"])
        self.sent.extend(body.model_dump_json() for body in slash)
        return AppendRecordsResponse(part=0, written=len(records), skipped=0)

    def session_end(
        self,
        session_id: uuid.UUID,
        body: SessionEnd | None = None,
    ) -> SessionEndResponse:
        del body
        return SessionEndResponse(id=session_id)


class _DownClient(_UploadsClient):
    """A server that refuses the session, so the run captures to its local file."""

    @override
    def session_start(self, body: SessionStart) -> SessionStartResponse:
        raise RuntimeError("server down")


class TestOpenSinkRedactsDeliveredSecrets:
    """Every sink ``_open_sink`` builds masks the values ``TRAX_REDACT_NAMES`` names."""

    @pytest.fixture(autouse=True)
    def _delivered_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRAX_REDACT_NAMES", "K")
        monkeypatch.setenv("K", "secretvalue1")

    def test_the_out_file_holds_the_placeholder_only(self, tmp_path: Path) -> None:
        out = tmp_path / "out.jsonl"
        config = RunConfig(cli_name="sh", out_path=out)
        self._capture(_open_sink(config, IOStreamAdapter()), tmp_path / "a.log")
        stored = out.read_text(encoding="utf-8")
        assert "[redacted:K]" in stored
        assert "secretvalue1" not in stored

    def test_the_server_and_its_fallback_file_hold_the_placeholder_only(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fallback = tmp_path / "fallback.jsonl"

        def default_out_path(adapter_name: str) -> Path:
            del adapter_name
            return fallback

        monkeypatch.setattr(session, "_default_out_path", default_out_path)
        up = _UploadsClient()
        config = RunConfig(cli_name="sh", client=cast(Client, up))
        self._capture(_open_sink(config, IOStreamAdapter()), tmp_path / "a.log")
        assert not fallback.exists()
        assert up.sent
        assert "secretvalue1" not in "".join(up.sent)
        assert "[redacted:K]" in "".join(up.sent)

        down = RunConfig(cli_name="sh", client=cast(Client, _DownClient()))
        self._capture(_open_sink(down, IOStreamAdapter()), tmp_path / "b.log")
        stored = fallback.read_text(encoding="utf-8")
        assert "[redacted:K]" in stored
        assert "secretvalue1" not in stored

    def test_a_named_variable_the_environment_lacks_stops_the_run(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("K")
        out = tmp_path / "out.jsonl"
        with pytest.raises(SystemExit, match="K"):
            _ = _open_sink(RunConfig(cli_name="sh", out_path=out), IOStreamAdapter())
        assert not out.exists()

    @classmethod
    def _capture(cls, sink: Sink, path: Path) -> None:
        # Opening first makes a server that refuses the session degrade before the
        # first record, so the fallback file records everything itself.
        _ = sink.open()
        _ = sink.feed(IOStreamAdapter(), path, b"echo secretvalue1\n")
        sink.emit_slash_command(
            SlashCommand(command="secretvalue1", args="x"),
            datetime(2026, 6, 1, tzinfo=UTC),
        )
        for reader in sink.readers.values():
            reader.close()
        sink.close()


_CHAT: Final = uuid.UUID("0a1b2c3d-0000-4000-8000-0000000000aa")


class TestAttach:
    """``--attach`` writes the run into an existing session, such as a chat's."""

    def test_the_flag_reaches_the_run_and_a_detached_host(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ran: list[RunConfig] = []
        hosted: list[Callable[[HostSpec | None], int]] = []
        monkeypatch.setattr(session, "run", partial(_keep_run, ran))
        monkeypatch.setattr(detach, "launch", partial(_keep_host, hosted))

        assert session.main(["--attach", str(_CHAT), "claude"]) == 0
        assert session.main(["--detach", "--attach", str(_CHAT), "codex"]) == 0
        for start in hosted:
            assert start(None) == 0

        assert [(config.cli_name, config.attach) for config in ran] == [
            ("claude", _CHAT),
            ("codex", _CHAT),
        ]

    def test_a_resume_hands_the_run_its_session_and_loaded_history(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        ran: list[RunConfig] = []
        monkeypatch.setattr(session, "run", partial(_keep_run, ran))
        resumed = tmp_path / f"{_RESUMED}.jsonl"

        status = session.main(
            ["claude"],
            resume_path=resumed,
            cli_session_id=_RESUMED,
            attach=_CHAT,
            prefix_bytes=12,
        )

        assert status == 0
        [config] = ran
        assert (config.attach, config.prefix_bytes, config.resume_path) == (
            _CHAT,
            12,
            resumed,
        )

    @pytest.mark.parametrize("local", ["--no-sync", "--out=capture.jsonl"])
    def test_an_attached_run_must_sync(
        self,
        local: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with pytest.raises(SystemExit):
            _ = session.main(["--attach", str(_CHAT), local, "claude"])

        assert "--attach writes into a server session" in capsys.readouterr().err

    @pytest.mark.parametrize("local", ["--no-sync", "--out=capture.jsonl"])
    def test_a_resume_may_still_run_without_syncing(
        self,
        local: str,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """``agentsession N run claude --no-sync`` names no ``--attach`` of its own."""
        ran: list[RunConfig] = []
        monkeypatch.setattr(session, "run", partial(_keep_run, ran))

        status = session.main(
            [local, "claude"],
            resume_path=tmp_path / f"{_RESUMED}.jsonl",
            cli_session_id=_RESUMED,
            attach=_CHAT,
            prefix_bytes=12,
        )

        assert status == 0
        [config] = ran
        assert (config.syncing, config.prefix_bytes) == (False, 12)


class TestLoadedHistoryIsNotUploaded:
    """A resume's loaded history is read but never uploaded; positions run on."""

    def test_no_record_of_the_history_is_uploaded_and_the_first_sits_at_k(
        self,
        tmp_path: Path,
    ) -> None:
        resumed = tmp_path / "resumed.jsonl"
        history = [b"one", b"two", b"", b"three"]
        prefix = session._Prefix(
            path=resumed,
            size=sum(len(line) + 1 for line in history),
        )
        sink = _RecordingSink()
        adapter = _FakeAdapter(tmp_path)

        for raw in [*history, b"mine"]:
            session._emit_line(
                adapter,
                sink,
                _Stats(),
                RunConfig(cli_name="fake"),
                session._Captured(path=resumed, raw=raw),
                prefix=prefix,
            )
        session._emit_line(
            adapter,
            sink,
            _Stats(),
            RunConfig(cli_name="fake"),
            session._Captured(path=tmp_path / "other.jsonl", raw=b"elsewhere"),
            prefix=prefix,
        )

        assert [(idx, event.path.name) for idx, _, event in sink.events] == [
            (3, "resumed.jsonl"),
            (0, "other.jsonl"),
        ]
        assert _texts(sink) == ["mine", "elsewhere"]

    def test_a_rewrite_reads_the_history_again_from_its_start(self) -> None:
        path = Path("/sessions/s.jsonl")
        prefix = session._Prefix(path=path, size=4)

        covered = [
            prefix.covers(session._Captured(path=path, raw=b"abc")),
            prefix.covers(session._Captured(path=path, raw=b"new")),
            prefix.covers(session._Captured(path=path, raw=b"abc", restart=True)),
        ]

        assert covered == [True, False, True]

    def test_the_drain_skips_the_history_a_resume_config_names(
        self,
        tmp_path: Path,
    ) -> None:
        resumed = tmp_path / "resumed.jsonl"
        history = "loaded one\nloaded two\n"
        _ = resumed.write_text(history)
        config = RunConfig(
            cli_name="fake",
            resume_path=resumed,
            prefix_bytes=len(history.encode()),
        )
        sink = _RecordingSink()
        stop = threading.Event()
        armed = threading.Event()
        worker = threading.Thread(
            target=lambda: _drain_filesystem_loop(
                _FakeAdapter(tmp_path),
                sink,
                _Stats(),
                config,
                stop,
                baseline=frozenset(),
                slash_queue=deque(),
                armed=armed,
            ),
            daemon=True,
        )
        worker.start()
        try:
            assert armed.wait(5.0), "session-log watch did not arm"
            with resumed.open("a") as log:
                _ = log.write("the cli wrote this\n")
            _wait_for_events(sink, 1)
        finally:
            stop.set()
            worker.join(timeout=5.0)

        assert [idx for idx, _, _ in sink.events] == [2]
        assert _texts(sink) == ["the cli wrote this"]


class TestInboundReadiness:
    """Nothing is drained until the CLI can take a typed line."""

    def test_claude_is_ready_once_its_hook_writes_the_marker(
        self,
        tmp_path: Path,
    ) -> None:
        marker = tmp_path / "ready"
        state = InboundState(marker=marker, fallback_sec=20.0, clock=lambda: 0.0)

        before = state.ready()
        state.record_seen()
        after_a_record = state.ready()
        marker.touch()

        assert (before, after_a_record, state.ready()) == (False, False, True)

    def test_a_cli_without_a_hook_is_ready_on_its_first_record(self) -> None:
        state = InboundState(by_record=True, fallback_sec=15.0, clock=lambda: 0.0)

        before = state.ready()
        state.record_seen()

        assert (before, state.ready()) == (False, True)

    def test_a_cli_that_never_says_is_ready_after_the_fallback(self) -> None:
        now = [0.0]
        state = InboundState(fallback_sec=20.0, clock=lambda: now[0])

        early = state.ready()
        now[0] = 20.0

        assert (early, state.ready()) == (False, True)

    def test_a_pending_prompt_leaves_the_drain_open(self) -> None:
        """Its answer comes through the drain; the poller holds the lines instead."""
        state = InboundState(fallback_sec=0.0)
        state.prompt_pending.set()

        assert state.wait_open(_RecordingStop(), poll_sec=0.5)

    def test_the_drain_waits_for_the_marker(self, tmp_path: Path) -> None:
        marker = tmp_path / "ready"
        state = InboundState(marker=marker, fallback_sec=20.0, clock=lambda: 0.0)
        stop = _StopThatReadies(marker)
        client = _MarkerCheckingClient(marker, stop)

        _inbound_poll_loop(
            cast(Client, client),
            cast(Sink, _SessionSink()),
            cast(ThreadedRelay, _RecordingRelay()),
            stop,
            poll_interval=0.5,
            inbound=state,
        )

        assert stop.waits == 1
        assert client.marked == [True]

    def test_the_drain_starts_after_the_fallback(self, tmp_path: Path) -> None:
        now = [0.0]
        state = InboundState(
            marker=tmp_path / "never",
            fallback_sec=20.0,
            clock=lambda: now[0],
        )
        stop = _StopThatAdvances(now, to=20.0)
        client = _MarkerCheckingClient(tmp_path / "never", stop)

        _inbound_poll_loop(
            cast(Client, client),
            cast(Sink, _SessionSink()),
            cast(ThreadedRelay, _RecordingRelay()),
            stop,
            poll_interval=0.5,
            inbound=state,
        )

        assert stop.waits == 1
        assert client.marked == [False]

    @pytest.mark.parametrize(
        ("adapter", "by_record", "fallback_sec"),
        [
            (ClaudeAdapter(), False, 20.0),
            (CodexAdapter(), True, 15.0),
            (IOStreamAdapter(), False, 0.0),
        ],
    )
    def test_each_cli_says_it_is_ready_its_own_way(
        self,
        adapter: Adapter,
        *,
        by_record: bool,
        fallback_sec: float,
    ) -> None:
        state = session._inbound_state(adapter)

        assert (state.by_record, state.fallback_sec) == (by_record, fallback_sec)


class TestDeliveredLinesAreStamped:
    """The record of a line the run typed names who sent it; a look-alike does not."""

    def test_a_delivered_line_is_stamped_and_a_look_alike_is_not(
        self,
        tmp_path: Path,
    ) -> None:
        state = InboundState(fallback_sec=0.0)
        relay = _RecordingRelay()
        session._deliver_one(
            cast(ThreadedRelay, relay),
            InboundDrainItem(text="hello\n  there", source="alice@x", room="lab"),
            stream=False,
            cli="claude",
            inbound=state,
        )
        sink = _RecordingSink()
        # The CLI's record of the paste, its whitespace changed; a look-alike typed
        # at the terminal; and the same text typed again by the person.
        recorded = [
            "[lab] alice@x: hello there",
            "bob@x: fake",
            "[lab] alice@x: hello there",
        ]

        for text in recorded:
            session._process_chunk(
                session._Captured(path=tmp_path / "s.jsonl", raw=text.encode()),
                _FakeAdapter(tmp_path),
                sink,
                _Stats(),
                RunConfig(cli_name="fake"),
                inbound=state,
            )

        assert relay.submitted == ["[lab] alice@x: hello\n  there"]
        assert _stamps(sink) == [
            {"source": "alice@x", "room": "lab", "kind": "line"},
            None,
            None,
        ]

    def test_a_line_that_was_not_typed_is_forgotten(self) -> None:
        state = InboundState(fallback_sec=0.0)
        session._deliver_one(
            cast(ThreadedRelay, _PickyRelay(reject="alice@x: poison")),
            InboundDrainItem(text="poison", source="alice@x"),
            stream=False,
            cli="claude",
            inbound=state,
        )

        record = UserMessage(content="alice@x: poison")

        assert state.stamp(record) is record

    def test_a_record_that_is_not_a_users_line_is_left_alone(self) -> None:
        state = InboundState(fallback_sec=0.0)
        state.typed("hi", source="a@x", room=None, kind="line")
        record = AssistantMessage(content="hi")

        assert state.stamp(record) is record


def _stamps(sink: _RecordingSink) -> list[PlainTree]:
    """Return each captured user line's ``$inbound`` stamp, ``None`` when unstamped."""
    return [
        event.record.extra.get("$inbound")
        for _, _, event in sink.events
        if isinstance(event.record, UserMessage)
    ]


class _StopThatReadies(threading.Event):
    """A stop whose first wait is when claude's hook writes its marker."""

    def __init__(self, marker: Path) -> None:
        super().__init__()
        self._marker = marker
        self.waits = 0

    @override
    def wait(self, timeout: float | None = None) -> bool:
        del timeout
        self.waits += 1
        self._marker.touch()
        return self.is_set()


class _StopThatAdvances(threading.Event):
    """A stop whose first wait outlasts the readiness fallback."""

    def __init__(self, now: list[float], *, to: float) -> None:
        super().__init__()
        self._now = now
        self._to = to
        self.waits = 0

    @override
    def wait(self, timeout: float | None = None) -> bool:
        del timeout
        self.waits += 1
        self._now[0] = self._to
        return self.is_set()


class _MarkerCheckingClient:
    """Says, at each drain, whether the marker existed; the first drain ends the run."""

    def __init__(self, marker: Path, stop: threading.Event) -> None:
        self._marker = marker
        self._stop = stop
        self.marked: list[bool] = []

    def drain_inbound(
        self,
        session_id: uuid.UUID,
        *,
        wait_sec: float = 0.0,
        **where: str | None,
    ) -> list[InboundDrainItem]:
        del where
        del session_id, wait_sec
        self.marked.append(self._marker.exists())
        self._stop.set()
        return []


class TestPromptBridge:
    """Claude's prompts reach Chat through the bridge; Chat's answers never get typed."""

    def test_a_synced_claude_run_hands_its_prompts_to_a_socket(self) -> None:
        argv, files = _with_box_hook(
            ["claude"],
            adapter=ClaudeAdapter(),
            placed={},
            profile=None,
            bridge=True,
        )

        assert files is not None
        assert files.socket is not None
        assert argv == ["claude", "--settings", str(files.settings)]
        hooks = json_loads(files.settings.read_text(), dict[str, dict[str, object]])
        assert {"PreToolUse", "PermissionRequest", "PostToolUse"} <= set(hooks["hooks"])
        assert str(files.socket) in files.settings.read_text()

    def test_an_answer_goes_to_the_waiting_hook_and_is_never_typed(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        opened = _OpenPrompt(tmp_path)
        relay = _RecordingRelay()
        try:
            caplog.set_level("INFO", logger=session.__name__)
            session._deliver_one(
                cast(ThreadedRelay, relay),
                InboundDrainItem(
                    text="Yes",
                    kind="answer",
                    payload={"prompt_id": opened.prompt_id, "decision": "allow"},
                ),
                stream=False,
                inbound=opened.inbound,
                bridge=opened.bridge,
                cli="claude",
            )
            reply = opened.reply()
        finally:
            opened.bridge.close()

        assert json_loads(reply, dict[str, dict[str, object]])["hookSpecificOutput"][
            "decision"
        ] == {"behavior": "allow"}
        assert (relay.submitted, relay.written) == ([], [])
        assert not opened.inbound.prompt_pending.is_set()
        assert [r for r in caplog.records if r.name == session.__name__] == []

    def test_lines_wait_out_an_open_prompt_while_its_answer_goes_through(
        self,
        tmp_path: Path,
    ) -> None:
        opened = _OpenPrompt(tmp_path)
        relay = _PromptWatchingRelay(opened.inbound.prompt_pending)
        stop = threading.Event()
        answer = InboundDrainItem(
            text="Yes",
            kind="answer",
            payload={"prompt_id": opened.prompt_id, "decision": "allow"},
        )
        client = _ScriptedClient(
            [
                [
                    InboundDrainItem(text="first", source="ann@x"),
                    InboundDrainItem(text="interrupt", kind="interrupt"),
                ],
                [InboundDrainItem(text="/compact", kind="command"), answer],
            ],
            stop=stop,
        )
        try:
            _inbound_poll_loop(
                cast(Client, client),
                cast(Sink, _SessionSink()),
                cast(ThreadedRelay, relay),
                stop,
                poll_interval=0.01,
                wait_sec=25.0,
                inbound=opened.inbound,
                bridge=opened.bridge,
            )
            reply = opened.reply()
        finally:
            opened.bridge.close()

        assert "allow" in reply
        assert relay.written == [b"\x1b"]
        assert relay.submitted == ["ann@x: first", "/compact"]
        assert relay.typed_into_a_prompt == [False, False]
        assert client.waits == [25.0, 0.01, 25.0]

    def test_the_bridges_records_go_to_the_sink_on_the_runs_part(
        self,
        tmp_path: Path,
    ) -> None:
        records: deque[Event] = deque()
        pending = threading.Event()
        bridge = session._start_bridge(
            socket_address(tmp_path / "bridge.sock"),
            emit=partial(session._queue_run_record, records, Path("prompts-1")),
            pending=pending,
        )
        assert bridge is not None
        hook_reply = _held_hook(bridge_path=tmp_path / "bridge.sock")
        assert pending.wait(2.0)
        bridge.close()
        assert hook_reply() == ""
        sink, stats = _RecordingSink(), _Stats()

        asyncio.run(
            session._drain_queues(
                ClaudeAdapter(),
                sink,
                stats,
                RunConfig(cli_name="claude"),
                slash_queue=deque(),
                stream_queue=None,
                run_records=records,
                stream_part=Path("unused"),
            ),
        )

        kinds = [
            next(iter(event.record.extra))
            for _, _, event in sink.events
            if isinstance(event.record, SystemMessage)
        ]
        assert kinds == ["$interaction", "$interaction_resolved"]
        [part] = {event.path for _, _, event in sink.events}
        assert part.name.startswith("prompts-")
        assert [idx for idx, _, _ in sink.events] == [0, 1]
        assert stats.counts == {"SystemMessage": 2}
        assert not records

    def test_no_socket_or_one_that_cannot_listen_starts_no_bridge(
        self,
        tmp_path: Path,
    ) -> None:
        pending = threading.Event()
        records: list[SystemMessage] = []

        assert session._start_bridge(None, emit=records.append, pending=pending) is None
        assert (
            session._start_bridge(
                tmp_path / "gone" / "b.sock",
                emit=records.append,
                pending=pending,
            )
            is None
        )

    def test_a_synced_claude_runs_prompt_reaches_its_records_and_holds_its_lines(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        drained: dict[str, object] = {}
        hook = _PermissionHook()

        class _Claude:
            """Asks one permission, as claude does through its settings' hook."""

            def __init__(self, argv: Sequence[str], **kwargs: object) -> None:
                del kwargs
                self._settings = Path(argv[argv.index("--settings") + 1])

            def run(self) -> int:
                hook.ask(self._settings, drained)
                return 0

        def watch(*args: object, **kwargs: object) -> None:
            del args
            drained.update(kwargs)
            armed = kwargs["armed"]
            assert isinstance(armed, threading.Event)
            armed.set()

        polled: dict[str, object] = {}

        def poll(*args: object, **kwargs: object) -> None:
            del args
            polled.update(kwargs)

        def open_sink(config: RunConfig, adapter: Adapter) -> Sink:
            del config, adapter
            return _RecordingSink()

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(session, "_open_sink", open_sink)
        monkeypatch.setattr(session, "_drain_filesystem_loop", watch)
        monkeypatch.setattr(session, "_inbound_poll_loop", poll)
        monkeypatch.setattr(session, "ThreadedRelay", _Claude)
        monkeypatch.setattr(shutil, "which", _always_found)

        config = RunConfig(
            cli_name="claude",
            client=Client(base_url=LOCALHOST_FALLBACK_URL),
            quiesce_seconds=0.0,
        )
        assert run(config) == 0

        # Open while the hook waited, which held the run's lines; the run's end let
        # it go with no decision.
        assert hook.held == [True]
        assert hook.reply() == ""
        records = cast(deque[Event], drained["run_records"])
        assert [
            next(iter(event.record.extra))
            for event in records
            if isinstance(event.record, SystemMessage)
        ] == ["$interaction", "$interaction_resolved"]
        inbound = drained["inbound"]
        assert isinstance(inbound, InboundState)
        assert not inbound.prompt_pending.is_set()
        # The commands the poller types go on the bridge's part: one part per run.
        record = cast(Callable[[SystemMessage], None], polled["record"])
        record(command_record("/goal x", source="ann@x", room=None, state="typed"))
        [part] = {event.path for event in records}
        assert part.name.startswith("prompts-")
        assert len(records) == 3


class _OpenPrompt:
    """A started bridge with one prompt open, its hook waiting on a thread."""

    def __init__(self, tmp_path: Path) -> None:
        self.inbound = InboundState(fallback_sec=0.0)
        records: list[SystemMessage] = []
        self.bridge = PromptBridge(
            socket_address(tmp_path / "bridge.sock"),
            emit=records.append,
            pending=self.inbound.prompt_pending,
        )
        self.bridge.start()
        self.reply = _held_hook(bridge_path=tmp_path / "bridge.sock")
        assert self.inbound.prompt_pending.wait(2.0)
        card = from_plain(records[0].extra["$interaction"], dict[str, object])
        self.prompt_id = from_plain(card["id"], str)


def _held_hook(*, bridge_path: Path) -> Callable[[], str]:
    """Start a ``PermissionRequest`` hook on a thread; return what waits for its reply."""
    event = {
        "hook_event_name": "PermissionRequest",
        "prompt_id": "p1",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
    }
    replies: list[str] = []
    thread = threading.Thread(
        target=lambda: replies.append(
            prompt_bridge.ask(socket_address(bridge_path), json.dumps(event)),
        ),
        daemon=True,
    )
    thread.start()

    def reply() -> str:
        thread.join(2.0)
        return replies[0]

    return reply


class _PermissionHook:
    """A ``PermissionRequest`` hook claude runs, waiting on the run's bridge."""

    def __init__(self) -> None:
        self.held: list[bool] = []
        self._replies: list[str] = []
        self._thread: threading.Thread | None = None

    def ask(self, settings: Path, drained: Mapping[str, object]) -> None:
        """Start the hook the settings name; note whether the run saw it open."""
        hooks = json_loads(settings.read_text(), dict[str, dict[str, object]])["hooks"]
        entries = from_plain(
            hooks["PermissionRequest"],
            list[dict[str, list[dict[str, str | int]]]],
        )
        command = shlex.split(str(entries[-1]["hooks"][0]["command"]))
        socket_path = Path(command[command.index("--socket") + 1])
        event = {"hook_event_name": "PermissionRequest", "tool_name": "Bash"}
        self._thread = threading.Thread(
            target=lambda: self._replies.append(
                prompt_bridge.ask(socket_path, json.dumps(event)),
            ),
            daemon=True,
        )
        self._thread.start()
        inbound = drained["inbound"]
        assert isinstance(inbound, InboundState)
        records = cast(deque[Event], drained["run_records"])
        self.held.append(inbound.prompt_pending.wait(2.0) and len(records) == 1)

    def reply(self) -> str:
        """Return what the hook printed once let go."""
        assert self._thread is not None
        self._thread.join(2.0)
        return self._replies[0]


class _ScriptedClient:
    """Returns one scripted batch per drain, then stops the run.

    ``on_drain`` names what happens on the CLI's side during the drain of each number,
    counted from 1, before its batch returns.
    """

    def __init__(
        self,
        batches: list[list[InboundDrainItem]],
        *,
        stop: threading.Event,
        on_drain: Mapping[int, Callable[[], object]] | None = None,
    ) -> None:
        self._batches = batches
        self._stop = stop
        self._on_drain = on_drain or {}
        self.waits: list[float] = []

    def drain_inbound(
        self,
        session_id: uuid.UUID,
        *,
        wait_sec: float = 0.0,
        **where: str | None,
    ) -> list[InboundDrainItem]:
        del where
        del session_id
        self.waits.append(wait_sec)
        if (happens := self._on_drain.get(len(self.waits))) is not None:
            _ = happens()
        if self._batches:
            return self._batches.pop(0)
        self._stop.set()
        return []


class TestATrustCardHoldsTheRun:
    """A CLI asking to trust its folder gets a card in Chat, and no line meanwhile."""

    def test_a_card_opens_the_drain_before_the_cli_is_ready(
        self,
        tmp_path: Path,
    ) -> None:
        """The run drains, so its lease makes it live and the card can be answered."""
        state = InboundState(marker=tmp_path / "ready", fallback_sec=20.0)
        state.trust_pending.set()

        assert state.wait_open(_RecordingStop(), poll_sec=0.5)
        assert not state.ready()

    def test_the_fallback_waits_out_a_card_and_restarts_once_trusted(self) -> None:
        now = [0.0]
        state = InboundState(fallback_sec=20.0, clock=lambda: now[0])
        state.trust_pending.set()
        now[0] = 30.0
        while_open = state.ready()
        state.trust_pending.clear()
        state.rearm()
        just_trusted = state.ready()
        now[0] = 50.0

        assert (while_open, just_trusted, state.ready()) == (False, False, True)

    def test_a_line_waits_for_the_card_and_goes_once_the_terminal_trusted(
        self,
        tmp_path: Path,
    ) -> None:
        marker = tmp_path / "ready"
        state = InboundState(marker=marker, fallback_sec=20.0, clock=lambda: 0.0)
        relay = _RecordingRelay()
        records: list[SystemMessage] = []
        gate = TrustGate(
            emit=records.append,
            pending=state.trust_pending,
            ready=state.ready,
            rearm=state.rearm,
            relay=relay,
        )
        gate.open("claude", "~/proj", machine=None, remembered=True)
        stop = threading.Event()
        client = _ScriptedClient(
            [[InboundDrainItem(text="hi", source="ann@x")], []],
            stop=stop,
            on_drain={2: marker.touch},
        )

        _inbound_poll_loop(
            cast(Client, client),
            cast(Sink, _SessionSink()),
            cast(ThreadedRelay, relay),
            stop,
            poll_interval=0.01,
            wait_sec=25.0,
            inbound=state,
            gate=gate,
        )

        assert relay.submitted == ["ann@x: hi"]
        assert client.waits == [0.01, 0.01, 25.0]
        assert records[-1].extra["$interaction_resolved"] == {
            "id": from_plain(records[0].extra["$interaction"], dict[str, object])["id"],
            "decision": "allow",
            "by": "terminal",
        }

    def test_an_answer_naming_the_card_goes_to_the_gate_and_others_to_the_bridge(
        self,
        tmp_path: Path,
    ) -> None:
        opened = _OpenPrompt(tmp_path)
        relay = _RecordingRelay()
        records: list[SystemMessage] = []
        gate = TrustGate(
            emit=records.append,
            pending=opened.inbound.trust_pending,
            ready=opened.inbound.ready,
            rearm=opened.inbound.rearm,
            relay=relay,
        )
        gate.open("codex", "~/proj", machine="gx10", remembered=True)
        card = from_plain(records[0].extra["$interaction"], dict[str, object])
        deliver = partial(
            session._deliver_one,
            cast(ThreadedRelay, relay),
            stream=False,
            inbound=opened.inbound,
            bridge=opened.bridge,
            gate=gate,
            cli="codex",
        )
        try:
            deliver(
                InboundDrainItem(
                    text="Trust ~/proj",
                    kind="answer",
                    payload={"prompt_id": card["id"], "decision": "allow"},
                ),
            )
            deliver(
                InboundDrainItem(
                    text="Yes",
                    kind="answer",
                    payload={"prompt_id": opened.prompt_id, "decision": "allow"},
                ),
            )
            reply = opened.reply()
        finally:
            opened.bridge.close()

        assert relay.written == [b"\r"]
        assert relay.submitted == []
        assert not opened.inbound.trust_pending.is_set()
        assert "allow" in reply

    def test_an_untrusted_claude_run_opens_its_card_before_the_cli_starts(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv("CLAUDE_CODE_SANDBOXED")
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setattr("socket.gethostname", lambda: "gx10.lan")
        wiring, _ = _wire(
            monkeypatch,
            tmp_path,
            RunConfig(cli_name="claude", client=cast(Client, object())),
            ClaudeAdapter(),
            relay=_CardCheckingRelay,
        )

        relay = wiring.relay_instance
        assert isinstance(relay, _CardCheckingRelay)
        assert relay.at_start == (True, ["$interaction"])
        *_, options = wiring.inbound
        gate = cast(dict[str, object], options)["gate"]
        assert isinstance(gate, TrustGate)
        assert gate.relay is relay
        first, *_, last = _run_records(wiring)
        card = from_plain(first.extra["$interaction"], dict[str, object])
        assert (card["cli"], card["machine"]) == ("claude", "gx10")
        # The CLI exited with the card open: it quit at the dialog.
        assert last.extra["$interaction_resolved"] == {
            "id": card["id"],
            "decision": "deny",
            "by": "terminal",
        }
        assert "[trax run] claude is not trusted in " in capsys.readouterr().err

    def test_a_card_open_when_the_relay_fails_is_released(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """The CLI never ran, so nobody refused it."""
        monkeypatch.delenv("CLAUDE_CODE_SANDBOXED")
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        wiring = _Wiring()
        with pytest.raises(RuntimeError, match="pty allocation"):
            _ = _wire(
                monkeypatch,
                tmp_path,
                RunConfig(cli_name="claude", client=cast(Client, object())),
                ClaudeAdapter(),
                relay=_FailingWiringRelay,
                wiring=wiring,
            )

        [_, last] = _run_records(wiring)
        assert (
            from_plain(last.extra["$interaction_resolved"], dict[str, object])[
                "decision"
            ]
            == "released"
        )

    @pytest.mark.parametrize(
        ("output", "kinds"),
        [
            (
                b"\x1b[5;3H\x1b[22mTrust\x1b[5;9Hthis\x1b[5;14Hfolder?",
                ["$interaction", "$interaction_resolved"],
            ),
            (b"ready when you are", []),
        ],
        ids=["asks", "trusted-after-all"],
    )
    def test_an_untrusted_codex_run_opens_its_card_when_its_screen_asks(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        output: bytes,
        kinds: list[str],
    ) -> None:
        wiring, _ = _wire(
            monkeypatch,
            tmp_path,
            RunConfig(cli_name="codex", client=cast(Client, object())),
            CodexAdapter(),
            output=output,
        )

        assert [next(iter(record.extra)) for record in _run_records(wiring)] == kinds
        owner = wiring.drain["owner"]
        assert isinstance(owner, session._SessionOwner)
        assert wiring.relay["on_output"] != owner.output

    def test_a_local_run_reads_no_trust(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A run that syncs nothing has no Chat to ask in."""
        monkeypatch.delenv("CLAUDE_CODE_SANDBOXED")
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        wiring, _ = _wire(
            monkeypatch,
            tmp_path,
            RunConfig(cli_name="claude", sync=False),
            ClaudeAdapter(),
        )

        assert _run_records(wiring) == []


def _run_records(wiring: _Wiring) -> list[SystemMessage]:
    """Return the records the run queued on its own part, each a system message."""
    found = [
        event.record
        for event in cast(deque[Event], wiring.drain["run_records"])
        if isinstance(event.record, SystemMessage)
    ]
    assert len(found) == len(cast(deque[Event], wiring.drain["run_records"]))
    return found


class _FailingWiringRelay(_WiringRelay):
    """A relay whose terminal cannot be made, so its CLI never starts."""

    @override
    def run(self) -> int:
        raise RuntimeError("pty allocation failed")


class _CardCheckingRelay(_WiringRelay):
    """Notes, as the CLI is about to start, whether its trust card is already open."""

    def __init__(
        self,
        argv: list[str],
        *,
        wiring: _Wiring,
        rc: int,
        output: bytes,
        **kwargs: object,
    ) -> None:
        super().__init__(argv, wiring=wiring, rc=rc, output=output, **kwargs)
        inbound = wiring.drain["inbound"]
        assert isinstance(inbound, InboundState)
        self.at_start = (
            inbound.trust_pending.is_set(),
            [next(iter(record.extra)) for record in _run_records(wiring)],
        )


class TestChatCommands:
    """A command from Chat is typed bare and recorded; codex keeps its goal modal shut."""

    def test_the_servers_goal_is_typed_verbatim_and_recorded_as_chat_reads_it(
        self,
    ) -> None:
        """The server's command for ``/Goal ship it``, typed and recorded as the web reads it.

        The record is compared with ``web/test/seams/goalCommand.json``, which the web's
        tests render, so a change of shape on either side fails both.
        """
        command = classify("/Goal ship it", "claude")
        assert isinstance(command, Command)
        relay = _RecordingRelay()
        records: list[SystemMessage] = []

        session._deliver_one(
            cast(ThreadedRelay, relay),
            InboundDrainItem(
                text=command.text,
                kind="command",
                source="ada@example.com",
            ),
            stream=False,
            inbound=InboundState(fallback_sec=0.0),
            cli="claude",
            record=records.append,
        )

        assert relay.submitted == ["/goal ship it"]
        [record] = records
        stored = RecordBody.of(
            SessionRecordRow.of(
                session_id=uuid.UUID(int=0),
                part=1,
                idx=2,
                record=replace(record, timestamp="2026-10-20T00:00:01+00:00"),
            ),
        ).model_dump(mode="json")
        seam = _CWD.parents[1] / "web" / "test" / "seams" / "goalCommand.json"
        assert json_loads(json.dumps(stored)) == json_loads(
            seam.read_text(encoding="utf-8"),
        )

    def test_a_command_is_typed_bare_and_recorded_but_never_stamped(self) -> None:
        relay = _RecordingRelay()
        state = InboundState(fallback_sec=0.0)
        records: list[SystemMessage] = []

        session._deliver_one(
            cast(ThreadedRelay, relay),
            InboundDrainItem(
                text="/goal ship it",
                kind="command",
                source="ann@x",
                room="lab",
            ),
            stream=False,
            inbound=state,
            cli="claude",
            record=records.append,
        )
        later = UserMessage(content="/goal ship it")

        assert relay.submitted == ["/goal ship it"]
        [record] = records
        assert record.extra["$command"] == {
            "text": "/goal ship it",
            "verb": "goal",
            "args": "ship it",
            "source": "ann@x",
            "room": "lab",
            "state": "typed",
            "reason": None,
        }
        assert state.stamp(later) is later

    def test_codex_refuses_a_new_goal_over_an_open_one_until_it_is_cleared(
        self,
    ) -> None:
        relay = _RecordingRelay()
        state = InboundState(fallback_sec=0.0)
        state.goal.observe(
            UncategorizedRecord(
                kind="event_msg/thread_goal_updated",
                payload={"goal": {"objective": "a", "status": "active"}},
            ),
        )
        records: list[SystemMessage] = []
        deliver = partial(
            session._deliver_one,
            cast(ThreadedRelay, relay),
            stream=False,
            inbound=state,
            cli="codex",
            record=records.append,
        )

        # A pause is codex's own word: the goal stays open through it.
        for text in ("/goal pause", "/goal b", "/goal clear", "/goal b"):
            deliver(InboundDrainItem(text=text, kind="command", source="ann@x"))

        assert relay.submitted == ["/goal pause", "/goal clear", "/goal b"]
        assert [
            from_plain(record.extra["$command"], dict[str, object])["state"]
            for record in records
        ] == ["typed", "refused", "typed", "typed"]
        refused = from_plain(records[1].extra["$command"], dict[str, object])
        assert refused["reason"] == (
            "codex is following a goal: send /goal clear, then the new goal"
        )
        assert state.goal.open is False

    def test_claude_takes_a_new_goal_over_an_open_one(self) -> None:
        """Claude replaces its goal without asking."""
        relay = _RecordingRelay()
        state = InboundState(fallback_sec=0.0)
        state.goal.observe(
            UncategorizedRecord(
                kind="event_msg/thread_goal_updated",
                payload={"goal": {"status": "active"}},
            ),
        )

        session._deliver_one(
            cast(ThreadedRelay, relay),
            InboundDrainItem(text="/goal b", kind="command"),
            stream=False,
            inbound=state,
            cli="claude",
        )

        assert relay.submitted == ["/goal b"]

    def test_a_command_that_cannot_be_typed_is_logged_not_recorded(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        records: list[SystemMessage] = []
        with caplog.at_level("WARNING", logger=session.__name__):
            session._deliver_one(
                cast(ThreadedRelay, _PickyRelay(reject="/goal x")),
                InboundDrainItem(text="/goal x", kind="command"),
                stream=False,
                cli="claude",
                record=records.append,
            )

        assert records == []
        (logged,) = caplog.records
        assert logged.getMessage() == (
            "trax run: could not type a command; continuing with the rest"
        )
        assert isinstance(logged.exc_info, tuple)
        assert isinstance(logged.exc_info[1], RuntimeError)

    def test_a_loaded_history_past_the_fit_is_compacted_before_any_line(
        self,
        tmp_path: Path,
    ) -> None:
        """Held while the CLI starts, a line still goes after ``/compact``."""
        marker = tmp_path / "ready"
        state = InboundState(marker=marker, fallback_sec=20.0, clock=lambda: 0.0)
        state.trust_pending.set()
        relay = _RecordingRelay()
        records: list[SystemMessage] = []
        stop = threading.Event()
        client = _ScriptedClient(
            [[InboundDrainItem(text="hi", source="ann@x")], []],
            stop=stop,
            on_drain={2: partial(_trusted_and_ready, state, marker)},
        )

        _inbound_poll_loop(
            cast(Client, client),
            cast(Sink, _SessionSink()),
            cast(ThreadedRelay, relay),
            stop,
            poll_interval=0.01,
            inbound=state,
            cli="codex",
            record=records.append,
            compact="the loaded conversation is past what codex holds comfortably",
        )

        assert relay.submitted == ["/compact", "ann@x: hi"]
        [record] = records
        assert record.extra["$command"] == {
            "text": "/compact",
            "verb": "compact",
            "args": "",
            "source": "trax run",
            "room": None,
            "state": "typed",
            "reason": "the loaded conversation is past what codex holds comfortably",
        }

    def test_the_spawn_asks_for_compaction_only_past_the_fit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        wiring, _ = _wire(
            monkeypatch,
            tmp_path,
            RunConfig(
                cli_name="claude",
                client=cast(Client, object()),
                compact_after_load=True,
            ),
            ClaudeAdapter(),
        )

        *_, options = wiring.inbound
        assert cast(dict[str, object], options)["compact"] == (
            "the loaded conversation is past what claude holds comfortably"
        )


def _trusted_and_ready(state: InboundState, marker: Path) -> None:
    """Answer the trust dialog in the terminal: the card closes and the CLI is up."""
    state.trust_pending.clear()
    marker.touch()


class _ChatClient:
    """Serves a chat whose one part claude wrote, or a chat with no part at all."""

    def __init__(self, *, lines: Sequence[str] = ()) -> None:
        self._records = list(claude.normalize(io.StringIO("".join(lines))))
        self.asked = 0

    def get_inquiry(self, ref: object) -> tuple[str, uuid.UUID, dict[str, object]]:
        """Name the chat's session by its seq, which a window's note asks for."""
        del ref
        return "AgentSession", _CHAT, {"self": {"seq": 7}}

    def read_session_parts(self, session_id: uuid.UUID) -> list[PartBody]:
        del session_id
        self.asked += 1
        if not self._records:
            return []
        return [
            PartBody(
                part=0,
                name="s.jsonl",
                format="claude",
                records=len(self._records),
            ),
        ]

    def read_session_records(
        self,
        session_id: uuid.UUID,
        *,
        part: int = 0,
        **_: object,
    ) -> list[RecordBody]:
        return [
            RecordBody.of(
                SessionRecordRow.of(
                    session_id=session_id,
                    part=part,
                    idx=idx,
                    record=record,
                ),
            )
            for idx, record in enumerate(self._records)
        ]


class TestAnAttachedRunLoadsTheChat:
    """``--attach`` starts the CLI from the chat's conversation, as a switch does."""

    def test_a_detached_run_loads_the_chat_in_its_host(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """The launcher reads nothing before it forks.

        On macOS the TLS trust check loads system frameworks a forked child may not
        use, so a read before the fork crashed the host at its first file watch.
        """
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
        client = _ChatClient(lines=[_claude_line("hello")])
        ran: list[RunConfig] = []
        hosted: list[Callable[[HostSpec | None], int]] = []
        monkeypatch.setattr(session, "run", partial(_keep_run, ran))
        monkeypatch.setattr(detach, "launch", partial(_keep_host, hosted))

        argv = ["--detach", "--attach", str(_CHAT), "claude"]
        assert session.main(argv, client_factory=lambda: cast(Client, client)) == 0
        assert client.asked == 0

        [start] = hosted
        assert start(None) == 0
        [config] = ran
        assert client.asked == 1
        assert config.cli_args == ("--resume", config.cli_session_id)

    def test_the_cli_resumes_the_chats_conversation(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
        client = _ChatClient(lines=[_claude_line("hello"), _claude_line("again")])

        config = _keep_attached(monkeypatch, client, ["--attach", str(_CHAT), "claude"])

        assert config.resume_path is not None
        assert config.cli_session_id is not None
        assert config.cli_args == ("--resume", config.cli_session_id)
        assert config.prefix_bytes == config.resume_path.stat().st_size > 0
        assert config.attach == _CHAT
        assert "hello" in config.resume_path.read_text()
        assert not config.compact_after_load
        assert capsys.readouterr().err == (
            "[trax run] loaded 2 records (~2 tokens) into claude; 0 earlier not loaded\n"
        )

    def test_a_chat_that_compacted_loads_from_its_summary(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A switch to codex loads claude's window: the summary, then what follows.

        Claude resumes its own file whole, and honours its own boundary; another CLI is
        handed the window the one cut every switch makes.
        """
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
        boundary = {
            "type": "system",
            "subtype": "compact_boundary",
            "content": "Conversation compacted",
            "uuid": "boundary",
            "compactMetadata": {"trigger": "manual", "preTokens": 967_000},
        }
        summary = {
            "type": "user",
            "uuid": "summary",
            "isCompactSummary": True,
            "isVisibleInTranscriptOnly": True,
            "message": {"role": "user", "content": "Summary: F1 is apples."},
        }
        client = _ChatClient(
            lines=[
                _claude_line("F1 is apples"),
                json.dumps(boundary) + "\n",
                json.dumps(summary) + "\n",
                _claude_line("F2 is pears"),
            ],
        )

        config = _keep_attached(monkeypatch, client, ["--attach", str(_CHAT), "codex"])

        assert config.resume_path is not None
        with config.resume_path.open(encoding="utf-8") as handle:
            told = [
                record.content
                for record in codex.normalize(handle)
                if isinstance(record, UserMessage)
            ]
        assert "F1 is apples" not in told
        assert any("Summary: F1 is apples." in (each or "") for each in told)
        assert told[-1] == "the terminal user: F2 is pears"
        assert "earlier not loaded" in capsys.readouterr().err

    def test_a_load_past_the_fit_asks_the_cli_to_compact_it(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Codex holds 480 kB comfortably: 40 lines of 15 kB are past it."""
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
        lines = [_claude_line(f"{n:02} " + "x" * 15_000) for n in range(40)]

        config = _keep_attached(
            monkeypatch,
            _ChatClient(lines=lines),
            ["--attach", str(_CHAT), "codex"],
        )

        assert config.resume_path is not None
        assert config.compact_after_load

    @pytest.mark.parametrize("cli", ["claude", "sh"])
    def test_a_chat_with_nothing_to_load_starts_fresh(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        cli: str,
    ) -> None:
        config = _keep_attached(
            monkeypatch,
            _ChatClient(),
            ["--attach", str(_CHAT), cli],
        )

        assert (config.resume_path, config.cli_session_id, config.prefix_bytes) == (
            None,
            None,
            0,
        )
        assert config.cli_args == ()
        assert f"[trax run] {cli} starts without the chat's history: " in (
            capsys.readouterr().err
        )

    @pytest.mark.usefixtures("claude_scope")
    def test_a_resume_named_after_the_dashes_is_not_loaded_over(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = _ChatClient(lines=[_claude_line("hello")])

        config = _keep_attached(
            monkeypatch,
            client,
            ["--attach", str(_CHAT), "claude", "--", "--resume", _OLD],
        )

        assert client.asked == 0
        assert config.cli_args == ("--resume", _OLD)


def _keep_attached(
    monkeypatch: pytest.MonkeyPatch,
    client: _ChatClient,
    argv: Sequence[str],
) -> RunConfig:
    """Return the config ``trax run argv`` would run against ``client``."""
    ran: list[RunConfig] = []
    monkeypatch.setattr(session, "run", partial(_keep_run, ran))
    assert session.main(argv, client_factory=lambda: cast(Client, client)) == 0
    (config,) = ran
    return config


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
