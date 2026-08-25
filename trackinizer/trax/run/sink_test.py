"""Tests for the run sinks: FileSink JSONL shape + TrackinizerSink batching."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast, override
from uuid import UUID, uuid4

import contextlib
import io
import json
import threading

from trackinizer.client.client import Client
from trackinizer.trax.run.adapters.base import Event
from trackinizer.trax.run.sink import (
    FileSink,
    LockedSink,
    ResilientSink,
    Sink,
    TrackinizerSink,
)
from trackinizer.types.agent_session_events import (
    AssistantMessage,
    Message,
    ToolCall,
    UserMessage,
)
from trackinizer.wire.wire_sessions import (
    AppendEventsResponse,
    EventBody,
    SessionEnd,
    SessionEndResponse,
    SessionStart,
    SessionStartResponse,
)


def _event(message: Message) -> Event:
    return Event(message=message)


class TestFileSink:
    def test_writes_one_json_line_per_event(self) -> None:
        buf = io.StringIO()
        sink = FileSink(buf)
        sink.emit("codex", _event(UserMessage(text="hi")))
        sink.emit(
            "codex",
            _event(AssistantMessage(tool_calls=(ToolCall(id="t1", name="Read"),))),
        )
        lines = buf.getvalue().splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        # The sink mints seq per session, from 0.
        assert first["seq"] == 0
        assert json.loads(lines[1])["seq"] == 1
        assert first["kind"] == "UserMessage"
        assert first["adapter"] == "codex"
        assert first["message"]["text"] == "hi"


class _FakeClient:
    """Records session API calls without touching the network."""

    def __init__(self) -> None:
        self.started: list[SessionStart] = []
        self.appended: list[tuple[UUID, list[EventBody]]] = []
        self.ended: list[UUID] = []
        self.end_bodies: list[SessionEnd | None] = []
        self.granted_actor: str | None = None
        # The event-log continuation seq the server reports on start: 0 for a
        # fresh session, max(seq)+1 for a resumed one.
        self.start_seq = 0
        self._id = uuid4()

    def session_start(self, body: SessionStart) -> SessionStartResponse:
        self.started.append(body)
        return SessionStartResponse(
            id=self._id, seq=self.start_seq, actor=self.granted_actor or body.actor
        )

    def append_events(self, session_id: UUID, events: object) -> AppendEventsResponse:
        evs = list(cast(list[EventBody], events))
        self.appended.append((session_id, evs))
        return AppendEventsResponse(appended=len(evs), skipped=0)

    def session_end(
        self, session_id: UUID, body: SessionEnd | None = None
    ) -> SessionEndResponse:
        self.ended.append(session_id)
        self.end_bodies.append(body)
        return SessionEndResponse(id=session_id)


class TestFileSinkWriteBody:
    """``write_body`` (replay) must keep the seq counter past replayed bodies."""

    def test_emit_after_write_body_does_not_duplicate_seq(self) -> None:
        """A replayed body's seq must advance the counter so emit won't collide.

        REV-001: ``write_body`` preserves a pre-stamped seq but never advanced
        ``_next_seq``; after ``ResilientSink._degrade`` replays buffered bodies
        (seqs 0, 1, ...), the fallback's own ``emit`` re-minted from 0, so the
        replayed and freshly-emitted events shared seqs -- the server's
        ``(session_id, seq)`` PK then silently dropped one of each pair.
        """
        buf = io.StringIO()
        sink = FileSink(buf)
        # Replay two pre-stamped bodies (as a degrading primary's drain would).
        sink.write_body("claude", EventBody(seq=0, kind="UserMessage", message={}))
        sink.write_body("claude", EventBody(seq=1, kind="UserMessage", message={}))
        # A subsequent live emit must continue at seq 2, not re-mint from 0.
        sink.emit("claude", _event(UserMessage(text="after replay")))
        seqs = [json.loads(line)["seq"] for line in buf.getvalue().splitlines()]
        assert seqs == [0, 1, 2]


class TestTrackinizerSink:
    def test_lazy_start_batch_and_end(self) -> None:
        client = _FakeClient()
        sink = TrackinizerSink(cast(Client, client), "codex", batch_size=2)
        # No events yet -> no session opened.
        assert client.started == []

        sink.emit("codex", _event(UserMessage(text="hi")))
        # First event opens the session, naming its CLI.
        assert len(client.started) == 1
        assert client.started[0].cli == "codex"

        sink.emit("codex", _event(AssistantMessage(text="ok")))
        # batch_size=2 reached -> one flush of 2 events.
        assert len(client.appended) == 1
        assert [e.seq for e in client.appended[0][1]] == [0, 1]

        sink.emit("codex", _event(AssistantMessage(text="done")))
        sink.close()
        # close() flushes the remainder and ends the session.
        assert len(client.appended) == 2
        assert client.appended[1][1][0].seq == 2
        assert client.ended == [client._id]

    def test_no_events_opens_no_session(self) -> None:
        client = _FakeClient()
        sink = TrackinizerSink(cast(Client, client), "claude")
        sink.close()
        assert client.started == []
        assert client.ended == []

    def test_actor_sent_on_start_and_granted_name_adopted(self) -> None:
        """``--as`` rides on ``start``; the sink adopts the granted name.

        On a live collision the server returns a suffixed name; the sink must
        surface it via ``granted_actor`` so the routing handle is correct.
        """
        client = _FakeClient()
        client.granted_actor = "scientist#2"  # server renegotiated
        sink = TrackinizerSink(cast(Client, client), "claude", actor="scientist")
        sink.emit("claude", _event(UserMessage(text="hi")))
        assert client.started[0].actor == "scientist"
        assert sink.granted_actor == "scientist#2"

    def test_granted_actor_is_none_before_session_opens(self) -> None:
        client = _FakeClient()
        sink = TrackinizerSink(cast(Client, client), "claude", actor="scientist")
        assert sink.granted_actor is None

    def test_open_eagerly_returns_granted_handle(self) -> None:
        """``open`` opens the session before any event and returns the handle.

        Eager open lets the runner export the server-granted routing handle
        into the child env before fork (#453).
        """
        client = _FakeClient()
        client.granted_actor = "scientist#2"
        sink = TrackinizerSink(cast(Client, client), "claude", actor="scientist")
        granted = sink.open()
        assert granted == "scientist#2"
        assert len(client.started) == 1  # opened without an event
        assert sink.granted_actor == "scientist#2"

    def test_open_seeds_seq_from_resume_continuation(self) -> None:
        """On resume the server reports max(seq)+1; the sink continues there.

        Without seeding, a resumed run would re-mint seq 0 and the server's
        ``(session_id, seq)`` PK would silently drop the whole resumed log.
        """
        client = _FakeClient()
        client.start_seq = 5  # server: resumed log continues at seq 5
        sink = TrackinizerSink(cast(Client, client), "claude")
        sink.open()
        sink.emit("claude", _event(UserMessage(text="resumed")))
        sink.close()
        # The first appended event continues at the resume seq, not 0.
        assert client.appended[0][1][0].seq == 5

    def test_open_is_noop_on_file_sink(self) -> None:
        """A local FileSink has no server session: ``open`` returns None."""
        sink = FileSink(io.StringIO())
        assert sink.open() is None

    def test_cli_session_id_backfilled_at_close(self) -> None:
        """A mid-run-discovered CLI session id is sent on ``close`` for resume.

        A fresh claude run only learns its ``sessionId`` after it starts, so the
        session opens with none; the sink carries the id to ``end`` so the
        session becomes correlatable on the next ``--resume``.
        """
        client = _FakeClient()
        sink = TrackinizerSink(cast(Client, client), "claude")
        sink.open()
        sink.set_cli_session_id("claude-abc-123")
        sink.close()
        assert client.end_bodies[0] is not None
        assert client.end_bodies[0].cli_session_id == "claude-abc-123"

    def test_set_cli_session_id_is_noop_on_file_sink(self) -> None:
        """A local FileSink has no server session id to backfill."""
        sink = FileSink(io.StringIO())
        sink.set_cli_session_id("x")  # must not raise

    def test_flush_holds_until_interval_then_sends(self) -> None:
        """A partial buffer streams once it ages past ``flush_interval_sec``.

        This is the live-streaming fix: a short session (below ``batch_size``)
        must not withhold events until ``close``; the drain loop's periodic
        ``flush`` sends them once the oldest buffered event is old enough.
        """
        now = [100.0]
        client = _FakeClient()
        sink = TrackinizerSink(
            cast(Client, client),
            "claude",
            batch_size=50,
            flush_interval_sec=1.0,
            clock=lambda: now[0],
        )
        sink.emit("claude", _event(UserMessage(text="hi")))

        # Too soon: the buffer is younger than the interval, so flush no-ops.
        now[0] = 100.5
        sink.flush()
        assert client.appended == []

        # Past the interval: the partial buffer is sent.
        now[0] = 101.0
        sink.flush()
        assert len(client.appended) == 1
        assert [e.seq for e in client.appended[0][1]] == [0]

    def test_flush_on_empty_buffer_is_noop(self) -> None:
        client = _FakeClient()
        sink = TrackinizerSink(cast(Client, client), "claude")
        sink.flush()
        # No events buffered -> no session opened, nothing sent.
        assert client.started == []
        assert client.appended == []

    def test_flush_resets_buffer_age_so_next_batch_waits(self) -> None:
        """After a timed flush, a freshly buffered event restarts the clock."""
        now = [0.0]
        client = _FakeClient()
        sink = TrackinizerSink(
            cast(Client, client),
            "claude",
            batch_size=50,
            flush_interval_sec=1.0,
            clock=lambda: now[0],
        )
        sink.emit("claude", _event(UserMessage(text="a")))
        now[0] = 1.0
        sink.flush()
        assert len(client.appended) == 1

        # A new event right after the flush is young again -> held.
        sink.emit("claude", _event(UserMessage(text="b")))
        now[0] = 1.5
        sink.flush()
        assert len(client.appended) == 1
        # Aged past the interval from its own arrival -> sent.
        now[0] = 2.0
        sink.flush()
        assert len(client.appended) == 2
        assert [e.seq for e in client.appended[1][1]] == [1]

    def test_seq_is_per_session_not_global(self) -> None:
        """Each session numbers its events from 0; the counter is per-sink.

        ``seq`` is the per-session dedup key, so two sessions must not share a
        run-global counter (which would start the second session mid-sequence
        and break ``(session_id, seq)`` uniqueness across resends).
        """
        client_a = _FakeClient()
        first = TrackinizerSink(cast(Client, client_a), "codex", batch_size=1)
        first.emit("codex", _event(UserMessage(text="a")))
        first.emit("codex", _event(UserMessage(text="b")))

        client_b = _FakeClient()
        second = TrackinizerSink(cast(Client, client_b), "codex", batch_size=1)
        second.emit("codex", _event(UserMessage(text="c")))

        seqs_first = [e.seq for _, evs in client_a.appended for e in evs]
        seqs_second = [e.seq for _, evs in client_b.appended for e in evs]
        assert seqs_first == [0, 1]
        # The second session restarts at 0, not 2.
        assert seqs_second == [0]

    def test_close_stamps_ended_timestamp(self) -> None:
        """``close`` must record when the session ended, not a bare ``None``."""
        client = _FakeClient()
        sink = TrackinizerSink(cast(Client, client), "codex")
        sink.emit("codex", _event(UserMessage(text="hi")))
        sink.close()
        assert len(client.end_bodies) == 1
        body = client.end_bodies[0]
        assert body is not None
        assert body.ended is not None


class _FlakyFlushClient(_FakeClient):
    """First ``append_events`` raises; later calls succeed, to test retry."""

    def __init__(self) -> None:
        super().__init__()
        self.append_attempts = 0

    @override
    def append_events(self, session_id: UUID, events: object) -> AppendEventsResponse:
        self.append_attempts += 1
        if self.append_attempts == 1:
            raise RuntimeError("transient flush failure")
        return super().append_events(session_id, events)


class TestTrackinizerSinkCloseFlushOrdering:
    """A flush failure during ``close`` must not silently drop the buffer."""

    def test_failed_flush_keeps_buffer_for_retry(self) -> None:
        client = _FlakyFlushClient()
        sink = TrackinizerSink(cast(Client, client), "codex")
        sink.emit("codex", _event(UserMessage(text="hi")))

        # First close flushes, the flush raises; the event is not yet lost.
        with contextlib.suppress(RuntimeError):
            sink.close()

        # A retried close re-attempts the flush and the buffered event lands.
        sink.close()
        assert client.append_attempts == 2
        assert [e.seq for ev in client.appended for e in ev[1]] == [0]
        # Both closes end the session: ``session_end`` runs in the finally so
        # a flush failure can never leave the session live (phantom-session
        # guard); the duplicate end is idempotent server-side.
        assert client.ended == [client._id, client._id]

    def test_close_ends_session_even_when_flush_fails(self, tmp_path: Path) -> None:
        """A flush failure at close must not leave the session live forever.

        ``ResilientSink.close`` catches the primary's failure and degrades --
        there is no retried close in production. If ``session_end`` only runs
        after a successful flush, ``agentsession_ended`` stays NULL, so
        ``resolve_live_sessions`` returns the row forever and the subscriber
        push keeps feeding a queue nobody drains (a phantom live session).
        """
        client = _FlakyFlushClient()
        primary = TrackinizerSink(cast(Client, client), "claude")
        sink = ResilientSink(primary, fallback_path=tmp_path / "fb.jsonl")
        sink.emit("claude", _event(UserMessage(text="one")))

        sink.close()

        assert client.ended == [client._id], (
            "session_end never ran; the server session stays live (phantom)"
        )
        # The buffered event still reached the fallback (REV-02 preserved).
        fallback = (tmp_path / "fb.jsonl").read_text()
        assert "one" in fallback


class TestDrainPendingIsOnTheProtocol:
    """``drain_pending`` must be part of the ``Sink`` contract, not a getattr.

    ``ResilientSink._degrade`` previously reached it dynamically
    (``getattr(primary, "drain_pending", None)`` + an unchecked cast): a
    rename would type-check clean and silently re-open REV-02 (buffered
    events lost on degrade). On the Protocol, a rename is a type error.
    """

    def test_file_sink_has_empty_drain(self) -> None:
        sink = FileSink(io.StringIO())
        assert sink.drain_pending() == []

    def test_protocol_declares_drain_pending(self) -> None:
        assert hasattr(Sink, "drain_pending"), (
            "drain_pending is not on the Sink Protocol; _degrade must be "
            "reaching it via getattr, which a rename silently breaks"
        )


class _ExplodingSink:
    """A sink whose ``emit`` always raises, to drive the fallback path."""

    def __init__(self) -> None:
        self.emit_attempts = 0
        self.closed = False

    @property
    def session_id(self) -> None:
        return None

    def emit(self, adapter_name: str, event: Event) -> None:
        del adapter_name, event
        self.emit_attempts += 1
        raise RuntimeError("server exploded")

    def flush(self) -> None:
        pass

    def drain_pending(self) -> list[EventBody]:
        return []

    def close(self) -> None:
        self.closed = True


class _FailingOpenPrimary:
    """A primary sink whose ``open`` raises, to drive the eager-open degrade.

    Mirrors a ``TrackinizerSink`` against an unreachable server: opening the
    session before fork raises, and the wrapper must degrade rather than let
    the run abort. ``session_id`` is None (no session ever opened).
    """

    def __init__(self) -> None:
        self.open_attempts = 0
        self.emit_attempts = 0
        self.closed = False

    @property
    def session_id(self) -> None:
        return None

    def open(self) -> str | None:
        self.open_attempts += 1
        raise RuntimeError("server unreachable")

    def set_cli_session_id(self, cli_session_id: str) -> None:
        del cli_session_id

    def emit(self, adapter_name: str, event: Event) -> None:
        del adapter_name, event
        self.emit_attempts += 1

    def flush(self) -> None:
        pass

    def drain_pending(self) -> list[EventBody]:
        return []

    def close(self) -> None:
        self.closed = True


class TestResilientSink:
    """A failing primary sink must never propagate; it degrades to local file."""

    def test_emit_failure_falls_back_to_file_and_warns_once(
        self,
        tmp_path: Path,
        capsys: object,
    ) -> None:
        primary = _ExplodingSink()
        fallback_path = tmp_path / "fallback.jsonl"
        sink = ResilientSink(cast(Sink, primary), fallback_path=fallback_path)

        # Two emits: the primary explodes on the first; neither raises.
        sink.emit("claude", _event(UserMessage(text="one")))
        sink.emit("claude", _event(AssistantMessage(text="two")))
        sink.close()

        # The primary was tried exactly once, then abandoned (no re-attempt).
        assert primary.emit_attempts == 1
        # Both events landed in the local fallback file as JSONL.
        lines = fallback_path.read_text(encoding="utf-8").splitlines()
        assert [json.loads(line)["seq"] for line in lines] == [0, 1]
        # The user is warned once, on stderr, not flooded per-event.
        err = cast(Any, capsys).readouterr().err
        assert err.count("[trax run]") == 1
        assert "falling back to local capture" in err
        assert str(fallback_path) in err

    def test_degrade_drains_orphaned_primary_buffer(self, tmp_path: Path) -> None:
        """Events buffered in the primary must not be lost when it degrades.

        REV-02: a ``TrackinizerSink`` buffers up to ``batch_size`` events
        before flushing. If the flush fails and the wrapper degrades, those
        already-buffered events were stranded -- only the current event reached
        the fallback. They must be replayed to the local file.
        """
        client = _FlakyFlushClient()  # first append_events raises
        primary = TrackinizerSink(cast(Client, client), "claude", batch_size=50)
        fallback_path = tmp_path / "fallback.jsonl"
        sink = ResilientSink(primary, fallback_path=fallback_path)

        # Three events buffer in the primary (below batch_size, no flush yet).
        sink.emit("claude", _event(UserMessage(text="one")))
        sink.emit("claude", _event(UserMessage(text="two")))
        sink.emit("claude", _event(UserMessage(text="three")))
        assert not fallback_path.exists()  # all still buffered server-side

        # A flush triggers the (failing) append; the wrapper degrades. All
        # three buffered events must end up in the fallback, not just later ones.
        sink.flush()
        sink.close()
        lines = fallback_path.read_text(encoding="utf-8").splitlines()
        texts = [json.loads(line)["message"]["text"] for line in lines]
        assert texts == ["one", "two", "three"]

    def test_open_failure_degrades_to_fallback(self, tmp_path: Path) -> None:
        """A primary whose ``open`` raises must degrade, not abort the run.

        TRAX-REV-008: the runner eagerly calls ``sink.open()`` before spawning
        the child CLI. An unreachable server raised there with no guard,
        aborting the whole run before the CLI started -- violating the wrapper's
        contract (degrade, never crash capture). ``open`` must swallow the
        failure, switch to the fallback, and keep capturing.
        """
        primary = _FailingOpenPrimary()
        fallback_path = tmp_path / "fallback.jsonl"
        sink = ResilientSink(cast(Sink, primary), fallback_path=fallback_path)

        # Eager open must not propagate; it returns None (no server handle).
        assert sink.open() is None
        assert primary.open_attempts == 1
        # The session id is None -- there is no live server session anymore.
        assert sink.session_id is None

        # Capture still works: a later emit writes to the local fallback file.
        sink.emit("claude", _event(UserMessage(text="after open failed")))
        sink.close()
        lines = fallback_path.read_text(encoding="utf-8").splitlines()
        assert [json.loads(line)["message"]["text"] for line in lines] == [
            "after open failed"
        ]
        # The primary was abandoned after the open failure: no later calls.
        assert primary.emit_attempts == 0

    def test_healthy_primary_never_touches_fallback(self, tmp_path: Path) -> None:
        primary = _FakeClient()
        wrapped = TrackinizerSink(cast(Client, primary), "claude")
        fallback_path = tmp_path / "unused.jsonl"
        sink = ResilientSink(wrapped, fallback_path=fallback_path)

        sink.emit("claude", _event(UserMessage(text="hi")))
        sink.close()

        # The primary handled everything; no fallback file was created.
        assert primary.started
        assert not fallback_path.exists()


class _BlockingSink:
    """A sink whose ``flush`` blocks until released, logging call spans.

    Records ``("op", "enter"/"exit")`` for each call so a test can prove the
    lock serializes overlapping calls from different threads: a serialized run
    never interleaves a second call's ``enter`` between the first call's
    ``enter`` and ``exit``.
    """

    def __init__(self, release: threading.Event) -> None:
        self._release = release
        self.entered = threading.Event()
        self.log: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    @property
    def session_id(self) -> UUID | None:
        return None

    def _record(self, op: str, phase: str) -> None:
        with self._lock:
            self.log.append((op, phase))

    def emit(self, adapter_name: str, event: Event) -> None:
        del adapter_name, event
        self._record("emit", "enter")
        self._record("emit", "exit")

    def flush(self) -> None:
        self._record("flush", "enter")
        self.entered.set()
        self._release.wait(5.0)
        self._record("flush", "exit")

    def close(self) -> None:
        self._record("close", "enter")
        self._record("close", "exit")


class TestLockedSink:
    """A :class:`LockedSink` serializes cross-thread access to the wrapped sink.

    R2R-024: drain (emit/flush), poll (session_id), and main (close) touch one
    sink concurrently; ``join(timeout=...)`` makes ownership non-binding, so
    ``close`` can race an in-flight ``flush``. The lock makes every Protocol
    method mutually exclusive.
    """

    def test_close_waits_for_in_flight_flush(self) -> None:
        release = threading.Event()
        inner = _BlockingSink(release)
        sink = LockedSink(cast(Sink, inner))

        # A drain thread enters flush and blocks inside it (holding the lock).
        flusher = threading.Thread(target=sink.flush, daemon=True)
        flusher.start()
        assert inner.entered.wait(2.0), "flush never entered the wrapped sink"
        assert inner.log[:1] == [("flush", "enter")]

        # Main thread calls close; without the lock it would run concurrently.
        closer = threading.Thread(target=sink.close, daemon=True)
        closer.start()
        closer.join(timeout=0.01)
        assert closer.is_alive(), "close returned while flush held the lock"
        assert ("close", "enter") not in inner.log, (
            "close must block until the in-flight flush releases the lock"
        )

        release.set()
        flusher.join(timeout=5.0)
        closer.join(timeout=5.0)
        # The flush fully completed before close even started.
        assert inner.log == [
            ("flush", "enter"),
            ("flush", "exit"),
            ("close", "enter"),
            ("close", "exit"),
        ]

    def test_delegates_session_id_and_emit(self) -> None:
        client = _FakeClient()
        inner = TrackinizerSink(cast(Client, client), "codex")
        sink = LockedSink(inner)
        assert sink.session_id is None
        sink.emit("codex", _event(UserMessage(text="hi")))
        assert sink.session_id == client._id
        sink.close()
        assert client.ended == [client._id]

    def test_close_returns_when_worker_wedged_holding_lock(self) -> None:
        """``close`` must not deadlock when a worker is wedged holding the lock.

        TRAX-REV-003: the runner's join watchdog is bounded, so it can return
        with a worker still stuck inside a locked ``flush`` / ``emit`` (a hung
        server POST). A ``close`` that blocked forever on that lock would hang
        ``trax run``. The process is exiting anyway, so a bounded acquire that
        fails skips the locked teardown and returns instead of deadlocking.
        """
        release = threading.Event()
        inner = _BlockingSink(release)
        sink = LockedSink(cast(Sink, inner))
        # Bound the wait short so the test is fast; the wedge outlives it.
        sink._CLOSE_LOCK_TIMEOUT_SEC = 0.02

        # A drain thread enters flush and stays wedged inside it (holding the
        # lock) -- the never-releasing server POST the watchdog could not bound.
        flusher = threading.Thread(target=sink.flush, daemon=True)
        flusher.start()
        assert inner.entered.wait(2.0), "flush never entered the wrapped sink"
        assert inner.log[:1] == [("flush", "enter")]

        # close() on the main path must RETURN despite the held lock, not hang.
        returned = threading.Event()

        def _close() -> None:
            sink.close()
            returned.set()

        closer = threading.Thread(target=_close, daemon=True)
        closer.start()
        assert returned.wait(2.0), "close() deadlocked on the wedged worker's lock"
        # The wedged worker held the lock, so the locked teardown was skipped.
        assert ("close", "enter") not in inner.log

        release.set()
        flusher.join(timeout=5.0)
        closer.join(timeout=5.0)


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
