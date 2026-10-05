"""Normalize and denormalize Claude Code session streams.

Claude repeats a session-wide envelope on every line, which becomes one
:class:`TurnContext` the records name by index. What the record types do not
name -- the message ids, the parent pointers, the provider's own bookkeeping
-- rides in ``extra``, so the writer replays the line rather than
approximating it.

Key order is claude's own and fixed per record type, so it lives here as a
tuple per type rather than being stored on every record.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import replace
from typing import Final, TextIO, TypeGuard, cast, get_args
from uuid import UUID, uuid5

import base64
import binascii
import json

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
from trackinizer.lib.agent.sessions.shell_results import (
    lift_shell_result,
    rewrite_shell_source,
    shell_result_for_replay,
)
from trackinizer.lib.agent.types.capability import ThinkingEffort
from trackinizer.lib.agent.types.sessions import (
    AgentStatusResult,
    AnyToolResult,
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
    SystemMessage,
    Thinking,
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
    JSONValue,
    MutableJSONValue,
    convert,
    convert_or_none,
    extract_unmodeled_fields,
    json_freeze,
    json_unfreeze,
    loads,
    read_field_keeping_invalid,
    restore_unmodeled_fields,
    same_json_value,
)


__all__ = ["denormalize", "normalize"]


def normalize(stream: TextIO) -> Iterator[SessionRecord]:
    """Normalize a Claude Code JSONL stream into its records.

    Yields as it reads (axiom 11), so a caller tailing a live transcript sees
    a record when its line lands. A session being written never reaches EOF,
    which is why returning one object could not serve a tailer at all.

    Args:
      stream: Claude Code JSONL text stream.

    Yields:
      record: Each record the transcript carries, in stream order.

    """
    reader = _Reader()
    for line in stream:
        yield from reader.read(line)


_FOREIGN_SEED: Final = UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")
"""Namespace for identity keys a foreign stream does not carry.

The URL namespace constant, used as a fixed arbitrary seed: what matters is
that two conversions of one stream agree, not which namespace names it.
"""


def denormalize(
    records: Iterable[SessionRecord],
    stream: TextIO,
    *,
    seed: UUID = _FOREIGN_SEED,
) -> None:
    """Denormalize records as Claude Code JSONL.

    Args:
      records: Provider-neutral records, in stream order.
      stream: Destination text stream.
      seed: Namespace for the identity keys a FOREIGN stream is MISSING.
        Claude's transcript is a linked list and the CLI hangs on a file with
        no root, so a session from another provider has uuids derived here.
        The default only keeps that derivation stable for a caller with no
        identity of its own.

        Fills, never overrides. A line that already states ``sessionId`` keeps
        it, because replaying a captured line verbatim is what makes the
        round-trip byte-exact -- so seeding a CAPTURED claude stream writes
        nothing at all. Renaming one is a different operation, per record:
        :func:`trackinizer.trax.run.materialize._renamed`.

    """
    ordered = list(records)
    contexts: dict[int | None, TurnContext] = {
        record.context_id: record
        for record in ordered
        if isinstance(record, TurnContext)
    }
    fallback_context = next(iter(contexts.values()), TurnContext())
    # The LAST context stating an encoding is the one in force: the escaping
    # convention is a majority that moves as the file grows, so the reader
    # restates it rather than resolving one final value.
    metadata: dict[str, object] = {}
    for record in ordered:
        if isinstance(record, TurnContext) and record.encoding:
            metadata = dict(record.encoding)
    escaped_default = convert(metadata.get("ascii_escaped"), bool, default=False)
    escape_exceptions = base64.b64decode(
        convert(metadata.get("ascii_escape_exceptions"), str, default=""),
        validate=True,
    )
    emitter = _Emitter(
        stream,
        escaped_default=escaped_default,
        exceptions=escape_exceptions,
        newline_terminated=metadata.get("newline_terminated") is not False,
    )
    # Whether another provider's adapter read this session -- the only case
    # needing claude's identity keys synthesized, since claude's transcript is
    # a LINKED LIST and the real CLI does not load a file without one.
    #
    # The ENCODING is what says: every adapter states one on its opening
    # context, and only claude's names an escaping convention. A stream built
    # BY HAND states none at all and is left alone -- nothing resumes one, and
    # adding keys there would rewrite bytes the caller chose.
    foreign = (
        any(isinstance(record, TurnContext) and record.encoding for record in ordered)
        and "ascii_escaped" not in metadata
    )
    # The claude tool each foreign act belongs to, by the call it answers.
    # Claude types a result by the NAME of its call, so a call still wearing
    # the source's name -- ``apply_patch``, ``shell`` -- made every crossed act
    # arrive as an uncategorized result: 42 of 150 captured rollouts lost one.
    renamed: dict[str, str] = (
        {
            record.call_id: name
            for record in ordered
            if isinstance(record, ToolResult)
            and record.call_id
            and (name := _claude_tool(record))
        }
        if foreign
        else {}
    )
    # Which foreign results already have a call line BEFORE them. Claude
    # correlates forward -- a result is typed by the call it has already read --
    # so a call is only useful where it precedes its answer. Codex states both
    # shapes: a search reports its end event with no call at all, and elsewhere
    # reports the result BEFORE the call it answers. Neither types on arrival,
    # and both are repaired by writing the call first.
    opened: set[str] = set()
    seen_result: set[str] = set()
    for record in ordered:
        if isinstance(record, ToolCall) and record.call_id not in seen_result:
            opened.add(record.call_id)
        elif isinstance(record, ToolResult):
            seen_result.add(record.call_id)
    parent: str | None = None
    for group in _group(ordered):
        head = group[0]
        if isinstance(head, IncompleteRecord):
            emitter.add(head.text)
            continue
        context = contexts.get(head.context_id, fallback_context)
        if (
            renamed
            and isinstance(head, ToolResult)
            and head.call_id
            and head.call_id not in opened
        ):
            call = _synthetic_call(head, renamed[head.call_id], context)
            call, parent = _linked(call, parent, ordered, seed)
            emitter.add(call)
            opened.add(head.call_id)
        line = _write_group(group, context, dict(json_unfreeze(context.extra)))
        if line is not None and renamed:
            _rename_calls(line, renamed)
        if line is not None and foreign:
            # A claude transcript is a LINKED LIST -- every line names its own
            # ``uuid`` and its predecessor's ``parentUuid``, and the CLI walks
            # that chain to rebuild history. A session from another provider
            # has neither, and the real binary hung indefinitely on the file
            # rather than rejecting it: no root, so the walk never finished.
            #
            # Written only for a foreign session. One read FROM claude already
            # carries its own, and adding any key there would break the byte
            # exactness the whole reader is built on.
            line, parent = _linked(line, parent, ordered, seed)
        emitter.add(
            line,
            record=head,
            # EVERY call on the line, not just the one that led it. A line may
            # speak before it calls, and may open several calls at once; a
            # result reaching back for its own found nothing when only the
            # head was registered, and the rename it carried was dropped.
            calls=[r for r in group if isinstance(r, ToolCall)],
        )
    emitter.close()


# Written only for a FOREIGN result whose provider reported no call of its own -- codex
# states a web search as an end event alone. Claude names the acting tool nowhere else,
# so without this line the act crosses as an uncategorized result.
def _synthetic_call(
    item: ToolResult,
    name: str,
    context: TurnContext,
) -> dict[str, object]:
    """Return the ``tool_use`` line an act needs to be typed by."""
    settings = dict(json_unfreeze(context.extra))
    block: dict[str, object] = {
        "type": "tool_use",
        "id": item.call_id,
        "name": name,
        "input": {},
    }
    return _splice(
        assistant_order(),
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [block]},
            "timestamp": item.timestamp,
        },
        {},
        settings,
    )


# In place, on the rendered line, because the block is the object the writer already
# built. Only a call whose result this session typed is touched: one answering an act
# claude has no tool for keeps the name it came with.
def _rename_calls(line: dict[str, object], renamed: Mapping[str, str]) -> None:
    """Name each foreign call for the claude tool its answer describes."""
    message = line.get("message")
    if not isinstance(message, dict):
        return
    content = cast(dict[str, object], message).get("content")
    if not isinstance(content, list):
        return
    # The blocks the writer built, not copies of them: mutating those changes the line.
    for block in cast(list[object], content):
        if not isinstance(block, dict):
            continue
        found = cast(dict[str, object], block)
        name = renamed.get(convert(found.get("id"), str, default=""))
        if (
            name is not None
            and convert(found.get("type"), str, default="") == "tool_use"
        ):
            found["name"] = name


# Every provider records one somewhere -- codex on its launch settings and on each turn
# context -- and it is the same value claude repeats per line.
def _stated_cwd(records: Sequence[SessionRecord]) -> str:
    """Return the working directory the source session named, if any."""
    for record in records:
        if isinstance(record, TurnContext):
            payload = convert(
                record.extra.get("payload"),
                dict[str, object],
                default={},
            )
            stated = payload.get("cwd")
            if isinstance(stated, str):
                return stated
            break
    for record in records:
        if isinstance(record, TurnContext):
            found = (record.extra).get("cwd")
            if isinstance(found, str):
                return found
    return ""


# Returns the line and the uuid the NEXT one points at. The id is DERIVED from ``seed``
# and the line's position, not minted fresh, so converting the same stream twice
# produces the same bytes -- a random uuid would make every conversion differ from the
# last.
def _linked(
    line: dict[str, object],
    parent: str | None,
    records: Sequence[SessionRecord],
    seed: UUID,
) -> tuple[dict[str, object], str]:
    """Give one foreign line the identity claude's own reader needs."""
    stated = line.get("uuid")
    own = (
        stated
        if isinstance(stated, str)
        else str(uuid5(seed, f"{parent or ''}/{len(line)}/{line!s:.64}"))
    )
    # Only what the line does not already say. A record may carry claude's own
    # identity in its residual -- a synthesized one built by hand, a fused
    # session -- and replacing that would rewrite ids the caller chose.
    defaults: dict[str, object] = {
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "external",
        # The workspace the turn ran in. Claude resolves a transcript to its
        # project directory through this, so a line without one describes a
        # session belonging to no project.
        "cwd": _stated_cwd(records),
        "version": "2.1.241",
        "sessionId": str(seed),
    }
    filled = dict(line) | {
        key: value
        for key, value in defaults.items()
        if key not in line or line[key] is None
    }
    filled["uuid"] = own
    return (_ordered_keys(filled, ["parentUuid", "isSidechain", *line]), own)


