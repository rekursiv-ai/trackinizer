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

from collections.abc import Iterable, Iterator, Mapping
from typing import TextIO
from uuid import UUID, uuid5

import json

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
from trackinizer.lib.custom_json import (
    DictCodec,
    JSONValue,
    ListCodec,
    MutableJSONValue,
    StrCodec,
    decode_or_none,
    json_freeze,
    json_unfreeze,
    residual,
)


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


def _read(text: str) -> list[SessionRecord]:
    """The records one whole document holds."""
    # Settings before the acts they govern, then the context the window opens
    # from. Gemini declares neither a prompt nor an escaping convention, so
    # both state only what the format itself fixes.
    out: list[SessionRecord] = [
        TurnContext(encoding=json_freeze({"newline_terminated": True})),
        ContextClear(extra=json_freeze({"$opens": True})),
    ]
    if not text.strip():
        return out
    try:
        decoded: object = json.loads(text)
    except json.JSONDecodeError:
        return [*out, IncompleteRecord(text=text)]
    # A document that is not an object carries no session: an array or a bare
    # scalar is kept verbatim rather than read as an empty one, which would
    # silently discard whatever the file did hold.
    document: dict[str, object] = DictCodec.coerce(decoded)
    if not document:
        return [*out, IncompleteRecord(text=text)]
    messages = ListCodec.coerce(document.get("messages"))
    # Everything outside ``messages`` is the file's own declaration, which is
    # settings: it rides the opening context rather than a record of its own.
    extra = dict(json_unfreeze(residual(document, ("messages",))))
    # Whether the file separates compactly, decided by re-encoding the parsed
    # document that way and seeing whether it reproduces the input. Both
    # spellings occur, and guessing one rewrites the other's bytes.
    extra["$compact"] = (
        json.dumps(decoded, ensure_ascii=False, separators=(",", ":")) == text
    )
    out[0] = TurnContext(
        encoding=json_freeze({"newline_terminated": True}), extra=json_freeze(extra)
    )
    for message in messages:
        out.extend(_read_message(DictCodec.coerce(message)))
    return out


