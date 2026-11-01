"""Sweep ``session_records`` into ``session_embeddings`` under a mapper policy.

The one pass that serves BOTH backfill and live ingest. A sweep reconciles one
``(mapper, model)`` pair: it asks a
:class:`~trackinizer.server.semantic_mapper.SemanticMapper` which spans of
each record to index, and re-embeds exactly the records whose text changed --
``md5(r.text)`` against the record's freshness marker, so a missing marker (never
embedded) and a stale one (text rewritten by a claude compaction's ``restart``
upsert, or by the legacy retype) are the same case.

Incremental: each pair keeps a cursor in ``session_index_cursor``, the snapshot
its last sweep read through. A sweep reads only the records whose
``changed_xid`` (stamped by a trigger on every insert and text change) that
snapshot could not see, so a round costs what changed, not the corpus. A pair
with no cursor -- a new model, or a database from before schema.063 -- catches up
with one full pending scan first. The snapshot is taken before either read, so a
record changed during the sweep is left for the next one, never lost.

Its embedder is its OWN, not the Store's: ``session_embeddings`` is
``halfvec(1024)`` while ``inquiry_embeddings`` is ``vector(384)``, and
``Store.__init__`` rejects a non-384 embedder. The sweep therefore takes the
1024-dim embedder as an argument and never touches ``self.embedders``.

Orphan reaping: a record whose kind stopped being indexed, or whose units
shrank (fewer chunks after an edit), leaves ``session_embeddings`` rows with no
current unit. Each swept record DELETEs its stale rows for this
``(mapper, model)`` before inserting the current ones, in one transaction, so a
reader never sees a torn set.

Freshness is tracked in ``session_index_state`` (one marker row per swept
record, keyed ``(session_id, part, idx, mapper, model)``), NOT by the presence
of a ``session_embeddings`` row. That decoupling is what lets the sweep handle
an fts-ONLY record (a machine-output head, a SystemMessage, a Thinking block --
all ``embed=False`` under the footprint policy): it writes zero vectors but a
marker, so it reads up-to-date on the next pass instead of re-sweeping forever.

The GPU bulk backfill (``server/tools/backfill_embedding.py``) shares this
module's pending predicate and writer, so both paths agree on what is pending and
write vectors and markers the same way.

One writer per pair: a sweep and a GPU run each hold :func:`sole_writer` while
they write, so two processes never embed the same records at once. A sweep that
finds its pair held writes nothing and says so.
"""

from __future__ import annotations

from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

import hashlib

from trackinizer.server.notify import tx
from trackinizer.server.values import manifest_bound, vetted_sql


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from asyncpg import Record

    import asyncpg

    from trackinizer.lib.postgres import Conn, DatabaseEngine
    from trackinizer.server.semantic_mapper import IndexUnit, SemanticMapper
    from trackinizer.types.embedder import Embedder
else:
    from wrapt import lazy_import

    asyncpg = lazy_import("asyncpg")  # ~60 ms.


__all__ = [
    "Pool",
    "SweepStats",
    "current_snapshot",
    "embed_texts",
    "pending_page_sql",
    "pending_where",
    "save_cursor",
    "sole_writer",
    "sweep_session_embeddings",
    "write_pool",
]


# Records awaiting a write: each with the units that get vectors (empty for an
# fts-only record, which still gets its marker) and its text's md5.
type Pool = list[tuple[Record, tuple[IndexUnit, ...], str]]


@dataclass(frozen=True, slots=True, kw_only=True)
class SweepStats:
    """What one sweep pass did.

    Attributes:
      records_embedded: Records reconciled this pass: vectors and marker
        rewritten, or the marker alone for an fts-only record.
      units_written: ``session_embeddings`` rows inserted this pass.
      full_scan: The pair had no cursor, so this pass read every pending record
        rather than only the ones changed since the last sweep.
      busy: Another process was writing the pair, so this pass wrote nothing.

    """

    records_embedded: int = 0
    units_written: int = 0
    full_scan: bool = False
    busy: bool = False


