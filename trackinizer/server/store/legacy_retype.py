"""Retype ``legacy/*`` :class:`UncategorizedRecord` rows into typed IR records.

``schema.020.sql`` backfilled every ``agent_session_events`` row as an
:class:`UncategorizedRecord` whose ``kind`` field preserves the old
discriminator as ``legacy/<Kind>``. The old ``Message`` union carried every
field its typed IR successor needs (verified against the pre-drop
``types/agent_session_events.py`` and the live payload keys), so those rows
are mechanically re-typable -- the migration comment's "the union it came
from is lossy" does not hold for seven of its eight kinds
(``legacy/UnknownMessage`` stays uncategorized; see :data:`LEGACY_KINDS`).

This module is the PURE half: one legacy record in, its typed records out.
The sharded runner that rewrites ``part = -1`` streams lives with the store;
keeping the mapping side-effect free is what lets the tests pin every
mapping without a database.

Fan-out: the old ``AssistantMessage`` nested text, thinking, and tool calls
in one message; the IR states each act as its own record (axiom 3 of
``trackinizer/lib/agent/types/sessions.py``). One legacy record therefore becomes
one OR SEVERAL records, in the order the acts occurred: prose, thinking,
tool calls, token usage.

Ciphertext: 020 stripped ``thinking_encrypted``/``thinking_signature`` from
the stored payload and wrote their CONCATENATION to ``session_ciphertext``
(``coalesce(enc,'')||coalesce(sig,'')``). The concatenation is not
splittable, so the retyped :class:`Thinking` record carries the blob
verbatim in ``encrypted`` -- bytes preserved, no structure fabricated.
``SessionRecordRow.of`` then re-splits it to the ciphertext table under the
Thinking record's own idx, which is the live convention.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from trackinizer.lib.agent.types.sessions import (
    AgentToAgentMessage,
    AssistantMessage,
    Attachment,
    ContextCompaction,
    SessionRecord,
    SystemMessage,
    Thinking,
    TokenUsage,
    ToolCall,
    UncategorizedRecord,
    UncategorizedToolResult,
    UserMessage,
)
from trackinizer.lib.custom_json import (
    BoolCodec,
    DictCodec,
    IntCodec,
    ListCodec,
    StrCodec,
    json_freeze,
    json_unfreeze,
)


if TYPE_CHECKING:
    from collections.abc import Mapping


__all__ = [
    "LEGACY_KINDS",
    "Retyped",
    "SlashCommandOut",
    "retype",
]


LEGACY_KINDS: frozenset[str] = frozenset(
    {
        "legacy/UserMessage",
        "legacy/AgentSendMessage",
        "legacy/SystemMessage",
        "legacy/AssistantMessage",
        "legacy/ToolResult",
        "legacy/Compaction",
        "legacy/SlashCommand",
    },
)
"""The ``UncategorizedRecord.kind`` values :func:`retype` maps.

``legacy/UnknownMessage`` is deliberately absent: it wrapped a record the
ORIGINAL adapter could not type, so it stays an ``UncategorizedRecord`` --
retyping it would claim structure the capture never had.
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class SlashCommandOut:
    """A ``legacy/SlashCommand`` mapped out of the record stream.

    Slash commands are not session records in the live schema -- they live in
    ``session_slash_commands`` (see ``session_ir.py::_append_slash_commands``:
    "a command sits BETWEEN the turns around it"). The runner routes this to
    that table instead of the record stream.
    """

    timestamp: str | None = None
    command: str = ""
    args: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class Retyped:
    """The typed output of one legacy record.

    Attributes:
      records: The IR records the legacy record becomes, in act order.
        Empty when the record maps entirely out of the stream (a
        ``legacy/SlashCommand``).
      slash: The slash-command row, for the one kind that produces one.

    """

    records: tuple[SessionRecord, ...] = ()
    slash: SlashCommandOut | None = None


