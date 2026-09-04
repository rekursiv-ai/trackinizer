"""Tests for the run sinks: FileSink JSONL shape + TrackinizerSink batching."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast, override
from uuid import UUID, uuid4

import contextlib
import io
import json
import threading

from trackinizer.client.client import Client
from trackinizer.lib.agent.types.sessions import AssistantMessage, ToolCall, UserMessage
from trackinizer.lib.custom_json import DictCodec
from trackinizer.trax.run.adapters.claude import ClaudeAdapter
from trackinizer.trax.run.adapters.iostream import IOStreamAdapter
from trackinizer.trax.run.adapters.tail import Tail
from trackinizer.trax.run.custom_types import Event
from trackinizer.trax.run.sink import (
    FileSink,
    LockedSink,
    ResilientSink,
    Sink,
    TrackinizerSink,
)
from trackinizer.trax.run.slash import SlashCommand
from trackinizer.wire.wire_session_ir import (
    AppendRecordsResponse,
    ManifestBody,
    RecordBody,
    SlashCommandBody,
)
from trackinizer.wire.wire_sessions import (
    SessionEnd,
    SessionEndResponse,
    SessionStart,
    SessionStartResponse,
)


_PART = Path("/sessions/a.jsonl")
_OTHER = Path("/sessions/b.jsonl")
_AT = datetime(2026, 6, 1, tzinfo=UTC)


def _event(content: str, *, path: Path = _PART, restart: bool = False) -> Event:
    """One captured user turn from ``path``."""
    return Event(record=UserMessage(content=content), path=path, restart=restart)


class TestFileSink:
    def test_writes_one_json_line_per_record(self) -> None:
        buf = io.StringIO()
        sink = FileSink(buf)
        sink.emit("codex", _event("hi"))
        sink.emit(
            "codex",
            Event(record=ToolCall(call_id="t1", name="Read"), path=_PART),
        )
        lines = buf.getvalue().splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        # Position within the part, derived and numbered from 0.
        assert first["idx"] == 0
        assert json.loads(lines[1])["idx"] == 1
        assert first["kind"] == "UserMessage"
        assert first["adapter"] == "codex"
        # The BASENAME, matching how the server resolves a part.
        assert first["part_name"] == "a.jsonl"
        assert first["text"] == "hi"

    def test_each_file_numbers_its_records_from_zero(self) -> None:
        """A session spans several files; each is its own part.

        One counter across files would number the second file's first record
        after the first file's last, and the server keys by
        ``(session_id, part, idx)`` -- so the second part would start
        mid-sequence with a hole at its front.
        """
        buf = io.StringIO()
        sink = FileSink(buf)
        sink.emit("claude", _event("a1", path=_PART))
        sink.emit("claude", _event("b1", path=_OTHER))
        sink.emit("claude", _event("a2", path=_PART))
        rows = [json.loads(line) for line in buf.getvalue().splitlines()]
        assert [(r["part_name"], r["idx"]) for r in rows] == [
            ("a.jsonl", 0),
            ("b.jsonl", 0),
            ("a.jsonl", 1),
        ]

    def test_a_restart_renumbers_that_file_from_zero(self) -> None:
        """A rewritten file re-derives its part, so positions restart.

        A claude compaction REPLACES the transcript; the records that follow
        describe the new file from its start, and a counter that kept climbing
        would store them past the rows they are meant to overwrite.
        """
        buf = io.StringIO()
        sink = FileSink(buf)
        sink.emit("claude", _event("one"))
        sink.emit("claude", _event("two"))
        sink.emit("claude", _event("compacted", restart=True))
        sink.emit("claude", _event("after"))
        idxs = [json.loads(line)["idx"] for line in buf.getvalue().splitlines()]
        assert idxs == [0, 1, 0, 1]

    def test_slash_command_is_written_as_its_own_shape(self) -> None:
        """A typed command belongs to no part, so it is not a record row."""
        buf = io.StringIO()
        sink = FileSink(buf)
        sink.emit_slash_command(SlashCommand(command="model", args="opus"), _AT)
        (line,) = buf.getvalue().splitlines()
        row = json.loads(line)
        assert row["slash_command"]["command"] == "model"
        assert row["slash_command"]["args"] == "opus"
        assert "idx" not in row

    def test_a_slash_command_does_not_consume_a_position(self) -> None:
        """It is absent from the log, so it cannot hold an ``idx``.

        Consuming one would renumber every record after it, and a replay --
        which re-derives positions from the file, where the command does not
        appear -- would then disagree with what was stored.
        """
        buf = io.StringIO()
        sink = FileSink(buf)
        sink.emit("claude", _event("before"))
        sink.emit_slash_command(SlashCommand(command="exit"), _AT)
        sink.emit("claude", _event("after"))
        idxs = [
            json.loads(line)["idx"]
            for line in buf.getvalue().splitlines()
            if "idx" in json.loads(line)
        ]
        assert idxs == [0, 1]


class _FakeClient:
    """Records session API calls without touching the network."""

    def __init__(self) -> None:
        self.started: list[SessionStart] = []
        self.appended: list[tuple[UUID, str, list[RecordBody], bool]] = []
        self.slash: list[SlashCommandBody] = []
        self.manifests: list[ManifestBody] = []
        self.ended: list[UUID] = []
        self.end_bodies: list[SessionEnd | None] = []
        self.granted_actor: str | None = None
        self.start_seq = 0
        self._id = uuid4()

    def session_start(self, body: SessionStart) -> SessionStartResponse:
        self.started.append(body)
        return SessionStartResponse(
            id=self._id, seq=self.start_seq, actor=self.granted_actor or body.actor
        )

    def append_records(
        self,
        session_id: UUID,
        *,
        name: str = "",
        manifest: ManifestBody | None = None,
        records: object = (),
        restart: bool = False,
        slash_commands: object = (),
    ) -> AppendRecordsResponse:
        bodies = list(cast(list[RecordBody], records))
        self.appended.append((session_id, name, bodies, restart))
        if manifest is not None:
            self.manifests.append(manifest)
        self.slash.extend(cast(list[SlashCommandBody], slash_commands))
        return AppendRecordsResponse(part=0, written=len(bodies), skipped=0)

    def session_end(
        self, session_id: UUID, body: SessionEnd | None = None
    ) -> SessionEndResponse:
        self.ended.append(session_id)
        self.end_bodies.append(body)
        return SessionEndResponse(id=session_id)


class TestFileSinkWriteBody:
    """``write_body`` (replay) must keep the position counter past replayed rows."""

    def test_emit_after_write_body_does_not_reuse_a_position(self) -> None:
        """A replayed body's ``idx`` must advance the counter for that file.

        REV-001's shape, in record space: ``ResilientSink._degrade`` replays a
        degrading primary's buffered bodies (idx 0, 1, ...) into the fallback.
        A counter that did not advance would re-mint 0 on the next live emit,
        and the two rows would collide on ``(session_id, part, idx)``.
        """
        buf = io.StringIO()
        sink = FileSink(buf)
        body = RecordBody(idx=0, kind="UserMessage")
        sink.write_body("claude", _PART, body)
        sink.write_body("claude", _PART, body.model_copy(update={"idx": 1}))
        sink.emit("claude", _event("after replay"))
        idxs = [json.loads(line)["idx"] for line in buf.getvalue().splitlines()]
        assert idxs == [0, 1, 2]

    def test_replay_advances_only_the_replayed_file(self) -> None:
        """Positions are per-part, so one file's replay must not skew another."""
        buf = io.StringIO()
        sink = FileSink(buf)
        sink.write_body("claude", _PART, RecordBody(idx=7, kind="UserMessage"))
        sink.emit("claude", _event("other", path=_OTHER))
        rows = [json.loads(line) for line in buf.getvalue().splitlines()]
        assert [(r["part_name"], r["idx"]) for r in rows] == [
            ("a.jsonl", 7),
            ("b.jsonl", 0),
        ]


