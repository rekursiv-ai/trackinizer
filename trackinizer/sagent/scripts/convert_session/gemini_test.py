"""Gemini normalization: one rewritten document, read whole.

Gemini writes ONE JSON object and rewrites it whole on every turn, so its
adapter is unlike the append-only pair: there is no line to follow, and the
same bytes arrive again with one more message appended. The properties that
matter are therefore about the round trip, since the document is what a resume
would replay, and about a re-read yielding the same records the last one did.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import StringIO
from typing import TypedDict

import json

from treekle import json_loads

import pytest

from trackinizer.sagent.scripts.convert_session import gemini
from trackinizer.sagent.scripts.convert_session.convert import _dropped, detect_format
from trackinizer.sagent.scripts.convert_session.testdata.mistype import (
    mistyped,
    unread,
    without,
)
from trackinizer.sagent.session.types import (
    AssistantMessage,
    ContextClear,
    IncompleteRecord,
    SessionContext,
    SessionRecord,
    Thinking,
    ToolCall,
    UncategorizedRecord,
    UserMessage,
)


class _Document(TypedDict):
    sessionId: str
    messages: list[dict[str, object]]


def _content(record: SessionRecord) -> str:
    """Return the prose of a user turn, narrowed so the union stays honest."""
    assert isinstance(record, UserMessage)
    return record.content or ""


def _document(*messages: object, session_id: str = "s1") -> str:
    """Return a gemini session document holding ``messages``."""
    return json.dumps({"sessionId": session_id, "messages": list(messages)})


def _read(text: str) -> list[SessionRecord]:
    """Every record a document yields, drained from the iterator."""
    return list(gemini.normalize(StringIO(text)))


def test_a_user_turn_normalizes_to_a_user_message() -> None:
    records = _read(_document({"type": "user", "content": "hi"}))

    record = records[2]
    assert isinstance(record, UserMessage)
    assert record.content == "hi"


def test_a_gemini_turn_splits_prose_from_its_tool_calls() -> None:
    """Axiom 3: every act is its own record, never nested content."""
    records = _read(
        _document(
            {
                "type": "gemini",
                "content": "running",
                "toolCalls": [{"id": "t1", "name": "read_file", "args": {"p": "x"}}],
            },
        ),
    )

    assert [type(r) for r in records] == [
        SessionContext,
        ContextClear,
        AssistantMessage,
        ToolCall,
    ]


def test_an_unrecognized_type_is_preserved_not_dropped() -> None:
    """Axiom 10: a record with no neutral representation still replays."""
    records = _read(_document({"type": "future", "content": "x"}))

    record = records[2]
    assert isinstance(record, UncategorizedRecord)
    assert record.kind == "future"


def test_a_malformed_document_is_one_incomplete_record() -> None:
    """Unparseable bytes are kept verbatim rather than silently dropped."""
    records = _read("{not json}")

    assert [type(r) for r in records] == [
        SessionContext,
        ContextClear,
        IncompleteRecord,
    ]


def test_the_document_round_trips_byte_exact() -> None:
    """What was read is what is written back.

    The document IS the resume artifact, so a rewrite that reorders keys or
    drops an unknown field would hand the CLI a file it did not author.
    """
    text = _document(
        {"type": "user", "content": "hi"},
        {"type": "gemini", "content": "hello", "toolCalls": []},
    )

    out = StringIO()
    gemini.denormalize(gemini.normalize(StringIO(text)), out)

    assert out.getvalue() == text


def test_re_reading_a_rewritten_document_yields_what_it_now_holds() -> None:
    """Gemini rewrites in place, so a re-read is the whole document again.

    The reader states what the bytes say, not a delta: a watcher that saw the
    file change re-reads it and gets every turn, the new one included. Telling
    old from new is the CONSUMER's job -- trax stores a record by its position,
    so a re-read lands each one back where it already was.
    """
    first = _document({"type": "user", "content": "one"})
    second = _document(
        {"type": "user", "content": "one"},
        {"type": "user", "content": "two"},
    )

    before = [r for r in _read(first) if isinstance(r, UserMessage)]
    after = [r for r in _read(second) if isinstance(r, UserMessage)]

    assert [_content(r) for r in before] == ["one"]
    assert [_content(r) for r in after] == ["one", "two"]


def test_a_call_after_a_user_turn_gets_its_own_assistant_turn() -> None:
    """A call is the assistant's act, whatever record preceded it.

    Gemini nests calls under the turn that made them, and the writer folded
    each into whatever message came last -- so a call following a user turn
    was written as one the PERSON made. Codex reaches this on every rollout:
    it states a search as an end event with no call before it.
    """
    records = [UserMessage(content="do it"), ToolCall(call_id="t1", name="read_file")]

    out = StringIO()
    gemini.denormalize(records, out)

    messages = json_loads(out.getvalue(), _Document)["messages"]
    assert [message["type"] for message in messages] == [
        "user",
        "gemini",
    ]
    assert "toolCalls" not in messages[0]


def test_a_leading_tool_call_writes_a_turn_rather_than_crashing() -> None:
    """A crossed-in call may arrive with no assistant turn before it.

    Codex reports a search as an end event with no call of its own, and a
    fused session can open mid-conversation -- so the first record really can
    be a :class:`ToolCall`. The writer asserted otherwise and aborted the whole
    conversion; gemini nests calls under a turn, so the turn is synthesized.
    """
    out = StringIO()

    gemini.denormalize([ToolCall(call_id="t1", name="read_file")], out)

    assert json_loads(out.getvalue(), _Document)["messages"] == [
        {
            "type": "gemini",
            "content": "",
            "toolCalls": [{"id": "t1", "name": "read_file", "args": {}}],
        },
    ]


def test_one_incomplete_record_does_not_discard_the_session() -> None:
    """An unparsed line is one record, not a verdict on every other.

    Claude and codex both emit an :class:`IncompleteRecord` for a blank line,
    so a claude session converted to gemini routinely carries one -- and the
    writer treated its presence as "the document never parsed", emitting that
    one line and dropping every real turn.
    """
    records = [
        UserMessage(content="real turn"),
        AssistantMessage(content="answer"),
        IncompleteRecord(text="\n"),
    ]

    out = StringIO()
    gemini.denormalize(records, out)

    messages = json_loads(out.getvalue(), _Document)["messages"]
    assert [m["content"] for m in messages] == [
        "real turn",
        "answer",
    ]


def test_a_crossed_session_declares_an_id_it_can_be_recognized_by() -> None:
    """A converted session must still be recognizable as gemini.

    The format is sniffed by the ``sessionId``/``messages`` pair, and a stream
    from another provider declares neither -- so the written document was
    detected as no format at all and could not be read back by ``convert``.
    """
    out = StringIO()

    gemini.denormalize([UserMessage(content="hi")], out)

    assert detect_format(out.getvalue()) == "gemini"
    assert json_loads(out.getvalue(), _Document)["sessionId"]


def test_a_foreign_encoding_key_is_not_written_as_a_document_field() -> None:
    """Another adapter's settings are not gemini's own document fields.

    Claude states ``ascii_escaped`` and its escape bitmap on the settings it
    opens with; writing those through put three keys on the wire gemini never
    authored, which is also what made the sniffer fail.
    """
    records = [
        SessionContext(encoding={"ascii_escaped": True, "newline_terminated": True}),
        UserMessage(content="hi"),
    ]

    out = StringIO()
    gemini.denormalize(records, out)

    assert set(json_loads(out.getvalue(), dict[str, object])) == {
        "sessionId",
        "messages",
    }


def test_a_call_stating_nothing_reads_as_an_unnamed_call_with_no_arguments() -> None:
    records = _read(_document({"type": "gemini", "toolCalls": [dict[str, object]()]}))

    assert [r for r in records if isinstance(r, ToolCall)] == [
        ToolCall(call_id="", name="", arguments={}),
    ]


def test_an_edited_turn_writes_its_new_content_and_time() -> None:
    text = _document({"id": "m1", "timestamp": "t0", "type": "user", "content": "hi"})
    edited = [
        replace(r, content="bye", timestamp=datetime(2026, 1, 1, tzinfo=UTC))
        if isinstance(r, UserMessage)
        else r
        for r in _read(text)
    ]
    out = StringIO()

    gemini.denormalize(edited, out)

    assert json_loads(out.getvalue(), _Document)["messages"] == [
        {
            "id": "m1",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "type": "user",
            "content": "bye",
        },
    ]


def test_another_cli_s_bookkeeping_is_no_gemini_message() -> None:
    out = StringIO()

    gemini.denormalize(
        [
            SessionContext(encoding={"newline_terminated": True}),
            UncategorizedRecord(kind="queue-operation", payload={"type": "x"}),
            UserMessage(content="hi"),
        ],
        out,
    )

    assert json_loads(out.getvalue(), _Document)["messages"] == [
        {"type": "user", "content": "hi"},
    ]


def test_a_gemini_cli_message_reads_its_time_and_rewrites_exactly() -> None:
    """Gemini-cli writes ``{id, timestamp, type, content, ...}`` for every message.

    The reader took a time only from a ``$timestamp`` key, so every real stamp
    was lost, and the writer put ``type`` and ``content`` first.
    """
    text = _document(
        {
            "id": "m1",
            "timestamp": "2026-11-13T10:00:01Z",
            "type": "user",
            "content": "hi",
        },
        {
            "id": "m2",
            "timestamp": "2026-11-13T10:00:02Z",
            "type": "gemini",
            "content": "",
            "toolCalls": [{"id": "t1", "name": "ls", "args": dict[str, object]()}],
            "model": "gemini-3",
        },
    )

    records = _read(text)
    out = StringIO()
    gemini.denormalize(records, out)

    assert [r.timestamp for r in records if not isinstance(r, IncompleteRecord)][
        2:
    ] == [
        datetime(2026, 11, 13, 10, 0, 1, tzinfo=UTC),
        datetime(2026, 11, 13, 10, 0, 2, tzinfo=UTC),
        datetime(2026, 11, 13, 10, 0, 2, tzinfo=UTC),
    ]
    assert out.getvalue() == text


def test_a_timestamp_survives_the_gemini_round_trip() -> None:
    """When a record states a time, converting must not silently discard it."""
    records = [UserMessage(content="hi", timestamp=datetime(2026, 1, 1, tzinfo=UTC))]

    out = StringIO()
    gemini.denormalize(records, out)

    back = _read(out.getvalue())
    assert [r.timestamp for r in back if isinstance(r, UserMessage)] == [
        datetime(2026, 1, 1, tzinfo=UTC),
    ]


def test_a_stamped_session_converts_to_gemini_losslessly() -> None:
    """The loss above is what ``convert`` measures, so it must report clean."""
    records = [
        UserMessage(content="hi", timestamp=datetime(2026, 1, 1, tzinfo=UTC)),
        AssistantMessage(
            content="yo",
            timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=1),
        ),
    ]

    out = StringIO()
    gemini.denormalize(records, out)

    assert _dropped(records, out.getvalue(), "gemini") == {}


def test_a_gemini_document_without_stamps_gains_none() -> None:
    """A native document states no time, so the writer must not invent one."""
    native = '{"sessionId": "s1", "messages": [{"type": "user", "content": "hi"}]}'

    out = StringIO()
    gemini.denormalize(gemini.normalize(StringIO(native)), out)

    assert out.getvalue() == native


def test_a_record_gemini_cannot_express_is_dropped_not_written() -> None:
    """Only records with a gemini shape become messages.

    A ``Thinking`` record has none, so the conversion is lossy and reports so
    -- inventing a message for it would put a turn in the document the model
    never took.
    """
    records = [
        UserMessage(content="hi"),
        Thinking(content="pondering"),
        UserMessage(content="again"),
    ]

    out = StringIO()
    gemini.denormalize(records, out)

    messages = json_loads(out.getvalue(), _Document)["messages"]
    # Skipped, not the end: the message after it is still written.
    assert [message["content"] for message in messages] == ["hi", "again"]


def test_only_incomplete_records_are_written_verbatim() -> None:
    out = StringIO()

    gemini.denormalize([IncompleteRecord(text="raw")], out)

    assert out.getvalue() == "raw"


def test_a_tool_call_after_a_user_turn_gets_a_timestamped_gemini_turn() -> None:
    out = StringIO()

    # A call another CLI's line spelled the time of, then one that keeps none.
    called = ToolCall(
        call_id="t",
        name="run",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        extra={"$timestamp_as_written": "2026-01-01T00:00:00.000Z"},
    )
    gemini.denormalize(
        [
            UserMessage(content="go"),
            called,
            UserMessage(content="on"),
            ToolCall(
                call_id="u",
                name="run",
                timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        ],
        out,
    )

    messages = json_loads(out.getvalue(), _Document)["messages"]
    assert list(messages[1].items()) == [
        ("timestamp", "2026-01-01T00:00:00.000Z"),
        ("type", "gemini"),
        ("content", ""),
        ("toolCalls", [{"id": "t", "name": "run", "args": {}}]),
    ]
    assert messages[3]["timestamp"] == "2026-01-01T00:00:00+00:00"


def test_an_uncategorized_record_is_written_as_its_payload() -> None:
    out = StringIO()

    gemini.denormalize(
        [UncategorizedRecord(kind="future", payload={"type": "future", "x": 1})],
        out,
    )

    assert json_loads(out.getvalue(), _Document)["messages"] == [
        {"type": "future", "x": 1},
    ]


def test_nested_tool_arguments_round_trip() -> None:
    # Frozen mappings were copied one level deep, and ``json.dump`` cannot
    # encode the ``mappingproxy`` that remained inside.
    message = json_loads(
        '{"type": "gemini", "content": "", "meta": {"deep": {"k": 1}},'
        ' "toolCalls": [{"id": "t", "name": "f", "args": {"n": {"x": [1]}},'
        ' "x": {"y": {}}}]}',
        dict[str, object],
    )
    text = _document(message)

    out = StringIO()
    gemini.denormalize(gemini.normalize(StringIO(text)), out)

    assert out.getvalue() == text


def test_different_foreign_streams_of_one_length_get_different_ids() -> None:
    def session_id(content: str) -> str:
        out = StringIO()
        gemini.denormalize([UserMessage(content=content)], out)
        return json_loads(out.getvalue(), _Document)["sessionId"]

    assert session_id("a") != session_id("b")
    assert session_id("a") == session_id("a")


@pytest.mark.parametrize(
    "message",
    [
        {"type": 7, "content": "x"},
        {"type": "user", "content": [1]},
        {"type": "gemini", "content": {}},
        {"type": "gemini", "content": "", "toolCalls": "x"},
        {"type": "gemini", "content": "", "toolCalls": [7]},
        {"type": "gemini", "content": "", "toolCalls": [None]},
        {"type": "user", "content": "x", "timestamp": 7},
        7,
    ],
    ids=[
        "type",
        "user-content",
        "gemini-content",
        "calls",
        "call",
        "null-call",
        "timestamp",
        "scalar",
    ],
)
def test_a_malformed_message_round_trips(message: object) -> None:
    # One malformed field raised ``ReadError`` out of ``normalize`` and lost
    # the whole document.
    text = _document({"type": "user", "content": "before"}, message)

    out = StringIO()
    gemini.denormalize(gemini.normalize(StringIO(text)), out)

    assert out.getvalue() == text


def test_a_top_level_array_is_kept_verbatim() -> None:
    records = _read("[1, 2]")

    assert records[-1] == IncompleteRecord(text="[1, 2]")


def test_empty_text_and_empty_object_are_incomplete_documents() -> None:
    assert [type(record) for record in _read("   ")] == [SessionContext, ContextClear]
    records = _read("{}")
    assert [type(record) for record in records] == [
        SessionContext,
        ContextClear,
        IncompleteRecord,
    ]


@pytest.mark.parametrize(
    ("text", "after"),
    [("", []), ("{not json}", [IncompleteRecord(text="{not json}")])],
    ids=["empty", "malformed"],
)
def test_a_document_with_no_session_opens_a_window_and_keeps_its_bytes(
    text: str,
    after: list[SessionRecord],
) -> None:
    assert _read(text) == [
        SessionContext(encoding={"newline_terminated": True}),
        ContextClear(extra={"$opens": True}),
        *after,
    ]


def test_a_document_with_no_message_list_holds_no_messages() -> None:
    """A session yet to take a turn is a session, not unread bytes."""
    assert _read('{"sessionId":"s1"}') == [
        SessionContext(
            encoding={"newline_terminated": True},
            extra={"sessionId": "s1", "$compact": True},
        ),
        ContextClear(extra={"$opens": True}),
    ]


def test_every_field_of_a_document_reads_where_its_record_keeps_it() -> None:
    """Each modeled key is a field; every other key rides the record's residual."""
    text = json.dumps(
        {
            "sessionId": "s1",
            "projectHash": "p",
            "messages": [
                {"id": "u1", "timestamp": "t0", "type": "user", "content": "hi"},
                {"type": "user"},
                {
                    "type": "gemini",
                    "content": "run",
                    "timestamp": "t1",
                    "model": "m",
                    "toolCalls": [
                        {"id": "c1", "name": "f", "args": {"a": 1}, "status": "ok"},
                    ],
                },
                {"type": "gemini"},
                {"type": "gemini", "content": "", "toolCalls": []},
                {"content": "untyped"},
                7,
            ],
        },
    )

    records = _read(text)

    assert records[:2] == [
        SessionContext(
            encoding={"newline_terminated": True},
            extra={"sessionId": "s1", "projectHash": "p", "$compact": False},
        ),
        ContextClear(extra={"$opens": True}),
    ]
    turns = [r for r in records if isinstance(r, UserMessage | AssistantMessage)]
    # ``t0`` and ``t1`` name no time: each is kept as its message spelled it.
    assert [
        (type(r), r.timestamp, r.extra.get("$timestamp_as_written"), r.content)
        for r in turns
    ] == [
        (UserMessage, None, "t0", "hi"),
        (UserMessage, None, None, ""),
        (AssistantMessage, None, "t1", "run"),
        (AssistantMessage, None, None, ""),
        (AssistantMessage, None, None, ""),
    ]
    assert [r for r in records if isinstance(r, ToolCall | UncategorizedRecord)] == [
        ToolCall(
            call_id="c1",
            name="f",
            arguments={"a": 1},
            extra={"status": "ok", "$timestamp_as_written": "t1"},
        ),
        UncategorizedRecord(kind="", payload={"content": "untyped"}),
        UncategorizedRecord(kind="", payload={"$message": 7}),
    ]
    out = StringIO()
    gemini.denormalize(records, out)
    assert out.getvalue() == text


