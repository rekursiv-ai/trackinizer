"""Tests for :mod:`trackinizer.server.store.legacy_retype`.

Fixture payloads are built to the shapes ``schema.020.sql`` actually wrote:
``jsonb_build_object('py/object', ..., 'kind', 'legacy/<K>', 'payload',
message - 'thinking_encrypted' - 'thinking_signature')`` where ``message``
is the ``DataclassCodec`` JSON of the pre-drop ``Message`` union (git
``6ced6c9^:trackinizer/types/agent_session_events.py``). Field names below
are that union's, not the IR's: old ``text`` vs IR ``content``, old
``ToolCall.id`` vs IR ``call_id``.
"""

from __future__ import annotations

import pytest

from trackinizer.lib.agent.types.sessions import (
    AgentToAgentMessage,
    AssistantMessage,
    ContextCompaction,
    IncompleteRecord,
    SystemMessage,
    Thinking,
    TokenUsage,
    ToolCall,
    UncategorizedRecord,
    UncategorizedToolResult,
    UserMessage,
)
from trackinizer.lib.custom_json import json_freeze
from trackinizer.server.store.legacy_retype import (
    LEGACY_KINDS,
    retype,
)


STAMP = "2026-01-02T03:04:05+00:00"


# ``timestamp=None`` matches the real rows: 020 wrote ``'timestamp', NULL`` into every
# payload; the value lives on the row column, which the runner passes to ``retype``
# separately.
def _legacy(kind: str, payload: dict[str, object]) -> UncategorizedRecord:
    """Build an UncategorizedRecord shaped as schema.020.sql stored it."""
    return UncategorizedRecord(
        context_id=0,
        timestamp=None,
        kind=kind,
        payload=json_freeze({"__type__": kind.removeprefix("legacy/"), **payload}),
    )


class TestUserMessage:
    def test_maps_text_to_content(self) -> None:
        out = retype(
            _legacy("legacy/UserMessage", {"text": "fix the bug"}),
            timestamp=STAMP,
        )
        assert len(out.records) == 1
        record = out.records[0]
        assert isinstance(record, UserMessage)
        assert record.content == "fix the bug"
        # The payload timestamp is NULL on every 020 row; the ROW column value
        # passed by the runner is what must land on the typed record.
        assert record.timestamp == STAMP

    def test_empty_text_maps_to_empty_content(self) -> None:
        out = retype(_legacy("legacy/UserMessage", {"text": ""}))
        record = out.records[0]
        assert isinstance(record, UserMessage)
        # Old union: empty string was a value (axiom 2: None is unset).
        assert record.content == ""


class TestAgentSendMessage:
    def test_maps_to_agent_to_agent_with_sender(self) -> None:
        out = retype(
            _legacy(
                "legacy/AgentSendMessage",
                {"text": "child report", "source": "worker-3"},
            ),
        )
        assert len(out.records) == 1
        record = out.records[0]
        assert isinstance(record, AgentToAgentMessage)
        assert record.content == "child report"
        assert record.sender == "worker-3"


class TestSystemMessage:
    def test_maps_text_and_preserves_role(self) -> None:
        out = retype(
            _legacy(
                "legacy/SystemMessage",
                {"text": "sandbox on", "role": "developer"},
            ),
        )
        record = out.records[0]
        assert isinstance(record, SystemMessage)
        assert record.content == "sandbox on"
        # ``role`` has no IR field; it must survive in extra (axiom 10).
        assert record.extra.get("role") == "developer"

    def test_default_role_is_not_stored(self) -> None:
        # "system" was the old union's default; storing it would present
        # noise as provenance.
        out = retype(_legacy("legacy/SystemMessage", {"text": "x", "role": "system"}))
        record = out.records[0]
        assert isinstance(record, SystemMessage)
        assert "role" not in record.extra


