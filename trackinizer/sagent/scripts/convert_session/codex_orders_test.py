"""Tests for Codex payload key orders."""

from __future__ import annotations

from importlib.resources import files

from treekle import ReadError, from_plain, json_loads

import pytest

from trackinizer.sagent.scripts.convert_session import codex
from trackinizer.sagent.scripts.convert_session.codex_orders import payload_orders


def _object(value: object) -> dict[str, object]:
    """Return ``value`` as a JSON object; ``{}`` for anything else."""
    try:
        return from_plain(value, dict[str, object], default=dict[str, object]())
    except ReadError:
        return {}


def _ordered_objects(line: dict[str, object]) -> list[tuple[str, dict[str, object]]]:
    """Return each table-ordered object of a rollout line, with its table's name."""
    payload = _object(line.get("payload"))
    outer = str(line.get("type"))
    # These payloads state no ``type`` of their own; the line's names them.
    kind = (
        outer
        if outer in {"compacted", "session_meta", "turn_context"}
        else str(payload.get("type"))
    )
    out = [(kind, payload)]
    if item := _object(payload.get("item")):
        out.append((str(item.get("type")), item))
    results = payload.get("results")
    rows = from_plain(
        None if isinstance(results, str) else results,
        list[object],
        default=list[object](),
    )
    out.extend(("result_row", _object(row)) for row in rows)
    return out


# Each table checked against lines a real codex wrote, unread by the adapter: every
# object orders the keys its table lists as the table does, and what the capture names
# is asserted, so a key spelled wrong reads as one never seen. Payload types no capture
# produced -- a function call, a web search, an agent message -- no line checks.
def test_every_captured_payload_orders_its_keys_as_its_table_does() -> None:
    tables = payload_orders()
    seen: dict[str, set[str]] = {name: set() for name in tables}
    disordered: list[tuple[str, list[str]]] = []
    testdata = files(__package__) / "testdata"
    for rollout in (
        testdata / "codex_main.jsonl",
        testdata / "kinds" / "codex_main.jsonl",
        testdata / "kinds" / "codex_fork.jsonl",
    ):
        for raw in rollout.read_text(encoding="utf-8").splitlines():
            for name, ordered in _ordered_objects(json_loads(raw, dict[str, object])):
                if name not in tables:
                    continue
                keys = [key for key in ordered if key in tables[name]]
                seen[name].update(keys)
                if keys != [key for key in tables[name] if key in ordered]:
                    disordered.append((name, keys))

    assert disordered == []
    covered = {
        name: [key for key in order if key in seen[name]]
        for name, order in tables.items()
        if seen[name]
    }
    assert covered == {
        "session_meta": [
            "session_id",
            "id",
            "forked_from_id",
            "timestamp",
            "cwd",
            "originator",
            "cli_version",
            "source",
            "thread_source",
            "model_provider",
            "base_instructions",
            "history_mode",
            "multi_agent_version",
            "context_window",
        ],
        "turn_context": [
            "turn_id",
            "cwd",
            "workspace_roots",
            "current_date",
            "timezone",
            "approval_policy",
            "approvals_reviewer",
            "sandbox_policy",
            "permission_profile",
            "file_system_sandbox_policy",
            "model",
            "comp_hash",
            "personality",
            "collaboration_mode",
            "multi_agent_version",
            "realtime_active",
            "effort",
            "summary",
        ],
        "message": [
            "type",
            "id",
            "role",
            "content",
            "phase",
            "internal_chat_message_metadata_passthrough",
        ],
        "reasoning": [
            "type",
            "id",
            "summary",
            "encrypted_content",
            "internal_chat_message_metadata_passthrough",
        ],
        "custom_tool_call": [
            "type",
            "id",
            "status",
            "call_id",
            "name",
            "input",
            "internal_chat_message_metadata_passthrough",
        ],
        "custom_tool_call_output": [
            "type",
            "id",
            "call_id",
            "output",
            "internal_chat_message_metadata_passthrough",
        ],
        "token_count": ["type", "info", "rate_limits"],
        "item_completed": [
            "type",
            "thread_id",
            "turn_id",
            "item",
            "started_at_ms",
            "completed_at_ms",
        ],
        "CommandExecution": [
            "type",
            "id",
            "process_id",
            "command",
            "cwd",
            "parsed_cmd",
            "source",
            "status",
            "stdout",
            "stderr",
            "aggregated_output",
            "exit_code",
            "duration",
            "formatted_output",
        ],
        "FileChange": ["type", "id", "changes", "status", "stdout", "stderr"],
        "compacted": ["message", "replacement_history"],
    }


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


