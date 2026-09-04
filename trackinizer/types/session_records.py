"""One stored IR record: the ``session_records`` row and its search text.

The typed contract for the ``session_records`` table -- the record-grained
store of a captured session, one row per act the model or its tools performed.
It is the source of truth for that table the way :mod:`types.inquiries` is for
``inquiries``: the SQL columns, the wire body, and the Store all derive from
:class:`SessionRecordRow` here.

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

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final, Self
from uuid import UUID

from trackinizer.lib.agent.types.sessions import (
    AgentStatusResult,
    AgentToAgentMessage,
    AssistantMessage,
    ContextClear,
    ContextCompaction,
    ContextState,
    FileEditResult,
    FileReadResult,
    FileWriteResult,
    IncompleteRecord,
    ShellCommandResult,
    SystemMessage,
    Thinking,
    TokenUsage,
    ToolCall,
    TurnContext,
    UncategorizedRecord,
    UncategorizedToolResult,
    UserMessage,
    WebFetchResult,
    WebSearchResults,
)
from trackinizer.lib.custom_json import JSON, DataclassCodec, json_freeze, json_unfreeze
from trackinizer.types.streams import Stderr, Stdin, Stdout, TraxRecord


__all__ = [
    "MAX_SEARCH_TEXT_BYTES",
    "SessionRecordRow",
    "search_text",
]


MAX_SEARCH_TEXT_BYTES: Final = 1_000_000
"""Byte cap on the search projection.

BYTES, not characters: the ``search`` column is ``GENERATED ... STORED``, so an
oversized value aborts the INSERT rather than degrading, and Postgres bounds a
tsvector by its encoded size. A character cap would let 4-byte codepoints past
it -- 250,000 emoji are a legal 250,000-character string and an illegal
1,000,000-byte one.
"""


def search_text(record: object) -> str:
    """The plaintext a record contributes to search, capped and never lossy.

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
    """The record class's name, which selects the type ``payload`` decodes as."""

    context_id: int | None = None
    """The ``idx`` of the :class:`TurnContext` that applied, within the same
    part. A claude context names ITSELF, so this may equal ``idx``; a codex
    stream has none until its first ``turn_context`` line."""

    timestamp: datetime | None = None
    """When the act happened, on the CLI's clock."""

    model: str | None = None
    """Denormalized from the applying ``TurnContext`` so the console feed need
    not self-join per row. A projection, never read back into a record."""

    payload: JSON
    """The record as ``DataclassCodec`` JSON, with ciphertext removed."""

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
            kind=type(record).__name__,
            context_id=getattr(record, "context_id", None),
            timestamp=_parsed(raw if isinstance(raw, str) else None),
            model=model,
            payload=json_freeze(DataclassCodec.to_json(stored)),
            text=search_text(record),
            ciphertext=encrypted if isinstance(encrypted, str) and encrypted else None,
        )

    def record(self) -> TraxRecord:
        """Rebuild the record this row stores.

        UNFROZEN before decoding, which is not a formality. ``json_freeze``
        maps ``list`` to ``tuple`` (``custom_json.py::json_freeze``), while
        ``DataclassCodec.to_json`` emits lists -- so decoding the frozen form
        hands ``from_json`` a shape it never produced. For a typed field that
        is harmless, but ``extra`` is untyped ``JSON``, and an untyped
        tuple-of-mappings is read as a tagged value rather than an array: a
        codex ``SystemMessage`` carrying ``$templates`` then fails to decode
        outright. Round-tripping through the shape ``to_json`` wrote keeps the
        two halves symmetric.

        Ciphertext is NOT spliced here: this type holds one row, and the bytes
        live in another table. A reader that fetched them calls
        ``dataclasses.replace(record, encrypted=...)`` itself.
        """
        return DataclassCodec.from_json(
            _class_for(self.kind), json_unfreeze(self.payload)
        )


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
        case ShellCommandResult():
            return (" ".join(record.command or ()), record.stdout, record.stderr)
        case FileReadResult() | FileWriteResult():
            return (record.path or "", record.content or "")
        case FileEditResult():
            return (
                record.path or "",
                *(splice.before or "" for splice in record.edits),
                *(splice.after or "" for splice in record.edits),
            )
        case WebSearchResults():
            return (
                record.query or "",
                *(row.title or "" for row in record.content),
                *(row.snippet or "" for row in record.content),
            )
        case WebFetchResult():
            return (record.url or "", record.content or "")
        case AgentStatusResult():
            return (record.prompt or "", record.content or "")
        case UncategorizedToolResult():
            return (record.content or "",)
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


def _truncate(text: str) -> str:
    """Cap ``text`` at the byte bound, cutting on a character boundary."""
    encoded = text.encode()
    if len(encoded) <= MAX_SEARCH_TEXT_BYTES:
        return text
    # ``errors="ignore"`` drops the partial codepoint the cut landed inside,
    # so the result is always re-encodable; a raw slice is not.
    return encoded[:MAX_SEARCH_TEXT_BYTES].decode(errors="ignore")


def _parsed(raw: str | None) -> datetime | None:
    """A record's ISO-8601 timestamp as a datetime, or ``None``.

    The IR keeps timestamps as the provider's own STRINGS so a session rewrites
    byte-exactly; the column is TIMESTAMPTZ so it can be ordered and filtered.
    An unparseable value stores NULL rather than failing the whole batch -- the
    string itself survives in ``payload``, which is what replays the file.
    """
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _class_for(kind: str) -> type[TraxRecord]:
    """The record class named by a row's ``kind``."""
    found = _BY_KIND.get(kind)
    if found is None:
        raise ValueError(f"unknown session record kind {kind!r}")
    return found


# Every concrete member of ``TraxRecord``, keyed by the name its rows store.
# Listed rather than walked from the union: ``session_records_test`` asserts the
# two agree, so a member added upstream fails that test instead of silently
# decoding as nothing here.
_BY_KIND: Final[dict[str, type[TraxRecord]]] = {
    cls.__name__: cls
    for cls in (
        UserMessage,
        AssistantMessage,
        Thinking,
        ToolCall,
        ShellCommandResult,
        FileReadResult,
        FileWriteResult,
        FileEditResult,
        WebSearchResults,
        WebFetchResult,
        AgentStatusResult,
        UncategorizedToolResult,
        SystemMessage,
        TokenUsage,
        ContextState,
        ContextCompaction,
        ContextClear,
        AgentToAgentMessage,
        TurnContext,
        UncategorizedRecord,
        IncompleteRecord,
        Stdin,
        Stdout,
        Stderr,
    )
}
