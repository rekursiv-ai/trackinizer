"""Which native record kinds carry meaning, and which only carry bytes.

A byte round-trip cannot catch under-mapping: a native record replays its
captured attributes verbatim, so a kind routed to the wrong semantic type
still reproduces its own bytes exactly. These tests assert the mapping
instead of the bytes.

A kind stays :class:`UncategorizedRecord` when it is one provider's alone --
a type for it would be a shape no other CLI can fill, which is what breaks a
cross-provider handoff. The lists below are that decision, written down: a
kind moving between them is a change to what the IR claims to understand.
"""

from __future__ import annotations

from collections.abc import Iterable
from io import StringIO

from trackinizer.lib.agent.sessions import claude, codex
from trackinizer.lib.agent.types.sessions import SessionRecord, UncategorizedRecord


CODEX_KINDS = (
    '{"type":"session_meta","payload":{"session_id":"s1"}}',
    '{"type":"turn_context","payload":{"cwd":"/w"}}',
    '{"type":"world_state","payload":{"full":true}}',
    '{"type":"compacted","payload":{"message":"m"}}',
    '{"type":"event_msg","payload":{"type":"token_count","info":{}}}',
    '{"type":"event_msg","payload":{"type":"task_started","turn_id":"t"}}',
    '{"type":"event_msg","payload":{"type":"task_complete","turn_id":"t"}}',
    '{"type":"event_msg","payload":{"type":"turn_aborted","turn_id":"t"}}',
    '{"type":"event_msg","payload":{"type":"agent_message","message":"m"}}',
    '{"type":"event_msg","payload":{"type":"agent_reasoning","text":"t"}}',
    '{"type":"event_msg","payload":{"type":"user_message","message":"m"}}',
    '{"type":"event_msg","payload":{"type":"exec_command_end","call_id":"c"}}',
    '{"type":"event_msg","payload":{"type":"patch_apply_end","call_id":"c"}}',
    '{"type":"event_msg","payload":{"type":"web_search_end","call_id":"c"}}',
    '{"type":"event_msg","payload":{"type":"context_compacted"}}',
    '{"type":"event_msg","payload":{"type":"mcp_tool_call_end","call_id":"c"}}',
    (
        '{"type":"event_msg","payload":{"type":"item_completed",'
        '"item":{"type":"CommandExecution","id":"e1","command":["/bin/sh"]}}}'
    ),
    (
        '{"type":"event_msg","payload":{"type":"item_completed",'
        '"item":{"type":"FileChange","id":"e2","changes":{}}}}'
    ),
    (
        '{"type":"event_msg","payload":{"type":"item_completed",'
        '"item":{"type":"WebSearch","id":"e3","query":"q"}}}'
    ),
    (
        '{"type":"event_msg","payload":{"type":"item_completed",'
        '"item":{"type":"AgentMessage","content":[{"type":"Text","text":"t"}]}}}'
    ),
    (
        '{"type":"event_msg","payload":{"type":"item_completed",'
        '"item":{"type":"Reasoning","id":"r1","summary_text":["s"]}}}'
    ),
    (
        '{"type":"event_msg","payload":{"type":"item_completed",'
        '"item":{"type":"UserMessage","content":[{"type":"text","text":"t"}]}}}'
    ),
    '{"type":"event_msg","payload":{"type":"error","message":"m"}}',
    '{"type":"inter_agent_communication_metadata","payload":{"trigger_turn":1}}',
    (
        '{"type":"response_item","payload":{"type":"message","role":"user",'
        '"content":[{"type":"input_text","text":"t"}]}}'
    ),
    (
        '{"type":"response_item","payload":{"type":"message","role":"assistant",'
        '"content":[{"type":"output_text","text":"t"}]}}'
    ),
    (
        '{"type":"response_item","payload":{"type":"message","role":"developer",'
        '"content":[{"type":"input_text","text":"t"}]}}'
    ),
    '{"type":"response_item","payload":{"type":"reasoning","summary":[]}}',
    (
        '{"type":"response_item","payload":{"type":"function_call","call_id":"c",'
        '"name":"n","arguments":"{}"}}'
    ),
    (
        '{"type":"response_item","payload":{"type":"function_call_output",'
        '"call_id":"c","output":"o"}}'
    ),
    (
        '{"type":"response_item","payload":{"type":"custom_tool_call",'
        '"call_id":"c","name":"n","input":"i"}}'
    ),
    (
        '{"type":"response_item","payload":{"type":"custom_tool_call_output",'
        '"call_id":"c","output":"o"}}'
    ),
    '{"type":"response_item","payload":{"type":"agent_message","content":"c"}}',
    (
        '{"type":"response_item","payload":{"type":"web_search_call","id":"w",'
        '"action":{"type":"search","query":"q"}}}'
    ),
    (
        '{"type":"response_item","payload":{"type":"tool_search_call",'
        '"call_id":"c","arguments":"{}"}}'
    ),
    (
        '{"type":"response_item","payload":{"type":"tool_search_output",'
        '"call_id":"c","tools":[]}}'
    ),
)
"""Every ``type/payload.type`` observed across the captured Codex corpus."""

