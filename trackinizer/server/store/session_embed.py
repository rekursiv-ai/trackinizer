"""Sweep ``session_records`` into ``session_embeddings`` under a mapper policy.

The one pass that serves BOTH backfill and live ingest (design:
``docs/private/session_indexing.md``). It keyset-scans records on the console
feed's own index ``(created, session_id, part, idx)``, asks a
:class:`~trackinizer.server.semantic_mapper.SemanticMapper` which spans of
each record to index, and re-embeds exactly the records whose text changed --
the predicate is ``md5(r.text) IS DISTINCT FROM e.text_md5``, so a missing row
(never embedded) and a stale row (text rewritten by a claude compaction's
``restart`` upsert, or by the legacy retype) are the same case.

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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from uuid import UUID

import hashlib

from trackinizer.server.notify import tx
from trackinizer.server.values import manifest_bound, vetted_sql


if TYPE_CHECKING:
    from asyncpg import Record

    from trackinizer.lib.postgres import Conn, DatabaseEngine
    from trackinizer.server.semantic_mapper import IndexUnit, SemanticMapper
    from trackinizer.types.embedder import Embedder


__all__ = [
    "SweepStats",
    "sweep_session_embeddings",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class SweepStats:
    """What one sweep pass did.

    Attributes:
      records_scanned: Records read across every batch.
      records_embedded: Records whose units were (re-)embedded this pass.
      units_written: ``session_embeddings`` rows inserted this pass.
      records_skipped: Indexed records already up to date (md5 matched).

    """

    records_scanned: int = 0
    records_embedded: int = 0
    units_written: int = 0
    records_skipped: int = 0


async def sweep_session_embeddings(
    engine: DatabaseEngine,
    *,
    mapper: SemanticMapper,
    embedder: Embedder,
    batch: int = 500,
) -> SweepStats:
    """Reconcile ``session_embeddings`` with the current records and policy.

    Args:
      engine: The store's engine; one connection for the whole sweep.
      mapper: The policy selecting which spans of a record to index.
      embedder: The 1024-dim embedder whose vectors fill ``halfvec(1024)``;
        its ``name`` keys the rows alongside ``mapper.name``.
      batch: Records per keyset page.

    Returns:
      stats: Counts over the whole sweep.

    """
    scanned = embedded = written = skipped = 0
    cursor: tuple[object, UUID, int, int] | None = None
    async with engine.acquire() as conn:
        while True:
            rows = await _page(conn, cursor, batch)
            if not rows:
                break
            for row in rows:
                created = row["created"]
                session_id = row["session_id"]
                assert isinstance(session_id, UUID)
                part = row["part"]
                assert isinstance(part, int)
                idx = row["idx"]
                assert isinstance(idx, int)
                kind = row["kind"]
                assert isinstance(kind, str)
                text = row["text"]
                assert isinstance(text, str)
                cursor = (created, session_id, part, idx)
                scanned += 1
                units = mapper.units(kind=kind, text=text)
                if not units:
                    continue
                text_md5 = hashlib.md5(  # A change-detector, not a security hash.
                    text.encode("utf-8"),
                    usedforsecurity=False,
                ).hexdigest()
                if await _up_to_date(
                    conn,
                    session_id,
                    part,
                    idx,
                    mapper=mapper.name,
                    model=embedder.name,
                    text_md5=text_md5,
                ):
                    skipped += 1
                    continue
                written += await _reembed(
                    conn,
                    session_id,
                    part,
                    idx,
                    units=units,
                    mapper=mapper.name,
                    embedder=embedder,
                    text_md5=text_md5,
                )
                embedded += 1
    return SweepStats(
        records_scanned=scanned,
        records_embedded=embedded,
        units_written=written,
        records_skipped=skipped,
    )


# Keyset on the console-feed index ``(created, session_id, part, idx)``: a plain
# OFFSET would re-scan the prefix every page and drift under concurrent inserts.
async def _page(
    conn: Conn,
    cursor: tuple[object, UUID, int, int] | None,
    batch: int,
) -> list[Record]:
    """Return the next ``batch`` records after ``cursor`` in feed order."""
    # Exclude stale tail rows a compaction-restart left beyond the live manifest
    # prefix, so the sweep never (re-)embeds a record the current file dropped.
    join, predicate = manifest_bound("r")
    if cursor is None:
        return list(
            await conn.fetch(
                vetted_sql(
                    "SELECT r.created, r.session_id, r.part, r.idx, r.kind, r.text "
                    "FROM session_records r ",
                    join,
                    "WHERE ",
                    predicate,
                    " ORDER BY r.created, r.session_id, r.part, r.idx LIMIT $1",
                ),
                batch,
            ),
        )
    created, session_id, part, idx = cursor
    return list(
        await conn.fetch(
            vetted_sql(
                "SELECT r.created, r.session_id, r.part, r.idx, r.kind, r.text "
                "FROM session_records r ",
                join,
                "WHERE (r.created, r.session_id, r.part, r.idx) > ($1, $2, $3, $4) "
                "AND ",
                predicate,
                " ORDER BY r.created, r.session_id, r.part, r.idx LIMIT $5",
            ),
            created,
            session_id,
            part,
            idx,
            batch,
        ),
    )


# Freshness lives in ``session_index_state``, keyed on (record, mapper, model) --
# NOT in ``session_embeddings``, so an fts-only record (no vector) still marks
# fresh. Every unit of a record shares its source text, so its marker holds one
# md5. A record with no marker reads as not up to date.
async def _up_to_date(
    conn: Conn,
    session_id: UUID,
    part: int,
    idx: int,
    *,
    mapper: str,
    model: str,
    text_md5: str,
) -> bool:
    """Whether this record's marker already carries ``text_md5``."""
    existing = await conn.fetchval(
        "SELECT text_md5 FROM session_index_state "
        "WHERE session_id = $1 AND part = $2 AND idx = $3 "
        "AND mapper = $4 AND model = $5",
        session_id,
        part,
        idx,
        mapper,
        model,
    )
    return existing == text_md5


