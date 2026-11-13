"""One stored IR record: the ``session_records`` row and its search text.

The typed contract for the ``session_records`` table -- the record-grained
store of a captured session, one row per act the model or its tools performed.
It is the source of truth for that table the way :mod:`types.inquiries` is for
``inquiries``: the SQL columns, the wire body, and the Store all derive from
:class:`SessionRecordRow` here.

A row's ``kind`` is its record's class name, except for the record of a tool
call. That record is always a ``ToolResult``, but each tool once had a record
class of its own, and SQL policy (offload, search, filter fields) is keyed on
those class names. So a tool-call row is stored under its tool's legacy class
name, such as ``BashResult`` (:func:`record_kind`). A legacy row still holds
that class's payload; a read converts it to the ``ToolResult`` shape and leaves
the row and its ``kind`` alone (:func:`served_payload`).

Identity is ``(session_id, part, idx)``. ``idx`` is DERIVED -- a record's
position in its file's normalized stream -- never counted by the writer, so
re-feeding a file that the CLI rewrote (a claude compaction does exactly that)
lands every record back on the key it already had. That is what makes ingest
idempotent, and it is why the runner needs no line-level dedup.

It is **not** an :class:`~trackinizer.types.inquiries.Inquiry`. The owning
session is the :class:`AgentSession` artifact row in ``inquiries``; records hang
off it by ``session_id`` and carry no edges, cost, or ``change_log`` audit -- a
captured act is not a knowledge mutation.

Two fields of the record do not ride in ``payload``:

  - :attr:`SessionRecordRow.text` is the search projection, computed once at
    ingest by :func:`search_text` and never re-derived.
  - ``Thinking.encrypted`` is stripped to ``""`` and returned separately as
    :attr:`SessionRecordRow.ciphertext`, so retention can drop the bytes
    without touching the searchable record.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType
from typing import Final, Self, cast, get_args
from uuid import UUID

from treekle import PlainTree, immutable

from trackinizer.sagent.session.types import (
    FORMER_RESULT_KINDS,
    RENAMED_KINDS,
    AgentSpawnResult,
    AgentToAgentMessage,
    AssistantMessage,
    BashResult,
    ContextCompaction,
    ContextState,
    EditResult,
    IncompleteRecord,
    ReadResult,
    Splice,
    SystemMessage,
    Thinking,
    ToolCall,
    ToolResult,
    UserMessage,
    WebFetchResult,
    WebSearchHit,
    WebSearchResult,
    WriteResult,
    former_kind,
    record_classes,
    stored_plain,
    stored_record,
)
from trackinizer.types.streams import Stderr, Stdin, Stdout, TraxRecord


__all__ = [
    "MAX_SEARCH_TEXT_BYTES",
    "SessionRecordRow",
    "payload_record",
    "record_class",
    "record_kind",
    "record_time",
    "search_text",
    "served_payload",
    "stored_payload",
]


MAX_SEARCH_TEXT_BYTES: Final = 1_000_000
"""Byte cap on the search projection.

