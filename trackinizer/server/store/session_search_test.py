"""The session search read path against real Postgres (pglite).

Seeds ``session_embeddings`` + the scoped tsvector exactly as production does
-- through ``sweep_session_embeddings`` with ``StubEmbedder(dim=1024)`` and
``FootprintMapper`` -- then exercises both arms and their RRF merge. The
properties are the database's: HNSW cosine ordering, ``websearch_to_tsquery``
matching, and the fused rank, none observable without the live index.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.semantic_mapper_footprint import FootprintMapper
from trackinizer.server.store.core import Store
from trackinizer.server.store.session_embed import sweep_session_embeddings
from trackinizer.server.store.session_search import (
    RRF_K,
    SessionSearchHit,
    _ArmHit,
    _merge,
    search_session_records,
)


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trackinizer.lib.postgres import PostgresEngine


_MAPPER = FootprintMapper().name
_MODEL = StubEmbedder(dim=1024).name
_DIM = StubEmbedder(dim=1024).dim


@pytest_asyncio.fixture(loop_scope="session")
async def store(integ_engine: PostgresEngine) -> AsyncIterator[Store]:
    """Bootstrapped store with the session tables emptied (search is global)."""
    built = Store(integ_engine, embed=StubEmbedder())
    await built.bootstrap()
    async with built.engine.acquire() as conn:
        await conn.execute(
            "TRUNCATE session_embeddings, session_records, session_manifests, "
            "inquiries CASCADE",
        )
    yield built


async def _session(store: Store) -> UUID:
    session_id = uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "'tester@example.com', 'search test')",
            session_id,
        )
    return session_id


async def _record(
    store: Store,
    session_id: UUID,
    *,
    idx: int,
    text: str,
    kind: str = "UserMessage",
) -> None:
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO session_records "
            "(session_id, part, idx, kind, payload, text) "
            "VALUES ($1, 0, $2, $3, '{}'::json, $4)",
            session_id,
            idx,
            kind,
            text,
        )
    # Every reader now bounds by the live manifest prefix (idx < records), so a
    # seeded record needs a manifest that covers it -- production never writes one
    # without the other. Grow the part's bound to include this idx.
    await _bound_part(store, session_id, records=idx + 1)


async def _bound_part(store: Store, session_id: UUID, *, records: int) -> None:
    """Upsert the part-0 manifest so records ``0..records-1`` are live."""
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO session_manifests "
            "(session_id, part, name, metadata, ir_id, format, records) "
            "VALUES ($1, 0, 's.jsonl', '{}'::json, gen_random_uuid(), "
            "'claude', $2) "
            "ON CONFLICT (session_id, part) DO UPDATE SET records = $2",
            session_id,
            records,
        )


async def _seed(store: Store) -> None:
    """Embed every seeded record through the real sweep."""
    await sweep_session_embeddings(
        store.engine,
        mapper=FootprintMapper(),
        embedder=StubEmbedder(dim=1024),
    )


async def _query_vector(text: str) -> list[float]:
    """Return the stub's deterministic vector for ``text`` -- an exact match."""
    return await StubEmbedder(dim=1024).embed(text)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_semantic_only_finds_the_nearest_unit(store: Store) -> None:
    """A vector query with no text returns the cosine-nearest unit first."""
    session_id = await _session(store)
    await _record(store, session_id, idx=0, text="deploy the release to production")
    await _record(store, session_id, idx=1, text="the cat napped on the sofa")
    await _seed(store)

    hits = await search_session_records(
        store.engine,
        query_vector=await _query_vector("deploy the release to production"),
        query_text="",
        mapper=_MAPPER,
        model=_MODEL,
        dim=_DIM,
    )

    assert hits
    assert all(h.source == "semantic" for h in hits)
    assert (hits[0].session_id, hits[0].idx) == (session_id, 0)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_fts_only_matches_the_term(store: Store) -> None:
    """A text query with no vector returns the term-matching records."""
    session_id = await _session(store)
    await _record(store, session_id, idx=0, text="advisory lock acquired cleanly")
    await _record(store, session_id, idx=1, text="unrelated prose about weather")
    await _seed(store)

    hits = await search_session_records(
        store.engine,
        query_vector=None,
        query_text="advisory lock",
        mapper=_MAPPER,
        model=_MODEL,
        dim=_DIM,
    )

    assert [(h.session_id, h.idx) for h in hits] == [(session_id, 0)]
    assert hits[0].source == "fts"
    assert "advisory" in hits[0].snippet.lower()


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_both_arms_rank_a_dual_match_first(store: Store) -> None:
    """A record matching BOTH arms outranks records matching only one.

    idx 0 matches the vector (exact) AND the text; idx 1 matches only the text;
    idx 2 matches only the vector. RRF must float the dual match to the top and
    label it ``both``.
    """
    session_id = await _session(store)
    await _record(store, session_id, idx=0, text="postgres advisory lock deadlock")
    await _record(store, session_id, idx=1, text="postgres advisory lock timeout")
    await _record(store, session_id, idx=2, text="a totally different subject line")
    await _seed(store)

    hits = await search_session_records(
        store.engine,
        query_vector=await _query_vector("postgres advisory lock deadlock"),
        query_text="advisory lock",
        mapper=_MAPPER,
        model=_MODEL,
        dim=_DIM,
    )

    by_idx = {h.idx: h for h in hits}
    assert hits[0].idx == 0
    assert by_idx[0].source == "both"
    # `idx` 0 fused two arms; its score must beat any single-arm hit.
    assert all(hits[0].score >= h.score for h in hits)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_limit_is_respected(store: Store) -> None:
    """No more than ``limit`` merged hits come back."""
    session_id = await _session(store)
    for i in range(10):
        await _record(store, session_id, idx=i, text=f"record number {i} about lock")
    await _seed(store)

    hits = await search_session_records(
        store.engine,
        query_vector=None,
        query_text="lock",
        mapper=_MAPPER,
        model=_MODEL,
        dim=_DIM,
        limit=3,
    )

    assert len(hits) == 3


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_dedup_keeps_one_hit_per_record(store: Store) -> None:
    """A long record fans into several chunk units; the merge keeps ONE hit.

    The chunked units share ``(session_id, part, idx)``; a semantic query near
    the record must collapse them to a single hit at that position.
    """
    session_id = await _session(store)
    long_text = "".join(f"deadlock lock token{n:04d} " for n in range(1000))
    await _record(store, session_id, idx=0, kind="AssistantMessage", text=long_text)
    await _seed(store)
    # Sanity: the record produced more than one embedding unit.
    async with store.engine.acquire() as conn:
        unit_count = await conn.fetchval(
            "SELECT count(*) FROM session_embeddings WHERE session_id = $1",
            session_id,
        )
    assert isinstance(unit_count, int)
    assert unit_count > 1

    hits = await search_session_records(
        store.engine,
        query_vector=await _query_vector(long_text[:1500]),
        query_text="",
        mapper=_MAPPER,
        model=_MODEL,
        dim=_DIM,
    )

    positions = [(h.session_id, h.part, h.idx) for h in hits]
    assert positions == [(session_id, 0, 0)]  # One hit, not one-per-chunk.


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_empty_query_raises(store: Store) -> None:
    """Neither arm present is a caller error, not an empty result."""
    with pytest.raises(ValueError, match="at least one"):
        _ = await search_session_records(
            store.engine,
            query_vector=None,
            query_text="",
            mapper=_MAPPER,
            model=_MODEL,
            dim=_DIM,
        )


