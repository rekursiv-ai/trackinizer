"""Tests for the session runner's file scoping (Bug B regression).

The runner shares one session root with concurrent sessions, so it must
drain only the files the wrapped run creates -- never re-emit lines from
sessions that already existed when the run started.
"""

from __future__ import annotations

from collections import deque
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Callable,
    Iterable,
    Iterator,
    Sequence,
)
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, TextIO, cast, override

import asyncio
import json
import os
import shutil
import sys
import threading
import time
import uuid

import pytest

from trackinizer.client.client import Client
from trackinizer.lib.agent.types.sessions import (
    AssistantMessage,
    IncompleteRecord,
    SessionRecord,
    UserMessage,
)
from trackinizer.lib.custom_json import DictCodec, ListCodec, loads
from trackinizer.lib.posix import follow
from trackinizer.lib.posix.follow import follow_tree
from trackinizer.lib.posix.host import HostSpec
from trackinizer.lib.posix.relay import ThreadedRelay
from trackinizer.lib.posix.testing import poll_fsevents
from trackinizer.trax.profile import LOCALHOST_FALLBACK_URL
from trackinizer.trax.run import session
from trackinizer.trax.run.adapters.claude import ClaudeAdapter
from trackinizer.trax.run.adapters.codex import CodexAdapter
from trackinizer.trax.run.adapters.gemini import GeminiAdapter
from trackinizer.trax.run.adapters.iostream import IOStreamAdapter
from trackinizer.trax.run.adapters.tail import Tail
from trackinizer.trax.run.session import (
    RunConfig,
    _cli_argv,
    _drain_filesystem_loop,
    _emit_slash_commands,
    _existing_session_files,
    _inbound_poll_loop,
    _process_chunk,
    _render_inbound,
    _routing_env,
    _session_owner,
    _Stats,
    resume_argv,
    run,
)
from trackinizer.trax.run.sink import Sink
from trackinizer.trax.run.slash import SlashCommand
from trackinizer.wire.wire_sessions import WorkspaceMessageContext


if TYPE_CHECKING:
    from trackinizer.trax.run.adapters.custom_types import Adapter
    from trackinizer.trax.run.custom_types import Event
    from trackinizer.wire.wire_session_ir import RecordBody


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
        self.closed = False
        self.flushes = 0
        self.cli_session_ids: list[str] = []
        self._next_idx: dict[Path, int] = {}

    @property
    @override
    def session_id(self) -> None:
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
    def emit(self, adapter_name: str, event: Event) -> None:
        idx = self._next_idx.get(event.path, 0)
        self._next_idx[event.path] = idx + 1
        self.events.append((idx, adapter_name, event))

    @override
    def emit_slash_command(self, command: SlashCommand, at: datetime) -> None:
        self.slash.append((command, at))

    @override
    def flush(self) -> None:
        self.flushes += 1

    @override
    def drain_pending(self) -> list[tuple[Path, RecordBody]]:
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
    obj = DictCodec.coerce(loads(stream.read()))
    for text in ListCodec.coerce(obj.get("messages"), str):
        yield UserMessage(content=text)


class _FakeAdapter:
    """Treats every ``*.jsonl`` line as one ``UserMessage`` record."""

    name: str = "fake"
    cli_binary: str = "fake"
    whole_file: bool = False
    parent_session_env: frozenset[str] = frozenset[str]()

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

    def reader(self) -> Tail:
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
    def reader(self) -> Tail:
        return Tail(_document_records, whole_file=True)


class _PoisonAdapter(_FakeAdapter):
    """Raises on a line whose text is ``boom``; otherwise a ``UserMessage``."""

    name: str = "poison"
    cli_binary: str = "poison"

    @override
    def reader(self) -> Tail:
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
        baseline = _existing_session_files(adapter)
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
        baseline = _existing_session_files(adapter)

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

        def record(text: str) -> str:
            return (
                json.dumps(
                    {
                        "type": "user",
                        "uuid": text,
                        "message": {"role": "user", "content": text},
                    },
                )
                + "\n"
            )

        def write() -> None:
            _ = (theirs / "other.jsonl").write_text(record("theirs"))
            _ = (mine / "own.jsonl").write_text(record("mine"))

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
                (scope / f"{session_id}.jsonl").write_text(
                    json.dumps(
                        {
                            "type": "user",
                            "uuid": name,
                            "message": {"role": "user", "content": name},
                        },
                    )
                    + "\n",
                )
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
                threading.BrokenBarrierError,
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

        def line_reader(self: CodexAdapter) -> Tail:
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
                threading.BrokenBarrierError,
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

        def line_reader(self: CodexAdapter) -> Tail:
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
            (fresh / "abc-123.jsonl").write_text(
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "u1",
                        "message": {"role": "user", "content": "captured"},
                    },
                )
                + "\n",
            )

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
            (fresh / "abc-123.jsonl").write_text(
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "u1",
                        "message": {"role": "user", "content": "captured"},
                    },
                )
                + "\n",
            )

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
                    json.dumps(
                        {
                            "type": "user",
                            "uuid": "u1",
                            "message": {"role": "user", "content": "first"},
                        },
                    )
                    + "\n",
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
                _ = handle.write(_uuid_line("a"))
            _wait_for_events(sink, 1)
            assert sink.events, "the first line never reached the sink"
            with log.open("ab") as handle:
                _ = handle.write(_uuid_line("b") + _uuid_line("c"))
            _wait_for_events(sink, 3)
            captured = len(sink.events)

            # Compaction: the file is REPLACED, smaller, and keeps ``c``.
            log.write_bytes(_uuid_line("c") + _uuid_line("d"))
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


