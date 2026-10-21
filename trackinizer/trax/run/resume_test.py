"""The resume tail: which targets are allowed, and how a lossy one is gated."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast, override
from uuid import UUID, uuid4

import json

from treekle import PlainTree, immutable

import pytest

from trackinizer.client.client import Client
from trackinizer.client.conversation import read_part
from trackinizer.lib.agent.sessions import (
    claude,
    codex,
    sagent,
)
from trackinizer.lib.agent.sessions.tail import Tail
from trackinizer.lib.agent.types.sessions import (
    AgentToAgentMessage,
    AssistantMessage,
    ContextClear,
    IncompleteRecord,
    ShellCommandResult,
    Thinking,
    ToolCall,
    UncategorizedRecord,
    UncategorizedToolResult,
    UserMessage,
)
from trackinizer.trax.run.custom_types import Event
from trackinizer.trax.run.errors import (
    CiphertextDroppedError,
    LossyConversionError,
    NotResumableError,
)
from trackinizer.trax.run.materialize import materialize
from trackinizer.trax.run.resume import (
    Resumed,
    _undroppable,
    prepare_resume,
)
from trackinizer.trax.run.sink import _record_body
from trackinizer.trax.run.truncate import (
    MAX_RECORD_BYTES,
    MAX_TOOL_RESULT_BYTES,
    NOTE,
)
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.refs import UuidRef
from trackinizer.wire.wire_session_ir import PartBody, RecordBody


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from trackinizer.lib.agent.types.sessions import SessionRecord
    from trackinizer.types.streams import TraxRecord


# Asked of the MODULE that owns it, not counted in parents from here: the
# export republishes this tree one directory shallower, so a fixed hop
# count resolved outside the package and the fixtures vanished.
_TESTDATA: Final = Path(claude.__file__).resolve().parent / "testdata"


# Fed a line at a time through :class:`Tail`, which is what capture does: the reader
# PULLS and the runner PUSHES, so driving the pull side directly would exercise a path
# the runner never takes.
def _records(name: str) -> tuple[list[TraxRecord], Mapping[str, PlainTree]]:
    """Return a corpus fixture's records and how its file spells its bytes."""
    reader = Tail((codex if name.startswith("codex") else claude).normalize)
    out: list[TraxRecord] = []
    with (_TESTDATA / name).open(encoding="utf-8") as handle:
        for line in handle:
            out.extend(reader.feed(line))
    out.extend(reader.close())
    return out, immutable(reader.encoding)


class _FakeClient:
    """Serves one stored session.

    It has no method that stamps the CLI's id onto the row, so a resume that tried to
    would fail here: that absence is what the attach tests rely on.
    """

    def __init__(self, name: str, *, session_format: str) -> None:
        records, self.encoding = _records(name)
        self.records: Sequence[TraxRecord] = records
        self._format = session_format

    def read_session_parts(self, session_id: UUID) -> list[PartBody]:
        del session_id
        return [
            PartBody(
                part=0,
                name="s.jsonl",
                format=self._format,
                records=len(self.records),
                metadata=self.encoding,
                ir_id=uuid4(),
            ),
        ]

    def read_session_records(
        self,
        session_id: UUID,
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
            for idx, record in enumerate(self.records)
        ]


class _PartsClient:
    """Serves a session of several parts, each one a file of its own format."""

    def __init__(
        self,
        parts: Mapping[int, tuple[str, Sequence[SessionRecord]]],
    ) -> None:
        self._parts = parts

    def read_session_parts(self, session_id: UUID) -> list[PartBody]:
        del session_id
        return [
            PartBody(
                part=number,
                name=f"{number}.jsonl",
                format=fmt,
                records=len(records),
            )
            for number, (fmt, records) in self._parts.items()
        ]

    def read_session_records(
        self,
        session_id: UUID,
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
            for idx, record in enumerate(self._parts[part][1])
        ]

    def get_inquiry(
        self,
        ref: object,
    ) -> tuple[str, UUID, dict[str, PlainTree]]:
        """Return the session's detail view, which names its seq."""
        assert isinstance(ref, UuidRef)
        return "AgentSession", ref.uuid, {"self": {"seq": 7}}