CLAUDE_KINDS = (
    '{"type":"user","uuid":"u","message":{"role":"user","content":"t"}}',
    (
        '{"type":"assistant","uuid":"a","message":{"role":"assistant",'
        '"content":[{"type":"text","text":"t"}]}}'
    ),
    (
        '{"type":"assistant","uuid":"a","message":{"role":"assistant",'
        '"content":[{"type":"thinking","thinking":"t","signature":"s"}]}}'
    ),
    (
        '{"type":"user","uuid":"u","message":{"role":"user",'
        '"content":[{"type":"tool_result","tool_use_id":"c","content":"o"}]}}'
    ),
    (
        '{"type":"assistant","uuid":"a","message":{"role":"assistant",'
        '"content":[{"type":"tool_use","id":"c","name":"Read","input":{}}]}}'
    ),
    '{"type":"system","content":"c"}',
    '{"type":"queue-operation","operation":"enqueue"}',
    '{"type":"attachment","attachment":{}}',
    '{"type":"last-prompt","lastPrompt":"p"}',
    '{"type":"mode","mode":"normal"}',
    '{"type":"permission-mode","permissionMode":"plan"}',
    '{"type":"ai-title","aiTitle":"t"}',
    '{"type":"agent-name","agentName":"n"}',
    '{"type":"atis-latch","atis":"","sessionId":"s"}',
    '{"type":"bridge-session","sessionId":"s","bridgeSessionId":"b"}',
    '{"type":"file-history-snapshot","messageId":"m"}',
    '{"type":"file-history-delta","messageId":"m"}',
    '{"type":"relocated","relocatedCwd":"/w"}',
    '{"type":"worktree-state","worktreeSession":"w"}',
    '{"type":"started","agentId":"a"}',
    '{"type":"result","agentId":"a"}',
)
"""Every record ``type`` observed across the captured Claude corpus."""


def _unmapped(records: Iterable[SessionRecord]) -> list[str]:
    """Return the kinds that reached the uncategorized fallback."""
    return [r.kind for r in records if isinstance(r, UncategorizedRecord)]


CODEX_UNCATEGORIZED = (
    # Turn boundaries: codex marks them, claude does not, so a record type
    # would be one only one provider ever fills.
    "event_msg/task_started",
    "event_msg/task_complete",
    "event_msg/turn_aborted",
    # Echoes: the CLI re-rendering a record that also arrives as a
    # ``response_item``. Reading one would put a second copy of a turn in the
    # session.
    "event_msg/agent_message",
    "event_msg/agent_reasoning",
    "event_msg/user_message",
    "event_msg/item_completed/AgentMessage",
    "event_msg/item_completed/Reasoning",
    "event_msg/item_completed/UserMessage",
    "inter_agent_communication_metadata/root",
)
"""Codex kinds deliberately left uncategorized, and why."""

CLAUDE_UNCATEGORIZED = (
    # Claude's own bookkeeping: its identity lines, undo checkpoints, and
    # input queue. Each is claude's alone.
    "queue-operation",
    "last-prompt",
    "mode",
    "permission-mode",
    "ai-title",
    "agent-name",
    "atis-latch",
    "bridge-session",
    "file-history-snapshot",
    "file-history-delta",
    "relocated",
    "worktree-state",
    "started",
    "result",
)
"""Claude kinds deliberately left uncategorized, and why."""


def test_codex_maps_every_kind_it_shares_with_another_provider() -> None:
    records = list(codex.normalize(StringIO("\n".join(CODEX_KINDS) + "\n")))

    assert _unmapped(records) == list(CODEX_UNCATEGORIZED)


def test_claude_maps_every_kind_it_shares_with_another_provider() -> None:
    records = list(claude.normalize(StringIO("\n".join(CLAUDE_KINDS) + "\n")))

    assert _unmapped(records) == list(CLAUDE_UNCATEGORIZED)


def test_a_kind_seen_only_in_grep_is_unmapped_not_invented() -> None:
    # These nine appear in no captured session. A type modelled from a grep
    # hit alone is a guessed shape that a round-trip cannot falsify, so they
    # stay unmapped until a capture shows what they carry.
    unverified = (
        "sub_agent_activity",
        "collab_agent_spawn_end",
        "collab_waiting_end",
        "guardian_assessment",
        "entered_review_mode",
        "exited_review_mode",
        "thread_settings_applied",
        "thread_name_updated",
        "thread_rolled_back",
    )
    native = "".join(
        '{"type":"event_msg","payload":{"type":"' + kind + '"}}\n'
        for kind in unverified
    )

    records = list(codex.normalize(StringIO(native)))

    assert _unmapped(records) == [f"event_msg/{kind}" for kind in unverified]


def test_an_unrecognized_content_block_is_kept_not_emptied() -> None:
    # A turn whose only block is one nothing maps keeps the block whole:
    # reading it as an empty message would silently discard what it carried.
    native = (
        '{"type":"assistant","uuid":"a","message":{"role":"assistant",'
        '"content":[{"type":"future_block","x":1}]}}\n'
    )

    records = list(claude.normalize(StringIO(native)))

    out = StringIO()
    claude.denormalize(records, out)
    assert out.getvalue() == native


def test_an_unrecognized_kind_is_named_not_absorbed() -> None:
    # The guard on this whole file: a kind nobody mapped must be visible as
    # UncategorizedRecord, never quietly shaped into a plausible neighbour.
    codex_records = list(
        codex.normalize(
            StringIO('{"type":"event_msg","payload":{"type":"brand_new_thing"}}\n')
        )
    )
    claude_records = list(claude.normalize(StringIO('{"type":"brand-new-thing"}\n')))

    assert _unmapped(codex_records) == ["event_msg/brand_new_thing"]
    assert _unmapped(claude_records) == ["brand-new-thing"]


def test_an_unrecognized_completed_item_is_named_by_its_inner_type() -> None:
    # ``item_completed`` dispatches on ``item.type``, so an unmapped item must
    # name that inner type: reporting the outer kind alone would say
    # ``item_completed`` is unmapped when five of its six shapes are handled.
    records = list(
        codex.normalize(
            StringIO(
                '{"type":"event_msg","payload":{"type":"item_completed",'
                '"item":{"type":"FutureThing"}}}\n'
            )
        )
    )

    assert _unmapped(records) == ["event_msg/item_completed/FutureThing"]


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
