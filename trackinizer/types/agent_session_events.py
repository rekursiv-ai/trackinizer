"""The agent-session event: one captured turn of a ``trax run`` session.

This is the typed contract for the ``agent_session_events`` table -- the
append-only, turn-grained log produced when ``trax run <cli>`` captures an
agent CLI session. It is the source of truth for that table the same way
:mod:`types.inquiries` is for ``inquiries`` and :mod:`types.change_log` is
for ``change_log``: the SQL columns (via :class:`ColumnSpec`), the wire
body, and the Store all derive from the :class:`AgentSessionEvent`
dataclass here.

It is **not** an :class:`~trackinizer.types.inquiries.Inquiry`.
The owning session is the :class:`AgentSession` artifact row in
``inquiries``; these events hang off it by ``session_id`` and carry no
edges, cost, supersession, or ``change_log`` audit -- a captured turn is
not a knowledge mutation (see ``docs/design.md``, "Everything is
provenance").

The row's ``message`` is **not** opaque JSON: it is one :data:`Message`
value type, discriminated by the row's ``kind`` column. The capture
adapters normalize each CLI's native log shape into one of these, so the
same query works across claude, codex, gemini, and cursor. The vocabulary
mirrors the provider-unified model interface in ``sagent/types/runtime.py``.

The message union::

    Message
    +- UserMessage       (human-authored user-role input)
    +- AgentSendMessage  (agent-authored user-role input)
    +- SystemMessage     (system/developer primed context the model saw)
    +- AssistantMessage  (one model turn: text / thinking / tool calls)
    +- ToolResult        (one tool invocation's result)
    +- Compaction        (a context-window compaction)
    +- SlashCommand      (a CLI slash-command the human typed, e.g. /exit)
    +- UnknownMessage    (escape hatch: an unrecognized record, raw-wrapped)

``ToolCall`` is nested inside :class:`AssistantMessage`, never a row of its
own; ``Attachment`` (bytes, a file path, or a URL) rides on the message
types that can carry media. These message/attachment classes are value
types -- like :class:`Cost`, they live inside a row, so they carry no
``ColumnSpec``. Each gains ``to_json`` / ``from_json`` from the shared
:class:`~trackinizer.lib.custom_json.JsonCodec` mixin, round-tripping through the
``message`` JSONB column; :func:`message_for_kind` resolves the row's
``kind`` string back to its class for decode (``{cls.__name__: cls}``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Self, cast
from uuid import UUID, uuid4

from trackinizer.lib.custom_json import JSON, JsonCodec
from trackinizer.types.columns import ColumnSpec, Row
from trackinizer.types.cost import TokenCount


# Value types (attachments, tool calls, token counts, message members) all
# round-trip via the shared, type-hint-driven :class:`JsonCodec` mixin: each
# gains ``to_json`` / ``from_json`` with no per-type code. ``bytes`` (base64),
# ``Path`` / ``UUID`` / ``datetime``, and the ``Attachment`` union (tagged by
# class name) are handled by the codec, not here.


# -- Attachments --------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class BytesAttachment(JsonCodec):
    """A binary attachment held inline: an image or PDF.

    An :data:`Attachment`, not a :data:`Message`: it rides on a message, it is
    never a turn of its own. Named for what it is so the vocabulary stays
    clean.
    """

    data: bytes = b""
    """Raw bytes of the attachment."""

    descriptor: str = ""
    """MIME-style content type, e.g. ``image/png``."""


@dataclass(frozen=True, slots=True, kw_only=True)
class FilePath(JsonCodec):
    """An attachment referenced by path rather than carried inline."""

    path: Path = Path()
    """Path the attachment lives at."""


@dataclass(frozen=True, slots=True, kw_only=True)
class WebUrl(JsonCodec):
    """A web link attached to a turn."""

    url: str = ""
    """The link target."""


type Attachment = BytesAttachment | FilePath | WebUrl
"""Media a message can carry: inline bytes, a file path, or a URL."""


# -- Messages (the typed message union) ---------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolCall(JsonCodec):
    """One tool invocation the model requested, nested in an assistant turn.

    Never a row of its own: a model turn can request several tools at once,
    so the calls live as a list on :attr:`AssistantMessage.tool_calls`. The
    matching output arrives later as a separate :class:`ToolResult` keyed by
    :attr:`id`.
    """

    id: str = ""
    """Provider-assigned call id (claude ``toolu_01...``, codex ``call_id``)."""

    name: str = ""
    """The tool the model wants to invoke."""

    args: dict[str, object] = field(default_factory=dict)
    """Parsed call arguments (codex's JSON-encoded string is decoded here)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class UserMessage(JsonCodec):
    """Human-authored user-role input the model saw."""

    text: str = ""
    """Plain-text content."""

    attachments: tuple[Attachment, ...] = ()
    """Images / PDFs / files / links sent alongside the text."""


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentSendMessage(JsonCodec):
    """Agent-authored user-role input the model saw.

    The same shape as :class:`UserMessage`, plus :attr:`source` -- the label
    of the agent that produced it -- so a multi-agent transcript can be
    attributed. Kept distinct from :class:`UserMessage` because human vs
    agent authorship is real provenance, not a flag.
    """

    text: str = ""
    """Plain-text content."""

    attachments: tuple[Attachment, ...] = ()
    """Images / PDFs / files / links sent alongside the text."""

    source: str = ""
    """The agent label that authored this input."""


@dataclass(frozen=True, slots=True, kw_only=True)
class SystemMessage(JsonCodec):
    """System / developer context the model saw but the human did not type.

    A CLI primes the model with provider-injected context -- a permissions or
    sandbox preamble, a developer system prompt -- that its own UI never shows
    the user. Captured for fidelity (it is part of what the model saw) but
    kept distinct from :class:`AssistantMessage`: a ``developer``-role record
    is not a model reply. :attr:`role` preserves the wire role
    (``system`` / ``developer``) so a viewer can label or hide it.
    """

    text: str = ""
    """Plain-text content."""

    role: str = "system"
    """The provider wire role this primed context came in as
    (``system`` / ``developer``)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class AssistantMessage(JsonCodec):
    """One model turn: text and/or thinking and/or tool calls, together.

    A single turn -- not split per modality. The user-visible reply, the
    chain-of-thought, and any tool calls the model fired all belong to the
    same turn, mirroring ``sagent``'s ``AssistantMessage``. This is the only
    message that carries :attr:`tokens`, because it is the billed model call;
    USD cost is inferred from the counts, not stored.
    """

    text: str = ""
    """User-visible response text."""

    thinking: str = ""
    """Plaintext chain-of-thought (claude ``thinking`` block, codex
    ``reasoning.summary``). Empty when the model exposed no readable
    reasoning."""

    thinking_signature: str = ""
    """Claude's opaque signature over the thinking block; empty otherwise."""

    thinking_encrypted: str = ""
    """Codex's encrypted raw CoT (``encrypted_content``); server-decryptable
    only. Coexists with the plaintext :attr:`thinking` summary; empty
    otherwise."""

    tool_calls: tuple[ToolCall, ...] = ()
    """Tool invocations the model requested this turn; possibly several."""

    tokens: TokenCount = field(default_factory=TokenCount)
    """Token usage for this model call. USD cost is not stored: it is
    inferred from these counts and the model's pricing."""

    def __post_init__(self) -> None:
        # A duplicate ``ToolCall.id`` corrupts call<->result pairing: the
        # later ``ToolResult.call_id`` can no longer name one call. Reject
        # at construction so a malformed turn fails at the boundary rather
        # than silently mis-joining downstream (mirrors sagent).
        seen: set[str] = set()
        for tc in self.tool_calls:
            if tc.id in seen:
                raise ValueError(f"duplicate ToolCall id: {tc.id!r}")
            seen.add(tc.id)


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolResult(JsonCodec):
    """The result of one tool invocation, echoed back to the model."""

    call_id: str = ""
    """The :attr:`ToolCall.id` this is the result for."""

    content: str = ""
    """Result text shown to the model."""

    is_error: bool = False
    """True when the tool raised or signalled failure."""

    attachments: tuple[Attachment, ...] = ()
    """Images / PDFs / files / links the tool produced."""

    diff: str = ""
    """Unified-diff fragment for renderers (e.g. Edit / Write tools)."""

    diff_file_path: str = ""
    """Path the :attr:`diff` applies to."""

    summary: str = ""
    """Optional short post-execution receipt line."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Compaction(JsonCodec):
    """A context-window compaction event.

    Compaction runs its own summarizing model call, so it carries
    before/after context sizes (:attr:`token_before` / :attr:`token_after`)
    rather than an :class:`AssistantMessage`'s :class:`TokenCount` usage --
    the two are different measurements and deliberately do not unify.
    """

    text: str = ""
    """The compaction summary the runtime substituted for prior history."""

    token_before: int = 0
    """Context token count before compaction."""

    token_after: int = 0
    """Context token count after compaction."""

    fallback_reason: str = ""
    """Empty when a summary was produced; else why fallback history was used."""


@dataclass(frozen=True, slots=True, kw_only=True)
class SlashCommand(JsonCodec):
    """A CLI slash-command the human typed into the TUI (``/exit``, ``/model``).

    Handled inside the CLI -- it changes CLI state, not model context -- so the
    model never sees it and it is absent from the rollout/session log. The pump
    captures it by observing the human's keystrokes on the PTY (the only place
    it is visible), making it a distinct provenance class: not
    :class:`UserMessage` (the model never saw it) nor :class:`SystemMessage`
    (the human, not the provider, authored it).
    """

    command: str = ""
    """The command verb without its leading slash (``exit``, ``model``)."""

    args: str = ""
    """Everything after the verb (``gpt-5`` for ``/model gpt-5``); empty for a
    bare command."""


@dataclass(frozen=True, slots=True, kw_only=True)
class UnknownMessage(JsonCodec):
    """A record an adapter recognized but cannot yet map to a typed member.

    The escape hatch: rather than drop an unrecognized CLI log record, the
    capture adapter wraps its raw object here so nothing is lost and the
    record can be promoted to a real member later.
    """

    raw: JSON = field(default_factory=lambda: cast(JSON, {}))
    """The raw CLI-shape object, stored verbatim."""


type Message = (
    UserMessage
    | AgentSendMessage
    | SystemMessage
    | AssistantMessage
    | ToolResult
    | Compaction
    | SlashCommand
    | UnknownMessage
)
"""The typed interior of one event's ``message``; the row's ``kind`` selects
the member."""

type Kind = Literal[
    "UserMessage",
    "AgentSendMessage",
    "SystemMessage",
    "AssistantMessage",
    "ToolResult",
    "Compaction",
    "SlashCommand",
    "UnknownMessage",
]
"""The ``agent_session_events.kind`` discriminator: the class name of the
:data:`Message` member, mirroring how :data:`Inquiry.InquiryKind` stores
``"Issue"`` / ``"AgentSession"``. The ``kind`` -> class map is just
``{cls.__name__: cls}``.