@pytest.fixture(autouse=True)
def local_session_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Materialize into temp roots, never the operator's own.

    BOTH CLIs: each adapter derives its own directory, so isolating one leaves
    the other writing real rollouts into ``~/.codex`` -- which the operator's
    codex then offers in its resume picker, and which makes the suite's result
    depend on what is already on the machine.
    """
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(
        "trackinizer.trax.run.materialize._codex_cli_version",
        lambda: "0.999.0",
    )


class TestWhichTargetsResume:
    """Only a CLI that names a session by a stable id can be re-entered."""

    @pytest.mark.parametrize("target", ["gemini", "sh"])
    def test_a_non_resumable_target_is_refused_by_name(self, target: str) -> None:
        """Refused with the target named, not a generic failure.

        Each still DOWNLOADS in its own format; what it lacks is a way to be
        handed back to a running process -- gemini rewrites one document in
        place, and a scrape has no native log at all.
        """
        client = _FakeClient("claude_sidechain.jsonl", session_format="claude")

        with pytest.raises(NotResumableError, match=target):
            prepare_resume(cast(Client, client), uuid4(), target)

    def test_claude_resumes(self) -> None:
        client = _FakeClient("claude_sidechain.jsonl", session_format="claude")

        written = prepare_resume(cast(Client, client), uuid4(), "claude")

        assert written.path.exists()

    @pytest.mark.compute_large_fixture
    def test_codex_resumes(self) -> None:
        """``codex resume <uuid>`` re-enters a rollout, so codex is a target."""
        client = _FakeClient("codex_main.jsonl", session_format="codex")

        written = prepare_resume(cast(Client, client), uuid4(), "codex")

        assert written.path.exists()
        assert written.path.name.startswith("rollout-")

    def test_a_claude_capture_resumes_as_codex(self) -> None:
        """The crossing that was previously impossible in this direction.

        The target is chosen at RESUME time, not by what captured the session,
        so both directions have to work -- ``--lossy`` because a claude
        transcript holds acts a rollout cannot state.
        """
        client = _FakeClient("claude_sidechain.jsonl", session_format="claude")

        written = prepare_resume(cast(Client, client), uuid4(), "codex", lossy=True)

        assert written.path.exists()

    def test_a_session_with_no_native_part_is_refused(self) -> None:
        """An ``sh`` scrape is searchable and has no format to hand back."""
        client = _FakeClient("claude_sidechain.jsonl", session_format="")

        with pytest.raises(NotResumableError, match="no part with a native format"):
            prepare_resume(cast(Client, client), uuid4(), "claude")


class TestTheChatKeepsItsSession:
    """The run re-enters the session it resumed, and uploads only what follows the file."""

    def test_the_run_attaches_to_the_session_it_resumed(self) -> None:
        """The chat keeps its own id, so the run must open that same AgentSession."""
        client = _FakeClient("claude_sidechain.jsonl", session_format="claude")
        session_id = uuid4()

        written = prepare_resume(cast(Client, client), session_id, "claude")

        assert written.attach == session_id

    def test_the_prefix_is_the_file_as_it_was_written(self) -> None:
        """Everything before this many bytes is history, and is not uploaded again."""
        client = _FakeClient("claude_sidechain.jsonl", session_format="claude")

        written = prepare_resume(cast(Client, client), uuid4(), "claude")

        assert written.prefix_bytes == written.path.stat().st_size

    def test_the_minted_id_names_the_written_file(self) -> None:
        """``--resume <id>`` and the file on disk have to agree."""
        client = _FakeClient("claude_sidechain.jsonl", session_format="claude")

        written = prepare_resume(cast(Client, client), uuid4(), "claude")

        assert written.path.stem == str(written.cli_session_id)


class TestAMixedSessionResumes:
    """Every part of a conversation is loaded, whichever CLI wrote it."""

    @pytest.mark.parametrize("target", ["claude", "codex"])
    def test_a_bot_session_of_two_parts_gives_every_persons_line(
        self,
        target: str,
    ) -> None:
        """Four people's lines, two from each part, read back as ``sender: text``."""
        session_id = uuid4()
        client = _PartsClient(
            {
                0: (
                    "sagent",
                    [
                        AgentToAgentMessage(sender="ada@example.com", content="q1"),
                        AgentToAgentMessage(sender="bob@example.com", content="q2"),
                        AssistantMessage(content="a1"),
                    ],
                ),
                1: (
                    "sagent",
                    [
                        AgentToAgentMessage(sender="carol@example.com", content="q3"),
                        AgentToAgentMessage(sender="dave@example.com", content="q4"),
                        AssistantMessage(content="a2"),
                    ],
                ),
            },
        )

        written = prepare_resume(cast(Client, client), session_id, target)

        reader = claude.normalize if target == "claude" else codex.normalize
        with written.path.open(encoding="utf-8") as handle:
            users = [
                record.content
                for record in reader(handle)
                if isinstance(record, UserMessage)
            ]
        # Turns that follow one another are one turn, a line apart. The trailing switch
        # note is the CLI's own, not a line anyone wrote.
        people = [
            line
            for text in users
            for line in (text or "").split("\n")
            if not line.startswith("[trax]")
        ]
        assert people == [
            "ada@example.com: q1",
            "bob@example.com: q2",
            "carol@example.com: q3",
            "dave@example.com: q4",
        ]
        assert written.attach == session_id

    def test_a_session_of_four_parts_resumes_as_claude(self) -> None:
        """Bot, claude, bot and codex parts all reach the file."""
        client = _PartsClient(
            {
                0: (
                    "sagent",
                    [AgentToAgentMessage(sender="ada@example.com", content="q1")],
                ),
                1: (
                    "claude",
                    [
                        UserMessage(content="typed at the terminal"),
                        AssistantMessage(content="claude says"),
                    ],
                ),
                2: (
                    "sagent",
                    [
                        AgentToAgentMessage(sender="bob@example.com", content="q2"),
                        AssistantMessage(content="bot says"),
                    ],
                ),
                3: ("codex", [AssistantMessage(content="codex says")]),
            },
        )

        written = prepare_resume(cast(Client, client), uuid4(), "claude")

        text = written.path.read_text(encoding="utf-8")
        assert "ada@example.com: q1" in text
        assert "the terminal user: typed at the terminal" in text
        assert "bob@example.com: q2" in text
        assert "claude says" in text
        assert "bot says" in text
        assert "codex says" in text

    def test_a_provider_that_calls_a_tool_before_it_answers_resumes(self) -> None:
        """The switch note is not written behind claude's own tool result, and kept.

        Claude's writer folds a user note that follows a tool result into the result's
        line and drops its text, so a note placed there was a refused resume.
        """
        client = _PartsClient(
            {
                0: (
                    "sagent",
                    [
                        AgentToAgentMessage(sender="ada@example.com", content="q1"),
                        AssistantMessage(content="bot says"),
                    ],
                ),
                1: (
                    "claude",
                    [
                        UserMessage(content="look it up"),
                        ToolCall(call_id="t1", name="Grep"),
                        UncategorizedToolResult(call_id="t1", content="nothing"),
                        AssistantMessage(content="claude says"),
                    ],
                ),
            },
        )

        written = prepare_resume(cast(Client, client), uuid4(), "claude")

        assert "answered by bot. From here claude answers" in written.path.read_text(
            encoding="utf-8",
        )

    def test_a_person_line_after_a_tool_result_resumes_as_claude(self) -> None:
        """The line is kept as its own line, not folded into the result it follows.

        A person's line typed after claude's own tool result was refused as dropped, and
        without the flag the file would have lost the line.
        """
        client = _PartsClient(
            {
                0: (
                    "sagent",
                    [
                        AgentToAgentMessage(sender="ada@example.com", content="q1"),
                        AssistantMessage(content="bot says"),
                    ],
                ),
                1: (
                    "claude",
                    [
                        UserMessage(content="look it up"),
                        ToolCall(call_id="t1", name="Grep"),
                        UncategorizedToolResult(call_id="t1", content="nothing"),
                        UserMessage(content="stop, wrong file"),
                        AssistantMessage(content="ok"),
                    ],
                ),
            },
        )

        written = prepare_resume(cast(Client, client), uuid4(), "claude")

        assert "the terminal user: stop, wrong file" in written.path.read_text(
            encoding="utf-8",
        )

    def test_a_claude_result_with_no_call_in_the_session_resumes(self) -> None:
        """A claude result whose call is not in the session is stated as text, and resumes.

        Claude's writer drops a bare result, so without the text the resume was refused
        as losing an act. The real capture this is measured on carries such a result.
        """
        client = _PartsClient(
            {
                0: (
                    "sagent",
                    [
                        AgentToAgentMessage(sender="ada@example.com", content="q1"),
                        AssistantMessage(content="bot says"),
                    ],
                ),
                1: (
                    "claude",
                    [
                        UserMessage(content="look it up"),
                        UncategorizedToolResult(call_id="orphan", content="found it"),
                        AssistantMessage(content="claude says"),
                    ],
                ),
            },
        )

        written = prepare_resume(cast(Client, client), uuid4(), "claude")

        assert "[a tool returned: found it]" in written.path.read_text(encoding="utf-8")

    def test_a_real_capture_with_a_result_it_never_calls_resumes_as_claude(
        self,
    ) -> None:
        """The captured claude transcript carries a result with no call in it.

        Behind a bot part it resumed as a refused conversion, which dropped the result.
        """
        records, _ = _records("claude_main.jsonl")
        client = _PartsClient(
            {
                0: (
                    "sagent",
                    [
                        AgentToAgentMessage(sender="ada@example.com", content="q1"),
                        AssistantMessage(content="bot says"),
                    ],
                ),
                1: ("claude", cast("Sequence[SessionRecord]", records)),
            },
        )

        written = prepare_resume(cast(Client, client), uuid4(), "claude")

        assert written.path.exists()

    def test_a_truncated_line_of_the_same_cli_requires_lossy(self) -> None:
        """A single codex part the target already speaks is replayed, so it must parse."""
        client = _FakeClient("claude_sidechain.jsonl", session_format="codex")
        client.records = [
            UserMessage(content="kept"),
            IncompleteRecord(text="{}{}"),
        ]

        with pytest.raises(LossyConversionError, match="IncompleteRecord"):
            prepare_resume(cast(Client, client), uuid4(), "codex")

    def test_a_truncated_line_of_another_cli_is_not_a_line_of_the_chat(self) -> None:
        """Only a conversation line crosses; a torn line of the other CLI is not one."""
        client = _FakeClient("claude_sidechain.jsonl", session_format="claude")
        client.records = [
            UserMessage(content="kept"),
            IncompleteRecord(text='{"x":1'),
        ]

        written = prepare_resume(cast(Client, client), uuid4(), "codex")

        assert '{"x":1' not in written.path.read_text(encoding="utf-8")


