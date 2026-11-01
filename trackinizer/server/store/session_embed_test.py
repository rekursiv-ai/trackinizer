"""The session-embedding sweep against real Postgres (pglite).

Every test runs the sweep over real ``session_records`` rows: the freshness
predicate, the ``changed_xid`` trigger and the snapshot visibility the cursor
reads are the database's, and the halfvec(1024) upsert only exists against a live
pgvector. StubEmbedder(dim=1024) matches the ``session_embeddings`` column; the
Store keeps its own 384-dim embedder for ``inquiry_embeddings``, untouched here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast, override
from uuid import UUID, uuid4

import pytest

from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.semantic_mapper import IndexUnit
from trackinizer.server.semantic_mapper_footprint import (
    CHUNK_CHARS,
    FootprintMapper,
)
from trackinizer.server.store import session_embed
from trackinizer.server.store.session_embed import (
    Pool,
    SweepStats,
    embed_texts,
    pending_page_sql,
    save_cursor,
    sole_writer,
    sweep_session_embeddings,
    write_pool,
)
from trackinizer.server.values import manifest_bound


if TYPE_CHECKING:
    from asyncpg import Record

    from trackinizer.lib.postgres import Conn
    from trackinizer.server.store.core import Store


@pytest.fixture
def store(integ_store: Store) -> Store:
    """Return the store on the shared Postgres database, freshly bootstrapped.

    The sweep is GLOBAL (it scans every session), so a prior test's records
    would otherwise land in this test's batch counts and stats; ``integ_store``
    empties every table per test, so each sweep sees only the rows the test
    itself inserts, and with no cursor left each test's first sweep is a full
    one.
    """
    return integ_store


async def _session(store: Store) -> UUID:
    """Return an AgentSession row the records can hang off."""
    session_id = uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title, "
            "agentsession_audience) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "'tester@example.com', 'embed test', 'open')",
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
        self.received: list[list[str]] = []

    @property
    @override
    def name(self) -> str:
        return "stub-batch"

    @property
    def batch_sizes(self) -> list[int]:
        return [len(texts) for texts in self.received]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.received.append(texts)
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


async def _sweep(store: Store, embedder: StubEmbedder | None = None) -> SweepStats:
    """Run one sweep with the footprint mapper and a 1024-dim stub by default."""
    return await sweep_session_embeddings(
        store.engine,
        mapper=FootprintMapper(),
        embedder=embedder or StubEmbedder(dim=1024),
    )


async def _make_marker_stale(store: Store, session_id: UUID, *, idx: int) -> None:
    """Corrupt one record's marker: a sweep that reads the record re-embeds it."""
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE session_index_state SET text_md5 = 'stale' "
            "WHERE session_id = $1 AND idx = $2",
            session_id,
            idx,
        )


async def _marker_md5s(store: Store, session_id: UUID) -> dict[int, str]:
    """Return ``idx -> text_md5`` of the session's markers."""
    return {
        idx: md5
        for _part, idx, _mapper, _model, md5 in await _marker_rows(store, session_id)
    }


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_second_sweep_reads_only_records_changed_since_the_first(
    store: Store,
) -> None:
    """After one full pass, a sweep reads the changes, never the whole table.

    A stale marker on an UNCHANGED record is the probe: a rescan of every record
    would find it and re-embed it; a sweep that reads only changes never looks.
    """
    session_id = await _session(store)
    await _record(store, session_id, idx=0, kind="UserMessage", text="old line")
    first = await _sweep(store)
    await _make_marker_stale(store, session_id, idx=0)
    await _record(store, session_id, idx=1, kind="UserMessage", text="new line")

    second = await _sweep(store)

    assert (first.full_scan, first.records_embedded) == (True, 1)
    assert (second.full_scan, second.records_embedded) == (False, 1)
    assert (await _marker_md5s(store, session_id))[0] == "stale"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_rewriting_a_record_with_the_same_text_is_not_a_change(
    store: Store,
) -> None:
    """A compaction replay re-feeds identical text: no re-read, no re-embed."""
    session_id = await _session(store)
    await _record(store, session_id, idx=0, kind="UserMessage", text="same words")
    await _sweep(store)
    await _make_marker_stale(store, session_id, idx=0)
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE session_records SET text = text, payload = '{\"v\": 2}'::json "
            "WHERE session_id = $1",
            session_id,
        )

    stats = await _sweep(store)

    assert stats.records_embedded == 0
    assert (await _marker_md5s(store, session_id))[0] == "stale"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(("in_progress", "read"), [(True, 1), (False, 0)])
