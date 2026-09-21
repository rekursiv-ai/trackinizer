"""The session-embedding sweep against real Postgres (pglite).

Every test runs the sweep over real ``session_records`` rows: the predicate is
``md5(text) IS DISTINCT FROM text_md5``, which is the database's, and the
halfvec(1024) upsert only exists against a live pgvector. StubEmbedder(dim=1024)
matches the ``session_embeddings`` column; the Store keeps its own 384-dim
embedder for ``inquiry_embeddings``, untouched here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.semantic_mapper_footprint import (
    CHUNK_CHARS,
    FootprintMapper,
)
from trackinizer.server.store.core import Store
from trackinizer.server.store.session_embed import sweep_session_embeddings


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trackinizer.lib.postgres import PostgresEngine


@pytest_asyncio.fixture(loop_scope="session")
async def store(integ_engine: PostgresEngine) -> AsyncIterator[Store]:
    """Return a bootstrapped store with the session tables emptied.

    The sweep is GLOBAL (it scans every session), so a prior test's records
    would otherwise land in this test's batch counts and stats. Emptying the
    record + embedding + inquiry tables per test isolates each sweep to the
    rows the test itself inserts.
    """
    built = Store(integ_engine, embed=StubEmbedder())
    await built.bootstrap()
    async with built.engine.acquire() as conn:
        await conn.execute(
            "TRUNCATE session_embeddings, session_index_state, session_records, "
            "session_manifests, inquiries CASCADE",
        )
    yield built


async def _session(store: Store) -> UUID:
    """Return an AgentSession row the records can hang off."""
    session_id = uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "'tester@example.com', 'embed test')",
            session_id,
        )
    return session_id


# The sweep now reads only the live manifest prefix (``idx < records``), so a record
# needs a manifest that covers it -- production never writes one without the other. Grow
# this part's bound to include ``idx``.
async def _record(
    store: Store,
    session_id: UUID,
    *,
    idx: int,
    kind: str,
    text: str,
    part: int = 0,
) -> None:
    """Insert one raw ``session_records`` row plus the manifest that bounds it."""
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO session_records "
            "(session_id, part, idx, kind, payload, text) "
            "VALUES ($1, $2, $3, $4, '{}'::json, $5)",
            session_id,
            part,
            idx,
            kind,
            text,
        )
        await conn.execute(
            "INSERT INTO session_manifests "
            "(session_id, part, name, metadata, ir_id, format, records) "
            "VALUES ($1, $2, $3, '{}'::json, gen_random_uuid(), 'claude', $4) "
            "ON CONFLICT (session_id, part) DO UPDATE SET "
            "records = GREATEST(session_manifests.records, $4)",
            session_id,
            part,
            f"s-{part}.jsonl",
            idx + 1,
        )


async def _embed_rows(
    store: Store,
    session_id: UUID,
) -> list[tuple[int, int, str, int, str, str]]:
    """Return ``(part, idx, field, chunk, mapper, text_md5)`` rows, ordered."""
    async with store.engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT part, idx, field, chunk, mapper, model, text_md5, "
            "embedding IS NOT NULL AS has_vec "
            "FROM session_embeddings WHERE session_id = $1 "
            "ORDER BY part, idx, field, chunk",
            session_id,
        )
    result: list[tuple[int, int, str, int, str, str]] = []
    for row in rows:
        assert row["has_vec"]
        part = row["part"]
        idx = row["idx"]
        field = row["field"]
        chunk = row["chunk"]
        mapper = row["mapper"]
        text_md5 = row["text_md5"]
        assert isinstance(part, int)
        assert isinstance(idx, int)
        assert isinstance(field, str)
        assert isinstance(chunk, int)
        assert isinstance(mapper, str)
        assert isinstance(text_md5, str)
        result.append((part, idx, field, chunk, mapper, text_md5))
    return result


async def _marker_rows(
    store: Store,
    session_id: UUID,
) -> list[tuple[int, int, str, str, str]]:
    """Return ``(part, idx, mapper, model, text_md5)`` marker rows, ordered."""
    async with store.engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT part, idx, mapper, model, text_md5 FROM session_index_state "
            "WHERE session_id = $1 ORDER BY part, idx, mapper, model",
            session_id,
        )
    result: list[tuple[int, int, str, str, str]] = []
    for row in rows:
        part = row["part"]
        idx = row["idx"]
        mapper = row["mapper"]
        model = row["model"]
        text_md5 = row["text_md5"]
        assert isinstance(part, int)
        assert isinstance(idx, int)
        assert isinstance(mapper, str)
        assert isinstance(model, str)
        assert isinstance(text_md5, str)
        result.append((part, idx, mapper, model, text_md5))
    return result


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_fts_only_record_marks_fresh_without_a_vector(store: Store) -> None:
    """A SystemMessage (fts-only) writes a marker row but no embedding row.

    The whole point of the marker table: freshness is decoupled from the
    presence of a vector, so an fts-only record is swept once and then reads
    up-to-date instead of re-sweeping forever.
    """
    session_id = await _session(store)
    await _record(store, session_id, idx=0, kind="SystemMessage", text="you are an ai")

    stats = await sweep_session_embeddings(
        store.engine,
        mapper=FootprintMapper(),
        embedder=StubEmbedder(dim=1024),
    )

    assert stats.records_embedded == 1
    assert stats.units_written == 0
    assert await _embed_rows(store, session_id) == []
    markers = await _marker_rows(store, session_id)
    assert [(idx, mapper) for _p, idx, mapper, _m, _h in markers] == [
        (0, "footprint-v1"),
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_fts_only_record_rerun_is_a_no_op(store: Store) -> None:
    """A second sweep over an fts-only record re-embeds nothing (marker matched)."""
    session_id = await _session(store)
    await _record(store, session_id, idx=0, kind="Thinking", text="maybe the cache")

    first = await sweep_session_embeddings(
        store.engine,
        mapper=FootprintMapper(),
        embedder=StubEmbedder(dim=1024),
    )
    second = await sweep_session_embeddings(
        store.engine,
        mapper=FootprintMapper(),
        embedder=StubEmbedder(dim=1024),
    )

    assert first.records_embedded == 1
    assert second.records_embedded == 0
    assert second.records_skipped == 1
    assert await _embed_rows(store, session_id) == []
    assert len(await _marker_rows(store, session_id)) == 1


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_headed_record_marks_fresh_without_a_vector(store: Store) -> None:
    """A machine-output head is fts-only now: marker row, no embedding row."""
    session_id = await _session(store)
    await _record(
        store,
        session_id,
        idx=0,
        kind="ShellCommandResult",
        text="Script completed",
    )

    stats = await sweep_session_embeddings(
        store.engine,
        mapper=FootprintMapper(),
        embedder=StubEmbedder(dim=1024),
    )

    assert stats.records_embedded == 1
    assert stats.units_written == 0
    assert await _embed_rows(store, session_id) == []
    assert len(await _marker_rows(store, session_id)) == 1


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_embedded_record_writes_both_a_vector_and_a_marker(store: Store) -> None:
    """An F+E record writes its vector AND a marker keyed on the same md5."""
    session_id = await _session(store)
    await _record(store, session_id, idx=0, kind="UserMessage", text="deploy it")

    await sweep_session_embeddings(
        store.engine,
        mapper=FootprintMapper(),
        embedder=StubEmbedder(dim=1024),
    )

    embed = await _embed_rows(store, session_id)
    markers = await _marker_rows(store, session_id)
    assert len(embed) == 1
    assert len(markers) == 1
    # The vector row and the marker carry the same md5 for the same record.
    assert embed[0][5] == markers[0][4]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_editing_fts_only_text_re_marks_that_row(store: Store) -> None:
    """A rewritten fts-only record's md5 diverges, so its marker is refreshed."""
    session_id = await _session(store)
    await _record(store, session_id, idx=0, kind="SystemMessage", text="prompt one")
    await sweep_session_embeddings(
        store.engine,
        mapper=FootprintMapper(),
        embedder=StubEmbedder(dim=1024),
    )
    before = await _marker_rows(store, session_id)

    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE session_records SET text = 'prompt two' "
            "WHERE session_id = $1 AND idx = 0",
            session_id,
        )
    stats = await sweep_session_embeddings(
        store.engine,
        mapper=FootprintMapper(),
        embedder=StubEmbedder(dim=1024),
    )

    after = await _marker_rows(store, session_id)
    assert stats.records_embedded == 1
    assert len(after) == 1
    assert after[0][4] != before[0][4]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_indexed_kinds_produce_embedding_rows(store: Store) -> None:
    """An F+E prose record lands one embedded content unit.

    A headed record (machine output) is fts-only under the footprint policy:
    it is swept (marker written) but embeds no vector, so only the prose row
    lands in ``session_embeddings``.
    """
    session_id = await _session(store)
    await _record(store, session_id, idx=0, kind="UserMessage", text="deploy it")
    await _record(
        store,
        session_id,
        idx=1,
        kind="UncategorizedToolResult",
        text="pg_advisory_lock acquired",
    )

    stats = await sweep_session_embeddings(
        store.engine,
        mapper=FootprintMapper(),
        embedder=StubEmbedder(dim=1024),
    )

    # Both records swept; only the prose one has a vector.
    assert stats.records_embedded == 2
    assert stats.units_written == 1
    rows = await _embed_rows(store, session_id)
    fields = [(idx, field, chunk) for _p, idx, field, chunk, _m, _h in rows]
    assert fields == [(0, "content", 0)]
    assert all(mapper == "footprint-v1" for *_x, mapper, _h in rows)
    # Both records carry a freshness marker, embedded or not.
    markers = await _marker_rows(store, session_id)
    assert {idx for _p, idx, *_rest in markers} == {0, 1}


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_non_indexed_kinds_produce_nothing(store: Store) -> None:
    """A SILENT kind (FileRead, telemetry) yields no rows -- mapper gate holds."""
    session_id = await _session(store)
    await _record(store, session_id, idx=0, kind="FileReadResult", text="file body")
    await _record(store, session_id, idx=1, kind="TokenUsage", text="tokens")

    stats = await sweep_session_embeddings(
        store.engine,
        mapper=FootprintMapper(),
        embedder=StubEmbedder(dim=1024),
    )

    assert stats.units_written == 0
    assert await _embed_rows(store, session_id) == []


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_rerun_is_a_no_op(store: Store) -> None:
    """The text_md5 predicate makes a second pass write nothing."""
    session_id = await _session(store)
    await _record(store, session_id, idx=0, kind="AssistantMessage", text="on it")

    first = await sweep_session_embeddings(
        store.engine,
        mapper=FootprintMapper(),
        embedder=StubEmbedder(dim=1024),
    )
    second = await sweep_session_embeddings(
        store.engine,
        mapper=FootprintMapper(),
        embedder=StubEmbedder(dim=1024),
    )

    assert first.records_embedded == 1
    assert second.records_embedded == 0
    assert second.units_written == 0
    assert len(await _embed_rows(store, session_id)) == 1


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_editing_text_re_embeds_exactly_that_row(store: Store) -> None:
    """A rewritten record's text_md5 diverges, so only it re-embeds."""
    session_id = await _session(store)
    await _record(store, session_id, idx=0, kind="UserMessage", text="original")
    await _record(store, session_id, idx=1, kind="UserMessage", text="untouched")
    await sweep_session_embeddings(
        store.engine,
        mapper=FootprintMapper(),
        embedder=StubEmbedder(dim=1024),
    )
    before = {
        (idx, md5) for _p, idx, _f, _c, _m, md5 in await _embed_rows(store, session_id)
    }

    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE session_records SET text = 'rewritten' "
            "WHERE session_id = $1 AND idx = 0",
            session_id,
        )
    stats = await sweep_session_embeddings(
        store.engine,
        mapper=FootprintMapper(),
        embedder=StubEmbedder(dim=1024),
    )

    assert stats.records_embedded == 1
    after = {
        (idx, md5) for _p, idx, _f, _c, _m, md5 in await _embed_rows(store, session_id)
    }
    changed = after - before
    unchanged = after & before
    assert {idx for idx, _md5 in changed} == {0}
    assert {idx for idx, _md5 in unchanged} == {1}


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_long_prose_produces_chunk_rows(store: Store) -> None:
    """Text over the chunk size fans out into overlapping chunk units."""
    session_id = await _session(store)
    long_text = "".join(f"word{n:05d} " for n in range(1000))  # >> CHUNK_CHARS.
    assert len(long_text) > CHUNK_CHARS
    await _record(store, session_id, idx=0, kind="AssistantMessage", text=long_text)

    stats = await sweep_session_embeddings(
        store.engine,
        mapper=FootprintMapper(),
        embedder=StubEmbedder(dim=1024),
    )

    rows = await _embed_rows(store, session_id)
    chunks = sorted(chunk for _p, _i, _f, chunk, _m, _h in rows)
    assert chunks == list(range(len(rows)))
    assert len(rows) > 1
    assert stats.units_written == len(rows)


