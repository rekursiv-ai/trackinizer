"""Normalize and denormalize Codex rollout streams.

Codex nests every record under one ``payload``, so the outer line carries
only its timestamp, kind, and -- in a file that numbers its lines -- an
ordinal. What the record types do not name rides in ``extra``, which is what
lets the writer replay the payload rather than approximate it.

Key order is codex's own and fixed per payload type, so it lives here as a
tuple per type rather than being stored on every record.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import replace
from types import MappingProxyType
from typing import Final, TextIO, cast, get_args

import base64
import binascii
import json

from trackinizer.lib.absent import Absent
from trackinizer.lib.agent.sessions.codex_orders import payload_orders
from trackinizer.lib.agent.sessions.shell_results import (
    lift_shell_result,
    shell_result_for_replay,
)
from trackinizer.lib.agent.sessions.udiff import parse_udiff, render_udiff
from trackinizer.lib.agent.types.sessions import (
    AgentStatusResult,
    AgentToAgentMessage,
    AssistantMessage,
    Attachment,
    ContextClear,
    ContextCompaction,
    ContextState,
    FileEditResult,
    FileReadResult,
    FileWriteResult,
    IncompleteRecord,
    SessionRecord,
    ShellCommandResult,
    Splice,
    SummaryKind,
    SystemMessage,
    Thinking,
    ThinkingEffort,
    TokenUsage,
    ToolCall,
    ToolResult,
    TurnContext,
    UncategorizedRecord,
    UncategorizedToolResult,
    UserMessage,
    WebFetchResult,
    WebSearchResult,
    WebSearchResults,
)
from trackinizer.lib.custom_json import (
    JSON,
    DictCodec,
    FieldState,
    IntCodec,
    Invalid,
    JSONValue,
    ListCodec,
    MutableJSONValue,
    StrCodec,
    decode_or_none,
    json_freeze,
    json_unfreeze,
    replay,
    residual,
    take,
)


__all__ = ["denormalize", "normalize"]


def normalize(stream: TextIO) -> Iterator[SessionRecord]:
    """Normalize a Codex rollout JSONL stream into its records.

    Yields as it reads (axiom 11), so a caller tailing a live rollout sees a
    record when its line lands rather than when the file ends -- a session
    being written never ends, which is why returning one object could not
    serve a tailer at all.

    Args:
      stream: Codex rollout JSONL text stream.

    Yields:
      record: Each record the rollout declares, in stream order.

    """
    reader = _Reader()
    for line in stream:
        yield from reader.read(line)
    yield from reader.close()


class _Reader:
    """Read a Codex rollout one line at a time.

    Line by line, never the whole file as one string (axiom 11): ONE non-ASCII
    character makes CPython store the entire string as 4 bytes per character,
    so reading a 273 MB rollout whole cost 1.09 GB before any parsing. Per
    line, that widening is confined to the lines that need it.

    Private to :func:`normalize`, which is the only entry point: the generator
    IS the incremental interface, so nothing outside needs a reader object.
    """

    def __init__(self) -> None:
        self._records: list[SessionRecord] = []
        self._declared: dict[str, JSONValue] = {}
        self._launch_timestamp: str | None = None
        # The index of the most recent ``turn_context``; every later record
        # names it. Codex writes one per turn, so this moves as the stream
        # advances.
        self._context_id: int | None = None
        self._position = 0
        self._ends_newline = True
        self._opened = False
        # Where the opening clear sits, and what the fresh context was given.
        # Codex NAMES a prompt on its launch line and then sends more before
        # the first turn -- a skills block, a role prompt -- so the record
        # that states what the model began from is only complete once an act
        # arrives.
        self._opening: int | None = None
        self._given: list[str] = []
        # How much of ``_records`` the caller already holds.
        self._yielded = 0

    def read(self, line: str) -> Iterator[SessionRecord]:
        """Consume one rollout line; yield the records it produced.

        A record is the caller's once yielded (axiom 11), so the opening clear
        is HELD until the instructions it states are complete: codex keeps
        sending them after the launch line, and a clear already handed over
        cannot gain them. Held records are released the moment the first act
        closes the opening, which is the only point at which the window is
        fully described.
        """
        emitted = len(self._records)
        position = self._position
        self._position += 1
        self._ends_newline = line.endswith("\n")
        if not self._opened:
            # Settings before the acts they govern. Codex declares them on its
            # launch line, so a rollout that opens with one supersedes this
            # immediately -- the reader states what it knows and restates.
            self._opened = True
            self._records.append(
                TurnContext(
                    # No ``ascii_escaped``: codex writes raw UTF-8 on every one
                    # of 13138 captured non-ASCII lines, so the convention is
                    # the format's, not the file's.
                    encoding=json_freeze({"newline_terminated": True})
                )
            )
            self._context_id = 0
        del emitted
        self._read(line, position)
        # From what was last handed over, never from where this line began:
        # the opening HOLDS records across several lines, so a per-line start
        # would re-yield the block on the line that releases it.
        start, self._yielded = self._yielded, self._release()
        return iter(self._records[start : self._yielded])

    def _release(self) -> int:
        """How far the stream may be handed over, given the window's state.

        Everything, once the opening is closed. While it is open, only the
        records BEFORE the clear: codex keeps sending instructions after its
        launch line, so the clear is not yet what it will be, and a record is
        the caller's the moment it is yielded (axiom 11).
        """
        return len(self._records) if self._opening is None else self._opening

    def close(self) -> Iterator[SessionRecord]:
        """Yield what only the END of the stream could say."""
        if self._opening is not None:
            # A rollout that is nothing but its opening -- a launch line and
            # its instructions, no turn yet. EOF closes the window that no
            # first act closed, or the clear would never be handed over at all.
            self._opening = None
            at, self._yielded = self._yielded, len(self._records)
            yield from self._records[at:]
        if self._position and not self._ends_newline:
            # Knowable only here: a later state record supersedes the opening
            # one rather than mutating a record already handed to the caller.
            yield TurnContext(encoding=json_freeze({"newline_terminated": False}))

    def _read(self, line: str, position: int) -> None:
        """Append whatever one line contributes to the record stream."""
        # Every line yields a record, so a blank one cannot vanish.
        if not line.strip():
            self._records.append(IncompleteRecord(text=line))
            return
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError:
            self._records.append(IncompleteRecord(text=line))
            return
        record = DictCodec.coerce(decoded)
        if not isinstance(decoded, dict):
            self._records.append(IncompleteRecord(text=line))
            return
        payload_value = record.get("payload")
        if not isinstance(payload_value, Mapping):
            self._records.append(IncompleteRecord(text=line))
            return
        outer = StrCodec.coerce(record.get("type"))
        payload = DictCodec.coerce(record.get("payload"))
        timestamp = decode_or_none(str, record.get("timestamp"))
        if outer == "session_meta" and not self._declared:
            # The launch line IS settings, so it supersedes the opening
            # context rather than becoming a record beside it. The clear that
            # follows states what the model begins from; both are derived, so
            # neither costs a line on the way back out.
            self._declared = _read_declaration(record, payload, position)
            self._launch_timestamp = decode_or_none(str, record.get("timestamp"))
            declared = TurnContext(
                timestamp=self._launch_timestamp,
                encoding=json_freeze({"newline_terminated": True}),
                extra=json_freeze(dict(self._declared)),
            )
            if position:
                # A launch line the file did not open with -- 1 captured
                # rollout begins with a blank one. The opening context has
                # already been YIELDED by now, so it is superseded by a fresh
                # record rather than rewritten: a record handed to the caller
                # is the caller's (axiom 11), and overwriting index 0 lost the
                # declaration into a slot the blank line's record had taken.
                self._context_id = len(self._records)
                self._records.append(declared)
            else:
                self._records[0] = declared
            declared_prompt = _declared_instructions(payload)
            opens: dict[str, JSONValue] = {"$opens": True}
            if declared_prompt is not None:
                self._given.append(declared_prompt)
                # How much of the assembled prompt the LAUNCH LINE owns. The
                # clear states everything the fresh context was given, and the
                # rest arrives as later system messages -- so a writer
                # rebuilding ``base_instructions`` from the whole thing put
                # the skills block on the launch line, where codex never
                # wrote it, and the rollout no longer matched its own bytes.
                opens["$declared"] = len(declared_prompt)
            self._opening = len(self._records)
            self._records.append(
                ContextClear(
                    timestamp=timestamp,
                    cleared_session_id=StrCodec.coerce(payload.get("forked_from_id"))
                    or None,
                    system_prompt=declared_prompt,
                    extra=json_freeze(opens),
                )
            )
            return
        if outer == "session_meta":
            # 19 captured rollouts declare the session twice -- a fork
            # re-announcing itself. Only the first is the file's declaration;
            # a later one is a record in the stream like any other.
            self._records.append(
                _with_line_state(
                    UncategorizedRecord(
                        context_id=self._context_id,
                        timestamp=timestamp,
                        kind="session_meta/repeat",
                        payload=json_freeze(payload),
                    ),
                    record,
                )
            )
            return
        if outer == "turn_context":
            self._context_id = len(self._records)
            self._records.append(
                _with_line_state(_read_context(payload, timestamp), record)
            )
            return
        for item in _read_records(outer, payload, self._context_id, timestamp):
            self._records.append(self._opened_with(_with_line_state(item, record)))

    def _opened_with(self, item: SessionRecord) -> SessionRecord:
        """Fold an opening instruction into the clear, and return the record.

        The clear is what delineates a session, so it has to state everything
        the fresh context was GIVEN -- not only the prompt the launch line
        named. Codex sends its skills block and role prompt as ordinary system
        messages before the first turn, and a clear carrying just the declared
        one describes part of what the model saw, which a consumer cannot tell
        from all of it.

        The record still stands on its own: this fills a field, it does not
        consume the line. Dropping it would cost the rewrite its bytes.
        """
        if self._opening is None:
            return item
        if isinstance(item, SystemMessage) and item.content:
            self._given.append(item.content)
            opening = self._records[self._opening]
            assert isinstance(opening, ContextClear)
            self._records[self._opening] = replace(
                opening, system_prompt="\n".join(self._given)
            )
            return item
        if isinstance(item, UncategorizedRecord | ContextState):
            # Bookkeeping around the opening rather than part of it: a turn
            # boundary, a world-state snapshot. Neither is an instruction, and
            # neither ends the opening.
            return item
        # The first act closes it: what follows is the conversation.
        self._opening = None
        return item


def _read_declaration(
    record: Mapping[str, object],
    payload: Mapping[str, object],
    position: int,
) -> dict[str, JSONValue]:
    """Read the launch line: the settings a rollout opens with.

    Returned as the residual of the opening :class:`TurnContext`, because a
    launch line IS settings -- cwd, cli version, model provider, git state --
    and axiom 6 makes a context full state. There is no separate declaration
    record for it to live on.
    """
    extra: dict[str, JSONValue] = {}
    # The whole payload verbatim, order included: codex writes keys no table
    # anticipated (``dynamic_tools``, ``agent_role``), and a table that misses
    # one misplaces it.
    extra["payload"] = residual(payload, ())
    # Where the declaration sat, and whether the file numbers its lines: a
    # rollout whose launch line carries an ordinal numbers every line.
    extra["line"] = position
    extra["$timestamp"] = "timestamp" in record
    if "payload" in record:
        # Only WHERE it sat, since the payload itself is stored above.
        extra["$payload_at"] = list(record).index("payload")
    # The instructions move to the opening ``ContextClear``, which is where the
    # IR states what a session begins from. Held in BOTH places the whole
    # system prompt was stored twice -- +18 KB on a 94 KB captured golden --
    # which is the same duplication the ``$outer`` note below exists to stop.
    # A STENCIL, not a deletion: the key keeps its place and its siblings --
    # the object also carries a ``provenance`` naming the model that wrote the
    # prompt, and no field on the clear holds that.
    if _declared_instructions(payload) is not None:
        stored = DictCodec.coerce(extra["payload"])
        held = stored.get("base_instructions")
        extra["payload"] = json_freeze(
            {
                key: _emptied_instructions(value)
                if key == "base_instructions"
                else value
                for key, value in stored.items()
            }
            if isinstance(held, Mapping | str)
            else stored
        )
    # WITHOUT the payload, which ``extra["payload"]`` above already is and
    # ``_write_line`` overwrites from anyway. Keeping it stored the launch
    # line's body twice -- and that body carries ``base_instructions``, so the
    # whole system prompt was held twice: ~19 KB of a 112 KB captured golden.
    # The rule ``_with_line_state`` states for every other line; this was the
    # one path that skipped it.
    extra["$outer"] = json_freeze(
        {key: value for key, value in record.items() if key != "payload"}
    )
    # No ``$launch_timestamp_raw``: a string stamp is what the metadata's own
    # field carries, so storing it here wrote every launch stamp twice. Only a
    # MALFORMED one -- which the field cannot hold -- survives nowhere else.
    raw = record.get("timestamp")
    if raw is not None and not isinstance(raw, str):
        extra["$launch_timestamp_raw"] = cast(JSONValue, raw)
    if "ordinal" in record:
        extra["ordinal"] = IntCodec.coerce(record.get("ordinal"), 0)
    return extra


def _with_line_state(item: SessionRecord, outer: Mapping[str, object]) -> SessionRecord:
    """Store Codex line replay state on its owning record.

    The outer line WITHOUT its payload: the payload is the record, and
    :func:`_write_line` overwrites the key from it, so keeping a copy stored
    every line's body twice -- 59.5% of everything ``extra`` held.

    Nor its ``timestamp`` when the record's own field holds it: the writer
    reads the field and only falls back to this copy for a stamp no field can
    carry. Storing both wrote every stamp twice -- 1241 of 1470 records on one
    captured rollout. The key's POSITION still matters, so an empty string
    marks where it sat.
    """
    stencil: dict[str, object] = {}
    for key, value in outer.items():
        if key == "payload":
            continue
        stencil[key] = "" if key == "timestamp" and isinstance(value, str) else value
    state: dict[str, MutableJSONValue] = {"outer": json_unfreeze(json_freeze(stencil))}
    if "payload" in outer:
        # Only WHERE it sat: the payload itself is the record.
        state["payload_at"] = list(outer).index("payload")
    if _is_canonical_line(outer):
        return item
    if isinstance(item, UncategorizedRecord):
        payload = dict(json_unfreeze(item.payload))
        if "$codex_line" in payload:
            state["value"] = payload["$codex_line"]
        payload["$codex_line"] = state
        return replace(item, payload=json_freeze(payload))
    if isinstance(item, IncompleteRecord):
        return item
    extra = dict(json_unfreeze(item.extra))
    extra["$codex_line"] = state
    return replace(item, extra=json_freeze(extra))


def _is_canonical_line(outer: Mapping[str, object]) -> bool:
    """Whether :func:`_write_line` rebuilds this outer line from the record.

    The shape it emits: a string ``timestamp`` the record's field carries, the
    ``type`` it is told, then ``payload`` last. A line with an extra key, a
    malformed stamp, or a different order is not reproducible and keeps its own
    copy.
    """
    if list(outer) != ["timestamp", "type", "payload"]:
        return False
    return isinstance(outer["timestamp"], str) and isinstance(outer["type"], str)


def _pop_line_state(
    item: SessionRecord,
) -> tuple[dict[str, object], SessionRecord]:
    """Remove and return record-owned Codex line replay state."""
    if isinstance(item, IncompleteRecord):
        return {}, item
    if isinstance(item, UncategorizedRecord):
        payload = dict(json_unfreeze(item.payload))
        state = DictCodec.coerce(payload.get("$codex_line"))
        if not isinstance(state.get("outer"), Mapping):
            return {}, item
        payload.pop("$codex_line")
        if "value" in state:
            payload["$codex_line"] = json_unfreeze(state["value"])
        return state, replace(item, payload=json_freeze(payload))
    extra = dict(json_unfreeze(item.extra))
    state = DictCodec.coerce(extra.get("$codex_line"))
    if not isinstance(state.get("outer"), Mapping):
        return {}, item
    extra.pop("$codex_line")
    return state, replace(item, extra=json_freeze(extra))


def _read_context(payload: Mapping[str, object], timestamp: str | None) -> TurnContext:
    """Read the settings line codex writes once per turn.

    A summary the IR does not name stays in the residual rather than being
    dropped, so the field means what it says and the line still rewrites.
    """
    model = take(payload, "model", str)
    permission = take(payload, "approval_policy", str)
    wire = StrCodec.coerce(payload.get("effort"))
    effort = _effort(wire)
    summary = _summary_kind(StrCodec.coerce(payload.get("summary")))
    # Through ``replay``, not consumed and re-appended: a key the writer adds
    # back lands at the end, and codex writes ``summary`` BEFORE
    # ``truncation_policy`` on 57 captured rollouts.
    fields: dict[str, FieldState[object]] = {
        "model": model,
        "approval_policy": permission,
    }
    if effort is not None:
        fields["effort"] = effort
    if summary is not None:
        fields["summary"] = summary
    extra = residual(payload, fields=fields)
    if effort is not None and wire != effort:
        # Which spelling this line used. ``residual`` keeps an original only
        # for a value a round trip could RESPELL, which it judges numerically,
        # so an aliased string would otherwise come back as the canonical name
        # and rewrite 611 captured rollouts differently than codex wrote them.
        extra["$effort"] = wire
    return TurnContext(
        timestamp=timestamp,
        model=model if isinstance(model, str) else None,
        effort=effort,
        summary_kind=summary,
        permission=permission if isinstance(permission, str) else None,
        extra=json_freeze(extra),
    )


def _read_records(
    outer: str,
    payload: Mapping[str, object],
    context_id: int | None,
    timestamp: str | None,
) -> list[SessionRecord]:
    """Read one line into the records its payload carries."""
    kind = StrCodec.coerce(payload.get("type"))
    if outer == "response_item":
        return [_read_response_item(kind, payload, context_id, timestamp)]
    if outer == "event_msg":
        found = _read_event(kind, payload, context_id, timestamp)
        return list(found) if isinstance(found, list) else [found]
    if outer == "world_state":
        return [
            ContextState(
                context_id=context_id,
                timestamp=timestamp,
                kind="world_state",
                extra=json_freeze(_codex_residual(payload)),
            )
        ]
    if outer == "compacted":
        return _read_compacted(payload, context_id, timestamp)
    # No silent fallback: an unrecognized kind is named so a coverage test
    # counts it.
    return [
        UncategorizedRecord(
            context_id=context_id,
            timestamp=timestamp,
            kind=f"{outer}/{kind or 'root'}",
            payload=json_freeze(payload),
        )
    ]


def _emptied_instructions(value: object) -> JSONValue:
    """Empty the prompt text a launch line declared, keeping its shape.

    ``None`` marks the slot the clear's field fills, the way every other
    stencil in this module does; a sibling key -- ``provenance`` -- survives
    nowhere else, so the object is kept rather than replaced.
    """
    if not isinstance(value, Mapping):
        return None
    declared = cast(Mapping[str, object], value)
    return {
        key: None if key == "text" else cast(JSONValue, item)
        for key, item in declared.items()
    }


def _declared_instructions(payload: Mapping[str, object]) -> str | None:
    """Return the system prompt a codex launch line declares.

    Codex writes it as ``base_instructions``, which is either the text or an
    object carrying it under ``text`` -- the captured corpus uses the object
    form, with a ``provenance`` naming the model that produced it.
    """
    declared = payload.get("base_instructions")
    if isinstance(declared, str):
        return declared
    if not isinstance(declared, Mapping):
        return None
    return decode_or_none(str, cast(Mapping[str, object], declared).get("text"))


def _read_compacted(
    payload: Mapping[str, object],
    context_id: int | None,
    timestamp: str | None,
) -> list[SessionRecord]:
    """Read the line that replaces a session's history with a summary.

    ``replacement_history`` is the context AFTER compacting -- the turns the
    CLI kept, ending in a sealed ``compaction`` marker -- so it fills
    :attr:`history`, which is what the field was declared for. Left in the
    residual it round-tripped and passed every byte check while being
    invisible to a consumer: 343 KB across the captured corpus.

    The summary rides on that trailing marker rather than in ``message``:
    measured over 1355 rollouts, 0 of 8 compacted lines populate ``message``
    and all 8 carry ``encrypted_content``.
    """
    entries = ListCodec.coerce(payload.get("replacement_history"))
    kept: list[SessionRecord] = []
    sealed: str | None = None
    for value in entries:
        entry = DictCodec.coerce(value)
        if StrCodec.coerce(entry.get("type")) == "compaction":
            sealed = decode_or_none(str, entry.get("encrypted_content"))
            continue
        kept.append(
            _read_response_item(
                StrCodec.coerce(entry.get("type")), entry, context_id, timestamp
            )
        )
    stated = decode_or_none(str, payload.get("message"))
    consumed: set[str] = set()
    if stated is not None:
        consumed.add("message")
    if isinstance(payload.get("replacement_history"), list):
        consumed.add("replacement_history")
    extra = _codex_residual(payload, consumed)
    # WHICH shape the history was stored in, so the writer rebuilds the entry
    # list rather than inventing one. Axiom 2: an empty list is a value.
    if "replacement_history" in consumed:
        extra["$history"] = [
            cast(JSONValue, value) for value in entries if _is_sealed_marker(value)
        ]
    # TWO records: the event, then the window it opened. What a compaction
    # PRODUCED -- the summary and the turns that survived -- is the next
    # context, and every window opens with a clear whatever caused the reset.
    # One rule for "what was the model looking at": the last clear plus every
    # record after it.
    return [
        ContextCompaction(
            context_id=context_id, timestamp=timestamp, extra=json_freeze(extra)
        ),
        ContextClear(
            context_id=context_id,
            timestamp=timestamp,
            summary=stated or sealed,
            history=tuple(kept),
            extra=json_freeze({"$opens": True}),
        ),
    ]


def _is_sealed_marker(value: object) -> bool:
    """Whether one ``replacement_history`` entry is the sealed summary."""
    return StrCodec.coerce(DictCodec.coerce(value).get("type")) == "compaction"


def _read_response_item(
    kind: str,
    payload: Mapping[str, object],
    context_id: int | None,
    timestamp: str | None,
) -> SessionRecord:
    """Read one item of the transcript the model saw."""
    if kind == "message":
        return _read_message(payload, context_id, timestamp)
    if kind == "reasoning":
        return _read_thinking(payload, context_id, timestamp)
    if kind in {"function_call", "custom_tool_call"}:
        return _read_tool_call(payload, context_id, timestamp)
    if kind in {"function_call_output", "custom_tool_call_output"}:
        return _read_tool_result(payload, context_id, timestamp)
    if kind in {"web_search_call", "tool_search_call"}:
        # A search IS a tool invocation: it names an operation and carries
        # arguments. Mapping it to a state record instead lost it on every
        # cross-provider conversion. Its id is under ``id``, not ``call_id``.
        return _read_search_call(payload, context_id, timestamp)
    if kind == "tool_search_output":
        # It answers with ``tools``, never an ``output``, so nothing here maps
        # to :attr:`content` and the writer must not synthesize one.
        return UncategorizedToolResult(
            context_id=context_id,
            timestamp=timestamp,
            call_id=StrCodec.coerce(payload.get("call_id")),
            extra=json_freeze(_codex_residual(payload, {"call_id"}) | {"$whole": True}),
        )
    if kind == "agent_message":
        parts, attachments, templates = _read_content(payload.get("content"))
        extra = _codex_residual(payload, {"author", "recipient", "content"})
        if len(parts) > 1:
            extra["$parts"] = [len(part) for part in parts]
        extra["$templates"] = templates
        extra["$order"] = _block_order(payload.get("content"))
        return AgentToAgentMessage(
            context_id=context_id,
            timestamp=timestamp,
            content="\n".join(parts) if parts else None,
            attachments=attachments,
            sender=decode_or_none(str, payload.get("author")),
            recipient=decode_or_none(str, payload.get("recipient")),
            extra=json_freeze(extra),
        )
    return UncategorizedRecord(
        context_id=context_id,
        timestamp=timestamp,
        kind=f"response_item/{kind}",
        payload=json_freeze(payload),
    )


def _read_message(
    payload: Mapping[str, object], context_id: int | None, timestamp: str | None
) -> SessionRecord:
    """Read a message by the role that sent it."""
    role_value = payload.get("role")
    role = StrCodec.coerce(role_value)
    content_value = payload.get("content")
    content_list = ListCodec.coerce(content_value)
    parts, attachments, templates = _read_content(content_list)
    consumed: set[str] = set()
    if isinstance(role_value, str):
        consumed.add("role")
    if isinstance(content_value, list):
        consumed.add("content")
    extra = _codex_residual(payload, consumed)
    if "content" not in payload:
        extra["$content_absent"] = True
    elif isinstance(content_value, list):
        extra["$templates"] = templates
        extra["$order"] = _block_order(content_list)
    if len(parts) > 1:
        # Codex splits a long message across blocks, and the split is part of
        # the bytes. A part may itself contain newlines, so the LENGTHS travel
        # with the prose rather than a count.
        extra["$parts"] = [len(part) for part in parts]
    content = "\n".join(parts) if parts else None
    if role == "user":
        return UserMessage(
            context_id=context_id,
            timestamp=timestamp,
            content=content,
            attachments=attachments,
            extra=json_freeze(extra),
        )
    if role == "assistant":
        return AssistantMessage(
            context_id=context_id,
            timestamp=timestamp,
            content=content,
            attachments=attachments,
            extra=json_freeze(extra),
        )
    # ``developer`` / ``system``: real instructions the model saw, so they
    # belong in the record stream rather than being dropped for having an
    # unexpected role.
    return SystemMessage(
        context_id=context_id,
        timestamp=timestamp,
        content=content,
        attachments=attachments,
        subtype=role,
        extra=json_freeze(extra),
    )


def _block_order(content: object) -> list[JSONValue]:
    """Return each content member's semantic kind in wire order."""
    kinds: list[JSONValue] = []
    for value in ListCodec.coerce(content):
        part = DictCodec.coerce(value)
        if isinstance(part.get("text"), str):
            kinds.append("text")
        elif _read_attachment(part) is not None:
            kinds.append("image")
        else:
            kinds.append("other")
    return kinds