class TestTrackinizerSink:
    def test_lazy_start_batch_and_end(self) -> None:
        client = _FakeClient()
        sink = TrackinizerSink(cast(Client, client), "codex", batch_size=2)
        # No records yet -> no session opened.
        assert client.started == []

        sink.emit("codex", _event("hi"))
        # The first record opens the session, naming its CLI.
        assert len(client.started) == 1
        assert client.started[0].cli == "codex"

        sink.emit("codex", Event(record=AssistantMessage(content="ok"), path=_PART))
        # batch_size=2 reached -> one flush of 2 records.
        assert len(client.appended) == 1
        assert [b.idx for b in client.appended[0][2]] == [0, 1]

        sink.emit("codex", Event(record=AssistantMessage(content="done"), path=_PART))
        sink.close()
        # close() flushes the remainder and ends the session.
        assert len(client.appended) == 2
        assert client.appended[1][2][0].idx == 2
        assert client.ended == [client._id]

    def test_each_file_is_its_own_request(self) -> None:
        """The server resolves one part per request, from the file's basename.

        Two files in one request would land one of them under the other's
        part, so the buffer is grouped by path before it is sent.
        """
        client = _FakeClient()
        sink = TrackinizerSink(cast(Client, client), "claude", batch_size=50)
        sink.emit("claude", _event("a1", path=_PART))
        sink.emit("claude", _event("b1", path=_OTHER))
        sink.close()
        names = sorted(name for _, name, _, _ in client.appended)
        assert names == ["a.jsonl", "b.jsonl"]
        for _, name, bodies, _ in client.appended:
            assert [b.idx for b in bodies] == [0], name

    def test_restart_rides_the_batch_for_that_file_only(self) -> None:
        """``restart`` is batch-level and describes ONE part's production.

        It selects the conflict policy: a restarted batch overwrites, because
        a compaction is a replacement and disk is truth. A sibling file that
        did not restart must not be overwritten alongside it.
        """
        client = _FakeClient()
        sink = TrackinizerSink(cast(Client, client), "claude", batch_size=50)
        sink.emit("claude", _event("a", path=_PART, restart=True))
        sink.emit("claude", _event("b", path=_OTHER))
        sink.close()
        by_name = {name: restart for _, name, _, restart in client.appended}
        assert by_name == {"a.jsonl": True, "b.jsonl": False}

    def test_restart_is_cleared_after_it_is_sent(self) -> None:
        """One rewrite overwrites once; later appends must not keep doing so.

        A sticky flag would make every subsequent batch ``DO UPDATE``, which
        rewrites rows the runner has no newer version of.
        """
        client = _FakeClient()
        sink = TrackinizerSink(cast(Client, client), "claude", batch_size=1)
        sink.emit("claude", _event("a", restart=True))
        sink.emit("claude", _event("b"))
        assert [restart for _, _, _, restart in client.appended] == [True, False]

    def test_manifest_counts_the_records_the_part_holds(self) -> None:
        """``records`` bounds every reader (``idx < records``), so it must grow."""
        client = _FakeClient()
        sink = TrackinizerSink(cast(Client, client), "claude", batch_size=2)
        sink.emit("claude", _event("one"))
        sink.emit("claude", _event("two"))
        assert client.manifests[0].records == 2
        assert client.manifests[0].name == "a.jsonl"
        assert client.manifests[0].format == "claude"

    def test_the_manifest_ir_id_is_stable_across_batches(self) -> None:
        """The id names the FILE, so a second batch must not re-mint it.

        A fresh uuid per batch would rewrite the manifest's declared session
        id on every flush, and the last one written would win arbitrarily.
        """
        client = _FakeClient()
        sink = TrackinizerSink(cast(Client, client), "claude", batch_size=1)
        sink.emit("claude", _event("one"))
        sink.emit("claude", _event("two"))
        assert client.manifests[0].ir_id == client.manifests[1].ir_id

    def test_no_records_opens_no_session(self) -> None:
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
        sink.emit("claude", _event("hi"))
        assert client.started[0].actor == "scientist"
        assert sink.granted_actor == "scientist#2"

    def test_granted_actor_is_none_before_session_opens(self) -> None:
        client = _FakeClient()
        sink = TrackinizerSink(cast(Client, client), "claude", actor="scientist")
        assert sink.granted_actor is None

    def test_open_eagerly_returns_granted_handle(self) -> None:
        """``open`` opens the session before any record and returns the handle.

        Eager open lets the runner export the server-granted routing handle
        into the child env before fork (#453).
        """
        client = _FakeClient()
        client.granted_actor = "scientist#2"
        sink = TrackinizerSink(cast(Client, client), "claude", actor="scientist")
        granted = sink.open()
        assert granted == "scientist#2"
        assert len(client.started) == 1  # opened without a record
        assert sink.granted_actor == "scientist#2"

    def test_a_resumed_session_re_derives_its_positions(self) -> None:
        """The start response's ``seq`` is not a position seed any more.

        A record's key is DERIVED from its position in the file's normalized
        stream, so a resumed run re-feeding that file lands each record back
        on the key it already had. Seeding from the server (as the legacy
        event log required) would offset every one of them.
        """
        client = _FakeClient()
        client.start_seq = 5  # legacy continuation point; must be ignored
        sink = TrackinizerSink(cast(Client, client), "claude")
        sink.open()
        sink.emit("claude", _event("resumed"))
        sink.close()
        assert client.appended[0][2][0].idx == 0

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
        must not withhold records until ``close``; the drain loop's periodic
        ``flush`` sends them once the oldest buffered record is old enough.
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
        sink.emit("claude", _event("hi"))

        # Too soon: the buffer is younger than the interval, so flush no-ops.
        now[0] = 100.5
        sink.flush()
        assert client.appended == []

        # Past the interval: the partial buffer is sent.
        now[0] = 101.0
        sink.flush()
        assert len(client.appended) == 1
        assert [b.idx for b in client.appended[0][2]] == [0]

    def test_flush_on_empty_buffer_is_noop(self) -> None:
        client = _FakeClient()
        sink = TrackinizerSink(cast(Client, client), "claude")
        sink.flush()
        # Nothing buffered -> no session opened, nothing sent.
        assert client.started == []
        assert client.appended == []

    def test_flush_resets_buffer_age_so_next_batch_waits(self) -> None:
        """After a timed flush, a freshly buffered record restarts the clock."""
        now = [0.0]
        client = _FakeClient()
        sink = TrackinizerSink(
            cast(Client, client),
            "claude",
            batch_size=50,
            flush_interval_sec=1.0,
            clock=lambda: now[0],
        )
        sink.emit("claude", _event("a"))
        now[0] = 1.0
        sink.flush()
        assert len(client.appended) == 1

        # A new record right after the flush is young again -> held.
        sink.emit("claude", _event("b"))
        now[0] = 1.5
        sink.flush()
        assert len(client.appended) == 1
        # Aged past the interval from its own arrival -> sent.
        now[0] = 2.0
        sink.flush()
        assert len(client.appended) == 2
        assert [b.idx for b in client.appended[1][2]] == [1]

    def test_positions_are_per_session_not_global(self) -> None:
        """Each session numbers each of its parts from 0."""
        client_a = _FakeClient()
        first = TrackinizerSink(cast(Client, client_a), "codex", batch_size=1)
        first.emit("codex", _event("a"))
        first.emit("codex", _event("b"))

        client_b = _FakeClient()
        second = TrackinizerSink(cast(Client, client_b), "codex", batch_size=1)
        second.emit("codex", _event("c"))

        assert [b.idx for _, _, bodies, _ in client_a.appended for b in bodies] == [
            0,
            1,
        ]
        # The second session restarts at 0, not 2.
        assert [b.idx for _, _, bodies, _ in client_b.appended for b in bodies] == [0]

    def test_close_stamps_ended_timestamp(self) -> None:
        """``close`` must record when the session ended, not a bare ``None``."""
        client = _FakeClient()
        sink = TrackinizerSink(cast(Client, client), "codex")
        sink.emit("codex", _event("hi"))
        sink.close()
        assert len(client.end_bodies) == 1
        body = client.end_bodies[0]
        assert body is not None
        assert body.ended is not None


