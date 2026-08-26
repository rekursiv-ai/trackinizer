"""Tests for the session runner's file scoping (Bug B regression).

The runner shares one session root with concurrent sessions, so it must
drain only the files the wrapped run creates -- never re-emit lines from
sessions that already existed when the run started.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast, override

import inspect
import json
import os
import threading
import time
import uuid

import pytest

from trackinizer.trax.run import session as session_mod
from trackinizer.trax.run.adapters.base import Adapter, Event
from trackinizer.trax.run.adapters.claude import ClaudeAdapter
from trackinizer.trax.run.adapters.codex import CodexAdapter
from trackinizer.trax.run.adapters.gemini import GeminiAdapter
from trackinizer.trax.run.session import (
    RunConfig,
    _drain_filesystem_loop,
    _emit_slash_commands,
    _existing_session_files,
    _inbound_poll_loop,
    _process_chunk,
    _render_inbound,
    _routing_env,
    _Stats,
    run,
)
from trackinizer.types.agent_session_events import (
    SlashCommand,
    UserMessage,
)
from trackinizer.wire.wire_sessions import EventBody


class _RecordingSink:
    """Collects emitted events for assertions; mirrors the ``Sink`` protocol.

    Mints its own per-session ``seq`` like the real sinks, so tests can assert
    the sequence is monotonic from 0.
    """

    def __init__(self) -> None:
        self.events: list[tuple[int, str, Event]] = []
        self.closed = False
        self.flushes = 0
        self.cli_session_ids: list[str] = []
        self._next_seq = 0

    @property
    def session_id(self) -> None:
        return None

    def open(self) -> str | None:
        return None

    def set_cli_session_id(self, cli_session_id: str) -> None:
        self.cli_session_ids.append(cli_session_id)

    def emit(self, adapter_name: str, event: Event) -> None:
        self.events.append((self._next_seq, adapter_name, event))
        self._next_seq += 1

    def flush(self) -> None:
        self.flushes += 1

    def drain_pending(self) -> list[EventBody]:
        return []

    def close(self) -> None:
        self.closed = True


class _FakeAdapter:
    """Treats every ``*.jsonl`` line as a ``UserMessage`` event."""

    name: str = "fake"
    cli_binary: str = "fake"
    whole_file: bool = False

    def __init__(self, root: Path) -> None:
        self._root = root

    def session_dirs(self) -> Iterable[Path]:
        return (self._root,)

    def matches_session_file(self, path: Path) -> bool:
        return path.suffix == ".jsonl"

    def session_id_from_path(self, path: Path) -> str | None:
        del path
        return None

    def parse(self, raw: bytes, *, whole_file: bool) -> Iterable[Event]:
        del whole_file
        return (Event(message=UserMessage(text=raw.decode())),)


class _WholeFileAdapter:
    """A whole-file adapter: the runner must feed it the entire file body.

    Mirrors gemini, which rewrites one JSON object in place rather than
    appending lines. ``parse`` reads ``messages[-1]`` from the whole body.
    """

    name: str = "wholefile"
    cli_binary: str = "wholefile"
    whole_file: bool = True

    def __init__(self, root: Path) -> None:
        self._root = root

    def session_dirs(self) -> Iterable[Path]:
        return (self._root,)

    def matches_session_file(self, path: Path) -> bool:
        return path.suffix == ".json"

    def session_id_from_path(self, path: Path) -> str | None:
        del path
        return None

    def parse(self, raw: bytes, *, whole_file: bool) -> Iterable[Event]:
        assert whole_file
        obj = cast(dict[str, list[str]], json.loads(raw))
        messages = obj.get("messages") or []
        if not messages:
            return ()
        return (Event(message=UserMessage(text=messages[-1])),)


class _PoisonAdapter:
    """Raises on a line whose text is ``boom``; otherwise a ``UserMessage``."""

    name: str = "poison"
    cli_binary: str = "poison"
    whole_file: bool = False

    def __init__(self, root: Path) -> None:
        self._root = root

    def session_dirs(self) -> Iterable[Path]:
        return (self._root,)

    def matches_session_file(self, path: Path) -> bool:
        return path.suffix == ".jsonl"

    def session_id_from_path(self, path: Path) -> str | None:
        del path
        return None

    def parse(self, raw: bytes, *, whole_file: bool) -> Iterable[Event]:
        del whole_file
        if raw == b"boom":
            raise ValueError("parser blew up")
        return (Event(message=UserMessage(text=raw.decode())),)


def _write(path: Path, lines: int) -> None:
    path.write_text("".join(json.dumps({"n": i}) + "\n" for i in range(lines)))


def _drain_once(
    adapter: Adapter,
    tmp_path: Path,
    write: Callable[[], object],
    *,
    baseline: frozenset[Path] = frozenset(),
    expected: int = 1,
) -> tuple[_Stats, _RecordingSink]:
    """Run the real drain, perform ``write``, and collect what it captured.

    The drain is wake-driven, so a test cannot call one scan and inspect the
    result: it starts the loop, writes, and waits for delivery.
    """
    del tmp_path
    sink = _RecordingSink()
    stats = _Stats()
    stop = threading.Event()

    def _run() -> None:
        _drain_filesystem_loop(
            adapter,
            sink,
            stats,
            RunConfig(cli_name=adapter.name),
            stop,
            baseline=baseline,
            slash_queue=deque(),
        )

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    try:
        time.sleep(0.2)  # let the watch arm
        write()
        deadline = time.monotonic() + 3.0
        while len(sink.events) < expected and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        stop.set()
        worker.join(timeout=5.0)
    return stats, sink


class TestSessionScoping:
    """Only this run's session files are captured."""

    def test_baseline_files_are_skipped(self, tmp_path: Path) -> None:
        old = tmp_path / "old.jsonl"
        _write(old, lines=5)
        adapter = _FakeAdapter(tmp_path)
        baseline = _existing_session_files(adapter)
        assert old in baseline

        stats, sink = _drain_once(
            cast(Adapter, adapter),
            tmp_path,
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
            cast(Adapter, adapter),
            tmp_path,
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
            cast(Adapter, adapter),
            tmp_path,
            lambda: None,
            expected=0,
        )

        assert sink.events == []
        assert stats.counts == {}

    def test_this_runs_own_file_is_drained(self, tmp_path: Path) -> None:
        """A file created after the watch is armed IS this run's."""
        adapter = _FakeAdapter(tmp_path)
        stats, _sink = _drain_once(
            cast(Adapter, adapter),
            tmp_path,
            lambda: _write(tmp_path / "mine.jsonl", lines=3),
            expected=3,
        )
        assert stats.counts == {"UserMessage": 3}


