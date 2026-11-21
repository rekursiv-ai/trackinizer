"""Tests for the provider-neutral session JSON format."""

from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Final

import json

from treekle import ReadError, from_plain, json_loads

import pytest

from trackinizer.sagent.scripts.convert_session import claude, codex, normalized
from trackinizer.sagent.session.types import (
    AgentMessage,
    AssistantMessage,
    Attachment,
    BashResult,
    ContextClear,
    ContextState,
    IncompleteRecord,
    ReadResult,
    SessionContext,
    SessionRecord,
    Thinking,
    TokenUsage,
    ToolCall,
    ToolMessage,
    UncategorizedRecord,
    UncategorizedToolResult,
    UserMessage,
    stored_plain,
)


if TYPE_CHECKING:
    from trackinizer.sagent.scripts.convert_session.convert import _Adapter


_CWD: Final[Path] = Path(__file__).resolve().parent


def test_session_json_round_trips_every_record_type() -> None:
    records: list[SessionRecord] = [
        # Everything the deleted metadata used to hold rides here: the file's
        # encoding on ``encoding``, the provider's launch payload on ``extra``.
        SessionContext(
            context_id=0,
            timestamp=datetime(2026, 8, 22, tzinfo=UTC),
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
            # As a reader keeps a time its line spelled another way.
            timestamp=datetime(2026, 8, 22, tzinfo=UTC),
            extra={"uuid": "u1", "$timestamp_as_written": "2026-08-22T00:00:00.000Z"},
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
        ToolMessage(
            call_id="c1",
            state="done",
            stdout=("body",),
            result=ReadResult(path="/a"),
        ),
        ToolMessage(
            call_id="c2",
            state="done",
            result=BashResult(command=("ls",), exit_code=0),
        ),
        ToolMessage(
            call_id="c3",
            state="done",
            stdout=("ok", Attachment(mime_descriptor="image/png", data=b"png")),
            result=UncategorizedToolResult(),
        ),
        AgentMessage(content="Look.", sender="/root", recipient="/kid"),
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

    assert rebuilt == records
    context, message = json_loads(stream.getvalue(), list[dict[str, object]])[:2]
    assert (context["timestamp"], message["timestamp"], message["extra"]) == (
        "2026-08-22T00:00:00+00:00",
        "2026-08-22T00:00:00.000Z",
        {"uuid": "u1"},
    )


def test_session_json_keeps_a_nested_residual_verbatim() -> None:
    # ``extra`` is where every key the record types do not name survives, so
    # the JSON codec has to carry an arbitrary nesting depth through it.
    nested = {"a": [{"b": {"c": [1, 2.5, True, None, "x"]}}]}
    stream = StringIO()

    written = SessionContext(extra=nested)
    normalized.denormalize([written], stream)
    stream.seek(0)

    context = next(iter(normalized.normalize(stream)))
    assert isinstance(context, SessionContext)
    assert context == written
    assert json.loads(stream.getvalue())[0]["extra"] == nested


def test_session_json_is_a_bare_array_of_tagged_records() -> None:
    # A session IS its records, so the document is the ARRAY and nothing else:
    # an object with the records under a key would put a container back around
    # them, which is the materialization axiom 11 forbids.
    stream = StringIO()

    normalized.denormalize([SessionContext(), UserMessage(content="Hi.")], stream)

    text = stream.getvalue()
    assert text.endswith("\n")
    document = json_loads(text, list[dict[str, object]])
    assert [
        from_plain(entry.get("py/object"), str, default="").rpartition(".")[2]
        for entry in document
    ] == ["SessionContext", "UserMessage"]


def test_session_json_is_compact_and_keeps_its_text_as_written() -> None:
    """No space after a separator, and no escape for a character JSON need not."""
    stream = StringIO()

    normalized.denormalize([UserMessage(content="é")], stream)

    assert (
        stream.getvalue() == '[{"py/object":"UserMessage","content":"é","extra":{}}]\n'
    )


def test_a_document_that_is_no_array_is_refused() -> None:
    with pytest.raises(ReadError):
        _ = list(normalized.normalize(StringIO("{}")))


def test_only_a_class_tag_is_renamed_from_its_import_path() -> None:
    """A string that merely spells an import path is the provider's, kept as it is."""
    module = UserMessage.__module__
    record = UserMessage(content="u", extra={"note": f"{module}.UserMessage"})
    old = json.dumps([stored_plain(record)]).replace(
        '"py/object": "',
        f'"py/object": "{module}.',
    )

    assert list(normalized.normalize(StringIO(old))) == [record]


def test_each_entry_is_a_record_as_every_store_writes_it() -> None:
    """One encoding for a journal line, a row, and a document entry."""
    records: list[SessionRecord] = [
        ContextClear(history=(UserMessage(content="u"), AssistantMessage(content="a"))),
    ]
    stream = StringIO()

    normalized.denormalize(records, stream)

    assert json.loads(stream.getvalue()) == [stored_plain(r) for r in records]


def test_a_document_naming_classes_by_import_path_still_reads() -> None:
    """An archive written before tags were names reads back, nested members too."""
    record = ContextClear(
        history=(UserMessage(content="u"), AssistantMessage(content="a")),
    )
    module = ContextClear.__module__
    old = json.dumps([stored_plain(record)]).replace(
        '"py/object": "',
        f'"py/object": "{module}.',
    )

    assert list(normalized.normalize(StringIO(old))) == [record]


def test_a_row_tagged_with_a_former_result_class_import_path_still_reads() -> None:
    old = json.dumps(
        [
            {
                "py/object": f"{ToolMessage.__module__}.UncategorizedToolResult",
                "call_id": "c",
                "content": "ok",
            },
        ],
    )

    assert list(normalized.normalize(StringIO(old))) == [
        ToolMessage(
            call_id="c",
            state="done",
            stdout=("ok",),
            result=UncategorizedToolResult(),
        ),
    ]


@pytest.mark.parametrize(
    ("adapter", "fixture"),
    [
        pytest.param(
            claude,
            "claude_main.jsonl",
            marks=pytest.mark.compute_large_fixture,
        ),
        (claude, "claude_sidechain.jsonl"),
        pytest.param(
            codex,
            "codex_main.jsonl",
            marks=pytest.mark.compute_large_fixture,
        ),
    ],
    ids=["claude-main", "claude-sidechain", "codex-main"],
)
def test_a_captured_session_survives_the_json_round_trip(
    adapter: _Adapter,
    fixture: str,
) -> None:
    # The conversion path the CLI exposes, over bytes a real CLI wrote:
    # native -> records -> JSON -> records -> native.
    native = (_CWD / "testdata" / fixture).read_text(encoding="utf-8")
    as_json = StringIO()
    normalized.denormalize(adapter.normalize(StringIO(native)), as_json)
    as_json.seek(0)

    rebuilt = StringIO()
    adapter.denormalize(normalized.normalize(as_json), rebuilt)

    assert rebuilt.getvalue() == native


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