class TestTrackinizerSinkManifestMetadata:
    """The manifest carries what a REWRITE needs, not just what a list shows."""

    def test_the_manifest_carries_the_files_declared_encoding(self) -> None:
        r"""Without it a resumed file is not the file that was captured.

        Claude's ascii-escaping convention rides on the ``TurnContext.encoding``
        in force (a majority flag plus its exception bitmap). A rewrite that
        does not have it writes raw UTF-8 where the CLI wrote ``\\u00e9`` --
        every record matches and the bytes differ, which is exactly what the
        ``JSON``-not-``JSONB`` column and ``PartBody.metadata`` exist to
        prevent. A provider is entitled to reject a transcript it did not
        write.

        Fed through ``Sink.feed`` rather than ``emit``: the encoding lives on
        the per-file reader, so only the path that builds one has it.
        """
        client = _FakeClient()
        sink = TrackinizerSink(cast(Client, client), "claude", batch_size=1)
        line = (
            b'{"type":"user","sessionId":"s1","message":'
            b'{"role":"user","content":"calf\\u00e9"}}\n'
        )
        _ = sink.feed(ClaudeAdapter(), _PART, line)
        sink.close()

        assert client.manifests, "no manifest was sent at all"
        metadata = DictCodec.coerce(client.manifests[0].metadata)
        assert "ascii_escape_exceptions" in metadata, (
            "the manifest carries no encoding; a resume rewrites the file "
            "with different bytes than were captured"
        )

    def test_the_metadata_tracks_the_prefix_read_so_far(self) -> None:
        r"""Re-sent per batch because it CHANGES as the file grows.

        ``ascii_escaped`` is a majority over the lines consumed, so the value
        correct for one batch may be wrong for the next. A manifest written
        once at open would pin the first batch's answer forever -- here the
        second line is ASCII and flips the majority the first line set.

        The first line's ``é`` is RAW UTF-8, not the ``\\u00e9`` escape: the
        escape is six ASCII characters, so a line spelled that way never moves
        the majority and the test could not tell a restated encoding from a
        pinned one.
        """
        client = _FakeClient()
        sink = TrackinizerSink(cast(Client, client), "claude", batch_size=1)
        adapter = ClaudeAdapter()
        _ = sink.feed(
            adapter,
            _PART,
            '{"type":"user","sessionId":"s1","message":'
            '{"role":"user","content":"café"}}\n'.encode(),
        )
        _ = sink.feed(
            adapter,
            _PART,
            b'{"type":"user","sessionId":"s1","message":'
            b'{"role":"user","content":"plain"}}\n',
        )
        sink.close()

        assert len(client.manifests) >= 2
        escaped = [
            DictCodec.coerce(m.metadata).get("ascii_escaped") for m in client.manifests
        ]
        assert len(set(escaped)) > 1, (
            "every batch declared the same majority; the manifest pinned one "
            "batch's answer instead of restating the prefix read so far"
        )
        # One of the two lines is ASCII, so the majority in force at the end is
        # ``True`` -- and it is the LAST manifest that a rewrite reads.
        assert escaped[-1] is True