async def sweep_session_embeddings(
    engine: DatabaseEngine,
    *,
    mapper: SemanticMapper,
    embedder: Embedder,
    batch: int = 500,
) -> SweepStats:
    """Reconcile ``session_embeddings`` with the records changed since the cursor.

    Args:
      engine: The store's engine; one connection for the whole sweep.
      mapper: The policy selecting which spans of a record to index.
      embedder: The 1024-dim embedder whose vectors fill ``halfvec(1024)``;
        its ``name`` keys the rows alongside ``mapper.name``.
      batch: Records per page; one page is one embed call and one write.

    Returns:
      stats: Counts over the whole sweep.

    """
    embedded = written = 0
    head = (mapper.name, embedder.name, sorted(mapper.indexed_kinds))
    async with (
        engine.acquire() as conn,
        sole_writer(conn, mapper=mapper.name, model=embedder.name) as held,
    ):
        if not held:
            return SweepStats(busy=True)
        # First, so a record changed while this sweep runs is newer than the
        # cursor it saves and is read by the next sweep.
        through = await _snapshot(conn)
        since = cast(
            "str | None",
            await conn.fetchval(
                "SELECT snapshot::text FROM session_index_cursor "
                "WHERE mapper = $1 AND model = $2",
                mapper.name,
                embedder.name,
            ),
        )
        last: Record | None = None
        while rows := await _next_page(
            conn,
            since=since,
            through=through,
            last=last,
            head=head,
            batch=batch,
        ):
            last = rows[-1]
            pool = _pool(rows, mapper)
            vectors = await embed_texts(
                embedder,
                [unit.text for _row, units, _md5 in pool for unit in units],
            )
            written += await write_pool(
                conn,
                pool,
                vectors,
                embedder=embedder,
                mapper_name=mapper.name,
            )
            embedded += len(pool)
        await _save_cursor(conn, mapper.name, embedder.name, through)
    return SweepStats(
        records_embedded=embedded,
        units_written=written,
        full_scan=since is None,
    )


# Session-level, not per transaction: a writer commits one transaction per pool and
# holds the pair across all of them. The lock dies with its connection, so a killed
# writer never leaves the pair held. Unlocking a connection that already died raises
# the connection error again, which would hide why the block failed.
@asynccontextmanager
async def sole_writer(conn: Conn, *, mapper: str, model: str) -> AsyncGenerator[bool]:
    """Hold ``(mapper, model)``'s writer lock on ``conn`` for the block, if free.

    Args:
      conn: The connection that holds the lock while the block runs.
      mapper: The mapper policy's name.
      model: The stored model identity.

    Yields:
      held: True when the block is the pair's one writer; False when another
        connection holds the pair, and the block must write nothing.

    """
    key = f"trackinizer.session_embed:{mapper}:{model}"
    held = await conn.fetchval("SELECT pg_try_advisory_lock(hashtext($1))", key) is True
    try:
        yield held
    finally:
        if held:
            with suppress(
                asyncpg.PostgresConnectionError,
                asyncpg.InterfaceError,
                ConnectionRefusedError,
            ):
                await conn.execute("SELECT pg_advisory_unlock(hashtext($1))", key)


async def current_snapshot(engine: DatabaseEngine) -> str:
    """Return the database's current snapshot, for a later :func:`save_cursor`.

    A caller that reconciles a pair some other way (the GPU bulk backfill) takes
    this BEFORE it starts reading, then saves it once it is done, so every record
    changed meanwhile is still newer than the cursor.

    Args:
      engine: The store's engine.

    Returns:
      snapshot: ``pg_current_snapshot()`` as text (``xmin:xmax:xip,...``).

    """
    async with engine.acquire() as conn:
        return await _snapshot(conn)