def _read_content(
    content: object,
) -> tuple[tuple[str, ...], tuple[Attachment, ...], list[JSONValue]]:
    """Split content into semantics and per-member wire templates.

    A template is a STENCIL: the keys the block carried, minus the values the
    record's own fields hold and the writer overwrites anyway. Keeping those
    stored every block's prose twice, which was 23.2% of all ``extra``.
    """
    parts: list[str] = []
    attachments: list[Attachment] = []
    templates: list[JSONValue] = []
    for value in ListCodec.coerce(content):
        part = DictCodec.coerce(value)
        if isinstance(part.get("text"), str):
            parts.append(StrCodec.coerce(part.get("text")))
            templates.append(_stencil(part, "text"))
        elif (found := _read_attachment(part)) is not None:
            # Whatever else the block carries. ``detail`` is metadata ABOUT the
            # image, not a different kind of block, and refusing one that had
            # it produced no attachment at all -- the image reached the IR
            # nowhere, while the stencil kept its bytes and every byte check
            # passed. The stencil holds every key the record's fields do not.
            attachments.append(found)
            templates.append(_stencil(part, "type", "image_url"))
        else:
            templates.append(cast(JSONValue, value))
    return tuple(parts), tuple(attachments), templates


def _stencil(block: Mapping[str, object], *held: str) -> JSONValue:
    """Return a block whose held values are emptied but whose keys still sit.

    ``None`` rather than absent: the writer puts the field's value back, and a
    key it has to append lands at the end rather than where it was.
    """
    return {
        key: None if key in held else cast(JSONValue, value)
        for key, value in block.items()
    }


