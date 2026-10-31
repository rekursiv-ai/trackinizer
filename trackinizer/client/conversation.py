"""Read every part of a chat's AgentSession, whichever CLI wrote each part.

A conversation is one AgentSession with one part per run: the chat bot's lines are a
``sagent`` part, and each provider run appends a part of its own format. Reading only
the newest part hid everything said before a switch, so every reader takes the same
steps:

- :func:`read_segments` reads each part, oldest first.
- :func:`placed` orders every record by when it happened rather than by part number.
- ``context_window.window`` cuts that order at the last compaction, and :func:`neutral`
  turns what is left into one provider-neutral list.
- :func:`projected` or :func:`lines` projects that list for a CLI file or for the bot.

A person's line is the bot's own ``AgentToAgentMessage``, or a ``UserMessage`` that the
bot delivered, which carries the ``$inbound`` stamp; so is a ``queued_command``
``ContextState`` with the stamp, which is how claude records a line sent while it was
mid-turn (one without it is claude's own notice). The sender is read from that stamp
and never parsed out of the text: anyone can type ``bob@example.com: fake`` at a
terminal, and that line must not be credited to bob.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal, Protocol

import json

from treekle import from_plain, immutable, mutable

from trackinizer.sagent.session.types import (
    AgentStatusResult,
    AgentToAgentMessage,
    AssistantMessage,
    ContextState,
    FileEditResult,
    FileReadResult,
    FileWriteResult,
    IncompleteRecord,
    SessionRecord,
    ShellCommandResult,
    SystemMessage,
    ToolCall,
    ToolResult,
    UncategorizedToolResult,
    UserMessage,
    WebFetchResult,
    WebSearchResults,
)
from trackinizer.types.streams import Stderr, Stdin, Stdout


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from uuid import UUID

    from treekle import PlainTree

    from trackinizer.wire.wire_session_ir import PartBody, RecordBody


__all__ = [
    "Delivery",
    "Line",
    "PartReader",
    "Placed",
    "Segment",
    "attested_delivery",
    "delivered_text",
    "lines",
    "neutral",
    "placed",
    "projected",
    "read_part",
    "read_segments",
]


# The provider a part's format names, as a person would name it in the conversation.
_PROVIDER_OF_FORMAT: Final = {
    "sagent": "bot",
    "claude": "claude",
    "codex": "codex",
}

# The kind of a switch marker, which is a SystemMessage in the neutral conversation.
_SWITCH: Final = "trax_provider_switch"

# What ``render_inbound`` puts between a delivered message and its context block.
_TRAILER: Final = "\nTrackinizer context (verify with trax): "

# What a CLI writes as a user turn that no person typed.
_HARNESS: Final = (
    "<environment_context>",
    "<user_instructions>",
    "# AGENTS.md instructions",
    "<command-name>",
    "<command-message>",
    "<local-command-stdout>",
    "<local-command-caveat>",
    "<local-command-stderr>",
    "Caveat: The messages below were generated",
    "<system-reminder>",
    "<permissions instructions>",
    "<turn_aborted>",
)

# Results whose tool is named by what they DID, so they cross to another CLI as structure.
_TYPED: Final = (
    ShellCommandResult,
    FileReadResult,
    FileWriteResult,
    FileEditResult,
    WebFetchResult,
    WebSearchResults,
)


class PartReader(Protocol):
    """What reading a session's parts needs of a client: ``Client``, or a narrower one."""

    def read_session_parts(self, session_id: UUID) -> list[PartBody]:
        """List the files a session was captured from."""
        ...

    def read_session_records(
        self,
        session_id: UUID,
        *,
        part: int = 0,
        plaintext_only: bool = False,
    ) -> list[RecordBody]:
        """Read one part's records in ``idx`` order."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class Segment:
    """One native part of a session: what it holds, and which provider wrote it.

    Attributes:
      part: The part's number within the session.
      name: The source file's basename.
      format: The adapter that reads the part, such as ``claude`` or ``sagent``.
      records: The part's records in ``idx`` order.

    """

    part: int
    name: str
    format: str
    records: list[SessionRecord]

    @property
    def provider(self) -> str:
        """The provider that wrote this part."""
        return _PROVIDER_OF_FORMAT.get(self.format, self.format)


@dataclass(frozen=True, slots=True, kw_only=True)
class Placed:
    """One record of a session: where it is stored, who wrote it, and when."""

    part: int
    idx: int
    """The record's position in its part, which ``trax agentsession N records`` names."""

    provider: str
    moment: datetime
    """When it happened, filled in from its part's neighbours when it carries no stamp."""

    record: SessionRecord