async def save_cursor(
    engine: DatabaseEngine,
    *,
    mapper: str,
    model: str,
    snapshot: str,
) -> None:
    """Record that ``(mapper, model)`` is reconciled through ``snapshot``."""
    async with engine.acquire() as conn:
        await _save_cursor(conn, mapper, model, snapshot)


def pending_where(predicate: str) -> str:
    """Return the INDEXABLE + PENDING filter over ``r`` (``$1..$3`` bound).

    ``$1`` and ``$2`` are the mapper and model, ``$3`` the kinds that yield units
    as a ``text[]``. A row is indexable only if its kind yields units and its text
    is non-empty -- exactly the mapper's contract -- so a never-indexed kind or an
    empty text is never counted pending forever. Pending is keyed on the marker,
    not on a vector, so an fts-only record reads fresh once swept.

    Args:
      predicate: The manifest-bound predicate over ``r`` (from ``manifest_bound``).

    Returns:
      sql: The ``WHERE`` body, without the keyword.

    """
    return vetted_sql(
        predicate,
        " AND r.kind = ANY($3::text[]) AND r.text <> ''",
        " AND NOT EXISTS (SELECT 1 FROM session_index_state s "
        "WHERE s.session_id = r.session_id AND s.part = r.part "
        "AND s.idx = r.idx AND s.mapper = $1 AND s.model = $2 "
        "AND s.text_md5 = md5(r.text))",
    )


# Audience: internal -- a fragment the sweep and the GPU backfill complete; neither
# returns what it reads to a caller.
def pending_page_sql(
    join: str,
    predicate: str,
    *,
    first: bool,
    columns: str,
) -> str:
    """Return one keyset page of PENDING rows in feed order.

    Feed order is the console's index ``(created, session_id, part, idx)``, so the
    walk is index-driven and starts at the first pending row by construction.

    Args:
      join: The manifest join (from ``manifest_bound``).
      predicate: The manifest-bound predicate over ``r``.
      first: The first page binds no keyset; later pages bind ``$4..$7``.
      columns: The columns to select.

    Returns:
      sql: The page query; its ``LIMIT`` is the last parameter.

    """
    keyset = (
        ""
        if first
        else "(r.created, r.session_id, r.part, r.idx) > ($4, $5, $6, $7) AND "
    )
    return vetted_sql(
        "SELECT ",
        columns,
        " FROM session_records r ",
        join,
        "WHERE ",
        keyset,
        pending_where(predicate),
        " ORDER BY r.created, r.session_id, r.part, r.idx LIMIT ",
        "$4" if first else "$8",
    )