# Only ``embed`` units become vectors; an ``fts``-only unit rides the tsvector surface
# and stores no ``session_embeddings`` row. An fts-only record (zero embed units) writes
# no vector at all -- just its freshness marker -- rather than re-sweeping forever.
# Delete-then-insert-then-mark in one transaction so a shrunk unit set leaves no orphan
# and a reader never sees a torn set.
async def _reembed(
    conn: Conn,
    session_id: UUID,
    part: int,
    idx: int,
    *,
    units: tuple[IndexUnit, ...],
    mapper: str,
    embedder: Embedder,
    text_md5: str,
) -> int:
    """Replace this record's vectors + marker for ``(mapper, model)``; return vectors."""
    embed_units = [unit for unit in units if unit.embed]
    # Embed the record's embed units in ONE batch when the embedder offers it: a
    # long message fans into several chunk units, and backfill throughput is
    # dominated by per-call overhead at batch=1. Vectors are computed before the
    # transaction opens so a slow forward pass never holds the row lock. An
    # fts-only record embeds nothing here and only refreshes its marker.
    vectors = await _embed_units(embedder, [unit.text for unit in embed_units])
    async with tx(conn):
        await conn.execute(
            "DELETE FROM session_embeddings "
            "WHERE session_id = $1 AND part = $2 AND idx = $3 "
            "AND mapper = $4 AND model = $5",
            session_id,
            part,
            idx,
            mapper,
            embedder.name,
        )
        for unit, vector in zip(embed_units, vectors, strict=True):
            await conn.execute(
                "INSERT INTO session_embeddings "
                "(session_id, part, idx, field, chunk, mapper, model, "
                "embedding, text_md5) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
                session_id,
                part,
                idx,
                unit.field,
                unit.chunk,
                mapper,
                embedder.name,
                "[" + ",".join(repr(v) for v in vector) + "]",
                text_md5,
            )
        # The freshness marker: written for EVERY swept record, embedded or not,
        # so the next pass reads it up-to-date. Upsert because a re-embed after an
        # edit reconciles the same key to the new md5.
        await conn.execute(
            "INSERT INTO session_index_state "
            "(session_id, part, idx, mapper, model, text_md5) "
            "VALUES ($1, $2, $3, $4, $5, $6) "
            "ON CONFLICT (session_id, part, idx, mapper, model) "
            "DO UPDATE SET text_md5 = EXCLUDED.text_md5, created = now()",
            session_id,
            part,
            idx,
            mapper,
            embedder.name,
            text_md5,
        )
    return len(embed_units)


# Prefer the batch path when the embedder exposes one (QwenEmbedder does): the
# base ``Embedder`` protocol guarantees only ``embed``, so this feature-detects
# rather than widening the protocol every embedder must then implement.
async def _embed_units(embedder: Embedder, texts: list[str]) -> list[list[float]]:
    """Embed one record's unit texts, batched when the embedder supports it."""
    if not texts:
        return []
    if isinstance(embedder, _BatchEmbedder):
        return await embedder.embed_batch(texts)
    return [await embedder.embed(text) for text in texts]


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