class TestTrackinizerSinkSlashCommands:
    """A typed command rides the record append it is adjacent to."""

    def test_a_command_commits_with_the_records_around_it(self) -> None:
        """One request, so a crash cannot store one half of the transcript."""
        client = _FakeClient()
        sink = TrackinizerSink(cast(Client, client), "claude", batch_size=50)
        sink.emit("claude", _event("before"))
        sink.emit_slash_command(SlashCommand(command="model", args="opus"), _AT)
        sink.close()
        assert len(client.appended) == 1
        assert [c.command for c in client.slash] == ["model"]

    def test_a_command_alone_names_no_file(self) -> None:
        """It belongs to the SESSION, not to any part.

        A command typed before the CLI has written a transcript has no part to
        belong to, so the request carries no name and the server stores it
        without resolving one.
        """
        client = _FakeClient()
        sink = TrackinizerSink(cast(Client, client), "claude")
        sink.emit_slash_command(SlashCommand(command="exit"), _AT)
        sink.close()
        assert [name for _, name, _, _ in client.appended] == [""]
        assert [c.command for c in client.slash] == ["exit"]

    def test_a_command_is_sent_once_across_several_parts(self) -> None:
        """It rides the FIRST part only; a resend would store a second copy.

        A command's ``seq`` is server-assigned, so an accidental duplicate
        does not collide -- it lands as a distinct row and the transcript
        shows the human typing twice.
        """
        client = _FakeClient()
        sink = TrackinizerSink(cast(Client, client), "claude", batch_size=50)
        sink.emit("claude", _event("a", path=_PART))
        sink.emit("claude", _event("b", path=_OTHER))
        sink.emit_slash_command(SlashCommand(command="exit"), _AT)
        sink.close()
        assert len(client.appended) == 2
        assert [c.command for c in client.slash] == ["exit"]

    def test_a_command_alone_opens_the_session(self) -> None:
        """The human typed something, so there is a session to record it in."""
        client = _FakeClient()
        sink = TrackinizerSink(cast(Client, client), "claude")
        sink.emit_slash_command(SlashCommand(command="exit"), _AT)
        assert len(client.started) == 1

    def test_a_buffered_command_ages_into_a_timed_flush(self) -> None:
        """A quiet session that only typed a command still streams it.

        The interval flush is keyed off the oldest buffered item; a command
        that did not start that clock would sit until ``close``.
        """
        now = [0.0]
        client = _FakeClient()
        sink = TrackinizerSink(
            cast(Client, client),
            "claude",
            batch_size=50,
            flush_interval_sec=1.0,
            clock=lambda: now[0],
        )
        sink.emit_slash_command(SlashCommand(command="exit"), _AT)
        now[0] = 0.5
        sink.flush()
        assert client.slash == []
        now[0] = 1.0
        sink.flush()
        assert [c.command for c in client.slash] == ["exit"]