class TestAssistantMessageFanOut:
    """One legacy assistant turn becomes one record per act, in act order."""

    def test_text_only_yields_one_assistant_message(self) -> None:
        out = retype(_legacy("legacy/AssistantMessage", {"text": "done."}))
        kinds = [type(r).__name__ for r in out.records]
        assert kinds == ["AssistantMessage"]
        record = out.records[0]
        assert isinstance(record, AssistantMessage)
        assert record.content == "done."

    def test_full_turn_fans_out_in_act_order(self) -> None:
        out = retype(
            _legacy(
                "legacy/AssistantMessage",
                {
                    "text": "running it",
                    "thinking": "I should test first",
                    "tool_calls": [
                        {"id": "call_1", "name": "Bash", "args": {"cmd": "pytest"}},
                        {"id": "call_2", "name": "Read", "args": {"path": "x.py"}},
                    ],
                    "tokens": {"input": 100, "output": 50},
                },
            ),
        )
        kinds = [type(r).__name__ for r in out.records]
        assert kinds == [
            "AssistantMessage",
            "Thinking",
            "ToolCall",
            "ToolCall",
            "TokenUsage",
        ]
        first_call = out.records[2]
        assert isinstance(first_call, ToolCall)
        assert first_call.call_id == "call_1"
        assert first_call.name == "Bash"
        assert first_call.arguments == {"cmd": "pytest"}

    def test_empty_thinking_produces_no_thinking_record(self) -> None:
        out = retype(_legacy("legacy/AssistantMessage", {"text": "hi", "thinking": ""}))
        assert not any(isinstance(r, Thinking) for r in out.records)

    def test_ciphertext_lands_on_thinking_encrypted(self) -> None:
        out = retype(
            _legacy(
                "legacy/AssistantMessage",
                {"text": "x", "thinking": "summary text"},
            ),
            ciphertext="SEALEDBYTES",
        )
        thinking = next(r for r in out.records if isinstance(r, Thinking))
        assert thinking.encrypted == "SEALEDBYTES"
        assert thinking.content == "summary text"

    def test_ciphertext_without_thinking_text_still_yields_record(self) -> None:
        # Claude sealed turns carried ONLY encrypted+signature; the retype
        # must not drop the bytes because the plaintext was empty.
        out = retype(
            _legacy("legacy/AssistantMessage", {"text": "x"}),
            ciphertext="SEALEDONLY",
        )
        thinking = next(r for r in out.records if isinstance(r, Thinking))
        assert thinking.encrypted == "SEALEDONLY"

    def test_every_record_shares_the_row_timestamp(self) -> None:
        out = retype(
            _legacy(
                "legacy/AssistantMessage",
                {"text": "a", "thinking": "b", "tokens": {"input": 1}},
            ),
            timestamp=STAMP,
        )
        assert len(out.records) == 3
        stamps = {
            r.timestamp for r in out.records if not isinstance(r, IncompleteRecord)
        }
        assert stamps == {STAMP}

    def test_token_usage_carries_counts(self) -> None:
        out = retype(
            _legacy(
                "legacy/AssistantMessage",
                {"text": "a", "tokens": {"input": 7, "output": 3}},
            ),
        )
        usage = next(r for r in out.records if isinstance(r, TokenUsage))
        assert usage.info.get("input") == 7
        assert usage.info.get("output") == 3

    def test_all_zero_tokens_produce_no_token_usage(self) -> None:
        # The old union defaulted ``tokens`` to an all-zero TokenCount, so 020
        # payloads carry a zeros object even for turns that were never billed.
        # Zeros are the default's noise, not an act: no record is fabricated.
        out = retype(
            _legacy(
                "legacy/AssistantMessage",
                {"text": "a", "tokens": {"input": 0, "output": 0}},
            ),
        )
        assert [type(r).__name__ for r in out.records] == ["AssistantMessage"]