BYTES, not characters: the ``search`` column is ``GENERATED ... STORED``, so an
oversized value aborts the INSERT rather than degrading, and Postgres bounds a
tsvector by its encoded size. A character cap would let 4-byte codepoints past
it -- 250,000 emoji are a legal 250,000-character string and an illegal
1,000,000-byte one.
"""


def record_kind(record: TraxRecord) -> str:
    """Return the ``kind`` under which the row of ``record`` is stored.

    Args:
      record: Any :data:`TraxRecord` member.

    Returns:
      kind: The record's class name. For a ``ToolResult``, the legacy class name
        sagent's ``former_kind`` returns, such as ``BashResult``.

    """
    if isinstance(record, ToolResult):
        return former_kind(record)
    return type(record).__name__


def record_class(kind: str) -> type[TraxRecord]:
    """Return the record class a row's ``kind`` names, as :func:`record_kind` wrote it.

    Args:
      kind: A row's ``kind``, of any version.

    Returns:
      record_class: The class its payload decodes as.

    Raises:
      ValueError: ``kind`` names no record class.

    """
    # A row stored before a record type was renamed carries its old name.
    found = _BY_KIND.get(RENAMED_KINDS.get(kind, kind))
    if found is None:
        raise ValueError(f"unknown session record kind {kind!r}")
    return found


def served_payload(
    kind: str,
    payload: Mapping[str, PlainTree],
) -> Mapping[str, PlainTree]:
    """Return a stored row's payload in the current record shape.

    A legacy tool-call row holds the payload of a retired record class. This
    converts it to the ``ToolResult`` payload :func:`stored_payload` writes
    today. The stored row and its ``kind`` do not change.

    Args:
      kind: The row's stored kind.
      payload: The row's stored payload.

    Returns:
      payload: The converted payload for a legacy tool-call row. The same
        ``payload`` for any other kind, for a row already in the current shape,
        and for a payload that does not decode, such as an offloaded body's stub.

    """
    current = ToolResult.__name__
    if RENAMED_KINDS.get(kind, kind) != current:
        return payload
    # The tag alone does not prove the current shape. A legacy placeholder row is
    # tagged ``ToolResult`` too but has no ``state``, and an earlier ``ToolResult``
    # stored a bare exit code as ``result``. Both must still be converted.
    if (
        payload.get("py/object") == current
        and "state" in payload
        and not isinstance(payload.get("result"), int)
    ):
        return payload
    try:
        return stored_payload(payload_record(payload, record_class(kind)))
    # Ingest stores a client's payload without decoding it, and an offloaded
    # body's row holds only a stub. Raising here would fail the whole read for
    # one such row; its ``text`` column is still shown.
    except (TypeError, ValueError):
        return payload


def search_text(record: object) -> str:
    """Return the plaintext a record contributes to search, capped and never lossy.

    Computed once at ingest and stored, never re-derived on read: phase 7
    backfills legacy rows with a ``text`` this rule would compute as ``""``,
    so a reindex would erase their searchability.

    ``Thinking.encrypted`` is excluded by construction -- it lives in
    ``session_ciphertext`` precisely so that dropping it leaves the record
    searchable, which a projection that indexed it would defeat.

    Args:
      record: Any :data:`TraxRecord` member.

    Returns:
      text: The record's searchable prose, truncated to
        :data:`MAX_SEARCH_TEXT_BYTES` on a character boundary.

    """
    return _truncate("\n".join(part for part in _parts(record) if part))


def stored_payload(record: TraxRecord) -> Mapping[str, PlainTree]:
    """Return ``record`` encoded as a row stores it, ciphertext and all.

    As :func:`~trackinizer.sagent.session.types.stored_plain` writes it, the scrape
    streams named too. :meth:`SessionRecordRow.of` strips the ciphertext before
    calling this; a rewrite of a stored row keeps whatever the row held.

    Args:
      record: Any :data:`TraxRecord` member.

    Returns:
      payload: The record as frozen plain data.

    """
    payload = immutable(stored_plain(record, classes=_CLASSES))
    assert isinstance(payload, MappingProxyType)
    return payload


def payload_record[R: TraxRecord](payload: object, target: type[R]) -> R:
    """Return the ``target`` record :func:`stored_payload` wrote as ``payload``."""
    return stored_record(payload, target, classes=_CLASSES)


def record_time(record: object) -> datetime | None:
    """Return when ``record`` says it happened, as its row's ``timestamp`` column holds.

    Args:
      record: Any :data:`TraxRecord` member.

    Returns:
      time: Its ISO-8601 ``timestamp`` read; ``None`` when it has none or no reader
        reads it.

    """
    raw = getattr(record, "timestamp", None)
    return _parsed(raw if isinstance(raw, str) else None)


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionRecordRow:
    """One ``session_records`` row.

    Built by :meth:`of` from a record the normalizer emitted; the columns are
    projections of it, so nothing here is a second source of truth. ``record``
    rebuilds the original, with ciphertext spliced back by the caller that
    holds it.
    """

    session_id: UUID
    """The :class:`AgentSession` (an ``inquiries`` row) this record belongs to."""

    part: int = 0
    """Which source file, assigned server-side by basename."""

    idx: int
    """Position in that part's normalized stream, from 0. Derived, never
    counted -- see the module docstring."""

    kind: str
    """The row's kind (:func:`record_kind`), which selects the type ``payload``
    decodes as."""

    context_id: int | None = None
    """The ``idx`` of the :class:`TurnContext` that applied, within the same
    part. A claude context names ITSELF, so this may equal ``idx``; a codex
    stream has none until its first ``turn_context`` line."""

    timestamp: datetime | None = None
    """When the act happened, on the CLI's clock."""

    model: str | None = None
    """Denormalized from the applying ``TurnContext`` so the console feed need
    not self-join per row. A projection, never read back into a record."""

    payload: Mapping[str, PlainTree]
    """The record as :func:`stored_payload` writes it, without ciphertext."""

    text: str = ""
    """The search projection; see :func:`search_text`."""

    ciphertext: str | None = None
    """``Thinking.encrypted`` verbatim, or ``None``. NOT a column on this
    table: the caller writes it to ``session_ciphertext`` under this row's own
    key, and its presence there is what signals a splice on read."""

    @classmethod
    def of(
        cls,
        *,
        session_id: UUID,
        part: int,
        idx: int,
        record: TraxRecord,
        model: str | None = None,
    ) -> Self:
        """Build a row from one normalized record.

        Args:
          session_id: The owning AgentSession's id.
          part: Which source file the record came from.
          idx: Its position in that file's normalized stream.
          record: The record itself.
          model: The applying ``TurnContext``'s model, when the caller tracked
            one.

        Returns:
          row: The storable row, with ciphertext split out.

        """
        encrypted = getattr(record, "encrypted", None)
        stored = (
            replace(record, encrypted="")
            if isinstance(record, Thinking) and encrypted
            else record
        )
        raw = getattr(record, "timestamp", None)
        return cls(
            session_id=session_id,
            part=part,
            idx=idx,
            kind=record_kind(record),
            context_id=getattr(record, "context_id", None),
            timestamp=_parsed(raw if isinstance(raw, str) else None),
            model=model,
            payload=stored_payload(stored),
            text=search_text(record),
            ciphertext=encrypted if isinstance(encrypted, str) and encrypted else None,
        )

    def record(self) -> TraxRecord:
        """Rebuild the record this row stores.

        Read by :func:`payload_record`, each ``JSON`` field frozen as the
        record holds it.

        Ciphertext is NOT spliced here: this type holds one row, and the bytes
        live in another table. A reader that fetched them calls
        ``dataclasses.replace(record, encrypted=...)`` itself.

        Returns:
          record: Decoded TraxRecord of the appropriate subtype.

        """
        return payload_record(self.payload, record_class(self.kind))