class _FlakyFlushClient(_FakeClient):
    """First ``append_records`` raises; later calls succeed, to test retry."""

    def __init__(self) -> None:
        super().__init__()
        self.append_attempts = 0

    @override
    def append_records(
        self,
        session_id: UUID,
        *,
        name: str = "",
        manifest: ManifestBody | None = None,
        records: object = (),
        restart: bool = False,
        slash_commands: object = (),
    ) -> AppendRecordsResponse:
        self.append_attempts += 1
        if self.append_attempts == 1:
            raise RuntimeError("transient flush failure")
        return super().append_records(
            session_id,
            name=name,
            manifest=manifest,
            records=records,
            restart=restart,
            slash_commands=slash_commands,
        )


class TestTrackinizerSinkCloseFlushOrdering:
    """A flush failure during ``close`` must not silently drop the buffer."""

    def test_failed_flush_keeps_buffer_for_retry(self) -> None:
        client = _FlakyFlushClient()
        sink = TrackinizerSink(cast(Client, client), "codex")
        sink.emit("codex", _event("hi"))

        # First close flushes, the flush raises; the record is not yet lost.
        with contextlib.suppress(RuntimeError):
            sink.close()

        # A retried close re-attempts the flush and the buffered record lands.
        sink.close()
        assert client.append_attempts == 2
        assert [b.idx for _, _, bodies, _ in client.appended for b in bodies] == [0]
        # Both closes end the session: ``session_end`` runs in the finally so
        # a flush failure can never leave the session live (phantom-session
        # guard); the duplicate end is idempotent server-side.
        assert client.ended == [client._id, client._id]

    def test_a_failed_flush_keeps_slash_commands_for_retry(self) -> None:
        """Their ``seq`` is server-assigned, so a lost one is simply gone.

        Clearing the pending list before the request returned would drop the
        command on a transient failure, with no derived key to re-send it
        under.
        """
        client = _FlakyFlushClient()
        sink = TrackinizerSink(cast(Client, client), "claude")
        sink.emit("claude", _event("hi"))
        sink.emit_slash_command(SlashCommand(command="exit"), _AT)

        with contextlib.suppress(RuntimeError):
            sink.close()
        sink.close()

        assert [c.command for c in client.slash] == ["exit"]

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
        sink.emit("claude", _event("one"))

        sink.close()

        assert client.ended == [client._id], (
            "session_end never ran; the server session stays live (phantom)"
        )
        # The buffered record still reached the fallback (REV-02 preserved).
        assert "one" in (tmp_path / "fb.jsonl").read_text()