@dataclass(frozen=True, slots=True, kw_only=True)
class Delivery:
    """The sender and room the bot attested when it delivered a person's message."""

    source: str
    """The route-attested sender, empty when none was attested."""

    room: str
    """The room the message was sent to, empty when none."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Line:
    """One line of the bot's history, with who said or answered it."""

    role: Literal["user", "assistant"]
    author: str
    """The person who wrote a line, or the name of the answering provider."""

    text: str


def read_part(
    client: PartReader,
    session_id: UUID,
    part: int,
    *,
    plaintext_only: bool,
) -> tuple[list[SessionRecord], list[str | None]]:
    """Read every record of one part, with the ciphertext each one carried.

    Args:
      client: Talks to the server holding the session.
      session_id: The AgentSession the part belongs to.
      part: The part's number within the session.
      plaintext_only: Whether to skip the ciphertext. A replay needs it; a reader
        that never replays does not.

    Returns:
      records: The part's records in ``idx`` order.
      sealed: Each record's ciphertext, aligned with ``records``; ``None`` where a
        record has none.

    Raises:
      TypeError: The part holds a stream record, which only a formatless part does.

    """
    records: list[SessionRecord] = []
    sealed: list[str | None] = []
    for body in client.read_session_records(
        session_id,
        part=part,
        plaintext_only=plaintext_only,
    ):
        row = body.row(session_id, part)
        record = row.record()
        # A native part holds no stream records, so narrowing here is an invariant
        # that a future part breaking it fails loudly instead of inside a writer.
        if isinstance(record, Stdin | Stdout | Stderr):
            raise TypeError(
                f"a native part holds no stream records; {row.kind} came from one",
            )
        records.append(record)
        sealed.append(row.ciphertext)
    return records, sealed


def read_segments(client: PartReader, session_id: UUID) -> list[Segment]:
    """Read every native part of a session, oldest first.

    A part with no format is an ``sh`` scrape: searchable, never a conversation, so it
    is skipped. Ciphertext is not read, since the merged conversation never replays
    sealed reasoning.

    Args:
      client: Talks to the server holding the session.
      session_id: The AgentSession to read.

    Returns:
      segments: One per native part, in part order.

    """
    segments: list[Segment] = []
    for part in sorted(client.read_session_parts(session_id), key=lambda p: p.part):
        if not part.format:
            continue
        records, _ = read_part(client, session_id, part.part, plaintext_only=True)
        segments.append(
            Segment(
                part=part.part,
                name=part.name,
                format=part.format,
                records=records,
            ),
        )
    return segments


# Two parts can be written at once: a bot turn still writing when a claude run answers.
# Ties keep the order the parts were given in.
def placed(segments: Sequence[Segment]) -> list[Placed]:
    """Return every record of every part, by the moment it happened.

    Args:
      segments: The parts, from :func:`read_segments`.

    Returns:
      placed: Each record with its part, position, provider and moment, oldest first.

    """
    out = [
        Placed(
            part=segment.part,
            idx=idx,
            provider=segment.provider,
            moment=moment,
            record=record,
        )
        for segment in segments
        for idx, (moment, record) in enumerate(
            zip(_moments(segment.records), segment.records, strict=True),
        )
    ]
    out.sort(key=lambda each: each.moment)
    return out


def neutral(
    records: Sequence[Placed],
    *,
    terminal_user: str = "the terminal user",
    answerer: str = "",
) -> list[Placed]:
    """Turn placed records into one provider-neutral conversation.

    Kept: people's lines as :class:`AgentToAgentMessage`, with the box paths of the
    files their sender attached (``extra["attachments"]``), and answers and tool records
    each tagged with the provider that gave them. A switch between answering providers
    becomes a :class:`SystemMessage` marker, placed where the record that switched is.
    Dropped: every :class:`ContextClear`, whose summary opens the window instead,
    thinking, settings, usage, uncategorized and incomplete records, and the CLI's own
    harness lines, which each target restates for itself.

    Args:
      records: The records to keep from, from :func:`placed`, oldest first.
      terminal_user: Who a typed line with no attested sender is credited to.
      answerer: The provider that answered just before ``records``, if any.

    Returns:
      neutral: The conversation, oldest first.

    """
    out: list[Placed] = []
    for each in records:
        kept = _neutral(
            each.record,
            provider=each.provider,
            terminal_user=terminal_user,
        )
        if kept is None:
            continue
        # A tool call answers too: a provider often calls before it speaks, and a marker
        # placed after its calls landed behind its own tool result, where claude's writer
        # folds a user note into the result's line and drops it.
        if isinstance(kept, AssistantMessage | ToolCall):
            if answerer and answerer != each.provider:
                out.append(replace(each, record=_switch(answerer, each.provider)))
            answerer = each.provider
        out.append(replace(each, record=kept))
    return out


