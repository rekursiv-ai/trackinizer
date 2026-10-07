"""Tests for the canvas Chat wire contract."""

from __future__ import annotations

from pydantic import ValidationError

import pytest

from trackinizer.wire.wire_chats import ChatReply


class TestChatReply:
    @pytest.mark.parametrize("text", ["", "  ", "\n\t"])
    def test_an_answer_needs_a_non_space_character(self, text: str) -> None:
        with pytest.raises(ValidationError, match="A reply needs text"):
            _ = ChatReply(text=text, kind="answer")

    def test_an_empty_status_clears(self) -> None:
        assert ChatReply(text="", kind="status").text == ""

    @pytest.mark.parametrize("text", [" ", "\n\t "])
    def test_a_status_of_only_whitespace_is_refused(self, text: str) -> None:
        with pytest.raises(ValidationError, match="A reply needs text"):
            _ = ChatReply(text=text, kind="status")

    def test_a_reply_is_at_most_65536_characters(self) -> None:
        assert ChatReply(text="x" * 65_536, kind="answer").text
        with pytest.raises(ValidationError):
            _ = ChatReply(text="x" * 65_537, kind="answer")

    def test_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            _ = ChatReply.model_validate({"text": "x", "kind": "answer", "to": "y"})


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