async def test_a_record_in_flight_at_the_cursor_is_read_once_it_commits(
    store: Store,
    *,
    in_progress: bool,
    read: int,
) -> None:
    """The cursor is a snapshot, so a late-committing writer is never skipped.

    The cursor is written by hand: once listing the record's transaction as in
    progress -- what a sweep records when it reads while a long ingest is still
    open -- and once as committed. Only the first leaves the record to read.
    """
    session_id = await _session(store)
    await _record(store, session_id, idx=0, kind="UserMessage", text="late commit")
    await _sweep(store)
    await _make_marker_stale(store, session_id, idx=0)
    async with store.engine.acquire() as conn:
        xid = int(
            str(
                await conn.fetchval(
                    "SELECT changed_xid::text FROM session_records WHERE session_id = $1",
                    session_id,
                ),
            ),
        )
    await save_cursor(
        store.engine,
        mapper=FootprintMapper().name,
        model=StubEmbedder(dim=1024).name,
        # Both put the xid inside the snapshot's [xmin, xmax) window, so only the
        # visibility check, not the xid range, tells them apart.
        snapshot=f"{xid}:{xid + 1}:{xid}" if in_progress else f"{xid}:{xid + 1}:",
    )

    stats = await _sweep(store)

    assert (stats.full_scan, stats.records_embedded) == (False, read)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_each_model_keeps_its_own_cursor(store: Store) -> None:
    """A model swept for the first time catches up, whatever another model did."""
    session_id = await _session(store)
    await _record(store, session_id, idx=0, kind="UserMessage", text="shared line")
    await _sweep(store)

    challenger = await _sweep(store, _BatchStub())

    assert (challenger.full_scan, challenger.records_embedded) == (True, 1)
    async with store.engine.acquire() as conn:
        cursors = await conn.fetch(
            "SELECT mapper, model FROM session_index_cursor ORDER BY model",
        )
    assert [(row["mapper"], row["model"]) for row in cursors] == [
        ("footprint-v1", "stub-1024"),
        ("footprint-v1", "stub-batch"),
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_pair_has_one_writer_until_it_lets_go(store: Store) -> None:
    """A second connection is refused the pair, not another model, until release."""
    async with store.engine.acquire() as first, store.engine.acquire() as second:
        async with sole_writer(first, mapper="m", model="a") as held:
            async with sole_writer(second, mapper="m", model="a") as refused:
                pass
            async with sole_writer(second, mapper="m", model="b") as other:
                pass
        async with sole_writer(second, mapper="m", model="a") as after:
            pass

    assert (held, refused, other, after) == (True, False, True, True)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_sweep_writes_nothing_while_another_process_writes_the_pair(
    store: Store,
) -> None:
    """A sweep that finds the pair held leaves records, markers and cursor alone."""
    session_id = await _session(store)
    await _record(store, session_id, idx=0, kind="UserMessage", text="held line")
    async with (
        store.engine.acquire() as other,
        sole_writer(
            other,
            mapper=FootprintMapper().name,
            model=StubEmbedder(dim=1024).name,
        ),
    ):
        refused = await _sweep(store)
        markers = await _marker_rows(store, session_id)
        async with store.engine.acquire() as conn:
            cursors = await conn.fetchval("SELECT count(*) FROM session_index_cursor")

    after = await _sweep(store)

    assert refused == SweepStats(busy=True)
    assert (markers, cursors) == ([], 0)
    assert (after.busy, after.records_embedded) == (False, 1)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_one_page_embeds_every_records_units_in_one_batch(store: Store) -> None:
    """Units pool across records, so a page is one batch call, not one per record.

    The stored vectors must still land on their own records: each one equals what
    the one-at-a-time stub path stores for the same record.
    """
    session_id = await _session(store)
    texts = ["a much longer user message", "short", "mid length"]
    for idx, text in enumerate(texts):
        await _record(store, session_id, idx=idx, kind="UserMessage", text=text)
    batch = _BatchStub()

    await _sweep(store, batch)
    await _sweep(store)

    assert batch.received == [sorted(texts, key=len)]
    async with store.engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT model, idx, embedding::text AS e FROM session_embeddings "
            "WHERE session_id = $1 ORDER BY idx",
            session_id,
        )
    by_model: dict[str, list[str]] = {}
    for row in rows:
        by_model.setdefault(str(row["model"]), []).append(str(row["e"]))
    assert by_model["stub-batch"] == by_model["stub-1024"]
    assert len(by_model["stub-batch"]) == len(texts)