def _parts(record: object) -> tuple[str, ...]:
    """Every searchable string a record carries, in a stable order."""
    match record:
        case Thinking():
            # ``encrypted`` is deliberately absent; see the module docstring.
            return (record.content or "", record.summary or "")
        case (
            UserMessage()
            | AssistantMessage()
            | SystemMessage()
            | AgentToAgentMessage()
            | ContextState()
        ):
            return (record.content or "",)
        case ContextCompaction():
            return (record.summary or "",)
        case ToolCall():
            return (
                record.name,
                *(v for v in record.arguments.values() if isinstance(v, str)),
            )
        case ToolResult():
            return _result_parts(record)
        case IncompleteRecord() | Stdin() | Stdout() | Stderr():
            # A captured stream is prose and nothing else, so the line IS the
            # searchable text -- including a question typed IN, which is what
            # makes an answer findable by what prompted it.
            return (record.text,)
        case _:
            # TurnContext, TokenUsage, ContextClear, UncategorizedRecord: no
            # prose, so no filter field either -- a clause naming one could
            # never match.
            return ()


def _result_parts(record: ToolResult) -> tuple[str, ...]:
    """Return a tool-call record's searchable strings: result fields, then output."""
    tool_result = record.result
    stdout_text = "".join(item for item in record.stdout if isinstance(item, str))
    stderr_text = "".join(item for item in record.stderr if isinstance(item, str))
    match tool_result:
        case BashResult():
            return (" ".join(tool_result.command or ()), stdout_text, stderr_text)
        case ReadResult():
            return (tool_result.path or "", stdout_text, stderr_text)
        # Write, Edit and WebSearch leave ``stdout_text`` out: it repeats the
        # result fields and typed items indexed here. Adding it would index the
        # same words twice and differ from the text legacy rows were stored with.
        case WriteResult():
            return (tool_result.path or "", tool_result.content or "", stderr_text)
        case EditResult():
            edits = [item for item in record.stdout if isinstance(item, Splice)]
            return (
                tool_result.path or "",
                *(splice.before or "" for splice in edits),
                *(splice.after or "" for splice in edits),
                stderr_text,
            )
        case WebSearchResult():
            hits = [item for item in record.stdout if isinstance(item, WebSearchHit)]
            return (
                tool_result.query or "",
                *(hit.title or "" for hit in hits),
                *(hit.snippet or "" for hit in hits),
                stderr_text,
            )
        case WebFetchResult():
            return (tool_result.url or "", stdout_text, stderr_text)
        case AgentSpawnResult():
            return (tool_result.prompt or "", stdout_text, stderr_text)
        case _:
            # A record with no output, such as a pending call's, has only ``shown``.
            return (
                (stdout_text, stderr_text)
                if stdout_text or stderr_text
                else (record.shown or "",)
            )


