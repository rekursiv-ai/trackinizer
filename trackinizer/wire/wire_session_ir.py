"""Wire bodies for the session IR: records, manifests, and their responses.

The HTTP shape of :mod:`types.session_records`. Phase 4's append route reuses
these same bodies, so the read side and the write side cannot describe a record
differently.

Two fields the client never sends. ``part`` is resolved server-side from the
file's basename, so a restarted or resumed client cannot invent a conflicting
number; and ``text`` is computed at ingest from the record itself, so a client
cannot decide what its own transcript matches.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from trackinizer.lib.custom_json import JSON, json_freeze
from trackinizer.types.session_records import SessionRecordRow


__all__ = [
    "MAX_RECORD_BATCH",
    "AppendRecordsRequest",
    "AppendRecordsResponse",
    "ManifestBody",
    "PartBody",
    "ReadPartsResponse",
    "ReadRecordsResponse",
    "RecordBody",
    "SlashCommandBody",
    "session_parts_path",
    "session_records_path",
]


MAX_RECORD_BATCH: Final = 1_000
"""Records one append may carry.

A tailer batches whatever a wake delivered, and a claude compaction re-feeds a
whole file at once -- so the natural batch is unbounded unless the wire caps
it. Bounds the JSON one request decodes and the array one INSERT unnests.
"""


class RecordBody(BaseModel):
    """One stored IR record, as the wire carries it.

    ``idx`` is DERIVED from the record's position in its file's normalized
    stream, never counted by the sender: that is what makes a re-fed file
    idempotent rather than duplicated.
    """

    idx: int = Field(ge=0)
    """Position in the part's normalized stream, from 0."""

    kind: str = Field(min_length=1)
    """The record class's name; selects the type ``payload`` decodes as."""

    context_id: int | None = Field(default=None, ge=0)
    """The ``idx`` of the applying ``TurnContext``, within the same part. May
    EQUAL ``idx``: a claude context is appended at its own index and names
    itself."""

    timestamp: datetime | None = None
    model: str | None = None

    payload: JSON = Field(default_factory=dict)
    """The record as ``DataclassCodec`` JSON, with ciphertext removed."""

    text: str = ""
    """The search projection, computed at ingest."""

    ciphertext: str | None = None
    """``Thinking.encrypted`` verbatim as base64 ASCII, or ``None``.

    Carried beside the payload rather than inside it: the server stores it in
    ``session_ciphertext`` under this record's own key, so retention can drop
    every session's ciphertext without rewriting a single record.
    """

    @classmethod
    def of(cls, row: SessionRecordRow) -> RecordBody:
        """Build a wire body from a stored row."""
        return cls(
            idx=row.idx,
            kind=row.kind,
            context_id=row.context_id,
            timestamp=row.timestamp,
            model=row.model,
            payload=row.payload,
            text=row.text,
            ciphertext=row.ciphertext,
        )

    def row(self, session_id: UUID, part: int) -> SessionRecordRow:
        """Rebuild the storable row for ``session_id`` and ``part``."""
        return SessionRecordRow(
            session_id=session_id,
            part=part,
            idx=self.idx,
            kind=self.kind,
            context_id=self.context_id,
            timestamp=self.timestamp,
            model=self.model,
            payload=json_freeze(dict(self.payload)),
            text=self.text,
            ciphertext=self.ciphertext,
        )


class ManifestBody(BaseModel):
    """What one part's source file is, re-sent with every append batch.

    Re-sent rather than written once because it CHANGES as the file grows:
    ``records`` counts what has arrived, and the encoding a reader reports is
    only correct for the prefix it has consumed -- claude's ascii-escaping
    convention is a majority that is not decided until the stream ends.
    """

    name: str = Field(min_length=1)
    """The file's basename. Not a path: the same session resumed on another
    machine must resolve to the same part."""

    metadata: JSON = Field(default_factory=dict)
    """The ``TurnContext.encoding`` in force for the prefix read so far."""

    ir_id: UUID
    """The id the capturing client minted for this file."""

    format: str = ""
    """The ``convert`` adapter that reads this file. Empty means no native
    format, which is what makes a part searchable but never resumable."""

    records: int = Field(default=0, ge=0)
    """How many records the part currently holds."""