def _uuid_line(marker: str) -> bytes:
    """One claude-shaped record whose uuid is stable for ``marker``."""
    return (
        json.dumps(
            {
                "type": "user",
                "uuid": f"uuid-{marker}",
                "message": {"role": "user", "content": marker},
            },
        )
        + "\n"
    ).encode()


def _uuid_records(stream: TextIO) -> Iterator[SessionRecord]:
    """Read the claude-shaped fixture lines ``_uuid_line`` writes."""
    for line in stream:
        obj = DictCodec.coerce(loads(line))
        message = DictCodec.coerce(obj["message"])
        yield UserMessage(content=str(message["content"]))


class _UuidAdapter(_FakeAdapter):
    """A line adapter over uuid-stamped records, like claude's."""

    name: str = "uuids"
    cli_binary: str = "uuids"

    @override
    def reader(self) -> Tail:
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
        [
            (("--session-id", "s1"), "s1"),
            (("--session-id=s2",), "s2"),
            (("--resume", "s3"), "s3"),
            (("--resume=s4",), "s4"),
            (("-r", "s5"), "s5"),
        ],
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

    @pytest.mark.parametrize("flag", ["--continue", "-c"])
    def test_a_continued_claude_session_is_unknown_until_observed(
        self,
        flag: str,
    ) -> None:
        config = RunConfig(cli_name="claude", cli_args=(flag,))
        owner = _session_owner(ClaudeAdapter(), config)
        assert owner is not None
        assert owner.session_id is None
        assert _cli_argv(ClaudeAdapter(), config, owner) == ["claude", flag]

    def test_a_resumed_claude_run_claims_its_materialized_id(self) -> None:
        """The resume path names the id itself; claude must not get a second."""
        config = RunConfig(cli_name="claude", cli_session_id="r1")
        owner = _session_owner(ClaudeAdapter(), config)
        assert owner is not None
        assert owner.session_id == "r1"
        assert _cli_argv(ClaudeAdapter(), config, owner) == ["claude"]

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
            "exec",
            "hi",
        ]
        assert _cli_argv(CodexAdapter(), explicit, None) == ["codex", "--no-daemon"]

    def test_another_cli_claims_nothing_and_gets_its_args_verbatim(self) -> None:
        config = RunConfig(cli_name="gemini", cli_args=("-p", "x"))
        assert _session_owner(GeminiAdapter(), config) is None
        assert _cli_argv(GeminiAdapter(), config, None) == ["gemini", "-p", "x"]


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
) -> tuple[_Wiring, int]:
    """Run ``_spawn_and_drain`` against recording collaborators."""
    wiring = _Wiring()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(
        session,
        "ThreadedRelay",
        partial(_WiringRelay, wiring=wiring, rc=rc, output=output),
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

    def test_a_fresh_run_names_no_session_before_open(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        sink = _GrantingSink(granted="scientist")
        _ = _wire(
            monkeypatch,
            tmp_path,
            RunConfig(cli_name="claude", sync=False),
            ClaudeAdapter(),
            sink=sink,
        )
        assert sink.calls[0] == "open"

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
        assert "session-log watch not ready" in capsys.readouterr().err

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
        assert options == {"stream": stream}
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
    ) -> list[tuple[str, str | None, str | None, WorkspaceMessageContext | None]]:
        del session_id, wait_sec
        self.drains += 1
        if self.drains > 1:
            _real_pause(0.02)
            return []
        return [(text, None, None, None) for text in self._texts]


class _SessionSink:
    """A sink whose session is already open, so inbound has an id to use."""

    session_id = uuid.UUID("11111111-2222-3333-4444-555555555555")


class _RecordingRelay:
    """Records what would have been typed into the CLI."""

    def __init__(self) -> None:
        self.submitted: list[str] = []

    def submit(self, text: str) -> None:
        self.submitted.append(text)


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
    ) -> list[tuple[str, str | None, str | None, WorkspaceMessageContext | None]]:
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
    ) -> list[tuple[str, str | None, str | None, WorkspaceMessageContext | None]]:
        del session_id, wait_sec
        self.attempts += 1
        raise RuntimeError("back-channel down")


