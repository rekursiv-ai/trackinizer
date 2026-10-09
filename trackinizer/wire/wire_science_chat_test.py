"""The science chat wire: ids and labels, and what a posted line may say."""

from __future__ import annotations

import uuid

from pydantic import ValidationError

import pytest

from trackinizer.wire.wire_science_chat import (
    CHAT_SESSION_PREFIX,
    ChatSend,
    chat_actor,
    chat_session_id,
    poster_label,
)


_WORKSPACE = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def test_a_conversations_session_is_named_by_its_id() -> None:
    """The id is what a restart re-attaches and a link finds."""
    conversation = uuid.uuid4()
    assert chat_session_id(conversation) == f"{CHAT_SESSION_PREFIX}{conversation}"
    assert chat_session_id(conversation).startswith("chat:")


def test_a_conversations_routing_name_is_short_and_never_the_assistants() -> None:
    conversation = uuid.UUID("3d0e9f1a-1b2c-4d5e-8f60-7a8b9c0d1e2f")
    assert chat_actor(conversation) == "chat-3d0e9f1a1b2c"


def test_a_poster_label_names_the_attested_email() -> None:
    assert poster_label("ada@example.com") == "poster:ada@example.com"


def test_a_line_is_science_chat_from_a_canvas_by_default() -> None:
    sent = ChatSend(workspace_id=_WORKSPACE, text="hello")
    assert (sent.kind, sent.conversation_id) == ("science", None)


@pytest.mark.parametrize("text", ["", " ", "\n\t "])
def test_a_line_must_hold_a_non_space_character(text: str) -> None:
    """Whitespace alone is nothing to send."""
    with pytest.raises(ValidationError):
        ChatSend(workspace_id=_WORKSPACE, text=text)


def test_a_line_is_at_most_16384_characters() -> None:
    """The bound is exactly 16,384."""
    assert ChatSend(workspace_id=_WORKSPACE, text="x" * 16_384).text
    with pytest.raises(ValidationError):
        ChatSend(workspace_id=_WORKSPACE, text="x" * 16_385)


def test_a_line_names_no_poster_and_no_unknown_field() -> None:
    """The poster is the attested identity, so the body has nowhere to put one."""
    for field, value in (("source", "mallory@example.com"), ("poster", "mallory")):
        with pytest.raises(ValidationError):
            ChatSend.model_validate(
                {"workspace_id": str(_WORKSPACE), "text": "hello", field: value},
            )


def test_science_is_the_only_kind_of_chat() -> None:
    with pytest.raises(ValidationError):
        ChatSend.model_validate(
            {"workspace_id": str(_WORKSPACE), "text": "hello", "kind": "incognito"},
        )


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
