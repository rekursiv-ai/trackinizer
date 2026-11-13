"""Read a sagent ``session.jsonl`` into the session IR, and write one for a resume.

Today's sagent journals the IR itself: each line is one record, tagged with its
class, read back as sagent wrote it and written the same way, so a stored session
resumes as a journal of its own.

An old sagent's log is told apart, so it is never read as another CLI's, but its
records are kept uncategorized: ``legacy_journal`` alone reads them, and
``convert_session upgrade`` rewrites such a log as today's journal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import json

from treekle import PlainTree, immutable, json_loads

from trackinizer.sagent.session.jsonl import read_record, record_line
from trackinizer.sagent.session.types import (
    RENAMED_KINDS,
    IncompleteRecord,
    SessionRecord,
    UncategorizedRecord,
    record_classes,
)


if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from typing import TextIO


__all__ = ["denormalize", "is_sagent", "normalize"]


def normalize(stream: TextIO) -> Iterator[SessionRecord]:
    """Normalize a sagent session stream into IR records.

    Yields as it reads (axiom 11), so a live session reads as far as it has
    been written.

    Args:
      stream: A sagent ``session.jsonl`` text stream.

    Yields:
      record: Each record the stream states, in order. A line that is not
        JSON becomes an ``IncompleteRecord``; an old sagent's record, an
        ``UncategorizedRecord`` of its kind.

    """
    for line in stream:
        if line.strip():
            yield _read(line)


def denormalize(records: Iterable[SessionRecord], stream: TextIO) -> None:
    """Write ``records`` as a journal today's sagent resumes.

    A line read torn stays the bytes it was, so the journal reads back the same.

    Args:
      records: The records, in order.
      stream: Where the journal's text goes.

    """
    for record in records:
        if isinstance(record, IncompleteRecord):
            _ = stream.write(record.text)
        else:
            _ = stream.write(record_line(record).decode())


def is_sagent(head: str) -> bool:
    """Return whether the first lines of a file were written by sagent.

    Args:
      head: The file's opening lines.

    Returns:
      found: Whether the first non-blank line is a record only sagent writes:
        today's tagged record, or a kind only an old sagent wrote.

    """
    only_sagent = {
        "meta",
        "history",
        "tool_state",
        "context_override",
        "context_splice",
        "persistent_agent",
        "runtime_event",
        "event",
        "update",
        "clear",
    }
    for line in head.splitlines():
        if not line.strip():
            continue
        try:
            record = json_loads(line)
        except json.JSONDecodeError:
            return False
        if not isinstance(record, dict):
            return False
        # A record type sagent names now, or named once: its journals keep those.
        if record.get("py/object") in {*record_classes(), *RENAMED_KINDS}:
            return True
        kind = record.get("kind")
        return isinstance(kind, str) and (
            kind in only_sagent
            or (kind == "message" and ("descriptor" in record or "role" in record))
        )
    return False


def _read(line: str) -> SessionRecord:
    """Return the record one line states."""
    try:
        record = json_loads(line, dict[str, PlainTree])
    except (json.JSONDecodeError, ValueError, TypeError):
        return IncompleteRecord(text=line)
    # Today's journal tags every record with its type, as ``py/object``, and only
    # sagent's own decoder reads it: a type stored under a former name is read as
    # its name now. An untagged record is an old sagent's.
    if "py/object" in record:
        return read_record(line.encode())
    kind = record.get("kind")
    return UncategorizedRecord(
        kind=kind if isinstance(kind, str) and kind else "unknown",
        payload=immutable(record),
    )
