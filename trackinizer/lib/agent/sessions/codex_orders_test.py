"""Tests for Codex payload key orders."""

from __future__ import annotations

from trackinizer.lib.agent.sessions.codex_orders import payload_orders


def test_payload_orders_cover_every_payload_type() -> None:
    assert set(payload_orders()) == {
        "session_meta",
        "turn_context",
        "message",
        "reasoning",
        "function_call",
        "custom_tool_call",
        "function_call_output",
        "custom_tool_call_output",
        "agent_message",
        "tool_search_call",
        "web_search_call",
        "tool_search_output",
        "token_count",
        "exec_command_end",
        "patch_apply_end",
        "web_search_end",
        "result_row",
        "item_completed",
        "CommandExecution",
        "FileChange",
        "WebSearch",
        "compacted",
        "error",
    }


def test_every_order_names_each_key_once() -> None:
    for name, order in payload_orders().items():
        assert len(set(order)) == len(order), name


def test_orders_place_keys_where_codex_writes_them() -> None:
    orders = payload_orders()

    assert orders["session_meta"][:2] == ("session_id", "id")
    assert orders["token_count"] == ("type", "info", "rate_limits")
    assert orders["function_call"].index("name") < orders["function_call"].index(
        "arguments",
    )
    assert orders["compacted"] == ("message", "replacement_history")
    assert orders["error"][-1] == "codex_error_info"


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