class _CountingBatch(StubEmbedder):
    """A batch embedder that counts its calls and answers one length vector a text."""

    calls = 0

    @override
    async def embed(self, text: str) -> list[float]:
        raise AssertionError(f"the batch path embeds {text!r} one at a time")

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[float(len(text))] for text in texts]


@pytest.mark.asyncio
async def test_no_units_never_reach_a_batch_embedder() -> None:
    embedder = _CountingBatch(dim=1)

    assert await embed_texts(embedder, []) == []
    assert embedder.calls == 0


@pytest.mark.asyncio
async def test_a_batch_embedder_gets_all_the_units_in_one_call() -> None:
    embedder = _CountingBatch(dim=1)

    assert await embed_texts(embedder, ["a", "bb"]) == [[1.0], [2.0]]
    assert embedder.calls == 1


@pytest.mark.asyncio
async def test_a_batch_embedder_sees_texts_shortest_first_in_input_order_back() -> None:
    """Sorting by length keeps a short text off a long one's padding; order returns."""
    embedder = _BatchStub()
    texts = ["three", "x", "fourteen chars", "ab"]

    vectors = await embed_texts(embedder, texts)

    assert embedder.received == [["x", "ab", "three", "fourteen chars"]]
    assert vectors == [await StubEmbedder(dim=1024).embed(text) for text in texts]


@pytest.mark.asyncio
async def test_a_scalar_embedder_is_called_once_per_unit_in_order() -> None:
    embedder = StubEmbedder(dim=4)

    vectors = await embed_texts(embedder, ["a", "b"])

    assert vectors == [await embedder.embed("a"), await embedder.embed("b")]
    assert vectors[0] != vectors[1]


def test_pending_page_sql_returns_exact_first_and_keyset_queries() -> None:
    join, predicate = manifest_bound("r")
    columns = "r.created, r.session_id, r.part, r.idx"
    first = pending_page_sql(join, predicate, first=True, columns=columns)
    later = pending_page_sql(join, predicate, first=False, columns=columns)

    expected_prefix = (
        "SELECT r.created, r.session_id, r.part, r.idx FROM session_records r "
        "JOIN session_manifests m ON m.session_id = r.session_id AND m.part = r.part "
        "WHERE "
    )
    expected_suffix = (
        "r.idx < m.records AND r.kind = ANY($3::text[]) AND r.text <> '' "
        "AND NOT EXISTS (SELECT 1 FROM session_index_state s "
        "WHERE s.session_id = r.session_id AND s.part = r.part "
        "AND s.idx = r.idx AND s.mapper = $1 AND s.model = $2 "
        "AND s.text_md5 = md5(r.text)) "
        "ORDER BY r.created, r.session_id, r.part, r.idx LIMIT "
    )
    assert first == expected_prefix + expected_suffix + "$4"
    assert later == (
        expected_prefix
        + "(r.created, r.session_id, r.part, r.idx) > ($4, $5, $6, $7) AND "
        + expected_suffix
        + "$8"
    )


class _ExecuteConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def execute(self, query: str, *args: object) -> None:
        self.calls.append((query, args))


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb


class _NamedEmbedder(StubEmbedder):
    @property
    @override
    def name(self) -> str:
        return "length-fake"