class TestLossyConversion:
    """A cross-format resume that DROPS records needs the flag."""

    def test_missing_seal_writes_nothing(self, tmp_path: Path) -> None:
        client = _FakeClient("claude_sidechain.jsonl", session_format="claude")
        client.records = [Thinking(encrypted="")]
        with pytest.raises(CiphertextDroppedError, match="no longer stored"):
            prepare_resume(cast(Client, client), uuid4(), "claude")
        assert not list(tmp_path.rglob("*.jsonl"))

    def test_failed_file_write_raises(self, tmp_path: Path) -> None:
        client = _FakeClient("claude_sidechain.jsonl", session_format="claude")
        (tmp_path / "claude").write_text("not a directory")
        with pytest.raises(NotADirectoryError):
            prepare_resume(cast(Client, client), uuid4(), "claude")

    def test_a_claude_session_is_never_lossy(self) -> None:
        """Same format in and out: nothing to drop, no flag needed."""
        client = _FakeClient("claude_sidechain.jsonl", session_format="claude")

        assert prepare_resume(cast(Client, client), uuid4(), "claude").path.exists()

    @pytest.mark.compute_large_fixture
    def test_a_codex_session_resumes_as_claude_without_the_flag(self) -> None:
        """A rollout's own events are not conversation, so they cross without a flag.

        The conversation the model answers from is every person's line and every
        answer, and both cross. Codex's launch and event records are its own
        bookkeeping, which claude restates for itself, so refusing on them made
        every real rollout need ``--lossy`` for a transcript that lost no turn.
        """
        client = _FakeClient("codex_main.jsonl", session_format="codex")

        written = prepare_resume(cast(Client, client), uuid4(), "claude")

        assert written.path.exists()

    @pytest.mark.compute_large_fixture
    def test_the_flag_accepts_the_loss(self) -> None:
        client = _FakeClient("codex_main.jsonl", session_format="codex")

        written = prepare_resume(cast(Client, client), uuid4(), "claude", lossy=True)

        assert written.path.exists()

    def test_the_gate_measures_the_file_that_gets_written(self) -> None:
        """The loss report has to describe the FILE, not a dry run beside it.

        Materialization stamps identity before writing -- codex needs a
        ``session_meta`` line, synthesized when the source carried none -- and
        that line is what a ``ContextClear`` rides out on. Measuring the
        unstamped records reported a ``ContextClear`` drop that the written
        file does not have, so the gate refused a resume over a record it was
        about to preserve.
        """
        client = _FakeClient("claude_sidechain.jsonl", session_format="claude")
        records, _ = read_part(cast(Client, client), uuid4(), 0, plaintext_only=False)

        written = materialize(
            attach=uuid4(),
            target="codex",
            records=records,
            encoding=client.encoding,
            source="claude",
        )

        rebuilt = Counter(
            type(record).__name__
            for record in codex.normalize(
                StringIO(written.path.read_text(encoding="utf-8")),
            )
        )
        dropped = _undroppable(records, "claude", "codex")

        assert "ContextClear" not in dropped, (
            "the gate names a loss the written file does not have"
        )
        assert rebuilt["ContextClear"] == 1

    def test_derived_state_is_not_counted_as_loss(self) -> None:
        """A ``TurnContext`` is DERIVED, so a differing count is not a drop.

        Each adapter states settings in its own shape -- claude repeats an
        envelope per line, codex declares once per turn -- so the counts
        legitimately differ across a crossing while the conversation is
        untouched. Counting them made every claude-to-codex resume look lossy
        and demand ``--lossy`` for a transcript that loses no turn at all.
        """
        client = _FakeClient("claude_sidechain.jsonl", session_format="claude")
        records, _ = read_part(cast(Client, client), uuid4(), 0, plaintext_only=False)

        dropped = _undroppable(records, "claude", "codex")

        assert "TurnContext" not in dropped

    def test_a_lost_act_is_still_reported(self) -> None:
        """The gate must still bite on an act the target cannot state.

        Loosening it to ignore derived state is only correct if a real loss
        still trips it, so this pins the half that must not move. An
        ``UncategorizedRecord`` is a line claude wrote and codex has no shape
        for at all -- measured, it writes nothing and reads back as nothing.
        """
        records = [
            UserMessage(content="kept"),
            UncategorizedRecord(kind="queue-operation"),
        ]

        dropped = _undroppable(records, "claude", "codex")

        assert "UncategorizedRecord" in dropped
        assert "UserMessage" not in dropped

    def test_a_refused_resume_writes_nothing(self, tmp_path: Path) -> None:
        """The gate runs BEFORE anything is written, so a refusal leaves no file.

        A file left behind would be the next run's transcript to attach to. A
        capture cut at upload is refused on the merged path too, so it stands in for
        the refusal.
        """
        client = _CapturedClient()

        with pytest.raises(LossyConversionError, match="truncated at capture"):
            prepare_resume(cast(Client, client), uuid4(), "codex")

        assert not list((tmp_path / "codex").rglob("*.jsonl"))

        assert not list((tmp_path / "claude").rglob("*.jsonl"))