def _read_thinking(
    payload: Mapping[str, object], context_id: int | None, timestamp: str | None
) -> Thinking:
    """Read reasoning, whose readable part is a summary codex may split."""
    summary_value = payload.get("summary")
    parts: list[str] = []
    templates: list[JSONValue] = []
    order: list[JSONValue] = []
    for value in ListCodec.coerce(summary_value):
        block = DictCodec.coerce(value)
        if isinstance(block.get("text"), str):
            parts.append(StrCodec.coerce(block.get("text")))
            templates.append(_stencil(block, "text"))
            order.append("text")
        else:
            templates.append(cast(JSONValue, value))
            order.append("other")
    encrypted_text = decode_or_none(str, payload.get("encrypted_content"))
    consumed: set[str] = set()
    if isinstance(summary_value, list):
        consumed.add("summary")
    if encrypted_text is not None:
        consumed.add("encrypted_content")
    extra = _codex_residual(payload, consumed)
    if "summary" not in payload:
        extra["$summary_absent"] = True
    elif isinstance(summary_value, list):
        extra["$templates"] = templates
        extra["$order"] = order
    if len(parts) > 1:
        extra["$parts"] = [len(part) for part in parts]
    return Thinking(
        context_id=context_id,
        timestamp=timestamp,
        summary="\n".join(parts) if parts else None,
        encrypted=encrypted_text,
        extra=json_freeze(extra),
    )


def _read_tool_call(
    payload: Mapping[str, object], context_id: int | None, timestamp: str | None
) -> ToolCall | UncategorizedRecord:
    """Read a tool invocation, whose arguments are JSON inside JSON."""
    kind = StrCodec.coerce(payload.get("type"))
    freeform = kind != "function_call"
    consumed: set[str] = set()
    for key in ("call_id", "name"):
        if isinstance(payload.get(key), str):
            consumed.add(key)
    input_key = "input" if freeform else "arguments"
    if isinstance(payload.get(input_key), str):
        consumed.add(input_key)
    extra = _codex_residual(payload, consumed)
    extra["$present"] = list(payload)
    if freeform:
        arguments: dict[str, object] = {"input": StrCodec.coerce(payload.get("input"))}
    else:
        text = StrCodec.coerce(payload.get("arguments"), "{}")
        parsed = _parse_arguments(text)
        if parsed is None and isinstance(payload.get("arguments"), str):
            # The model wrote something that is not JSON. Calling it an empty
            # argument list would replace the call's real input with one it
            # never had.
            return UncategorizedRecord(
                context_id=context_id,
                timestamp=timestamp,
                kind=f"response_item/{kind}",
                payload=json_freeze(payload),
            )
        arguments = parsed or {}
        # ``arguments`` is a STRING inside the line, so its own separator
        # spacing is part of the bytes.
        compact = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        spaced = json.dumps(arguments, ensure_ascii=False)
        if text == spaced:
            extra["$spaced"] = True
        elif text != compact:
            # Neither spelling reproduces it: the model wrote a DUPLICATE key,
            # and parsing kept only the last. The string itself is the value.
            extra["$raw"] = text
    return ToolCall(
        context_id=context_id,
        timestamp=timestamp,
        call_id=StrCodec.coerce(payload.get("call_id")),
        name=StrCodec.coerce(payload.get("name")),
        arguments=json_freeze(DictCodec.coerce(arguments)),
        extra=json_freeze(extra),
    )


def _parse_arguments(text: str) -> dict[str, object] | None:
    """Parse a provider-supplied JSON argument string, or ``None`` if invalid.

    ``arguments`` is JSON nested inside JSON, so a model that writes malformed
    JSON leaves a syntactically valid record carrying an invalid string.
    """
    try:
        return DictCodec.coerce(json.loads(text))
    except json.JSONDecodeError:
        return None


def _read_tool_result(
    payload: Mapping[str, object], context_id: int | None, timestamp: str | None
) -> UncategorizedToolResult:
    """Read what one tool invocation returned.

    Uncategorized by construction: a codex output line says only which call it
    answers, so what the tool DID is knowable only from the call, and axiom 9
    types the record by the act. The ``item_completed`` event is where codex
    reports the act, and that reads into a typed result.
    """
    output = payload.get("output")
    consumed = {"call_id"}
    if isinstance(output, str | list):
        consumed.add("output")
    extra = _codex_residual(payload, consumed)
    attachments: tuple[Attachment, ...] = ()
    if isinstance(output, str):
        content = output
    elif isinstance(output, list):
        output_list = ListCodec.coerce(payload.get("output"))
        parts, attachments, templates = _read_content(output_list)
        content = "\n".join(parts)
        extra["$parts"] = [len(part) for part in parts] or [0]
        extra["$templates"] = templates
        extra["$order"] = _block_order(output_list)
    else:
        content = None
        if "output" not in payload:
            extra["$output_absent"] = True
    return UncategorizedToolResult(
        context_id=context_id,
        timestamp=timestamp,
        call_id=StrCodec.coerce(payload.get("call_id")),
        content=content,
        attachments=attachments,
        extra=json_freeze(extra),
    )