There is no standalone ``ToolCall`` kind -- a tool call is nested in
:attr:`AssistantMessage.tool_calls`. Session lifecycle (``started`` /
``ended``) is not an event: it lives on the ``AgentSession`` row.
"""


# The one irreducible piece of decode-from-a-kind-string: a class lookup.
# Encoding needs no registry (``message.to_json()``); decoding from a row's
# ``kind`` string resolves the member here and calls its inherited
# ``from_json``. Used inline by :meth:`AgentSessionEvent.from_row`.
_MESSAGE_BY_KIND: Mapping[str, type[Message]] = {
    cls.__name__: cls
    for cls in (
        UserMessage,
        AgentSendMessage,
        SystemMessage,
        AssistantMessage,
        ToolResult,
        Compaction,
        SlashCommand,
        UnknownMessage,
    )
}


def message_for_kind(kind: str) -> type[Message]:
    """The :data:`Message` member class named by a :data:`Kind` value.

    The one place the ``kind`` string maps to a class -- the analog of
    :data:`KIND_TO_CLASS` for inquiries. Callers decode a stored / wire
    ``message`` via ``message_for_kind(kind).from_json(data)``.
    """
    member = _MESSAGE_BY_KIND.get(kind)
    if member is None:
        raise ValueError(f"unknown message kind {kind!r}")
    return member


# -- The row ------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentSessionEvent:
    """One turn of a captured agent session: one ``agent_session_events`` row.

    Identity is ``(session_id, seq)``: ``seq`` is harness-assigned and
    monotonic per session, so a re-sent event collides and dedups rather
    than duplicating. ``message`` is the typed :data:`Message` for the turn,
    selected by :attr:`kind`, stored whole as JSON (no app-level offload --
    Postgres TOAST absorbs the large ones; heavy attachments choose the
    ``FilePath`` / ``WebUrl`` :data:`Attachment` variant instead of inline
    bytes).
    """

    session_id: UUID = field(default_factory=uuid4)
    """The :class:`AgentSession` (an ``inquiries`` row) this turn belongs to."""

    seq: int = 0
    """Harness-assigned per-session ordinal, from 0. With ``session_id`` it
    is the primary key and the dedup key."""

    kind: Kind = field(
        default="UserMessage",
        metadata=ColumnSpec(sql_type="TEXT", required=True),
    )
    """Which :data:`Message` member :attr:`message` holds; see :data:`Kind`.
    Always equals ``type(self.message).__name__``."""

    model: str | None = field(
        default=None,
        metadata=ColumnSpec(sql_type="TEXT"),
    )
    """The model for this turn (turns within a session may differ)."""

    timestamp: datetime | None = field(
        default=None,
        metadata=ColumnSpec(sql_type="TIMESTAMPTZ"),
    )
    """When the turn happened, on the agent/CLI clock. Distinct from
    :attr:`created` (when trackinizer wrote the row)."""

    message: Message = field(
        default_factory=UserMessage,
        metadata=ColumnSpec(sql_type="JSONB", sql_default="'{}'::jsonb"),
    )
    """The turn content, a :data:`Message` selected by :attr:`kind`. Stored
    as JSON under the ``message`` column (Postgres TOAST handles the larger
    ones transparently). The default member matches the default :attr:`kind`
    (``"UserMessage"``)."""

    created: datetime | None = None
    """When trackinizer wrote the row (DB clock). Server-managed; ``None``
    until persisted."""

    def __post_init__(self) -> None:
        # ``kind`` is the discriminator for ``message``; a mismatch would let
        # ``from_row`` decode against the wrong type. Enforce agreement at
        # construction so the two can never drift.
        actual = type(self.message).__name__
        if self.kind != actual:
            raise ValueError(
                f"kind {self.kind!r} disagrees with message type {actual!r}"
            )

    @classmethod
    def from_row(cls, row: Row) -> Self:
        """Build an event from one ``agent_session_events`` row.

        ``message`` is decoded from its JSON column into the typed
        :data:`Message` member named by ``kind`` -- the registry resolves the
        class, the class's own ``from_json`` rebuilds it; every other field is
        read straight off the row.

        Raises:
          KeyError: The row omits an identity column (``session_id``, ``seq``,
            or ``kind``). These are NOT NULL primary-key components, so a
            missing one is a malformed row, not a default -- fabricating a
            random ``session_id`` (the field default) would mis-scope the
            event. Mirrors :meth:`Inquiry.from_row` requiring its identity keys.

        """
        kwargs: dict[str, Any] = {
            "session_id": row["session_id"],
            "seq": row["seq"],
            "kind": row["kind"],
        }
        for f in fields(cls):
            if f.name == "message" or f.name in kwargs:
                continue
            if f.name in row:
                kwargs[f.name] = row[f.name]
        raw = cast("Mapping[str, object]", row.get("message") or {})
        kwargs["message"] = message_for_kind(cast(str, kwargs["kind"])).from_json(raw)
        return cls(**kwargs)