# Audience: internal -- a background job's writer; it returns only a count.
async def write_pool(
    conn: Conn,
    pool: Pool,
    vectors: Sequence[Sequence[float]],
    *,
    embedder: Embedder,
    mapper_name: str,
) -> int:
    """Write one embedded pool: one unnest DELETE+INSERT+marker transaction.

    Every unit of a record shares its source text, so the marker, one per record,
    carries one md5. The DELETE covers every pooled record, so an fts-only record
    or one whose units shrank keeps no stale vector.

    Args:
      conn: A connection outside any transaction.
      pool: The records and their vector-bearing units, in ``vectors`` order.
      vectors: One vector per unit across the pool, in pool order.
      embedder: The embedder that produced ``vectors``; its name keys the rows.
      mapper_name: The policy that produced the units.

    Returns:
      units: ``session_embeddings`` rows inserted.

    """
    sids: list[object] = []
    parts: list[object] = []
    idxs: list[object] = []
    fields: list[str] = []
    chunks: list[int] = []
    embeds: list[str] = []
    md5s: list[str] = []
    at = 0
    for row, units, text_md5 in pool:
        take = vectors[at : at + len(units)]
        at += len(units)
        for unit, vector in zip(units, take, strict=True):
            sids.append(row["session_id"])
            parts.append(row["part"])
            idxs.append(row["idx"])
            fields.append(unit.field)
            chunks.append(unit.chunk)
            embeds.append("[" + ",".join(repr(v) for v in vector) + "]")
            md5s.append(text_md5)
    async with tx(conn):
        await conn.execute(
            "DELETE FROM session_embeddings e USING unnest($1::uuid[], "
            "$2::int[], $3::int[]) AS t(session_id, part, idx) "
            "WHERE e.session_id = t.session_id AND e.part = t.part "
            "AND e.idx = t.idx AND e.mapper = $4 AND e.model = $5",
            [row["session_id"] for row, _units, _md5 in pool],
            [row["part"] for row, _units, _md5 in pool],
            [row["idx"] for row, _units, _md5 in pool],
            mapper_name,
            embedder.name,
        )
        await conn.execute(
            "INSERT INTO session_embeddings "
            "(session_id, part, idx, field, chunk, mapper, model, embedding, "
            "text_md5) "
            "SELECT session_id, part, idx, field, chunk, $8, $9, "
            "embedding::halfvec, text_md5 FROM unnest($1::uuid[], $2::int[], "
            "$3::int[], $4::text[], $5::int[], $6::text[], $7::text[]) "
            "AS t(session_id, part, idx, field, chunk, embedding, text_md5)",
            sids,
            parts,
            idxs,
            fields,
            chunks,
            embeds,
            md5s,
            mapper_name,
            embedder.name,
        )
        await conn.execute(
            "INSERT INTO session_index_state "
            "(session_id, part, idx, mapper, model, text_md5) "
            "SELECT DISTINCT session_id, part, idx, $4, $5, text_md5 "
            "FROM unnest($1::uuid[], $2::int[], $3::int[], $6::text[]) "
            "AS t(session_id, part, idx, text_md5) "
            "ON CONFLICT (session_id, part, idx, mapper, model) "
            "DO UPDATE SET text_md5 = EXCLUDED.text_md5, created = now()",
            [row["session_id"] for row, _units, _md5 in pool],
            [row["part"] for row, _units, _md5 in pool],
            [row["idx"] for row, _units, _md5 in pool],
            mapper_name,
            embedder.name,
            [text_md5 for _row, _units, text_md5 in pool],
        )
    return len(sids)


# The base ``Embedder`` protocol guarantees only ``embed``, so the batch path is
# feature-detected rather than widening the protocol every embedder must meet.
async def embed_texts(embedder: Embedder, texts: list[str]) -> list[list[float]]:
    """Embed ``texts`` in one batch call when the embedder offers it, input order.

    Shortest first, so a forward batch pads to a similar length instead of every
    short text padding out to the call's longest.

    Args:
      embedder: The embedder; its ``embed_batch`` is used when it has one.
      texts: The texts, any order.

    Returns:
      vectors: One vector per text, in ``texts`` order.

    """
    if not texts:
        return []
    order = sorted(range(len(texts)), key=lambda slot: len(texts[slot]))
    shortest_first = [texts[slot] for slot in order]
    if isinstance(embedder, _BatchEmbedder):
        vectors = await embedder.embed_batch(shortest_first)
    else:
        vectors = [await embedder.embed(text) for text in shortest_first]
    by_slot = dict(zip(order, vectors, strict=True))
    return [by_slot[slot] for slot in range(len(texts))]


async def _snapshot(conn: Conn) -> str:
    """Return the current snapshot as text (``xmin:xmax:xip,...``)."""
    snapshot = await conn.fetchval("SELECT pg_current_snapshot()::text")
    assert isinstance(snapshot, str)
    return snapshot


async def _save_cursor(conn: Conn, mapper: str, model: str, snapshot: str) -> None:
    """Upsert ``(mapper, model)``'s cursor to ``snapshot``."""
    await conn.execute(
        "INSERT INTO session_index_cursor (mapper, model, snapshot) "
        "VALUES ($1, $2, $3::text::pg_snapshot) "
        "ON CONFLICT (mapper, model) DO UPDATE SET snapshot = EXCLUDED.snapshot, "
        "updated = now()",
        mapper,
        model,
        snapshot,
    )


