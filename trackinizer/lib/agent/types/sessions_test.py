"""Tests for the provider-neutral session model."""

from __future__ import annotations

from trackinizer.lib.agent.types.sessions import (
    AssistantMessage,
    Attachment,
    FileReadResult,
    Thinking,
    ToolCall,
    ToolResult,
    UncategorizedToolResult,
    UserMessage,
)


def test_a_session_preserves_interleaved_tool_lifecycles() -> None:
    # Two calls run at once, so their answers arrive out of order; only the
    # call id ties each result to the call that asked for it.
    records = (
        AssistantMessage(content="Starting both."),
        ToolCall(call_id="a", name="Read", arguments={"path": "/a"}),
        UncategorizedToolResult(
            call_id="a",
            attachments=(Attachment(mime_descriptor="image/png", data=b"png"),),
        ),
        UserMessage(content="Also check B."),
        ToolCall(call_id="b", name="Read", arguments={"path": "/b"}),
        FileReadResult(call_id="b", content="B"),
        FileReadResult(call_id="a", content="A"),
    )

    assert [item.call_id for item in records if isinstance(item, ToolResult)] == [
        "a",
        "b",
        "a",
    ]


def test_reasoning_is_a_sibling_of_the_reply_not_nested_in_it() -> None:
    # Axiom 3: both come off one source line, and both are their own record --
    # a nested one would be unreachable by the index a context names.
    thinking = Thinking(context_id=0, content="Check the file.", encrypted="sig")
    assistant = AssistantMessage(context_id=0, content="Found it.")
    user = UserMessage(context_id=0, content="Check this.")

    records = (user, thinking, assistant)

    assert [type(record).__name__ for record in records] == [
        "UserMessage",
        "Thinking",
        "AssistantMessage",
    ]
    assert not hasattr(assistant, "thinking")


def test_a_record_names_its_settings_by_index_rather_than_copying_them() -> None:
    # Axiom 5: settings live in one context, and a record points at it, so a
    # session that switches providers mid-stream has one context per turn.
    records = (
        AssistantMessage(context_id=0, content="First turn."),
        AssistantMessage(context_id=4, content="Second turn."),
    )

    assert [record.context_id for record in records] == [0, 4]


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