@pytest.mark.asyncio
async def test_write_pool_executes_delete_vectors_and_freshness_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _ExecuteConnection()
    transaction_connections: list[object] = []

    def fake_tx(connection: object) -> _Transaction:
        transaction_connections.append(connection)
        return _Transaction()

    monkeypatch.setattr(session_embed, "tx", fake_tx)
    session_id = uuid4()
    row = cast("Record", {"session_id": session_id, "part": 2, "idx": 3})
    pool: Pool = [(row, (IndexUnit(text="hello", field="content", chunk=1),), "digest")]
    embedder = _NamedEmbedder(dim=2)

    result = await write_pool(
        cast("Conn", conn),
        pool,
        [[1.25, -2.5]],
        embedder=embedder,
        mapper_name="mapper",
    )

    assert result == 1
    assert transaction_connections == [conn]
    assert len(conn.calls) == 3
    assert conn.calls[0][1] == ([session_id], [2], [3], "mapper", "length-fake")
    assert conn.calls[1][1] == (
        [session_id],
        [2],
        [3],
        ["content"],
        [1],
        ["[1.25,-2.5]"],
        ["digest"],
        "mapper",
        "length-fake",
    )
    assert conn.calls[2][1] == (
        [session_id],
        [2],
        [3],
        "mapper",
        "length-fake",
        ["digest"],
    )
    assert conn.calls[0][0] == (
        "DELETE FROM session_embeddings e USING unnest($1::uuid[], "
        "$2::int[], $3::int[]) AS t(session_id, part, idx) "
        "WHERE e.session_id = t.session_id AND e.part = t.part "
        "AND e.idx = t.idx AND e.mapper = $4 AND e.model = $5"
    )
    assert conn.calls[1][0] == (
        "INSERT INTO session_embeddings "
        "(session_id, part, idx, field, chunk, mapper, model, embedding, "
        "text_md5) "
        "SELECT session_id, part, idx, field, chunk, $8, $9, "
        "embedding::halfvec, text_md5 FROM unnest($1::uuid[], $2::int[], "
        "$3::int[], $4::text[], $5::int[], $6::text[], $7::text[]) "
        "AS t(session_id, part, idx, field, chunk, embedding, text_md5)"
    )
    assert conn.calls[2][0] == (
        "INSERT INTO session_index_state "
        "(session_id, part, idx, mapper, model, text_md5) "
        "SELECT DISTINCT session_id, part, idx, $4, $5, text_md5 "
        "FROM unnest($1::uuid[], $2::int[], $3::int[], $6::text[]) "
        "AS t(session_id, part, idx, text_md5) "
        "ON CONFLICT (session_id, part, idx, mapper, model) "
        "DO UPDATE SET text_md5 = EXCLUDED.text_md5, created = now()"
    )


@pytest.mark.asyncio
async def test_write_pool_consumes_vectors_across_multiple_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _ExecuteConnection()

    def fake_tx(conn: object) -> _Transaction:
        del conn
        return _Transaction()

    monkeypatch.setattr(session_embed, "tx", fake_tx)
    row = cast("Record", {"session_id": uuid4(), "part": 0, "idx": 0})
    second_row = cast("Record", {"session_id": uuid4(), "part": 0, "idx": 1})
    third_row = cast("Record", {"session_id": uuid4(), "part": 0, "idx": 2})
    fts_only = cast("Record", {"session_id": uuid4(), "part": 0, "idx": 3})
    pool: Pool = [
        (
            row,
            (
                IndexUnit(text="a", field="content", chunk=0),
                IndexUnit(text="b", field="content", chunk=1),
            ),
            "digest",
        ),
        (second_row, (IndexUnit(text="c", field="content", chunk=0),), "digest-2"),
        (fts_only, (), "digest-fts"),
        (third_row, (IndexUnit(text="d", field="content", chunk=0),), "digest-3"),
    ]

    await write_pool(
        cast("Conn", conn),
        pool,
        [[1.0], [2.0], [3.0], [4.0]],
        embedder=_NamedEmbedder(dim=1),
        mapper_name="mapper",
    )

    assert conn.calls[1][1][3:7] == (
        ["content", "content", "content", "content"],
        [0, 1, 0, 0],
        ["[1.0]", "[2.0]", "[3.0]", "[4.0]"],
        ["digest", "digest", "digest-2", "digest-3"],
    )
    # The fts-only record gets no vector but is still deleted from and marked.
    assert conn.calls[0][1][2] == [0, 1, 3, 2]
    assert conn.calls[2][1][5] == ["digest", "digest-2", "digest-fts", "digest-3"]
    with pytest.raises(ValueError, match="zip\\(\\) argument"):
        await write_pool(
            cast("Conn", conn),
            pool,
            [[1.0]],
            embedder=_NamedEmbedder(dim=1),
            mapper_name="mapper",
        )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
