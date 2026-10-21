"""Tests for the run sinks: FileSink JSONL shape + TrackinizerSink batching."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, TypeGuard, cast, override
from uuid import UUID, uuid4

import asyncio
import contextlib
import io
import json
import threading

from treekle import from_plain, immutable, json_loads

import httpx2
import pytest

from trackinizer.client.client import Client
from trackinizer.client.errors import ClientError
from trackinizer.lib.agent.types.sessions import (
    AgentStatusResult,
    AgentToAgentMessage,
    AssistantMessage,
    ContextClear,
    ContextCompaction,
    ContextState,
    FileEditResult,
    FileReadResult,
    FileWriteResult,
    IncompleteRecord,
    ShellCommandResult,
    Splice,
    SystemMessage,
    Thinking,
    TokenUsage,
    ToolCall,
    TurnContext,
    UncategorizedRecord,
    UncategorizedToolResult,
    UserMessage,
    WebFetchResult,
    WebSearchResult,
    WebSearchResults,
)
from trackinizer.lib.posix.follow import follow_tree
from trackinizer.trax.run.adapters.claude import ClaudeAdapter
from trackinizer.trax.run.adapters.codex import CodexAdapter
from trackinizer.trax.run.adapters.iostream import IOStreamAdapter
from trackinizer.trax.run.custom_types import Event
from trackinizer.trax.run.redact import Redactor
from trackinizer.trax.run.sink import (
    FileSink,
    LockedSink,
    PrivateNotHeldError,
    ResilientSink,
    Sink,
    TrackinizerSink,
)
from trackinizer.trax.run.slash import SlashCommand
from trackinizer.trax.run.truncate import (
    MAX_TOOL_RESULT_BYTES,
    encoded_size,
    was_truncated,
)
from trackinizer.trax.run.worktree_diff import WORKTREE_DIFF_PART
from trackinizer.types.line_capture import LineCapture
from trackinizer.types.session_records import _BY_KIND, SessionRecordRow
from trackinizer.types.streams import Stderr, Stdin, Stdout
from trackinizer.wire.wire_session_ir import (
    AppendRecordsRequest,
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


if TYPE_CHECKING:
    from trackinizer.lib.agent.sessions.tail import Tail
    from trackinizer.types.streams import TraxRecord


_PART = Path("/sessions/a.jsonl")
_OTHER = Path("/sessions/b.jsonl")
_AT = datetime(2026, 6, 1, tzinfo=UTC)

_PROXY_LIMIT: Final = 8_000_000
"""The live proxy's request-body cap: Caddy's ``8MB`` counts decimal megabytes."""

_WORKER_LOG: Final = (
    '\x1b[2K\r\x1b[36m[w3]\x1b[0m \x1b[2m22:45:01\x1b[0m\t{"step":120,"loss":0.25}'
    " \x1b[32m━━━━\x1b[0m\x1b[1A" + "\x00" * 28 + "\n"
)
"""One line of the worker logs a scientist read in bulk: a progress bar, then the
NUL padding of a preallocated log. Each control character takes six bytes on the
wire, which is how ~1 MB of output became an 11.39 MB record."""

_OVER_THE_LIMIT: Final = '\x00\x1b[0m"' * 500_000
"""Text one copy of which is over 8 MB on the wire: each control character
encodes as six bytes."""

_OVERSIZED: Final[dict[str, Callable[[str], TraxRecord]]] = {
    "AgentStatusResult": lambda text: AgentStatusResult(call_id="c", content=text),
    "AgentToAgentMessage": lambda text: AgentToAgentMessage(content=text),
    "AssistantMessage": lambda text: AssistantMessage(content=text),
    "ContextClear": lambda text: ContextClear(history=(UserMessage(content=text),)),
    "ContextCompaction": lambda text: ContextCompaction(summary=text),
    "ContextState": lambda text: ContextState(content=text),
    "FileEditResult": lambda text: FileEditResult(
        call_id="c",
        edits=(Splice(before=text, after=text),),
    ),
    "FileReadResult": lambda text: FileReadResult(call_id="c", content=text),
    "FileWriteResult": lambda text: FileWriteResult(call_id="c", content=text),
    "IncompleteRecord": lambda text: IncompleteRecord(text=text),
    "ShellCommandResult": lambda text: ShellCommandResult(call_id="c", stdout=text),
    "Stderr": lambda text: Stderr(text=text),
    "Stdin": lambda text: Stdin(text=text),
    "Stdout": lambda text: Stdout(text=text),
    "SystemMessage": lambda text: SystemMessage(content=text),
    "Thinking": lambda text: Thinking(content=text),
    "TokenUsage": lambda text: TokenUsage(info=immutable({"note": text})),
    "ToolCall": lambda text: ToolCall(
        call_id="c",
        name="Write",
        arguments=immutable({"content": text}),
    ),
    "TurnContext": lambda text: TurnContext(extra=immutable({"instructions": text})),
    "UncategorizedRecord": lambda text: UncategorizedRecord(
        kind="k",
        payload=immutable({"text": text}),
    ),
    "UncategorizedToolResult": lambda text: UncategorizedToolResult(
        call_id="c",
        content=text,
    ),
    "UserMessage": lambda text: UserMessage(content=text),
    "WebFetchResult": lambda text: WebFetchResult(call_id="c", content=text),
    "WebSearchResults": lambda text: WebSearchResults(
        call_id="c",
        content=(WebSearchResult(snippet=text),),
    ),
}
"""One record of each stored kind, holding ``text`` where that kind holds prose."""


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
        first = json_loads((lines[0]), dict[str, object])
        # Position within the part, derived and numbered from 0.
        assert first["idx"] == 0
        assert json_loads((lines[1]), dict[str, object])["idx"] == 1
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
        rows = [
            json_loads(line, dict[str, object]) for line in buf.getvalue().splitlines()
        ]
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
        sink.restart(_PART)
        sink.emit("claude", _event("compacted", restart=True))
        sink.emit("claude", _event("after"))
        idxs = [
            json_loads(line, dict[str, object])["idx"]
            for line in buf.getvalue().splitlines()
        ]
        assert idxs == [0, 1, 0, 1]

    def test_slash_command_is_written_as_its_own_shape(self) -> None:
        """A typed command belongs to no part, so it is not a record row."""
        buf = io.StringIO()
        sink = FileSink(buf)
        sink.emit_slash_command(SlashCommand(command="model", args="opus"), _AT)
        (line,) = buf.getvalue().splitlines()
        row = json_loads(line, dict[str, object])
        command = row["slash_command"]
        assert isinstance(command, dict)
        assert command["command"] == "model"
        assert command["args"] == "opus"
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
            json_loads(line, dict[str, object])["idx"]
            for line in buf.getvalue().splitlines()
            if "idx" in json_loads(line, dict[str, object])
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
        # How the server answers a start: what it was asked, or a fixed audience.
        self.hides = True
        self.audience: str | None = None

    def session_start(self, body: SessionStart) -> SessionStartResponse:
        self.started.append(body)
        return SessionStartResponse(
            id=self._id,
            seq=self.start_seq,
            actor=self.granted_actor or body.actor,
            audience=("private" if body.private else "open")
            if self.hides
            else self.audience,
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
        self,
        session_id: UUID,
        body: SessionEnd | None = None,
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
        idxs = [
            json_loads(line, dict[str, object])["idx"]
            for line in buf.getvalue().splitlines()
        ]
        assert idxs == [0, 1, 2]

    def test_replay_advances_only_the_replayed_file(self) -> None:
        """Positions are per-part, so one file's replay must not skew another."""
        buf = io.StringIO()
        sink = FileSink(buf)
        sink.write_body("claude", _PART, RecordBody(idx=7, kind="UserMessage"))
        sink.emit("claude", _event("other", path=_OTHER))
        rows = [
            json_loads(line, dict[str, object]) for line in buf.getvalue().splitlines()
        ]
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
        # Taking a record never needs the server: the first SEND opens the session.
        assert client.started == []

        sink.emit("codex", Event(record=AssistantMessage(content="ok"), path=_PART))
        # batch_size=2 reached -> the session opens, naming its CLI, and one flush
        # of 2 records.
        assert [start.cli for start in client.started] == ["codex"]
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
        client.granted_actor = "scientist#2"  # `server` renegotiated.
        sink = TrackinizerSink(cast(Client, client), "claude", actor="scientist")
        sink.emit("claude", _event("hi"))
        sink.sync()
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
        assert len(client.started) == 1  # Opened without a record.
        assert sink.granted_actor == "scientist#2"

    def test_a_resumed_session_re_derives_its_positions(self) -> None:
        """The start response's ``seq`` is not a position seed any more.

        A record's key is DERIVED from its position in the file's normalized
        stream, so a resumed run re-feeding that file lands each record back
        on the key it already had. Seeding from the server (as the legacy
        event log required) would offset every one of them.
        """
        client = _FakeClient()
        client.start_seq = 5  # Legacy continuation point; must be ignored.
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
        sink.set_cli_session_id("x")  # Must not raise.

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

    def test_the_runner_defaults_send_fifty_at_once_and_stream_within_a_second(
        self,
    ) -> None:
        now = [0.0]
        client = _FakeClient()
        sink = TrackinizerSink(cast(Client, client), "claude", clock=lambda: now[0])
        for n in range(49):
            sink.emit("claude", _event(str(n)))
        assert client.appended == []
        sink.emit("claude", _event("49"))
        assert _sent_positions(client) == [list(range(50))]

        sink.emit("claude", _event("paused"))
        now[0] = 0.99
        sink.flush()
        assert _sent_positions(client) == [list(range(50))]
        now[0] = 1.0
        sink.flush()
        assert _sent_positions(client) == [list(range(50)), [50]]

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