def _read_search_call(
    payload: Mapping[str, object], context_id: int | None, timestamp: str | None
) -> ToolCall:
    """Read a web or tool search as the tool invocation it is."""
    # Both the id and the input go under different keys per search kind: a web
    # search writes ``id``/``action``, a tool search ``call_id``/``arguments``.
    # Which pair held them decides where the writer puts them back.
    # A search sometimes names neither, so ``$id`` may be absent entirely and
    # the writer must not invent an empty one.
    id_key = next((key for key in ("call_id", "id") if key in payload), "")
    arg_key = next((key for key in ("action", "arguments") if key in payload), "")
    consumed = {"type"}
    if isinstance(payload.get(id_key), str):
        consumed.add(id_key)
    if isinstance(payload.get(arg_key), Mapping):
        consumed.add(arg_key)
    return ToolCall(
        context_id=context_id,
        timestamp=timestamp,
        call_id=StrCodec.coerce(payload.get(id_key)),
        name=StrCodec.coerce(payload.get("type")).removesuffix("_call"),
        arguments=json_freeze(DictCodec.coerce(payload.get(arg_key))),
        extra=json_freeze(
            _codex_residual(payload, consumed)
            | {
                "$id": id_key,
                "$args": arg_key,
                "$kind": StrCodec.coerce(payload.get("type")),
            }
        ),
    )


def _read_event(
    kind: str,
    payload: Mapping[str, object],
    context_id: int | None,
    timestamp: str | None,
) -> SessionRecord | list[SessionRecord]:
    """Read one event the CLI reported alongside the transcript.

    Usually one record. A patch touching SEVERAL files is one record per file
    (axiom 10), since ``changes`` is keyed by path and one record could name
    only one of them.
    """
    if kind == "token_count":
        info = _mapping_state(payload, "info")
        rate_limits = _mapping_state(payload, "rate_limits")
        usage_extra = residual(
            payload, {"type"}, fields={"info": info, "rate_limits": rate_limits}
        )
        # Which of the two codex wrote as ``null``. The fields are objects, so
        # a null read back as ``{}`` and the line rewrote as one -- 1033
        # captured rollouts, every first count of a turn.
        nulls = [
            key
            for key, state in (("info", info), ("rate_limits", rate_limits))
            if state is None
        ]
        if nulls:
            usage_extra["$nulls"] = nulls
        # The envelope describes key order and field presence, and BOTH are
        # already known: ``_ORDER["token_count"]`` names the order the writer
        # falls back to, and ``$nulls`` names the field codex wrote as null.
        # A payload that says nothing more than those needs no copy of it --
        # one distinct envelope was stored 490 times on a single captured
        # rollout, 3.6% of that file's whole normalized size.
        if _is_canonical_usage(payload, usage_extra, (info, rate_limits)):
            del usage_extra[_FIELD_STATE_KEY]
        return TokenUsage(
            context_id=context_id,
            timestamp=timestamp,
            info=json_freeze(DictCodec.coerce(info)),
            rate_limits=json_freeze(DictCodec.coerce(rate_limits)),
            extra=json_freeze(usage_extra),
        )
    if kind == "error":
        return SystemMessage(
            context_id=context_id,
            timestamp=timestamp,
            content=decode_or_none(str, payload.get("message")),
            subtype="error",
            # An error is an EVENT, not a response item, so the writer needs
            # to know which outer kind wrote it.
            extra=json_freeze(_codex_residual(payload, {"message"}) | {"$event": True}),
        )
    if kind == "item_completed":
        return _read_completed(payload, context_id, timestamp)
    if kind == "context_compacted":
        # The marker codex writes beside the ``compacted`` line that carries
        # the summary; both name one act, and this one holds no payload.
        return ContextCompaction(
            context_id=context_id,
            timestamp=timestamp,
            extra=json_freeze(_echoing(payload, kind, ())),
        )
    if kind == "mcp_tool_call_end":
        # Nothing here maps to :attr:`content`; the whole payload is the
        # connector's own shape, so it rides in the residual.
        return UncategorizedToolResult(
            context_id=context_id,
            timestamp=timestamp,
            call_id=StrCodec.coerce(payload.get("call_id")),
            extra=json_freeze(_echoing(payload, kind, {"call_id"})),
        )
    # The pre-0.149 spelling of what ``item_completed`` now carries; the act
    # is the same, so it reads into the same record.
    if kind == "exec_command_end":
        shell = ShellCommandResult(
            context_id=context_id,
            timestamp=timestamp,
            call_id=StrCodec.coerce(payload.get("call_id")),
            command=_command(payload.get("command")),
            stdout=StrCodec.coerce(payload.get("stdout")),
            stderr=StrCodec.coerce(payload.get("stderr")),
            exit_code=decode_or_none(int, payload.get("exit_code")),
            extra=json_freeze(_shell_residual(payload, kind)),
        )
        return lift_shell_result(shell) or shell
    if kind == "patch_apply_end":
        changes = DictCodec.coerce(payload.get("changes"))
        edits, counts = _patch_edits(changes)
        return _per_path_edits(
            changes,
            edits,
            counts,
            context_id=context_id,
            timestamp=timestamp,
            call_id=StrCodec.coerce(payload.get("call_id")),
            extra=json_freeze(
                _stencil_changes(_echoing(payload, kind, {"call_id"}), counts)
            ),
        )
    if kind == "web_search_end":
        results_value = payload.get("results")
        values = ListCodec.coerce(results_value)
        rows: list[dict[str, object]] = []
        templates: list[JSONValue] = []
        row_order: list[JSONValue] = []
        for value in values:
            row = DictCodec.coerce(value)
            if isinstance(value, Mapping):
                rows.append(row)
                templates.append(
                    residual(
                        row,
                        fields={
                            "url": take(row, "url", str),
                            "title": take(row, "title", str),
                            "snippet": take(row, "snippet", str),
                        },
                    )
                )
                row_order.append("row")
            else:
                templates.append(json_freeze(value))
                row_order.append("other")
        consumed = {"type"}
        for key in ("call_id", "query"):
            if isinstance(payload.get(key), str):
                consumed.add(key)
        if isinstance(results_value, list):
            consumed.add("results")
        extra = _codex_residual(payload, consumed) | {"$echoes": kind}
        if isinstance(results_value, list):
            extra["$rows"] = templates
            extra["$row_order"] = row_order
        return WebSearchResults(
            context_id=context_id,
            timestamp=timestamp,
            call_id=StrCodec.coerce(payload.get("call_id")),
            # An empty query is a value: 2 captured searches carry one, and
            # ``None`` would drop the key.
            query=StrCodec.coerce(payload.get("query")) if "query" in payload else None,
            content=tuple(_search_rows(rows)),
            extra=json_freeze(extra),
        )
    # ``event_msg/agent_message`` and its siblings are the CLI re-rendering a
    # record that also arrives as a ``response_item``. An echo carries nothing
    # the session does not already hold, so it keeps its bytes rather than
    # entering the IR as a second copy of a turn.
    return UncategorizedRecord(
        context_id=context_id,
        timestamp=timestamp,
        kind=f"event_msg/{kind}",
        payload=json_freeze(payload),
    )


_FIELD_STATE_KEY: Final = "$__custom_json_fields__"
"""Where :func:`residual` records key order and per-field presence."""


_MESSAGE_ROLES: Final = frozenset({"assistant", "developer", "system", "user"})
"""The roles a codex message may carry.

The API's own enum, and what 1449 captured rollouts contain: ``assistant``,
``user``, and ``developer``, plus ``system`` which it accepts. Anything else
is a foreign CLI's subtype and is rejected with ``invalid_enum_value``.
"""


def _is_canonical_usage(
    payload: Mapping[str, object],
    extra: Mapping[str, JSONValue],
    states: Sequence[FieldState[object]],
) -> bool:
    """Whether a usage line's envelope says only what the writer rebuilds.

    True when the line carries no key the record's fields do not hold, every
    field holds a value the field's TYPE can carry, and the order is the one
    :data:`_ORDER` already names. A malformed field -- ``"info":7``, which is
    no object -- survives nowhere but the envelope, so that line keeps it.
    """
    if set(extra) - {_FIELD_STATE_KEY, "$nulls"}:
        return False
    if any(isinstance(state, Invalid | Absent) for state in states):
        return False
    order = payload_orders().get("token_count", ())
    return list(payload) == [key for key in order if key in payload]


def _mapping_state(payload: Mapping[str, object], key: str) -> FieldState[object]:
    """Read one object field without collapsing malformed values."""
    state = take(payload, key, object)
    if state is None or isinstance(state, (Absent, Invalid)):
        return state
    if isinstance(state, Mapping):
        return cast(Mapping[str, object], state)
    return Invalid(raw=cast(JSONValue, state))


def _echoing(
    payload: Mapping[str, object], kind: str, consumed: Iterable[str]
) -> dict[str, JSONValue]:
    """Return a residual that remembers which event spelling wrote it.

    Codex reports the same act under a pre-0.149 ``*_end`` event and under
    ``item_completed``; nothing on the record distinguishes them, so the
    reader records which one it saw.
    """
    return _codex_residual(payload, {"type", *consumed}) | {"$echoes": kind}


def _stencil_changes(
    extra: dict[str, JSONValue], counts: Sequence[int] = ()
) -> dict[str, JSONValue]:
    """Empty each patch entry's diff, which :attr:`edits` now holds.

    A stencil, not a deletion: the per-path structure around the diff is the
    provider's and no field carries it, so the key stays and only its value
    goes. Keeping the value too stored every diff twice -- 100% of one
    captured rollout's 91 KB of diff text, half that file's whole IR growth.

    ``counts`` says how many splices each diff-bearing path contributed, which
    is the only place that boundary survives: one path may hold SEVERAL hunks,
    so neither the splices nor the rendered text reveals where a path ends.
    """
    changes = extra.get("changes")
    if not isinstance(changes, dict):
        return extra
    stencilled: dict[str, JSONValue] = {}
    for path, entry in changes.items():
        found = entry if isinstance(entry, dict) else None
        # Whichever form this entry used, the record's own field now holds it:
        # a diff for an update, the whole content for an add.
        filled = next(
            (
                key
                for key in ("unified_diff", "content")
                if found is not None and isinstance(found.get(key), str)
            ),
            None,
        )
        if found is None or filled is None:
            stencilled[path] = entry
            continue
        # WHICH key the record's field fills. An entry carrying both -- 9 of
        # 451 captured events mix an add with an update -- was stencilled on
        # one and rebuilt from the other, so the diff came back ``null`` and
        # the content was overwritten with the splice's rendering.
        stencilled[path] = {
            key: None if key == filled else value for key, value in found.items()
        } | {"$filled": filled}
    out = extra | {"changes": stencilled}
    if len(counts) > 1:
        out["$splice_counts"] = list(counts)
    return out


