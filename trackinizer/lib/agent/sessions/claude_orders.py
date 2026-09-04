"""The key order Claude Code writes each record type's keys in.

Claude repeats a session-wide envelope on every line and orders each record
type's keys the same way every time, so the order is the FORMAT's rather than
any one line's -- a table here, not a copy stored on every record.

Measured across captured sessions; a type whose order varies stores its own
(``system`` writes 5 distinct orders across 31 lines) and this is only the
fallback.
"""

from __future__ import annotations

from collections.abc import Mapping


__all__ = [
    "assistant_order",
    "attachment_order",
    "envelope_keys",
    "failed_order",
    "message_consumed",
    "payload_orders",
    "settings_keys",
    "system_order",
    "user_order",
]


# Repeated verbatim on every line, so it is the turn's rather than the
# record's. Order is claude's.
def envelope_keys() -> tuple[str, ...]:
    """Return the keys claude repeats verbatim on every line.

    Repeated per line, so they are the TURN's rather than the record's. Order
    is claude's own.

    Returns:
      keys: Envelope key names, in the order claude writes them.

    """
    return (
        "userType",
        "entrypoint",
        "cwd",
        "sessionId",
        "version",
        "gitBranch",
    )


# Settings claude states per line that never vary within a session.
def settings_keys() -> tuple[str, ...]:
    """Return the settings claude states per line that never vary in a session.

    Returns:
      keys: Setting key names.

    """
    return ("isSidechain", "agentId", "mode")


# The order claude writes each record type's keys in, up to the envelope every
# type ends with. A key whose value no field holds is read from ``extra``.
def user_order() -> tuple[str, ...]:
    """Return the order claude writes a ``user`` line's keys in.

    Returns:
      order: Key names, up to the envelope every type ends with.

    """
    return (
        "parentUuid",
        "isSidechain",
        "promptId",
        "agentId",
        "type",
        "message",
        "isMeta",
        # A compaction boundary: claude carries the summary of the file it
        # replaced as a user turn, and this pair is how the new file marks it.
        "isVisibleInTranscriptOnly",
        "isCompactSummary",
        "uuid",
        "timestamp",
        "permissionMode",
        "origin",
        "promptSource",
        "sourceToolUseID",
        "toolEndsTurn",
        "interruptedByShutdown",
        "interruptedMessageId",
        "toolUseResult",
        "toolDenialKind",
        "userFeedback",
        "sourceToolAssistantUUID",
        "session_id",
    )


def assistant_order() -> tuple[str, ...]:
    """Return the order claude writes an ``assistant`` line's keys in.

    Returns:
      order: Key names, up to the envelope every type ends with.

    """
    return (
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
        "isAbortedMidStream",
        "effort",
        "error",
        "errorDetails",
        "apiErrorStatus",
        "quotaLimits",
        "isApiErrorMessage",
        "healsDistinctCarrier",
        "session_id",
    )


def attachment_order() -> tuple[str, ...]:
    """Return the order claude writes an ``attachment`` line's keys in.

    Returns:
      order: Key names, up to the envelope every type ends with.

    """
    return (
        "parentUuid",
        "isSidechain",
        "agentId",
        "attachment",
        "type",
        "uuid",
        "timestamp",
        "session_id",
    )


# A ``system`` line is the one type whose order is NOT fixed: 5 distinct
# orders across 31 captured lines, differing in where ``content`` and
# ``level`` sit. So it is the one type that stores its own.
def system_order() -> tuple[str, ...]:
    """Return the order claude writes a ``system`` line's keys in.

    The one type whose order is NOT fixed: 5 distinct orders across 31
    captured lines, differing in where ``content`` and ``level`` sit -- so a
    line stores its own and this is only the fallback.

    Returns:
      order: Key names, up to the envelope every type ends with.

    """
    return (
        "parentUuid",
        "isSidechain",
        "type",
        "subtype",
        "content",
        "timestamp",
        "uuid",
        "isMeta",
        "session_id",
    )


# Keys a record type's own fields hold, so the residual must not repeat them.
def message_consumed() -> frozenset[str]:
    """Return the message keys a record's own fields hold.

    The residual must not repeat these.

    Returns:
      keys: Key names the fields carry.

    """
    return frozenset({"role", "content"})


# The order each tool writes its own return value's keys in. Measured fixed
# per tool across 1692 captured results; a key not listed keeps source order.
def payload_orders() -> Mapping[str, tuple[str, ...]]:
    """Return the order each tool writes its own return value's keys in.

    Measured fixed per tool across 1692 captured results; a key not listed
    keeps source order.

    Returns:
      orders: Key order per tool name.

    """
    return {
        "Bash": (
            "stdout",
            "stderr",
            "interrupted",
            "isImage",
            "returnCodeInterpretation",
            "noOutputExpected",
        ),
        "Read": ("type", "file"),
        # ``Read`` nests its payload one level down, so the nested object has its
        # own fixed order too.
        "Read.file": ("filePath", "content", "numLines", "startLine", "totalLines"),
        "Write": (
            "type",
            "filePath",
            "content",
            "structuredPatch",
            "originalFile",
            "userModified",
        ),
        "Edit": (
            "filePath",
            "oldString",
            "newString",
            "originalFile",
            "structuredPatch",
            "userModified",
            "replaceAll",
        ),
        "WebSearch": ("query", "results", "durationSeconds", "searchCount"),
        "WebFetch": ("bytes", "code", "codeText", "result", "durationMs", "url"),
        # A subagent answers in one of two shapes, and they order the same keys
        # differently: a launch states its description before the model, a
        # finished report states its prompt second. ``isAsync`` picks.
        "Agent": (
            "isAsync",
            "status",
            "agentId",
            "description",
            "resolvedModel",
            "prompt",
            "outputFile",
            "canReadOutputFile",
        ),
        "Agent.done": (
            "status",
            "prompt",
            "agentId",
            "agentType",
            "content",
            "resolvedModel",
            "totalDurationMs",
            "totalTokens",
            "totalToolUseCount",
        ),
    }


# The order claude writes an API-failure line's keys in. Distinct enough from
# a served line to be its own table: the message object moves after the
# timestamp and leads with the provider's diagnostics.
def failed_order() -> tuple[str, ...]:
    """Return the order claude writes an API-failure line's keys in.

    Distinct enough from a served line to be its own table: the message object
    moves after the timestamp and leads with the provider's diagnostics.

    Returns:
      order: Key names, up to the envelope every type ends with.

    """
    return (
        "parentUuid",
        "isSidechain",
        "type",
        "uuid",
        "timestamp",
        "message",
        "requestId",
        "quotaLimits",
        "error",
        "errorDetails",
        "isApiErrorMessage",
        "apiErrorStatus",
        "healsDistinctCarrier",
        "session_id",
    )