def denormalize(records: Iterable[SessionRecord], stream: TextIO) -> None:
    """Denormalize records as a Gemini session document.

    Args:
      records: Provider-neutral records, in stream order.
      stream: Destination text stream.

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
        (record for record in ordered if isinstance(record, TurnContext)), TurnContext()
    )
    stored = dict(json_unfreeze(declared.extra))
    compact = bool(stored.pop("$compact", False))
    document: dict[str, MutableJSONValue] = {}
    # Only keys a gemini document itself carries. Another adapter's metadata
    # names its own conventions -- claude states ``ascii_escaped`` and an
    # escape bitmap -- and writing those through put keys on the wire gemini
    # never authored, which also cost the document its ``sessionId`` and left
    # it detected as no format at all.
    if "sessionId" in stored:
        document.update(stored)
    else:
        # A stream from another provider declares no gemini id, and identity is
        # the caller's rather than the session's -- but the format is SNIFFED
        # by this key's presence, so a document without one reads as no format
        # at all. Derived from the records so two conversions agree.
        document["sessionId"] = str(uuid5(_GEMINI_NAMESPACE, str(len(body))))
    document["messages"] = [
        json_unfreeze(json_freeze(message)) for message in _write_messages(body)
    ]
    json.dump(
        document,
        stream,
        ensure_ascii=False,
        separators=(",", ":") if compact else (", ", ": "),
    )


def _read_message(message: Mapping[str, object]) -> list[SessionRecord]:
    """Normalize one gemini message into its acts.

    A ``gemini`` turn carries prose AND any calls it made, which axiom 3 keeps
    as sibling records rather than one nested blob.
    """
    kind = StrCodec.coerce(message.get("type"))
    if kind == "user":
        return [
            UserMessage(
                content=StrCodec.coerce(message.get("content")),
                timestamp=decode_or_none(str, message.get("$timestamp")),
                extra=json_freeze(residual(message, ("type", "content", "$timestamp"))),
            )
        ]
    if kind != "gemini":
        return [
            UncategorizedRecord(
                kind=kind,
                payload=json_freeze(DictCodec.coerce(message)),
            )
        ]
    calls = ListCodec.coerce(message.get("toolCalls"))
    stamp = decode_or_none(str, message.get("$timestamp"))
    kept = dict(
        json_unfreeze(residual(message, ("type", "content", "toolCalls", "$timestamp")))
    )
    if "toolCalls" in message and not calls:
        # Axiom 2: an empty list is a VALUE, not absence. The writer rebuilds
        # ``toolCalls`` from the sibling ToolCall records, of which there are
        # none here -- so without this the key would vanish on rewrite and the
        # document would not match the bytes it was read from.
        kept["$tool_calls_present"] = True
    return [
        AssistantMessage(
            content=StrCodec.coerce(message.get("content")),
            timestamp=stamp,
            extra=json_freeze(kept),
        ),
        *(
            ToolCall(
                call_id=StrCodec.coerce(DictCodec.coerce(call).get("id")),
                name=StrCodec.coerce(DictCodec.coerce(call).get("name")),
                timestamp=stamp,
                arguments=json_freeze(
                    DictCodec.coerce(DictCodec.coerce(call).get("args"))
                ),
                extra=json_freeze(
                    residual(DictCodec.coerce(call), ("id", "name", "args"))
                ),
            )
            for call in calls
        ),
    ]


def _write_messages(records: Iterable[SessionRecord]) -> list[dict[str, JSONValue]]:
    """Rebuild the document's message list from the stream's records."""
    out: list[dict[str, JSONValue]] = []
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
                        **json_unfreeze(record.extra),
                    }
                )
            case AssistantMessage():
                extra = dict(json_unfreeze(record.extra))
                empty_calls = extra.pop("$tool_calls_present", None) is not None
                out.append(
                    {
                        "type": "gemini",
                        "content": record.content or "",
                        **(
                            {"$timestamp": record.timestamp} if record.timestamp else {}
                        ),
                        **extra,
                        **({"toolCalls": []} if empty_calls else {}),
                    }
                )
            case ToolCall():
                # A call belongs to the turn that made it: gemini nests them,
                # so the sibling record folds back into the prior message.
                #
                # Only into a ``gemini`` turn. A call can follow a USER one --
                # codex states a web search as an end event with no call of its
                # own, and a fused session can open mid-conversation -- and
                # folding it there claimed the person made the call. Asserting
                # a turn was already open instead aborted the conversion.
                open_turn = out and StrCodec.coerce(out[-1].get("type")) == "gemini"
                if not open_turn:
                    opened: dict[str, JSONValue] = {"type": "gemini", "content": ""}
                    if record.timestamp:
                        opened["$timestamp"] = record.timestamp
                    out.append(opened)
                call: dict[str, JSONValue] = {
                    "id": record.call_id,
                    "name": record.name,
                    "args": json_unfreeze(record.arguments),
                    **json_unfreeze(record.extra),
                }
                appended: list[JSONValue] = [
                    *(
                        json_freeze(DictCodec.coerce(existing))
                        for existing in ListCodec.coerce(out[-1].get("toolCalls"))
                    ),
                    json_freeze(call),
                ]
                out[-1]["toolCalls"] = appended
            case UncategorizedRecord():
                out.append(dict(json_unfreeze(record.payload)))
            case _:
                # Every other IR record kind came from another provider; a
                # gemini document has no shape for it, so a conversion into
                # this format reports lossy rather than inventing one.
                continue
    return out


_GEMINI_NAMESPACE = UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
"""Namespace for deriving the ``sessionId`` a foreign stream declares none of.

The DNS namespace constant, used as an arbitrary fixed seed: what matters is
that the derivation is stable across processes, not which namespace it names.
"""