def test_a_compact_document_with_any_characters_round_trips_byte_exact() -> None:
    """Gemini writes some documents without spaces, and never escapes ``é``."""
    text = json.dumps(
        {
            "sessionId": "s1",
            "messages": [
                {"type": "user", "content": "é"},
                {"type": "gemini", "content": "x"},
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )

    out = StringIO()
    gemini.denormalize(gemini.normalize(StringIO(text)), out)

    assert out.getvalue() == text


def test_a_foreign_stream_writes_one_fixed_document() -> None:
    """Spaced, its id derived from what it holds, a call after a kept message its own turn.

    The id is pinned: a conversion re-run in a later release names the same session.
    """
    out = StringIO()

    gemini.denormalize(
        [
            UserMessage(),
            UncategorizedRecord(kind="future", payload={"type": "future"}),
            ToolCall(call_id="c", name="f"),
            UserMessage(content="é"),
        ],
        out,
    )

    assert out.getvalue() == (
        '{"sessionId": "cfcf8d74-bf02-54ce-88ab-21be042d028d", "messages":'
        ' [{"type": "user", "content": ""}, {"type": "future"},'
        ' {"type": "gemini", "content": "", "toolCalls":'
        ' [{"id": "c", "name": "f", "args": {}}]},'
        ' {"type": "user", "content": "é"}]}'
    )


def _nested() -> str:
    """Return a document whose turns nest objects, as the CLI's own do."""
    return json.dumps(
        {
            "sessionId": "s1",
            "projectHash": "p",
            "messages": [
                {"type": "user", "content": "list it", "id": "u1"},
                {
                    "type": "gemini",
                    "content": "done",
                    "thoughts": [{"subject": "plan", "description": "ls"}],
                    "tokens": {"input": 3, "output": 4, "total": 7},
                    "model": "gemini-3-pro",
                    "toolCalls": [
                        {
                            "id": "c1",
                            "name": "run_shell_command",
                            "args": {"command": "ls", "env": {"A": "1"}},
                            "result": [{"functionResponse": {"output": "a"}}],
                            "status": "success",
                        },
                    ],
                },
            ],
        },
    )


def test_a_document_with_nested_fields_round_trips_byte_exact() -> None:
    """A nested object is written back as the object it was read as."""
    text = _nested()

    out = StringIO()
    gemini.denormalize(gemini.normalize(StringIO(text)), out)

    assert out.getvalue() == text


def test_a_mistyped_field_aborts_neither_the_read_nor_the_write() -> None:
    """A document field of the wrong type reads as absent, as a missing one does."""
    document = json_loads(_nested(), dict[str, object])
    failed: list[str] = []
    missing_counts: dict[str, int] = {}
    for path, changed in mistyped(document):
        records = list(gemini.normalize(StringIO(json.dumps(changed))))
        try:
            gemini.denormalize(records, StringIO())
        except TypeError as error:
            failed.append(f"{path}: {error}")
        # Anything else in a message is kept verbatim with its message rather
        # than read as absent: reading it so would lose the value when the
        # document is written back.
        kept_whole = path.startswith("messages") and not path.endswith(".type")
        if not kept_whole:
            if path not in missing_counts:
                missing = json.dumps(without(document, path))
                missing_counts[path] = unread(gemini.normalize(StringIO(missing)))
            if unread(records) > missing_counts[path]:
                failed.append(
                    f"{path}: the document reads worse than without the field",
                )

    assert failed == [], "\n".join(failed)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