class TestAppendedLineDrain:
    def test_rotation_discards_the_previous_files_partial_line(
        self, tmp_path: Path
    ) -> None:
        """A truncated file starts a new byte stream with an empty buffer.

        A held fragment would prepend dead bytes to the first line of the
        replacement, corrupting a turn that parsed fine on disk.
        """
        log = tmp_path / "session.jsonl"
        adapter = _PoisonAdapter(tmp_path)

        def write() -> None:
            log.write_bytes(b"stale-partial")
            time.sleep(0.2)
            log.write_bytes(b"fresh\n")

        _stats, sink = _drain_once(cast(Adapter, adapter), tmp_path, write)

        texts = [cast(UserMessage, event.message).text for _, _, event in sink.events]
        assert texts == ["fresh"]


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
        session_mod._prepare_session_dirs(CodexAdapter())
        assert expected.is_dir()

    def test_claude_captures_a_project_directory_created_after_the_watch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        projects = tmp_path / "projects"
        # One project from an earlier run: what ``session_dirs()`` can see at
        # arming time. The run under test happens in a DIFFERENT workspace.
        (projects / "-existing-workspace").mkdir(parents=True)

        def write() -> None:
            fresh = projects / "-brand-new-workspace"
            fresh.mkdir()
            (fresh / "abc-123.jsonl").write_text(
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "u1",
                        "message": {"role": "user", "content": "captured"},
                    }
                )
                + "\n"
            )

        _stats, sink = _drain_once(cast(Adapter, ClaudeAdapter()), tmp_path, write)

        texts = [cast(UserMessage, e.message).text for _, _, e in sink.events]
        assert texts == ["captured"], "a new project directory captured nothing"

    def test_gemini_captures_a_project_directory_created_after_the_watch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        tmp = tmp_path / ".gemini" / "tmp"
        (tmp / "existingsha" / "chats").mkdir(parents=True)

        def write() -> None:
            chats = tmp / "brandnewsha" / "chats"
            chats.mkdir(parents=True)
            (chats / "session-1.json").write_text(
                json.dumps(
                    {
                        "sessionId": "sess-A",
                        "messages": [{"type": "user", "content": "captured"}],
                    }
                )
            )

        _stats, sink = _drain_once(cast(Adapter, GeminiAdapter()), tmp_path, write)

        texts = [cast(UserMessage, e.message).text for _, _, e in sink.events]
        assert texts == ["captured"], "a new project directory captured nothing"

    def test_claude_captures_when_no_project_directory_exists_yet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A first-ever run has no project dir at all when the watch arms.

        With nothing to watch the runner arms no follower and capture is
        disabled for the whole run -- the worst shape of this bug, since it
        needs no concurrency to hit.
        """
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        (tmp_path / "projects").mkdir()

        def write() -> None:
            fresh = tmp_path / "projects" / "-first-ever-workspace"
            fresh.mkdir()
            (fresh / "abc-123.jsonl").write_text(
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "u1",
                        "message": {"role": "user", "content": "captured"},
                    }
                )
                + "\n"
            )

        _stats, sink = _drain_once(cast(Adapter, ClaudeAdapter()), tmp_path, write)

        texts = [cast(UserMessage, e.message).text for _, _, e in sink.events]
        assert texts == ["captured"], "a first-ever run captured nothing"


class TestWholeFileDrain:
    """A whole-file adapter (gemini) must receive the entire file, re-read."""

    def test_in_place_rewrite_emits_event(self, tmp_path: Path) -> None:
        adapter = _WholeFileAdapter(tmp_path)
        log = tmp_path / "session-x.json"

        stats, sink = _drain_once(
            cast(Adapter, adapter),
            tmp_path,
            lambda: log.write_text(json.dumps({"messages": ["hello"]})),
        )

        assert stats.counts == {"UserMessage": 1}
        texts = [cast(UserMessage, e.message).text for _, _, e in sink.events]
        assert texts == ["hello"]

    def test_same_size_rewrite_emits_event(self, tmp_path: Path) -> None:
        """A rewrite to identical byte size still emits.

        Gemini rewrites one JSON object in place; a same-length edit leaves
        ``st_size`` unchanged, so a size-only check would drop the new turn.
        """
        adapter = _WholeFileAdapter(tmp_path)
        log = tmp_path / "session-x.json"

        def write() -> None:
            log.write_text(json.dumps({"messages": ["a"]}))
            time.sleep(0.3)
            log.write_text(json.dumps({"messages": ["b"]}))

        _stats, sink = _drain_once(cast(Adapter, adapter), tmp_path, write, expected=2)

        texts = [cast(UserMessage, e.message).text for _, _, e in sink.events]
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

        def write() -> None:
            log.write_text(body)
            time.sleep(0.3)
            log.write_text(body)  # identical bytes: no new turn
            time.sleep(0.3)  # let the drain deliver whatever it captured

        _stats, sink = _drain_once(cast(Adapter, adapter), tmp_path, write)

        texts = [cast(UserMessage, e.message).text for _, _, e in sink.events]
        assert texts == ["b"]

    def test_a_body_that_returns_after_a_change_is_emitted_again(
        self, tmp_path: Path
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

        def write() -> None:
            for text in ("a", "b", "a"):
                log.write_text(json.dumps({"messages": [text]}))
                time.sleep(0.3)

        _stats, sink = _drain_once(cast(Adapter, adapter), tmp_path, write, expected=3)

        texts = [cast(UserMessage, e.message).text for _, _, e in sink.events]
        assert texts == ["a", "b", "a"], "a returning body was swallowed"


class TestGeminiMultiFileDrain:
    """One GeminiAdapter draining several session files keeps cursors apart.

    #498: the runner reuses ONE adapter across every matching session file. A
    per-adapter message cursor carried one file's count into the next, so a
    second gemini session file's turns were dropped.
    """

    def test_two_session_files_both_fully_drained(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        chats = tmp_path / ".gemini" / "tmp" / "deadbeef" / "chats"
        chats.mkdir(parents=True)

        def _session(name: str, session_id: str, msgs: list[str]) -> None:
            (chats / name).write_text(
                json.dumps(
                    {
                        "sessionId": session_id,
                        "messages": [{"type": "user", "content": m} for m in msgs],
                    }
                )
            )

        def write() -> None:
            _session("session-1.json", "sess-A", ["a-q", "a-r"])
            _session("session-2.json", "sess-B", ["b-q", "b-r"])

        _stats, sink = _drain_once(
            cast(Adapter, GeminiAdapter()), tmp_path, write, expected=4
        )

        texts = sorted(cast(UserMessage, e.message).text for _, _, e in sink.events)
        assert texts == ["a-q", "a-r", "b-q", "b-r"]


class TestDrainSurvivesParseError:
    """A parser exception on one line must not stop capture for the rest."""

    def test_bad_line_is_skipped_and_drain_continues(self, tmp_path: Path) -> None:
        log = tmp_path / "session.jsonl"
        adapter = _PoisonAdapter(tmp_path)

        stats, sink = _drain_once(
            cast(Adapter, adapter),
            tmp_path,
            lambda: log.write_bytes(b"alpha\nboom\nomega\n"),
            expected=2,
        )

        # The poison line raised but was swallowed; the good lines emitted.
        texts = [cast(UserMessage, e.message).text for _, _, e in sink.events]
        assert texts == ["alpha", "omega"]
        assert stats.counts == {"UserMessage": 2}

    def test_parse_failure_logs_with_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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

        monkeypatch.setattr(session_mod._logger, "warning", record_warning)

        _process_chunk(
            b"boom",
            cast(Adapter, adapter),
            sink,
            _Stats(),
            RunConfig(cli_name="poison"),
            whole_file=False,
        )

        assert calls == [
            ("trax run: %s adapter failed to parse a chunk", ("poison",), True)
        ]


class _FlakyFlushSink(_RecordingSink):
    """Records events, but ``flush`` raises a transient error a fixed number of
    times before succeeding, to drive the drain loop's resilience.
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
        queue: deque[Event] = deque(maxlen=session_mod._STREAM_QUEUE_MAX)
        overfill = session_mod._STREAM_QUEUE_MAX + 1_000
        for i in range(overfill):
            queue.append(Event(message=UserMessage(text=str(i))))
        assert len(queue) == session_mod._STREAM_QUEUE_MAX
        # Oldest dropped, newest kept.
        newest = queue[-1].message
        assert isinstance(newest, UserMessage)
        assert newest.text == str(overfill - 1)

    def test_spawn_constructs_a_bounded_queue(self) -> None:
        """The runner's queue literal must carry the cap, not a bare deque.

        Guards the construction site: a bare ``deque()`` reintroduces the
        unbounded handoff even with the constant still defined.
        """
        source = inspect.getsource(session_mod._spawn_and_drain)
        assert "deque(maxlen=_STREAM_QUEUE_MAX)" in source

    def test_overflow_is_counted_and_warned(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An eviction must be visible: stats counter + one WARN per run.

        A silent drop is a quiet gap in the transcript; the counter surfaces
        in the end-of-run stats line and the WARN names the stall.
        """
        queue: deque[Event] = deque(maxlen=2)
        stats = _Stats()
        with caplog.at_level("WARNING"):
            for i in range(5):
                session_mod._enqueue_stream_event(
                    queue, stats, Event(message=UserMessage(text=str(i)))
                )
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

        class _FastPollingStop(threading.Event):
            """Preserve poll boundaries without paying the production interval."""

            @override
            def wait(self, timeout: float | None = None) -> bool:
                return super().wait(
                    None if timeout is None else min(timeout, 0.001),
                )

        stop = _FastPollingStop()
        slash_queue: deque[tuple[SlashCommand, datetime]] = deque()

        # Written AFTER the drain arms its watch, as a real session file is:
        # the runner starts watching before it spawns the CLI, and a file that
        # predates the watch belongs to an earlier run.
        log = tmp_path / "session.jsonl"

        def _run() -> None:
            _drain_filesystem_loop(
                cast(Adapter, adapter),
                sink,
                stats,
                config,
                stop,
                baseline=frozenset(),
                slash_queue=slash_queue,
            )

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        # Each retry APPENDS rather than rewriting: a repeated identical
        # rewrite leaves the byte length unchanged, so the follower's cursor
        # correctly reports nothing new and the line would never arrive.
        deadline = time.monotonic() + 5.0
        while not sink.events and time.monotonic() < deadline:
            with log.open("ab") as handle:
                _ = handle.write(b"alpha\n")
            time.sleep(0.05)
        assert sink.events, "the first line never reached the sink"
        assert sink.flush_attempts > 0, "the failing flush never fired"
        before = len(sink.events)

        with log.open("ab") as handle:
            _ = handle.write(b"omega\n")
        deadline = time.monotonic() + 5.0
        while len(sink.events) <= before and time.monotonic() < deadline:
            time.sleep(0.005)
        stop.set()
        worker.join(timeout=5.0)

        assert not worker.is_alive(), "drain thread died on the transient flush error"
        texts = [cast(UserMessage, e.message).text for _, _, e in sink.events]
        # A retry may have appended ``alpha`` more than once; what the flush
        # failure must not do is stop the line written after it.
        assert texts[0] == "alpha"
        assert texts[-1] == "omega", (
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
                cast(Adapter, adapter),
                sink,
                stats,
                config,
                stop,
                baseline=frozenset(),
                slash_queue=deque(),
            )

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(time, "sleep", watched_sleep)
            worker = threading.Thread(target=_run, daemon=True)
            worker.start()
            try:
                real_sleep(0.2)  # let the watch arm before the write
                (tmp_path / "session.jsonl").write_bytes(b"alpha\n")
                deadline = time.monotonic() + 5.0
                while not sink.events and time.monotonic() < deadline:
                    real_sleep(0.01)
            finally:
                stop.set()
                worker.join(timeout=5.0)

        texts = [cast(UserMessage, e.message).text for _, _, e in sink.events]
        assert texts == ["alpha"]
        assert slept_in_drain == [], (
            f"the drain slept {slept_in_drain}; it must wake on a write"
        )

    def test_does_not_walk_every_session_directory(self, tmp_path: Path) -> None:
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

        adapter.session_dirs = counting_dirs  # ty: ignore[invalid-assignment]
        sink = _RecordingSink()
        stop = threading.Event()

        def _run() -> None:
            _drain_filesystem_loop(
                cast(Adapter, adapter),
                sink,
                _Stats(),
                RunConfig(cli_name="fake"),
                stop,
                baseline=frozenset(),
                slash_queue=deque(),
            )

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        try:
            log = tmp_path / "session.jsonl"
            # APPEND, never rewrite: an identical rewrite leaves the byte
            # length unchanged, so the cursor rightly reports nothing new.
            # Retry the first line until it lands, rather than guessing how
            # long arming the watch takes.
            deadline = time.monotonic() + 5.0
            while not sink.events and time.monotonic() < deadline:
                with log.open("ab") as handle:
                    _ = handle.write(b"one\n")
                time.sleep(0.05)
            assert sink.events, "the first line never reached the sink"
            delivered = len(sink.events)
            for text in (b"two\n", b"three\n"):
                with log.open("ab") as handle:
                    _ = handle.write(text)
                deadline = time.monotonic() + 3.0
                while len(sink.events) <= delivered and time.monotonic() < deadline:
                    time.sleep(0.01)
                delivered = len(sink.events)
        finally:
            stop.set()
            worker.join(timeout=5.0)

        texts = [cast(UserMessage, e.message).text for _, _, e in sink.events]
        assert texts[-2:] == ["two", "three"], "not every line was captured"
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
        log = tmp_path / "session-x.json"

        def _run() -> None:
            _drain_filesystem_loop(
                cast(Adapter, adapter),
                sink,
                _Stats(),
                RunConfig(cli_name="wholefile"),
                stop,
                baseline=frozenset(),
                slash_queue=deque(),
            )

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        try:
            # Pretty-printed across several lines, as gemini writes it: no
            # single line is valid JSON on its own.
            body = json.dumps({"messages": ["hello"]}, indent=2)
            deadline = time.monotonic() + 5.0
            while not sink.events and time.monotonic() < deadline:
                log.write_text(body + "\n")
                time.sleep(0.05)
        finally:
            stop.set()
            worker.join(timeout=5.0)

        texts = [cast(UserMessage, e.message).text for _, _, e in sink.events]
        assert texts == ["hello"], "a whole-file session was drained line-by-line"


class TestCompactionDoesNotReEmit:
    """A compacted transcript must not replay turns already captured.

    Claude does not append on compaction -- it REWRITES the session file,
    smaller, retaining the turns it kept. A byte cursor sees the shrink,
    restarts from zero, and re-emits every retained line as new. The server
    cannot dedup it either: ``seq`` is minted per read, so a re-read line
    arrives with a fresh ordinal and lands as a distinct row.
    """

    def test_retained_lines_are_not_emitted_twice(self, tmp_path: Path) -> None:
        adapter = _UuidAdapter(tmp_path)
        sink = _RecordingSink()
        stop = threading.Event()
        log = tmp_path / "session.jsonl"

        def _run() -> None:
            _drain_filesystem_loop(
                cast(Adapter, adapter),
                sink,
                _Stats(),
                RunConfig(cli_name="uuids"),
                stop,
                baseline=frozenset(),
                slash_queue=deque(),
            )

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 5.0
            while not sink.events and time.monotonic() < deadline:
                with log.open("ab") as handle:
                    _ = handle.write(_uuid_line("a"))
                time.sleep(0.05)
            assert sink.events, "the first line never reached the sink"
            with log.open("ab") as handle:
                _ = handle.write(_uuid_line("b") + _uuid_line("c"))
            deadline = time.monotonic() + 3.0
            while len(sink.events) < 3 and time.monotonic() < deadline:
                time.sleep(0.01)
            captured = len(sink.events)

            # Compaction: the file is REPLACED, smaller, and keeps ``c``.
            log.write_bytes(_uuid_line("c") + _uuid_line("d"))
            deadline = time.monotonic() + 3.0
            while len(sink.events) <= captured and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            stop.set()
            worker.join(timeout=5.0)

        ids = [cast(UserMessage, e.message).text for _, _, e in sink.events]
        assert "d" in ids, "the post-compaction turn was never captured"
        assert ids.count("c") == 1, f"compaction re-emitted a captured turn: {ids}"


def _uuid_line(marker: str) -> bytes:
    """One claude-shaped record whose uuid is stable for ``marker``."""
    return (
        json.dumps(
            {
                "type": "user",
                "uuid": f"uuid-{marker}",
                "message": {"role": "user", "content": marker},
            }
        )
        + "\n"
    ).encode()


class _UuidAdapter:
    """A line adapter over uuid-stamped records, like claude's."""

    name: str = "uuids"
    cli_binary: str = "uuids"
    whole_file: bool = False

    def __init__(self, root: Path) -> None:
        self._root = root

    def session_dirs(self) -> Iterable[Path]:
        return (self._root,)

    def matches_session_file(self, path: Path) -> bool:
        return path.suffix == ".jsonl"

    def session_id_from_path(self, path: Path) -> str | None:
        del path
        return None

    def parse(self, raw: bytes, *, whole_file: bool) -> Iterable[Event]:
        del whole_file
        obj = cast(dict[str, object], json.loads(raw))
        message = cast(dict[str, object], obj["message"])
        return (Event(message=UserMessage(text=str(message["content"]))),)


class TestAdapterRegistryFreshPerRun:
    """Each run gets a fresh adapter so per-run state never leaks across runs."""

    def test_registry_builds_a_fresh_adapter_each_call(self) -> None:
        """The codex adapter carries per-run ``_last_model`` state; two runs in
        one process (tests, a future supervisor) must not share it. The registry
        holds a factory, so each lookup yields a distinct instance.
        """
        factory = session_mod._ADAPTERS["codex"]
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
            cli_name=adapter.name, sync=False, out_path=tmp_path / "o.jsonl"
        )
        session_mod._ADAPTERS[adapter.name] = lambda: cast(Adapter, adapter)
        try:
            with pytest.raises(SystemExit, match="not found in PATH"):
                run(config)
        finally:
            session_mod._ADAPTERS.pop(adapter.name, None)