def _shell_residual(payload: Mapping[str, object], kind: str) -> dict[str, JSONValue]:
    """Return a shell result's residual without losing malformed fields."""
    consumed = {"type"}
    command_value = payload.get("command")
    if _is_command_shape(command_value):
        consumed.add("command")
    for key, target in (
        ("call_id", str),
        ("stdout", str),
        ("stderr", str),
        ("exit_code", int),
    ):
        if decode_or_none(target, payload.get(key)) is not None:
            consumed.add(key)
    extra = _codex_residual(payload, consumed) | {"$echoes": kind}
    extra["$present"] = list(payload)
    return extra


def _read_completed(
    payload: Mapping[str, object], context_id: int | None, timestamp: str | None
) -> SessionRecord | list[SessionRecord]:
    """Read an ``item_completed`` event by the kind of item it completed.

    Codex 0.149 replaced the per-kind ``*_end`` events with this one, moving
    the discriminator down to ``item.type``.

    A patch is one record per PATH, since ``changes`` is keyed by one and a
    single record could name only one of them.
    """
    item = DictCodec.coerce(payload.get("item"))
    item_type = StrCodec.coerce(item.get("type"))
    outer = _codex_residual(payload, {"type", "item"}) | {"$echoes": "item_completed"}
    if item_type == "CommandExecution":
        consumed = {"type"}
        command_value = item.get("command")
        if _is_command_shape(command_value):
            consumed.add("command")
        for key, target in (
            ("id", str),
            ("stdout", str),
            ("stderr", str),
            ("exit_code", int),
        ):
            if decode_or_none(target, item.get(key)) is not None:
                consumed.add(key)
        nested = _codex_residual(item, consumed)
        nested["$present"] = list(item)
        shell = ShellCommandResult(
            context_id=context_id,
            timestamp=timestamp,
            call_id=StrCodec.coerce(item.get("id")),
            command=_command(item.get("command")),
            stdout=StrCodec.coerce(item.get("stdout")),
            stderr=StrCodec.coerce(item.get("stderr")),
            exit_code=decode_or_none(int, item.get("exit_code")),
            extra=json_freeze(outer | {"item": nested}),
        )
        return lift_shell_result(shell) or shell
    if item_type == "FileChange":
        consumed = {"type"}
        if isinstance(item.get("id"), str):
            consumed.add("id")
        nested = _codex_residual(item, consumed)
        nested["$present"] = list(item)
        changes = DictCodec.coerce(item.get("changes"))
        edits, counts = _patch_edits(changes)
        return _per_path_edits(
            changes,
            edits,
            counts,
            context_id=context_id,
            timestamp=timestamp,
            call_id=StrCodec.coerce(item.get("id")),
            extra=json_freeze(outer | {"item": _stencil_changes(nested, counts)}),
        )
    if item_type == "WebSearch":
        consumed = {"type"}
        for key in ("id", "query"):
            if isinstance(item.get(key), str):
                consumed.add(key)
        nested = _codex_residual(item, consumed)
        nested["$present"] = list(item)
        return WebSearchResults(
            context_id=context_id,
            timestamp=timestamp,
            call_id=StrCodec.coerce(item.get("id")),
            query=decode_or_none(str, item.get("query")),
            extra=json_freeze(outer | {"item": nested}),
        )
    # ``AgentMessage``/``UserMessage``/``Reasoning`` complete an item that
    # also arrives as its own ``response_item``; see the echo note above.
    return UncategorizedRecord(
        context_id=context_id,
        timestamp=timestamp,
        kind=f"event_msg/item_completed/{item_type}",
        payload=json_freeze(payload),
    )


def _patch_edits(changes: Mapping[str, object]) -> tuple[tuple[Splice, ...], list[int]]:
    """Return a patch's replacements, and how many belong to each path.

    Codex reports one entry per path in either of two forms. An UPDATE carries
    a ``unified_diff``; an ADD carries the whole ``content`` -- 82 of 466
    captured entries, and 67 whole events carry nothing else. An add is a pure
    insertion, which the shape already states as an empty ``before``, so it
    needs no field of its own and a mixed event stays one record. Leaving it
    unharvested kept the bytes in the residual, which round-trips to codex and
    so passes every byte check while making the file invisible to a consumer
    that reads the record.

    Each path's diff is parsed ON ITS OWN and the results concatenated:
    joining the texts first and parsing once moved a newline between adjacent
    paths, since the join added a terminator the last line did not have.

    The per-path COUNTS come back too, because one path may contribute several
    splices -- a diff with two hunks is one file, not two -- so the boundary
    cannot be recovered from the splices alone.

    Args:
      changes: A patch's per-path entries.

    Returns:
      edits: Every replacement, in the order the paths were written.
      counts: How many of them each diff-bearing path contributed.

    """
    out: list[Splice] = []
    counts: list[int] = []
    for value in changes.values():
        entry = DictCodec.coerce(value)
        content = entry.get("content")
        if entry.get("unified_diff") is not None:
            found = parse_udiff(StrCodec.coerce(entry.get("unified_diff")))
            out.extend(found)
            counts.append(len(found))
        elif isinstance(content, str):
            out.append(Splice(before="", after=content))
            counts.append(1)
    return (tuple(out), counts)


def _per_path_edits(
    changes: Mapping[str, object],
    edits: Sequence[Splice],
    counts: Sequence[int],
    *,
    context_id: int | None,
    timestamp: str | None,
    call_id: str,
    extra: JSON,
) -> list[SessionRecord]:
    """Return one edit record per path the patch changed.

    ``changes`` is keyed BY path, so a record naming none describes an edit to
    no file -- and folding several paths' splices into one sequence loses which
    belongs to which. The line's residual rides on the FIRST record (axiom 10),
    so the writer rebuilds one event from the group rather than one per record.
    """
    paths = [
        path
        for path, entry in changes.items()
        if isinstance(DictCodec.coerce(entry).get("unified_diff"), str)
        or isinstance(DictCodec.coerce(entry).get("content"), str)
    ]
    out: list[SessionRecord] = []
    at = 0
    for index, path in enumerate(paths):
        count = counts[index] if index < len(counts) else 0
        found = tuple(edits[at : at + count])
        # Only the first: one line, one residual (axiom 10).
        own = extra if index == 0 else json_freeze({})
        # A write ONLY when content is the form the entry used. An entry
        # carrying both keys is an update whose diff the reader took, and
        # typing it by key presence made the writer fill the wrong one.
        entry = DictCodec.coerce(changes.get(path))
        added = not isinstance(entry.get("unified_diff"), str) and isinstance(
            entry.get("content"), str
        )
        out.append(
            # An ADD states the file's whole bytes, which is what a write is --
            # and codex's own writer maps a write back to this entry. Typing it
            # as an edit left the IR unable to round-trip one.
            FileWriteResult(
                context_id=context_id,
                timestamp=timestamp,
                call_id=call_id,
                path=path,
                content=found[0].after if found else None,
                extra=own,
            )
            if added
            else FileEditResult(
                context_id=context_id,
                timestamp=timestamp,
                call_id=call_id,
                path=path,
                edits=found,
                extra=own,
            )
        )
        at += count
    return out or [
        FileEditResult(
            context_id=context_id,
            timestamp=timestamp,
            call_id=call_id,
            extra=extra,
        )
    ]


def _search_rows(rows: Sequence[Mapping[str, object]]) -> list[WebSearchResult]:
    """Read a search's result rows."""
    return [
        WebSearchResult(
            url=decode_or_none(str, row.get("url")),
            title=decode_or_none(str, row.get("title")),
            snippet=decode_or_none(str, row.get("snippet")),
        )
        for row in rows
    ]


def _read_attachment(part: Mapping[str, object]) -> Attachment | None:
    """Read an inline image, which codex writes as a data URL."""
    if StrCodec.coerce(part.get("type")) != "input_image":
        return None
    header, separator, data = StrCodec.coerce(part.get("image_url")).partition(",")
    if (
        not separator
        or not header.startswith("data:")
        or not header.endswith(";base64")
    ):
        return None
    try:
        decoded = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return None
    return Attachment(
        mime_descriptor=header.removeprefix("data:").removesuffix(";base64"),
        data=decoded,
    )


def denormalize(records: Iterable[SessionRecord], stream: TextIO) -> None:
    """Denormalize records as Codex rollout JSONL.

    Args:
      records: Provider-neutral records, in stream order.
      stream: Destination text stream.

    """
    # The state records the writer needs before it can emit anything: the
    # launch settings, and the prompt the opening clear states. Both are at
    # the head of the stream by the grammar, so this reads a BOUNDED prefix
    # rather than the file (axiom 11).
    ordered = list(records)
    # The one CARRYING a launch payload, not merely the first: a rollout whose
    # first line is blank states its encoding before it declares itself, so the
    # opening context is superseded rather than being the declaration.
    settings = next(
        (
            record
            for record in ordered
            if isinstance(record, TurnContext) and "payload" in record.extra
        ),
        TurnContext(),
    )
    declaration = dict(json_unfreeze(settings.extra))
    encoding = dict(
        json_unfreeze(
            next(
                (
                    record.encoding
                    for record in ordered
                    if isinstance(record, TurnContext)
                ),
                json_freeze({}),
            )
        )
    )
    for record in ordered:
        # A later context supersedes: whether the file ended on a newline is
        # knowable only at EOF, so the reader restates it there.
        if isinstance(record, TurnContext) and "newline_terminated" in record.encoding:
            encoding = dict(json_unfreeze(record.encoding))
    # Numbering lines is a per-file convention, and the launch line is the one
    # that shows it: a file whose ``session_meta`` carries an ordinal numbers
    # every line, one whose does not numbers none.
    numbered = "ordinal" in declaration
    at = IntCodec.coerce(declaration.get("line"), 0)
    # One line held back, never the file (axiom 11): the last line may have to
    # lose its newline, which is knowable only once the stream ends, and a 273
    # MB session buffered whole cost 2.6 GB since one non-ASCII line widens the
    # entire buffer. Everything before the held line is already written.
    held: str | None = None
    written = 0
    launch = (
        _write_line(
            declaration.get("$launch_timestamp_raw", settings.timestamp),
            IntCodec.coerce(declaration.get("ordinal"), 0) if numbered else None,
            "session_meta",
            # The prompt lives on the opening clear, so the launch payload is
            # rebuilt from it rather than holding a second copy.
            _with_instructions(
                DictCodec.coerce(declaration.get("payload", {})), ordered
            ),
            template=DictCodec.coerce(declaration.get("$outer")),
            include_timestamp=bool(declaration.get("$timestamp", True)),
            payload_at=decode_or_none(int, declaration.get("$payload_at")),
        )
        if "payload" in declaration
        else None
    )
    ordinal = IntCodec.coerce(declaration.get("ordinal"), 0) + 1 if numbered else 0
    for group in _grouped(ordered):
        item = group[0]
        # The launch line goes back where it sat, which is after any blank
        # line the file opened with.
        if written == at and launch is not None:
            if held is not None:
                _ = stream.write(held)
            held = launch
            written += 1
            launch = None
        if isinstance(item, IncompleteRecord):
            if held is not None:
                _ = stream.write(held)
            held = item.text
            written += 1
            ordinal += 1
            continue
        line_state, wire_item = _pop_line_state(item)
        assert not isinstance(wire_item, IncompleteRecord)
        template = DictCodec.coerce(line_state.get("outer"))
        raw_timestamp = template.get("timestamp")
        timestamp = (
            raw_timestamp
            if wire_item.timestamp is None
            and raw_timestamp is not None
            and not isinstance(raw_timestamp, str)
            else wire_item.timestamp
        )
        include_timestamp = (
            "timestamp" in template if line_state else wire_item.timestamp is not None
        )
        payload_at = line_state.get("payload_at")
        for outer, payload in _write_record(wire_item, group[1:]):
            if held is not None:
                _ = stream.write(held)
            held = _write_line(
                timestamp,
                ordinal if numbered else None,
                outer,
                payload,
                template=template,
                include_timestamp=include_timestamp,
                payload_at=decode_or_none(int, payload_at),
            )
            written += 1
            ordinal += 1
    if launch is not None:
        # A launch line whose recorded position sits past every record, which
        # is how a rollout that opens with nothing but its declaration reads.
        if held is not None:
            _ = stream.write(held)
        held = launch
    if held is None:
        return
    _ = stream.write(
        held.removesuffix("\n") if encoding.get("newline_terminated") is False else held
    )