_CLAUDE_ID = "44444444-4444-4444-4444-444444444444"
_SEALED = "gAAAAABpSealedSummary=="


def _stamp(second: int) -> str:
    return f"2026-10-20T10:{second // 60:02d}:{second % 60:02d}Z"


def _claude_part(*lines: Mapping[str, object]) -> list[SessionRecord]:
    """Read claude transcript lines, each with the envelope claude repeats."""
    envelope = {
        "parentUuid": None,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/w",
        "sessionId": _CLAUDE_ID,
        "version": "2.1.304",
    }
    text = "".join(json.dumps(envelope | dict(line)) + "\n" for line in lines)
    return list(claude.normalize(StringIO(text)))


def _claude_said(
    role: str,
    text: str,
    *,
    at: int,
    **flags: object,
) -> dict[str, object]:
    content: object = text if role == "user" else [{"type": "text", "text": text}]
    said: dict[str, object] = {
        "type": role,
        "message": {"role": role, "content": content},
        "uuid": uuid4().hex,
        "timestamp": _stamp(at),
    }
    return said | flags


def _claude_compaction(summary: str, *, at: int) -> list[dict[str, object]]:
    """Return the boundary and flagged summary claude 2.1.304 writes."""
    return [
        {
            "type": "system",
            "subtype": "compact_boundary",
            "content": "Conversation compacted",
            "logicalParentUuid": "before",
            "compactMetadata": {"trigger": "manual", "preTokens": 967_000},
            "uuid": uuid4().hex,
            "timestamp": _stamp(at),
        },
        _claude_said(
            "user",
            summary,
            at=at,
            isCompactSummary=True,
            isVisibleInTranscriptOnly=True,
        ),
    ]