class TestRoutingEnv:
    """The routing identity exported into the wrapped CLI's environment."""

    def test_actor_and_rooms_exported(self) -> None:
        env = _routing_env(
            RunConfig(cli_name="codex", actor="scientist", rooms=("sear", "lab"))
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
            RunConfig(cli_name="codex", actor="scientist"), granted_actor=None
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
            ]
        )
        _emit_slash_commands(
            cast(Adapter, _FakeAdapter(Path())),
            sink,
            stats,
            RunConfig(cli_name="fake"),
            queue,
        )
        kinds = [ev.kind for _seq, _name, ev in sink.events]
        assert kinds == ["SlashCommand", "SlashCommand"]
        assert not queue  # fully drained
        first = sink.events[0][2]
        assert isinstance(first.message, SlashCommand)
        assert first.message.command == "exit"
        # The submit-time clock the detector stamped rides onto the event.
        assert first.timestamp == at

    def test_empty_queue_is_a_noop(self) -> None:
        sink = _RecordingSink()
        _emit_slash_commands(
            cast(Adapter, _FakeAdapter(Path())),
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
        rc = session_mod._dry_run_drain(
            RunConfig(cli_name="wholefile"),
            cast(Adapter, adapter),
            sink,
            stats,
            stop=stop,
        )
        assert rc == 0
        texts = [cast(UserMessage, e.message).text for _, _, e in sink.events]
        assert texts == ["replayed"]

    def test_returns_when_stopped(self, tmp_path: Path) -> None:
        """The dry-run loop exits promptly once ``stop`` is set (no spin)."""
        adapter = _FakeAdapter(tmp_path)
        stop = threading.Event()
        stop.set()  # already stopped: the loop runs one final sweep and returns
        rc = session_mod._dry_run_drain(
            RunConfig(cli_name="fake"),
            cast(Adapter, adapter),
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

    def test_run_leaves_config_client_open(self) -> None:
        """The CLI cache, not one run, owns the supplied transport.

        The daemon reuses this client across requests. Closing it here leaves
        the closed instance cached, so the next invocation cannot send.
        """
        client = _BorrowedClient()
        config = RunConfig(cli_name="codex", dry_run=True, client=cast(Any, client))

        # Stub the drain so no session files are scanned and the run returns at
        # once; the client ownership boundary is the only thing under test.
        def _fake_dry_run(*_a: object, **_k: object) -> int:
            return 0

        original = session_mod._dry_run_drain
        session_mod._dry_run_drain = cast(Any, _fake_dry_run)
        try:
            rc = run(config)
        finally:
            session_mod._dry_run_drain = original
        assert rc == 0
        assert client.close_calls == 0


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
                cast(Any, client),
                cast(Any, _SessionSink()),
                cast(Any, _RecordingRelay()),
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
                cast(Any, client),
                cast(Any, _SessionSink()),
                cast(Any, _RecordingRelay()),
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
        """A persistent outage must not become a hot retry loop."""
        client = _FailingClient()
        stop = threading.Event()

        worker = threading.Thread(
            target=lambda: _inbound_poll_loop(
                cast(Any, client),
                cast(Any, _SessionSink()),
                cast(Any, _RecordingRelay()),
                stop,
                poll_interval=0.05,
            ),
            daemon=True,
        )
        worker.start()
        try:
            time.sleep(0.3)
        finally:
            stop.set()
            worker.join(timeout=5.0)

        # Bounded by the backoff, not spinning: ~6 at 0.05s, not hundreds.
        assert 1 <= client.attempts <= 30, f"retried {client.attempts} times in 0.3s"


class _SessionSink:
    """A sink whose session is already open, so inbound has an id to use."""

    session_id = uuid.UUID("11111111-2222-3333-4444-555555555555")


class _RecordingRelay:
    """Records what would have been typed into the CLI."""

    def __init__(self) -> None:
        self.submitted: list[str] = []

    def submit(self, text: str) -> None:
        self.submitted.append(text)


def _busy_wait(seconds: float) -> None:
    """Block without ``time.sleep``, so a sleep assertion stays meaningful."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        pass


class _WaitingClient:
    """A client whose drain holds, as the real long-poll route does."""

    def __init__(self, *, hold_sec: float) -> None:
        self._hold_sec = hold_sec
        self.waits: list[float] = []

    def drain_inbound(
        self, session_id: uuid.UUID, *, wait_sec: float = 0.0
    ) -> list[tuple[str, str | None, str | None]]:
        del session_id
        self.waits.append(wait_sec)
        # A real hold blocks in the transport, not in ``time.sleep``: a fake
        # that slept here would be indistinguishable from the loop sleeping,
        # which is the very thing the caller asserts about.
        _busy_wait(self._hold_sec)
        return []


class _FailingClient:
    """A client whose drain always raises, to drive the backoff path."""

    def __init__(self) -> None:
        self.attempts = 0

    def drain_inbound(
        self, session_id: uuid.UUID, *, wait_sec: float = 0.0
    ) -> list[tuple[str, str | None, str | None]]:
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


_ENVELOPE = json.dumps(
    {
        "agent_message": "FYI: trax issue 42 status changed (by bob)",
        "id": "29b5982f-2e1f-4749-9bb6-fe601444282c",
        "kind": "status",
        "subject_ref": "issue 42",
        "row": "trax issue 42",
    }
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


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