class TestAPrivateRun:
    """``trax run --private`` sends nothing to a server that would not hide it."""

    def test_a_server_that_hides_it_takes_the_run(self) -> None:
        client = _FakeClient()
        sink = TrackinizerSink(cast(Client, client), "claude", private=True)

        sink.open()
        sink.emit("claude", _event("hidden"))
        sink.close()

        assert [body.private for body in client.started] == [True]
        assert len(client.appended) == 1

    def test_a_fresh_sink_records_no_refusal_so_it_can_still_open(self) -> None:
        """The refusal reason starts EMPTY -- a null would read the same in the
        ``if self._refusal`` guard, so assert the value: it is what lets a sink
        that has met no refusal open instead of re-raising a stored one.
        """
        sink = TrackinizerSink(cast(Client, _FakeClient()), "claude", private=True)
        assert sink._refusal == ""
        sink.open()  # Empty reason -> opens rather than raising PrivateNotHeldError.
        assert sink.session_id is not None

    @pytest.mark.parametrize(
        "answered",
        [None, "open"],
        ids=["an older server", "open"],
    )
    def test_a_private_run_refuses_a_server_that_does_not_hide(
        self,
        tmp_path: Path,
        answered: str | None,
    ) -> None:
        """The session it opened anyway is ended; no later send opens another."""
        client = _FakeClient()
        client.hides, client.audience = False, answered
        primary = TrackinizerSink(
            cast(Client, client),
            "claude",
            private=True,
            instance="b" * 32,
        )
        sink = ResilientSink(primary, fallback_path=tmp_path / "fallback.jsonl")

        with pytest.raises(PrivateNotHeldError):
            _ = sink.open()
        primary.emit("claude", _event("never sent"))
        with pytest.raises(PrivateNotHeldError):
            primary.sync()

        assert len(client.started) == 1
        assert len(client.ended) == 1
        assert client.appended == []
        # Named, so the end of a chat's session another run holds changes nothing.
        [end] = client.end_bodies
        assert end is not None
        assert (end.cli, end.instance) == ("claude", "b" * 32)


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
        metadata = from_plain(client.manifests[0].metadata, dict[str, object])
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
            from_plain(m.metadata, dict[str, object]).get("ascii_escaped")
            for m in client.manifests
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
        sink.sync()
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


class _UnreachableClient(_FlakyFlushClient):
    """A server that stops answering after the session opens: no upload, no end."""

    @override
    def session_end(
        self,
        session_id: UUID,
        body: SessionEnd | None = None,
    ) -> SessionEndResponse:
        del session_id, body
        raise RuntimeError("server gone")


class _OutageClient(_FakeClient):
    """A server that goes down and comes back: while it is down, every call fails."""

    def __init__(self) -> None:
        super().__init__()
        self.down = False
        self.attempts = 0

    @override
    def session_start(self, body: SessionStart) -> SessionStartResponse:
        self._answer()
        return super().session_start(body)

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
        self._answer()
        return super().append_records(
            session_id,
            name=name,
            manifest=manifest,
            records=records,
            restart=restart,
            slash_commands=slash_commands,
        )

    def _answer(self) -> None:
        self.attempts += 1
        if self.down:
            raise RuntimeError("server down")


class TestWorktreeDiffSink:
    """``emit_worktree_diff`` stores the net diff beside the records."""

    def test_diff_part_is_a_restart_with_an_empty_format(self) -> None:
        """The diff is a restart append to a formatless part, so resume skips it."""
        client = _FakeClient()
        sink = _instant_sink(client)
        sink.emit_worktree_diff(
            [
                FileEditResult(
                    call_id="",
                    path="a.py",
                    edits=(Splice(before="old\n", after="new\n"),),
                ),
            ],
        )
        [(_, name, bodies, restart)] = client.appended
        assert name == WORKTREE_DIFF_PART
        assert restart is True
        assert [body.kind for body in bodies] == ["FileEditResult"]
        manifest = client.manifests[-1]
        assert manifest.name == WORKTREE_DIFF_PART
        assert manifest.format == ""
        assert manifest.records == 1

    def test_the_diff_passes_the_redactor(self) -> None:
        """A secret the diff carries is masked, like every other stored record."""
        client = _FakeClient()
        sink = _instant_sink(client, redactor=Redactor({"K": _VALUE}))
        sink.emit_worktree_diff(
            [
                FileEditResult(
                    call_id="",
                    path="a.py",
                    edits=(Splice(before="old\n", after=f"token {_VALUE}\n"),),
                ),
            ],
        )
        stored = _stored_by(client)
        assert _VALUE_ESCAPED not in stored
        assert "[redacted:K]" in stored

    def test_file_sink_writes_the_diff_part(self) -> None:
        """A local-file run writes the diff under the diff part's name."""
        buf = io.StringIO()
        sink = FileSink(buf)
        sink.emit_worktree_diff(
            [
                FileEditResult(
                    call_id="",
                    path="a.py",
                    edits=(Splice(before="old\n", after="new\n"),),
                ),
                FileEditResult(
                    call_id="",
                    path="b.py",
                    edits=(Splice(before="old\n", after="two\n"),),
                ),
            ],
        )
        rows = [
            json_loads(line, dict[str, object]) for line in buf.getvalue().splitlines()
        ]
        assert [row["part_name"] for row in rows] == [
            WORKTREE_DIFF_PART,
            WORKTREE_DIFF_PART,
        ]
        assert {row["kind"] for row in rows} == {"FileEditResult"}