class _Emitter:
    """Write lines in order, holding only the window a shell edit can reach.

    A lifted :class:`FileWriteResult` edits the ``command`` of the ``tool_use``
    line that opened it, so that line must still be mutable when the result
    arrives. Buffering the file to allow it cost 2.6 GB on a 273 MB session
    (axiom 11); buffering ``lookahead`` lines costs a constant.

    Args:
      stream: Destination for finished lines.
      escaped_default: Whether the file escapes non-ASCII by convention.
      exceptions: Bitmap of lines departing from that convention.
      newline_terminated: Whether the source file ended with a newline.
      lookahead: How many rendered lines stay editable behind the write
        cursor. A lifted file result carries an edit belonging to the Bash
        call that OPENED it, which sits earlier in the stream; measured over
        15,647 captured call/result pairs the gap never exceeded 9 lines. A
        pair further apart replays the command unedited rather than costing
        the whole file's memory.

    """

    def __init__(
        self,
        stream: TextIO,
        *,
        escaped_default: bool,
        exceptions: bytes,
        newline_terminated: bool,
        lookahead: int = 32,
    ) -> None:
        self._stream = stream
        self._escaped_default = escaped_default
        self._exceptions = exceptions
        self._newline_terminated = newline_terminated
        self._lookahead = lookahead
        self._pending: deque[tuple[int, dict[str, object] | str]] = deque()
        # Where each open call's rendered line sits in ``_pending``, so a
        # result can reach back into the object rather than re-render it.
        self._calls: dict[str, dict[str, object]] = {}
        self._index = 0
        self._held: str | None = None

    def add(
        self,
        line: dict[str, object] | str | None,
        *,
        record: SessionRecord | None = None,
        calls: Sequence[ToolCall] = (),
    ) -> None:
        """Queue one line, applying any shell edit it makes to an open call.

        ``calls`` is every invocation the line opened. A line's first act is
        not always its call -- it may speak or think first -- and one line may
        open several, so each is registered by its own id.

        Args:
          line: Claude JSONL-line dict or text repr; None for line-less records.
          record: SessionRecord if this line produced one (shell edit context).
          calls: ToolCall invocations the line opened (by id).

        """
        if line is None:
            # A record with no claude representation. It still consumed no
            # line, so the escaping index must not advance past it.
            return
        if record is not None:
            self._apply_shell_edit(record)
        self._pending.append((self._index, line))
        self._index += 1
        if isinstance(line, dict):
            for call in calls:
                self._calls[call.call_id] = line
        while len(self._pending) > self._lookahead:
            self._release()

    def close(self) -> None:
        """Flush the window, trimming the newline the source file lacked."""
        while self._pending:
            self._release()
        if self._held is None:
            return
        text = self._held if self._newline_terminated else self._held.removesuffix("\n")
        _ = self._stream.write(text)
        self._held = None

    def _apply_shell_edit(self, record: SessionRecord) -> None:
        """Rewrite a lifted file edit into the Bash call still in the window."""
        if not isinstance(record, FileReadResult | FileWriteResult | FileEditResult):
            return
        if "$shell" not in record.extra:
            return
        call = self._calls.get(record.call_id)
        if call is None:
            return
        message = convert(call.get("message"), dict[str, object], default={})
        content = convert(message.get("content"), list[object], default=[])
        for block in (
            cast(dict[str, object], value)
            for value in content
            if isinstance(value, dict)
        ):
            # By ID, not by "the first Bash on the line": one line may open
            # several, and rewriting the first put one result's rename onto a
            # different command entirely.
            if (
                convert(block.get("name"), str, default="") != "Bash"
                or convert(block.get("id"), str, default="") != record.call_id
            ):
                continue
            arguments = convert(block.get("input"), dict[str, object], default={})
            command = arguments.get("command")
            if not isinstance(command, str):
                continue
            # The rendered line is the object the writer already queued, so
            # editing it in place is what makes one pass possible at all.
            mutable = cast(dict[str, object], json_unfreeze(block["input"]))
            mutable["command"] = rewrite_shell_source(command, record)
            block["input"] = mutable

    def _release(self) -> None:
        """Write the oldest queued line, one behind so ``close`` can trim it."""
        index, line = self._pending.popleft()
        if self._held is not None:
            _ = self._stream.write(self._held)
        if isinstance(line, str):
            self._held = line
            return
        byte_index = index // 8
        exception = byte_index < len(self._exceptions) and bool(
            self._exceptions[byte_index] & (1 << (index % 8)),
        )
        self._held = (
            json.dumps(
                json_unfreeze(line),
                ensure_ascii=self._escaped_default != exception,
                separators=(",", ":"),
            )
            + "\n"
        )
        for call_id, held in tuple(self._calls.items()):
            if held is line:
                del self._calls[call_id]


# One claude line carries its acts, and when the provider reported it, that line's
# usage. Axiom 3 splits them, and axiom 10 says the FIRST act owns the line's residual
# -- so a following record with none of its own belongs to the line before it.
#
# Yielded, never accumulated (axiom 11): returning one list per record built a structure
# proportional to the session -- measured at ~75-83 bytes per record, 16776 / 31176 /
# 60456 bytes for 200 / 400 / 800 -- which is the whole-file hold a writer is not
# allowed. Only ONE line's acts ever share a group, so the open group is bounded by the
# blocks a line carried.
def _group(records: Iterable[SessionRecord]) -> Iterator[Sequence[SessionRecord]]:
    """Regroup records into the source lines they came from."""
    open_group: list[SessionRecord] = []
    for record in records:
        if isinstance(record, TurnContext):
            # Session-scoped: claude repeats its fields on every line rather
            # than giving it a line of its own.
            continue
        if open_group and _continues(record, open_group[0]):
            open_group.append(record)
            continue
        if open_group:
            yield open_group
        open_group = [record]
    if open_group:
        yield open_group


# Only an act the model emitted, or the usage reported beside it, only when it carries
# no residual of its own -- which is what marks it as not having led a line -- and only
# onto a line the model spoke. A ``$`` key is this reader's note about the record's own
# shape, not a key off the line, so it does not count as one.
def _continues(record: SessionRecord, head: SessionRecord) -> bool:
    """Whether a record came off the line before it rather than its own."""
    valid_pair = (
        isinstance(record, TokenUsage | ToolCall | Thinking | AssistantMessage)
        and isinstance(head, AssistantMessage | Thinking | ToolCall)
    ) or (
        isinstance(record, UserMessage | ToolResult)
        and isinstance(head, UserMessage | ToolResult)
    )
    if not valid_pair:
        return False
    assert isinstance(
        record,
        TokenUsage | ToolCall | Thinking | AssistantMessage | UserMessage | ToolResult,
    )
    assert isinstance(
        head,
        AssistantMessage | Thinking | ToolCall | UserMessage | ToolResult,
    )
    if record.context_id != head.context_id or "$keys" in record.extra:
        return False
    return all(key.startswith("$") for key in record.extra)


def _write_group(
    group: Sequence[SessionRecord],
    context: TurnContext,
    settings: Mapping[str, object],
) -> dict[str, object] | None:
    """Return the one line a group of records came from."""
    head = group[0]
    if isinstance(head, UncategorizedRecord):
        # Only a record this reader wrote replays verbatim: its payload IS a
        # claude line. Another provider's is that provider's bytes, and
        # emitting it put codex lines in a claude file -- the output was then
        # detected as NEITHER format, on 60 of 60 sampled conversions.
        #
        # Provenance is in the kind: claude names a bare line type, every
        # other adapter namespaces its own (``event_msg/task_started``).
        # Measured over 1500 captured transcripts: 17 distinct claude line
        # types, none containing a slash.
        if "/" in head.kind:
            return None
        return dict(json_unfreeze(head.payload))
    if isinstance(head, UserMessage):
        return _write_user(head, context, settings, group)
    if isinstance(head, ToolResult):
        return _write_result(head, settings, group)
    if isinstance(head, AssistantMessage | ToolCall | Thinking):
        usage = next((r for r in group if isinstance(r, TokenUsage)), None)
        return _write_assistant(head, group, usage, context, settings)
    if isinstance(head, SystemMessage):
        return _write_system(head, settings)
    if isinstance(head, ContextState):
        return _write_context_state(head, settings)
    # A record with no Claude representation -- another CLI's state, a fusion
    # boundary. Dropped here and reported by the converter's loss summary.
    return None


def _stored(
    extra: dict[str, MutableJSONValue],
    fallback: Sequence[str],
) -> tuple[Sequence[str], dict[str, MutableJSONValue]]:
    """Return source key order and replay state without mutating ``extra``."""
    trailing = set(convert(extra.get("$trailing"), list[str], default=[]))
    stored = convert(extra.get("$keys"), list[str], default=[])
    replay_extra = {key: value for key, value in extra.items() if key != "$keys"}
    if stored:
        replay_extra["$source_order"] = True
    order = [key for key in stored if key not in trailing]
    return order or fallback, replay_extra


# Each key's value comes from the record's own fields, from its residual, or from the
# turn's repeated envelope -- whichever holds it.
def _splice(
    order: Sequence[str],
    named: Mapping[str, object],
    extra: Mapping[str, object],
    settings: Mapping[str, object],
) -> dict[str, object]:
    """Return one line, in the fixed order claude writes its keys."""
    record: dict[str, object] = {}
    # A ``$`` key is the reader's own note about the line's shape, not one of
    # the line's keys, so it never reaches the output.
    trailing = set(convert(extra.get("$trailing"), list[str], default=[]))
    nulls = set(convert(extra.get("$nulls"), list[str], default=[]))
    keys = {
        (key.removeprefix("$wire") if key.startswith("$wire$") else key): value
        for key, value in extra.items()
        if (not key.startswith("$") or key.startswith("$wire$")) and key not in trailing
    }
    for key in order:
        if key == "timestamp" and "$timestamp" in extra:
            record[key] = extra["$timestamp"]
        elif key in named:
            if named[key] is not None or key in nulls:
                record[key] = named[key]
        elif key in keys:
            record[key] = keys[key]
        elif key in settings:
            record[key] = settings[key]
    # Source residuals can carry provider extensions. Synthesized records may
    # carry another adapter's residual, which must not become Claude wire.
    if convert(extra.get("$source_order"), bool, default=False):
        for key, value in keys.items():
            if key not in record:
                record[key] = value
    for key in envelope_keys():
        if f"${key}" in extra:
            record[key] = extra[f"${key}"]
        elif key in settings:
            record[key] = settings[key]
    for key in convert(extra.get("$trailing"), list[str], default=[]):
        if key in extra:
            output_key = key.removeprefix("$wire") if key.startswith("$wire$") else key
            record[output_key] = extra[key]
    return record


# ``group`` is every record off that line. One claude line can carry prose AND a tool's
# answer, and each is its own record (axiom 3) -- so a writer that saw only the head
# filled one and left the other's stencil empty: with prose first the result's content
# became ``null``, and with the result first the prose did.
def _write_user(
    item: UserMessage,
    context: TurnContext,
    settings: Mapping[str, object],
    group: Sequence[SessionRecord] = (),
) -> dict[str, object]:
    """Return the ``user`` line a message came from."""
    extra = dict(json_unfreeze(item.extra))
    message: dict[str, object] = {"role": "user"}
    message.update(convert(extra.pop("message", {}), dict[str, object]))
    bare = extra.pop("$bare", False)
    parts_value = extra.pop("$parts", 1)
    parts = parts_value if isinstance(parts_value, int) else 1
    media_first = bool(extra.pop("$media_first", False))
    shape = convert(extra.pop("$content_shape", []), list[object])
    if "$content_value" in extra:
        message["content"] = extra.pop("$content_value")
    elif bare:
        message["content"] = item.content
    elif shape:
        message["content"] = _write_user_shape(item, shape, group)
    else:
        blocks: list[dict[str, object]] = [
            {"type": "text", "text": part} for part in _split(item.content, parts)
        ]
        blocks.extend(
            _write_attachment(found, media_first=media_first)
            for found in item.attachments
        )
        blocks.extend(_blocks(extra.pop("$blocks", [])))
        message["content"] = blocks
    message = _ordered_keys(
        message,
        convert(extra.pop("$message_keys", []), list[str]),
    )
    named: dict[str, object] = {
        "type": "user",
        "message": message,
        "timestamp": item.timestamp,
    }
    # Stated only on a line that also names its prompt source: the two appear
    # together on all 835 captured user lines that carry either.
    if "$permissionMode" in extra:
        named["permissionMode"] = extra["$permissionMode"]
    elif "promptSource" in extra and context.permission is not None:
        named["permissionMode"] = context.permission
    order, extra = _stored(extra, user_order())
    return _splice(order, named, extra, settings)


