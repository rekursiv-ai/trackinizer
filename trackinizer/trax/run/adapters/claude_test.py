"""Tests for the claude adapter: real session-line fixtures → typed messages."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

import json


if TYPE_CHECKING:
    import pytest

from trackinizer.trax.run.adapters.base import Event
from trackinizer.trax.run.adapters.claude import ClaudeAdapter
from trackinizer.types.agent_session_events import (
    AssistantMessage,
    ToolResult,
    UnknownMessage,
    UserMessage,
)


# The claude adapter is line-stateless, so one instance serves every case.
adapter = ClaudeAdapter()


def _encode(obj: object) -> bytes:
    return (json.dumps(obj) + "\n").encode()


def _parse_one(raw: bytes) -> Event | None:
    """The single event for a one-message line, or ``None`` when skipped."""
    events = list(adapter.parse(raw, whole_file=False))
    assert len(events) <= 1, events
    return events[0] if events else None


def _user(content: object) -> bytes:
    return _encode({"type": "user", "sessionId": "s1", "message": {"content": content}})


def _assistant(blocks: list[object]) -> bytes:
    return _encode(
        {"type": "assistant", "sessionId": "s1", "message": {"content": blocks}}
    )


class TestClaudeParseLine:
    """Fixtures mirror the real claude 2.1.158 schema (top-level ``type`` +
    ``message.content`` blocks), verified against an on-disk session log.
    """

    def test_user_string_content_is_user_message(self) -> None:
        event = _parse_one(_user("hi"))
        assert event is not None
        assert isinstance(event.message, UserMessage)
        assert event.message.text == "hi"

    def test_user_tool_result_block_is_tool_result(self) -> None:
        event = _parse_one(
            _user([{"type": "tool_result", "tool_use_id": "toolu_x", "content": "ok"}])
        )
        assert event is not None
        assert isinstance(event.message, ToolResult)
        assert event.message.call_id == "toolu_x"
        assert event.message.content == "ok"

    def test_assistant_text_is_assistant_message(self) -> None:
        event = _parse_one(_assistant([{"type": "text", "text": "hello"}]))
        assert event is not None
        assert isinstance(event.message, AssistantMessage)
        assert event.message.text == "hello"

    def test_assistant_thinking_is_assistant_message(self) -> None:
        event = _parse_one(
            _assistant([{"type": "thinking", "thinking": "...", "signature": "s"}])
        )
        assert event is not None
        assert isinstance(event.message, AssistantMessage)
        assert event.message.thinking == "..."
        assert event.message.thinking_signature == "s"

    def test_assistant_two_thinking_blocks_are_joined(self) -> None:
        """Two thinking blocks in one assistant line must both survive (R2R-036).

        ``thinking`` was assigned per block, so a second block overwrote the
        first while text/tool_calls accumulate. Mirror the text handling and
        join the parts; the signature follows the last block that carries one.
        """
        event = _parse_one(
            _assistant(
                [
                    {"type": "thinking", "thinking": "first.", "signature": "s1"},
                    {"type": "thinking", "thinking": "second.", "signature": "s2"},
                ]
            )
        )
        assert event is not None
        assert isinstance(event.message, AssistantMessage)
        assert event.message.thinking == "first.second."
        assert event.message.thinking_signature == "s2"

    def test_assistant_tool_use_is_nested_tool_call(self) -> None:
        event = _parse_one(
            _assistant(
                [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Read",
                        "input": {"path": "x"},
                    }
                ]
            )
        )
        assert event is not None
        assert isinstance(event.message, AssistantMessage)
        assert len(event.message.tool_calls) == 1
        call = event.message.tool_calls[0]
        assert call.id == "t1"
        assert call.name == "Read"
        assert call.args == {"path": "x"}

    def test_duplicate_tool_use_id_is_deduped_not_dropped(self) -> None:
        """A repeated ``tool_use`` id must not abort the whole turn (R-41).

        ``AssistantMessage.__post_init__`` raises on a duplicate ``ToolCall.id``;
        an unguarded raise here is swallowed by the runner's ``_process_chunk``
        and the entire turn is dropped. The adapter must dedup tool calls by id
        (last-wins) before constructing the message, so the turn survives.
        """
        event = _parse_one(
            _assistant(
                [
                    {"type": "text", "text": "doing it"},
                    {
                        "type": "tool_use",
                        "id": "dup",
                        "name": "Read",
                        "input": {"n": 1},
                    },
                    {
                        "type": "tool_use",
                        "id": "dup",
                        "name": "Read",
                        "input": {"n": 2},
                    },
                ]
            )
        )
        assert event is not None, "duplicate tool_use_id dropped the whole turn"
        assert isinstance(event.message, AssistantMessage)
        assert event.message.text == "doing it"
        # One surviving call under the id; last write wins.
        assert len(event.message.tool_calls) == 1
        call = event.message.tool_calls[0]
        assert call.id == "dup"
        assert call.args == {"n": 2}

    def test_bookkeeping_types_are_skipped(self) -> None:
        for line_type in (
            "attachment",
            "ai-title",
            "queue-operation",
            "last-prompt",
            "mode",
            "permission-mode",
            "file-history-snapshot",
            "system",
        ):
            event = _parse_one(_encode({"type": line_type, "sessionId": "s1"}))
            assert event is None, line_type

    def test_unrecognized_type_is_unknown(self) -> None:
        event = _parse_one(_encode({"type": "telemetry-v2", "sessionId": "s1"}))
        assert event is not None
        assert isinstance(event.message, UnknownMessage)

    def test_malformed_json_returns_none(self) -> None:
        assert _parse_one(b"{not json}") is None

    def test_non_dict_returns_none(self) -> None:
        assert _parse_one(b'"just a string"') is None

    def test_user_line_with_two_tool_results_emits_both(self) -> None:
        """Batched parallel tool_results in one user line must all surface."""
        events = list(
            adapter.parse(
                _user(
                    [
                        {"type": "tool_result", "tool_use_id": "a", "content": "ra"},
                        {"type": "tool_result", "tool_use_id": "b", "content": "rb"},
                    ]
                ),
                whole_file=False,
            )
        )
        results = [e.message for e in events]
        assert all(isinstance(m, ToolResult) for m in results)
        assert [cast(ToolResult, m).call_id for m in results] == ["a", "b"]
        assert [cast(ToolResult, m).content for m in results] == ["ra", "rb"]


class TestClaudeSessionId:
    """Claude's own session id is the ``<session-id>.jsonl`` filename stem."""

    def test_session_id_from_path_is_filename_stem(self) -> None:
        path = Path.home() / ".claude" / "projects" / "hash" / "abc-123-def.jsonl"
        assert adapter.session_id_from_path(path) == "abc-123-def"

    def test_session_id_from_non_jsonl_path_is_none(self, tmp_path: Path) -> None:
        assert adapter.session_id_from_path(tmp_path / "notes.txt") is None


class TestClaudeProjectsDir:
    """The projects root honors ``$CLAUDE_CONFIG_DIR`` (hermetic launchers)."""

    def test_claude_config_dir_env_locates_sessions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A run under ``CLAUDE_CONFIG_DIR=<dir>`` must discover its projects.

        Study launchers spawn claude with a throwaway ``$CLAUDE_CONFIG_DIR``
        for hermeticity; an adapter hard-coded to ``~/.claude`` polls the
        wrong tree and captures nothing.
        """
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        project = tmp_path / "projects" / "-Users-x-repo"
        project.mkdir(parents=True)
        fixture = project / "abc-123-def.jsonl"
        fixture.write_text('{"type": "user"}\n')
        assert tuple(adapter.session_dirs()) == (project,)
        assert adapter.matches_session_file(fixture)

    def test_falls_back_to_home_claude_without_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        project = tmp_path / ".claude" / "projects" / "hash"
        project.mkdir(parents=True)
        assert tuple(adapter.session_dirs()) == (project,)


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
