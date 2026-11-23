"""One field of a provider's session log, read the way a missing one is.

    read_or_default       # (value, target, default=) -> value, or default when it is not a target
    kept_spelling         # (value) -> what a record keeps to write that ``timestamp`` back
    stamped               # (record, spelled) -> the record at the time ``spelled`` names
    MISSING_TIMESTAMP     # the time a session that states none is written with

A session log is another program's file. One field of the wrong type -- a failed
call's ``toolUseResult`` is a bare string, an MCP tool's a list -- must cost the
record that field, not the record: the reader keeps the rest of the line typed.

A record holds its time as a ``datetime``, which is written as ``isoformat``
spells it. A line that spells its time any other way, as claude, codex and
gemini each do (``2026-01-02T03:04:05.678Z``), keeps that spelling beside its
record, under ``$timestamp_as_written``, so the line is written back byte for
byte and the record is stored with the time as the line spelled it.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Final, overload

from treekle import ReadError, from_plain

from trackinizer.sagent.journal.types import (
    AgentMessage,
    AssistantMessage,
    ContextClear,
    ContextCompaction,
    ContextState,
    SessionContext,
    SystemMessage,
    Thinking,
    TokenUsage,
    ToolCall,
    ToolMessage,
    UncategorizedRecord,
    UserMessage,
    parsed_timestamp,
)


if TYPE_CHECKING:
    from treekle import PlainTree


__all__ = [
    "MISSING_TIMESTAMP",
    "Stamped",
    "kept_spelling",
    "read_or_default",
    "stamped",
]


MISSING_TIMESTAMP: Final = "1970-01-01T00:00:00.000Z"
"""The time written for a session that states none: claude and codex refuse a
line with no time."""

type Stamped = (
    UserMessage
    | AssistantMessage
    | Thinking
    | ToolCall
    | ToolMessage
    | SystemMessage
    | TokenUsage
    | ContextState
    | ContextCompaction
    | ContextClear
    | AgentMessage
    | SessionContext
    | UncategorizedRecord
)
"""A record that says when it happened."""


@overload
def read_or_default[T](
    value: object,
    target: type[T],
    *,
    default: None,
) -> T | None: ...


@overload
def read_or_default[T](value: object, target: type[T], *, default: T) -> T: ...


def read_or_default[T](
    value: object,
    target: type[T],
    *,
    default: T | None,
) -> T | None:
    """Return ``value`` as ``target``, or ``default`` when it is absent or mistyped.

    Unlike :func:`convert`, whose ``default`` covers only a missing or null
    value and which raises on any other type, this reads a mistyped value as
    absent. Use it only for a field whose wrong value the record can still
    write back byte for byte; any other field keeps ``convert``, so the line's
    guard stores the whole line as text.

    Args:
      value: Parsed JSON.
      target: The type to produce.
      default: Returned when ``value`` is ``None`` or is not a ``target``.

    Returns:
      result: ``value`` as a ``target``, or ``default``.

    """
    try:
        return from_plain(value, target, default=default)
    except ReadError:
        return default


def kept_spelling(value: object) -> dict[str, PlainTree]:
    """Return what a record keeps to write a line's ``timestamp`` back as it was.

    Args:
      value: The line's ``timestamp``, as parsed JSON.

    Returns:
      kept: The text, under ``$timestamp_as_written``, for a record's ``extra``.
        Nothing for a time spelled as ``isoformat`` spells it, and for a value
        that is no text, which each reader keeps its own way.

    """
    if not isinstance(value, str):
        return {}
    when = parsed_timestamp(value)
    if when is not None and when.isoformat() == value:
        return {}
    return {"$timestamp_as_written": value}


def stamped(record: Stamped, spelled: str) -> Stamped:
    """Return ``record`` at the time ``spelled`` names, kept as it is spelled.

    Args:
      record: A record, whose own time is replaced.
      spelled: A time as a line spells it, such as another record's
        ``stated_timestamp``.

    Returns:
      record: The record with that time, and the spelling beside it where
        ``isoformat`` would spell the time another way.

    """
    when = parsed_timestamp(spelled)
    if isinstance(record, UncategorizedRecord):
        payload = {
            key: value
            for key, value in record.payload.items()
            if key != "$timestamp_as_written"
        }
        return replace(
            record,
            timestamp=when,
            payload={**payload, **kept_spelling(spelled)},
        )
    extra = {
        key: value
        for key, value in record.extra.items()
        if key != "$timestamp_as_written"
    }
    return replace(record, timestamp=when, extra={**extra, **kept_spelling(spelled)})
