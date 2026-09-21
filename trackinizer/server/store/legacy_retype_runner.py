"""Rewrite ``part = -1`` legacy streams through :func:`legacy_retype.retype`.

The impure half of the retype (Issue#20799): reads one session's
020-backfilled records, maps the ``legacy/*`` ones through the pure
:func:`~trackinizer.server.store.legacy_retype.retype`, and REPLACES the
part's rows in one transaction.

Replacement rather than update-in-place: a ``legacy/AssistantMessage`` fans
out to several records and a ``legacy/SlashCommand`` maps out of the stream
entirely, so the row count changes and every ``idx`` after the first fan-out
shifts. ``part = -1`` is never resumable (``format = ''``, per 020), so
renumbering breaks no CLI contract; the manifest's ``records`` bound is
rewritten in the same transaction, which is what keeps a reader from seeing
a torn stream (``read_session_records`` bounds every read by the manifest).

Rows that are NOT retypable -- ``legacy/UnknownMessage`` and the odd foreign
row -- are copied byte-for-byte: stored payload text, stored ``text``, stored
ciphertext, renumbered only. They are never decoded and re-encoded, because
a codec round-trip is not byte-stable for untyped payloads and 020's
hand-written ``text`` must survive ("a reindex would erase legacy
searchability", schema.sql).

No ``notify_after_commit``: the console feed pages by ``created``, which this
rewrite preserves, so retyped history must not re-surface as fresh activity.

Idempotent by content: a part with no ``legacy/*`` payload kinds is a no-op,
so a cancelled run resumes by re-running.

Sharding: ``retype_all`` splits sessions by ``hashtext(session_id::text)`` --
the split ``session_ir_storage_cost.md`` measured at 171 s where the serial
form burned 19 minutes. Shards are disjoint; each session rewrites under its
own transaction.

Maintenance run-hook (``docs/db_schema_migration.md``: a backfill runs
against the live database BEFORE any restart, old server still serving)::

    uv --quiet run --frozen python -m \
        trackinizer.server.store.legacy_retype_runner \
        "$TRACKINIZER_DSN" 16

Arguments: the DSN and the shard count. Shards run as concurrent tasks on
independent connections; per-session transactions keep a cancelled run
resumable by re-running.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final
from uuid import UUID

import json

from trackinizer.lib.agent.types.sessions import UncategorizedRecord
from trackinizer.lib.custom_json import (
    DataclassCodec,
    DictCodec,
    StrCodec,
    json_unfreeze,
    loads,
)
from trackinizer.server.notify import tx
from trackinizer.server.store.legacy_retype import (
    LEGACY_KINDS,
    SlashCommandOut,
    retype,
)
from trackinizer.types.session_records import SessionRecordRow


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn, DatabaseEngine


__all__ = [
    "RetypeStats",
    "retype_all",
    "retype_session",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class RetypeStats:
    """What one retype pass did.

    Attributes:
      sessions: Sessions examined.
      rewritten: Sessions whose part ``-1`` was rewritten.
      records_in: Legacy records read across rewritten sessions.
      records_out: Records written in their place.
      slash_commands: Commands routed to ``session_slash_commands``.

    """

    sessions: int = 0
    rewritten: int = 0
    records_in: int = 0
    records_out: int = 0
    slash_commands: int = 0


async def retype_all(
    engine: DatabaseEngine,
    *,
    shards: int = 1,
    shard: int = 0,
) -> RetypeStats:
    """Retype every session's legacy part in one shard.

    Args:
      engine: The store's engine; one connection per call.
      shards: Total shard count across concurrent callers.
      shard: This caller's shard, in ``[0, shards)``.

    Returns:
      stats: Aggregated over every session in the shard.

    """
    if shard < 0 or shard >= shards:
        raise ValueError(f"shard {shard} outside [0, {shards})")
    sessions = rewritten = records_in = records_out = slash = 0
    async with engine.acquire() as conn:
        rows = await conn.fetch(
            # ``& 2147483647`` (mask the sign bit), NOT ``abs()``: hashtext can
            # return INT_MIN, whose abs() overflows int4 ("integer out of range").
            # Masking is total and preserves the uniform shard spread.
            "SELECT DISTINCT session_id FROM session_records "
            "WHERE part = $1 "
            "AND (hashtext(session_id::text) & 2147483647) % $2 = $3 "
            "ORDER BY session_id",
            _LEGACY_PART,
            shards,
            shard,
        )
        for row in rows:
            session_id = row["session_id"]
            assert isinstance(session_id, UUID)
            stats = await retype_session(conn, session_id)
            sessions += 1
            rewritten += stats.rewritten
            records_in += stats.records_in
            records_out += stats.records_out
            slash += stats.slash_commands
    return RetypeStats(
        sessions=sessions,
        rewritten=rewritten,
        records_in=records_in,
        records_out=records_out,
        slash_commands=slash,
    )


async def retype_session(conn: Conn, session_id: UUID) -> RetypeStats:
    """Rewrite one session's legacy part; no-op when nothing is legacy.

    Args:
      conn: An acquired connection; the rewrite runs in its own transaction.
      session_id: The owning AgentSession.

    Returns:
      stats: ``rewritten == 0`` when the part held no ``legacy/*`` rows.

    """
    async with tx(conn):
        sources = await _read_sources(conn, session_id)
        if not any(source.legacy_kind in LEGACY_KINDS for source in sources):
            return RetypeStats(sessions=1)
        outputs: list[_Output] = []
        commands: list[SlashCommandOut] = []
        for source in sources:
            outputs.extend(_outputs_for(source, commands))
        await _replace_part(conn, session_id, outputs)
        await _insert_slash_commands(conn, session_id, commands)
        return RetypeStats(
            sessions=1,
            rewritten=1,
            records_in=len(sources),
            records_out=len(outputs),
            slash_commands=len(commands),
        )


_LEGACY_PART: Final = -1


@dataclass(frozen=True, slots=True, kw_only=True)
class _Source:
    """One stored part ``-1`` row, decoded just far enough to route."""

    kind: str
    context_id: int | None
    timestamp: datetime | None
    model: str | None
    created: datetime
    payload_text: str
    text: str
    legacy_kind: str
    ciphertext: str


@dataclass(frozen=True, slots=True, kw_only=True)
class _Output:
    """One row to write: the storable projection plus its ride-along columns."""

    kind: str
    context_id: int | None
    timestamp: datetime | None
    model: str | None
    created: datetime
    payload_text: str
    text: str
    ciphertext: str


async def _read_sources(conn: Conn, session_id: UUID) -> list[_Source]:
    """Read the part's rows in ``idx`` order, ciphertext joined, rows locked."""
    rows = await conn.fetch(
        "SELECT r.kind, r.context_id, r.timestamp, r.model, r.created, "
        "r.payload, r.text, "
        "(SELECT c.bytes FROM session_ciphertext c "
        " WHERE c.session_id = r.session_id AND c.part = r.part "
        " AND c.idx = r.idx) AS bytes "
        "FROM session_records r "
        "WHERE r.session_id = $1 AND r.part = $2 "
        "ORDER BY r.idx "
        "FOR UPDATE OF r",
        session_id,
        _LEGACY_PART,
    )
    sources: list[_Source] = []
    for row in rows:
        kind = row["kind"]
        assert isinstance(kind, str)
        context_id = row["context_id"]
        if context_id is not None and not isinstance(context_id, int):
            raise ValueError(
                "Expected context_id is None or isinstance(context_id, int).",
            )
        timestamp = row["timestamp"]
        if timestamp is not None and not isinstance(timestamp, datetime):
            raise ValueError(
                "Expected timestamp is None or isinstance(timestamp, datetime).",
            )
        model = row["model"]
        if model is not None and not isinstance(model, str):
            raise ValueError("Expected model is None or isinstance(model, str).")
        created = row["created"]
        assert isinstance(created, datetime)
        payload_text = row["payload"]
        assert isinstance(payload_text, str)
        text = row["text"]
        assert isinstance(text, str)
        raw_bytes = row["bytes"]
        if raw_bytes is not None and not isinstance(raw_bytes, bytes):
            raise ValueError(
                "Expected raw_bytes is None or isinstance(raw_bytes, bytes).",
            )
        sources.append(
            _Source(
                kind=kind,
                context_id=context_id,
                timestamp=timestamp,
                model=model,
                created=created,
                payload_text=payload_text,
                text=text,
                legacy_kind=StrCodec.coerce(
                    DictCodec.coerce(loads(payload_text)).get("kind"),
                ),
                ciphertext="" if raw_bytes is None else raw_bytes.decode(),
            ),
        )
    return sources


def _outputs_for(
    source: _Source,
    commands: list[SlashCommandOut],
) -> list[_Output]:
    """Return the rows one source becomes; slash commands collect into ``commands``."""
    retypable = (
        source.kind == "UncategorizedRecord" and source.legacy_kind in LEGACY_KINDS
    )
    if not retypable:
        # Verbatim copy: payload text, search text, and ciphertext exactly as
        # stored -- only ``idx`` is reassigned at write. Decoding would risk a
        # re-encode that is not byte-identical, and 020's hand-written
        # ``text`` would be lost to a projection that computes ``''`` for
        # UncategorizedRecord.
        return [
            _Output(
                kind=source.kind,
                context_id=source.context_id,
                timestamp=source.timestamp,
                model=source.model,
                created=source.created,
                payload_text=source.payload_text,
                text=source.text,
                ciphertext=source.ciphertext,
            ),
        ]
    record = DataclassCodec.from_json(
        UncategorizedRecord,
        DictCodec.coerce(loads(source.payload_text)),
    )
    out = retype(
        record,
        timestamp=(
            source.timestamp.isoformat() if source.timestamp is not None else None
        ),
        ciphertext=source.ciphertext,
    )
    if out.slash is not None:
        commands.append(out.slash)
    outputs: list[_Output] = []
    for typed in out.records:
        row = SessionRecordRow.of(
            session_id=UUID(int=0),  # Identity is assigned at write time.
            part=_LEGACY_PART,
            idx=0,
            record=typed,
            model=source.model,
        )
        outputs.append(
            _Output(
                kind=row.kind,
                context_id=row.context_id,
                timestamp=row.timestamp,
                model=source.model,
                created=source.created,
                payload_text=json.dumps(
                    json_unfreeze(row.payload),
                    separators=(",", ":"),
                ),
                text=row.text,
                ciphertext=row.ciphertext or "",
            ),
        )
    return outputs


async def _replace_part(
    conn: Conn,
    session_id: UUID,
    outputs: list[_Output],
) -> None:
    """Swap the part's rows and ciphertext; rewrite the manifest bound."""
    await conn.execute(
        "DELETE FROM session_ciphertext WHERE session_id = $1 AND part = $2",
        session_id,
        _LEGACY_PART,
    )
    await conn.execute(
        "DELETE FROM session_records WHERE session_id = $1 AND part = $2",
        session_id,
        _LEGACY_PART,
    )
    if outputs:
        # ``unnest`` arrays, not executemany: the largest legacy session holds
        # over a million records, and per-row round trips there are the 19
        # serial minutes the storage-cost doc warns about.
        await conn.execute(
            "INSERT INTO session_records (session_id, part, idx, kind, "
            "context_id, timestamp, model, payload, text, created) "
            "SELECT $1, $2, t.ordinality - 1, t.kind, t.context_id, "
            "t.timestamp, t.model, t.payload::json, t.text, t.created "
            "FROM unnest($3::text[], $4::int[], $5::timestamptz[], $6::text[], "
            "$7::text[], $8::text[], $9::timestamptz[]) "
            "WITH ORDINALITY AS t(kind, context_id, timestamp, model, payload, "
            "text, created, ordinality)",
            session_id,
            _LEGACY_PART,
            [output.kind for output in outputs],
            [output.context_id for output in outputs],
            [output.timestamp for output in outputs],
            [output.model for output in outputs],
            [output.payload_text for output in outputs],
            [output.text for output in outputs],
            [output.created for output in outputs],
        )
        sealed = [
            (index, output.ciphertext)
            for index, output in enumerate(outputs)
            if output.ciphertext
        ]
        if sealed:
            await conn.execute(
                "INSERT INTO session_ciphertext (session_id, part, idx, bytes) "
                "SELECT $1, $2, t.idx, t.bytes "
                "FROM unnest($3::int[], $4::bytea[]) AS t(idx, bytes)",
                session_id,
                _LEGACY_PART,
                [index for index, _ in sealed],
                [ciphertext.encode() for _, ciphertext in sealed],
            )
    await conn.execute(
        "UPDATE session_manifests SET records = $3 WHERE session_id = $1 AND part = $2",
        session_id,
        _LEGACY_PART,
        len(outputs),
    )


# Server-numbered from the session's own max, matching
# ``session_ir.py::_append_slash_commands`` -- a retyped session may hold
# commands from a live post-migration run already.
async def _insert_slash_commands(
    conn: Conn,
    session_id: UUID,
    commands: list[SlashCommandOut],
) -> None:
    """Store routed commands after the session's existing max seq."""
    if not commands:
        return
    await conn.execute(
        "INSERT INTO session_slash_commands "
        "(session_id, seq, timestamp, command, args) "
        "SELECT $1, "
        "  (SELECT coalesce(max(seq) + 1, 0) FROM session_slash_commands "
        "   WHERE session_id = $1) + ordinality - 1, "
        "  t.timestamp::timestamptz, t.command, t.args "
        "FROM unnest($2::text[], $3::text[], $4::text[]) "
        "  WITH ORDINALITY AS t(timestamp, command, args, ordinality) "
        "ON CONFLICT (session_id, seq) DO NOTHING",
        session_id,
        [command.timestamp for command in commands],
        [command.command for command in commands],
        [command.args for command in commands],
    )


if __name__ == "__main__":
    import asyncio
    import sys

    from trackinizer.lib.postgres import PostgresEngine
    from trackinizer.server.notify import NOTIFY_CHANNEL

    async def _run(dsn: str, shards: int) -> None:
        async with PostgresEngine(
            dsn=dsn,
            listen_channel=NOTIFY_CHANNEL,
            # One pooled connection per shard, or gather serializes on the
            # default pool cap and the fan-out buys nothing.
            max_size=shards,
        ) as engine:
            results = await asyncio.gather(
                *(
                    retype_all(engine, shards=shards, shard=shard)
                    for shard in range(shards)
                ),
            )
        for shard, stats in enumerate(results):
            print(f"shard {shard}: {stats}")  # noqa: T201 -- Run-hook output.
        total_in = sum(stats.records_in for stats in results)
        total_out = sum(stats.records_out for stats in results)
        print(f"total: {total_in} in -> {total_out} out")  # noqa: T201 -- Run-hook output.

    asyncio.run(_run(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 16))
