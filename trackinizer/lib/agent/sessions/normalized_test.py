"""Tests for the provider-neutral session JSON format."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import json

import pytest

from trackinizer.lib.agent.sessions import claude, codex, normalized
from trackinizer.lib.agent.sessions.convert import _Adapter
from trackinizer.lib.agent.types.sessions import (
    AgentToAgentMessage,
    AssistantMessage,
    Attachment,
    ContextClear,
    ContextState,
    FileReadResult,
    IncompleteRecord,
    SessionRecord,
    ShellCommandResult,
    Thinking,
    TokenUsage,
    ToolCall,
    TurnContext,
    UncategorizedRecord,
    UncategorizedToolResult,
    UserMessage,
)
from trackinizer.lib.custom_json import ListCodec, StrCodec


_TESTDATA = Path(__file__).resolve().parent / "testdata"


def test_session_json_round_trips_every_record_type() -> None:
    records: list[SessionRecord] = [
        # Everything the deleted metadata used to hold rides here: the file's
        # encoding on ``encoding``, the provider's launch payload on ``extra``.
        TurnContext(
            context_id=0,
            timestamp="2026-08-22T00:00:00Z",
            effort="high",
            summary_kind="auto",
            permission="never",
            encoding={"newline_terminated": False},
            extra={
                "payload": {"session_id": "s1"},
                "sandbox_policy": {"type": "workspace-write"},
            },
        ),
        UserMessage(
            context_id=0,
            content="Hi.",
            timestamp="2026-08-22T00:00:00Z",
            extra={"uuid": "u1"},
        ),
        AssistantMessage(context_id=0, content="Here.", extra={"id": "msg1"}),
        Thinking(context_id=0, content="t", encrypted="sig"),
        ToolCall(
            context_id=0,
            call_id="c1",
            name="Read",
            arguments={"path": "/a"},
            extra={"$spaced": True},
        ),
        FileReadResult(call_id="c1", path="/a", content="body"),
        ShellCommandResult(call_id="c2", command=("ls",), exit_code=0),
        UncategorizedToolResult(
            call_id="c3",
            content="ok",
            attachments=(Attachment(mime_descriptor="image/png", data=b"png"),),
        ),
        AgentToAgentMessage(content="Look.", sender="/root", recipient="/kid"),
        TokenUsage(info={"total": 7}, rate_limits={"used": 1}),
        ContextState(kind="world_state", extra={"full": True}),
        ContextClear(cleared_session_id="s0"),
        UncategorizedRecord(kind="queue-operation", payload={"op": "enqueue"}),
        IncompleteRecord(text='{"trunc'),
    ]
    stream = StringIO()

    normalized.denormalize(records, stream)
    stream.seek(0)
    rebuilt = list(normalized.normalize(stream))

    first = rebuilt[1]
    assert rebuilt == records
    assert isinstance(first, UserMessage)
    assert first.timestamp == "2026-08-22T00:00:00Z"


def test_session_json_keeps_a_nested_residual_verbatim() -> None:
    # ``extra`` is where every key the record types do not name survives, so
    # the JSON codec has to carry an arbitrary nesting depth through it.
    nested = {"a": [{"b": {"c": [1, 2.5, True, None, "x"]}}]}
    stream = StringIO()

    normalized.denormalize([TurnContext(extra=nested)], stream)
    stream.seek(0)

    context = next(iter(normalized.normalize(stream)))
    assert isinstance(context, TurnContext)
    assert context.extra == nested


def test_session_json_is_a_bare_array_of_tagged_records() -> None:
    # A session IS its records, so the document is the ARRAY and nothing else:
    # an object with the records under a key would put a container back around
    # them, which is the materialization axiom 11 forbids.
    stream = StringIO()

    normalized.denormalize([TurnContext(), UserMessage(content="Hi.")], stream)

    text = stream.getvalue()
    assert text.endswith("\n")
    document = ListCodec.mappings(json.loads(text))
    assert [
        StrCodec.coerce(entry.get("py/object")).rpartition(".")[2] for entry in document
    ] == ["TurnContext", "UserMessage"]


@pytest.mark.parametrize(
    ("adapter", "fixture"),
    [
        (claude, "claude_main.jsonl"),
        (claude, "claude_sidechain.jsonl"),
        (codex, "codex_main.jsonl"),
    ],
    ids=["claude-main", "claude-sidechain", "codex-main"],
)
def test_a_captured_session_survives_the_json_round_trip(
    adapter: _Adapter, fixture: str
) -> None:
    # The conversion path the CLI exposes, over bytes a real CLI wrote:
    # native -> records -> JSON -> records -> native.
    native = (_TESTDATA / fixture).read_text(encoding="utf-8")
    as_json = StringIO()
    normalized.denormalize(adapter.normalize(StringIO(native)), as_json)
    as_json.seek(0)

    rebuilt = StringIO()
    adapter.denormalize(normalized.normalize(as_json), rebuilt)

    assert rebuilt.getvalue() == native


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
