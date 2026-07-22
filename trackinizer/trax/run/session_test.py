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

import json
import logging
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
        obj = cast("dict[str, list[str]]", json.loads(raw))
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
    _scan_and_read(adapter, sink, stats, config, {}, {}, baseline)
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
        baseline: frozenset[Path] = frozenset()  # B's snapshot missed run A's file

        sink = _RecordingSink()
        stats = _Stats()
        config = RunConfig(cli_name="fake")
        _scan_and_read(
            adapter, sink, stats, config, {}, {}, baseline, spawn_time=spawn_time
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
            adapter, sink, stats, config, {}, {}, frozenset(), spawn_time=spawn_time
        )
        assert stats.counts == {"UserMessage": 3}


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
        texts = [cast("UserMessage", e.message).text for _, _, e in sink.events]
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

        _scan_and_read(adapter, sink, stats, config, {}, {}, frozenset(), stamps=stamps)
        assert [cast("UserMessage", e.message).text for _, _, e in sink.events] == ["a"]

        # Same byte length, different content; bump mtime so a time-based
        # detector sees the change even on a coarse-grained filesystem clock.
        first_mtime = log.stat().st_mtime
        log.write_text(json.dumps({"messages": ["b"]}))
        os.utime(log, (first_mtime + 1, first_mtime + 1))
        assert log.stat().st_size == len(json.dumps({"messages": ["a"]}))

        _scan_and_read(adapter, sink, stats, config, {}, {}, frozenset(), stamps=stamps)
        assert [cast("UserMessage", e.message).text for _, _, e in sink.events] == [
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
            {},
            frozenset(),
            stamps=stamps,
        )

        texts = sorted(cast("UserMessage", e.message).text for _, _, e in sink.events)
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
        texts = [cast("UserMessage", e.message).text for _, _, e in sink.events]
        assert texts == ["alpha", "omega"]
        assert stats.counts == {"UserMessage": 2}

    def test_parse_failure_logs_with_traceback(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The swallowed parse error must log a traceback, not an opaque line.

        K6-004: the ``_process_chunk`` except logged a bare message without
        ``exc_info``, so a malformed-output loss gave no stack trace to diagnose
        which adapter path raised. The warning must carry the exception info.
        """
        adapter = _PoisonAdapter(tmp_path)
        sink = _RecordingSink()
        with caplog.at_level(logging.WARNING):
            _process_chunk(
                b"boom",
                cast(Adapter, adapter),
                sink,
                _Stats(),
                RunConfig(cli_name="poison"),
                whole_file=False,
            )
        records = [r for r in caplog.records if "failed to parse" in r.getMessage()]
        assert len(records) == 1
        # The record carries the traceback (exc_info), not just a flat message.
        assert records[0].exc_info is not None


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
        stop = threading.Event()
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
                frozenset(),
                slash_queue,
                0.0,
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
        texts = [cast("UserMessage", e.message).text for _, _, e in sink.events]
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
        rc: list[int] = []

        def _run() -> None:
            rc.append(
                session_mod._dry_run_drain(
                    RunConfig(cli_name="wholefile"),
                    cast(Adapter, adapter),
                    sink,
                    stats,
                    stop=stop,
                )
            )

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        # Let at least one poll happen, then stop; a final sweep still runs.
        time.sleep(0.3)
        stop.set()
        worker.join(timeout=5.0)
        assert not worker.is_alive(), "dry-run loop did not stop"
        assert rc == [0]
        texts = [cast("UserMessage", e.message).text for _, _, e in sink.events]
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


class _ClosingClient:
    """A stand-in client that records whether ``close`` was called.

    Only the surface ``run`` touches on the no-network dry-run path: ``close``.
    The run owns this client, so it must be closed when the run finishes.
    """

    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class TestRunClosesClient:
    """A sync ``trax run`` must close the client it owns (TRAX-REV-002)."""

    def test_run_closes_config_client(self) -> None:
        """The ``httpx.Client`` opened for a run is closed when the run ends.

        The sink flushes and ends the session but never owns the transport, so
        without an explicit close in ``run``'s finally the client (and its
        pooled sockets) leaked. Driven on the dry-run path so no CLI spawns and
        no network is touched -- the close is the only thing under test.
        """
        client = _ClosingClient()
        config = RunConfig(cli_name="codex", dry_run=True, client=cast("Any", client))

        # Stub the drain so no session files are scanned and the run returns at
        # once; the close in ``run``'s finally is the only thing under test.
        def _fake_dry_run(*_a: object, **_k: object) -> int:
            return 0

        original = session_mod._dry_run_drain
        session_mod._dry_run_drain = cast("Any", _fake_dry_run)
        try:
            rc = run(config)
        finally:
            session_mod._dry_run_drain = original
        assert rc == 0
        assert client.close_calls == 1, "run() must close the client it owns"


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


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