def _truncate(text: str) -> str:
    """Cap ``text`` at the byte bound, cutting on a character boundary."""
    encoded = text.encode()
    if len(encoded) <= MAX_SEARCH_TEXT_BYTES:
        return text
    # ``errors="ignore"`` drops the partial codepoint the cut landed inside,
    # so the result is always re-encodable; a raw slice is not.
    return encoded[:MAX_SEARCH_TEXT_BYTES].decode(errors="ignore")


# The IR keeps timestamps as the provider's own STRINGS so a session rewrites byte-
# exactly; the column is TIMESTAMPTZ so it can be ordered and filtered. An unparseable
# value stores NULL rather than failing the whole batch -- the string itself survives in
# ``payload``, which is what replays the file.
def _parsed(raw: str | None) -> datetime | None:
    """Return a record's ISO-8601 timestamp as a datetime, or ``None``."""
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _members(alias: object) -> list[type]:
    """Return each concrete class a possibly nested type alias names."""
    found: list[type] = []
    stack: list[object] = [alias]
    while stack:
        entry = stack.pop()
        if isinstance(entry, type):
            found.append(entry)
        else:
            # A ``type`` alias names its union by ``__value__``; a bare union is one.
            stack.extend(get_args(getattr(entry, "__value__", entry)))
    return found


# Walked from the union, so a record type added upstream decodes here at once. A
# tool-call row is stored under a legacy class name (``record_kind``), so each of
# those names decodes as ``ToolResult``; ``wire/session_record_fields.py`` derives
# one filter field from each key here.
_BY_KIND: Final[dict[str, type[TraxRecord]]] = {
    **{cls.__name__: cast("type[TraxRecord]", cls) for cls in _members(TraxRecord)},
    **dict.fromkeys(
        (
            *sorted(FORMER_RESULT_KINDS & record_classes().keys()),
            "ToolErrorResult",
        ),
        cast("type[TraxRecord]", ToolResult),
    ),
}

# The session's classes and the scrape streams: given ``classes``, a tag naming any
# other is refused, so a stream record would store under no name at all.
_CLASSES: Final = MappingProxyType(
    {**record_classes(), **{cls.__name__: cls for cls in (Stdin, Stdout, Stderr)}},
)