def _hit(idx: int, *, field: str = "content", chunk: int = 0) -> _ArmHit:
    """Make an ``_ArmHit`` at part 0, a fixed session, position ``idx``."""
    return _ArmHit(
        session_id=UUID(int=1),
        part=0,
        idx=idx,
        field=field,
        chunk=chunk,
        snippet=f"snippet-{idx}-{field}-{chunk}",
        title=f"session-{idx}",
    )


def test_merge_accumulates_rrf_across_arms() -> None:
    """A record ranked #2 in BOTH arms beats a record ranked #1 in ONE.

    ``2/(K+2) = 0.0323 > 1/(K+1) = 0.0164`` only if the arms' contributions
    ADD. A merge that overwrote instead of summing would rank the single-arm
    #1 first. Pins the fusion, not just "both appears somewhere".
    """
    dual = _hit(2)  # Rank 2 in each arm.
    single = _hit(1)  # Rank 1 in fts only.
    semantic = [_hit(9), dual]  # `dual` is rank 2.
    fts = [single, dual]  # `single` rank 1, dual rank 2.

    merged = _merge(semantic, fts, limit=10)

    by_idx = {h.idx: h for h in merged}
    assert merged[0].idx == 2  # The dual match wins on summed rank.
    assert by_idx[2].source == "both"
    assert by_idx[2].score > by_idx[1].score
    # The exact RRF arithmetic, so a changed constant or formula is caught.
    assert by_idx[2].score == pytest.approx(2.0 / (RRF_K + 2))
    assert by_idx[1].score == pytest.approx(1.0 / (RRF_K + 1))


