"""Tests for the codex app-server JSON-RPC notification parser.

Fixtures mirror the ``ServerNotification`` shapes from
``codex app-server generate-json-schema`` (codex-cli 0.135.0).
"""

from __future__ import annotations

import json

from trackinizer.trax.run.adapters.codex_appserver import (
    parse_notification,
)
from trackinizer.types.agent_session_events import (
    AssistantMessage,
    ToolResult,
    UnknownMessage,
    UserMessage,
)


def _notif(method: str, params: object) -> bytes:
    return (
        json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n"
    ).encode()


class TestParseNotification:
    def test_agent_message_delta_is_assistant(self) -> None:
        ev = parse_notification(
            _notif(
                "item/agentMessage/delta",
                {"delta": "hi", "itemId": "i1", "threadId": "t", "turnId": "u"},
            )
        )
        assert ev is not None
        assert isinstance(ev.message, AssistantMessage)
        assert ev.message.text == "hi"

    def test_reasoning_summary_delta_is_thinking(self) -> None:
        ev = parse_notification(
            _notif(
                "item/reasoning/summaryTextDelta",
                {
                    "delta": "**Clarifying**",
                    "itemId": "i",
                    "threadId": "t",
                    "turnId": "u",
                },
            )
        )
        assert ev is not None
        assert isinstance(ev.message, AssistantMessage)
        assert ev.message.thinking == "**Clarifying**"

    def test_command_execution_output_delta_links_to_item(self) -> None:
        # Schema: CommandExecutionOutputDeltaNotification carries ``delta`` +
        # ``itemId`` (not ``chunk``); the result must name the call it answers.
        ev = parse_notification(
            _notif(
                "item/commandExecution/outputDelta",
                {"delta": "out", "itemId": "c1", "threadId": "t", "turnId": "u"},
            )
        )
        assert ev is not None
        assert isinstance(ev.message, ToolResult)
        assert ev.message.content == "out"
        assert ev.message.call_id == "c1"

    def test_item_completed_command_uses_command_name_and_args(self) -> None:
        # Schema: commandExecution ThreadItem -> name in ``command``, id in
        # ``id``; args are the real inputs, not the whole envelope.
        ev = parse_notification(
            _notif(
                "item/completed",
                {
                    "completedAtMs": 1,
                    "threadId": "t",
                    "turnId": "u",
                    "item": {
                        "type": "commandExecution",
                        "id": "c1",
                        "command": "ls -la",
                        "cwd": "/repo",
                        "status": "completed",
                    },
                },
            )
        )
        assert ev is not None
        assert isinstance(ev.message, AssistantMessage)
        assert len(ev.message.tool_calls) == 1
        call = ev.message.tool_calls[0]
        assert call.name == "ls -la"
        assert call.id == "c1"
        assert call.args == {"command": "ls -la", "cwd": "/repo"}
        assert "type" not in call.args

    def test_item_completed_mcp_tool_uses_tool_and_arguments(self) -> None:
        # Schema: mcpToolCall ThreadItem -> name in ``tool``, args in
        # ``arguments`` (arbitrary JSON), server in ``server``.
        ev = parse_notification(
            _notif(
                "item/completed",
                {
                    "completedAtMs": 1,
                    "threadId": "t",
                    "turnId": "u",
                    "item": {
                        "type": "mcpToolCall",
                        "id": "m1",
                        "server": "files",
                        "tool": "search",
                        "arguments": {"q": "x"},
                        "status": "completed",
                    },
                },
            )
        )
        assert ev is not None
        assert isinstance(ev.message, AssistantMessage)
        call = ev.message.tool_calls[0]
        assert call.name == "search"
        assert call.id == "m1"
        assert call.args == {"q": "x"}

    def test_item_started_user_message_is_user(self) -> None:
        ev = parse_notification(
            _notif(
                "item/started",
                {
                    "startedAtMs": 1,
                    "threadId": "t",
                    "turnId": "u",
                    "item": {"type": "userMessage", "text": "hi"},
                },
            )
        )
        assert ev is not None
        assert isinstance(ev.message, UserMessage)
        assert ev.message.text == "hi"

    def test_item_completed_unknown_item_type_is_unknown(self) -> None:
        ev = parse_notification(
            _notif(
                "item/completed",
                {
                    "completedAtMs": 1,
                    "threadId": "t",
                    "turnId": "u",
                    "item": {"type": "somethingNew"},
                },
            )
        )
        assert ev is not None
        assert isinstance(ev.message, UnknownMessage)

    def test_thread_started_is_skipped(self) -> None:
        assert parse_notification(_notif("thread/started", {"threadId": "t"})) is None

    def test_turn_completed_is_skipped(self) -> None:
        assert parse_notification(_notif("turn/completed", {"turnId": "u"})) is None

    def test_request_with_id_is_skipped(self) -> None:
        # A JSON-RPC request/response carries no session content.
        line = (json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}) + "\n").encode()
        assert parse_notification(line) is None

    def test_unmapped_method_is_skipped(self) -> None:
        assert parse_notification(_notif("account/updated", {})) is None

    def test_malformed_json_returns_none(self) -> None:
        assert parse_notification(b"{not json}") is None


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