def projected(
    conversation: Sequence[SessionRecord],
    target: str,
    *,
    opening: Sequence[SessionRecord] = (),
) -> list[SessionRecord]:
    """Project the conversation into records a CLI writer takes as its own.

    A person's line becomes a user turn spelled ``sender: text``, as the agent runner
    types one, so a line typed live and a line loaded read alike. A switch becomes a
    user note naming who answered before. A foreign tool call and its result become
    assistant text, unless the result is typed: typed results cross as structure, and
    each writer names them for its own tool. Consecutive turns of one role become one
    turn. Every record is timestamped, which claude's reader requires.

    Args:
      conversation: The neutral records, from :func:`neutral`.
      target: The CLI the file is written for, ``claude`` or ``codex``.
      opening: Records already in the target's terms that the file opens with: the
        window's summary or its native compaction, and its note.

    Returns:
      records: What the target's writer takes, oldest first.

    """
    typed_calls = {
        record.call_id for record in conversation if isinstance(record, _TYPED)
    }
    names: dict[str, str] = {}
    out: list[SessionRecord] = list(opening)
    for record in conversation:
        out.extend(
            _projected(record, target=target, typed_calls=typed_calls, names=names),
        )
    last = (
        next(
            (
                _provider(record)
                for record in reversed(conversation)
                if isinstance(record, AssistantMessage | ToolCall)
            ),
            None,
        )
        or target
    )
    if last != target:
        out.append(
            UserMessage(
                content=(
                    f"[trax] Provider switch: the assistant turns above since the "
                    f"last switch were answered by {last}. From here {target} "
                    "answers -- that is you."
                ),
            ),
        )
        out.append(AssistantMessage(content=f"Understood; continuing as {target}."))
    if target == "claude":
        out = _own_lines(out)
    return _stamped(_folded(out))


def lines(conversation: Sequence[SessionRecord], *, bot: str) -> list[Line]:
    """Read the conversation as the bot's history: people's lines and named answers.

    Args:
      conversation: The neutral records, from :func:`neutral`.
      bot: The name the bot answers under, for its own answers.

    Returns:
      lines: Oldest first. Tool calls and results are not lines.

    """
    out: list[Line] = []
    for record in conversation:
        if isinstance(record, AgentToAgentMessage):
            out.append(
                Line(
                    role="user",
                    author=record.sender or "unknown",
                    text=record.content or "",
                ),
            )
        elif isinstance(record, AssistantMessage) and (record.content or "").strip():
            provider = _provider(record)
            out.append(
                Line(
                    role="assistant",
                    author=bot if provider in {"", "bot"} else provider,
                    text=record.content or "",
                ),
            )
    return out


def attested_delivery(extra: Mapping[str, object]) -> Delivery | None:
    """Read the sender and room the bot attested for a delivered message.

    The stamp is written by the bot's own delivery, so a line typed at a terminal cannot
    claim a sender by carrying one.

    Args:
      extra: The record's provider residual.

    Returns:
      delivery: The attested sender and room, or None when the message carries no
        stamp.

    """
    stamp = from_plain(extra.get("$inbound"), dict[str, object], default={})
    if not stamp:
        return None
    return Delivery(
        source=from_plain(stamp.get("source"), str, default=""),
        room=from_plain(stamp.get("room"), str, default=""),
    )


def delivered_text(content: str, delivery: Delivery) -> str:
    """Return what a delivery said, without the prefix the bot rendered on it.

    Args:
      content: The message as the CLI recorded it.
      delivery: The sender and room it was rendered with.

    Returns:
      text: The message, without its ``[room] `` and ``sender: `` prefixes (the sender
        may be absent) or context block.

    """
    room = f"[{delivery.room}] " if delivery.room else ""
    sender = f"{delivery.source}: " if delivery.source else ""
    return content.removeprefix(room).removeprefix(sender).split(_TRAILER, 1)[0]


# A record with no stamp takes the one before it in its own part, and a leading one takes
# the first stamp in the part, so a part's own order holds whatever the other parts did.
def _moments(records: Sequence[SessionRecord]) -> list[datetime]:
    """Return when each record of one part happened, filled in from its neighbours."""
    stamped = [_moment(record) for record in records]
    last = next((m for m in stamped if m is not None), datetime.min.replace(tzinfo=UTC))
    out: list[datetime] = []
    for moment in stamped:
        last = last if moment is None else moment
        out.append(last)
    return out


