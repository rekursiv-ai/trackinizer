"""Gemini normalization: one rewritten document, read whole.

Gemini writes ONE JSON object and rewrites it whole on every turn, so its
adapter is unlike the append-only pair: there is no line to follow, and the
same bytes arrive again with one more message appended. The properties that
matter are therefore about the round trip, since the document is what a resume
would replay, and about a re-read yielding the same records the last one did.
"""

from __future__ import annotations

from io import StringIO

import json

from trackinizer.lib.agent.sessions import gemini
from trackinizer.lib.agent.sessions.convert import _dropped, detect_format
from trackinizer.lib.agent.types.sessions import (
    AssistantMessage,
    ContextClear,
    IncompleteRecord,
    SessionRecord,
    Thinking,
    ToolCall,
    TurnContext,
    UncategorizedRecord,
    UserMessage,
)


def _content(record: SessionRecord) -> str:
    """The prose of a user turn, narrowed so the union stays honest."""
    assert isinstance(record, UserMessage)
    return record.content or ""


def _document(*messages: dict[str, object], session_id: str = "s1") -> str:
    """A gemini session document holding ``messages``."""
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
            }
        )
    )

    assert [type(r) for r in records] == [
        TurnContext,
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

    assert [type(r) for r in records] == [TurnContext, ContextClear, IncompleteRecord]


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
        {"type": "user", "content": "one"}, {"type": "user", "content": "two"}
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

    messages = json.loads(out.getvalue())["messages"]
    assert [message["type"] for message in messages] == ["user", "gemini"]
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

    assert json.loads(out.getvalue())["messages"] == [
        {
            "type": "gemini",
            "content": "",
            "toolCalls": [{"id": "t1", "name": "read_file", "args": {}}],
        }
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

    assert [m["content"] for m in json.loads(out.getvalue())["messages"]] == [
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
    assert json.loads(out.getvalue())["sessionId"]


def test_a_foreign_encoding_key_is_not_written_as_a_document_field() -> None:
    """Another adapter's settings are not gemini's own document fields.

    Claude states ``ascii_escaped`` and its escape bitmap on the settings it
    opens with; writing those through put three keys on the wire gemini never
    authored, which is also what made the sniffer fail.
    """
    records = [
        TurnContext(encoding={"ascii_escaped": True, "newline_terminated": True}),
        UserMessage(content="hi"),
    ]

    out = StringIO()
    gemini.denormalize(records, out)

    assert set(json.loads(out.getvalue())) == {"sessionId", "messages"}


def test_a_timestamp_survives_the_gemini_round_trip() -> None:
    """When a record states a time, converting must not silently discard it.

    Gemini's own documents carry no per-message stamp, so the writer wrote
    none -- and a claude or codex session, where every record states one, lost
    every timestamp crossing in. Kept in the message's residual, which is where
    the reader already restores keys the format does not model.
    """
    records = [UserMessage(content="hi", timestamp="2026-01-01T00:00:00Z")]

    out = StringIO()
    gemini.denormalize(records, out)

    back = _read(out.getvalue())
    assert [r.timestamp for r in back if isinstance(r, UserMessage)] == [
        "2026-01-01T00:00:00Z"
    ]


def test_a_stamped_session_converts_to_gemini_losslessly() -> None:
    """The loss above is what ``convert`` measures, so it must report clean."""
    records = [
        UserMessage(content="hi", timestamp="2026-01-01T00:00:00Z"),
        AssistantMessage(content="yo", timestamp="2026-01-01T00:00:01Z"),
    ]

    out = StringIO()
    gemini.denormalize(records, out)

    assert _dropped(records, out.getvalue(), "gemini") == ()


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
    records = [UserMessage(content="hi"), Thinking(content="pondering")]

    out = StringIO()
    gemini.denormalize(records, out)

    assert len(json.loads(out.getvalue())["messages"]) == 1


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