_PAYLOAD = ("internal_chat_message_metadata_passthrough",)


# Every key, including those no capture holds, by hand: the reader stores a line's own
# order only where the table cannot reproduce it.
@pytest.mark.parametrize(
    ("kind", "order"),
    [
        (
            "session_meta",
            (
                "session_id",
                "id",
                "forked_from_id",
                "parent_thread_id",
                "timestamp",
                "cwd",
                "originator",
                "cli_version",
                "source",
                "thread_source",
                "agent_nickname",
                "agent_role",
                "agent_path",
                "model_provider",
                "base_instructions",
                "history_mode",
                "multi_agent_version",
                "context_window",
                "git",
            ),
        ),
        (
            "turn_context",
            (
                "turn_id",
                "cwd",
                "workspace_roots",
                "current_date",
                "timezone",
                "approval_policy",
                "approvals_reviewer",
                "sandbox_policy",
                "permission_profile",
                "active_permission_profile",
                "file_system_sandbox_policy",
                "model",
                "comp_hash",
                "personality",
                "collaboration_mode",
                "multi_agent_version",
                "multi_agent_mode",
                "realtime_active",
                "effort",
                "summary",
                "user_instructions",
                "truncation_policy",
            ),
        ),
        ("message", ("type", "id", "role", "content", "phase", *_PAYLOAD)),
        (
            "reasoning",
            ("type", "id", "summary", "content", "encrypted_content", *_PAYLOAD),
        ),
        (
            "function_call",
            ("type", "id", "name", "namespace", "arguments", "call_id", *_PAYLOAD),
        ),
        (
            "custom_tool_call",
            ("type", "id", "status", "call_id", "name", "input", *_PAYLOAD),
        ),
        (
            "function_call_output",
            ("type", "id", "call_id", "name", "output", *_PAYLOAD),
        ),
        (
            "custom_tool_call_output",
            ("type", "id", "call_id", "name", "output", *_PAYLOAD),
        ),
        (
            "agent_message",
            ("type", "id", "author", "recipient", "content", *_PAYLOAD),
        ),
        (
            "tool_search_call",
            (
                "type",
                "id",
                "call_id",
                "status",
                "execution",
                "action",
                "arguments",
                *_PAYLOAD,
            ),
        ),
        ("web_search_call", ("type", "id", "status", "action", *_PAYLOAD)),
        (
            "tool_search_output",
            ("type", "id", "call_id", "status", "execution", "tools", *_PAYLOAD),
        ),
        ("token_count", ("type", "info", "rate_limits")),
        (
            "exec_command_end",
            (
                "type",
                "call_id",
                "process_id",
                "turn_id",
                "command",
                "cwd",
                "parsed_cmd",
                "source",
                "stdout",
                "stderr",
                "aggregated_output",
                "exit_code",
                "duration",
                "formatted_output",
            ),
        ),
        (
            "patch_apply_end",
            (
                "type",
                "call_id",
                "turn_id",
                "stdout",
                "stderr",
                "success",
                "changes",
                "status",
            ),
        ),
        ("web_search_end", ("type", "call_id", "query", "action", "results")),
        ("result_row", ("type", "ref_id", "title", "url", "domain", "snippet")),
        (
            "item_completed",
            (
                "type",
                "thread_id",
                "turn_id",
                "item",
                "started_at_ms",
                "completed_at_ms",
            ),
        ),
        (
            "CommandExecution",
            (
                "type",
                "id",
                "process_id",
                "command",
                "cwd",
                "parsed_cmd",
                "source",
                "status",
                "stdout",
                "stderr",
                "aggregated_output",
                "exit_code",
                "duration",
                "formatted_output",
            ),
        ),
        ("FileChange", ("type", "id", "changes", "status", "stdout", "stderr")),
        ("WebSearch", ("type", "id", "query")),
        ("compacted", ("message", "replacement_history")),
        ("error", ("type", "message", "codex_error_info")),
    ],
)
def test_a_payload_in_codexs_order_needs_no_stored_order(
    kind: str,
    order: tuple[str, ...],
) -> None:
    """Pins each table as the spec; it is no evidence of the order codex writes."""
    # Reversed, with a key no table lists first: it goes last, after the table's.
    source = dict.fromkeys(("unlisted", *reversed(order)), 0)

    assert codex._canonical_order(source, kind=kind) == [*order, "unlisted"]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
