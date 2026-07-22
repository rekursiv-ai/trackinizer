"""Tests for the AgentSessionEvent type and its typed message union."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast, get_args
from uuid import uuid4

import pytest

from trackinizer.types.agent_session_events import (
    AgentSendMessage,
    AgentSessionEvent,
    AssistantMessage,
    BytesAttachment,
    Compaction,
    FilePath,
    Kind,
    Message,
    SystemMessage,
    ToolCall,
    ToolResult,
    UnknownMessage,
    UserMessage,
    WebUrl,
    message_for_kind,
)
from trackinizer.types.columns import Row
from trackinizer.types.cost import TokenCount


_MESSAGES = [
    UserMessage(
        text="hi",
        attachments=(
            BytesAttachment(data=b"\x89PNG", descriptor="image/png"),
            FilePath(path=Path("/var/data/a")),
            WebUrl(url="http://x"),
        ),
    ),
    AgentSendMessage(text="go", source="agent7"),
    SystemMessage(text="<permissions>", role="developer"),
    AssistantMessage(
        text="ok",
        thinking="hmm",
        thinking_signature="sig",
        thinking_encrypted="ZZ",
        tool_calls=(
            ToolCall(id="t1", name="Read", args={"path": "x"}),
            ToolCall(id="t2", name="Bash", args={}),
        ),
        tokens=TokenCount(input_tokens=5, output_tokens=3),
    ),
    ToolResult(call_id="t1", content="out", is_error=True, diff="--- a"),
    Compaction(text="summary", token_before=100, token_after=20),
    UnknownMessage(raw={"weird": [1, 2, 3]}),
]


class TestMessageRoundTrip:
    @pytest.mark.parametrize("msg", _MESSAGES, ids=lambda m: type(m).__name__)
    def test_to_from_json_is_identity(self, msg: Message) -> None:
        assert type(msg).from_json(msg.to_json()) == msg

    def test_kind_matches_class_name(self) -> None:
        for msg in _MESSAGES:
            assert message_for_kind(type(msg).__name__) is type(msg)

    def test_message_for_kind_rejects_unknown(self) -> None:
        with pytest.raises(ValueError, match="unknown message kind"):
            message_for_kind("NotAMessage")


class TestAssistantMessage:
    def test_default_constructs_empty(self) -> None:
        a = AssistantMessage()
        assert a.text == ""
        assert a.tool_calls == ()
        assert a.tokens == TokenCount()

    def test_rejects_duplicate_tool_call_ids(self) -> None:
        with pytest.raises(ValueError, match="duplicate ToolCall id"):
            AssistantMessage(tool_calls=(ToolCall(id="x"), ToolCall(id="x")))


class TestAgentSessionEvent:
    def test_default_constructs(self) -> None:
        e = AgentSessionEvent()
        assert e.kind == "UserMessage"
        assert isinstance(e.message, UserMessage)

    def test_kind_must_match_message_type(self) -> None:
        with pytest.raises(ValueError, match="disagrees with message type"):
            AgentSessionEvent(kind="ToolResult", message=UserMessage())

    def test_from_row_decodes_typed_message(self) -> None:
        sid = uuid4()
        now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        msg = AssistantMessage(text="y", tool_calls=(ToolCall(id="a", name="Read"),))
        event = AgentSessionEvent(
            session_id=sid,
            seq=3,
            kind="AssistantMessage",
            model="gpt-5.5",
            timestamp=now,
            message=msg,
        )
        row = {
            "session_id": sid,
            "seq": 3,
            "kind": "AssistantMessage",
            "model": "gpt-5.5",
            "timestamp": now,
            "message": msg.to_json(),
            "created": None,
        }
        rebuilt = AgentSessionEvent.from_row(cast(Row, row))
        assert rebuilt == event
        assert isinstance(rebuilt.message, AssistantMessage)
        assert rebuilt.message.tool_calls[0].name == "Read"

    def test_from_row_unknown_message(self) -> None:
        sid = uuid4()
        msg = UnknownMessage(raw={"unmapped": True})
        row = {
            "session_id": sid,
            "seq": 0,
            "kind": "UnknownMessage",
            "model": None,
            "timestamp": None,
            "message": msg.to_json(),
            "created": None,
        }
        rebuilt = AgentSessionEvent.from_row(cast(Row, row))
        assert isinstance(rebuilt.message, UnknownMessage)
        assert rebuilt.message.raw == {"unmapped": True}

    def test_from_row_requires_session_id(self) -> None:
        # A row missing ``session_id`` must raise, not fabricate a random
        # uuid4 default (which would mis-scope the event to a nonexistent
        # session). Mirrors ``Inquiry.from_row`` requiring its identity keys.
        with pytest.raises(KeyError):
            AgentSessionEvent.from_row(
                cast(Row, {"kind": "UserMessage", "seq": 0, "message": {}})
            )

    def test_every_kind_value_has_a_member(self) -> None:
        for kind in get_args(Kind.__value__):
            assert message_for_kind(kind) is not None


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