def _moment(record: SessionRecord) -> datetime | None:
    """Return the record's stamp as an aware datetime, or None when unstamped or unreadable."""
    if isinstance(record, IncompleteRecord) or record.timestamp is None:
        return None
    try:
        found = datetime.fromisoformat(record.timestamp)
    except ValueError:
        return None
    return found if found.tzinfo is not None else found.replace(tzinfo=UTC)


def _neutral(
    record: SessionRecord,
    *,
    provider: str,
    terminal_user: str,
) -> SessionRecord | None:
    """Return the provider-neutral form of one record, or None when only its CLI can state it."""
    tag = immutable({"provider": provider})
    if isinstance(record, AgentToAgentMessage):
        attached = record.extra.get("attachments")
        return AgentToAgentMessage(
            timestamp=record.timestamp,
            content=record.content,
            sender=record.sender or "unknown",
            extra=tag
            if attached is None
            else immutable({**tag, "attachments": attached}),
        )
    if isinstance(record, UserMessage):
        return _person(record, tag=tag, terminal_user=terminal_user)
    if isinstance(record, ContextState) and record.kind == "queued_command":
        return _queued_person(record, tag=tag)
    if isinstance(record, AssistantMessage):
        if not (record.content or "").strip():
            return None
        return AssistantMessage(
            timestamp=record.timestamp,
            content=record.content,
            extra=tag,
        )
    if isinstance(record, ToolCall):
        return replace(record, context_id=None, extra=tag)
    if isinstance(record, ToolResult):
        failed = from_plain(record.extra.get("is_error"), bool, default=False)
        return replace(
            record,
            context_id=None,
            extra=immutable({"provider": provider, "is_error": failed}),
        )
    return None


def _person(
    record: UserMessage,
    *,
    tag: Mapping[str, PlainTree],
    terminal_user: str,
) -> AgentToAgentMessage | None:
    """Return a typed user turn as a person's line, or None for a line the CLI wrote."""
    content = record.content or ""
    if _is_meta(record) or _is_harness(content):
        return None
    delivery = attested_delivery(record.extra)
    if delivery is None:
        return AgentToAgentMessage(
            timestamp=record.timestamp,
            content=content,
            sender=terminal_user,
            extra=tag,
        )
    return AgentToAgentMessage(
        timestamp=record.timestamp,
        content=delivered_text(content, delivery),
        sender=delivery.source or "unknown",
        extra=tag,
    )


def _queued_person(
    record: ContextState,
    *,
    tag: Mapping[str, PlainTree],
) -> AgentToAgentMessage | None:
    """Return a stamped queued line as a person's line, or None for claude's own notice."""
    delivery = attested_delivery(record.extra)
    if delivery is None:
        return None
    return AgentToAgentMessage(
        timestamp=record.timestamp,
        content=delivered_text(record.content or "", delivery),
        sender=delivery.source or "unknown",
        extra=tag,
    )


# The compaction summary is also stated on the ContextClear before it. Keeping both put the
# summary into the chat twice, the second time as a person's line.
def _is_meta(record: UserMessage) -> bool:
    """Return whether a user line is one the CLI wrote for itself: a meta note or a summary."""
    return any(
        from_plain(record.extra.get(flag), bool, default=False)
        for flag in ("isMeta", "isCompactSummary", "isVisibleInTranscriptOnly")
    )


def _is_harness(content: str) -> bool:
    """Return whether a user turn is empty or CLI harness text, not typed by a person."""
    stripped = content.strip()
    return not stripped or stripped.startswith(_HARNESS)


def _switch(before: str, after: str) -> SystemMessage:
    """Return the marker that the answering provider changed from ``before`` to ``after``."""
    return SystemMessage(
        subtype=_SWITCH,
        content=f"{before} -> {after}",
        extra=immutable({"from": before, "to": after}),
    )


