"""The science chat wire: ids and labels, and what a start may say."""

from __future__ import annotations

import uuid

from pydantic import ValidationError

import pytest

from trackinizer.wire.wire_science_chat import (
    CHAT_SESSION_PREFIX,
    NO_KEY,
    ChatForkAt,
    ChatSent,
    ChatStart,
    chat_actor,
    chat_conversation_id,
    chat_session_id,
    fork_point_label,
    poster_label,
)


_CANVAS = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


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


def test_a_chat_sessions_id_reads_back_as_its_conversation() -> None:
    conversation = uuid.uuid4()

    assert chat_conversation_id(chat_session_id(conversation)) == conversation


@pytest.mark.parametrize(
    "cli_session_id",
    [None, "0b3c2f7e-1d4a-4f5e-9a1b-2c3d4e5f6a7b", "chat:not-a-uuid", "chat:"],
    ids=["none", "another-cli", "malformed", "empty"],
)
def test_any_other_session_id_names_no_conversation(cli_session_id: str | None) -> None:
    assert chat_conversation_id(cli_session_id) is None


def test_a_fork_point_label_names_the_records_part_and_position() -> None:
    assert fork_point_label(part=2, idx=17) == "fork-at:2:17"


def test_a_start_names_its_canvas_and_a_fork_and_refuses_a_line() -> None:
    fork = {"session_id": str(uuid.uuid4()), "part": 1, "idx": 2}

    plain = ChatStart.model_validate({"canvas_id": str(_CANVAS)})
    forked = ChatStart.model_validate({"canvas_id": str(_CANVAS), "fork": fork})

    assert (plain.canvas_id, plain.fork) == (_CANVAS, None)
    assert forked.fork == ChatForkAt.model_validate(fork)
    with pytest.raises(ValidationError, match="extra_forbidden"):
        _ = ChatStart.model_validate({"canvas_id": str(_CANVAS), "text": "hello"})


def test_a_started_chat_always_names_its_session() -> None:
    sent = {"conversation_id": str(uuid.uuid4()), "canvas_id": str(_CANVAS)}

    with pytest.raises(ValidationError, match="session_id"):
        _ = ChatSent.model_validate({**sent, "session_id": None})
    assert NO_KEY == "no_key"


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