class TestToolResult:
    def test_maps_to_uncategorized_tool_result(self) -> None:
        out = retype(
            _legacy(
                "legacy/ToolResult",
                {
                    "call_id": "call_9",
                    "content": "exit 1: ModuleNotFoundError",
                    "is_error": True,
                    "diff": "",
                    "diff_file_path": "",
                    "summary": "",
                },
            ),
        )
        assert len(out.records) == 1
        record = out.records[0]
        assert isinstance(record, UncategorizedToolResult)
        assert record.call_id == "call_9"
        assert record.content == "exit 1: ModuleNotFoundError"
        assert record.extra.get("is_error") is True

    def test_diff_fields_survive_in_extra(self) -> None:
        out = retype(
            _legacy(
                "legacy/ToolResult",
                {
                    "call_id": "c",
                    "content": "ok",
                    "diff": "-a\n+b",
                    "diff_file_path": "f.py",
                    "summary": "edited f.py",
                },
            ),
        )
        record = out.records[0]
        assert isinstance(record, UncategorizedToolResult)
        assert record.extra.get("diff") == "-a\n+b"
        assert record.extra.get("diff_file_path") == "f.py"
        assert record.extra.get("summary") == "edited f.py"

    def test_falsey_receipt_fields_are_not_stored(self) -> None:
        out = retype(
            _legacy(
                "legacy/ToolResult",
                {"call_id": "c", "content": "ok", "is_error": False},
            ),
        )
        record = out.records[0]
        assert isinstance(record, UncategorizedToolResult)
        # Old union defaults (False, "") are noise, not provenance.
        assert "is_error" not in record.extra
        assert "diff" not in record.extra


class TestCompaction:
    def test_maps_text_to_summary(self) -> None:
        out = retype(
            _legacy(
                "legacy/Compaction",
                {
                    "text": "The session so far: ...",
                    "token_before": 180_000,
                    "token_after": 20_000,
                },
            ),
        )
        record = out.records[0]
        assert isinstance(record, ContextCompaction)
        assert record.summary == "The session so far: ..."
        assert record.extra.get("token_before") == 180_000
        assert record.extra.get("token_after") == 20_000


class TestSlashCommand:
    def test_routes_out_of_the_record_stream(self) -> None:
        out = retype(
            _legacy("legacy/SlashCommand", {"command": "model", "args": "gpt-5"}),
            timestamp=STAMP,
        )
        assert out.records == ()
        assert out.slash is not None
        assert out.slash.command == "model"
        assert out.slash.args == "gpt-5"
        assert out.slash.timestamp == STAMP


class TestAttachments:
    def test_nonempty_attachments_raise_rather_than_guess(self) -> None:
        # Measured live (2026-09-19): zero legacy rows carry attachments, so
        # the codec shape is unverified; an encounter must surface loudly.
        with pytest.raises(ValueError, match="attachment"):
            retype(
                _legacy(
                    "legacy/UserMessage",
                    {"text": "x", "attachments": [{"data": "AAAA"}]},
                ),
            )


class TestContract:
    def test_unknown_message_is_not_a_member(self) -> None:
        assert "legacy/UnknownMessage" not in LEGACY_KINDS

    def test_non_legacy_kind_raises(self) -> None:
        record = UncategorizedRecord(kind="queue-operation", payload=json_freeze({}))
        with pytest.raises(ValueError, match="queue-operation"):
            retype(record)

    def test_ciphertext_with_non_assistant_kind_raises(self) -> None:
        record = _legacy("legacy/UserMessage", {"text": "hi"})
        with pytest.raises(ValueError, match="ciphertext"):
            retype(record, ciphertext="SEALED")

    @pytest.mark.parametrize("kind", sorted(LEGACY_KINDS))
    def test_every_member_maps_without_error(self, kind: str) -> None:
        out = retype(_legacy(kind, {"call_id": "c"} if "ToolResult" in kind else {}))
        assert out.records or out.slash is not None


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
