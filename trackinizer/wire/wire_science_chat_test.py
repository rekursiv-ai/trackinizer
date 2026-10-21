"""The science chat wire: ids and labels, and what a posted line may say."""

from __future__ import annotations

from typing import get_args

import uuid

from pydantic import ValidationError

import pytest

from trackinizer.wire.wire_science_chat import (
    CHAT_PARTIAL_CHARS,
    CHAT_SESSION_PREFIX,
    ChatLineKind,
    ChatPartial,
    ChatPartialState,
    ChatSend,
    chat_actor,
    chat_session_id,
    poster_label,
)
from trackinizer.wire.wire_sessions import InboundKind


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


def test_a_post_is_a_persons_line_by_default() -> None:
    sent = ChatSend(workspace_id=_WORKSPACE, text="hello")
    assert (sent.kind, sent.payload, sent.conversation_id) == ("line", None, None)


def test_the_post_kinds_are_the_inbound_kinds() -> None:
    """A post's kind is handed on as the queued message's, so the sets match."""
    assert get_args(ChatLineKind.__value__) == get_args(InboundKind.__value__)


def test_a_browser_that_still_sends_science_posts_a_line() -> None:
    sent = ChatSend.model_validate(
        {"workspace_id": str(_WORKSPACE), "text": "hello", "kind": "science"},
    )
    assert sent.kind == "line"


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("command", None),
        ("interrupt", None),
        ("answer", {"prompt_id": "p1", "decision": "allow"}),
        (
            "answer",
            {
                "prompt_id": "p1",
                "decision": "deny",
                "updated_input": None,
                "message": "not that file",
            },
        ),
        ("settings", {"model": "opus"}),
        ("settings", {"effort": "high"}),
    ],
)
def test_each_kind_takes_the_payload_its_queue_documents(
    kind: str,
    payload: dict[str, object] | None,
) -> None:
    sent = ChatSend.model_validate(
        {
            "workspace_id": str(_WORKSPACE),
            "text": "Yes",
            "kind": kind,
            "payload": payload,
        },
    )
    assert (sent.kind, sent.payload) == (kind, payload)


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("line", {"model": "opus"}),
        ("command", {"x": 1}),
        ("answer", None),
        ("answer", {"prompt_id": "p1", "decision": "maybe"}),
        ("answer", {"prompt_id": "", "decision": "allow"}),
        ("answer", {"prompt_id": "p1", "decision": "allow", "poster": "mallory"}),
        ("settings", {}),
        ("settings", None),
        ("incognito", None),
    ],
)
def test_a_payload_the_kind_does_not_take_is_refused(
    kind: str,
    payload: dict[str, object] | None,
) -> None:
    with pytest.raises(ValidationError):
        ChatSend.model_validate(
            {
                "workspace_id": str(_WORKSPACE),
                "text": "Yes",
                "kind": kind,
                "payload": payload,
            },
        )


def test_a_fork_starts_from_a_line_only() -> None:
    fork = {"session_id": str(uuid.uuid4()), "part": 0, "idx": 3}
    assert ChatSend.model_validate(
        {"workspace_id": str(_WORKSPACE), "text": "hi", "fork": fork},
    ).fork
    with pytest.raises(ValidationError):
        ChatSend.model_validate(
            {
                "workspace_id": str(_WORKSPACE),
                "text": "/compact",
                "kind": "command",
                "fork": fork,
            },
        )


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


def test_a_partial_holds_at_most_16384_characters() -> None:
    """The server keeps a partial per chat in memory, so its size is bounded."""
    assert CHAT_PARTIAL_CHARS == 16_384
    assert ChatPartial(turn="t1", text="x" * 16_384).done is False
    with pytest.raises(ValidationError):
        ChatPartial(turn="t1", text="x" * 16_385)


@pytest.mark.parametrize("turn", ["", "a/b", "a b", "x" * 65])
def test_a_partials_turn_is_a_short_word_that_fits_a_path(turn: str) -> None:
    """The turn is a path segment of the delete, so it holds no slash or space."""
    assert ChatPartial(turn="ab_C-9" + "x" * 58, text="").turn
    with pytest.raises(ValidationError):
        ChatPartial(turn=turn, text="")


def test_a_partial_and_its_state_name_no_unknown_field() -> None:
    with pytest.raises(ValidationError):
        ChatPartial.model_validate({"turn": "t1", "text": "", "poster": "mallory"})
    with pytest.raises(ValidationError):
        ChatPartialState.model_validate({"seq": 0, "partial": None, "text": "x"})
    assert ChatPartialState(seq=0, partial=None).partial is None


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