def _with_instructions(
    payload: Mapping[str, object], records: Sequence[SessionRecord]
) -> dict[str, object]:
    """Return the launch payload with its prompt restored from the clear.

    The reader emptied ``base_instructions`` because the opening
    :class:`ContextClear` states it, so an edited prompt reaches the wire from
    the record rather than from a stored copy. Only the FIRST clear answers:
    a later one is a ``/new``, which the launch line never described.

    Only the launch line's OWN share of it. The clear states everything the
    fresh context was given, and codex keeps sending instructions after the
    launch line -- writing the whole assembled prompt back put the skills
    block on a line codex never wrote it on.
    """
    if "base_instructions" not in payload:
        return dict(payload)
    # ``None`` is the EMPTIED slot, not an absent key: the reader stencils a
    # string prompt to null the way it stencils every other held value, so
    # keying on the value skipped exactly the form it had emptied.
    stencil = payload["base_instructions"]
    opening = next(
        (record for record in records if isinstance(record, ContextClear)), None
    )
    if opening is None or opening.system_prompt is None:
        return dict(payload)
    declared = opening.system_prompt[
        : IntCodec.coerce(
            dict(json_unfreeze(opening.extra)).get("$declared"),
            len(opening.system_prompt),
        )
    ]
    if isinstance(stencil, Mapping):
        held = cast(Mapping[str, object], stencil)
        filled: object = {
            key: declared if key == "text" else value for key, value in held.items()
        }
    else:
        filled = declared
    return {
        key: filled if key == "base_instructions" else value
        for key, value in payload.items()
    }


def _grouped(records: Sequence[SessionRecord]) -> Iterator[Sequence[SessionRecord]]:
    """Yield the records of each line, grouped as the line they came from.

    A patch touching several files reads as one record per file, and they came
    off ONE ``changes`` map -- so they are written back as one event rather
    than each emitting one codex never wrote. A follower is recognized by
    carrying the same call id and no residual of its own, which is what axiom
    10 makes the mark of a record that did not lead its line.

    Yielded, never accumulated (axiom 11): returning one list per record built
    a structure proportional to the session -- measured at ~73 bytes per
    record, 15096 / 29496 / 58776 bytes for 200 / 400 / 800 -- which is the
    whole-file hold a writer is not allowed. Only a patch's followers share a
    line, so the open group is bounded by the paths one event touched.
    """
    open_group: list[SessionRecord] = []
    for record in records:
        leader = open_group[0] if open_group else None
        if isinstance(record, ContextClear) and isinstance(leader, ContextCompaction):
            # The window a compaction opened: one ``compacted`` line states
            # both, so they are written from one group.
            open_group.append(record)
            continue
        if isinstance(record, FileEditResult | FileWriteResult) and isinstance(
            leader, FileEditResult | FileWriteResult
        ):
            # ``$codex_line`` is this reader's own note about the line, added
            # after the split, so a follower carries it and nothing else.
            own = {key for key in record.extra if key != "$codex_line"}
            if record.call_id and record.call_id == leader.call_id and not own:
                open_group.append(record)
                continue
        if open_group:
            yield open_group
        open_group = [record]
    if open_group:
        yield open_group


def _write_line(
    timestamp: object,
    ordinal: int | None,
    outer: str,
    payload: Mapping[str, object],
    *,
    template: Mapping[str, object],
    include_timestamp: bool = True,
    payload_at: int | None = None,
) -> str:
    """Return one rollout line, in the order codex writes its outer keys.

    ``payload_at`` is where the payload key sat among the others; the template
    holds every other key, since the payload is the record itself.
    """
    record: dict[str, object] = dict(json_unfreeze(template))
    if include_timestamp:
        if not template or "timestamp" in record:
            record["timestamp"] = timestamp
    else:
        record.pop("timestamp", None)
    if ordinal is not None and not template:
        record["ordinal"] = ordinal
    if not template or isinstance(record.get("type"), str):
        record["type"] = outer
    if payload_at is None:
        record["payload"] = dict(payload)
    else:
        keys = list(record)
        record = {
            key: dict(payload) if key == "payload" else record[key]
            for key in (*keys[:payload_at], "payload", *keys[payload_at:])
        }
    return json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"


def _codex_residual(
    source: Mapping[str, object], consumed: Iterable[str] = ()
) -> dict[str, JSONValue]:
    """Preserve residual fields while escaping provider dollar keys.

    The line's key order is stored only when :data:`_ORDER` cannot reproduce
    it. The table is what the writer falls back to, so a line agreeing with it
    needs no copy -- 425 records on one captured rollout stored 13 distinct
    orders between them, 1.5% of the whole normalized size.
    """
    extra = residual(source, consumed)
    dollar = {key: extra.pop(key) for key in tuple(extra) if key.startswith("$")}
    if dollar:
        extra["$wire"] = {"order": list(source), "values": dollar}
    if list(source) != _canonical_order(source):
        extra["$native_order"] = list(source)
    return extra


def _canonical_order(source: Mapping[str, object]) -> list[str]:
    """Return the key order :data:`_ORDER` reproduces for this payload.

    The writer's own rule, so the reader can ask whether storing the line's
    order would say anything the table does not: table keys the payload has,
    then anything the table never named, which is how :func:`_ordered` splices.
    """
    order = payload_orders().get(StrCodec.coerce(source.get("type")), ())
    return [key for key in order if key in source] + [
        key for key in source if key not in order
    ]


def _ordered(kind: str, payload: Mapping[str, object]) -> dict[str, object]:
    """Return one payload in its native or canonical key order."""
    values = dict(payload)
    native_order = ListCodec.coerce(values.pop("$native_order", []), str)
    wire = DictCodec.coerce(values.pop("$wire", {}))
    wire_values = DictCodec.coerce(wire.get("values"))
    values = {key: value for key, value in values.items() if not key.startswith("$")}
    values.update(wire_values)
    if not native_order:
        native_order = ListCodec.coerce(wire.get("order"), str)
    if native_order:
        return {key: values[key] for key in native_order if key in values} | {
            key: value for key, value in values.items() if key not in native_order
        }
    order = payload_orders().get(kind, ())
    return {key: values[key] for key in order if key in values} | {
        key: value for key, value in values.items() if key not in order
    }


def _write_record(
    item: SessionRecord, followers: Sequence[SessionRecord] = ()
) -> list[tuple[str, dict[str, object]]]:
    """Return the rollout lines one record becomes.

    ``followers`` are the other records off the same line -- one patch event
    reads as one record per path -- so the writer fills every entry of the one
    ``changes`` map rather than emitting an event per record.
    """
    if isinstance(item, TurnContext):
        if "$codex_line" not in item.extra and "payload" in item.extra:
            # The LAUNCH settings, which ``denormalize`` already wrote as the
            # ``session_meta`` line. Codex states them once; emitting a
            # ``turn_context`` here too would write the declaration twice.
            return []
        if not item.extra and item.encoding:
            # A context stating only how the file was encoded -- the opening
            # one before a launch line supersedes it, and the EOF restatement.
            # Neither is a settings line codex ever wrote.
            return []
        return [("turn_context", _write_context(item))]
    if isinstance(item, UncategorizedRecord):
        # Only a record THIS reader wrote replays verbatim: its payload is a
        # codex payload. Another provider's is that provider's bytes, and
        # emitting it fabricated a codex line of a type codex never writes --
        # a claude ``queue-operation`` became a codex outer record.
        #
        # Provenance is in the kind: codex namespaces its own
        # (``event_msg/task_started``), claude names a bare line type. The
        # mirror of the guard the claude writer already applies.
        if "/" not in item.kind:
            return []
        outer, _, _ = item.kind.partition("/")
        return [(outer, dict(json_unfreeze(item.payload)))]
    if isinstance(item, ContextState):
        return [("world_state", _ordered("world_state", json_unfreeze(item.extra)))]
    if isinstance(item, ContextCompaction):
        extra = dict(json_unfreeze(item.extra))
        echoes = StrCodec.coerce(extra.pop("$echoes", ""))
        if echoes:
            # The bare marker, not the line that carries the summary.
            return [("event_msg", _ordered(echoes, {"type": echoes, **extra}))]
        # The window it opened follows it and holds what the line states, so
        # the two records rebuild ONE ``compacted`` line.
        window = next(
            (record for record in followers if isinstance(record, ContextClear)),
            ContextClear(),
        )
        return [("compacted", _write_compacted(window, extra))]
    if isinstance(item, ContextClear):
        # Derived: a window opens with one whatever caused it, and the line
        # that caused it -- a launch or a compaction -- already wrote it.
        return []
    if isinstance(item, TokenUsage):
        stored = dict(json_unfreeze(item.extra))
        nulls = set(ListCodec.coerce(stored.pop("$nulls", []), str))
        values: dict[str, object] = {
            # ``None``, not the empty object the field holds: an object field
            # cannot tell a null apart from an absent one.
            "info": None if "info" in nulls else json_unfreeze(item.info),
            "rate_limits": None
            if "rate_limits" in nulls
            else json_unfreeze(item.rate_limits),
        }
        payload = (
            replay(stored, values)
            if _FIELD_STATE_KEY in stored
            # No envelope means the line said nothing the table does not, so
            # the fields ARE the payload -- in the order the table names, which
            # is where a null field sat too.
            else {
                key: values[key]
                for key in payload_orders().get("token_count", ())
                if key in values and (values[key] is not None or key in nulls)
            }
        )
        return [
            (
                "event_msg",
                _ordered(
                    "token_count",
                    _codex_residual({"type": "token_count", **payload}),
                ),
            )
        ]
    if isinstance(item, ToolResult):
        result = _write_result(item, followers)
        return [result] if result is not None else []
    if isinstance(item, AgentToAgentMessage):
        return [("response_item", _write_agent_message(item))]
    if isinstance(item, SystemMessage) and "$event" in item.extra:
        extra = dict(json_unfreeze(item.extra))
        del extra["$event"]
        return [
            (
                "event_msg",
                _ordered("error", {"type": "error", "message": item.content} | extra),
            )
        ]
    if isinstance(
        item, UserMessage | AssistantMessage | SystemMessage | Thinking | ToolCall
    ):
        return [("response_item", _write_item(item))]
    # A record with no Codex representation -- a fusion boundary, another
    # CLI's state. Dropped here and reported by the converter's loss summary.
    return []