class _BatchStub(StubEmbedder):
    """A 1024-dim stub that also offers ``embed_batch``, recording batch sizes.

    Extends the real StubEmbedder so its vectors are still valid halfvec(1024)
    input; the sweep must prefer ``embed_batch`` and pass a record's units in
    one call.
    """

    def __init__(self) -> None:
        super().__init__(dim=1024)
        self.batch_sizes: list[int] = []

    @property
    @override
    def name(self) -> str:
        return "stub-batch"

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.batch_sizes.append(len(texts))
        return [await self.embed(text) for text in texts]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_batch_path_embeds_a_records_units_in_one_call(store: Store) -> None:
    """When the embedder has ``embed_batch``, a record's units go in one call.

    A long AssistantMessage fans into several chunk units; the sweep must batch
    them (one ``embed_batch`` call carrying every unit), and the stored rows
    must match the per-call fallback exactly.
    """
    session_id = await _session(store)
    long_text = "".join(f"word{n:05d} " for n in range(1000))
    assert len(long_text) > CHUNK_CHARS
    await _record(store, session_id, idx=0, kind="AssistantMessage", text=long_text)
    embedder = _BatchStub()

    stats = await sweep_session_embeddings(
        store.engine,
        mapper=FootprintMapper(),
        embedder=embedder,
    )

    rows = await _embed_rows(store, session_id)
    # One batch call, carrying every chunk unit of the single record.
    assert embedder.batch_sizes == [len(rows)]
    assert len(rows) > 1
    assert stats.units_written == len(rows)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_batch_and_scalar_paths_agree(store: Store) -> None:
    """The batch path stores the same vectors the per-unit path would.

    Same deterministic stub math either way, so a byte-identical embedding
    per (idx, chunk) proves the batch wiring did not reorder or mismatch units.
    """
    session_id = await _session(store)
    text = "".join(f"tok{n:04d} " for n in range(1000))
    await _record(store, session_id, idx=0, kind="AssistantMessage", text=text)

    await sweep_session_embeddings(
        store.engine,
        mapper=FootprintMapper(),
        embedder=_BatchStub(),
    )
    async with store.engine.acquire() as conn:
        batched = await conn.fetch(
            "SELECT chunk, embedding::text AS e FROM session_embeddings "
            "WHERE session_id = $1 AND model = 'stub-batch' ORDER BY chunk",
            session_id,
        )
    # Re-embed the same record via the scalar-only StubEmbedder (different
    # model name, so rows coexist) and compare vectors chunk-for-chunk.
    await sweep_session_embeddings(
        store.engine,
        mapper=FootprintMapper(),
        embedder=StubEmbedder(dim=1024),
    )
    async with store.engine.acquire() as conn:
        scalar = await conn.fetch(
            "SELECT chunk, embedding::text AS e FROM session_embeddings "
            "WHERE session_id = $1 AND model = 'stub-1024' ORDER BY chunk",
            session_id,
        )
    assert [r["e"] for r in batched] == [r["e"] for r in scalar]
    assert len(batched) > 1


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_sweep_skips_the_stale_tail_after_a_compaction(store: Store) -> None:
    """The sweep never embeds rows a compaction-restart left beyond the prefix.

    A shrunk part's old tail rows stay on disk; the sweep's ``_page`` scan bounds
    by ``session_manifests.records`` so they are neither embedded nor counted.
    Seeds 3 records, shrinks the manifest to 1, and asserts only idx 0 embeds.
    """
    session_id = await _session(store)
    await _record(store, session_id, idx=0, kind="UserMessage", text="live deploy")
    await _record(store, session_id, idx=1, kind="UserMessage", text="stale one")
    await _record(store, session_id, idx=2, kind="UserMessage", text="stale two")
    # The compaction: rewritten shorter, so only idx 0 remains live.
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE session_manifests SET records = 1 WHERE session_id = $1",
            session_id,
        )

    stats = await sweep_session_embeddings(
        store.engine,
        mapper=FootprintMapper(),
        embedder=StubEmbedder(dim=1024),
    )

    embedded = await _embed_rows(store, session_id)
    assert {idx for _part, idx, *_rest in embedded} == {0}
    assert stats.records_embedded == 1


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
