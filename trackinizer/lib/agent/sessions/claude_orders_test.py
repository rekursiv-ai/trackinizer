"""Tests for Claude record key orders."""

from __future__ import annotations

from dataclasses import replace
from importlib.resources import files
from io import StringIO
from typing import TYPE_CHECKING

from treekle import ReadError, from_plain, immutable, json_loads

import pytest

from trackinizer.lib.agent.sessions import claude
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
from trackinizer.sagent.session.types import IncompleteRecord, UncategorizedRecord


if TYPE_CHECKING:
    from trackinizer.sagent.session.types import SessionRecord


def _orderless(record: SessionRecord) -> SessionRecord:
    """Return ``record`` without the key order its line stored, as a foreign one has."""
    if isinstance(record, IncompleteRecord | UncategorizedRecord):
        return record
    if "$keys" not in record.extra:
        return record
    kept = {key: value for key, value in record.extra.items() if key != "$keys"}
    return replace(record, extra=immutable(kept))


# Independent evidence of claude's order: lines a real claude wrote. Written back from the
# tables alone, as a record converted from another provider is, each comes out byte for
# byte. A ``system`` line is the exception the tables name: it writes several orders, so
# it always stores its own.
@pytest.mark.parametrize("fixture", ["claude_main.jsonl", "claude_sidechain.jsonl"])
def test_a_captured_line_is_written_in_claudes_order_from_the_tables_alone(
    fixture: str,
) -> None:
    native = (files(__package__) / "testdata" / fixture).read_text(encoding="utf-8")
    records = [_orderless(r) for r in claude.normalize(StringIO(native))]

    out = StringIO()
    claude.denormalize(records, out)

    pairs = zip(native.splitlines(), out.getvalue().splitlines(), strict=True)
    assert [
        (written, read)
        for read, written in pairs
        if json_loads(read, dict[str, object]).get("type") != "system"
        and written != read
    ] == []


# The tables are only the fallback: a line claude wrote stores its own order, so keys no
# table lists -- a newer CLI's ``turnOrigin``, ``wireToolInputs`` -- survive in place.
def test_a_native_line_keeps_the_keys_no_table_lists() -> None:
    fixture = files(__package__) / "testdata" / "kinds" / "claude_main.jsonl"
    native = fixture.read_text(encoding="utf-8")
    unlisted = {"turnOrigin", "wireToolInputs", "permissionDecision"}
    assert unlisted <= {
        key for raw in native.splitlines() for key in json_loads(raw, dict[str, object])
    }
    assert not unlisted & {*user_order(), *assistant_order()}

    out = StringIO()
    claude.denormalize(list(claude.normalize(StringIO(native))), out)

    assert out.getvalue() == native


def _governed(
    line: dict[str, object],
    *,
    tools: dict[str, str],
) -> list[tuple[str, dict[str, object]]]:
    """Return each table that orders part of ``line``, with the object it orders."""
    kind = line.get("type")
    message = _object(line.get("message"))
    # A user line's content may be a string, which holds no blocks.
    content = message.get("content")
    said = None if isinstance(content, str) else content
    blocks = [_object(b) for b in from_plain(said, list[object], default=[])]
    if kind == "assistant":
        for block in blocks:
            if block.get("type") == "tool_use":
                tools[str(block["id"])] = str(block["name"])
    out = [("failed" if "isApiErrorMessage" in line else str(kind), line)]
    if result := _object(line.get("toolUseResult")):
        called = [tools.get(str(b.get("tool_use_id")), "") for b in blocks]
        # A finished subagent's report is the one Agent shape that states no isAsync.
        tool = next(filter(None, called), "")
        tool = "Agent.done" if tool == "Agent" and "isAsync" not in result else tool
        out.append((tool, result))
        if tool == "Read":
            out.append(("Read.file", _object(result.get("file"))))
    return out