# A ``tool_result`` slot is filled from the RESULT record that owns it, not from this
# message: the two are siblings off one line, and a slot left as a bare stencil emitted
# the answer's content as ``null``.
def _write_user_shape(
    item: UserMessage,
    shape: Sequence[object],
    group: Sequence[SessionRecord] = (),
) -> list[object]:
    """Replay user blocks in place while applying semantic edits."""
    texts = _split_widths(item.content, _text_lengths(shape))
    attachments = list(item.attachments)
    results = {
        record.call_id: record for record in group if isinstance(record, ToolResult)
    }
    text_index = 0
    attachment_index = 0
    out: list[object] = []
    for value in shape:
        if not isinstance(value, Mapping):
            out.append(value)
            continue
        block = convert(cast(Mapping[str, object], value), dict[str, object])
        owner = results.get(convert(block.get("tool_use_id"), str, default=""))
        if (
            convert(block.get("type"), str, default="") == "tool_result"
            and owner is not None
        ):
            out.append(_write_result_block(owner))
            continue
        if _restored(block, "text"):
            if text_index < len(texts):
                out.append(_replace_key(_bare(block), "text", texts[text_index]))
            text_index += 1
            continue
        if _is_image(block):
            if attachment_index < len(attachments):
                out.append(
                    _write_attachment_shape(block, attachments[attachment_index]),
                )
            attachment_index += 1
            continue
        out.append(_bare(block) if "$held" in block else cast(dict[str, object], value))
    out.extend({"type": "text", "text": text} for text in texts[text_index:])
    out.extend(
        _write_attachment(attachment, media_first=False)
        for attachment in attachments[attachment_index:]
    )
    return out


def _is_image(block: Mapping[str, object]) -> bool:
    """Whether a stencil or a whole block describes an inline image."""
    if _read_attachment(block) is not None:
        return True
    source = convert(block.get("source"), dict[str, object], default={})
    return convert(block.get("type"), str, default="") == "image" and bool(
        convert(source.get("$held"), list[str], default=[]),
    )


def _write_attachment_shape(
    block: Mapping[str, object],
    attachment: Attachment | None,
) -> dict[str, object]:
    """Replay one image's field presence and key order."""
    if attachment is None:
        return _bare(block)
    source = convert(block.get("source"), dict[str, object], default={})
    if _restored(source, "data"):
        source = _replace_key(
            source,
            "data",
            base64.b64encode(attachment.data).decode("ascii"),
        )
    if _restored(source, "media_type"):
        source = _replace_key(source, "media_type", attachment.mime_descriptor)
    return _replace_key(_bare(block), "source", _bare(source))


def _replace_key(
    source: Mapping[str, object],
    key: str,
    value: object,
) -> dict[str, object]:
    """Replace one mapping value without changing its key position."""
    return {name: value if name == key else found for name, found in source.items()}


def _assistant_stencil(value: object) -> JSONValue:
    """Empty an assistant block's values, which sit on their own records."""
    if not isinstance(value, Mapping):
        return cast(JSONValue, value)
    block = convert(cast(Mapping[str, object], value), dict[str, object])
    out = _stencil(block, "text", "thinking", "signature", "input", "id", "name")
    call_id = block.get("id")
    if convert(block.get("type"), str, default="") == "tool_use" and isinstance(
        call_id,
        str,
    ):
        # WHICH call this slot held. The id itself is emptied -- the record's
        # field carries it -- but without a name for the slot the writer can
        # only fill slots in order, and deleting one call then slid every later
        # one back into it: from ``text, a, text, b``, dropping ``a`` moved
        # ``b`` across the prose it followed.
        out["$call_id"] = call_id
    return out


# An image's base64 belongs to the attachment record and a ``tool_result``'s content to
# the result record, which rebuilds its own block; only the key positions stay here.
def _user_stencil(
    block: Mapping[str, object],
    attachment: Attachment | None,
) -> dict[str, JSONValue]:
    """Empty a user block's values, which each sit on a different record."""
    if convert(block.get("type"), str, default="") == "tool_result":
        return _stencil(block, "content")
    if attachment is not None:
        source = convert(block.get("source"), dict[str, object], default={})
        return _stencil(block, "text") | {
            "source": _stencil(source, "data", "media_type"),
        }
    return _stencil(block, "text")


def _text_lengths(shape: Sequence[object]) -> list[int]:
    """Return each text block's line count, in the order they were written."""
    return [
        convert(block.get("$lines"), int, default=1)
        for value in shape
        if isinstance(value, Mapping)
        if (block := convert(cast(Mapping[str, object], value), dict[str, object]))
        and _restored(block, "text")
    ]


# A stencil, not a copy: the writer puts each held value back from the record, so
# storing it too wrote every block's prose, signature, and tool input twice -- 98.4% of
# every template, measured over 6413 files.
#
# Only a ``str`` is emptied. A key holding anything else is one the field could not
# take, so the residual is the only place it survives; ``None`` marks "the field has
# this", which is what the writer reads.
def _stencil(block: Mapping[str, object], *held: str) -> dict[str, JSONValue]:
    """Return a block with the values a record's own fields hold emptied."""
    # Only a value the matching field can carry: ``input`` is an object and
    # the rest are strings, and anything else -- a malformed ``"input":"bad"``
    # -- survives nowhere but here.
    emptied = [
        key
        for key in held
        if isinstance(block.get(key), Mapping if key == "input" else str)
    ]
    out: dict[str, JSONValue] = {
        key: None if key in emptied else cast(JSONValue, value)
        for key, value in block.items()
    }
    if emptied:
        # WHICH keys the record fills. A genuine ``null`` claude wrote is a
        # value no field carries, so the two cannot be told apart otherwise.
        out["$held"] = emptied
    text = block.get("text")
    if "text" in emptied and isinstance(text, str) and "\n" in text:
        # Where this block's share of the prose ends, since the prose itself
        # now lives only on the record.
        out["$lines"] = text.count("\n") + 1
    return out


# A block read before ``$held`` existed still carries its values, so a string counts
# too; a bare ``null`` does not, since claude writes those and no field represents one.
def _restored(block: Mapping[str, object], key: str) -> bool:
    """Whether ``key`` is a slot the record's own field fills."""
    if key in convert(block.get("$held"), list[str], default=[]):
        return True
    return isinstance(block.get(key), str)


def _bare(block: Mapping[str, object]) -> dict[str, object]:
    """Return one stencil without the reader's own notes."""
    return {key: value for key, value in block.items() if not key.startswith("$")}


def _ordered_keys(
    source: Mapping[str, object],
    order: Sequence[str],
) -> dict[str, object]:
    """Return keys in source order, followed by newly synthesized keys."""
    return {key: source[key] for key in order if key in source} | {
        key: value for key, value in source.items() if key not in order
    }


def _split_like(content: str | None, originals: Sequence[str]) -> list[str]:
    """Split edited prose across the source block boundaries."""
    return _split_widths(content, [text.count("\n") + 1 for text in originals])


# Line COUNTS, not the source strings: the prose is already on the record, and keeping a
# second copy to mark the boundaries is what made a template 98% duplicate.
def _split_widths(content: str | None, widths: Sequence[int]) -> list[str]:
    """Split prose back into blocks of ``widths`` lines each."""
    if content is None:
        return []
    if not widths:
        return [content]
    lines = content.split("\n")
    if sum(widths) != len(lines):
        # The prose was edited into a different shape than it was read in;
        # the boundaries no longer describe it, so it stays whole.
        return [content] if len(widths) == 1 else ["\n".join(lines)]
    out: list[str] = []
    offset = 0
    for width in widths:
        out.append("\n".join(lines[offset : offset + width]))
        offset += width
    return out


# ``group`` is every record off that line, so a line answering several calls fills each
# block from the record that owns it rather than from a stored copy of its content.
def _write_result(
    item: ToolResult,
    settings: Mapping[str, object],
    group: Sequence[SessionRecord] = (),
) -> dict[str, object]:
    """Return the ``user`` line a tool's answer came from."""
    extra = dict(json_unfreeze(item.extra))
    shape = convert(extra.pop("$result", {}), dict[str, object])
    message_blocks = shape.get("message_blocks", [_write_result_block(item)])
    content_shape = convert(extra.pop("$content_shape", []), list[object])
    if content_shape:
        siblings = {
            record.call_id: record for record in group if isinstance(record, ToolResult)
        }
        # The prose off the SAME line. A line may answer a tool and speak, and
        # each is its own record (axiom 3); a text slot left as a bare stencil
        # emitted ``"text": null`` and lost what the user said.
        spoken = next(
            (record for record in group if isinstance(record, UserMessage)),
            None,
        )
        texts = _split_widths(
            spoken.content if spoken is not None else None,
            _text_lengths(content_shape),
        )
        text_index = 0
        rebuilt: list[object] = []
        for value in content_shape:
            candidate = convert(value, dict[str, object])
            owner = siblings.get(convert(candidate.get("tool_use_id"), str, default=""))
            if (
                convert(candidate.get("type"), str, default="") == "tool_result"
                and owner is not None
            ):
                rebuilt.append(_write_result_block(owner))
            elif _restored(candidate, "text"):
                if text_index < len(texts):
                    rebuilt.append(
                        _replace_key(_bare(candidate), "text", texts[text_index]),
                    )
                text_index += 1
            else:
                rebuilt.append(_bare(candidate) if "$held" in candidate else value)
        message_blocks = rebuilt
    message = {"role": "user", "content": message_blocks}
    message.update(convert(extra.pop("message", {}), dict[str, object]))
    message = _ordered_keys(
        message,
        convert(extra.pop("$message_keys", []), list[str]),
    )
    named: dict[str, object] = {
        "type": "user",
        "message": message,
        "timestamp": item.timestamp,
    }
    payload_stated = "toolUseResult" in extra
    payload = _write_tool_payload(item, extra, shape)
    if payload is not None or payload_stated:
        named["toolUseResult"] = payload
    order, extra = _stored(extra, user_order())
    return _splice(order, named, extra, settings)


# The inverse of :func:`_rendered`: the reader stored a marker rather than the string,
# because the record already holds what it is made of.
def _result_text(item: ToolResult) -> str:
    """Rebuild the block text a result's own fields render."""
    if isinstance(item, ShellCommandResult):
        return _shell_text(item.stdout, item.stderr)
    if isinstance(item, FileReadResult | FileWriteResult | FileEditResult):
        shell = shell_result_for_replay(item)
        if shell is not None:
            return _shell_text(shell.stdout, shell.stderr)
    if isinstance(item, FileReadResult):
        return _numbered(item.content or "")
    return ""


def _write_result_block(item: ToolResult) -> dict[str, object]:
    """Return the ``tool_result`` block one result record came from."""
    shape = convert(item.extra.get("$result"), dict[str, object], default={})
    neutral_content = (
        item.content
        if isinstance(item, UncategorizedToolResult)
        else _result_text(item)
    )
    content = (
        _write_result_content(
            item,
            convert(shape.get("content"), list[object], default=[]),
        )
        if "content" in shape and isinstance(item, UncategorizedToolResult)
        else shape["content"]
        if "content" in shape
        else _result_text(item)
        if convert(shape.get("$text"), bool, default=False)
        else shape.get("text", neutral_content or "")
    )
    values: dict[str, object] = {
        "type": "tool_result",
        "tool_use_id": item.call_id,
        "content": content,
        **convert(shape.get("block"), dict[str, object], default={}),
    }
    keys = convert(shape.get("keys"), list[str], default=[]) or [
        "type",
        "tool_use_id",
        "content",
    ]
    return {key: values[key] for key in keys if key in values}


def _write_result_content(
    item: UncategorizedToolResult,
    shape: Sequence[object],
) -> list[object]:
    """Replay result content blocks while applying prose edits."""
    originals = [
        convert(part.get("text"), str, default="")
        for value in shape
        if (part := convert(value, dict[str, object]))
        and convert(part.get("type"), str, default="") == "text"
    ]
    texts = iter(_split_like(item.content, originals))
    out: list[object] = []
    for value in shape:
        part = convert(value, dict[str, object])
        if convert(part.get("type"), str, default="") == "text":
            out.append(
                _replace_key(
                    part,
                    "text",
                    next(texts, convert(part.get("text"), str, default="")),
                ),
            )
        else:
            out.append(value)
    return out