def _projected(
    record: SessionRecord,
    *,
    target: str,
    typed_calls: set[str],
    names: dict[str, str],
) -> list[SessionRecord]:
    """Return the records one neutral record becomes in the target's own file."""
    provider = _provider(record)
    own = provider == target
    if isinstance(record, AgentToAgentMessage):
        # Spelled as ``render_inbound`` spells a sender with no room and no context,
        # which is how a line typed live reads. The client package cannot import the
        # agent runner's package that owns that function.
        return [
            UserMessage(
                timestamp=record.timestamp,
                content=f"{record.sender}: {record.content or ''}",
            ),
        ]
    if isinstance(record, SystemMessage) and record.subtype == _SWITCH:
        return _switch_note(record, target=target)
    if isinstance(record, AssistantMessage):
        return [AssistantMessage(timestamp=record.timestamp, content=record.content)]
    if isinstance(record, ToolCall):
        names[record.call_id] = record.name
        if record.call_id in typed_calls or own:
            return [replace(record, extra=immutable({}))]
        args = json.dumps(mutable(record.arguments), sort_keys=True)[:400]
        return [
            AssistantMessage(
                timestamp=record.timestamp,
                content=f"[{provider} called its tool {record.name}({args})]",
            ),
        ]
    if isinstance(record, ToolResult):
        failed = from_plain(record.extra.get("is_error"), bool, default=False)
        # An own result crosses as structure only where its call was projected too: a
        # bare result with no tool_use is dropped by the writer that wrote it. A typed
        # one is synthesized a call by each writer, so it crosses either way.
        if record.call_id in typed_calls or (own and record.call_id in names):
            return [
                replace(
                    record,
                    extra=immutable({"is_error": failed} if failed else {}),
                ),
            ]
        name = names.get(record.call_id, "a tool")
        # Only an uncategorized or agent-status result has words no writer can name.
        words = (
            record.content
            if isinstance(record, UncategorizedToolResult | AgentStatusResult)
            else None
        )
        return [
            AssistantMessage(
                timestamp=record.timestamp,
                content=f"[{name} returned: {(words or '')[:1500]}]",
            ),
        ]
    return []


def _switch_note(record: SystemMessage, *, target: str) -> list[SessionRecord]:
    """Return a switch as the user note and acknowledgement a target reads it as."""
    before = from_plain(record.extra.get("from"), str, default="")
    after = from_plain(record.extra.get("to"), str, default="")
    note = (
        f"[trax] Provider switch: the assistant turns above were answered by {before}. "
        f"From here {after} answers" + (" -- that is you." if after == target else ".")
    )
    return [
        UserMessage(content=note),
        AssistantMessage(content=f"Understood; continuing as {after}."),
    ]


# Claude's writer joins a turn that carries no residual onto the tool result before it, and
# writes only the result, so the turn's text is lost. A key that is not one of the reader's
# own ``$`` keys marks the turn as a line of its own, which is how claude states a line a
# person typed. A stopgap for the writer, which is claude's to fix at the join itself.
def _own_lines(records: Sequence[SessionRecord]) -> list[SessionRecord]:
    """Return the records with each user turn that follows a tool result marked as its own."""
    out: list[SessionRecord] = []
    for record in records:
        marked = record
        if isinstance(record, UserMessage) and out and isinstance(out[-1], ToolResult):
            marked = replace(record, extra=immutable({"userType": "external"}))
        out.append(marked)
    return out


# Folded by the writer, so the turns are folded here first. Claude's writer joins
# consecutive turns of one role into a single line and reads that line back as one turn,
# so two people speaking in a row would be one turn at the writer and two at the loss gate.
def _folded(records: Sequence[SessionRecord]) -> list[SessionRecord]:
    """Return each run of consecutive user turns, or of assistant turns, as one turn."""
    out: list[SessionRecord] = []
    for record in records:
        previous = out[-1] if out else None
        if (isinstance(record, UserMessage) and isinstance(previous, UserMessage)) or (
            isinstance(record, AssistantMessage)
            and isinstance(
                previous,
                AssistantMessage,
            )
        ):
            out[-1] = replace(
                previous,
                content=f"{previous.content or ''}\n{record.content or ''}",
            )
        else:
            out.append(record)
    return out


# Claude refuses a file whose lines lack one. Measured: a file whose last two lines (a
# synthesized switch note) carried no timestamp answered ``No conversation found with
# session ID``, and the same conversation with them resumed.
def _stamped(records: Sequence[SessionRecord]) -> list[SessionRecord]:
    """Give every record a timestamp, filled in from the one before it."""
    last = datetime.now(UTC).isoformat()
    out: list[SessionRecord] = []
    for record in records:
        if isinstance(record, IncompleteRecord):
            out.append(record)
            continue
        stamp = record.timestamp
        if stamp is None or not stamp:
            out.append(replace(record, timestamp=last))
            continue
        last = stamp
        out.append(record)
    return out


def _provider(record: SessionRecord) -> str:
    """Return the provider a neutral record was tagged with, or an empty string."""
    extra = from_plain(getattr(record, "extra", None), dict[str, object], default={})
    return from_plain(extra.get("provider"), str, default="")