class TestDrainPendingIsOnTheProtocol:
    """``drain_pending`` must be part of the ``Sink`` contract, not a getattr.

    ``ResilientSink._degrade`` previously reached it dynamically
    (``getattr(primary, "drain_pending", None)`` + an unchecked cast): a
    rename would type-check clean and silently re-open REV-02 (buffered
    records lost on degrade). On the Protocol, a rename is a type error.
    """

    def test_file_sink_has_empty_drain(self) -> None:
        sink = FileSink(io.StringIO())
        assert sink.drain_pending() == []

    def test_protocol_declares_drain_pending(self) -> None:
        assert hasattr(Sink, "drain_pending"), (
            "drain_pending is not on the Sink Protocol; _degrade must be "
            "reaching it via getattr, which a rename silently breaks"
        )


class _ExplodingSink(Sink):
    """A sink whose ``emit`` always raises, to drive the fallback path."""

    def __init__(self) -> None:
        self.emit_attempts: int = 0
        self.closed: bool = False

    @property
    @override
    def session_id(self) -> UUID | None:
        return None

    @override
    def open(self) -> str | None:
        return None

    @override
    def set_cli_session_id(self, cli_session_id: str) -> None:
        del cli_session_id

    @override
    def emit(self, adapter_name: str, event: Event) -> None:
        del adapter_name, event
        self.emit_attempts += 1
        raise RuntimeError("server exploded")

    @override
    def emit_slash_command(self, command: SlashCommand, at: datetime) -> None:
        del command, at
        raise RuntimeError("server exploded")

    @override
    def flush(self) -> None:
        pass

    @override
    def drain_pending(self) -> list[tuple[Path, RecordBody]]:
        return []

    @override
    def close(self) -> None:
        self.closed = True


class _FailingOpenPrimary(_ExplodingSink):
    """A primary sink whose ``open`` raises, to drive the eager-open degrade.

    Mirrors a ``TrackinizerSink`` against an unreachable server: opening the
    session before fork raises, and the wrapper must degrade rather than let
    the run abort.
    """

    def __init__(self) -> None:
        super().__init__()
        self.open_attempts = 0

    @override
    def open(self) -> str | None:
        self.open_attempts += 1
        raise RuntimeError("server unreachable")

    @override
    def emit(self, adapter_name: str, event: Event) -> None:
        del adapter_name, event
        self.emit_attempts += 1


class _FailingSessionIdPrimary(_FailingOpenPrimary):
    """A primary whose ``set_cli_session_id`` raises, to drive that degrade."""

    @override
    def open(self) -> str | None:
        return None

    @override
    def set_cli_session_id(self, cli_session_id: str) -> None:
        del cli_session_id
        raise RuntimeError("server unreachable")