def _codex_part(*rows: Mapping[str, object]) -> list[SessionRecord]:
    """Read a codex rollout: its launch line, then ``rows``."""
    launch = {
        "timestamp": _stamp(0),
        "type": "session_meta",
        "payload": {"id": "s1", "session_id": "s1", "cwd": "/w"},
    }
    text = "".join(json.dumps(row) + "\n" for row in (launch, *rows))
    return list(codex.normalize(StringIO(text)))


def _codex_turn(role: str, text: str) -> dict[str, object]:
    kind = "output_text" if role == "assistant" else "input_text"
    return {"type": "message", "role": role, "content": [{"type": kind, "text": text}]}


def _codex_said(role: str, text: str, *, at: int) -> dict[str, object]:
    return {
        "timestamp": _stamp(at),
        "type": "response_item",
        "payload": _codex_turn(role, text),
    }


def _codex_compaction(*, at: int, message: str = "") -> dict[str, object]:
    """Return the measured compaction line: sealed unless ``message`` says otherwise."""
    return {
        "timestamp": _stamp(at),
        "type": "compacted",
        "payload": {
            "message": message,
            "replacement_history": [
                _codex_turn("user", "kept: which fruit?"),
                _codex_turn("developer", "<permissions instructions>"),
                {"type": "compaction", "encrypted_content": _SEALED},
            ],
        },
    }