class TestCatchUp:
    """A run that loses the server keeps capturing, and catches it up on return."""

    def test_the_server_gets_everything_once_it_answers_again(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        client, now = _OutageClient(), [0.0]
        fallback_path = tmp_path / "fallback.jsonl"
        sink = _catching_up(client, now=now, fallback_path=fallback_path)
        sink.emit("claude", _event("before"))
        sink.flush()
        client.down = True
        sink.emit("claude", _event("during"))
        sink.flush()
        sink.emit("claude", _event("still down"))

        client.down = False
        now[0] = 30.0
        sink.flush()
        assert _sent_positions(client) == [[0], [1, 2]]
        sink.emit("claude", _event("after"))
        sink.flush()
        sink.close()

        assert _sent_positions(client) == [[0], [1, 2], [3]]
        # The local file holds the outage, numbered as the server numbers it.
        assert [
            (
                json_loads(line, dict[str, object])["text"],
                json_loads(line, dict[str, object])["idx"],
            )
            for line in fallback_path.read_text(encoding="utf-8").splitlines()
        ] == [("during", 1), ("still down", 2)]
        err = capsys.readouterr().err
        assert "[trax run] sync recovered; the server has caught up" in err
        assert "missing" not in err

    def test_a_retry_waits_out_its_interval(self, tmp_path: Path) -> None:
        """An unreachable server is asked once per interval, not once per tick."""
        client, now = _OutageClient(), [0.0]
        client.down = True
        sink = _catching_up(client, now=now, fallback_path=tmp_path / "fallback.jsonl")
        sink.emit("claude", _event("one"))
        sink.flush()
        assert client.attempts == 1

        for tick, attempts in ((29.9, 1), (30.0, 2), (59.9, 2), (60.0, 3)):
            now[0] = tick
            sink.flush()
            assert client.attempts == attempts, tick

    def test_a_run_started_during_an_outage_opens_its_session_on_return(
        self,
        tmp_path: Path,
    ) -> None:
        """It registers when the server is back, stamped with when the run began."""
        client, now = _OutageClient(), [0.0]
        client.down = True
        began = datetime.now(UTC)
        sink = _catching_up(client, now=now, fallback_path=tmp_path / "fallback.jsonl")
        assert sink.open() is None
        sink.emit("claude", _event("first"))
        assert sink.session_id is None

        client.down = False
        now[0] = 30.0
        returned = datetime.now(UTC)
        sink.flush()

        assert sink.session_id == client._id
        started = client.started[-1].started
        assert started is not None
        assert began <= started <= returned
        assert _sent_positions(client) == [[0]]

    def test_a_retry_that_fails_loses_nothing(self, tmp_path: Path) -> None:
        """A held record replayed into a primary still offline stays to be sent."""
        client, now = _OutageClient(), [0.0]
        client.down = True
        sink = _catching_up(client, now=now, fallback_path=tmp_path / "fallback.jsonl")
        assert sink.open() is None
        sink.emit("claude", _event("one"))
        sink.emit("claude", _event("two"))

        now[0] = 30.0
        sink.flush()
        client.down = False
        now[0] = 60.0
        sink.flush()

        assert _sent_positions(client) == [[0, 1]]

    def test_a_backlog_goes_in_requests_the_server_accepts(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A long outage buffers more than one request may carry."""
        monkeypatch.setattr("trackinizer.trax.run.sink.MAX_RECORD_BATCH", 2)
        client = _FakeClient()
        sink = TrackinizerSink(cast(Client, client), cli="claude", batch_size=100)
        sink.emit_slash_command(SlashCommand(command="exit"), _AT)
        for n in range(5):
            sink.emit("claude", _event(str(n)))
        sink.sync()
        sink.restart(_PART)
        for n in range(3):
            sink.emit("claude", _event(f"rewritten {n}"))
        sink.sync()

        assert _sent_positions(client) == [[0, 1], [2, 3], [4], [0, 1], [2]]
        assert [restart for *_, restart in client.appended] == [
            False,
            False,
            False,
            True,
            True,
        ]
        assert [command.command for command in client.slash] == ["exit"]


def _catching_up(
    client: _FakeClient,
    *,
    now: list[float],
    fallback_path: Path,
) -> ResilientSink:
    """Return a sink that sends each record at once, retrying as the runner does."""
    primary = TrackinizerSink(
        cast(Client, client),
        cli="claude",
        flush_interval_sec=0.0,
    )
    return ResilientSink(
        primary,
        fallback_path=fallback_path,
        clock=lambda: now[0],
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


class TestAKeptOpenSessionIsNotEnded:
    """A run a chat refuses leaves the session it shared with the chat's other run.

    ``close`` ends the session its sink opened or re-attached. For a chat's run that
    session is the chat's, so a refused second run that ended it would end the first
    run's too, and take the first run's unread lines with it.
    """

    @pytest.mark.parametrize("wrapped", ["bare", "resilient", "locked"])
    def test_close_flushes_the_records_and_does_not_end_the_session(
        self,
        tmp_path: Path,
        wrapped: str,
    ) -> None:
        client = _FakeClient()
        sink: Sink = TrackinizerSink(cast(Client, client), "claude")
        if wrapped != "bare":
            sink = ResilientSink(sink, fallback_path=tmp_path / "fb.jsonl")
        if wrapped == "locked":
            sink = LockedSink(sink)
        sink.emit("claude", _event("hi"))

        sink.keep_session_open()
        sink.close()

        assert [b.idx for _, _, bodies, _ in client.appended for b in bodies] == [0]
        assert client.ended == []

    def test_a_flush_failure_at_close_still_reaches_the_local_file(
        self,
        tmp_path: Path,
    ) -> None:
        client = _FlakyFlushClient()
        sink = ResilientSink(
            TrackinizerSink(cast(Client, client), "claude"),
            fallback_path=tmp_path / "fb.jsonl",
        )
        sink.emit("claude", _event("one"))

        sink.keep_session_open()
        sink.close()

        assert client.ended == []
        assert "one" in (tmp_path / "fb.jsonl").read_text()

    def test_a_sink_not_told_to_keep_the_session_open_ends_it(self) -> None:
        client = _FakeClient()
        sink = TrackinizerSink(cast(Client, client), "claude")
        sink.emit("claude", _event("hi"))

        sink.close()

        assert client.ended == [client._id]

    @pytest.mark.parametrize("cli", ["claude", "codex"])
    def test_the_end_names_the_runs_cli(self, cli: str) -> None:
        """The server keeps the chat open when a run of a CLI it left ends it."""
        client = _FakeClient()
        sink = TrackinizerSink(cast(Client, client), cli)
        sink.emit(cli, _event("hi"))

        sink.close()

        [end] = client.end_bodies
        assert end is not None
        assert end.cli == cli

    def test_the_end_names_the_run_its_drains_name(self) -> None:
        """The server tells the end of a run a reattach replaced by its instance."""
        client = _FakeClient()
        sink = TrackinizerSink(cast(Client, client), "claude", instance="a" * 32)
        sink.emit("claude", _event("hi"))

        sink.close()

        [end] = client.end_bodies
        assert end is not None
        assert (end.cli, end.instance) == ("claude", "a" * 32)

    def test_a_sink_with_no_server_session_has_nothing_to_keep(self) -> None:
        buf = io.StringIO()
        sink = FileSink(buf)

        sink.keep_session_open()
        sink.close()

        assert buf.closed


class TestDrainPendingIsOnTheProtocol:
    """``pending`` must be part of the ``Sink`` contract, not a getattr.

    ``ResilientSink._degrade`` previously reached it dynamically
    (``getattr(primary, "pending", None)`` + an unchecked cast): a
    rename would type-check clean and silently re-open REV-02 (buffered
    records lost on degrade). On the Protocol, a rename is a type error.
    """

    def test_file_sink_has_empty_drain(self) -> None:
        sink = FileSink(io.StringIO())
        assert sink.pending() == []

    def test_protocol_declares_drain_pending(self) -> None:
        assert hasattr(Sink, "pending"), (
            "pending is not on the Sink Protocol; _degrade must be "
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
    def restart(self, path: Path) -> None:
        del path
        raise RuntimeError("server exploded")

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
    def emit_worktree_diff(self, records: Sequence[FileEditResult]) -> None:
        del records
        raise RuntimeError("server exploded")

    @override
    def flush(self) -> None:
        pass

    @override
    def pending(self) -> list[tuple[Path, RecordBody]]:
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


def _sent_positions(client: _FakeClient) -> list[list[int]]:
    """Return the record positions of each upload ``client`` received, in order."""
    return [[body.idx for body in bodies] for _, _, bodies, _ in client.appended]


def _fallback_texts(path: Path) -> list[str]:
    """Return the ``text`` of each record row the fallback file holds."""
    texts: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json_loads(line, dict[str, object])
        if "text" not in row:
            continue
        text = row["text"]
        assert isinstance(text, str)
        texts.append(text)
    return texts


class TestResilientSink:
    """A failing primary sink must never propagate; it degrades to local file."""

    def test_emit_failure_falls_back_to_file_and_warns_once(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        primary = _ExplodingSink()
        fallback_path = tmp_path / "fallback.jsonl"
        sink = ResilientSink(primary, fallback_path=fallback_path)

        # Two emits: the primary explodes on the first; neither raises.
        sink.emit("claude", _event("one"))
        sink.emit("claude", _event("two"))
        sink.close()

        # Tried once, then once more at exit -- never once per record.
        assert primary.emit_attempts == 2
        # Both records landed in the local fallback file as JSONL.
        assert _fallback_texts(fallback_path) == ["one", "two"]
        # The user is warned once, on stderr, not flooded per-record, and told at
        # exit where the whole run is.
        err = capsys.readouterr().err
        assert err.count("sync failed") == 1
        assert f"falling back to local capture at {fallback_path}" in err
        assert f"all of it is at {fallback_path}" in err

    def test_degrade_drains_orphaned_primary_buffer(self, tmp_path: Path) -> None:
        """Records buffered in the primary must not be lost when it degrades.

        REV-02: a ``TrackinizerSink`` buffers up to ``batch_size`` records
        before flushing. If the flush fails and the wrapper degrades, those
        already-buffered records were stranded -- only the current one reached
        the fallback. They must be replayed to the local file.
        """
        client = _FlakyFlushClient()  # `first` append_records raises.
        primary = TrackinizerSink(cast(Client, client), "claude", batch_size=50)
        fallback_path = tmp_path / "fallback.jsonl"
        sink = ResilientSink(primary, fallback_path=fallback_path)

        # Three records buffer in the primary (below batch_size, no flush yet).
        sink.emit("claude", _event("one"))
        sink.emit("claude", _event("two"))
        sink.emit("claude", _event("three"))
        assert not fallback_path.exists()  # `all` still buffered server-side.

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
            json_loads(line, dict[str, object])["idx"]
            for line in fallback_path.read_text(encoding="utf-8").splitlines()
        ]
        assert idxs == [0, 1]

    def test_a_degraded_run_keeps_its_session_catches_up_and_ends_it(
        self,
        tmp_path: Path,
    ) -> None:
        """One failed upload moves capture to a file, never the session itself.

        Inbound needs only the session id: a poller that lost it stopped for the
        rest of the run while the server kept reporting deliveries. A run that
        never ended its session left it ``active`` for good. And the server, once
        it answers again, gets what it missed.
        """
        client = _FlakyFlushClient()
        primary = TrackinizerSink(
            cast(Client, client),
            "claude",
            batch_size=50,
            flush_interval_sec=0.0,
        )
        fallback_path = tmp_path / "fallback.jsonl"
        sink = ResilientSink(primary, fallback_path=fallback_path)
        sink.emit("claude", _event("one"))
        sink.flush()

        assert sink.session_id == client._id
        sink.emit("claude", _event("two"))
        sink.close()
        assert _fallback_texts(fallback_path) == ["one", "two"]
        assert client.ended == [client._id]
        assert client.append_attempts == 2
        assert _sent_positions(client) == [[0, 1]]

    def test_a_failed_end_after_a_degrade_is_reported_not_raised(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        client = _UnreachableClient()
        primary = TrackinizerSink(
            cast(Client, client),
            "claude",
            batch_size=50,
            flush_interval_sec=0.0,
        )
        sink = ResilientSink(primary, fallback_path=tmp_path / "fallback.jsonl")
        sink.emit("claude", _event("one"))
        sink.flush()

        sink.close()

        assert "could not close the server session (server gone)" in (
            capsys.readouterr().err
        )

    def test_each_fallback_line_is_on_disk_before_close(self, tmp_path: Path) -> None:
        """A run killed before ``close`` keeps every line it already wrote."""
        fallback_path = tmp_path / "fallback.jsonl"
        sink = ResilientSink(_ExplodingSink(), fallback_path=fallback_path)
        sink.emit("claude", _event("one"))

        assert _fallback_texts(fallback_path) == ["one"]
        sink.close()

    def test_the_fallback_directory_is_created(self, tmp_path: Path) -> None:
        fallback_path = tmp_path / "state" / "trax" / "run" / "fallback.jsonl"
        sink = ResilientSink(_ExplodingSink(), fallback_path=fallback_path)
        sink.emit("claude", _event("one"))
        sink.close()

        assert _fallback_texts(fallback_path) == ["one"]

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
        # The session id is None -- no server session has opened yet.
        assert sink.session_id is None

        # Capture still works: a later emit writes to the local fallback file.
        sink.emit("claude", _event("after open failed"))
        assert primary.emit_attempts == 0
        sink.close()
        assert _fallback_texts(fallback_path) == ["after open failed"]
        # The record reached the primary once it answered, at the exit attempt.
        assert primary.emit_attempts == 1

    def test_the_record_that_triggered_the_degrade_lands_once(
        self,
        tmp_path: Path,
    ) -> None:
        """The record whose emit failed must not be written twice.

        A ``TrackinizerSink.emit`` buffers the record BEFORE the batch flush,
        so an ``append_records`` failure raises with that record still in the
        buffer. ``_degrade`` replays the buffer into the fallback -- including
        it -- and then ``emit`` falls through and writes it again under a fresh
        position. Two rows for one turn.
        """
        client = _FlakyFlushClient()  # The first append_records raises.
        primary = TrackinizerSink(cast(Client, client), "claude", batch_size=1)
        fallback_path = tmp_path / "fallback.jsonl"
        sink = ResilientSink(primary, fallback_path=fallback_path)

        sink.emit("claude", _event("only"))
        sink.close()

        assert _fallback_texts(fallback_path) == ["only"]

    def test_a_degrade_with_nothing_buffered_still_writes_the_record(
        self,
        tmp_path: Path,
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

        sink.emit_slash_command(SlashCommand(command="exit"), _AT)  # Must not raise.
        sink.close()

        rows = [
            json_loads(line, dict[str, object])
            for line in fallback_path.read_text(encoding="utf-8").splitlines()
        ]
        command = rows[0]["slash_command"]
        assert isinstance(command, dict)
        assert command["command"] == "exit"

    def test_set_cli_session_id_failure_degrades(self, tmp_path: Path) -> None:
        """Every primary call degrades on failure -- including this one.

        It was the one call reaching the primary with no guard, so a server
        that failed there raised straight into the drain thread and ended
        capture, contradicting the wrapper's whole contract.
        """
        primary = _FailingSessionIdPrimary()
        fallback_path = tmp_path / "fallback.jsonl"
        sink = ResilientSink(primary, fallback_path=fallback_path)

        sink.set_cli_session_id("claude-abc-123")  # Must not raise.

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
        rows = [
            json_loads(line, dict[str, object]) for line in buf.getvalue().splitlines()
        ]
        assert rows[-1]["text"] == "hello\n"

    def test_on_record_sees_each_record_the_chunk_produced(self) -> None:
        """The diff capture reads edited paths through ``on_record``: one per record."""
        buf = io.StringIO()
        sink = FileSink(buf)
        seen: list[TraxRecord] = []
        kinds = sink.feed(IOStreamAdapter(), _PART, b"hello\n", on_record=seen.append)
        assert [type(record).__name__ for record in seen] == kinds

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
        rows = [
            json_loads(line, dict[str, object]) for line in buf.getvalue().splitlines()
        ]
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
        rows = [
            json_loads(line, dict[str, object]) for line in buf.getvalue().splitlines()
        ]
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
            def reader(self) -> Tail[TraxRecord]:
                built.append(self.name)
                return super().reader()

        sink = FileSink(io.StringIO())
        sink.feed(_Recording(), _PART, b"x\n")
        assert built == ["sh"], "the sink did not ask the adapter for its reader"

    def test_annotate_rewrites_each_record_before_it_is_seen_and_kept(self) -> None:
        buf = io.StringIO()
        sink = FileSink(buf)
        seen: list[TraxRecord] = []

        def shout(record: TraxRecord) -> TraxRecord:
            return Stdout(text="HI\n") if isinstance(record, Stdout) else record

        _ = sink.feed(
            IOStreamAdapter(),
            _PART,
            b"hi\n",
            on_record=seen.append,
            annotate=shout,
        )

        assert seen[-1] == Stdout(text="HI\n")
        rows = [
            json_loads(line, dict[str, object]) for line in buf.getvalue().splitlines()
        ]
        assert rows[-1]["text"] == "HI\n"


class TestLoadedHistoryIsNotUploaded:
    """A resume's loaded history is read, so positions run on, but never uploaded."""

    def test_the_first_record_uploaded_sits_where_the_history_ends(self) -> None:
        client = _FakeClient()
        sink = LockedSink(
            ResilientSink(
                TrackinizerSink(cast(Client, client), "sh"),
                fallback_path=Path("/nonexistent/fallback.jsonl"),
            ),
        )
        adapter = IOStreamAdapter()
        seen: list[TraxRecord] = []

        held = sink.feed(
            adapter,
            _PART,
            b"loaded\n",
            upload=False,
            on_record=seen.append,
        )
        kinds = sink.feed(adapter, _PART, b"new\n")
        sink.close()

        assert held == []
        assert seen == []
        assert kinds == ["Stdout"]
        [(_, _, bodies, _)] = client.appended
        assert [(body.idx, body.kind) for body in bodies] == [(3, "Stdout")]
        assert client.manifests[0].records == 4

    def test_a_local_file_numbers_on_from_the_history_too(self) -> None:
        buf = io.StringIO()
        sink = FileSink(buf)
        adapter = IOStreamAdapter()

        _ = sink.feed(adapter, _PART, b"loaded\n", upload=False)
        _ = sink.feed(adapter, _PART, b"new\n")

        rows = [
            json_loads(line, dict[str, object]) for line in buf.getvalue().splitlines()
        ]
        assert [(r["idx"], r["text"]) for r in rows] == [(3, "new\n")]

    def test_a_degraded_run_numbers_its_local_copy_from_the_history(
        self,
        tmp_path: Path,
    ) -> None:
        fallback = tmp_path / "fallback.jsonl"
        sink = ResilientSink(_ExplodingSink(), fallback_path=fallback)

        sink.emit("sh", _event("trips the degrade"))
        sink.pass_over(_OTHER)
        sink.emit("sh", _event("after", path=_OTHER))
        sink.close()

        rows = [
            json_loads(line, dict[str, object])
            for line in fallback.read_text().splitlines()
        ]
        assert [(r["part_name"], r["idx"]) for r in rows] == [
            ("a.jsonl", 0),
            ("b.jsonl", 1),
        ]


class TestAttachedRun:
    """A run attached to a session writes into it and never renames it."""

    def test_the_start_names_the_session_and_no_cli_session_id(self) -> None:
        client = _FakeClient()
        attach = uuid4()
        sink = TrackinizerSink(cast(Client, client), "claude", attach=attach)

        sink.set_cli_session_id("claude-abc-123")
        _ = sink.open()
        sink.set_cli_session_id("claude-abc-123")
        sink.close()

        [start] = client.started
        assert start.attach == attach
        assert start.cli_session_id is None
        [end] = client.end_bodies
        assert end is not None
        assert end.cli_session_id is None

    def test_a_run_not_attached_sends_no_attach(self) -> None:
        client = _FakeClient()
        sink = TrackinizerSink(cast(Client, client), "claude")

        _ = sink.open()

        assert client.started[0].attach is None


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
    def restart(self, path: Path) -> None:
        del path
        self._record("restart", "enter")
        self._record("restart", "exit")

    @override
    def emit(self, adapter_name: str, event: Event) -> None:
        del adapter_name, event
        self._record("emit", "enter")
        self._record("emit", "exit")

    @override
    def emit_slash_command(self, command: SlashCommand, at: datetime) -> None:
        del command, at

    @override
    def emit_worktree_diff(self, records: Sequence[FileEditResult]) -> None:
        del records

    @override
    def flush(self) -> None:
        self._record("flush", "enter")
        self.entered.set()
        self._release.wait(5.0)
        self._record("flush", "exit")

    @override
    def pending(self) -> list[tuple[Path, RecordBody]]:
        return []

    @override
    def close(self) -> None:
        self._record("close", "enter")
        self._record("close", "exit")


class TestWrappedSinksSendEachFilesEncoding:
    """A synced run tells the server how each file spells its bytes.

    The runner feeds the outermost wrapper, so a reader the wrapper kept for itself
    was invisible to the server sink inside, and every manifest declared nothing.
    A resumed transcript is then written back in different bytes than the CLI wrote.
    """

    def test_the_runner_stack_sends_what_a_bare_server_sink_sends(
        self,
        tmp_path: Path,
    ) -> None:
        line = (
            b'{"type":"user","sessionId":"s","uuid":"u1",'
            b'"timestamp":"2026-10-03T12:00:00Z",'
            b'"message":{"role":"user","content":"\\u00e9t\\u00e9"}}\n'
        )
        bare_client, wrapped_client = _FakeClient(), _FakeClient()
        bare = _instant_sink(bare_client)
        wrapped = LockedSink(
            ResilientSink(
                _instant_sink(wrapped_client),
                fallback_path=tmp_path / "fallback.jsonl",
            ),
        )

        for sink in (bare, wrapped):
            _ = sink.feed(ClaudeAdapter(), path=tmp_path / "s.jsonl", raw=line)
            sink.flush()

        declared = [manifest.metadata for manifest in bare_client.manifests]
        assert declared != [{}], "the bare sink declared nothing either"
        assert [manifest.metadata for manifest in wrapped_client.manifests] == declared


def _instant_sink(
    client: _FakeClient,
    *,
    redactor: Redactor | None = None,
) -> TrackinizerSink:
    """Return a server sink that sends each record as soon as it is flushed."""
    return TrackinizerSink(
        cast(Client, client),
        cli="claude",
        flush_interval_sec=0.0,
        redactor=redactor,
    )


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
        sink.sync()
        assert sink.session_id == client._id
        sink.close()
        assert client.ended == [client._id]

    def test_a_second_close_is_a_no_op(self) -> None:
        """The runner closes mid-teardown, then again on every path.

        A second pass would retry a degraded sink's catch-up against the server
        that already failed it.
        """
        inner = _BlockingSink(threading.Event())
        sink = LockedSink(inner)
        sink.close()
        sink.close()
        assert inner.log == [("close", "enter"), ("close", "exit")]

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


@pytest.mark.parametrize("destination", ["file", "server", "locked", "resilient"])
@pytest.mark.parametrize("batch_size", [1, 2, 50])
def test_consecutive_restart_chunks(
    tmp_path: Path,
    destination: str,
    batch_size: int,
) -> None:
    client = _FakeClient()
    output = io.StringIO()
    primary = TrackinizerSink(cast(Client, client), "sh", batch_size=batch_size)
    sink: Sink = FileSink(output) if destination == "file" else primary
    if destination == "locked":
        sink = LockedSink(primary)
    if destination == "resilient":
        sink = ResilientSink(primary, fallback_path=tmp_path / "fallback.jsonl")
    readers: list[Tail[TraxRecord]] = []
    try:
        for index in range(3):
            sink.feed(IOStreamAdapter(), _PART, b"one\n", restart=index > 0)
            owner = primary if destination == "locked" else sink
            readers.append(owner.readers[_PART])
        if destination == "file":
            positions = [
                json_loads(line, dict[str, object])["idx"]
                for line in output.getvalue().splitlines()
            ]
        else:
            sink.close()
            positions = [b.idx for _, _, bodies, _ in client.appended for b in bodies]
        assert positions == [0, 1, 2] * 3
    finally:
        for reader in readers:
            reader.close()
            if reader._reader is not None:
                reader._reader.join(timeout=2)
        sink.close()


def test_restart_closes_displaced_reader_without_emitting_eof() -> None:
    output = io.StringIO()
    sink = FileSink(output)
    sink.feed(IOStreamAdapter(), _PART, b"unterminated")
    displaced = sink.readers[_PART]
    sink.feed(IOStreamAdapter(), _PART, b"new\n", restart=True)
    assert displaced._reader is not None
    reader = displaced._reader
    try:
        reader.join(timeout=1)
        assert not reader.is_alive()
        rows = [
            json_loads(line, dict[str, object])
            for line in output.getvalue().splitlines()
        ]
        assert [row["idx"] for row in rows] == [0, 1, 2] * 2
        assert [row["kind"] for row in rows] == [
            "TurnContext",
            "ContextClear",
            "Stdout",
        ] * 2
    finally:
        displaced.close()
        reader.join(timeout=2)
        for open_reader in sink.readers.values():
            open_reader.close()
        sink.close()


@pytest.mark.parametrize("server", [False, True])
def test_repeated_claude_replacement_pipeline(tmp_path: Path, *, server: bool) -> None:
    output = io.StringIO()
    client = _FakeClient()
    sink: Sink = (
        TrackinizerSink(cast(Client, client), "claude", batch_size=1)
        if server
        else FileSink(output)
    )
    readers: list[Tail[TraxRecord]] = []
    try:
        asyncio.run(_replace_claude(tmp_path, sink, readers))
        positions = (
            [b.idx for _, _, bodies, _ in client.appended for b in bodies]
            if server
            else [
                json_loads(line, dict[str, object])["idx"]
                for line in output.getvalue().splitlines()
            ]
        )
        assert positions == list(range(5)) * 3
        if server:
            assert [restart for _, _, _, restart in client.appended] == [False] * 5 + [
                True,
            ] * 10
    finally:
        for reader in readers:
            reader.close()
            if reader._reader is not None:
                reader._reader.join(timeout=2)
        sink.close()


def _is_async_generator[T](
    value: AsyncIterator[T],
) -> TypeGuard[AsyncGenerator[T, None]]:
    return isinstance(value, AsyncGenerator)


async def _replace_claude(
    root: Path,
    sink: Sink,
    readers: list[Tail[TraxRecord]],
) -> None:
    target = root / "log.jsonl"
    target.write_text('{"type":"user","message":{"role":"user","content":"one"}}\n')
    lines = follow_tree(root, match=lambda p: p == target, replay=True)
    assert _is_async_generator(lines)
    async with contextlib.aclosing(lines):
        for index, text in enumerate(("one", "two", "six")):
            if index:
                staged = root / "replacement"
                staged.write_text(
                    json.dumps(
                        {"type": "user", "message": {"role": "user", "content": text}},
                    )
                    + "\n",
                )
                staged.replace(target)
            line = await asyncio.wait_for(anext(lines), 2)
            assert line.restart == (index > 0)
            sink.feed(
                ClaudeAdapter(),
                line.path,
                (line.text + "\n").encode(),
                restart=line.restart,
            )
            readers.append(sink.readers[target])


def test_restart_flush_failure_preserves_old_positions_in_fallback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _FlakyFlushClient()
    primary = TrackinizerSink(cast(Client, client), "sh")
    fallback = tmp_path / "fallback.jsonl"
    sink = ResilientSink(primary, fallback_path=fallback)
    try:
        sink.feed(IOStreamAdapter(), _PART, b"old\n")
        sink.feed(IOStreamAdapter(), _PART, b"new\n", restart=True)
        sink.feed(IOStreamAdapter(), _PART, b"latest\n", restart=True)
        sink.flush()
        rows = [
            json_loads(line, dict[str, object])
            for line in fallback.read_text().splitlines()
        ]
        assert [row["idx"] for row in rows] == [0, 1, 2] * 3
        assert all(row["adapter"] == "sh" for row in rows)
    finally:
        for reader in sink.readers.values():
            reader.close()
        sink.close()
    assert caplog.records == []
    failed, recovered = capsys.readouterr().err.splitlines()
    assert "transient flush failure" in failed
    assert str(fallback) in failed
    assert recovered == "[trax run] sync recovered; the server has caught up"


@pytest.mark.parametrize("server", [False, True])
def test_empty_restart_chunk_resets_only_its_part(*, server: bool) -> None:
    output = io.StringIO()
    client = _FakeClient()
    sink: Sink = (
        TrackinizerSink(cast(Client, client), "sh", batch_size=1)
        if server
        else FileSink(output)
    )
    try:
        sink.emit("sh", _event("one"))
        sink.emit("sh", _event("two"))
        sink.emit("sh", _event("other", path=_OTHER))
        assert sink.feed(IOStreamAdapter(), _PART, b"", restart=True) == []
        sink.emit("sh", _event("new"))
        sink.emit("sh", _event("other again", path=_OTHER))
        if server:
            rows = [
                (name, body.idx)
                for _, name, bodies, _ in client.appended
                for body in bodies
            ]
            assert [restart for _, _, _, restart in client.appended] == [
                False,
                False,
                False,
                True,
                False,
            ]
        else:
            parsed = [
                json_loads(line, dict[str, object])
                for line in output.getvalue().splitlines()
            ]
            rows = [(row["part_name"], row["idx"]) for row in parsed]
        assert rows == [
            (_PART.name, 0),
            (_PART.name, 1),
            (_OTHER.name, 0),
            (_PART.name, 0),
            (_OTHER.name, 1),
        ]
    finally:
        for reader in sink.readers.values():
            reader.close()
        sink.close()


def test_locked_sink_delegates_restart_and_lifecycle() -> None:
    client = _FakeClient()
    inner = TrackinizerSink(cast(Client, client), "sh", batch_size=1)
    sink = LockedSink(inner)
    sink.open()
    sink.set_cli_session_id("native-id")
    sink.emit("sh", _event("old"))
    sink.restart(_PART)
    sink.emit("sh", _event("new", restart=True))
    sink.emit_slash_command(SlashCommand(command="exit"), _AT)
    assert sink.pending() == []
    sink.close()
    assert [body.idx for _, _, bodies, _ in client.appended for body in bodies] == [
        0,
        0,
    ]
    assert client.end_bodies[0] is not None
    assert client.end_bodies[0].cli_session_id == "native-id"


@pytest.mark.parametrize("batch_size", [1, 2, 50])
def test_replacement_overwrites_every_reused_position(batch_size: int) -> None:
    client = _FakeClient()
    sink = TrackinizerSink(cast(Client, client), "sh", batch_size=batch_size)
    try:
        for version, line_count in enumerate((3, 1, 2, 4)):
            for line in range(line_count):
                sink.feed(
                    IOStreamAdapter(),
                    _PART,
                    f"version-{version}-line-{line}\n".encode(),
                    restart=version > 0 and line == 0,
                )
        sink.close()
        seen: set[int] = set()
        for _, _, bodies, restart in client.appended:
            for body in bodies:
                if body.idx in seen:
                    assert restart, f"position {body.idx} would retain its obsolete row"
                seen.add(body.idx)
        assert seen == set(range(6))
        if batch_size == 1:
            assert not client.appended[-1][3]
    finally:
        for reader in sink.readers.values():
            reader.close()
        sink.close()


# Its raw form holds a quote and a newline, so its JSON-escaped form differs from it.
_VALUE = 'sec"ret\nvalue1'
_VALUE_ESCAPED = json.dumps(_VALUE)[1:-1]
_REDACTOR = Redactor({"K": _VALUE})


class TestRedaction:
    """A delivered secret reaches no sink, whichever way the record travels."""

    def test_the_out_file_holds_no_value(self) -> None:
        buf = io.StringIO()
        sink = FileSink(buf, redactor=_REDACTOR)
        sink.emit("claude", _event(_carrying_the_secret()))
        sink.emit_slash_command(
            SlashCommand(command=_VALUE, args=_carrying_the_secret()),
            _AT,
        )
        _assert_redacted(buf.getvalue())

    def test_the_server_gets_no_value(self) -> None:
        client = _FakeClient()
        sink = _instant_sink(client, redactor=_REDACTOR)
        sink.emit("claude", _event(_carrying_the_secret()))
        sink.emit_slash_command(
            SlashCommand(command=_VALUE, args=_carrying_the_secret()),
            _AT,
        )
        sink.close()
        _assert_redacted(_stored_by(client))

    def test_the_fallback_file_holds_no_value(self, tmp_path: Path) -> None:
        fallback_path = tmp_path / "fallback.jsonl"
        sink = ResilientSink(
            _ExplodingSink(),
            fallback_path=fallback_path,
            redactor=_REDACTOR,
        )
        sink.emit("claude", _event(_carrying_the_secret()))
        sink.emit_slash_command(
            SlashCommand(command=_VALUE, args=_carrying_the_secret()),
            _AT,
        )
        sink.close()
        _assert_redacted(fallback_path.read_text(encoding="utf-8"))

    def test_records_the_server_never_got_reach_the_fallback_redacted(
        self,
        tmp_path: Path,
    ) -> None:
        client = _FlakyFlushClient()
        fallback_path = tmp_path / "fallback.jsonl"
        sink = ResilientSink(
            TrackinizerSink(cast(Client, client), "claude", redactor=_REDACTOR),
            fallback_path=fallback_path,
            redactor=_REDACTOR,
        )
        sink.emit("claude", _event(_carrying_the_secret()))
        sink.flush()
        sink.close()
        _assert_redacted(fallback_path.read_text(encoding="utf-8"))

    @pytest.mark.parametrize("server", [False, True])
    def test_a_fed_chunk_through_lockedsink_is_redacted(
        self,
        tmp_path: Path,
        *,
        server: bool,
    ) -> None:
        buf = io.StringIO()
        client = _FakeClient()
        sink = LockedSink(
            _instant_sink(client, redactor=_REDACTOR)
            if server
            else FileSink(buf, redactor=_REDACTOR),
        )
        raw = (_carrying_the_secret().replace("\n", " ") + "\n").encode()
        raw += _VALUE.encode() + b"\n"
        _ = sink.feed(IOStreamAdapter(), tmp_path / "a.jsonl", raw)
        for reader in sink.readers.values():
            reader.close()
        sink.flush()
        _assert_redacted(_stored_by(client) if server else buf.getvalue())
        sink.close()

    @pytest.mark.parametrize("server", [False, True])
    def test_a_multi_line_value_framed_into_lines_is_redacted(
        self,
        tmp_path: Path,
        *,
        server: bool,
    ) -> None:
        # The pump hands the sink one framed line per call, each ending ``\r\n``
        # as a pty writes it, so no call holds the whole value.
        pem = (
            "-----BEGIN KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n"
            "AmFrZXNlY3JldGJvZHk=\n-----END KEY-----"
        )
        buf = io.StringIO()
        client = _FakeClient()
        redactor = Redactor({"K": pem})
        sink = LockedSink(
            _instant_sink(client, redactor=redactor)
            if server
            else FileSink(buf, redactor=redactor),
        )
        path = tmp_path / "a.jsonl"

        def feed_line(raw: bytes) -> None:
            _ = sink.feed(IOStreamAdapter(), path=path, raw=raw)

        capture = LineCapture(feed_line)
        capture.feed(pem.replace("\n", "\r\n").encode() + b"\r\n")
        capture.close()
        for reader in sink.readers.values():
            reader.close()
        sink.flush()
        stored = _stored_by(client) if server else buf.getvalue()
        sink.close()
        assert "[redacted:K]" in stored
        for line in pem.split("\n"):
            assert line not in stored

    def test_a_sink_without_a_redactor_keeps_the_value(self) -> None:
        buf = io.StringIO()
        sink = FileSink(buf)
        sink.emit("claude", _event(_carrying_the_secret()))
        assert _VALUE_ESCAPED in buf.getvalue()

    def test_a_secret_cut_in_two_by_truncation_is_still_redacted(self) -> None:
        """Redacted BEFORE the cut: the redactor knows a value's prefix, not its tail.

        Cutting first leaves the end of a value at the start of the kept tail,
        where no redactor pattern reaches it.
        """
        secret = "".join(f"{n:05d}" for n in range(400))
        client = _FakeClient()
        sink = _instant_sink(client, redactor=Redactor({"K": secret}))
        # Mostly secret, so a cut lands inside one; still over budget once masked.
        shell = ShellCommandResult(call_id="c", stdout=f"{secret} {'x' * 300}\n" * 420)
        sink.emit("claude", Event(record=shell, path=_PART))
        sink.close()
        stored = _stored_by(client)
        assert "Trackinizer capture truncated" in stored
        # Every 16-character run of the stored text, so each probe is one lookup.
        runs = {stored[n : n + 16] for n in range(len(stored) - 15)}
        leaked = [n for n in range(len(secret) - 16) if secret[n : n + 16] in runs]
        assert not leaked


class TestOneRecordOverTheProxyLimit:
    """One captured record larger than the proxy accepts must still upload.

    Uploaded through the real :class:`Client` to a transport that refuses what
    the live proxy refuses, so the bytes measured are the bytes sent.
    """

    @pytest.mark.compute_large_fixture
    def test_the_record_that_hit_413_uploads_with_its_marker(self) -> None:
        """~1 MB of shell output, stated three times by codex and once for search."""
        line = _codex_shell_line((_WORKER_LOG * 10_000)[:1_050_000])
        reader = CodexAdapter().reader()
        (failing,) = [
            r for r in reader.feed(line.decode()) if isinstance(r, ShellCommandResult)
        ]
        assert encoded_size(_uncut(failing)) > _PROXY_LIMIT
        server = _Server()
        sink = TrackinizerSink(server.client(), "codex")

        _ = sink.feed(CodexAdapter(), _PART, line)
        sink.close()

        assert server.uploads
        assert max(len(upload) for upload in server.uploads) <= _PROXY_LIMIT
        (shell,) = [r for r in server.read_back() if isinstance(r, ShellCommandResult)]
        assert was_truncated(shell)
        item = from_plain(shell.extra["item"], dict[str, object])
        for kept in (shell.stdout, item["aggregated_output"], item["formatted_output"]):
            assert isinstance(kept, str)
            assert (
                "[Trackinizer capture truncated: kept beginning/end; omitted " in kept
            )

    @pytest.mark.compute_large_fixture
    @pytest.mark.parametrize("kind", sorted(_OVERSIZED))
    def test_a_record_of_every_kind_uploads(self, kind: str) -> None:
        """Tool results are the usual culprit, but any kind can hold 8 MB."""
        record = _OVERSIZED[kind](_OVER_THE_LIMIT)
        assert encoded_size(_uncut(record)) > _PROXY_LIMIT
        server = _Server()
        sink = TrackinizerSink(server.client(), "claude")

        sink.emit("claude", Event(record=record, path=_PART))
        sink.close()

        assert max(len(upload) for upload in server.uploads) <= _PROXY_LIMIT
        (stored,) = server.read_back()
        assert type(stored) is type(record)
        assert was_truncated(stored)

    def test_every_kind_has_an_oversized_example(self) -> None:
        """A kind added to the store must be proven to upload, too."""
        assert set(_OVERSIZED) == set(_BY_KIND)


class TestTheBatchBytesBound:
    """A batch is bounded by its bytes, not only by its count."""

    def test_mid_size_records_go_in_requests_under_4_mib(self) -> None:
        """Twelve 400 KiB records: no count trigger fires, and no one record is cut."""
        server = _Server()
        sink = TrackinizerSink(server.client(), "claude", batch_size=50)
        for n in range(12):
            sink.emit("claude", _event(f"{n}" * (200 << 10)))
        assert server.uploads, "nothing was sent before the count trigger"

        sink.close()

        assert len(server.uploads) > 1
        assert max(len(upload) for upload in server.uploads) <= (4 << 20) + (1 << 10)
        assert [body.idx for body in server.records()] == list(range(12))

    def test_a_record_larger_than_a_batch_goes_alone(self) -> None:
        client = _FakeClient()
        sink = TrackinizerSink(cast(Client, client), "claude", batch_bytes=1_000)
        sink.emit("claude", _event("small"))
        sink.emit("claude", _event("x" * 2_000))
        sink.emit("claude", _event("small"))
        sink.close()

        assert _sent_positions(client) == [[0], [1], [2]]


def test_the_out_file_holds_the_bounded_record() -> None:
    """Local capture and upload share one bound: the file is what the server gets."""
    buf = io.StringIO()
    sink = FileSink(buf)
    shell = ShellCommandResult(call_id="c", stdout="x" * MAX_TOOL_RESULT_BYTES)

    sink.emit("codex", Event(record=shell, path=_PART))

    (line,) = buf.getvalue().splitlines()
    assert "Trackinizer capture truncated" in from_plain(
        json_loads(line, dict[str, object])["text"],
        str,
    )
    assert len(line) < 2 * MAX_TOOL_RESULT_BYTES


def _carrying_the_secret() -> str:
    return f"raw {_VALUE} escaped {_VALUE_ESCAPED}"


def _assert_redacted(stored: str) -> None:
    """Fail unless ``stored`` holds the placeholder and no form of the secret."""
    assert "[redacted:K]" in stored
    assert _VALUE not in stored
    assert _VALUE_ESCAPED not in stored
    assert json.dumps(_VALUE) not in stored


def _stored_by(client: _FakeClient) -> str:
    """Return everything ``client`` was sent, as JSON text."""
    bodies = [body for _, _, batch, _ in client.appended for body in batch]
    return "".join(body.model_dump_json() for body in bodies) + "".join(
        body.model_dump_json() for body in client.slash
    )


class _Server:
    """Answers as Trackinizer does, behind a proxy that refuses a body over 8 MB."""

    def __init__(self) -> None:
        self.session_id = uuid4()
        self.uploads: list[bytes] = []

    def client(self) -> Client:
        """Return a real client whose requests reach this server and nothing else."""
        client = Client("http://server")
        client._http.close()
        client._http = httpx2.Client(
            base_url=client.base_url,
            transport=httpx2.MockTransport(self._answer),
        )
        return client

    def records(self) -> list[RecordBody]:
        """Return every record body uploaded, in order."""
        return [
            body
            for upload in self.uploads
            for body in AppendRecordsRequest.model_validate_json(upload).records
        ]

    def read_back(self) -> list[TraxRecord]:
        """Return every uploaded record as a server reader decodes it."""
        return [body.row(self.session_id, 0).record() for body in self.records()]

    def _answer(self, request: httpx2.Request) -> httpx2.Response:
        if len(request.content) > _PROXY_LIMIT:
            return httpx2.Response(413, text="Request Entity Too Large")
        path = request.url.path
        if path.endswith("/records"):
            self.uploads.append(request.content)
            return httpx2.Response(200, json={"written": 0, "skipped": 0})
        if path.endswith("/start"):
            return httpx2.Response(200, json={"id": str(self.session_id), "seq": 0})
        return httpx2.Response(200, json={"id": str(self.session_id)})


# Codex states the output three times -- ``stdout``, ``aggregated_output`` and
# ``formatted_output`` -- and capture adds a fourth copy for search.
def _codex_shell_line(output: str) -> bytes:
    """Return one codex rollout line for a shell command that printed ``output``."""
    item = {
        "type": "CommandExecution",
        "id": "exec-1",
        "command": [
            "/bin/bash",
            "-lc",
            "find runs -name '*.log' -exec sed -n '1,180p' {} \\;",
        ],
        "status": "completed",
        "stdout": output,
        "stderr": "",
        "aggregated_output": output,
        "exit_code": 0,
        "duration": {"secs": 3, "nanos": 0},
        "formatted_output": output,
    }
    event = {
        "timestamp": "2026-09-17T22:45:02.000Z",
        "type": "event_msg",
        "payload": {"type": "item_completed", "item": item},
    }
    return (json.dumps(event) + "\n").encode()


def _uncut(record: TraxRecord) -> RecordBody:
    """Return the wire body ``record`` had before capture bounded records."""
    return RecordBody.of(
        SessionRecordRow.of(session_id=uuid4(), part=0, idx=0, record=record),
    )


class _RefusingClient(_FakeClient):
    """Refuses, as the server does a value it cannot store, any batch with a NUL."""

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
        if any("\x00" in body.text for body in cast(list[RecordBody], records)):
            raise ClientError("cannot store", status_code=422)
        return super().append_records(
            session_id,
            name=name,
            manifest=manifest,
            records=records,
            restart=restart,
            slash_commands=slash_commands,
        )


def _stored_kinds(client: _FakeClient) -> dict[int, str]:
    return {
        body.idx: body.kind for _, _, bodies, _ in client.appended for body in bodies
    }


class TestARefusedRecord:
    """A record the server can never store must not hold back the rest of a run."""

    def test_it_is_stood_in_and_every_other_record_and_command_is_sent(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        client = _RefusingClient()
        sink = TrackinizerSink(cast(Client, client), "claude", batch_size=50)
        for text in ("one", "binary \x00 output", "three", "four"):
            sink.emit("claude", _event(text))
        sink.emit_slash_command(SlashCommand(command="model", args="opus"), _AT)

        sink.close()

        assert _stored_kinds(client) == {
            0: "UserMessage",
            1: "UncategorizedRecord",
            2: "UserMessage",
            3: "UserMessage",
        }
        assert [command.command for command in client.slash] == ["model"]
        assert client.ended == [client._id]
        assert "refused record 1 of a.jsonl (UserMessage)" in capsys.readouterr().err

    def test_a_run_whose_record_is_refused_never_falls_back_to_local_capture(
        self,
        tmp_path: Path,
    ) -> None:
        client = _RefusingClient()
        fallback = tmp_path / "run.jsonl"
        sink = ResilientSink(
            TrackinizerSink(cast(Client, client), "claude", batch_size=2),
            fallback_path=fallback,
        )
        for text in ("one", "binary \x00 output", "three", "four"):
            sink.emit("claude", _event(text))

        sink.close()

        assert not fallback.exists()
        assert sorted(_stored_kinds(client)) == [0, 1, 2, 3]


_RUN_PART = Path("prompts-1")


class TestRunRecords:
    """``emit_run_record`` stores what the run writes itself on a formatless part."""

    def test_a_run_record_is_sent_at_once_on_a_formatless_part(self) -> None:
        client = _FakeClient()
        sink = TrackinizerSink(cast(Client, client), cli="claude")
        sink.emit("claude", _event("turn"))

        sink.emit_run_record(
            Event(record=SystemMessage(content="asks"), path=_RUN_PART),
        )
        sink.emit_run_record(
            Event(record=SystemMessage(content="answered"), path=_RUN_PART),
        )

        sent = [
            (name, body.idx)
            for _, name, bodies, _ in client.appended
            for body in bodies
        ]
        assert sent == [(_PART.name, 0), (_RUN_PART.name, 0), (_RUN_PART.name, 1)]
        formats = {manifest.name: manifest.format for manifest in client.manifests}
        assert formats == {_PART.name: "claude", _RUN_PART.name: ""}

    def test_a_file_sink_writes_it_under_the_runs_part(self) -> None:
        buf = io.StringIO()

        FileSink(buf).emit_run_record(
            Event(record=SystemMessage(content="asks"), path=_RUN_PART),
        )

        row = json_loads(buf.getvalue(), dict[str, object])
        assert (row["part_name"], row["kind"], row["idx"]) == (
            _RUN_PART.name,
            "SystemMessage",
            0,
        )

    def test_the_wrapping_sinks_hand_it_on_as_a_run_record(
        self,
        tmp_path: Path,
    ) -> None:
        client = _FakeClient()
        sink = LockedSink(
            ResilientSink(
                TrackinizerSink(cast(Client, client), cli="claude"),
                fallback_path=tmp_path / "fallback.jsonl",
            ),
        )

        sink.emit_run_record(
            Event(record=SystemMessage(content="asks"), path=_RUN_PART),
        )

        assert [manifest.format for manifest in client.manifests] == [""]

    def test_an_outage_keeps_it_locally_and_sends_it_on_return(
        self,
        tmp_path: Path,
    ) -> None:
        client, now = _OutageClient(), [0.0]
        fallback_path = tmp_path / "fallback.jsonl"
        sink = _catching_up(client, now=now, fallback_path=fallback_path)
        sink.emit("claude", _event("before"))
        sink.flush()
        client.down = True

        sink.emit_run_record(
            Event(record=SystemMessage(content="asks"), path=_RUN_PART),
        )
        local = fallback_path.read_text()
        client.down = False
        now[0] = 60.0
        sink.flush()

        assert _RUN_PART.name in local
        assert _RUN_PART.name in [name for _, name, _, _ in client.appended]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