def _fallback_texts(path: Path) -> list[str]:
    """The ``text`` of each record row the fallback file holds."""
    return [
        json.loads(line)["text"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if "text" in json.loads(line)
    ]


class TestResilientSink:
    """A failing primary sink must never propagate; it degrades to local file."""

    def test_emit_failure_falls_back_to_file_and_warns_once(
        self,
        tmp_path: Path,
        capsys: object,
    ) -> None:
        primary = _ExplodingSink()
        fallback_path = tmp_path / "fallback.jsonl"
        sink = ResilientSink(primary, fallback_path=fallback_path)

        # Two emits: the primary explodes on the first; neither raises.
        sink.emit("claude", _event("one"))
        sink.emit("claude", _event("two"))
        sink.close()

        # The primary was tried exactly once, then abandoned (no re-attempt).
        assert primary.emit_attempts == 1
        # Both records landed in the local fallback file as JSONL.
        assert _fallback_texts(fallback_path) == ["one", "two"]
        # The user is warned once, on stderr, not flooded per-record.
        err = cast(Any, capsys).readouterr().err
        assert err.count("[trax run]") == 1
        assert "falling back to local capture" in err
        assert str(fallback_path) in err

    def test_degrade_drains_orphaned_primary_buffer(self, tmp_path: Path) -> None:
        """Records buffered in the primary must not be lost when it degrades.

        REV-02: a ``TrackinizerSink`` buffers up to ``batch_size`` records
        before flushing. If the flush fails and the wrapper degrades, those
        already-buffered records were stranded -- only the current one reached
        the fallback. They must be replayed to the local file.
        """
        client = _FlakyFlushClient()  # first append_records raises
        primary = TrackinizerSink(cast(Client, client), "claude", batch_size=50)
        fallback_path = tmp_path / "fallback.jsonl"
        sink = ResilientSink(primary, fallback_path=fallback_path)

        # Three records buffer in the primary (below batch_size, no flush yet).
        sink.emit("claude", _event("one"))
        sink.emit("claude", _event("two"))
        sink.emit("claude", _event("three"))
        assert not fallback_path.exists()  # all still buffered server-side

        # A flush triggers the (failing) append; the wrapper degrades. All
        # three buffered records must end up in the fallback, not just later.
        sink.flush()
        sink.close()
        assert _fallback_texts(fallback_path) == ["one", "two", "three"]

    def test_a_replayed_buffer_keeps_its_own_positions(self, tmp_path: Path) -> None:
        """Replayed bodies carry the ``idx`` the primary derived, not a re-mint.

        The position is the storage key, so re-deriving it in the fallback
        would file the same record under a different one -- and a later
        recovery reading both would see the turn twice.
        """
        client = _FlakyFlushClient()
        primary = TrackinizerSink(cast(Client, client), "claude", batch_size=50)
        fallback_path = tmp_path / "fallback.jsonl"
        sink = ResilientSink(primary, fallback_path=fallback_path)
        sink.emit("claude", _event("one"))
        sink.emit("claude", _event("two"))
        sink.flush()
        sink.close()
        idxs = [
            json.loads(line)["idx"]
            for line in fallback_path.read_text(encoding="utf-8").splitlines()
        ]
        assert idxs == [0, 1]

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
        sink = ResilientSink(primary, fallback_path=fallback_path)

        # Eager open must not propagate; it returns None (no server handle).
        assert sink.open() is None
        assert primary.open_attempts == 1
        # The session id is None -- there is no live server session anymore.
        assert sink.session_id is None

        # Capture still works: a later emit writes to the local fallback file.
        sink.emit("claude", _event("after open failed"))
        sink.close()
        assert _fallback_texts(fallback_path) == ["after open failed"]
        # The primary was abandoned after the open failure: no later calls.
        assert primary.emit_attempts == 0

    def test_the_record_that_triggered_the_degrade_lands_once(
        self, tmp_path: Path
    ) -> None:
        """The record whose emit failed must not be written twice.

        A ``TrackinizerSink.emit`` buffers the record BEFORE the batch flush,
        so an ``append_records`` failure raises with that record still in the
        buffer. ``_degrade`` replays the buffer into the fallback -- including
        it -- and then ``emit`` falls through and writes it again under a fresh
        position. Two rows for one turn.
        """
        client = _FlakyFlushClient()  # the first append_records raises
        primary = TrackinizerSink(cast(Client, client), "claude", batch_size=1)
        fallback_path = tmp_path / "fallback.jsonl"
        sink = ResilientSink(primary, fallback_path=fallback_path)

        sink.emit("claude", _event("only"))
        sink.close()

        assert _fallback_texts(fallback_path) == ["only"]

    def test_a_degrade_with_nothing_buffered_still_writes_the_record(
        self, tmp_path: Path
    ) -> None:
        """The counterpart: an unbuffered failure must still reach the fallback.

        A primary that raises without ever buffering (an unreachable server on
        the very first call) drains an empty buffer, so the triggering record
        is the fallback's only chance to record the turn.
        """
        primary = _ExplodingSink()
        fallback_path = tmp_path / "fallback.jsonl"
        sink = ResilientSink(primary, fallback_path=fallback_path)

        sink.emit("claude", _event("only"))
        sink.close()

        assert _fallback_texts(fallback_path) == ["only"]

    def test_a_slash_command_failure_degrades(self, tmp_path: Path) -> None:
        """Every primary call degrades, including the command path.

        It runs on the drain thread like every other sink call, so an
        unguarded raise there ends capture for the rest of the run.
        """
        primary = _ExplodingSink()
        fallback_path = tmp_path / "fallback.jsonl"
        sink = ResilientSink(primary, fallback_path=fallback_path)

        sink.emit_slash_command(SlashCommand(command="exit"), _AT)  # must not raise
        sink.close()

        rows = [
            json.loads(line)
            for line in fallback_path.read_text(encoding="utf-8").splitlines()
        ]
        assert [r["slash_command"]["command"] for r in rows] == ["exit"]

    def test_set_cli_session_id_failure_degrades(self, tmp_path: Path) -> None:
        """Every primary call degrades on failure -- including this one.

        It was the one call reaching the primary with no guard, so a server
        that failed there raised straight into the drain thread and ended
        capture, contradicting the wrapper's whole contract.
        """
        primary = _FailingSessionIdPrimary()
        fallback_path = tmp_path / "fallback.jsonl"
        sink = ResilientSink(primary, fallback_path=fallback_path)

        sink.set_cli_session_id("claude-abc-123")  # must not raise

        # Degraded: the primary is abandoned and capture continues locally.
        sink.emit("claude", _event("after"))
        sink.close()
        assert _fallback_texts(fallback_path) == ["after"]
        assert primary.emit_attempts == 0

    def test_healthy_primary_never_touches_fallback(self, tmp_path: Path) -> None:
        client = _FakeClient()
        wrapped = TrackinizerSink(cast(Client, client), "claude")
        fallback_path = tmp_path / "unused.jsonl"
        sink = ResilientSink(wrapped, fallback_path=fallback_path)

        sink.emit("claude", _event("hi"))
        sink.close()

        # The primary handled everything; no fallback file was created.
        assert client.started
        assert not fallback_path.exists()


class TestSinkFeed:
    """``feed`` normalizes a chunk and owns one reader per FILE."""

    def test_a_chunk_becomes_records(self) -> None:
        """One line in, the records it produced out, tallied by kind.

        THREE records for one line, not one: a reader opens its stream with
        the settings in force and the context the window opens from (axiom 6),
        so the line's own record is the third.

        A ``Stdout``, because a scrape read back from a FILE holds no
        descriptors -- the stream a line crossed is knowable only at capture,
        where the fds still exist.
        """
        buf = io.StringIO()
        sink = FileSink(buf)
        kinds = sink.feed(IOStreamAdapter(), _PART, b"hello\n")
        assert kinds == ["TurnContext", "ContextClear", "Stdout"]
        rows = [json.loads(line) for line in buf.getvalue().splitlines()]
        assert rows[-1]["text"] == "hello\n"

    def test_two_files_get_separate_readers(self) -> None:
        """A shared reader would number the second file after the first.

        A reader carries the position it has read to, so one instance across
        two files makes the second part start mid-sequence -- and it would
        state the opening context once rather than per file.
        """
        buf = io.StringIO()
        sink = FileSink(buf)
        adapter = IOStreamAdapter()
        sink.feed(adapter, _PART, b"a\n")
        sink.feed(adapter, _OTHER, b"b\n")
        rows = [json.loads(line) for line in buf.getvalue().splitlines()]
        assert [(r["part_name"], r["idx"]) for r in rows] == [
            ("a.jsonl", 0),
            ("a.jsonl", 1),
            ("a.jsonl", 2),
            ("b.jsonl", 0),
            ("b.jsonl", 1),
            ("b.jsonl", 2),
        ]

    def test_a_restart_rebuilds_that_files_reader(self) -> None:
        """A rewritten file's reader describes bytes that no longer exist.

        The records after a restart re-derive the part from offset 0, so a
        reader that kept its position would emit them at the wrong ones. The
        fresh reader restates its opening context, so the rewritten file
        numbers 0, 1, 2 exactly as the original did.
        """
        buf = io.StringIO()
        sink = FileSink(buf)
        adapter = IOStreamAdapter()
        sink.feed(adapter, _PART, b"one\n")
        sink.feed(adapter, _PART, b"two\n")
        sink.feed(adapter, _PART, b"rewritten\n", restart=True)
        rows = [json.loads(line) for line in buf.getvalue().splitlines()]
        assert [r["idx"] for r in rows] == [0, 1, 2, 3, 0, 1, 2]

    def test_the_reader_comes_from_the_adapter(self) -> None:
        """No second registry: the adapter already declares its reader.

        A name-keyed table in ``sink.py`` would have to be kept in step with
        ``session.py::_ADAPTERS``, and a CLI added to one and not the other
        fails at capture time rather than at import.
        """
        built: list[str] = []

        class _Recording(IOStreamAdapter):
            @override
            def reader(self) -> Tail:
                built.append(self.name)
                return super().reader()

        sink = FileSink(io.StringIO())
        sink.feed(_Recording(), _PART, b"x\n")
        assert built == ["sh"], "the sink did not ask the adapter for its reader"


class _BlockingSink(Sink):
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
    @override
    def session_id(self) -> UUID | None:
        return None

    @override
    def open(self) -> str | None:
        return None

    @override
    def set_cli_session_id(self, cli_session_id: str) -> None:
        del cli_session_id

    def _record(self, op: str, phase: str) -> None:
        with self._lock:
            self.log.append((op, phase))

    @override
    def emit(self, adapter_name: str, event: Event) -> None:
        del adapter_name, event
        self._record("emit", "enter")
        self._record("emit", "exit")

    @override
    def emit_slash_command(self, command: SlashCommand, at: datetime) -> None:
        del command, at

    @override
    def flush(self) -> None:
        self._record("flush", "enter")
        self.entered.set()
        self._release.wait(5.0)
        self._record("flush", "exit")

    @override
    def drain_pending(self) -> list[tuple[Path, RecordBody]]:
        return []

    @override
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
        sink = LockedSink(inner)

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

    def test_feed_holds_the_lock_across_the_whole_chunk(self) -> None:
        """Positions advance once per record, so a chunk cannot interleave.

        Another thread emitting between two records of one chunk would take a
        position out from under it, and the two turns would be stored out of
        order.
        """
        release = threading.Event()
        inner = _BlockingSink(release)
        sink = LockedSink(inner)
        flusher = threading.Thread(target=sink.flush, daemon=True)
        flusher.start()
        assert inner.entered.wait(2.0)

        fed = threading.Event()

        def _feed() -> None:
            sink.feed(IOStreamAdapter(), _PART, b"x\n")
            fed.set()

        feeder = threading.Thread(target=_feed, daemon=True)
        feeder.start()
        assert not fed.wait(0.05), "feed ran while flush held the lock"

        release.set()
        assert fed.wait(5.0), "feed never completed after the lock was released"
        flusher.join(timeout=5.0)
        feeder.join(timeout=5.0)

    def test_delegates_session_id_and_emit(self) -> None:
        client = _FakeClient()
        inner = TrackinizerSink(cast(Client, client), "codex")
        sink = LockedSink(inner)
        assert sink.session_id is None
        sink.emit("codex", _event("hi"))
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
        sink = LockedSink(inner)
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