def _bot_part(*rows: Mapping[str, object]) -> list[SessionRecord]:
    """Read the bot's chat log, written as its ``session_io`` writes it."""
    text = "".join(json.dumps(row) + "\n" for row in rows)
    return list(sagent.normalize(StringIO(text)))


def _bot_said(sender: str, text: str, *, at: int) -> dict[str, object]:
    return {
        "kind": "history",
        "ref": {"session_id": "agent", "ordinal": at},
        "type": "agent_send",
        "source": sender,
        "text": text,
        "timestamp": datetime.fromisoformat(_stamp(at)).timestamp(),
    }


def _bot_compaction(summary: str) -> dict[str, object]:
    return {
        "kind": "context_splice",
        "ref": {"session_id": "agent", "ordinal": 0},
        "mask": [],
        "insert_after": None,
        "payload": [{"type": "user", "text": summary}],
        "strategy": "summary",
    }


def _written(target: str, *parts: tuple[str, Sequence[SessionRecord]]) -> Resumed:
    client = _PartsClient(dict(enumerate(parts)))
    return prepare_resume(cast(Client, client), uuid4(), target)


def _user_turns(written: Resumed, target: str) -> list[str]:
    """Return the user turns the target reads back from its written file."""
    reader = claude.normalize if target == "claude" else codex.normalize
    with written.path.open(encoding="utf-8") as handle:
        return [
            record.content or ""
            for record in reader(handle)
            if isinstance(record, UserMessage)
        ]


def _claude_compacting() -> list[SessionRecord]:
    return _claude_part(
        _claude_said("user", "F1 is apples", at=1),
        _claude_said("assistant", "noted F1", at=2),
        *_claude_compaction("Summary: F1 is apples.", at=3),
        _claude_said("user", "F2 is pears", at=4),
        _claude_said("assistant", "noted F2", at=5),
    )