def _write_tool_payload(
    item: ToolResult,
    extra: dict[str, MutableJSONValue],
    shape: Mapping[str, object],
) -> object:
    """Return the tool's own return value, rebuilt from field and residual."""
    stored = extra.pop("toolUseResult", None)
    if stored is not None and not isinstance(stored, dict):
        # A failed call answers with a bare string rather than an object.
        return stored
    structured = isinstance(stored, dict)
    values = _result_values(item, shape)
    payload = restore_unmodeled_fields(
        convert({} if stored is None else stored, dict[str, object]),
        values,
    )
    if not payload and not structured and values:
        # ``restore_unmodeled_fields`` fills an envelope the claude reader stored, and a record
        # from another provider has none -- so it returned nothing and the line
        # carried no ``toolUseResult`` at all. Claude types a result from that
        # payload, so its absence is what made every crossed act arrive
        # uncategorized. The act's own values ARE the payload here.
        payload = {key: value for key, value in values.items() if value is not None}
    nested = convert(payload.get("file"), dict[str, object], default={})
    if isinstance(item, FileReadResult) and nested:
        payload["file"] = restore_unmodeled_fields(
            nested,
            {"filePath": item.path, "content": item.content},
        )
    if isinstance(item, FileEditResult) and "splice" in shape:
        # ``structuredPatch`` is claude's rendering of the SAME replacement the
        # splice holds, so an edited splice makes it stale. It is dropped
        # rather than re-rendered: rebuilding it needs the file's surrounding
        # lines, which the transcript never carried.
        stated = convert(shape.get("splice"), list[object], default=[])
        splice = item.edits[0] if item.edits else None
        current = [
            splice.before if splice is not None else None,
            splice.after if splice is not None else None,
        ]
        if list(stated) != current:
            payload.pop("structuredPatch", None)
    if not payload and not structured:
        return None
    # Order is the tool's own and fixed, so a field restored above goes back
    # where the tool wrote it rather than after the residual.
    #
    # A record from ANOTHER provider states no tool name -- ``$result`` is what
    # the claude reader stores, and a codex-sourced act never went through it --
    # so the neutral act names its own claude tool. Without this the payload is
    # unordered and, worse, the reader has no name to type the result by, so
    # every crossed act came back uncategorized.
    tool = convert(shape.get("tool_name"), str, default="") or _claude_tool(item)
    if tool == "Agent" and "isAsync" not in payload:
        tool = "Agent.done"
    for key, value in payload.items():
        if isinstance(value, dict):
            payload[key] = _ordered(cast(dict[str, object], value), f"{tool}.{key}")
    return _ordered(payload, tool)


# Claude names an act by the TOOL that performed it, and types a result by looking that
# name up. A record crossing from another provider carries the source's name --
# ``apply_patch``, ``shell``, ``web_search`` -- which claude recognizes as nothing, so
# the act arrived untyped. Naming the claude tool for the neutral act is what axiom 9
# asks: the same act, in the target's own vocabulary.
def _claude_tool(item: ToolResult) -> str:
    """Return the claude tool whose shape states what this act did."""
    for kind, name in (
        (FileReadResult, "Read"),
        (FileWriteResult, "Write"),
        (FileEditResult, "Edit"),
        (ShellCommandResult, "Bash"),
        (WebSearchResults, "WebSearch"),
        (WebFetchResult, "WebFetch"),
        (AgentStatusResult, "Agent"),
    ):
        if isinstance(item, kind):
            return name
    return ""


def _ordered(payload: dict[str, object], tool: str) -> dict[str, object]:
    """Return one tool payload in the fixed order that tool writes it in."""
    order = payload_orders().get(tool, ())
    return {key: payload[key] for key in order if key in payload} | {
        key: value for key, value in payload.items() if key not in order
    }


# An entry the reader saw as a group is restored as one; anything else was the model's
# own prose alongside the groups and is replayed as it stood.
def _write_search_groups(
    item: WebSearchResults,
    shape: Mapping[str, object],
) -> list[object]:
    """Return a search payload's ``results``, rows back inside their group."""
    rows = list(item.content)
    out: list[object] = []
    taken = 0
    groups = convert(shape.get("rows"), list[object], default=[])
    for index, entry in enumerate(groups):
        if not isinstance(entry, Mapping):
            out.append(entry)
            continue
        entry = cast(Mapping[str, object], entry)
        group_shape = convert(entry, dict[str, object])
        if "template" not in group_shape:
            out.append(entry)
            continue
        template = convert(group_shape.get("template"), dict[str, object], default={})
        original_content = convert(template.get("content"), list[object], default=[])
        originals = [
            convert(cast(Mapping[str, object], value), dict[str, object])
            for value in original_content
            if isinstance(value, Mapping)
        ]
        count = convert(group_shape.get("rows"), int, default=0)
        if index == len(groups) - 1:
            count = max(count, len(rows) - taken)
        current = rows[taken : taken + count]
        current_index = 0
        written: list[object] = []
        for value in original_content:
            if not isinstance(value, Mapping):
                written.append(value)
                continue
            value = cast(Mapping[str, object], value)
            if current_index < len(current):
                written.append(
                    _write_search_row(current[current_index], originals[current_index]),
                )
            current_index += 1
        written.extend(_write_search_row(row, None) for row in current[current_index:])
        out.append(_replace_key(template, "content", written))
        taken += count
    return out


def _write_search_row(
    row: WebSearchResult,
    template: Mapping[str, object] | None,
) -> dict[str, object]:
    """Replay a search row's field presence, order, and residual."""
    if template is None:
        return {
            key: value
            for key, value in {
                "title": row.title,
                "url": row.url,
                "snippet": row.snippet,
            }.items()
            if value is not None
        }
    values = {"url": row.url, "title": row.title, "snippet": row.snippet}
    return {
        key: values[key] if key in values and values[key] is not None else value
        for key, value in template.items()
    }


def _write_agent_blocks(
    item: AgentStatusResult,
    shape: Mapping[str, object],
) -> list[object]:
    """Replay agent prose blocks while preserving their boundaries."""
    stored = convert(shape.get("agent_blocks"), list[object], default=[])
    if not stored:
        return [{"type": "text", "text": item.content}]
    originals = [
        convert(block.get("text"), str, default="")
        for value in stored
        if (block := convert(value, dict[str, object]))
        and convert(block.get("type"), str, default="") == "text"
    ]
    texts = iter(_split_like(item.content, originals))
    return [
        _replace_key(
            block,
            "text",
            next(texts, convert(block.get("text"), str, default="")),
        )
        if (block := convert(value, dict[str, object]))
        and convert(block.get("type"), str, default="") == "text"
        else value
        for value in stored
    ]


def _result_values(item: ToolResult, shape: Mapping[str, object]) -> dict[str, object]:
    """Return current semantic values by their provider field names."""
    if isinstance(item, ShellCommandResult):
        return {"stdout": item.stdout, "stderr": item.stderr}
    if isinstance(item, FileReadResult | FileWriteResult | FileEditResult):
        shell = shell_result_for_replay(item)
        if shell is not None:
            return {"stdout": shell.stdout, "stderr": shell.stderr}
    if isinstance(item, FileReadResult):
        file = {
            key: value
            for key, value in {"filePath": item.path, "content": item.content}.items()
            if value is not None
        }
        return {"file": file or None}
    if isinstance(item, FileWriteResult):
        return {"filePath": item.path, "content": item.content}
    if isinstance(item, FileEditResult):
        splice = item.edits[0] if item.edits else None
        return {
            "filePath": item.path,
            "oldString": splice.before if splice is not None else None,
            "newString": splice.after if splice is not None else None,
        }
    if isinstance(item, WebSearchResults):
        return {
            "query": item.query,
            "durationSeconds": item.duration_sec,
            "results": _write_search_groups(item, shape),
        }
    if isinstance(item, WebFetchResult):
        return {
            "url": item.url,
            "result": item.content,
            "code": item.code,
            "durationMs": _millis(item.duration_sec, shape.get("duration_ms")),
            "bytes": item.size,
        }
    if isinstance(item, AgentStatusResult):
        return {
            "agentId": item.agent_id,
            "agentType": item.agent_kind,
            "prompt": item.prompt,
            "content": _write_agent_blocks(item, shape)
            if item.content is not None
            else None,
            "resolvedModel": item.model,
            "status": item.state,
            "totalTokens": item.tokens,
            "totalDurationMs": _millis(item.duration_sec, shape.get("duration_ms")),
            "totalToolUseCount": item.tool_calls,
            "outputFile": item.output_file,
        }
    return {}


def _write_assistant(
    head: AssistantMessage | ToolCall | Thinking,
    group: Sequence[SessionRecord],
    usage: TokenUsage | None,
    context: TurnContext,
    settings: Mapping[str, object],
) -> dict[str, object]:
    """Return the ``assistant`` line an act came from."""
    extra = dict(json_unfreeze(head.extra))
    # The line's own model, which the reader kept: a session can switch models
    # and a failed call writes a marker, so the turn's is only the fallback.
    message: dict[str, object] = dict(
        convert(extra.pop("message", {}), dict[str, object]),
    )
    if "model" not in message and context.model is not None:
        message["model"] = context.model
    parts_value = extra.pop("$parts", 1)
    parts = parts_value if isinstance(parts_value, int) else 1
    shape = convert(extra.pop("$content_shape", []), list[object])
    message["role"] = "assistant"
    message["content"] = (
        _write_blocks_shape(group, shape)
        if shape
        else _write_blocks(group, parts, extra)
    )
    if usage is not None and usage.info:
        message["usage"] = json_unfreeze(usage.info)
    elif "$usage" in extra:
        message["usage"] = extra.pop("$usage")
    message_order = convert(extra.pop("$message_keys", []), list[str])
    message = _ordered_keys(message, message_order)
    named: dict[str, object] = {
        "type": "assistant",
        "message": message if message_order else _ordered_message(message, extra),
        "timestamp": head.timestamp,
    }
    if "$effort" in extra:
        named["effort"] = extra["$effort"]
    elif context.effort is not None:
        named["effort"] = context.effort
    fallback = failed_order() if "isApiErrorMessage" in extra else assistant_order()
    order, extra = _stored(extra, fallback)
    return _splice(order, named, extra, settings)


def _ordered_message(
    message: Mapping[str, object],
    extra: Mapping[str, object],
) -> dict[str, object]:
    """Return the message object in the order claude writes its keys."""
    failed = (
        "diagnostics",
        "id",
        "container",
        "model",
        "role",
        "stop_details",
        "stop_reason",
        "stop_sequence",
        "type",
        "usage",
        "content",
        "context_management",
    )
    served = (
        "model",
        "id",
        "type",
        "role",
        "content",
        "stop_reason",
        "stop_sequence",
        "stop_details",
        "usage",
        "diagnostics",
    )
    # A failed call leads with the provider's diagnostics rather than the
    # model, and claude writes the whole object in a different order.
    order = failed if "isApiErrorMessage" in extra else served
    return {key: message[key] for key in order if key in message} | {
        key: value for key, value in message.items() if key not in order
    }


