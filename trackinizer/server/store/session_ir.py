""":class:`_SessionIRMixin` -- storing captured sessions as IR records.

The storage seam for the session IR: append records, upsert the per-file
manifest, and read either back. Ciphertext is split from the record on write
and spliced back on read, so ``DELETE FROM session_ciphertext`` drops the
encrypted half of a session without touching what search indexes.

Records are keyed ``(session_id, part, idx)`` where ``idx`` is DERIVED from
the record's position in its file's normalized stream. That is what makes
re-ingest idempotent: a claude compaction rewrites the session file, so the
runner re-feeds lines it already sent, and a derived key lands each record
back where it already was instead of appending a second copy.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import json

import asyncpg

from trackinizer.lib.custom_json import (
    JSON,
    DictCodec,
    IntCodec,
    json_freeze,
    json_unfreeze,
)
from trackinizer.lib.postgres import Conn
from trackinizer.server.notify import notify_after_commit, tx
from trackinizer.server.store.cascade import _CascadeAuditMixin
from trackinizer.server.values import vetted_sql
from trackinizer.types.errors import ConflictError, NotFoundError
from trackinizer.types.session_records import SessionRecordRow


__all__ = ["SessionManifest", "SlashCommandRow", "_SessionIRMixin"]


@dataclass(frozen=True, slots=True, kw_only=True)
class SlashCommandRow:
    """One ``session_slash_commands`` row: a command the human typed.

    Not a record: it is PTY-observed and absent from the session log, so it
    cannot be re-derived and holds no ``idx``. ``seq`` is server-assigned,
    which is why it is absent here -- the writer never names one.
    """

    timestamp: datetime
    """The submit-time clock the keystroke detector stamped."""

    command: str
    """The verb without its leading slash."""

    args: str = ""
    """Everything after the verb."""


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionManifest:
    """One ``session_manifests`` row: what a part's file was and declared."""

    part: int
    """Which file, assigned server-side by basename."""

    name: str
    """The file's basename. Not its path -- that differs across machines, and
    the same session resumed elsewhere must resolve to the same part."""

    metadata: JSON
    """How the file spells its bytes, as its reader stated it for this part.

    The ``TurnContext.encoding`` in force: ``newline_terminated`` plus, for
    claude, the ascii-escaping majority and its exception bitmap. A rewrite
    needs it verbatim, and nothing else in the IR carries it -- every record
    still matches without it while the bytes differ.
    """

    ir_id: UUID
    """The id the capturing client minted for this file."""

    format: str
    """The ``convert`` adapter that reads this file, or ``""`` for a capture
    with no native format -- which is what makes a part searchable but never
    resumable."""

    records: int
    """Live prefix bound: readers take ``idx < records``, so a file that shrank
    leaves its tail rows inert rather than deleted."""