class TestTheWindowIsWhatEachSwitchLoads:
    """Each resume loads from the last compaction of any part, within its budget."""

    def test_claude_then_the_bot_then_codex_opens_with_claudes_summary(self) -> None:
        """At the switch to codex: claude's summary, then F2, then the bot's F3."""
        bot = _bot_part(_bot_said("ada@example.com", "F3 is plums", at=60))

        written = _written("codex", ("claude", _claude_compacting()), ("sagent", bot))

        turns = _user_turns(written, "codex")
        assert "Summary of the conversation before this (by claude):" in turns[0]
        assert "Summary: F1 is apples." in turns[0]
        rest = "\n".join(turns[1:])
        assert "F1 is apples" not in rest
        assert rest.index("F2 is pears") < rest.index("F3 is plums")
        assert written.skipped > 0
        assert not written.compact_after_load

    @pytest.mark.parametrize("target", ["claude", "codex"])
    def test_the_bots_compaction_opens_every_later_switch(self, target: str) -> None:
        """The bot compacts, then claude, then codex: both open with the bot's summary."""
        bot = _bot_part(
            _bot_said("ada@example.com", "F1 is apples", at=1),
            _bot_compaction("Summary: ada said F1 is apples."),
            _bot_said("ada@example.com", "F2 is pears", at=2),
        )
        later = _claude_part(
            _claude_said("user", "F3 is plums", at=60),
            _claude_said("assistant", "noted F3", at=61),
        )

        written = _written(target, ("sagent", bot), ("claude", later))

        turns = _user_turns(written, target)
        opening = next(turn for turn in turns if "Summary of the conversation" in turn)
        assert "(by bot):\nSummary: ada said F1 is apples." in opening
        everything = "\n".join(turns)
        assert "ada@example.com: F1 is apples" not in everything
        assert everything.index("F2 is pears") < everything.index("F3 is plums")

    def test_no_compaction_loads_every_line_in_time_order_with_the_switches(
        self,
    ) -> None:
        bot = _bot_part(
            _bot_said("ada@example.com", "F1 is apples", at=1),
            _bot_said("ada@example.com", "F3 is plums", at=60),
        )
        plain = _claude_part(
            _claude_said("user", "F2 is pears", at=4),
            _claude_said("assistant", "noted F2", at=5),
        )

        written = _written("codex", ("sagent", bot), ("claude", plain))

        everything = "\n".join(_user_turns(written, "codex"))
        assert (
            everything.index("F1 is apples")
            < everything.index("F2 is pears")
            < everything.index("F3 is plums")
        )
        assert "[trax] Provider switch" in everything
        assert written.skipped == 0

    def test_a_clear_starts_every_later_load_after_it(self) -> None:
        before = _claude_part(_claude_said("user", "F1 is apples", at=1))
        cleared = _claude_part(
            _claude_said(
                "user",
                "<command-name>/clear</command-name>\n<command-message>clear"
                "</command-message>\n<command-args></command-args>",
                at=5,
            ),
            _claude_said("user", "fresh start", at=6),
        )

        written = _written("codex", ("claude", before), ("claude", cleared))

        everything = "\n".join(_user_turns(written, "codex"))
        assert "F1 is apples" not in everything
        assert "cleared with /clear" in everything
        assert "fresh start" in everything


class TestASealedCodexSummaryInAFile:
    """Codex's ciphertext goes back to codex alone; another CLI gets the kept turns."""

    def _parts(self) -> tuple[str, Sequence[SessionRecord]]:
        return (
            "codex",
            _codex_part(
                _codex_said("user", "F1 is apples", at=1),
                _codex_said("assistant", "noted F1", at=2),
                _codex_compaction(at=3),
                _codex_said("user", "F4 is figs", at=4),
            ),
        )

    def test_codex_reads_back_one_compacted_line_and_nothing_before_it(self) -> None:
        bot = _bot_part(_bot_said("ada@example.com", "F5 is limes", at=60))

        written = _written("codex", self._parts(), ("sagent", bot))

        lines = written.path.read_text(encoding="utf-8").splitlines()
        compacted = [line for line in lines if '"type":"compacted"' in line]
        assert len(compacted) == 1
        assert _SEALED in compacted[0]
        assert "F1 is apples" not in written.path.read_text(encoding="utf-8")

    def test_claude_gets_the_kept_turn_and_never_the_ciphertext(self) -> None:
        bot = _bot_part(_bot_said("ada@example.com", "F5 is limes", at=60))

        written = _written("claude", self._parts(), ("sagent", bot))

        text = written.path.read_text(encoding="utf-8")
        assert "gAAAA" not in text
        assert "kept: which fruit?" in text
        assert "codex's summary of the earlier conversation is sealed" in text


def test_a_claude_file_opens_with_the_summary_claude_flags_then_an_answer() -> None:
    """Claude builds its context from such a line, as from its own compaction."""
    bot = _bot_part(_bot_said("ada@example.com", "F3 is plums", at=60))
    written = _written("claude", ("claude", _claude_compacting()), ("sagent", bot))

    with written.path.open(encoding="utf-8") as handle:
        acts = [
            record
            for record in claude.normalize(handle)
            if isinstance(record, UserMessage | AssistantMessage | ContextClear)
        ]

    summary = next(
        record
        for record in acts
        if isinstance(record, ContextClear) and record.summary is not None
    )
    flagged = acts[acts.index(summary) + 1]
    assert isinstance(flagged, UserMessage)
    assert flagged.extra["isCompactSummary"] is True
    assert flagged.extra["isVisibleInTranscriptOnly"] is True
    assert isinstance(acts[acts.index(summary) + 2], AssistantMessage)


