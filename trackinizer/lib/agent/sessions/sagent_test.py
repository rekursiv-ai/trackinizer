"""sagent -> IR: each record family lands on the IR type for what it did."""

from __future__ import annotations

from io import StringIO
from typing import cast

import inspect
import json

from trackinizer.lib.agent.sessions import sagent
from trackinizer.lib.agent.sessions.convert import detect_format, main
from trackinizer.lib.agent.types.sessions import (
    AgentStatusResult,
    AgentToAgentMessage,
    AssistantMessage,
    ContextCompaction,
    FileEditResult,
    FileReadResult,
    FileWriteResult,
    IncompleteRecord,
    SessionRecord,
    ShellCommandResult,
    Thinking,
    TokenUsage,
    ToolCall,
    TurnContext,
    UncategorizedRecord,
    UncategorizedToolResult,
    UserMessage,
    WebFetchResult,
)


def _read(*lines: dict[str, object] | str) -> list[SessionRecord]:
    """Normalize ``lines`` written as one sagent session file."""
    text = "".join(
        (line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines
    )
    return list(sagent.normalize(StringIO(text)))


def _history(kind: str, **fields: object) -> dict[str, object]:
    return {
        "kind": "history",
        "ref": {"session_id": "s", "ordinal": 0},
        "type": kind,
    } | fields


def _meta(**fields: object) -> dict[str, object]:
    return {"kind": "meta", "session_id": "s", "model_id": "opus-5.5"} | fields


def _only[T](records: list[SessionRecord], kind: type[T]) -> list[T]:
    return [record for record in records if isinstance(record, kind)]


def test_a_user_turn_becomes_a_user_message() -> None:
    records = _read(_history("user", text="fix it", timestamp=1790000000.5))

    [message] = _only(records, UserMessage)
    assert message.content == "fix it"
    assert message.timestamp is not None
    assert message.timestamp.startswith("2026-09-21T")


def test_an_assistant_turn_splits_into_thinking_text_and_calls() -> None:
    """Axiom 3: each act is its own record, in the order the model made them."""
    records = _read(
        _history(
            "assistant",
            text="Reading the file.",
            thinking_blocks=[
                {"type": "thinking", "thinking": "look first", "signature": "sig"},
            ],
            tool_calls=[{"id": "t1", "name": "Read", "args": {"file_path": "/a.py"}}],
        ),
    )

    acts = [r for r in records if isinstance(r, (Thinking, AssistantMessage, ToolCall))]
    assert [type(r) for r in acts] == [Thinking, AssistantMessage, ToolCall]
    thinking, text, call = acts
    assert isinstance(thinking, Thinking)
    assert thinking.content == "look first"
    assert thinking.encrypted == "sig"
    assert isinstance(text, AssistantMessage)
    assert text.content == "Reading the file."
    assert isinstance(call, ToolCall)
    assert (call.call_id, call.name, dict(call.arguments)) == (
        "t1",
        "Read",
        {"file_path": "/a.py"},
    )


def test_an_empty_assistant_text_is_not_a_message() -> None:
    call: dict[str, object] = {"id": "t1", "name": "Bash", "args": {}}
    records = _read(_history("assistant", text="", tool_calls=[call]))

    assert not _only(records, AssistantMessage)


def test_a_result_is_typed_by_the_call_it_answers() -> None:
    """Sagent names no tool on a result; the reader remembers each call."""
    records = _read(
        _history(
            "assistant",
            text="",
            tool_calls=[
                {"id": "b", "name": "Bash", "args": {"command": "pytest -q"}},
                {"id": "r", "name": "Read", "args": {"file_path": "/a.py"}},
                {
                    "id": "e",
                    "name": "Edit",
                    "args": {
                        "file_path": "/a.py",
                        "old_string": "x",
                        "new_string": "y",
                    },
                },
                {"id": "g", "name": "Grep", "args": {"pattern": "x"}},
            ],
        ),
        _history("tool_result", call_id="b", content="1 failed", is_error=True),
        _history("tool_result", call_id="r", content="print(1)", is_error=False),
        _history("tool_result", call_id="e", content="ok", is_error=False),
        _history("tool_result", call_id="g", content="a.py:1", is_error=False),
    )

    shell, read, edit, grep = [
        r for r in records if hasattr(r, "call_id") and not isinstance(r, ToolCall)
    ]
    assert isinstance(shell, ShellCommandResult)
    assert shell.command == ("pytest -q",)
    assert shell.stdout == "1 failed"
    assert shell.extra["is_error"] is True
    assert isinstance(read, FileReadResult)
    assert (read.path, read.content) == ("/a.py", "print(1)")
    assert isinstance(edit, FileEditResult)
    assert edit.path == "/a.py"
    assert edit.edits[0].before == "x"
    assert edit.edits[0].after == "y"
    assert isinstance(grep, UncategorizedToolResult)
    assert grep.content == "a.py:1"
    assert grep.extra["is_error"] is False


def test_write_and_fetch_results_carry_what_their_calls_named() -> None:
    records = _read(
        _history(
            "assistant",
            text="",
            tool_calls=[
                {"id": "w", "name": "Write", "args": {"path": "/b.py", "content": "z"}},
                {"id": "f", "name": "WebFetch", "args": {"url": "https://x.org"}},
            ],
        ),
        _history("tool_result", call_id="w", content="wrote", is_error=False),
        _history("tool_result", call_id="f", content="page", is_error=False),
    )

    [write] = _only(records, FileWriteResult)
    assert (write.path, write.content) == ("/b.py", "z")
    [fetch] = _only(records, WebFetchResult)
    assert (fetch.url, fetch.content) == ("https://x.org", "page")


def test_a_context_override_is_a_compaction() -> None:
    records = _read({"kind": "context_override", "tokens": 12, "payload": {}})

    [compaction] = _only(records, ContextCompaction)
    assert dict(compaction.extra) == {"tokens": 12}


def test_unknown_message_families_are_kept_uncategorized() -> None:
    records = _read(
        {"kind": "message", "descriptor": "text/x-banner", "content": "hi"},
        {"kind": "message", "role": "system", "content": "be terse"},
    )

    assert [r.kind for r in _only(records, UncategorizedRecord)] == [
        "message/text/x-banner",
        "message/system",
    ]


def test_a_result_for_an_unseen_call_is_uncategorized() -> None:
    records = _read(
        _history("tool_result", call_id="nope", content="?", is_error=False),
    )

    [result] = _only(records, UncategorizedToolResult)
    assert result.call_id == "nope"


def test_a_spawn_result_names_the_child_it_started() -> None:
    """The parent-to-child link: a persistent spawn records its child's dir."""
    records = _read(
        _history(
            "assistant",
            text="",
            tool_calls=[
                {
                    "id": "sp",
                    "name": "AgentSpawn",
                    "args": {
                        "prompt": "review x",
                        "label": "rev",
                        "model_id": "luna",
                        "persistent": True,
                    },
                },
            ],
        ),
        {
            "kind": "persistent_agent",
            "label": "rev",
            "session_dir": "/home/u/.sagent/projects/p/s/3640f519-c4c6",
            "state": "running",
        },
        _history(
            "tool_result",
            call_id="sp",
            content="Persistent agent started: rev.",
            is_error=False,
        ),
    )

    [status] = _only(records, AgentStatusResult)
    assert status.agent_id == "3640f519-c4c6"
    assert status.agent_kind == "rev"
    assert status.prompt == "review x"
    assert status.model == "luna"


def test_a_one_shot_spawn_result_carries_its_prompt_and_reply() -> None:
    records = _read(
        _history(
            "assistant",
            text="",
            tool_calls=[
                {"id": "sp", "name": "AgentSpawn", "args": {"prompt": "count files"}},
            ],
        ),
        _history("tool_result", call_id="sp", content="There are 3.", is_error=False),
    )

    [status] = _only(records, AgentStatusResult)
    assert status.agent_id is None
    assert status.prompt == "count files"
    assert status.content == "There are 3."


def test_a_peer_message_is_an_agent_to_agent_message() -> None:
    records = _read(_history("agent_send", source="rev", text="Findings: none"))

    [message] = _only(records, AgentToAgentMessage)
    assert (message.sender, message.content) == ("rev", "Findings: none")


def test_a_completed_compaction_is_a_context_compaction() -> None:
    records = _read(
        _history("compact_started"),
        _history("compact_complete", token_before=900, token_after=100),
    )

    [compaction] = _only(records, ContextCompaction)
    assert compaction.extra["token_before"] == 900


def test_meta_states_model_changes_and_cumulative_usage() -> None:
    """The model is a setting (restated only on change); spend is accounting."""
    records = _read(
        _meta(total_cost_usd=0.0, tokens={"input_tokens": 0, "output_tokens": 0}),
        _meta(total_cost_usd=1.5, tokens={"input_tokens": 10, "output_tokens": 2}),
        _meta(
            model_id="luna-6",
            total_cost_usd=2.0,
            tokens={"input_tokens": 20, "output_tokens": 3},
        ),
    )

    assert [c.model for c in _only(records, TurnContext)] == ["opus-5.5", "luna-6"]
    usage = _only(records, TokenUsage)
    assert usage[-1].info["cost_usd"] == 2.0
    assert usage[-1].info["input_tokens"] == 20


def test_side_records_are_kept_uncategorized() -> None:
    records = _read({"kind": "tool_state", "bash_cwd": "/w"})

    [kept] = _only(records, UncategorizedRecord)
    assert kept.kind == "tool_state"


def test_a_line_that_is_not_json_is_kept_verbatim() -> None:
    records = _read("{truncated")

    [kept] = _only(records, IncompleteRecord)
    assert kept.text.startswith("{truncated")


def test_the_descriptor_family_reads_like_history() -> None:
    """The older ``kind: message`` records tagged by MIME-like descriptors."""
    call = {
        "descriptor": "multipart/x-tool-call",
        "content": [
            {"descriptor": "text/x-queue-id", "content": "t1"},
            {"descriptor": "application/x-tool-read", "content": {"file_path": "/v"}},
        ],
    }
    records = _read(
        {
            "kind": "message",
            "descriptor": "text/x-user-message",
            "content": "why slow",
            "_timestamp": 1_776_975_139_999_181_786,
        },
        {
            "kind": "message",
            "descriptor": "multipart/x-assistant-turn",
            "content": [
                {"descriptor": "text/x-queue-id", "content": "msg_1"},
                {
                    "descriptor": "application/x-thinking-anthropic",
                    "content": {"thinking": "hm", "signature": "s"},
                },
                {"descriptor": "text/plain", "content": "Looking."},
                call,
            ],
        },
        {
            "kind": "message",
            "descriptor": "multipart/x-tool-result",
            "content": [
                {"descriptor": "text/x-queue-id", "content": "t1"},
                {"descriptor": "text/plain", "content": "set x"},
            ],
        },
    )

    user = _only(records, UserMessage)[0]
    assert user.content == "why slow"
    assert user.timestamp is not None
    assert user.timestamp.startswith("2026-04-23T")
    assert [t.content for t in _only(records, Thinking)] == ["hm"]
    assert [a.content for a in _only(records, AssistantMessage)] == ["Looking."]
    [tool_call] = _only(records, ToolCall)
    assert (tool_call.call_id, tool_call.name) == ("t1", "Read")
    [read] = _only(records, FileReadResult)
    assert (read.path, read.content) == ("/v", "set x")


def test_a_descriptor_error_part_marks_the_result_failed() -> None:
    records = _read(
        {
            "kind": "message",
            "descriptor": "multipart/x-tool-result",
            "content": [
                {"descriptor": "text/x-queue-id", "content": "t9"},
                {"descriptor": "text/x-error", "content": "boom"},
            ],
        },
    )

    [result] = _only(records, UncategorizedToolResult)
    assert result.extra["is_error"] is True
    assert result.content == "boom"


def test_the_role_family_reads_like_history() -> None:
    """The oldest ``kind: message`` records tagged by ``role``."""
    records = _read(
        {"kind": "message", "role": "user", "content": "hi"},
        {
            "kind": "message",
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "t1", "name": "Bash", "input": {"command": "ls"}}],
        },
        {
            "kind": "message",
            "role": "tool",
            "tool_call_id": "t1",
            "content": "[exit code: 2]",
            "is_error": False,
        },
    )

    assert [u.content for u in _only(records, UserMessage)] == ["hi"]
    [call] = _only(records, ToolCall)
    assert dict(call.arguments) == {"command": "ls"}
    [shell] = _only(records, ShellCommandResult)
    assert shell.stdout == "[exit code: 2]"


def test_convert_names_the_format_but_does_not_offer_it() -> None:
    """Read-only: detected so a sagent file is refused by name, never written."""
    head = json.dumps({"kind": "context_override", "payload": {}}) + "\n"

    offered = cast(
        tuple[str, ...],
        inspect.signature(main).parameters["formats"].default,
    )

    assert detect_format(head) == "sagent"
    assert "sagent" not in offered


def test_sagent_is_recognized_by_its_record_kinds() -> None:
    assert sagent.is_sagent(json.dumps(_meta()) + "\n")
    assert sagent.is_sagent(json.dumps(_history("user", text="x")) + "\n")
    assert sagent.is_sagent(
        json.dumps({"kind": "message", "role": "user", "content": "x"}) + "\n",
    )
    assert not sagent.is_sagent(
        json.dumps({"type": "user", "sessionId": "x", "uuid": "u"}) + "\n",
    )
    assert sagent.is_sagent("\n" + json.dumps(_meta()) + "\n")
    assert not sagent.is_sagent("{truncated\n")
    assert not sagent.is_sagent("")


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