def _write_blocks_shape(
    group: Sequence[SessionRecord],
    shape: Sequence[object],
) -> list[object]:
    """Replay assistant blocks in place while applying semantic edits."""
    message = next(
        (record for record in group if isinstance(record, AssistantMessage)),
        None,
    )
    lengths = _text_lengths(shape)
    texts = _split_widths(message.content if message is not None else None, lengths)
    calls = [record for record in group if isinstance(record, ToolCall)]
    thoughts = [record for record in group if isinstance(record, Thinking)]
    # Each slot names the call it held, so a deleted call empties ITS slot
    # rather than letting every later call shift into the gap.
    by_id = {record.call_id: record for record in calls if record.call_id}
    placed: set[str] = set()
    text_index = 0
    call_index = 0
    thought_index = 0
    out: list[object] = []
    for value in shape:
        if not isinstance(value, Mapping):
            out.append(value)
            continue
        block = convert(cast(Mapping[str, object], value), dict[str, object])
        kind = convert(block.get("type"), str, default="")
        if kind == "text" and _restored(block, "text"):
            if text_index < len(texts):
                out.append(_replace_key(_bare(block), "text", texts[text_index]))
            text_index += 1
        elif kind == "tool_use":
            stated = convert(block.get("$call_id"), str, default="")
            if stated:
                owner = by_id.get(stated)
                if owner is not None:
                    out.append(_write_tool_call_shape(block, owner))
                    placed.add(stated)
            elif call_index < len(calls):
                # A slot read before ``$call_id`` existed, or a call claude
                # wrote without an id: order is the only thing naming it.
                out.append(_write_tool_call_shape(block, calls[call_index]))
            call_index += 1
        elif kind == "thinking":
            if thought_index < len(thoughts):
                out.append(_write_thinking_shape(block, thoughts[thought_index]))
            thought_index += 1
        else:
            out.append(cast(dict[str, object], value))
    out.extend({"type": "text", "text": text} for text in texts[text_index:])
    # A call no slot claimed: one added to the session after the line was read.
    out.extend(
        _write_tool_call(call, {})
        for index, call in enumerate(calls)
        if call.call_id not in placed and index >= call_index
    )
    out.extend(_write_thinking(thought) for thought in thoughts[thought_index:])
    return out


def _write_tool_call_shape(
    block: Mapping[str, object],
    item: ToolCall | None,
) -> dict[str, object]:
    """Replay a call's malformed and missing fields without fabrication."""
    if item is None:
        return dict(block)
    out = _bare(block)
    if _restored(block, "id"):
        out = _replace_key(out, "id", item.call_id)
    if _restored(block, "name"):
        out = _replace_key(out, "name", item.name)
    if isinstance(block.get("input"), Mapping) or "input" in convert(
        block.get("$held"),
        list[str],
        default=[],
    ):
        out = _replace_key(out, "input", json_unfreeze(item.arguments))
    return out


def _write_thinking_shape(
    block: Mapping[str, object],
    item: Thinking | None,
) -> dict[str, object]:
    """Replay a thinking block's residual and field presence."""
    if item is None:
        return _bare(block)
    out = _bare(block)
    if _restored(block, "thinking"):
        out = _replace_key(out, "thinking", item.content or "")
    if _restored(block, "signature"):
        out = _replace_key(out, "signature", item.encrypted or "")
    return out


# In record order, since one line may carry several kinds: 1 captured line in 2119 files
# writes a thinking block and then a tool call.
def _write_blocks(
    group: Sequence[SessionRecord],
    parts: int,
    extra: dict[str, MutableJSONValue],
) -> list[dict[str, object]]:
    """Return the content blocks an assistant line's acts came from."""
    blocks: list[dict[str, object]] = []
    for record in group:
        if isinstance(record, ToolCall):
            blocks.append(_write_tool_call(record, extra))
        elif isinstance(record, Thinking):
            blocks.append(_write_thinking(record))
        elif isinstance(record, AssistantMessage):
            blocks.extend(
                {"type": "text", "text": part} for part in _split(record.content, parts)
            )
    blocks.extend(_blocks(extra.pop("$blocks", [])))
    return blocks


def _write_tool_call(
    item: ToolCall,
    extra: dict[str, MutableJSONValue],
) -> dict[str, object]:
    """Return the ``tool_use`` block a call came from."""
    block: dict[str, object] = {
        "type": "tool_use",
        "id": item.call_id,
        "name": item.name,
        "input": json_unfreeze(item.arguments),
    }
    own = dict(json_unfreeze(item.extra))
    block.update(convert(own.pop("$call", extra.pop("$call", {})), dict[str, object]))
    return block


def _write_thinking(item: Thinking) -> dict[str, object]:
    """Return the ``thinking`` block reasoning came from."""
    # Claude has no summary field, so a summary crossing in from another
    # provider goes here or is lost.
    readable = item.content if item.content is not None else item.summary
    block: dict[str, object] = {"type": "thinking", "thinking": readable or ""}
    if item.encrypted is not None:
        block["signature"] = item.encrypted
    elif convert(item.extra.get("$signature_present"), bool, default=False):
        block["signature"] = ""
    return block


def _write_system(
    item: SystemMessage,
    settings: Mapping[str, object],
) -> dict[str, object]:
    """Return the ``system`` line a harness message came from."""
    extra = dict(json_unfreeze(item.extra))
    named: dict[str, object] = {
        "type": "system",
        "subtype": extra.pop("$subtype_value", item.subtype),
        "content": extra.pop("$content_value", item.content),
        "timestamp": item.timestamp,
    }
    order, extra = _stored(extra, system_order())
    return _splice(order, named, extra, settings)


def _write_context_state(
    item: ContextState,
    settings: Mapping[str, object],
) -> dict[str, object]:
    """Return the ``attachment`` line an injected context came from."""
    extra = dict(json_unfreeze(item.extra))
    where = convert(extra.pop("$prose", []), list[object])
    state: dict[str, object] = {"type": item.kind}
    state.update(convert(extra.pop("attachment", {}), dict[str, object]))
    if where and item.content is not None:
        # Back at the index it sat, which is not always right after the kind.
        state = _insert(
            state,
            convert(where[0], str),
            item.content,
            convert(where[1], int),
        )
    named: dict[str, object] = {
        "type": "attachment",
        "attachment": state,
        "timestamp": item.timestamp,
    }
    order, extra = _stored(extra, attachment_order())
    return _splice(order, named, extra, settings)


def _insert(
    source: Mapping[str, object],
    key: str,
    value: object,
    index: int,
) -> dict[str, object]:
    """Return ``source`` with ``key`` restored at the position it held."""
    items = list(source.items())
    items.insert(index, (key, value))
    return dict(items)


def _write_attachment(
    attachment: Attachment,
    *,
    media_first: bool,
) -> dict[str, object]:
    """Return the ``image`` block a binary came from."""
    encoded = base64.b64encode(attachment.data).decode("ascii")
    source: dict[str, object] = {"type": "base64"}
    if media_first:
        source["media_type"] = attachment.mime_descriptor
        source["data"] = encoded
    else:
        source["data"] = encoded
        source["media_type"] = attachment.mime_descriptor
    return {"type": "image", "source": source}


def _split(content: str | None, parts: int) -> list[str]:
    """Return prose as the blocks the provider wrote it in."""
    if content is None:
        return []
    if parts <= 1:
        return [content]
    pieces = content.split("\n")
    return pieces if len(pieces) == parts else [content]


def _effort(wire: str) -> ThinkingEffort | None:
    """Read a reported effort level."""
    if wire not in get_args(ThinkingEffort.__value__):  # pyright: ignore[reportAny] -- PEP 695 alias values are exposed as Any by the typing stub.
        return None
    return cast(ThinkingEffort, wire)


def _millis(value: float | None, original: object = None) -> float | int | None:
    """Return milliseconds, retaining an unchanged source numeric literal."""
    if value is None:
        return None
    source = None if original is None else convert(original, float)
    if source is not None and value == source / 1000:
        assert isinstance(original, int | float)
        return original
    return value * 1000