class PartBody(BaseModel):
    """One part of a session, as ``GET .../parts`` lists it."""

    part: int = Field(ge=0)
    name: str
    format: str
    records: int = Field(ge=0)

    metadata: JSON = Field(default_factory=dict)
    """What the source file declared, which a REWRITE needs verbatim.

    Not decoration: claude's ascii-escaping convention rides on the
    ``TurnContext`` in force, and rewriting without it escapes different
    characters -- so the file differs from the captured one while every record
    matches. A resume hands the file back to the provider, which is entitled
    to reject a transcript it did not write.
    """

    ir_id: UUID | None = None
    """The id the capturing client minted for the file.

    Carried so a caller can tell a rewrite what the ORIGINAL was, distinct
    from the fresh id a resume mints to name its own copy.
    """


class SlashCommandBody(BaseModel):
    """One slash command the human typed, as the wire carries it.

    It is NOT a record: a slash command is handled inside the CLI's TUI and
    never written to the session log, so it cannot be re-derived and cannot
    hold an ``idx``. It carries no ``seq`` either -- the server assigns one,
    because a sink counter restarts at 0 on a resumed run and would collide.
    """

    timestamp: datetime
    """The submit-time clock the keystroke detector stamped. Required: a typed
    command has no CLI-recorded time, so this is the only one there is."""

    command: str = Field(min_length=1)
    """The verb without its leading slash (``exit``, ``model``)."""

    args: str = ""
    """Everything after the verb; empty for a bare command."""


class AppendRecordsRequest(BaseModel):
    """One file's records, plus what that file currently is.

    One part per request. ``part`` is absent by design: the client names the
    FILE and the server resolves the number, so a restarted or resumed client
    cannot invent a conflicting one.
    """

    name: str = ""
    """The source file's basename; the server resolves ``part`` from it.

    Empty only on a slash-command-only append: a typed command belongs to the
    SESSION, not to any file, so a run that types one before its CLI has
    written a transcript names no part.
    """

    manifest: ManifestBody | None = None
    """Re-sent every batch, because it changes as the file grows. ``None``
    exactly when ``name`` is empty."""

    @model_validator(mode="after")
    def _records_name_a_file(self) -> AppendRecordsRequest:
        """Records need a part; a part needs a named file and its manifest."""
        if self.records and not (self.name and self.manifest):
            raise ValueError("records require 'name' and 'manifest'")
        if bool(self.name) != (self.manifest is not None):
            raise ValueError("'name' and 'manifest' are given together or not at all")
        return self

    restart: bool = False
    """Whether this batch re-derived the part from offset 0.

    Batch-level rather than per-record: it describes how the records were
    PRODUCED, and it selects the conflict policy. A restart overwrites,
    because a compaction is a replacement -- claude keeps the turns it did not
    summarize away, so a record re-derived at a position may legitimately
    differ from the stored one, and disk is truth.
    """

    records: list[RecordBody] = Field(default_factory=list, max_length=MAX_RECORD_BATCH)

    slash_commands: list[SlashCommandBody] = Field(
        default_factory=list, max_length=MAX_RECORD_BATCH
    )
    """Commands typed since the last batch, committed with these records.

    They ride the record append rather than a route of their own so a run that
    is interrupted mid-flush cannot leave a command stored without the turns
    around it, or the reverse.
    """


class AppendRecordsResponse(BaseModel):
    """What one append did: which part, and how much was new."""

    part: int | None = Field(default=None, ge=0)
    """The part the named file resolved to, or ``None`` when the request named
    no file (a slash-command-only append)."""

    written: int = Field(ge=0)
    skipped: int = Field(ge=0)
    slash_commands: int = Field(default=0, ge=0)
    """How many slash commands this request stored."""


class ReadPartsResponse(BaseModel):
    """Every part of one session, as ``GET .../parts`` answers."""

    parts: list[PartBody] = Field(default_factory=list)


class ReadRecordsResponse(BaseModel):
    """One page of a part's records, in ``idx`` order."""

    part: int = Field(ge=0)
    records: list[RecordBody] = Field(default_factory=list)


def session_records_path(session_id: UUID) -> str:
    """The append/read path for one session's IR records."""
    return f"/api/sessions/{session_id}/records"


def session_parts_path(session_id: UUID) -> str:
    """The path listing one session's parts."""
    return f"/api/sessions/{session_id}/parts"
