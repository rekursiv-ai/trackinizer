"""Read and write a Gemini CLI session document.

Unlike claude and codex, gemini keeps ONE JSON object and rewrites it whole on
every turn. There are no lines to follow, so :func:`normalize` reads the entire
document and yields the records it holds; a caller watching the file re-reads
it and takes the records past the ones it already has.

That difference is confined to the reader. The records it produces are the same
provider-neutral ones every adapter emits, so a gemini session converts to
claude or codex through the ordinary :mod:`convert` path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TextIO
from uuid import NAMESPACE_DNS, UUID, uuid5

import json

from treekle import (
    MutablePlainTree,
    ReadError,
    from_plain,
    immutable,
    json_loads,
    mutable,
)

from trackinizer.sagent.scripts.convert_session.provider_fields import (
    Stamped,
    kept_spelling,
    read_or_default,
    stamped,
)
from trackinizer.sagent.scripts.convert_session.unmodeled import (
    extract_unmodeled_fields,
    field_state,
    restore_unmodeled_fields,
)
from trackinizer.sagent.session.types import (
    AssistantMessage,
    ContextClear,
    IncompleteRecord,
    SessionContext,
    SessionRecord,
    ToolCall,
    UncategorizedRecord,
    UserMessage,
    stated_timestamp,
    timestamp_as_written,
)


if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping, Sequence


__all__ = ["denormalize", "normalize"]


def normalize(stream: TextIO) -> Iterator[SessionRecord]:
    """Normalize a Gemini session document into its records.

    Gemini rewrites ONE object per turn rather than appending, so there is no
    line to follow: the document is read whole and its records yielded. The
    stream signature is the same as every other adapter's, which is what lets
    a caller read any format without knowing which it has.

    Args:
      stream: Gemini session JSON text stream.

    Yields:
      record: Each record the document carries, in stream order.

    """
    yield from _read(stream.read())


def denormalize(
    records: Iterable[SessionRecord],
    stream: TextIO,
    *,
    seed: UUID = NAMESPACE_DNS,
) -> None:
    """Denormalize records as a Gemini session document.

    Args:
      records: Provider-neutral records, in stream order.
      stream: Destination text stream.
      seed: Namespace for the ``sessionId`` a FOREIGN stream is missing. The
        format is sniffed by that key, so a document without one reads as no
        format at all. The id is derived from the messages written, so one
        stream converts to one id and two different streams to two.

    """
    ordered = [
        record
        for record in records
        # The opening state records are DERIVED, so they write no document
        # field: the declaration they carry is restored below.
        if not isinstance(record, ContextClear)
    ]
    body = [record for record in ordered if not isinstance(record, SessionContext)]
    if body and all(isinstance(record, IncompleteRecord) for record in body):
        # The document never parsed, so its bytes are all that was kept. Only
        # when they are ALL that was read: claude and codex both emit one of
        # these for a blank line, so a crossed-in session routinely carries one
        # -- and treating its presence as "unparsed" wrote that single line and
        # discarded every real turn.
        for record in body:
            assert isinstance(record, IncompleteRecord)
            _ = stream.write(record.text)
        return
    declared = next(
        (record for record in ordered if isinstance(record, SessionContext)),
        None,
    )
    stored = mutable(declared.extra) if declared is not None else {}
    # Gemini's reader states ``$compact``; another CLI's bookkeeping is no message.
    foreign = declared is not None and "$compact" not in stored
    compact = bool(stored.pop("$compact", None))
    messages = _write_messages(
        [record for record in body if not isinstance(record, UncategorizedRecord)]
        if foreign
        else body,
    )
    document: dict[str, MutablePlainTree] = {}
    # Only keys a gemini document itself carries. Another adapter's metadata
    # names its own conventions -- claude states ``ascii_escaped`` and an
    # escape bitmap -- and writing those through put keys on the wire gemini
    # never authored, which also cost the document its ``sessionId`` and left
    # it detected as no format at all.
    if "sessionId" in stored:
        document.update(stored)
    else:
        # Derived from WHAT is written, so two conversions of one stream agree
        # and two streams that merely have the same length do not collide.
        document["sessionId"] = str(
            uuid5(seed, json.dumps(messages, ensure_ascii=False, sort_keys=True)),
        )
    document["messages"] = messages
    json.dump(
        document,
        stream,
        ensure_ascii=False,
        separators=(",", ":") if compact else (", ", ": "),
    )


# A ``gemini`` turn carries prose AND any calls it made, which axiom 3 keeps as sibling
# records rather than one nested blob.
def _read_message(message: Mapping[str, object]) -> Sequence[Stamped]:
    """Normalize one gemini message into its acts."""
    kind = read_or_default(message.get("type"), str, default="")
    if kind not in {"user", "gemini"}:
        return [
            UncategorizedRecord(
                kind=kind,
                payload=immutable(message),
            ),
        ]
    # A modeled field of the wrong type raises, and the caller keeps the message whole.
    content = from_plain(message["content"], str) if "content" in message else ""
    spelled = from_plain(message["timestamp"], str) if "timestamp" in message else None
    calls = (
        from_plain(message["toolCalls"], list[object]) if "toolCalls" in message else []
    )
    extra = immutable(
        extract_unmodeled_fields(
            message,
            fields={
                key: message[key]
                for key in ("type", "content", "timestamp", "toolCalls")
                if key in message
            },
        ),
    )
    read: list[Stamped] = (
        [UserMessage(content=content, extra=extra)]
        if kind == "user"
        else [
            AssistantMessage(content=content, extra=extra),
            *(
                ToolCall(
                    call_id=from_plain(call.get("id"), str, default=""),
                    name=from_plain(call.get("name"), str, default=""),
                    arguments=immutable(
                        from_plain(
                            call.get("args"),
                            dict[str, object],
                            default=dict[str, object](),
                        ),
                    ),
                    extra=immutable(
                        extract_unmodeled_fields(call, ("id", "name", "args")),
                    ),
                )
                for call in (from_plain(value, dict[str, object]) for value in calls)
            ),
        ]
    )
    # Each record of the message holds the message's time, as gemini spelled it.
    return read if spelled is None else [stamped(item, spelled) for item in read]


# Every value THAWED: a record's residual is frozen to arbitrary depth, and ``json``
# cannot encode the ``mappingproxy`` a one-level copy leaves inside.
def _write_messages(
    records: Iterable[SessionRecord],
) -> list[MutablePlainTree]:
    """Rebuild the document's message list from the stream's records."""
    out: list[MutablePlainTree] = []
    # The last message written, when it is a ``gemini`` turn a call can join.
    open_turn: dict[str, MutablePlainTree] | None = None
    for record in records:
        match record:
            case UserMessage():
                out.append(_written_message(record, "user"))
                open_turn = None
            case AssistantMessage():
                open_turn = _written_message(record, "gemini")
                out.append(open_turn)
            case ToolCall():
                # A call belongs to the turn that made it: gemini nests them,
                # so the sibling record folds back into the prior message.
                #
                # Only into a ``gemini`` turn. A call can follow a USER one --
                # codex states a web search as an end event with no call of its
                # own, and a fused session can open mid-conversation -- and
                # folding it there claimed the person made the call. Asserting
                # a turn was already open instead aborted the conversion.
                if open_turn is None:
                    # The turn is written at the call's time, as the call spells it.
                    open_turn = _written_message(
                        AssistantMessage(
                            timestamp=record.timestamp,
                            extra=kept_spelling(stated_timestamp(record)),
                        ),
                        "gemini",
                    )
                    out.append(open_turn)
                calls = open_turn.setdefault("toolCalls", [])
                assert isinstance(calls, list)
                # A call has no time of its own in gemini's document, so the
                # spelling it keeps is no key of gemini's.
                calls.append(
                    {
                        "id": record.call_id,
                        "name": record.name,
                        "args": mutable(record.arguments),
                        **{
                            key: value
                            for key, value in mutable(record.extra).items()
                            if key != "$timestamp_as_written"
                        },
                    },
                )
            case UncategorizedRecord():
                payload = mutable(record.payload)
                # A message the reader could not type, kept as it was: possibly
                # no object at all, so it rides under a key of its own.
                out.append(payload.get("$message", payload))
                open_turn = None
            case _:
                # Every other IR record kind came from another provider; a
                # gemini document has no shape for it, so a conversion into
                # this format reports lossy rather than inventing one.
                continue
    return out


def _written_message(
    record: UserMessage | AssistantMessage,
    kind: str,
) -> dict[str, MutablePlainTree]:
    """Return the message a turn came from, its keys where gemini wrote them."""
    content = record.content or ""
    stored = mutable(record.extra)
    written = timestamp_as_written(record.timestamp, stored)
    _ = stored.pop("$timestamp_as_written", None)
    if field_state(stored, "type") is None:
        # Another CLI's turn, in gemini-cli's own key order.
        return {
            **({"timestamp": written} if written is not None else {}),
            "type": kind,
            "content": content,
            **stored,
        }
    return mutable(
        immutable(
            restore_unmodeled_fields(
                stored,
                {
                    "type": kind,
                    "content": content,
                    "timestamp": written,
                    "toolCalls": [],
                },
            ),
        ),
    )


def _read(text: str) -> list[SessionRecord]:
    """Return the records one whole document holds."""
    # Settings before the acts they govern, then the context the window opens
    # from. Gemini declares neither a prompt nor an escaping convention, so
    # both state only what the format itself fixes.
    out: list[SessionRecord] = [
        SessionContext(encoding=immutable({"newline_terminated": True})),
        ContextClear(extra=immutable({"$opens": True})),
    ]
    if not text.strip():
        return out
    try:
        decoded = json_loads(text)
    except json.JSONDecodeError:
        return [*out, IncompleteRecord(text=text)]
    # A document that is not an object carries no session: an array or a bare
    # scalar is kept verbatim rather than read as an empty one, which would
    # silently discard whatever the file did hold.
    if not isinstance(decoded, dict) or not decoded:
        return [*out, IncompleteRecord(text=text)]
    document = from_plain(decoded, dict[str, object])
    try:
        messages = from_plain(
            document.get("messages"),
            list[object],
            default=list[object](),
        )
    except ReadError:
        # No message list, no messages to keep one by one: the document is kept whole.
        return [*out, IncompleteRecord(text=text)]
    # Everything outside ``messages`` is the file's own declaration, which is
    # settings: it rides the opening context rather than a record of its own.
    extra = dict(extract_unmodeled_fields(document, ("messages",)))
    # Whether the file separates compactly, decided by re-encoding the parsed
    # document that way and seeing whether it reproduces the input. Both
    # spellings occur, and guessing one rewrites the other's bytes.
    extra["$compact"] = (
        json.dumps(decoded, ensure_ascii=False, separators=(",", ":")) == text
    )
    out[0] = SessionContext(
        encoding=immutable({"newline_terminated": True}),
        extra=immutable(extra),
    )
    for message in messages:
        # One message is the unit a malformed field can spoil: it raised out of
        # ``normalize`` and lost the document. Kept whole instead, it is
        # written back exactly as it was read.
        try:
            out.extend(_read_message(from_plain(message, dict[str, object])))
        except ReadError:
            out.append(
                UncategorizedRecord(
                    kind="",
                    payload=immutable({"$message": message}),
                ),
            )
    return out