class _Reader:
    """Read a Claude Code session one line at a time.

    Line by line, never the whole file as one string (axiom 11): ONE
    non-ASCII character makes CPython store the entire string as 4 bytes per
    character, so reading a 273 MB transcript whole cost 1.09 GB before any
    parsing. Nothing here reaches backwards -- a tool call correlates with its
    result in stream order, and the one whole-file property claude has is
    restated as it moves rather than resolved at the end.

    Private to :func:`normalize`, which is the only entry point: the generator
    IS the incremental interface, so nothing outside needs a reader object.
    """

    def __init__(self) -> None:
        self._records: list[SessionRecord] = []
        self._context: TurnContext | None = None
        # A result says only which call it answers, so the tool that produced
        # it has to be carried forward from the ``tool_use`` block that opened
        # it.
        self._tools: dict[str, tuple[str, str | None]] = {}
        # Which lines were ASCII, packed as they arrive. The majority moves as
        # the stream does, so the bits are stored as read and reinterpreted
        # against whatever it is NOW -- the file's own convention decides which
        # value is the exception, not which was seen first.
        self._ascii_bits = bytearray()
        self._total = 0
        self._ascii_count = 0
        self._ends_newline = True
        self._encoding: JSON = {}
        # Where the opening clear will sit once it is known. Claude spreads
        # what a fresh context is GIVEN over several ``attachment`` lines --
        # its skills, its subagents, the tools it may call -- so the record
        # that states them cannot be built from line one.
        self._opening: int | None = None
        self._given: list[str] = []

    def read(self, line: str) -> Iterator[SessionRecord]:
        """Consume one native line; yield the records it produced.

        Args:
          line: One line from a claude.jsonl session file.

        Returns:
          result: Iterator of SessionRecord objects parsed from the line.

        """
        emitted = len(self._records)
        if not self._records:
            # Settings before the acts they govern, then the context the
            # window opens from.
            self._encoding = self._current_encoding()
            self._records.append(TurnContext(encoding=self._encoding))
            self._opening = len(self._records)
            self._records.append(ContextClear(extra=({"$opens": True})))
        if self._total % 8 == 0:
            self._ascii_bits.append(0)
        if line.isascii():
            self._ascii_bits[self._total // 8] |= 1 << (self._total % 8)
            self._ascii_count += 1
        self._ends_newline = line.endswith("\n")
        self._total += 1
        record = _parse(line)
        if record is None:
            self._records.append(IncompleteRecord(text=line))
            self._restate_encoding()
            return iter(self._records[emitted:])
        updated = _read_line_context(record, self._context)
        if self._context is None or updated != replace(self._context, context_id=None):
            self._context = replace(updated, context_id=len(self._records))
            self._records.append(self._context)
        produced = [
            item
            if isinstance(item, IncompleteRecord)
            else replace(item, context_id=self._context.context_id)
            for item in [
                _without_turn_envelope(r, self._context)
                for r in _read_record(record, self._tools)
            ]
        ]
        for item in produced:
            self._records.extend(self._opened(item))
        _update_tools(record, self._tools)
        self._restate_encoding()
        return iter(self._records[emitted:])

    # Two things happen here, both about the record a consumer reads to DELINEATE a
    # session -- the clear, whose rule is "this plus everything after it".
    #
    # The opening clear is FILLED rather than merely emitted. Claude states no prompt
    # field, so what a fresh context was given has to be assembled from the lines that
    # gave it: the skills, subagents, and tool availability the CLI injects before the
    # first turn. Left as loose state records, the delineating record described nothing.
    #
    # A compaction gets the same treatment it gets everywhere else. Claude marks the
    # carried summary as a user turn rather than announcing the reset, and only the FUSE
    # path noticed -- so a compaction inside one transcript put the summary in the
    # stream while nothing said a window had opened, and "the last clear" still named
    # the whole session.
    def _opened(self, item: SessionRecord) -> list[SessionRecord]:
        """Return one record, plus the window it opened where it opened one."""
        if isinstance(item, ContextState) and self._opening is not None:
            self._given.append(item.content or "")
            opening = self._records[self._opening]
            assert isinstance(opening, ContextClear)
            self._records[self._opening] = replace(
                opening,
                system_prompt="\n".join(part for part in self._given if part),
            )
            return [item]
        # The first act closes the opening: everything after it is the
        # conversation rather than the context it began from.
        self._opening = None
        if not _carries_a_compaction(item):
            return [item]
        return [
            ContextCompaction(
                context_id=item.context_id,
                timestamp=item.timestamp,
                extra=({"$in_place": True}),
            ),
            ContextClear(
                context_id=item.context_id,
                timestamp=item.timestamp,
                summary=item.content,
                extra=({"$in_place": True}),
            ),
            item,
        ]

    # Correct for the prefix consumed, never final: ``ascii_escaped`` is a MAJORITY over
    # the lines read, so it flips as the majority moves, and the exception bitmap is
    # stored relative to it -- a flip reinterprets lines already emitted. A tailer has
    # no EOF to resolve this at, so the reader restates and the last one before a record
    # is the one in force.
    def _restate_encoding(self) -> None:
        """Emit the file's spelling whenever it stops being what was said."""
        current = self._current_encoding()
        if current == self._encoding:
            return
        self._encoding = current
        self._records.append(TurnContext(encoding=current))

    def _current_encoding(self) -> JSON:
        """How the file spells its bytes, for the lines consumed so far."""
        ascii_default = self._ascii_count * 2 >= self._total
        return json_freeze(
            {
                "newline_terminated": not self._total or self._ends_newline,
                "ascii_escaped": ascii_default,
                "ascii_escape_exceptions": _ascii_escape_exceptions(
                    self._ascii_bits,
                    self._total,
                    default=ascii_default,
                ),
            },
        )


# Claude announces no reset: it writes the earlier conversation's summary as an ordinary
# user turn and flags it. The flag is the only mark, which is why ``fuse`` keys on the
# same one.
def _carries_a_compaction(item: SessionRecord) -> TypeGuard[UserMessage]:
    """Whether a record is the summary claude carried across a compaction."""
    return isinstance(item, UserMessage) and convert(
        dict(json_unfreeze(item.extra)).get("isCompactSummary"),
        bool,
        default=False,
    )


# Every line repeats ``cwd``/``sessionId``/``version``/``gitBranch``/
# ``userType``/``entrypoint``, and the reader kept each so a line that OVERRIDES one
# still replays. Measured across 40 sessions: 4188 stored, zero different from the
# turn's. The writer already falls back to the turn, so an equal copy is 0.113 MB of
# pure repetition.
def _without_turn_envelope(
    record: SessionRecord,
    context: TurnContext,
) -> SessionRecord:
    """Drop an envelope override that merely restates the turn's own value."""
    # The turn IS the envelope's source, an incomplete line is unparsed text,
    # and an uncategorized record replays its payload verbatim -- none of the
    # three holds an ``extra`` the override could live in.
    if isinstance(record, IncompleteRecord | TurnContext | UncategorizedRecord):
        return record
    settings = context.extra
    extra = dict(json_unfreeze(record.extra))
    kept = {
        key: value
        for key, value in extra.items()
        if not (
            key.startswith("$")
            and key[1:] in envelope_keys()
            and same_json_value(value, settings.get(key[1:]))
        )
    }
    return (
        record if len(kept) == len(extra) else replace(record, extra=json_freeze(kept))
    )


# ``ascii_bits`` marks which lines WERE ascii, accumulated as the stream was read; the
# majority is only known once it ends, so the stored bitmap is the difference against it
# rather than the raw observation.
def _ascii_escape_exceptions(
    ascii_bits: bytearray,
    total: int,
    *,
    default: bool,
) -> str:
    """Pack lines whose Unicode escaping differs from the majority."""
    packed = bytearray((total + 7) // 8)
    for index in range(total):
        was_ascii = bool(ascii_bits[index // 8] & (1 << (index % 8)))
        if was_ascii != default:
            packed[index // 8] |= 1 << (index % 8)
    return base64.b64encode(packed).decode("ascii")


def _read_line_context(
    record: Mapping[str, object],
    previous: TurnContext | None,
) -> TurnContext:
    """Return the full settings state applying to one line."""
    prior_extra = dict(json_unfreeze(previous.extra)) if previous is not None else {}
    for key in (*envelope_keys(), *settings_keys()):
        if key in record:
            prior_extra[key] = json_unfreeze(record[key])
    message = convert(record.get("message"), dict[str, object], default={})
    model = previous.model if previous is not None else None
    if "model" in message:
        model = convert(message.get("model"), str, default=None)
    effort = previous.effort if previous is not None else None
    if "effort" in record:
        effort = _effort(convert(record.get("effort"), str, default=""))
    permission = previous.permission if previous is not None else None
    if "permissionMode" in record:
        permission = convert(record.get("permissionMode"), str, default=None)
    return TurnContext(
        model=model,
        effort=effort,
        permission=permission,
        extra=(prior_extra),
    )


def _parse(line: str) -> dict[str, object] | None:
    """Return one line's record, or ``None`` when it carries none."""
    if not line.strip():
        return None
    try:
        parsed = loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, Mapping):
        return None
    return convert(parsed, dict[str, object])


def _update_tools(
    record: Mapping[str, object],
    tools: dict[str, tuple[str, str | None]],
) -> None:
    """Record calls after preceding results have consumed the prior mapping."""
    message = convert(record.get("message"), dict[str, object], default={})
    content = message.get("content")
    blocks = (
        [
            convert(cast(dict[str, object], block), dict[str, object])
            for block in cast(list[object], content)
            if isinstance(block, dict)
        ]
        if isinstance(content, list)
        else []
    )
    for block in blocks:
        call_id = block.get("id")
        if convert(block.get("type"), str, default="") == "tool_use" and isinstance(
            call_id,
            str,
        ):
            arguments = convert(block.get("input"), dict[str, object], default={})
            tools[call_id] = (
                convert(block.get("name"), str, default=""),
                convert(arguments.get("command"), str, default=None),
            )


def _read_record(
    record: Mapping[str, object],
    tools: Mapping[str, tuple[str, str | None]],
) -> list[SessionRecord]:
    """Read one parsed line into the records it carries."""
    message = convert(record.get("message"), dict[str, object], default={})
    record_type = convert(record.get("type"), str, default="")
    spoke = record_type in {"user", "assistant"} and convert(
        message.get("role"),
        str,
        default="",
    ) == (record_type)
    if spoke and record_type == "user":
        return _read_user(record, message, tools)
    if spoke:
        return _read_assistant(record, message)
    if record_type == "system":
        return [_read_system(record)]
    if record_type == "attachment":
        return [_read_attachment_record(record)]
    # Everything else claude writes -- its identity lines, its undo
    # checkpoints, its queue -- is claude's alone. Naming a record type for
    # one would put a shape in the IR that no other provider can fill, so it
    # keeps its bytes and says which kind it was.
    return [
        UncategorizedRecord(
            context_id=0,
            timestamp=convert_or_none(record.get("timestamp"), str),
            kind=record_type,
            payload=json_freeze(record),
        ),
    ]


# "The envelope" is where a value USUALLY lives, not where it always does: across 912
# archived files ``cwd`` varies within 32, ``gitBranch`` 16, ``version`` 14,
# ``sessionId`` 2. So a line that states one keeps its own and the turn's copy is only
# the fallback -- which also settles the order, since the turn cannot say where a key it
# never saw belongs.
def _line_residual(
    record: Mapping[str, object],
    *,
    consumed: Iterable[str] = (),
) -> dict[str, JSONValue]:
    """Return a line's residual, keeping the envelope value it stated."""
    # Once per line, not once per key: this runs on every record of a 273 MB
    # transcript, and the calls below sit inside comprehensions that rebuilt
    # the tuple for each key they tested.
    envelope = envelope_keys()
    extra = extract_unmodeled_fields(
        record,
        (
            *envelope,
            *settings_keys(),
            "type",
            "timestamp",
            "effort",
            "permissionMode",
            *consumed,
        ),
    )
    for key in tuple(extra):
        if key.startswith("$"):
            extra[f"$wire{key}"] = extra.pop(key)
    for key in (*envelope, "permissionMode"):
        if key in record:
            extra[f"${key}"] = cast(JSONValue, record[key])
    if "timestamp" in record and not isinstance(record["timestamp"], str | None):
        extra["$timestamp"] = cast(JSONValue, record["timestamp"])
    # The line's own key order. Not a per-type table: claude writes keys no
    # table anticipated -- ``toolEndsTurn``, ``classifierMetaLines``,
    # ``isCompactSummary`` -- and a table that misses one misplaces it.
    extra["$keys"] = [key for key in record if key not in envelope]
    nulls = [key for key, value in record.items() if value is None]
    if nulls:
        extra["$nulls"] = nulls
    if "effort" in record:
        extra["$effort"] = cast(JSONValue, record["effort"])
    # Which residual keys claude writes AFTER the envelope. Not a property of
    # the key -- ``sessionKind`` leads the envelope on a user line and trails
    # it on a system one -- so the line says.
    keys = list(record)
    if "userType" in keys:
        cut = keys.index("userType")
        extra["$trailing"] = [
            stored
            for key in keys[cut:]
            if key not in envelope
            and (stored := f"$wire{key}" if key.startswith("$") else key) in extra
        ]
    return extra


def _blocks(value: object) -> list[dict[str, object]]:
    """Return every object block, including an empty object."""
    items = cast(list[object], value) if isinstance(value, list) else []
    return [
        convert(cast(Mapping[str, object], item), dict[str, object])
        for item in items
        if isinstance(item, Mapping)
    ]


def _read_user(
    record: Mapping[str, object],
    message: Mapping[str, object],
    tools: Mapping[str, tuple[str, str | None]],
) -> list[SessionRecord]:
    """Read a user line: prose, its attachments, or a tool's answer."""
    content: object = message.get("content")
    extra = _line_residual(record, consumed=("message",))
    residual_message = extract_unmodeled_fields(message, message_consumed())
    if residual_message:
        extra["message"] = residual_message
    if list(message) != ["role", *residual_message, "content"]:
        extra["$message_keys"] = list(message)
    if "content" in message and not isinstance(content, str | list):
        extra["$content_value"] = cast(JSONValue, content)
    if isinstance(content, str):
        extra["$bare"] = True
        return [
            UserMessage(
                context_id=0,
                timestamp=convert_or_none(record.get("timestamp"), str),
                content=content,
                extra=json_freeze(extra),
            ),
        ]
    blocks = _blocks(message.get("content"))
    # Decoded ONCE and reused: the stencil needs to know which blocks were
    # images, and decoding again to ask doubles the base64 work on every line
    # carrying one.
    decoded = [(block, _read_attachment(block)) for block in blocks]
    parts = [
        convert(b.get("text"), str, default="")
        for b in blocks
        if isinstance(b.get("text"), str)
    ]
    attachments = tuple(found for _, found in decoded if found is not None)
    if isinstance(content, list):
        shapes = iter(_user_stencil(block, found) for block, found in decoded)
        extra["$content_shape"] = [
            next(shapes) if isinstance(value, Mapping) else cast(JSONValue, value)
            for value in cast(list[object], content)
        ]
    if len(parts) > 1:
        extra["$parts"] = len(parts)
    if any(_media_first(block) for block, found in decoded if found is not None):
        extra["$media_first"] = True
    timestamp = convert_or_none(record.get("timestamp"), str)
    acts: list[UserMessage | AnyToolResult] = []
    # A ``tool_result`` is never the message, whatever else it carries. One
    # that also holds a ``text`` key would otherwise be indexed twice -- once
    # here and once as a result -- and the duplicate index made ``sorted``
    # compare the records themselves, which define no order, raising
    # TypeError and aborting a file every other malformed shape survives.
    message_positions = [
        index
        for index, (block, found) in enumerate(decoded)
        if convert(block.get("type"), str, default="") != "tool_result"
        and (isinstance(block.get("text"), str) or found is not None)
    ]
    has_message = bool(message_positions) or not any(
        convert(block.get("type"), str, default="") == "tool_result" for block in blocks
    )
    if has_message:
        acts.append(
            UserMessage(
                context_id=0,
                timestamp=timestamp,
                content="\n".join(parts) if parts else None,
                attachments=attachments,
                extra={},
            ),
        )
    results = [
        _read_tool_result(
            record,
            block,
            tools,
            {},
            message_blocks=cast(JSONValue, content) if len(blocks) > 1 else None,
        )
        for block in blocks
        if convert(block.get("type"), str, default="") == "tool_result"
    ]
    acts.extend(results)
    positions = (
        [message_positions[0] if message_positions else len(blocks)]
        if has_message
        else []
    ) + [
        index
        for index, block in enumerate(blocks)
        if convert(block.get("type"), str, default="") == "tool_result"
    ]
    acts = [record for _, record in sorted(zip(positions, acts, strict=True))]
    acts[0] = _with_extra(acts[0], extra)
    out: list[SessionRecord] = list(acts)
    return out


# Axiom 3: each block is its own record. One captured line in 2119 files mixes kinds --
# thinking then a tool call -- so the blocks are read in order rather than by a single
# kind, and the line's residual rides on the first of them.
def _read_assistant(
    record: Mapping[str, object],
    message: Mapping[str, object],
) -> list[SessionRecord]:
    """Read an assistant line into the acts it carries, plus its usage."""
    timestamp = convert_or_none(record.get("timestamp"), str)
    blocks = _blocks(message.get("content"))
    extra = _line_residual(record, consumed=("message",))
    # ``model`` stays on the line. It is the turn's setting in the usual case,
    # but a session can switch models mid-file and a failed call writes
    # claude's own ``<synthetic>`` marker in place of one -- neither of which
    # one turn-wide value can reproduce.
    residual_message = extract_unmodeled_fields(message, {*message_consumed(), "usage"})
    if residual_message:
        extra["message"] = residual_message
    if list(message) != list(_ordered_message(dict(message), extra)):
        extra["$message_keys"] = list(message)
    content_value = message.get("content")
    content_blocks = convert(content_value, list[object])
    canonical_text = (
        len(content_blocks) == 1
        and (block := convert(content_blocks[0], dict[str, object])).keys()
        == {"type", "text"}
        and isinstance(block.get("text"), str)
    )
    if not canonical_text:
        extra["$content_shape"] = [
            _assistant_stencil(value) for value in content_blocks
        ]
    usage = convert(message.get("usage"), dict[str, object], default={})
    if "usage" in message and not usage:
        extra["$usage"] = cast(JSONValue, message["usage"])
    acts: list[AssistantMessage | Thinking | ToolCall] = []
    prose = [
        convert(block.get("text"), str, default="")
        for block in blocks
        if isinstance(block.get("text"), str)
    ]
    if len(prose) > 1:
        extra["$parts"] = len(prose)
    # A block kind nothing here recognizes: kept whole so the line replays,
    # rather than vanishing into an empty content list.
    unknown = [
        extract_unmodeled_fields(block, ())
        for block in blocks
        if convert(block.get("type"), str, default="")
        not in {"tool_use", "thinking", "text"}
    ]
    if unknown:
        extra["$blocks"] = unknown
    for block in blocks:
        kind = convert(block.get("type"), str, default="")
        if kind == "tool_use":
            call_extra = _call_residual(block)
            input_value = convert_or_none(block.get("input"), dict[str, object])
            name = convert_or_none(block.get("name"), str)
            acts.append(
                ToolCall(
                    context_id=0,
                    timestamp=timestamp,
                    call_id=convert(block.get("id"), str, default=""),
                    name=name or "",
                    arguments=json_freeze(input_value or {}),
                    extra=json_freeze(call_extra),
                ),
            )
        elif kind == "thinking":
            encrypted_text = convert(block.get("signature"), str, default=None)
            acts.append(
                Thinking(
                    context_id=0,
                    timestamp=timestamp,
                    content=convert(block.get("thinking"), str, default=None),
                    encrypted=encrypted_text,
                    extra=json_freeze(
                        {"$signature_present": True} if "signature" in block else {},
                    ),
                ),
            )
    if prose or not acts:
        # An empty turn is still a turn: the provider sent it, and dropping it
        # loses a message the model saw. Prose goes back where it sat, which
        # is not always first: a line may think, then speak, then call.
        prose_index = next(
            (i for i, b in enumerate(blocks) if isinstance(b.get("text"), str)),
            len(blocks),
        )
        at = sum(
            convert(block.get("type"), str, default="") in {"tool_use", "thinking"}
            for block in blocks[:prose_index]
        )
        acts.insert(
            at,
            AssistantMessage(
                context_id=0,
                timestamp=timestamp,
                content="\n".join(prose) if prose else None,
            ),
        )
    acts[0] = _with_extra(acts[0], extra)
    out: list[SessionRecord] = list(acts)
    if usage:
        # Claude reports usage on the assistant line rather than one of its
        # own, so it becomes a record here or is lost.
        out.append(
            TokenUsage(context_id=0, timestamp=timestamp, info=json_freeze(usage)),
        )
    return out


def _with_extra[
    T: AssistantMessage | Thinking | ToolCall | UserMessage | AnyToolResult,
](record: T, extra: dict[str, JSONValue]) -> T:
    """Return the record carrying the line's residual, per axiom 10."""
    own = dict(json_unfreeze(record.extra))
    return replace(record, extra=json_freeze(extra | own))


def _call_residual(block: Mapping[str, object]) -> dict[str, JSONValue]:
    """Return a ``tool_use`` block's keys that no field on the call holds."""
    extra = extract_unmodeled_fields(block, {"type", "id", "name", "input"})
    for key in ("name", "input"):
        if (
            key in block
            and block[key] is not None
            and not isinstance(block[key], str if key == "name" else Mapping)
        ):
            extra[key] = cast(JSONValue, block[key])
    return {"$call": extra} if extra else {}


def _read_system(record: Mapping[str, object]) -> SystemMessage:
    """Read a ``system`` line, which is the harness talking, not the model."""
    extra = _line_residual(record, consumed=("content", "subtype"))
    for key in ("content", "subtype"):
        if (
            key in record
            and record[key] is not None
            and not isinstance(record[key], str)
        ):
            extra[f"${key}_value"] = cast(JSONValue, record[key])
    return SystemMessage(
        context_id=0,
        timestamp=convert_or_none(record.get("timestamp"), str),
        content=convert_or_none(record.get("content"), str),
        subtype=convert_or_none(record.get("subtype"), str),
        extra=json_freeze(extra),
    )


def _read_attachment_record(record: Mapping[str, object]) -> ContextState:
    """Read context the harness injected for the model to read."""
    state = convert(record.get("attachment"), dict[str, object], default={})
    # Which key holds the prose. ``content`` is not always prose -- a task
    # reminder writes a LIST there -- so only a string is taken. A message
    # queued while the agent worked (``queued_command``) holds it under
    # ``prompt``; read nowhere else, that message was searchable nowhere.
    prose = next(
        (
            key
            for key in ("text", "content", "prompt")
            if isinstance(state.get(key), str)
        ),
        None,
    )
    # Empty string, not absent: a hook that produced no prose still writes
    # the key, and ``None`` would drop it.
    text = convert(state.get(prose), str, default="") if prose else None
    extra = _line_residual(record, consumed=("attachment",))
    attachment_extra = extract_unmodeled_fields(
        state,
        {"type", *((prose,) if prose else ())},
    )
    if attachment_extra:
        extra["attachment"] = attachment_extra
    if prose is not None:
        # Which key holds it and where it sat: a hook result writes its prose
        # fifth, after the hook's own name and the call it answered.
        extra["$prose"] = [prose, list(state).index(prose)]
    return ContextState(
        context_id=0,
        timestamp=convert_or_none(record.get("timestamp"), str),
        kind=convert(state.get("type"), str, default=""),
        content=text,
        extra=json_freeze(extra),
    )


def _read_tool_result(
    record: Mapping[str, object],
    block: Mapping[str, object],
    tools: Mapping[str, tuple[str, str | None]],
    extra: dict[str, JSONValue],
    *,
    message_blocks: JSONValue | None = None,
) -> AnyToolResult:
    """Read a tool's answer as the record for what the tool DID."""
    content = block.get("content")
    parts: list[str] = []
    attachments: list[Attachment] = []
    if isinstance(content, str):
        parts.append(content)
    else:
        for part in convert(
            [] if content is None else content,
            list[dict[str, object]],
        ):
            if found := _read_attachment(part):
                attachments.append(found)
            elif convert(part.get("type"), str, default="") == "text":
                parts.append(convert(part.get("text"), str, default=""))
    text = "\n".join(parts)
    call_id = convert(block.get("tool_use_id"), str, default="")
    name, command = tools.get(call_id, ("", None))
    result = convert(record.get("toolUseResult"), dict[str, object], default={})
    residual_block = extract_unmodeled_fields(block, {"type", "tool_use_id", "content"})
    shape: dict[str, JSONValue] = {
        "block": residual_block,
        # The block's own key order, which is per-block rather than per-line:
        # 136 captured files write it both ways within one file.
        "keys": list(block),
        "tool_name": name,
    }
    if message_blocks is not None:
        shape["message_blocks"] = message_blocks
    # What the model actually read. A typed result's fields come from
    # ``toolUseResult``, which is a DIFFERENT rendering -- a failed Bash call
    # answers with a bare string, and a read image nests base64 under ``file``
    # -- so the block's own content is kept rather than rebuilt from it.
    #
    # Unless the record's own fields already spell it: a shell result renders
    # as its two streams joined, and an uncategorized one as its content, so
    # storing the text too wrote the same output twice.
    if not isinstance(content, str):
        shape["content"] = cast(JSONValue, content)
    elif content == _rendered(name, result):
        shape["$text"] = True
    else:
        shape["text"] = content
    return _typed_result(
        name,
        result,
        record,
        call_id=call_id,
        command=command,
        failed="is_error" in block and block.get("is_error") is not False,
        text=text,
        attachments=tuple(attachments),
        extra=extra,
        shape=shape,
    )


# Two tools render from fields the record already holds, so storing the block's text
# beside them wrote the same output twice -- 0.20 MB of the 0.47 MB every replay note
# cost across 40 captured sessions.
def _rendered(name: str, result: Mapping[str, object]) -> str | None:
    """Return what the record's own fields spell the block's content as."""
    if name == "Bash":
        stdout = read_field_keeping_invalid(result, "stdout", str)
        stderr = read_field_keeping_invalid(result, "stderr", str)
        return _shell_text(
            stdout if isinstance(stdout, str) else "",
            stderr if isinstance(stderr, str) else "",
        )
    if name == "Read":
        content = convert(result.get("file"), dict[str, object], default={}).get(
            "content",
        )
        return _numbered(content) if isinstance(content, str) else None
    return None


def _shell_text(stdout: str, stderr: str) -> str:
    """Return the block content a shell result's two streams spell."""
    return stdout + stderr


# Verified against every captured read: one-based, a tab after the number, and JOINED by
# newlines rather than terminated by them -- the final line carries no trailing newline
# even when the file's content does.
def _numbered(content: str) -> str:
    """Return the ``cat -n`` rendering claude shows the model for a read."""
    return "\n".join(
        f"{index}\t{line}" for index, line in enumerate(content.split("\n"), 1)
    )


# Dispatch is on the tool's name because that is what claude reports; the class it
# selects is provider-neutral, which is what lets the record cross to a CLI that never
# had a tool by this name.
def _typed_result(
    name: str,
    result: Mapping[str, object],
    record: Mapping[str, object],
    *,
    call_id: str,
    command: str | None,
    failed: bool,
    text: str,
    attachments: tuple[Attachment, ...],
    extra: dict[str, JSONValue],
    shape: dict[str, JSONValue],
) -> AnyToolResult:
    """Return the result as the record for what its tool did."""
    timestamp = convert_or_none(record.get("timestamp"), str)
    structured_result = isinstance(record.get("toolUseResult"), Mapping)
    if name == "Bash":
        stdout = read_field_keeping_invalid(result, "stdout", str)
        stderr = read_field_keeping_invalid(result, "stderr", str)
        shell = ShellCommandResult(
            context_id=0,
            timestamp=timestamp,
            call_id=call_id,
            stdout=stdout if isinstance(stdout, str) else "",
            stderr=stderr if isinstance(stderr, str) else "",
            extra=json_freeze(
                _result_extra(
                    extra,
                    shape,
                    extract_unmodeled_fields(
                        result,
                        fields={"stdout": stdout, "stderr": stderr},
                    ),
                    structured=structured_result,
                ),
            ),
        )
        return (
            lift_shell_result(
                shell,
                command=command,
                succeeded=structured_result
                and not failed
                and result.get("interrupted") is False,
            )
            or shell
        )
    if name == "Read":
        # The payload nests under ``file``: a text read carries the content, an
        # image carries base64 that already arrived as an attachment.
        file_state = read_field_keeping_invalid(result, "file", dict[str, object])
        read_data = convert(
            file_state if isinstance(file_state, Mapping) else {},
            dict[str, object],
        )
        path = read_field_keeping_invalid(read_data, "filePath", str)
        content = read_field_keeping_invalid(read_data, "content", str)
        if isinstance(file_state, Mapping):
            stored = dict(extract_unmodeled_fields(result, {"file"}))
            stored["file"] = extract_unmodeled_fields(
                read_data,
                fields={"filePath": path, "content": content},
            )
        else:
            stored = dict(extract_unmodeled_fields(result, fields={"file": file_state}))
        return FileReadResult(
            context_id=0,
            timestamp=timestamp,
            call_id=call_id,
            path=path if isinstance(path, str) else None,
            content=content if isinstance(content, str) else None,
            extra=json_freeze(
                _result_extra(extra, shape, stored, structured=structured_result),
            ),
        )
    if name == "Write":
        path = read_field_keeping_invalid(result, "filePath", str)
        content = read_field_keeping_invalid(result, "content", str)
        return FileWriteResult(
            context_id=0,
            timestamp=timestamp,
            call_id=call_id,
            path=path if isinstance(path, str) else None,
            content=content if isinstance(content, str) else None,
            extra=json_freeze(
                _result_extra(
                    extra,
                    shape,
                    extract_unmodeled_fields(
                        result,
                        fields={"filePath": path, "content": content},
                    ),
                    structured=structured_result,
                ),
            ),
        )
    if name == "Edit":
        path = read_field_keeping_invalid(result, "filePath", str)
        old = read_field_keeping_invalid(result, "oldString", str)
        new = read_field_keeping_invalid(result, "newString", str)
        if "structuredPatch" in result:
            # What the splice said when the patch was rendered, so the writer
            # can tell an edited splice from an untouched one.
            shape["splice"] = [
                old if isinstance(old, str) else None,
                new if isinstance(new, str) else None,
            ]
        return FileEditResult(
            context_id=0,
            timestamp=timestamp,
            call_id=call_id,
            path=path if isinstance(path, str) else None,
            # Claude names the TEXT it replaced and never says where, so the
            # splice carries no position: resolving one would need the file.
            # Its ``structuredPatch`` says the same thing WITH positions, but
            # it is a rendering of this pair rather than a second edit, and
            # the writer rebuilds it from ``shape`` -- so only the pair the
            # provider stated becomes a splice.
            edits=(
                (
                    Splice(
                        before=old if isinstance(old, str) else None,
                        after=new if isinstance(new, str) else None,
                    ),
                )
                if isinstance(old, str) or isinstance(new, str)
                else ()
            ),
            extra=json_freeze(
                _result_extra(
                    extra,
                    shape,
                    extract_unmodeled_fields(
                        result,
                        fields={
                            "filePath": path,
                            "oldString": old,
                            "newString": new,
                        },
                    ),
                    structured=structured_result,
                ),
            ),
        )
    if name == "WebSearch":
        # Claude nests the rows one level deeper than codex -- under a
        # ``tool_use_id`` per search -- and reports only title and url, so the
        # ids and the nesting stay in the residual.
        rows = [
            convert(cast(dict[str, object], row), dict[str, object])
            for group in convert(result.get("results"), list[object], default=[])
            if isinstance(group, dict)
            for row in convert(
                cast(dict[str, object], group).get("content"),
                list[object],
                default=[],
            )
            if isinstance(row, dict)
        ]
        query = read_field_keeping_invalid(result, "query", str)
        duration = read_field_keeping_invalid(result, "durationSeconds", float)
        result_groups = read_field_keeping_invalid(result, "results", list[object])
        # ``results`` is not a row list: it holds one group per search plus,
        # sometimes, the model's own prose summary. The shape is kept -- each
        # group's id and how many of the flattened rows it owns -- so the rows
        # go back inside the group they came from.
        groups: list[JSONValue] = []
        for group in convert(result.get("results"), list[object], default=[]):
            if isinstance(group, Mapping):
                group = cast(Mapping[str, object], group)
                found = convert(group, dict[str, object])
                groups.append(
                    {
                        "template": cast(JSONValue, group),
                        "rows": sum(
                            isinstance(row, Mapping)
                            for row in convert(
                                found.get("content"),
                                list[object],
                                default=[],
                            )
                        ),
                    },
                )
            else:
                groups.append(cast(JSONValue, group))
        if "results" in result:
            shape["rows"] = groups
        return WebSearchResults(
            context_id=0,
            timestamp=timestamp,
            call_id=call_id,
            query=query if isinstance(query, str) else None,
            duration_sec=duration if isinstance(duration, float) else None,
            content=tuple(_search_rows(rows)),
            extra=json_freeze(
                _result_extra(
                    extra,
                    shape,
                    extract_unmodeled_fields(
                        result,
                        fields={
                            "query": query,
                            "durationSeconds": duration,
                            "results": result_groups,
                        },
                    ),
                    structured=structured_result,
                ),
            ),
        )
    if name == "WebFetch":
        url = read_field_keeping_invalid(result, "url", str)
        content_state = read_field_keeping_invalid(result, "result", str)
        code = read_field_keeping_invalid(result, "code", int)
        duration = read_field_keeping_invalid(result, "durationMs", float)
        size = read_field_keeping_invalid(result, "bytes", int)
        if isinstance(duration, float):
            shape["duration_ms"] = cast(JSONValue, result["durationMs"])
        return WebFetchResult(
            context_id=0,
            timestamp=timestamp,
            call_id=call_id,
            url=url if isinstance(url, str) else None,
            content=content_state if isinstance(content_state, str) else None,
            code=code if isinstance(code, int) else None,
            duration_sec=duration / 1000 if isinstance(duration, float) else None,
            size=size if isinstance(size, int) else None,
            extra=json_freeze(
                _result_extra(
                    extra,
                    shape,
                    extract_unmodeled_fields(
                        result,
                        fields={
                            "url": url,
                            "result": content_state,
                            "code": code,
                            "durationMs": duration,
                            "bytes": size,
                        },
                    ),
                    structured=structured_result,
                ),
            ),
        )
    if name == "Agent":
        agent_id = read_field_keeping_invalid(result, "agentId", str)
        agent_kind = read_field_keeping_invalid(result, "agentType", str)
        prompt = read_field_keeping_invalid(result, "prompt", str)
        content_state = read_field_keeping_invalid(result, "content", list[object])
        model = read_field_keeping_invalid(result, "resolvedModel", str)
        state = read_field_keeping_invalid(result, "status", str)
        tokens = read_field_keeping_invalid(result, "totalTokens", int)
        duration = read_field_keeping_invalid(result, "totalDurationMs", float)
        tool_calls = read_field_keeping_invalid(result, "totalToolUseCount", int)
        output_file = read_field_keeping_invalid(result, "outputFile", str)
        if isinstance(duration, float):
            shape["duration_ms"] = cast(JSONValue, result["totalDurationMs"])
        # A subagent answers in blocks, never a bare string, so its prose is
        # joined here and the block wrapper noted for the writer.
        blocks = convert(result.get("content"), list[dict[str, object]], default=[])
        if isinstance(result.get("content"), list):
            shape["agent_blocks"] = cast(JSONValue, result["content"])
        return AgentStatusResult(
            context_id=0,
            timestamp=timestamp,
            call_id=call_id,
            agent_id=agent_id if isinstance(agent_id, str) else None,
            agent_kind=agent_kind if isinstance(agent_kind, str) else None,
            prompt=prompt if isinstance(prompt, str) else None,
            content="\n".join(convert(b.get("text"), str, default="") for b in blocks)
            or None,
            model=model if isinstance(model, str) else None,
            state=state if isinstance(state, str) else None,
            tokens=tokens if isinstance(tokens, int) else None,
            duration_sec=duration / 1000 if isinstance(duration, float) else None,
            tool_calls=tool_calls if isinstance(tool_calls, int) else None,
            output_file=output_file if isinstance(output_file, str) else None,
            extra=json_freeze(
                _result_extra(
                    extra,
                    shape,
                    extract_unmodeled_fields(
                        result,
                        fields={
                            "agentId": agent_id,
                            "agentType": agent_kind,
                            "prompt": prompt,
                            "content": content_state,
                            "resolvedModel": model,
                            "status": state,
                            "totalTokens": tokens,
                            "totalDurationMs": duration,
                            "totalToolUseCount": tool_calls,
                            "outputFile": output_file,
                        },
                    ),
                    structured=structured_result,
                ),
            ),
        )
    # Every other tool -- Skill, ToolSearch, an MCP server's. Its shape is the
    # tool's own, so naming a type for it would be inventing one.
    return UncategorizedToolResult(
        context_id=0,
        timestamp=timestamp,
        call_id=call_id,
        content=text or None,
        attachments=attachments,
        extra=json_freeze(
            _result_extra(
                extra,
                shape,
                extract_unmodeled_fields(result),
                structured=structured_result,
            ),
        ),
    )


def _result_extra(
    extra: dict[str, JSONValue],
    shape: dict[str, JSONValue],
    stored: JSONValue,
    *,
    structured: bool,
) -> dict[str, JSONValue]:
    """Keep structured replay state without replacing a scalar result."""
    result = extra | {"$result": shape}
    if structured:
        result["toolUseResult"] = stored
    return result


def _search_rows(rows: Sequence[Mapping[str, object]]) -> list[WebSearchResult]:
    """Read a search's result rows. Claude reports no snippet."""
    return [
        WebSearchResult(
            url=convert_or_none(row.get("url"), str),
            title=convert_or_none(row.get("title"), str),
            snippet=convert_or_none(row.get("snippet"), str),
        )
        for row in rows
    ]


def _read_attachment(block: Mapping[str, object]) -> Attachment | None:
    """Read an image block, which is the only binary claude inlines."""
    if convert(block.get("type"), str, default="") != "image":
        return None
    source = convert(block.get("source"), dict[str, object], default={})
    if convert(source.get("type"), str, default="") != "base64":
        return None
    try:
        data = base64.b64decode(
            convert(source.get("data"), str, default=""),
            validate=True,
        )
    except (binascii.Error, ValueError):
        return None
    return Attachment(
        mime_descriptor=convert(source.get("media_type"), str, default=""),
        data=data,
    )


def _media_first(block: Mapping[str, object]) -> bool:
    """Whether the source named the media type before the base64 payload."""
    source = convert(block.get("source"), dict[str, object], default={})
    keys = [key for key in source if key in {"media_type", "data"}]
    return keys[:1] == ["media_type"]