def _write_compacted(
    item: ContextClear, extra: dict[str, MutableJSONValue]
) -> dict[str, object]:
    """Return the ``compacted`` line a history replacement came from.

    ``item`` is the WINDOW the compaction opened, not the event: the summary
    and the surviving turns are what the next context begins with, so that is
    where they live. The event supplies only the line's own residual, which is
    what ``extra`` holds.

    The kept turns are the window's own :attr:`history`, written back through
    the ordinary item writer rather than from a stored copy -- which is what
    lets an edited history reach the wire.
    """
    markers = ListCodec.coerce(extra.pop("$history", []))
    stated = "$history" in extra or bool(markers) or bool(item.history)
    payload: dict[str, object] = {}
    if (item.summary is not None and "message" in extra) or not stated:
        # ``message`` only where the line stated one. Codex seals the summary
        # in the trailing marker instead, and writing it to both would invent a
        # key 8 of 8 captured lines leave empty.
        payload["message"] = item.summary
    if stated:
        payload["message"] = payload.get("message", "")
        entries: list[object] = [
            _write_record(record)[0][1]
            for record in item.history
            if _write_record(record)
        ]
        entries.extend(json_unfreeze(marker) for marker in markers)
        payload["replacement_history"] = entries
    return _ordered("compacted", payload | extra)


def _write_context(item: TurnContext) -> dict[str, object]:
    """Return the settings line a turn context came from."""
    stored = dict(json_unfreeze(item.extra))
    # The word this line used for its effort, when it was not the IR's own.
    spelled = stored.pop("$effort", None)
    values: dict[str, object] = {
        "model": item.model,
        "approval_policy": item.permission,
    }
    if item.effort is not None:
        values["effort"] = (
            spelled
            if isinstance(spelled, str) and _effort(spelled) == item.effort
            else item.effort
        )
    if item.summary_kind is not None:
        values["summary"] = item.summary_kind
    payload = replay(stored, values)
    # A record built by hand carries no stored order, so a field the replay
    # envelope never named is appended rather than dropped.
    for key, value in values.items():
        if key not in payload and value is not None and key in {"effort", "summary"}:
            payload[key] = value
    return _ordered("turn_context", _codex_residual(payload))


def _write_agent_message(item: AgentToAgentMessage) -> dict[str, object]:
    """Return the response item a peer message came from."""
    extra = dict(json_unfreeze(item.extra))
    blocks = _write_content(
        item.content, item.attachments, extra, text_key="input_text"
    )
    payload: dict[str, object] = {
        "type": "agent_message",
        "author": item.sender,
        "recipient": item.recipient,
        "content": blocks,
        **extra,
    }
    return _ordered(
        "agent_message", {k: v for k, v in payload.items() if v is not None}
    )


def _write_item(
    item: UserMessage | AssistantMessage | SystemMessage | Thinking | ToolCall,
) -> dict[str, object]:
    """Return the response item one act of the transcript came from."""
    extra = dict(json_unfreeze(item.extra))
    if isinstance(item, Thinking):
        # Reasoning has no content list, so its own splits stay here rather
        # than going through the content writer.
        summary_absent = bool(extra.pop("$summary_absent", False))
        summary = _write_content(
            item.summary if item.summary is not None else item.content,
            (),
            extra,
            text_key="summary_text",
        )
        return _ordered(
            "reasoning",
            {
                "type": "reasoning",
                **({} if summary_absent and not summary else {"summary": summary}),
                **(
                    {"encrypted_content": item.encrypted}
                    if item.encrypted is not None
                    else {}
                ),
                **extra,
            },
        )
    if isinstance(item, ToolCall):
        return _write_call(item, extra)
    role = (
        "user"
        if isinstance(item, UserMessage)
        else "assistant"
        if isinstance(item, AssistantMessage)
        # A subtype outside the roles codex writes is another CLI's word, and
        # sending it is a 400 rather than a stored oddity: claude's
        # ``turn_duration`` crossed through as a role and failed the resumed
        # session's FIRST request.
        else item.subtype
        if item.subtype in _MESSAGE_ROLES
        else "system"
    )
    content_absent = bool(extra.pop("$content_absent", False))
    blocks = _write_content(
        item.content,
        item.attachments,
        extra,
        text_key="output_text" if role == "assistant" else "input_text",
    )
    payload: dict[str, object] = {"type": "message", "role": role}
    if blocks or not content_absent:
        payload["content"] = blocks
    return _ordered("message", payload | extra)


def _write_call(
    item: ToolCall, extra: dict[str, MutableJSONValue]
) -> dict[str, object]:
    """Return the response item a tool invocation came from."""
    spaced = bool(extra.pop("$spaced", False))
    present_value = extra.pop("$present", None)
    present = (
        set(ListCodec.coerce(present_value, str)) if present_value is not None else None
    )
    kind = StrCodec.coerce(extra.get("type"), "function_call")
    if "$id" in extra:
        id_key = StrCodec.coerce(extra.pop("$id"))
        arg_key = StrCodec.coerce(extra.pop("$args"))
        search = StrCodec.coerce(extra.pop("$kind"), "web_search_call")
        return _ordered(
            search,
            {"type": search}
            | ({id_key: item.call_id} if id_key else {})
            | ({arg_key: json_unfreeze(item.arguments)} if arg_key else {})
            | extra,
        )
    if kind == "function_call":
        payload: dict[str, object] = {
            "name": item.name,
            # The source string when re-serializing cannot reproduce it, else
            # raw UTF-8 matching the outer line: an em dash inside the nested
            # argument string is written as itself, not escaped.
            "arguments": StrCodec.coerce(extra.pop("$raw"))
            if "$raw" in extra
            else json.dumps(
                json_unfreeze(item.arguments),
                ensure_ascii=False,
                separators=None if spaced else (",", ":"),
            ),
            "call_id": item.call_id,
        }
    else:
        payload = {
            "call_id": item.call_id,
            "name": item.name,
            "input": StrCodec.coerce(item.arguments.get("input")),
        }
    if present is not None:
        defaults = {
            "call_id": item.call_id == "",
            "name": item.name == "",
            "arguments": not item.arguments,
            "input": not item.arguments.get("input"),
        }
        payload = {
            key: value
            for key, value in payload.items()
            if key in present or not defaults.get(key, False)
        }
    return _ordered(kind, {"type": kind} | payload | extra)


def _write_result(
    item: ToolResult, followers: Sequence[SessionRecord] = ()
) -> tuple[str, dict[str, object]] | None:
    """Return the line a tool's answer came from.

    Which of codex's several spellings wrote it is on the record: an output
    response item, a pre-0.149 ``*_end`` event, or an ``item_completed``.
    """
    if isinstance(item, FileReadResult | FileWriteResult | FileEditResult):
        item = shell_result_for_replay(item) or item
    by_path = {
        record.path: record
        for record in (item, *followers)
        if isinstance(record, FileEditResult | FileWriteResult) and record.path
    }
    extra = dict(json_unfreeze(item.extra))
    echoes = StrCodec.coerce(extra.pop("$echoes", ""))
    if echoes == "item_completed":
        completed = _write_completed(item, extra, by_path)
        return ("event_msg", completed) if completed is not None else None
    if echoes:
        return ("event_msg", _write_legacy_end(item, echoes, extra, by_path))
    # An output response item is the only spelling that carries the tool's own
    # text, and only an uncategorized result read one; a typed result came
    # from an event and took one of the branches above.
    if isinstance(item, UncategorizedToolResult):
        return ("response_item", _write_output(item, extra))
    if isinstance(
        item,
        ShellCommandResult
        | FileReadResult
        | FileWriteResult
        | FileEditResult
        | WebSearchResults,
    ):
        completed = _write_completed(item, extra, by_path)
        return ("event_msg", completed) if completed is not None else None
    # A fetch or a subagent report: acts codex has no event for -- across 400
    # captured rollouts it writes neither -- but which it CAN carry, since
    # every tool it does not model answers with a plain output item. Returning
    # None instead dropped the record, so an act the source recorded left no
    # trace at all; a generic output states what the tool returned, which is
    # the most the target's vocabulary can say.
    if isinstance(item, WebFetchResult | AgentStatusResult):
        payload: dict[str, object] = {
            "type": "function_call_output",
            "call_id": item.call_id,
        }
        # Axiom 2: an UNSET content is not an empty one. Codex already states
        # an absent ``output`` -- the reader records that state -- so a result
        # that returned nothing omits the key rather than claiming it returned
        # the empty string.
        if item.content is not None:
            payload["output"] = item.content
        return ("response_item", _ordered("function_call_output", payload | extra))
    return None


def _write_output(
    item: UncategorizedToolResult, extra: dict[str, MutableJSONValue]
) -> dict[str, object]:
    """Return the output response item a result came from."""
    kind = StrCodec.coerce(extra.get("type"), "function_call_output")
    if extra.pop("$whole", False):
        return _ordered(kind, {"type": kind, "call_id": item.call_id, **extra})
    output_absent = bool(extra.pop("$output_absent", False))
    output: object = item.content or ""
    if "$parts" in extra:
        output = _write_content(
            item.content, item.attachments, extra, text_key="input_text"
        )
    payload: dict[str, object] = {"type": kind, "call_id": item.call_id}
    if not output_absent or item.content is not None or item.attachments:
        payload["output"] = output
    return _ordered(kind, payload | extra)