def _object(value: object) -> dict[str, object]:
    """Return ``value`` as a JSON object; ``{}`` for anything else."""
    try:
        return from_plain(value, dict[str, object], default={})
    except ReadError:
        return {}


# Each table checked against lines a real claude wrote, unread by the adapter: every
# line orders the keys a table lists as the table does. The kinds capture is newer and
# carries keys no table lists (``wireToolInputs``, ``turnOrigin``), which this ignores.
# What the captures name is asserted too, so a key spelled wrong reads as one never
# seen. The rest -- an API failure, a compaction summary, a shutdown -- no capture holds.
def test_every_captured_line_orders_its_keys_as_its_table_does() -> None:
    tables = {
        "user": user_order(),
        "assistant": assistant_order(),
        "attachment": attachment_order(),
        "system": system_order(),
        "failed": failed_order(),
        **payload_orders(),
    }
    testdata = files(__package__) / "testdata"
    seen: dict[str, set[str]] = {name: set() for name in tables}
    disordered: list[tuple[str, list[str]]] = []
    for fixture in (
        testdata / "claude_main.jsonl",
        testdata / "claude_sidechain.jsonl",
        testdata / "kinds" / "claude_main.jsonl",
    ):
        tools: dict[str, str] = {}
        for raw in fixture.read_text(encoding="utf-8").splitlines():
            line = json_loads(raw, dict[str, object])
            for name, ordered in _governed(line, tools=tools):
                if name not in tables:
                    continue
                keys = [key for key in ordered if key in tables[name]]
                seen[name].update(keys)
                if keys != [key for key in tables[name] if key in ordered]:
                    disordered.append((name, keys))

    assert disordered == []
    assert {name: [k for k in tables[name] if k in seen[name]] for name in tables} == {
        "user": [
            "parentUuid",
            "isSidechain",
            "promptId",
            "agentId",
            "type",
            "message",
            "isMeta",
            "uuid",
            "timestamp",
            "permissionMode",
            "origin",
            "promptSource",
            "sourceToolUseID",
            "interruptedMessageId",
            "toolUseResult",
            "toolDenialKind",
            "sourceToolAssistantUUID",
            "session_id",
        ],
        "assistant": [
            "parentUuid",
            "isSidechain",
            "agentId",
            "message",
            "requestId",
            "attributionSkill",
            "attributionAgent",
            "attributionMcpServer",
            "attributionMcpTool",
            "type",
            "uuid",
            "timestamp",
            "effort",
            "session_id",
        ],
        "attachment": [
            "parentUuid",
            "isSidechain",
            "agentId",
            "attachment",
            "type",
            "uuid",
            "timestamp",
            "session_id",
        ],
        "system": [
            "parentUuid",
            "isSidechain",
            "type",
            "subtype",
            "timestamp",
            "uuid",
            "isMeta",
        ],
        "failed": [],
        "Bash": [
            "stdout",
            "stderr",
            "interrupted",
            "isImage",
            "returnCodeInterpretation",
            "noOutputExpected",
        ],
        "Read": ["type", "file"],
        "Read.file": ["filePath", "content", "numLines", "startLine", "totalLines"],
        "Write": [
            "type",
            "filePath",
            "content",
            "structuredPatch",
            "originalFile",
            "userModified",
        ],
        "Edit": [
            "filePath",
            "oldString",
            "newString",
            "originalFile",
            "structuredPatch",
            "userModified",
            "replaceAll",
        ],
        "WebSearch": ["query", "results", "durationSeconds", "searchCount"],
        "WebFetch": ["bytes", "code", "codeText", "result", "durationMs", "url"],
        "Agent": [
            "isAsync",
            "status",
            "agentId",
            "description",
            "resolvedModel",
            "prompt",
            "outputFile",
            "canReadOutputFile",
        ],
        "Agent.done": [
            "status",
            "prompt",
            "agentId",
            "agentType",
            "content",
            "resolvedModel",
            "totalDurationMs",
            "totalTokens",
            "totalToolUseCount",
        ],
    }


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