def test_merge_dedup_keeps_the_best_ranked_unit() -> None:
    """Several units of one record collapse to the unit that ranked best.

    Two chunk units of one record appear in the semantic arm; the merged hit
    must carry the FIRST (best-ranked) unit, not the later one, and count the
    record once.
    """
    best = _hit(5, field="content", chunk=0)
    worse = _hit(5, field="content", chunk=3)
    merged = _merge([best, worse], [], limit=10)

    assert len(merged) == 1
    assert (merged[0].chunk, merged[0].snippet) == (0, best.snippet)
    # One record, one arm, rank 1 -> its own single-arm score.
    assert merged[0].source == "semantic"
    assert merged[0].score == pytest.approx(1.0 / (RRF_K + 1))


def test_merge_returns_session_search_hits() -> None:
    """The merge yields the public hit type, not the internal arm hit."""
    merged = _merge([_hit(0)], [], limit=10)
    assert all(isinstance(h, SessionSearchHit) for h in merged)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_search_excludes_the_stale_tail_after_a_compaction(
    store: Store,
) -> None:
    """Neither arm serves rows a compaction-restart left beyond the live prefix.

    A shrunk part leaves its old tail rows (and their embeddings) on disk, inert;
    both search arms MUST bound by ``session_manifests.records`` so a query never
    surfaces a transcript that no longer exists. Seeds 3 records, embeds them,
    then shrinks the manifest to 1 -- idx 1 and 2 are now stale tail.
    """
    session_id = await _session(store)
    await _record(store, session_id, idx=0, text="live prefix deploy release")
    await _record(store, session_id, idx=1, text="stale tail deploy release")
    await _record(store, session_id, idx=2, text="stale tail advisory lock")
    await _seed(store)
    await _bound_part(store, session_id, records=1)  # The compaction shrink.

    semantic = await search_session_records(
        store.engine,
        query_vector=await _query_vector("stale tail deploy release"),
        query_text="",
        mapper=_MAPPER,
        model=_MODEL,
        dim=_DIM,
    )
    fts = await search_session_records(
        store.engine,
        query_vector=None,
        query_text="advisory lock",
        mapper=_MAPPER,
        model=_MODEL,
        dim=_DIM,
    )

    live = {(session_id, 0)}
    assert {(h.session_id, h.idx) for h in semantic} <= live
    # The fts term appears ONLY in the stale idx-2 row -> excluded entirely.
    assert all((h.session_id, h.idx) != (session_id, 2) for h in fts)


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
