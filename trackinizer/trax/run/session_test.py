"""Tests for the session runner's file scoping (Bug B regression).

The runner shares one session root with concurrent sessions, so it must
drain only the files the wrapped run creates -- never re-emit lines from
sessions that already existed when the run started.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast, override

import inspect
import json
import os
import threading
import time

import pytest

from trackinizer.trax.run import session as session_mod
from trackinizer.trax.run.adapters.base import Adapter, Event
from trackinizer.trax.run.adapters.gemini import GeminiAdapter
from trackinizer.trax.run.session import (
    RunConfig,
    _drain_filesystem_loop,
    _emit_slash_commands,
    _existing_session_files,
    _process_chunk,
    _render_inbound,
    _routing_env,
    _scan_and_read,
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


def _scan_once(
    adapter: Adapter, baseline: frozenset[Path]
) -> tuple[_Stats, _RecordingSink]:
    sink = _RecordingSink()
    stats = _Stats()
    config = RunConfig(cli_name="fake")
    _scan_and_read(adapter, sink, stats, config, {}, buffers={}, baseline=baseline)
    return stats, sink


class TestSessionScoping:
    def test_baseline_files_are_skipped(self, tmp_path: Path) -> None:
        old = tmp_path / "old.jsonl"
        _write(old, lines=5)
        adapter = _FakeAdapter(tmp_path)

        baseline = _existing_session_files(adapter)
        assert old in baseline

        new = tmp_path / "new.jsonl"
        _write(new, lines=3)

        stats, sink = _scan_once(adapter, baseline)

        # Only the 3 lines of the new file; none of the 5 pre-existing.
        assert stats.counts == {"UserMessage": 3}
        assert len(sink.events) == 3
        # seq is monotonic from 0 across the run.
        assert [seq for seq, _, _ in sink.events] == [0, 1, 2]

    def test_no_new_file_emits_nothing(self, tmp_path: Path) -> None:
        old = tmp_path / "old.jsonl"
        _write(old, lines=4)
        adapter = _FakeAdapter(tmp_path)
        baseline = _existing_session_files(adapter)

        stats, sink = _scan_once(adapter, baseline)

        assert stats.counts == {}
        assert sink.events == []

    def test_file_older_than_spawn_is_not_drained(self, tmp_path: Path) -> None:
        """A non-baseline file that predates spawn belongs to an earlier run (#283).

        The cross-pollination race: run B snapshots its baseline BEFORE fork; a
        concurrent run A's file may already exist on disk but be missed by B's
        snapshot, so it is not in B's baseline. Exclusion alone would make B
        drain A's file. The spawn-time floor closes it: A's file mtime predates
        B's spawn (A started first), so B skips it even though baseline missed
        it.
        """
        adapter = _FakeAdapter(tmp_path)
        # Run A's file: exists on disk, but NOT in B's baseline (B raced past it).
        others = tmp_path / "run_a.jsonl"
        _write(others, lines=5)
        # Stamp A's file in the past, before this run (B) spawned.
        past = time.time() - 60
        os.utime(others, (past, past))
        spawn_time = time.time()
        baseline: frozenset[Path] = frozenset[
            Path
        ]()  # B's snapshot missed run A's file

        sink = _RecordingSink()
        stats = _Stats()
        config = RunConfig(cli_name="fake")
        _scan_and_read(
            adapter,
            sink,
            stats,
            config,
            {},
            buffers={},
            baseline=baseline,
            spawn_time=spawn_time,
        )
        # A's file is older than B's spawn -> not B's -> not drained.
        assert sink.events == []
        assert stats.counts == {}

    def test_file_at_or_after_spawn_is_drained(self, tmp_path: Path) -> None:
        """A file created after spawn (this run's own) IS drained."""
        adapter = _FakeAdapter(tmp_path)
        spawn_time = time.time()
        mine = tmp_path / "mine.jsonl"
        _write(mine, lines=3)  # created now, after spawn_time

        sink = _RecordingSink()
        stats = _Stats()
        config = RunConfig(cli_name="fake")
        _scan_and_read(
            adapter,
            sink,
            stats,
            config,
            {},
            buffers={},
            baseline=frozenset(),
            spawn_time=spawn_time,
        )
        assert stats.counts == {"UserMessage": 3}


class TestAppendedLineDrain:
    def test_rotation_discards_the_previous_files_partial_line(
        self,
        tmp_path: Path,
    ) -> None:
        """A truncated file starts a new byte stream with an empty buffer."""
        log = tmp_path / "session.jsonl"
        log.write_bytes(b"stale-partial")
        adapter = _PoisonAdapter(tmp_path)
        sink = _RecordingSink()
        stats = _Stats()
        config = RunConfig(cli_name="fake")
        offsets: dict[Path, int] = {}
        buffers: dict[Path, bytearray] = {}

        _scan_and_read(
            adapter,
            sink,
            stats,
            config,
            offsets,
            buffers=buffers,
            baseline=frozenset(),
        )
        assert sink.events == []

        log.write_bytes(b"fresh\n")
        _scan_and_read(
            adapter,
            sink,
            stats,
            config,
            offsets,
            buffers=buffers,
            baseline=frozenset(),
        )

        texts = [cast(UserMessage, event.message).text for _, _, event in sink.events]
        assert texts == ["fresh"]


class TestWholeFileDrain:
    """A whole-file adapter (gemini) must receive the entire file, re-read."""

    def test_in_place_rewrite_emits_event(self, tmp_path: Path) -> None:
        log = tmp_path / "session-x.json"
        log.write_text(json.dumps({"messages": ["hello"]}))
        adapter = _WholeFileAdapter(tmp_path)

        # No baseline; the freshly-written whole-file session is in scope.
        stats, sink = _scan_once(adapter, frozenset())

        # The byte-offset drain would feed a partial slice and emit nothing;
        # a whole-file drain hands the full body over and emits one event.
        assert stats.counts == {"UserMessage": 1}
        texts = [cast(UserMessage, e.message).text for _, _, e in sink.events]
        assert texts == ["hello"]

    def test_same_size_rewrite_emits_event(self, tmp_path: Path) -> None:
        """A whole-file rewrite to identical byte size still emits an event.

        Gemini rewrites one JSON object in place; a same-length edit (the new
        message has the same byte count as the old) keeps ``st_size``
        unchanged. Tracking size alone makes the second scan skip the re-read,
        dropping the new turn. The drain must detect the change via mtime or
        content, not size alone.
        """
        log = tmp_path / "session-x.json"
        # Two payloads with the same single-character last message: identical
        # byte length, different content.
        log.write_text(json.dumps({"messages": ["a"]}))
        adapter = _WholeFileAdapter(tmp_path)
        sink = _RecordingSink()
        stats = _Stats()
        config = RunConfig(cli_name="fake")
        stamps: dict[Path, tuple[int, int]] = {}

        _scan_and_read(
            adapter,
            sink,
            stats,
            config,
            {},
            buffers={},
            baseline=frozenset(),
            stamps=stamps,
        )
        assert [cast(UserMessage, e.message).text for _, _, e in sink.events] == ["a"]

        # Same byte length, different content; bump mtime so a time-based
        # detector sees the change even on a coarse-grained filesystem clock.
        first_mtime = log.stat().st_mtime
        log.write_text(json.dumps({"messages": ["b"]}))
        os.utime(log, (first_mtime + 1, first_mtime + 1))
        assert log.stat().st_size == len(json.dumps({"messages": ["a"]}))

        _scan_and_read(
            adapter,
            sink,
            stats,
            config,
            {},
            buffers={},
            baseline=frozenset(),
            stamps=stamps,
        )
        assert [cast(UserMessage, e.message).text for _, _, e in sink.events] == [
            "a",
            "b",
        ]


class TestGeminiMultiFileDrain:
    """One GeminiAdapter draining multiple session files keeps cursors apart.

    #498: the runner reuses ONE adapter across every matching session file.
    A per-adapter message cursor carried one file's count into the next, so a
    second gemini session file's turns were dropped. Drive the real adapter
    through ``_scan_and_read`` over two on-disk gemini session files and assert
    every turn of both surfaces.
    """

    def test_two_session_files_both_fully_drained(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A fake $HOME whose ``.gemini/tmp/<sha>/chats`` holds two session
        # files, each two messages. ``_tmp_dir`` resolves ``Path.home()``.
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

        _session("session-1.json", "sess-A", ["a-q", "a-r"])
        _session("session-2.json", "sess-B", ["b-q", "b-r"])

        adapter = GeminiAdapter()  # ONE adapter for both files, like the runner
        sink = _RecordingSink()
        stats = _Stats()
        config = RunConfig(cli_name="gemini")
        stamps: dict[Path, tuple[int, int]] = {}
        _scan_and_read(
            cast(Adapter, adapter),
            sink,
            stats,
            config,
            {},
            buffers={},
            baseline=frozenset(),
            stamps=stamps,
        )

        texts = sorted(cast(UserMessage, e.message).text for _, _, e in sink.events)
        # All four turns from both files, none dropped by a shared cursor.
        assert texts == ["a-q", "a-r", "b-q", "b-r"]


class TestDrainSurvivesParseError:
    """A parser exception on one line must not stop capture for the rest."""

    def test_bad_line_is_skipped_and_drain_continues(self, tmp_path: Path) -> None:
        log = tmp_path / "session.jsonl"
        log.write_bytes(b"alpha\nboom\nomega\n")
        adapter = _PoisonAdapter(tmp_path)

        # No file existed at baseline, so the whole log is in scope.
        stats, sink = _scan_once(adapter, frozenset())

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

        log = tmp_path / "session.jsonl"
        log.write_bytes(b"alpha\n")

        def _run() -> None:
            _drain_filesystem_loop(
                cast(Adapter, adapter),
                sink,
                stats,
                config,
                stop,
                baseline=frozenset(),
                slash_queue=slash_queue,
                spawn_time=0.0,
            )

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        # Wait for the first (failing) flush, then write a second line that a
        # surviving loop must still capture.
        deadline = time.monotonic() + 3.0
        while sink.flush_attempts == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        log.write_bytes(b"alpha\nomega\n")

        deadline = time.monotonic() + 3.0
        while len(sink.events) < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        stop.set()
        worker.join(timeout=5.0)

        assert not worker.is_alive(), "drain thread died on the transient flush error"
        texts = [cast(UserMessage, e.message).text for _, _, e in sink.events]
        assert texts == ["alpha", "omega"], (
            "a transient flush error stopped capture instead of continuing"
        )


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