def _write_legacy_end(
    item: ToolResult,
    kind: str,
    extra: dict[str, MutableJSONValue],
    by_path: Mapping[str, FileEditResult | FileWriteResult],
) -> dict[str, object]:
    """Return the pre-0.149 ``*_end`` event an observation came from."""
    present_value = extra.pop("$present", None)
    present = (
        set(ListCodec.coerce(present_value, str)) if present_value is not None else None
    )
    payload: dict[str, object] = {"type": kind, "call_id": item.call_id}
    if isinstance(item, ShellCommandResult):
        payload |= {
            "command": list(item.command) if item.command else None,
            "stdout": item.stdout,
            "stderr": item.stderr,
            "exit_code": item.exit_code,
        }
        if present is not None:
            defaults = {
                "call_id": item.call_id == "",
                "command": item.command is None,
                "stdout": item.stdout == "",
                "stderr": item.stderr == "",
                "exit_code": item.exit_code is None,
            }
            payload = {
                key: value
                for key, value in payload.items()
                if key == "type" or key in present or not defaults.get(key, False)
            }
    elif isinstance(item, WebSearchResults):
        payload["query"] = item.query
        payload["results"] = _write_rows(item, extra)
    elif isinstance(item, FileEditResult | FileWriteResult):
        # The residual holds the per-path structure, but the VALUE is the
        # record's own field -- an edit's splices, a write's content -- so an
        # edited one has to reach the entry it came from.
        _ = extra.pop("$splice_counts", None)
        changes = _write_changes(by_path, DictCodec.coerce(extra.get("changes")))
        if changes is not None:
            extra["changes"] = changes
    return _ordered(kind, {k: v for k, v in payload.items() if v is not None} | extra)


def _write_changes(
    by_path: Mapping[str, FileEditResult | FileWriteResult],
    stored: Mapping[str, object],
) -> dict[str, MutableJSONValue] | None:
    """Return a patch's per-path entries carrying each record's current value.

    Codex writes one entry per path and one record answers for one path -- an
    UPDATE carries its splices, an ADD the whole content it wrote -- so no
    boundary has to be recovered: each record renders its own entry.
    """
    if not stored:
        return None
    # WHICH key the record's field fills, as the reader recorded it. Guessing
    # by key presence picked the wrong one for an entry carrying both: the
    # reader emptied ``unified_diff`` and the writer rebuilt ``content``, so
    # the diff came back null and the content was overwritten.
    filled = {
        path: found
        for path, entry in stored.items()
        if (found := StrCodec.coerce(DictCodec.coerce(entry).get("$filled")))
    }
    paths = list(filled)
    if not paths:
        return None
    out: dict[str, MutableJSONValue] = {}
    for path, entry in stored.items():
        found = json_unfreeze(json_freeze(DictCodec.coerce(entry)))
        owner = by_path.get(path)
        if isinstance(found, dict) and path in filled and owner is not None:
            # Each entry from the record that owns THAT path: an add states the
            # file's whole bytes, which a write holds outright; an update
            # states a diff, which its splices render.
            found[filled[path]] = (
                (owner.content or "")
                if isinstance(owner, FileWriteResult)
                else render_udiff(owner.edits)
            )
        if isinstance(found, dict):
            # This reader's own note about the entry's shape, not a key codex
            # wrote. ``_ordered`` strips ``$`` keys at the payload's top level;
            # this one is nested inside ``changes``.
            found.pop("$filled", None)
        out[path] = found
    return out


def _write_completed(
    item: ToolResult,
    extra: dict[str, MutableJSONValue],
    by_path: Mapping[str, FileEditResult | FileWriteResult] = MappingProxyType({}),
) -> dict[str, object] | None:
    """Return the ``item_completed`` event an observation came from."""
    nested = DictCodec.coerce(extra.pop("item", {}))
    present_value = nested.pop("$present", None)
    present = (
        set(ListCodec.coerce(present_value, str)) if present_value is not None else None
    )
    inner: dict[str, object] = {"type": "", "id": item.call_id}
    if isinstance(item, ShellCommandResult):
        kind = "CommandExecution"
        inner |= {
            "type": kind,
            "command": list(item.command) if item.command else None,
            "stdout": item.stdout,
            "stderr": item.stderr,
            "exit_code": item.exit_code,
        }
    elif isinstance(item, FileReadResult):
        # Codex has no read TOOL -- across 400 captured rollouts it names only
        # exec_command, apply_patch, and write_stdin -- so it reads by running
        # one, and that is the act this record crosses as (axiom 9: the same
        # act keeps its meaning in the target's own vocabulary). Inventing a
        # codex "FileRead" would put a shape on the wire codex never writes.
        kind = "CommandExecution"
        inner |= {
            "type": kind,
            # ``--`` first: a file whose name begins with a dash is an OPERAND,
            # and ``cat -n`` runs a flag instead -- the act crossed as a command
            # that reads nothing, and the record lost its type coming back.
            "command": ["/bin/cat", "--", item.path] if item.path else None,
            "stdout": item.content or "",
            "stderr": "",
            "exit_code": 0,
        }
    elif isinstance(item, FileEditResult | FileWriteResult):
        # One act per path, and codex states both under ``changes``: an update
        # writes a diff, an add the file's whole bytes.
        kind = "FileChange"
        inner["type"] = kind
        if "changes" in nested:
            # The residual holds the per-path structure, but the VALUE is the
            # record's own field, so an edited one has to reach its entry.
            _ = nested.pop("$splice_counts", None)
            changes = _write_changes(by_path, DictCodec.coerce(nested.get("changes")))
            if changes is not None:
                nested["changes"] = changes
        elif item.path is not None:
            inner["changes"] = {
                item.path: {"content": item.content or ""}
                if isinstance(item, FileWriteResult)
                else {"unified_diff": render_udiff(item.edits)}
            }
        elif isinstance(item, FileEditResult) and item.edits:
            return None
    elif isinstance(item, WebSearchResults):
        kind = "WebSearch"
        inner |= {"type": kind, "query": item.query}
    else:
        return None
    if present is not None:
        defaults = {
            "id": item.call_id == "",
            "command": isinstance(item, ShellCommandResult) and item.command is None,
            "stdout": isinstance(item, ShellCommandResult) and item.stdout == "",
            "stderr": isinstance(item, ShellCommandResult) and item.stderr == "",
            "exit_code": isinstance(item, ShellCommandResult)
            and item.exit_code is None,
            "query": isinstance(item, WebSearchResults) and item.query is None,
        }
        inner = {
            key: value
            for key, value in inner.items()
            if key == "type" or key in present or not defaults.get(key, value is None)
        }
    payload = {key: value for key, value in inner.items() if value is not None} | nested
    return _ordered(
        "item_completed",
        {"type": "item_completed", "item": _ordered(kind, payload), **extra},
    )


def _write_rows(
    item: WebSearchResults, extra: dict[str, MutableJSONValue]
) -> list[object] | None:
    """Return search results with opaque members in their original positions."""
    if "$rows" not in extra:
        return None
    templates = ListCodec.coerce(extra.pop("$rows", []))
    order = ListCodec.coerce(extra.pop("$row_order", []), str)
    named = [
        {"url": row.url, "title": row.title, "snippet": row.snippet}
        for row in item.content
    ]
    out: list[object] = []
    row_index = 0
    for index, kind in enumerate(order):
        template: object = templates[index] if index < len(templates) else {}
        if kind == "row":
            if row_index >= len(named):
                continue
            out.append(replay(DictCodec.coerce(template), named[row_index]))
            row_index += 1
        elif index < len(templates):
            out.append(json_unfreeze(template))
    out.extend(
        {key: value for key, value in row.items() if value is not None}
        for row in named[row_index:]
    )
    return out


def _write_content(
    content: str | None,
    attachments: tuple[Attachment, ...],
    extra: dict[str, MutableJSONValue],
    *,
    text_key: str,
) -> list[object]:
    """Return content members in native order, then append new semantics."""
    prose = _split(content, ListCodec.coerce(extra.pop("$parts", []), int))
    if "$templates" in extra:
        templates = ListCodec.coerce(extra.pop("$templates"))
    else:
        templates = ListCodec.coerce(extra.pop("$blocks", []))
    has_order = "$order" in extra
    order = ListCodec.coerce(extra.pop("$order", []), str)
    if not has_order:
        order = ["text"] * len(prose)
        order.extend(["image"] * len(attachments))
        order.extend(["other"] * len(templates))
    blocks: list[object] = []
    text_index = 0
    image_index = 0
    for index, kind in enumerate(order):
        template: object = {}
        if index < len(templates):
            template = templates[index]
        if kind == "text":
            if text_index >= len(prose):
                continue
            block = DictCodec.coerce(template)
            block.setdefault("type", text_key)
            block["text"] = prose[text_index]
            blocks.append(block)
            text_index += 1
        elif kind == "image":
            if image_index >= len(attachments):
                continue
            block = DictCodec.coerce(template) | _write_attachment(
                attachments[image_index]
            )
            blocks.append(block)
            image_index += 1
        elif index < len(templates):
            blocks.append(json_unfreeze(template))
    blocks.extend({"type": text_key, "text": part} for part in prose[text_index:])
    blocks.extend(_write_attachment(image) for image in attachments[image_index:])
    return blocks


def _write_attachment(attachment: Attachment) -> dict[str, object]:
    """Return the ``input_image`` block a binary came from."""
    return {
        "type": "input_image",
        "image_url": (
            f"data:{attachment.mime_descriptor};base64,"
            f"{base64.b64encode(attachment.data).decode('ascii')}"
        ),
    }


def _split(content: str | None, lengths: Sequence[int]) -> list[str]:
    """Return prose as the blocks the provider wrote it in.

    By length rather than by separator: a block may itself contain the
    newline the reader joined on, so splitting on it would land the boundary
    in the wrong place.
    """
    if content is None:
        return []
    if not lengths:
        return [content]
    if lengths == [0] and not content:
        # Zero blocks, not one empty one: an image-only output has no text.
        return []
    out: list[str] = []
    at = 0
    for size in lengths:
        out.append(content[at : at + size])
        at += size + 1
    return out if at == len(content) + 1 else [content]


def _is_command_shape(value: object) -> bool:
    """Return whether a command is a string or an all-string list."""
    if isinstance(value, str):
        return True
    values = ListCodec.coerce(value)
    strings = ListCodec.coerce(value, str)
    return isinstance(value, list) and len(strings) == len(values)


def _command(value: object) -> tuple[str, ...] | None:
    """Read a command, which the provider writes as its argument list."""
    if isinstance(value, str):
        return (value,) if value else None
    parts = ListCodec.coerce(value, str)
    return tuple(parts) if parts else None


def _effort(
    wire: str,
    *,
    aliases: Mapping[str, ThinkingEffort] = MappingProxyType({"ultra": "max"}),
) -> ThinkingEffort | None:
    """Read a reported effort level, resolving a provider's own spelling.

    ``ultra`` is codex's name for its top setting on 11111 captured turns;
    ``max`` is the same setting on 26, written by the same models under the
    same CLI build -- so the IR names one level and the line records which
    word it used.

    Args:
      wire: The word the line carried.
      aliases: Provider spellings of a level the IR already names.

    Returns:
      effort: The named level, or ``None`` when the word names none.

    """
    if wire in aliases:
        return aliases[wire]
    if wire not in get_args(ThinkingEffort.__value__):
        return None
    return cast(ThinkingEffort, wire)


def _summary_kind(wire: str) -> SummaryKind | None:
    """Read a requested summary verbosity."""
    if wire not in get_args(SummaryKind.__value__):
        return None
    return cast(SummaryKind, wire)
