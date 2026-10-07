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

from trackinizer.lib.agent.sessions.provider_fields import read_or_default
from trackinizer.lib.agent.types.sessions import (
    AssistantMessage,
    ContextClear,
    IncompleteRecord,
    SessionRecord,
    ToolCall,
    TurnContext,
    UncategorizedRecord,
    UserMessage,
)
from trackinizer.lib.codec import (
    MutablePlainTree,
    ReadError,
    from_plain,
    immutable,
    loads,
    mutable,
)
from trackinizer.lib.custom_json import extract_unmodeled_fields


if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping


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
    body = [record for record in ordered if not isinstance(record, TurnContext)]
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
        (record for record in ordered if isinstance(record, TurnContext)),
        TurnContext(),
    )
    stored = mutable(declared.extra)
    compact = bool(stored.pop("$compact", False))
    messages = _write_messages(body)
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
def _read_message(message: Mapping[str, object]) -> list[SessionRecord]:
    """Normalize one gemini message into its acts."""
    kind = read_or_default(message.get("type"), str, default="")
    if kind == "user":
        return [
            UserMessage(
                content=from_plain(message.get("content"), str, default=""),
                timestamp=read_or_default(message.get("$timestamp"), str, default=None),
                extra=immutable(
                    extract_unmodeled_fields(
                        message,
                        ("type", "content", "$timestamp"),
                    ),
                ),
            ),
        ]
    if kind != "gemini":
        return [
            UncategorizedRecord(
                kind=kind,
                payload=immutable(message),
            ),
        ]
    calls = from_plain(message.get("toolCalls"), list[object], default=[])
    stamp = read_or_default(message.get("$timestamp"), str, default=None)
    kept = dict(
        extract_unmodeled_fields(
            message,
            ("type", "content", "toolCalls", "$timestamp"),
        ),
    )
    if "toolCalls" in message and not calls:
        # Axiom 2: an empty list is a VALUE, not absence. The writer rebuilds
        # ``toolCalls`` from the sibling ToolCall records, of which there are
        # none here -- so without this the key would vanish on rewrite and the
        # document would not match the bytes it was read from.
        kept["$tool_calls_present"] = True
    return [
        AssistantMessage(
            content=from_plain(message.get("content"), str, default=""),
            timestamp=stamp,
            extra=immutable(kept),
        ),
        *(
            ToolCall(
                call_id=from_plain(call.get("id"), str, default=""),
                name=from_plain(call.get("name"), str, default=""),
                timestamp=stamp,
                arguments=immutable(
                    from_plain(call.get("args"), dict[str, object], default={}),
                ),
                extra=immutable(
                    extract_unmodeled_fields(call, ("id", "name", "args")),
                ),
            )
            for call in (
                from_plain(value, dict[str, object], default={}) for value in calls
            )
        ),
    ]


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
                out.append(
                    {
                        "type": "user",
                        "content": record.content or "",
                        # A gemini document states no per-message time, so a
                        # stamp from another provider survives only here. Under
                        # a ``$`` key, which the reader strips: a native
                        # document has none, and adding one would rewrite bytes
                        # gemini itself wrote.
                        **(
                            {"$timestamp": record.timestamp} if record.timestamp else {}
                        ),
                        **mutable(record.extra),
                    },
                )
                open_turn = None
            case AssistantMessage():
                extra = mutable(record.extra)
                empty_calls = extra.pop("$tool_calls_present", None) is not None
                open_turn = {
                    "type": "gemini",
                    "content": record.content or "",
                    **({"$timestamp": record.timestamp} if record.timestamp else {}),
                    **extra,
                    **({"toolCalls": []} if empty_calls else {}),
                }
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
                    open_turn = {"type": "gemini", "content": ""}
                    if record.timestamp:
                        open_turn["$timestamp"] = record.timestamp
                    out.append(open_turn)
                calls = open_turn.setdefault("toolCalls", [])
                assert isinstance(calls, list)
                calls.append(
                    {
                        "id": record.call_id,
                        "name": record.name,
                        "args": mutable(record.arguments),
                        **mutable(record.extra),
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


def _read(text: str) -> list[SessionRecord]:
    """Return the records one whole document holds."""
    # Settings before the acts they govern, then the context the window opens
    # from. Gemini declares neither a prompt nor an escaping convention, so
    # both state only what the format itself fixes.
    out: list[SessionRecord] = [
        TurnContext(encoding=immutable({"newline_terminated": True})),
        ContextClear(extra=immutable({"$opens": True})),
    ]
    if not text.strip():
        return out
    try:
        decoded = loads(text)
    except json.JSONDecodeError:
        return [*out, IncompleteRecord(text=text)]
    # A document that is not an object carries no session: an array or a bare
    # scalar is kept verbatim rather than read as an empty one, which would
    # silently discard whatever the file did hold.
    if not isinstance(decoded, dict) or not decoded:
        return [*out, IncompleteRecord(text=text)]
    document = from_plain(decoded, dict[str, object])
    try:
        messages = from_plain(document.get("messages"), list[object], default=[])
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
    out[0] = TurnContext(
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