class _SessionIRMixin(_CascadeAuditMixin):
    """Append and read session IR records, manifests, and ciphertext.

    Built on the cascade mixin for ``_buffer_notification``: a stored record
    wakes the same ``/api/web/subscribe`` fanout every other session mutation
    does, so a live viewer follows a capture as it lands.
    """

    async def append_session_records(
        self,
        session_id: UUID,
        rows: Sequence[SessionRecordRow],
        *,
        restart: bool = False,
        slash_commands: Sequence[SlashCommandRow] = (),
    ) -> tuple[int, int, int]:
        """Store records idempotently; return ``(written, skipped, slash)``.

        ``restart`` says this batch re-derived its part from offset 0, which
        selects the conflict policy. A normal append is ``DO NOTHING`` -- the
        runner re-feeds lines routinely and the stored row is already correct.
        A restart is ``DO UPDATE``, because a compaction is a REPLACEMENT: it
        keeps the turns it did not summarize away, so a record re-derived at a
        given ``idx`` may legitimately differ, and disk is truth.

        Ciphertext rides in ``rows`` but lands in ``session_ciphertext``, under
        each record's own key. Both writes share one transaction, so a record
        can never be readable while its ciphertext is missing.

        ``slash_commands`` share that transaction for the same reason: a
        command is typed BETWEEN the turns around it, so a crash that stored
        one half would leave a transcript that never happened. They are not
        records -- they carry no ``idx`` and the server assigns their ``seq``,
        because a sink counter restarts at 0 on a resumed run and would
        collide.

        Args:
          session_id: The owning AgentSession.
          rows: Records to store, each carrying its derived ``idx``.
          restart: Whether this batch re-derived the part from its start.
          slash_commands: Commands typed since the last batch.

        Returns:
          written: Rows this call inserted or updated.
          skipped: Rows an existing key already covered.
          slash: Slash commands stored.

        Raises:
          NotFoundError: The session does not exist (or was purged mid-write).
          ConflictError: The id is not an ``AgentSession``, or the session has
            already ended.

        """
        if not rows and not slash_commands:
            return (0, 0, 0)
        conflict = (
            "DO UPDATE SET kind = EXCLUDED.kind, context_id = EXCLUDED.context_id, "
            "timestamp = EXCLUDED.timestamp, model = EXCLUDED.model, "
            "payload = EXCLUDED.payload, text = EXCLUDED.text"
            if restart
            else "DO NOTHING"
        )
        async with (
            notify_after_commit(),
            self.engine.acquire() as conn,
            tx(conn),
        ):
            # Kind and liveness under the row lock, so a concurrent ``end``
            # cannot slip between the check and the insert. Capture attaches
            # only to a LIVE AgentSession: ``resolve_live_sessions`` reads
            # ``ended`` to decide a session is gone, so a record accepted
            # afterwards lands in a transcript nothing watches while the two
            # halves disagree about whether the session is running.
            #
            # Safe against the sink's own teardown: ``TrackinizerSink.close``
            # flushes BEFORE ``session_end`` (its ``try`` / ``finally``), so
            # the last batch is always written while the session is still
            # live.
            session = await conn.fetchrow(
                "SELECT kind, agentsession_ended FROM inquiries "
                "WHERE id = $1 FOR UPDATE",
                session_id,
            )
            if session is None:
                raise NotFoundError(f"session {session_id} not found")
            if session["kind"] != "AgentSession":
                raise ConflictError(
                    f"inquiry {session_id} is not an AgentSession "
                    f"(kind={session['kind']!r}); records may only attach to a session"
                )
            if session["agentsession_ended"] is not None:
                raise ConflictError(
                    f"session {session_id} has ended; cannot append records"
                )
            try:
                written = (
                    await conn.fetch(
                        vetted_sql(
                            "INSERT INTO session_records (session_id, part, idx, "
                            "kind, context_id, timestamp, model, payload, text) "
                            "SELECT * FROM unnest($1::uuid[], $2::int[], $3::int[], "
                            "$4::text[], $5::int[], $6::timestamptz[], $7::text[], "
                            "$8::json[], $9::text[]) "
                            "ON CONFLICT (session_id, part, idx) ",
                            conflict,
                            " RETURNING idx",
                        ),
                        [row.session_id for row in rows],
                        [row.part for row in rows],
                        [row.idx for row in rows],
                        [row.kind for row in rows],
                        [row.context_id for row in rows],
                        [row.timestamp for row in rows],
                        [row.model for row in rows],
                        [_encoded_payload(row.payload) for row in rows],
                        [row.text for row in rows],
                    )
                    if rows
                    else []
                )
            except asyncpg.ForeignKeyViolationError as exc:
                # Purged in the window between the caller's check and this
                # insert: gone is a 404, not a constraint name leaked as a 409.
                raise NotFoundError(f"session {session_id} not found") from exc
            encrypted = [row for row in rows if row.ciphertext is not None]
            if encrypted:
                await conn.execute(
                    "INSERT INTO session_ciphertext (session_id, part, idx, bytes) "
                    "SELECT * FROM unnest($1::uuid[], $2::int[], $3::int[], "
                    "$4::bytea[]) "
                    "ON CONFLICT (session_id, part, idx) "
                    "DO UPDATE SET bytes = EXCLUDED.bytes",
                    [row.session_id for row in encrypted],
                    [row.part for row in encrypted],
                    [row.idx for row in encrypted],
                    [_encoded(row.ciphertext) for row in encrypted],
                )
            stored = await self._append_slash_commands(conn, session_id, slash_commands)
            if written or stored:
                self._buffer_notification(session_id)
        return (len(written), len(rows) - len(written), stored)

    @staticmethod
    async def _append_slash_commands(
        conn: Conn, session_id: UUID, commands: Sequence[SlashCommandRow]
    ) -> int:
        """Store typed commands, numbering them from the session's own max.

        Server-assigned rather than client-counted: a resumed run's sink
        restarts at 0, so a client-supplied ``seq`` would collide with the
        earlier run's and the PK would silently drop every command.

        Runs on the caller's connection so it shares the record transaction --
        a command sits BETWEEN the turns around it, and storing one half of
        that is a transcript that never happened.
        """
        if not commands:
            return 0
        # ``generate_series`` off one ``max(seq)`` read INSIDE the transaction:
        # numbering in Python would need a second round trip and could not see
        # a concurrent append's rows.
        stored = await conn.fetch(
            "INSERT INTO session_slash_commands "
            "(session_id, seq, timestamp, command, args) "
            "SELECT $1, "
            "  (SELECT coalesce(max(seq) + 1, 0) FROM session_slash_commands "
            "   WHERE session_id = $1) + ordinality - 1, "
            "  t.timestamp, t.command, t.args "
            "FROM unnest($2::timestamptz[], $3::text[], $4::text[]) "
            "  WITH ORDINALITY AS t(timestamp, command, args, ordinality) "
            "ON CONFLICT (session_id, seq) DO NOTHING "
            "RETURNING seq",
            session_id,
            [row.timestamp for row in commands],
            [row.command for row in commands],
            [row.args for row in commands],
        )
        return len(stored)

    async def read_session_slash_commands(
        self, session_id: UUID
    ) -> list[SlashCommandRow]:
        """Every command typed into one session, in ``seq`` order."""
        async with self.engine.acquire() as conn:
            rows = await conn.fetch(
                "SELECT timestamp, command, args FROM session_slash_commands "
                "WHERE session_id = $1 ORDER BY seq",
                session_id,
            )
        return [
            SlashCommandRow(
                timestamp=row["timestamp"], command=row["command"], args=row["args"]
            )
            for row in rows
        ]

    async def read_session_records(
        self,
        session_id: UUID,
        *,
        part: int,
        after_idx: int = -1,
        limit: int = 500,
        plaintext_only: bool = False,
    ) -> list[SessionRecordRow]:
        """Read one part's records in ``idx`` order, ciphertext spliced back.

        ``plaintext_only`` skips the ciphertext join, which is what a search
        or feed reader wants: those never replay a file, and the bytes are the
        largest thing on the row.

        Bounded by the manifest's ``records``, the part's LIVE PREFIX. A
        compaction rewrites a file shorter and the re-derived batch overwrites
        positions 0..n, but nothing deletes the rows beyond n -- ``restart``
        is an overwrite, not a truncate. Reading past the bound therefore
        returns the new prefix followed by the previous file's suffix: a
        transcript that never existed, which ``resume`` would hand back to the
        CLI as conversation.

        A part with NO manifest reads as empty rather than unbounded. The
        manifest is upserted before its records, so rows without one are a
        torn write; serving them would expose the half-written state that
        ordering exists to prevent.

        Args:
          session_id: The owning AgentSession.
          part: Which file to read; parts are stored separately and never
            fused at write time.
          after_idx: Exclusive lower bound, for paging.
          limit: Maximum rows to return.
          plaintext_only: Skip the ciphertext splice.

        Returns:
          rows: The part's live records, ordered by ``idx``.

        """
        bytes_column = (
            "NULL::bytea AS bytes"
            if plaintext_only
            else "(SELECT c.bytes FROM session_ciphertext c "
            "WHERE c.session_id = r.session_id AND c.part = r.part "
            "AND c.idx = r.idx) AS bytes"
        )
        async with self.engine.acquire() as conn:
            rows = await conn.fetch(
                vetted_sql(
                    "SELECT r.session_id, r.part, r.idx, r.kind, r.context_id, "
                    "r.timestamp, r.model, r.payload, r.text, ",
                    bytes_column,
                    # JOIN, not a subquery with a fallback: a part whose
                    # manifest is missing has no bound, and the join dropping
                    # it is the "reads as empty" rule rather than a branch.
                    " FROM session_records r "
                    "JOIN session_manifests m "
                    "  ON m.session_id = r.session_id AND m.part = r.part "
                    "WHERE r.session_id = $1 AND r.part = $2 "
                    "AND r.idx > $3 AND r.idx < m.records "
                    "ORDER BY r.idx LIMIT $4",
                ),
                session_id,
                part,
                after_idx,
                limit,
            )
        return [
            SessionRecordRow(
                session_id=row["session_id"],
                part=row["part"],
                idx=row["idx"],
                kind=row["kind"],
                context_id=row["context_id"],
                timestamp=row["timestamp"],
                model=row["model"],
                payload=_decoded_payload(row["payload"]),
                text=row["text"],
                ciphertext=(
                    None if row["bytes"] is None else bytes(row["bytes"]).decode()
                ),
            )
            for row in rows
        ]

    async def upsert_session_manifest(
        self,
        session_id: UUID,
        *,
        name: str,
        metadata: JSON,
        ir_id: UUID,
        format: str,
        records: int,
    ) -> int:
        """Record what one part's file is; return its server-assigned ``part``.

        The client names the FILE and the server resolves the part, so a
        restarted or resumed client cannot invent a conflicting number. Racing
        appends on an unseen file both compute ``max(part)+1``; the unique
        constraint on ``(session_id, name)`` rejects the loser, which then
        re-reads the winner's.

        Re-upserted per batch rather than written once: ``metadata`` is only
        correct for the prefix consumed -- claude's ascii-escaping majority is
        not decided until the stream ends -- and ``records`` grows with the
        file.

        Args:
          session_id: The owning AgentSession.
          name: The file's basename.
          metadata: The encoding in force for the prefix read so far.
          ir_id: The id the capturing client minted for this file.
          format: The ``convert`` adapter that reads it, or ``""``.
          records: How many records the part currently holds.

        Returns:
          part: The part number this file resolved to.

        """
        # Serialized here, not handed over as a mapping: the column is
        # ``json`` to keep the provider's key order (see the schema), and the
        # driver's own codec would round-trip it through a dict.
        encoded = _encoded_payload(metadata)
        async with self.engine.acquire() as conn, tx(conn):
            found = await conn.fetchval(
                "SELECT part FROM session_manifests "
                "WHERE session_id = $1 AND name = $2",
                session_id,
                name,
            )
            part = IntCodec.coerce(
                found
                if found is not None
                else await conn.fetchval(
                    "SELECT coalesce(max(part) + 1, 0) FROM session_manifests "
                    "WHERE session_id = $1",
                    session_id,
                ),
                0,
            )
            try:
                await conn.execute(
                    "INSERT INTO session_manifests (session_id, part, name, "
                    "metadata, ir_id, format, records) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7) "
                    "ON CONFLICT (session_id, part) DO UPDATE SET "
                    "metadata = EXCLUDED.metadata, ir_id = EXCLUDED.ir_id, "
                    "format = EXCLUDED.format, records = EXCLUDED.records",
                    session_id,
                    part,
                    name,
                    encoded,
                    ir_id,
                    format,
                    records,
                )
            except asyncpg.UniqueViolationError:
                # A concurrent append claimed this part for a different file.
                # Its manifest is now the truth for that number, so re-read
                # rather than overwrite: ours belongs at a later part.
                retry = await conn.fetchval(
                    "SELECT part FROM session_manifests "
                    "WHERE session_id = $1 AND name = $2",
                    session_id,
                    name,
                )
                if retry is None:
                    raise
                part = IntCodec.coerce(retry, 0)
            except asyncpg.ForeignKeyViolationError as exc:
                raise NotFoundError(f"session {session_id} not found") from exc
        return part

    async def read_session_manifests(self, session_id: UUID) -> list[SessionManifest]:
        """Every part of one session, in ``part`` order."""
        async with self.engine.acquire() as conn:
            rows = await conn.fetch(
                "SELECT part, name, metadata, ir_id, format, records "
                "FROM session_manifests WHERE session_id = $1 ORDER BY part",
                session_id,
            )
        return [
            SessionManifest(
                part=row["part"],
                name=row["name"],
                metadata=_decoded_payload(row["metadata"]),
                ir_id=row["ir_id"],
                format=row["format"],
                records=row["records"],
            )
            for row in rows
        ]


def _encoded_payload(payload: JSON) -> str:
    """One record's payload as JSON text, key order intact.

    Serialized HERE rather than handed to asyncpg as a mapping: the column is
    ``json`` (not ``jsonb``) precisely to keep the provider's key order, and
    the driver's own codec would round-trip it through a dict whose ordering
    is no longer the file's. ``separators`` drops the whitespace ``json.dumps``
    adds by default -- the text is stored verbatim, so the padding would be
    too.
    """
    return json.dumps(json_unfreeze(payload), separators=(",", ":"))


def _decoded_payload(raw: str) -> JSON:
    """The stored payload text back as frozen JSON, key order intact."""
    return json_freeze(DictCodec.coerce(json.loads(raw)))


def _encoded(ciphertext: str | None) -> bytes:
    """The base64 ASCII as bytes, stored verbatim rather than decoded.

    Claude writes standard base64 and codex base64url, so one decode/encode
    pair cannot round-trip both -- the text is kept exactly as the provider
    wrote it and re-emitted unchanged.
    """
    assert ciphertext is not None
    return ciphertext.encode()
