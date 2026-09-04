"""The key order Codex writes each payload type's keys in.

Measured fixed per type across 58k captured lines: the variants differ only in
which keys are present, never in their relative order -- so the order is the
FORMAT's rather than any one line's, and a reader stores a line's own only
where this table cannot reproduce it.
"""

from __future__ import annotations

from collections.abc import Mapping


__all__ = ["payload_orders"]


def payload_orders() -> Mapping[str, tuple[str, ...]]:
    """Return the key order codex writes each payload type in.

    Measured fixed per type across 58k captured lines: the variants differ
    only in which keys are present, never in their relative order. A reader
    stores a line's own order only where this table cannot reproduce it.

    Returns:
      orders: Key order per payload ``type``.

    """
    return {
        "session_meta": (
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
        "turn_context": (
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
        "message": (
            "type",
            "id",
            "role",
            "content",
            "phase",
            "internal_chat_message_metadata_passthrough",
        ),
        "reasoning": (
            "type",
            "id",
            "summary",
            "content",
            "encrypted_content",
            "internal_chat_message_metadata_passthrough",
        ),
        "function_call": (
            "type",
            "id",
            "name",
            "namespace",
            "arguments",
            "call_id",
            "internal_chat_message_metadata_passthrough",
        ),
        "custom_tool_call": (
            "type",
            "id",
            "status",
            "call_id",
            "name",
            "input",
            "internal_chat_message_metadata_passthrough",
        ),
        "function_call_output": (
            "type",
            "id",
            "call_id",
            "name",
            "output",
            "internal_chat_message_metadata_passthrough",
        ),
        "custom_tool_call_output": (
            "type",
            "id",
            "call_id",
            "name",
            "output",
            "internal_chat_message_metadata_passthrough",
        ),
        "agent_message": (
            "type",
            "id",
            "author",
            "recipient",
            "content",
            "internal_chat_message_metadata_passthrough",
        ),
        "tool_search_call": (
            "type",
            "id",
            "call_id",
            "status",
            "execution",
            "action",
            "arguments",
            "internal_chat_message_metadata_passthrough",
        ),
        "web_search_call": (
            "type",
            "id",
            "status",
            "action",
            "internal_chat_message_metadata_passthrough",
        ),
        "tool_search_output": (
            "type",
            "id",
            "call_id",
            "status",
            "execution",
            "tools",
            "internal_chat_message_metadata_passthrough",
        ),
        "token_count": ("type", "info", "rate_limits"),
        "exec_command_end": (
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
        "patch_apply_end": (
            "type",
            "call_id",
            "turn_id",
            "stdout",
            "stderr",
            "success",
            "changes",
            "status",
        ),
        "web_search_end": ("type", "call_id", "query", "action", "results"),
        "result_row": ("type", "ref_id", "title", "url", "domain", "snippet"),
        "item_completed": (
            "type",
            "thread_id",
            "turn_id",
            "item",
            "started_at_ms",
            "completed_at_ms",
        ),
        "CommandExecution": (
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
        "FileChange": ("type", "id", "changes", "status", "stdout", "stderr"),
        "WebSearch": ("type", "id", "query"),
        "compacted": ("message", "replacement_history"),
        "error": ("type", "message", "codex_error_info"),
    }
