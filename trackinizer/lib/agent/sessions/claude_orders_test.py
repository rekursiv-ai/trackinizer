"""Tests for Claude record key orders."""

from __future__ import annotations

from trackinizer.lib.agent.sessions.claude_orders import (
    assistant_order,
    attachment_order,
    envelope_keys,
    failed_order,
    message_consumed,
    payload_orders,
    settings_keys,
    system_order,
    user_order,
)


def test_failed_order_preserves_provider_diagnostics_order() -> None:
    assert failed_order()[:6] == (
        "parentUuid",
        "isSidechain",
        "type",
        "uuid",
        "timestamp",
        "message",
    )


def test_envelope_and_settings_keys_are_claudes_per_line_keys() -> None:
    assert envelope_keys() == (
        "userType",
        "entrypoint",
        "cwd",
        "sessionId",
        "version",
        "gitBranch",
    )
    assert settings_keys() == ("isSidechain", "agentId", "mode")


def test_every_record_order_names_each_key_once_and_ends_with_the_session() -> None:
    orders = {
        "user": user_order(),
        "assistant": assistant_order(),
        "attachment": attachment_order(),
        "system": system_order(),
        "failed": failed_order(),
    }
    for name, order in orders.items():
        assert len(set(order)) == len(order), name
        assert order[0] == "parentUuid", name
        assert order[-1] == "session_id", name


def test_each_record_order_places_its_payload_key() -> None:
    assert user_order().index("message") < user_order().index("uuid")
    assert assistant_order().index("message") < assistant_order().index("requestId")
    assert attachment_order().index("attachment") < attachment_order().index("type")
    assert system_order().index("subtype") < system_order().index("content")


def test_a_records_own_fields_consume_role_and_content() -> None:
    assert message_consumed() == frozenset({"role", "content"})


def test_payload_orders_cover_each_tool_and_its_nested_shapes() -> None:
    orders = payload_orders()

    assert set(orders) == {
        "Bash",
        "Read",
        "Read.file",
        "Write",
        "Edit",
        "WebSearch",
        "WebFetch",
        "Agent",
        "Agent.done",
    }
    assert orders["Bash"][:2] == ("stdout", "stderr")
    assert orders["Read"] == ("type", "file")
    assert orders["Agent"].index("description") < orders["Agent"].index("prompt")
    assert orders["Agent.done"].index("prompt") < orders["Agent.done"].index("agentId")
    for name, order in orders.items():
        assert len(set(order)) == len(order), name


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