# A delta reads the rows the cursor's snapshot could not see and the sweep's own
# snapshot can: a transaction still open at the cursor counts as unseen however
# old its xid, which is what a timestamp watermark would get wrong. The xid range
# comes first so the partial index on ``changed_xid`` bounds the walk, and the page
# advances in ``changed_xid`` order. A row changed again after ``through`` drops
# out of this delta and into the next one, so the pages may come from separate
# transactions. With no cursor the page is the full pending scan in feed order, the
# walk the GPU backfill's scanner makes. ``changed_xid`` and the snapshots cross the
# driver as text: asyncpg ships no codec for ``xid8`` or ``pg_snapshot``.
# Audience: internal -- a background job; nothing it reads reaches a caller.
async def _next_page(
    conn: Conn,
    *,
    since: str | None,
    through: str,
    last: Record | None,
    head: tuple[str, str, list[str]],
    batch: int,
) -> list[Record]:
    """Return the pending rows after ``last``: changed since ``since``, or all."""
    join, predicate = manifest_bound("r")
    columns = (
        "r.created, r.changed_xid::text AS changed_xid, r.session_id, r.part, "
        "r.idx, r.kind, r.text"
    )
    if since is None:
        after = () if last is None else (last["created"], *_record_key(last))
        sql = pending_page_sql(join, predicate, first=last is None, columns=columns)
        return list(await conn.fetch(sql, *head, *after, batch))
    after = () if last is None else (last["changed_xid"], *_record_key(last))
    sql = vetted_sql(
        "SELECT ",
        columns,
        " FROM session_records r ",
        join,
        "WHERE r.changed_xid >= pg_snapshot_xmin($4::text::pg_snapshot) "
        "AND r.changed_xid < pg_snapshot_xmax($5::text::pg_snapshot) "
        "AND NOT pg_visible_in_snapshot(r.changed_xid, $4::text::pg_snapshot) "
        "AND pg_visible_in_snapshot(r.changed_xid, $5::text::pg_snapshot) AND ",
        ""
        if last is None
        else "(r.changed_xid, r.session_id, r.part, r.idx) "
        "> ($6::text::xid8, $7, $8, $9) AND ",
        pending_where(predicate),
        " ORDER BY r.changed_xid, r.session_id, r.part, r.idx LIMIT ",
        "$6" if last is None else "$10",
    )
    return list(await conn.fetch(sql, *head, since, through, *after, batch))


def _record_key(row: Record) -> tuple[object, object, object]:
    """Return a row's ``(session_id, part, idx)``, the tail of either keyset."""
    return (row["session_id"], row["part"], row["idx"])


def _pool(rows: Sequence[Record], mapper: SemanticMapper) -> Pool:
    """Map each pending row to its vector-bearing units and its text's md5."""
    pool: Pool = []
    for row in rows:
        kind = row["kind"]
        text = row["text"]
        assert isinstance(kind, str)
        assert isinstance(text, str)
        units = tuple(unit for unit in mapper.units(kind=kind, text=text) if unit.embed)
        text_md5 = hashlib.md5(  # A change-detector, not a security hash.
            text.encode("utf-8"),
            usedforsecurity=False,
        ).hexdigest()
        pool.append((row, units, text_md5))
    return pool


@runtime_checkable
class _BatchEmbedder(Protocol):
    """An embedder that can embed several texts in one call.

    The base ``Embedder`` protocol guarantees only ``embed``; this widens it
    for the sweep's batch path without forcing every embedder to implement
    ``embed_batch`` (``StubEmbedder`` does not).
    """

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed ``texts``, one unit vector each, in input order."""
        ...