def test_a_round_trip_keeps_the_summary_once_and_never_as_a_persons_line() -> None:
    """Written for claude, read back as claude's part, loaded again for codex."""
    bot = _bot_part(_bot_said("ada@example.com", "F3 is plums", at=60))
    first = _written("claude", ("claude", _claude_compacting()), ("sagent", bot))
    with first.path.open(encoding="utf-8") as handle:
        again = list(claude.normalize(handle))

    second = _written("codex", ("claude", again))

    turns = _user_turns(second, "codex")
    assert sum("Summary: F1 is apples." in turn for turn in turns) == 1
    assert not any(
        turn.startswith("the terminal user:") and "Summary: F1" in turn
        for turn in turns
    )


def test_a_cut_session_resumes_without_the_lossy_flag() -> None:
    """The window is what the file holds; the gate measures that, and drops nothing."""
    bot = _bot_part(_bot_said("ada@example.com", "F3 is plums", at=60))

    written = _written("claude", ("claude", _claude_compacting()), ("sagent", bot))

    assert written.loaded_records > 0
    assert written.estimated_tokens > 0


def test_a_single_part_of_the_targets_own_format_reports_its_window() -> None:
    """Replayed whole, as captured; its own CLI reads from its last compaction on."""
    client = _PartsClient({0: ("claude", _claude_compacting())})

    written = prepare_resume(cast(Client, client), uuid4(), "claude")

    assert written.skipped > 0
    assert "F1 is apples" in written.path.read_text(encoding="utf-8")
    assert not written.compact_after_load


class _CapturedClient(_FakeClient):
    """Serves each record as capture uploaded it: bounded, so a huge one arrives cut.

    Inflates the fixture's one shell result past the tool-result budget and one
    uncategorized line past every record's, the two places a cut leaves its note.
    """

    def __init__(self) -> None:
        super().__init__("claude_main.jsonl", session_format="claude")
        line = next(
            index
            for index, record in enumerate(self.records)
            if isinstance(record, UncategorizedRecord)
        )
        self.records = [
            _inflated(record)
            if index == line or isinstance(record, ShellCommandResult)
            else record
            for index, record in enumerate(self.records)
        ]

    @override
    def read_session_records(
        self,
        session_id: UUID,
        *,
        part: int = 0,
        **_: object,
    ) -> list[RecordBody]:
        del session_id, part
        return [
            _record_body(idx, Event(record=record, path=Path("s.jsonl")), redactor=None)
            for idx, record in enumerate(self.records)
        ]


class TestTruncatedAtCapture:
    """A record capture cut is a transcript the model never saw."""

    def test_resuming_it_is_refused_without_the_flag(self) -> None:
        client = _CapturedClient()

        with pytest.raises(LossyConversionError, match="truncated at capture"):
            prepare_resume(cast(Client, client), uuid4(), "claude")

    def test_the_flag_resumes_it_with_the_marker_and_without_the_note(self) -> None:
        """The note is Trackinizer's, never the CLI's; the marker is what the model reads."""
        client = _CapturedClient()

        written = prepare_resume(cast(Client, client), uuid4(), "claude", lossy=True)

        text = written.path.read_text(encoding="utf-8")
        assert text.count("Trackinizer capture truncated") >= 2
        assert NOTE not in text

    def test_a_session_nothing_was_cut_from_needs_no_flag(self) -> None:
        client = _CapturedClient()
        client.records = _records("claude_main.jsonl")[0]

        assert prepare_resume(cast(Client, client), uuid4(), "claude").path.exists()


def _inflated(record: TraxRecord) -> TraxRecord:
    """Return ``record`` grown past its budget, if it is one this suite cuts."""
    if isinstance(record, ShellCommandResult):
        return replace(record, stdout="x" * MAX_TOOL_RESULT_BYTES)
    if isinstance(record, UncategorizedRecord):
        return replace(
            record,
            payload=immutable({**record.payload, "content": "y" * MAX_RECORD_BYTES}),
        )
    return record


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