def retype(
    record: UncategorizedRecord,
    *,
    timestamp: str | None = None,
    ciphertext: str = "",
) -> Retyped:
    """Map one ``legacy/*`` record to its typed IR records.

    Args:
      record: An :class:`UncategorizedRecord` whose ``kind`` is a member of
        :data:`LEGACY_KINDS`.
      timestamp: The ROW's timestamp, ISO-8601. 020 wrote ``'timestamp',
        NULL`` inside every payload (``schema.020.sql:68``) -- the real value
        survives only on the ``session_records.timestamp`` column, so the
        runner must pass it; ``record.timestamp`` is ``None`` on every
        migrated row and is deliberately not consulted.
      ciphertext: The ``session_ciphertext`` bytes 020 parked under this
        record's key, decoded as UTF-8. Empty when none exist. Only a
        ``legacy/AssistantMessage`` consumes it (it becomes the retyped
        ``Thinking.encrypted``); passing it with any other kind is an error,
        because those kinds never had sealed reasoning to strip.

    Returns:
      out: The typed records and/or slash-command row, in act order.

    Raises:
      ValueError: ``record.kind`` is not in :data:`LEGACY_KINDS`, or
        ``ciphertext`` accompanies a kind that cannot carry one.

    """
    if record.kind not in LEGACY_KINDS:
        raise ValueError(f"not a retypable legacy kind: {record.kind!r}")
    if ciphertext and record.kind != "legacy/AssistantMessage":
        raise ValueError(
            f"ciphertext accompanies {record.kind!r}, but only a "
            "legacy/AssistantMessage carried sealed reasoning",
        )
    # Unfrozen before narrowing: ``json_freeze`` maps arrays to TUPLES, and
    # ``ListCodec.coerce`` narrows only ``list`` -- a frozen ``tool_calls``
    # would silently coerce to [] and the calls would vanish. Same asymmetry
    # ``SessionRecordRow.record()`` documents.
    payload = DictCodec.coerce(json_unfreeze(record.payload))
    match record.kind:
        case "legacy/UserMessage":
            return Retyped(
                records=(
                    UserMessage(
                        context_id=record.context_id,
                        timestamp=timestamp,
                        content=StrCodec.coerce(payload.get("text")),
                        attachments=_attachments(payload),
                    ),
                ),
            )
        case "legacy/AgentSendMessage":
            return Retyped(
                records=(
                    AgentToAgentMessage(
                        context_id=record.context_id,
                        timestamp=timestamp,
                        content=StrCodec.coerce(payload.get("text")),
                        attachments=_attachments(payload),
                        sender=StrCodec.coerce(payload.get("source")),
                    ),
                ),
            )
        case "legacy/SystemMessage":
            role = StrCodec.coerce(payload.get("role"))
            return Retyped(
                records=(
                    SystemMessage(
                        context_id=record.context_id,
                        timestamp=timestamp,
                        content=StrCodec.coerce(payload.get("text")),
                        # The old default ("system") is noise; only a wire
                        # role that differed is provenance (axiom 10).
                        extra={"role": role} if role and role != "system" else {},
                    ),
                ),
            )
        case "legacy/AssistantMessage":
            return Retyped(
                records=_assistant_fan_out(
                    record,
                    payload,
                    timestamp=timestamp,
                    ciphertext=ciphertext,
                ),
            )
        case "legacy/ToolResult":
            return Retyped(
                records=(
                    UncategorizedToolResult(
                        context_id=record.context_id,
                        timestamp=timestamp,
                        call_id=StrCodec.coerce(payload.get("call_id")),
                        content=StrCodec.coerce(payload.get("content")),
                        attachments=_attachments(payload),
                        # Only receipt fields the provider actually set: the
                        # old union's defaults (False, "") are noise a reader
                        # would mistake for provenance.
                        extra={
                            key: value
                            for key, value in (
                                ("is_error", BoolCodec.coerce(payload.get("is_error"))),
                                ("diff", StrCodec.coerce(payload.get("diff"))),
                                (
                                    "diff_file_path",
                                    StrCodec.coerce(payload.get("diff_file_path")),
                                ),
                                ("summary", StrCodec.coerce(payload.get("summary"))),
                            )
                            if value
                        },
                    ),
                ),
            )
        case "legacy/Compaction":
            return Retyped(
                records=(
                    ContextCompaction(
                        context_id=record.context_id,
                        timestamp=timestamp,
                        summary=StrCodec.coerce(payload.get("text")),
                        extra={
                            key: value
                            for key, value in (
                                (
                                    "token_before",
                                    IntCodec.coerce(payload.get("token_before")),
                                ),
                                (
                                    "token_after",
                                    IntCodec.coerce(payload.get("token_after")),
                                ),
                                (
                                    "fallback_reason",
                                    StrCodec.coerce(payload.get("fallback_reason")),
                                ),
                            )
                            if value
                        },
                    ),
                ),
            )
        case _:
            return Retyped(
                slash=SlashCommandOut(
                    timestamp=timestamp,
                    command=StrCodec.coerce(payload.get("command")),
                    args=StrCodec.coerce(payload.get("args")),
                ),
            )


def _assistant_fan_out(
    record: UncategorizedRecord,
    payload: Mapping[str, object],
    *,
    timestamp: str | None,
    ciphertext: str,
) -> tuple[SessionRecord, ...]:
    """Split one legacy assistant turn into its acts, in act order."""
    records: list[SessionRecord] = [
        AssistantMessage(
            context_id=record.context_id,
            timestamp=timestamp,
            content=StrCodec.coerce(payload.get("text")),
            attachments=_attachments(payload),
        ),
    ]
    thinking = StrCodec.coerce(payload.get("thinking"))
    if thinking or ciphertext:
        records.append(
            Thinking(
                context_id=record.context_id,
                timestamp=timestamp,
                content=thinking or None,
                encrypted=ciphertext or None,
            ),
        )
    records.extend(
        ToolCall(
            context_id=record.context_id,
            timestamp=timestamp,
            call_id=StrCodec.coerce(call.get("id")),
            name=StrCodec.coerce(call.get("name")),
            arguments=json_freeze(DictCodec.coerce(call.get("args"))),
        )
        for call in ListCodec.mappings(payload.get("tool_calls"))
    )
    tokens = {
        key: count
        for key, value in DictCodec.coerce(payload.get("tokens")).items()
        if (count := IntCodec.coerce(value))
    }
    if tokens:
        records.append(
            TokenUsage(
                context_id=record.context_id,
                timestamp=timestamp,
                info=json_freeze(tokens),
            ),
        )
    return tuple(records)


# Measured on the live corpus (2026-09-19): ZERO legacy rows carry a nonempty
# ``attachments`` array, so this path never runs against real data. It exists
# because the old union declared the field and a defensive drop would be
# silent; if a row with one ever appears, the ValueError below surfaces it
# rather than guessing at the union member's codec shape unverified.
def _attachments(payload: Mapping[str, object]) -> tuple[Attachment, ...]:
    """Rebuild inline attachments; raise on any shape never seen in the wild."""
    items = ListCodec.mappings(payload.get("attachments"))
    if items:
        raise ValueError(
            "legacy attachment encountered; the live corpus carried none and "
            f"the codec shape is unverified: {items[0]!r:.200}",
        )
    return ()