class TestRenderInbound:
    """Routed messages carry their room + sender into the injected text."""

    def test_room_and_sender_prefix(self) -> None:
        assert _render_inbound("go", "alice@x", "sear") == "[sear] alice@x: go"

    def test_sender_only_when_no_room(self) -> None:
        # A direct (session-id) enqueue has no room; the sender still shows.
        assert _render_inbound("go", "alice@x", None) == "alice@x: go"

    def test_bare_text_when_no_context(self) -> None:
        # Neither room nor attested sender: inject the message verbatim.
        assert _render_inbound("go", None, None) == "go"

    def test_workspace_chat_context_is_delivered_separately_from_user_text(
        self,
    ) -> None:
        context = WorkspaceMessageContext.model_validate(
            {
                "workspace_id": "c5286865-67b6-4bd8-ab51-e06e10c326c5",
                "record_id": "889ffcb2-cf44-43e7-9806-eb08428c6203",
                "record": {
                    "id": "889ffcb2-cf44-43e7-9806-eb08428c6203",
                    "kind": "Issue",
                    "seq": 21_706,
                    "title": "ARC3 effort\nwith a newline",
                },
                "visible_visuals": [
                    {
                        "id": "2de97e19-2624-4e89-804e-f19e7248eec3",
                        "type": "trax.chat",
                    },
                ],
            },
        )

        rendered = _render_inbound(
            "What led here?",
            "viewer@example.com",
            None,
            context=context,
        )

        assert rendered == (
            "viewer@example.com: What led here?\n"
            f"Trackinizer context (verify with trax): {context.model_dump_json()}"
            "\nCanvas commands: trax workspace c5286865-67b6-4bd8-ab51-e06e10c326c5; "
            "to show the context graph for this record, run "
            "trax workspace c5286865-67b6-4bd8-ab51-e06e10c326c5 "
            "show trax.subgraph --record 889ffcb2-cf44-43e7-9806-eb08428c6203 "
            "--placement side"
        )

    def test_artifact_chat_points_to_full_immutable_content(self) -> None:
        context = WorkspaceMessageContext.model_validate(
            {
                "workspace_id": "c5286865-67b6-4bd8-ab51-e06e10c326c5",
                "record_id": "251c60b8-1604-4e3a-9eda-1b5b046c3a4d",
                "artifact_content": {
                    "revision": 1,
                    "artifact_id": "251c60b8-1604-4e3a-9eda-1b5b046c3a4d",
                    "issue_id": "c5286865-67b6-4bd8-ab51-e06e10c326c5",
                    "title": "Atlas",
                    "summary": "Frozen summary",
                    "author": "viewer@example.com",
                    "created_at": "2026-09-30T00:00:00Z",
                    "scope": "team",
                    "format": "html",
                    "citations": [],
                    "sections": [],
                },
                "visible_visuals": [],
            },
        )

        rendered = _render_inbound("Explain the source", None, None, context=context)

        assert "trax artifact 251c60b8-1604-4e3a-9eda-1b5b046c3a4d" in rendered
        assert (
            "GET /api/artifacts/251c60b8-1604-4e3a-9eda-1b5b046c3a4d/content"
            in rendered
        )


_ENVELOPE = json.dumps(
    {
        "agent_message": "FYI: trax issue 42 status changed (by bob)",
        "id": "29b5982f-2e1f-4749-9bb6-fe601444282c",
        "kind": "status",
        "subject_ref": "issue 42",
        "row": "trax issue 42",
    },
)


class TestRenderInboundEnvelopes:
    """Change envelopes are shaped per consumer at the CLIENT, not the server.

    The server pushes one uniform JSON envelope to every session. The poller
    decides what reaches the child's stdin: a model CLI gets only the
    ``agent_message`` line (the rest of the fields would pollute its
    context), while an IO-stream child gets the raw JSON to parse itself.
    """

    def test_model_session_receives_only_the_agent_message(self) -> None:
        rendered = _render_inbound(_ENVELOPE, "trackinizer", None, stream=False)
        assert rendered == "FYI: trax issue 42 status changed (by bob)"

    def test_stream_session_receives_the_raw_envelope(self) -> None:
        rendered = _render_inbound(_ENVELOPE, "trackinizer", None, stream=True)
        assert rendered == f"trackinizer: {_ENVELOPE}"

    def test_spoofed_source_is_not_treated_as_an_envelope(self) -> None:
        """Only the route-attested ``trackinizer`` sender unwraps.

        ``source`` is stamped server-side from the principal, so a human
        cannot claim it -- but a JSON-looking message from any OTHER sender
        must render as a plain message, not unwrap.
        """
        rendered = _render_inbound(_ENVELOPE, "mallory@x", None, stream=False)
        assert rendered.startswith("mallory@x: ")

    def test_malformed_envelope_falls_back_to_plain_rendering(self) -> None:
        # A trackinizer-attested message that is not a JSON envelope (or
        # lacks agent_message) must still be delivered, not dropped.
        rendered = _render_inbound("not json", "trackinizer", None, stream=False)
        assert rendered == "trackinizer: not json"


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


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
