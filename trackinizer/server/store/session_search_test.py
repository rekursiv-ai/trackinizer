"""The session search read path against real Postgres (pglite).

Seeds ``session_embeddings`` + the scoped tsvector exactly as production does
-- through ``sweep_session_embeddings`` with ``StubEmbedder(dim=1024)`` and
``FootprintMapper`` -- then exercises both arms and their RRF merge. The
properties are the database's: HNSW cosine ordering, ``websearch_to_tsquery``
matching, and the fused rank, none observable without the live index.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Final, Literal, cast
from uuid import UUID, uuid4

import pytest

from trackinizer.conftest import FakeEngine, executed_sql, make_conn
from trackinizer.server.audience import NOBODY, SessionReader, visible_sql
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.semantic_mapper_footprint import FootprintMapper
from trackinizer.server.store.session_embed import sweep_session_embeddings
from trackinizer.server.store.session_feed import FeedScope
from trackinizer.server.store.session_search import (
    RRF_K,
    SessionSearchHit,
    _ArmHit,
    _merge,
    _semantic_arm,
    _semantic_scan,
    search_session_records,
)
from trackinizer.server.values import vetted_sql
from trackinizer.wire.audience import AUDIENCES


if TYPE_CHECKING:
    from unittest.mock import AsyncMock

    from trackinizer.lib.postgres import Conn, DatabaseEngine
    from trackinizer.server.store.core import Store


# Reads every open session; the sessions these tests seed are all open.
_ANYONE: Final = FeedScope(
    reader=SessionReader(audiences=frozenset({"open"}), every_session=True),
)
_MAPPER = FootprintMapper().name
_MODEL = StubEmbedder(dim=1024).name
_DIM = StubEmbedder(dim=1024).dim


@pytest.fixture
def store(integ_store: Store) -> Store:
    """Return the store on the shared Postgres database, freshly bootstrapped."""
    return integ_store


async def _session(store: Store) -> UUID:
    session_id = uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title, "
            "agentsession_audience) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "'tester@example.com', 'search test', 'open')",
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
        scope=_ANYONE,
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
        scope=_ANYONE,
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
        scope=_ANYONE,
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
        scope=_ANYONE,
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
        scope=_ANYONE,
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


def test_merge_hands_on_every_field_of_the_unit_it_keeps() -> None:
    """The console opens a session at ``(session_id, part, idx)`` and labels the hit."""
    unit = _ArmHit(
        session_id=UUID(int=7),
        part=3,
        idx=5,
        field="head",
        chunk=2,
        snippet="the head of it",
        title="a session",
    )

    [hit] = _merge([unit], [], limit=10)

    assert (hit.session_id, hit.part, hit.idx) == (UUID(int=7), 3, 5)
    assert (hit.field, hit.chunk) == ("head", 2)
    assert (hit.snippet, hit.title) == ("the head of it", "a session")


def test_merge_names_the_arm_each_hit_came_from() -> None:
    semantic, full_text = _hit(1), _hit(2)

    by_idx = {h.idx: h for h in _merge([semantic], [full_text], limit=10)}

    assert by_idx[1].source == "semantic"
    assert by_idx[2].source == "fts"


def test_merge_counts_a_repeated_position_once_and_reads_on_past_it() -> None:
    """A later record of the same arm still ranks, one place lower for the repeat."""
    first, repeat, later = _hit(1, chunk=0), _hit(1, chunk=1), _hit(2)

    merged = _merge([first, repeat, later], [], limit=10)

    assert [h.idx for h in merged] == [1, 2]
    assert merged[1].score == pytest.approx(1.0 / (RRF_K + 3))


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
        scope=_ANYONE,
    )
    fts = await search_session_records(
        store.engine,
        query_vector=None,
        query_text="advisory lock",
        mapper=_MAPPER,
        model=_MODEL,
        dim=_DIM,
        scope=_ANYONE,
    )

    live = {(session_id, 0)}
    assert {(h.session_id, h.idx) for h in semantic} <= live
    # The fts term appears ONLY in the stale idx-2 row -> excluded entirely.
    assert all((h.session_id, h.idx) != (session_id, 2) for h in fts)


# The semantic arm's one statement for ``dim=2`` and its reader as ``$6`` to ``$13``, as
# it ran before a reader who may not read every session got a scan of its own.
_PLAIN_SQL: Final = vetted_sql(
    "SELECT e.session_id, e.part, e.idx, e.field, e.chunk, "
    "left(r.text, $5) AS snippet, i.title AS title "
    "FROM session_embeddings e "
    "JOIN session_records r "
    "  ON r.session_id = e.session_id AND r.part = e.part AND r.idx = e.idx "
    "JOIN inquiries i ON i.id = e.session_id "
    "JOIN session_manifests m ON m.session_id = r.session_id AND m.part = r.part "
    "WHERE e.mapper = $2 AND e.model = $3 AND r.idx < m.records AND ",
    visible_sql("i", params=[None, None, None, None, None], reader=NOBODY),
    " ORDER BY (e.embedding::halfvec(2)) <=> $1::halfvec(2) LIMIT $4",
)
_OPEN: Final = frozenset({"open"})
_OPEN_READER: Final = SessionReader(audiences=_OPEN)
_EVERYTHING: Final = SessionReader(
    audiences=AUDIENCES,
    every_session=True,
    every_chat=True,
    oversight=True,
)
_READER_VALUES: Final = ("", None, False, False, False, True, "")
"""The values the open reader binds after its audiences: email, org, every session,
every chat, oversight, hidden, assistant."""
_SET_WIDTH: Final = "SELECT set_config('hnsw.ef_search', $1, true)"
_ITERATE: Final = "SET LOCAL hnsw.iterative_scan = strict_order"
_VERSION: Final = "SELECT extversion FROM pg_extension WHERE extname = 'vector'"


def _engine(conn: AsyncMock) -> DatabaseEngine:
    """Return an engine over ``conn``, which the scan choice tells apart by identity."""
    return cast("DatabaseEngine", FakeEngine(conn))


def _unit_rows(count: int) -> list[dict[str, object]]:
    """Return ``count`` rows shaped as the semantic query answers."""
    return [
        {
            "session_id": UUID(int=1),
            "part": 0,
            "idx": idx,
            "field": "content",
            "chunk": 0,
            "snippet": "s",
            "title": "t",
        }
        for idx in range(count)
    ]


async def _arm(
    conn: AsyncMock,
    *,
    scan: Literal["plain", "iterative", "widening"],
    reader: SessionReader = _OPEN_READER,
    limit: int = 3,
) -> list[_ArmHit]:
    """Run the semantic arm on a scripted connection, scanned as ``scan`` says."""
    return await _semantic_arm(
        cast("Conn", conn),
        [0.5, 0.25],
        mapper="mapper-x",
        model="model-y",
        dim=2,
        limit=limit,
        scope=FeedScope(reader=reader),
        scan=scan,
    )


@pytest.mark.asyncio
async def test_every_audience_reader_runs_the_one_statement_it_always_did() -> None:
    conn = make_conn()
    conn.fetch.return_value = _unit_rows(2)

    hits = await _arm(conn, scan="plain", reader=_EVERYTHING, limit=7)

    assert executed_sql(conn) == [_PLAIN_SQL]
    assert conn.fetch.await_args is not None
    assert conn.fetch.await_args.args[1:] == (
        "[0.5,0.25]",
        "mapper-x",
        "model-y",
        7,
        200,
        ["open", "private", "staff"],
        "",
        None,
        True,
        True,
        True,
        True,
        "",
    )
    assert [hit.idx for hit in hits] == [0, 1]


@pytest.mark.asyncio
async def test_the_iterative_scan_is_set_for_one_transaction() -> None:
    """The statement is the plain one; only the transaction around it differs."""
    conn = make_conn()
    conn.fetch.return_value = _unit_rows(3)

    hits = await _arm(conn, scan="iterative")

    assert executed_sql(conn) == ["BEGIN", _ITERATE, _PLAIN_SQL, "COMMIT"]
    assert conn.fetch.await_args is not None
    assert conn.fetch.await_args.args[1:] == (
        "[0.5,0.25]",
        "mapper-x",
        "model-y",
        3,
        200,
        ["open"],
        *_READER_VALUES,
    )
    assert [hit.idx for hit in hits] == [0, 1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("offered", "counts", "widths"),
    [
        pytest.param("40", [3], [], id="the first query fills the limit"),
        pytest.param("40", [5], [], id="the first query passes the limit"),
        pytest.param("40", [0, 3], ["160"], id="one step fills it"),
        pytest.param("40", [0, 1, 3], ["160", "640"], id="two steps fill it"),
        pytest.param("40", [0, 1, 2, 3], ["160", "640", "1000"], id="the cap fills it"),
        pytest.param("40", [0, 1, 2, 2], ["160", "640", "1000"], id="the cap ends it"),
        pytest.param("300", [1, 2], ["1000"], id="a step stops at the cap"),
        pytest.param("1000", [1], [], id="the index already offers the cap"),
    ],
)
async def test_widening_asks_for_more_candidates_until_the_limit_is_filled_or_the_cap(
    offered: str,
    counts: list[int],
    widths: list[str],
) -> None:
    conn = make_conn()
    conn.fetch.side_effect = [_unit_rows(count) for count in counts]
    conn.fetchval.return_value = offered

    hits = await _arm(conn, scan="widening")

    assert executed_sql(conn) == [
        "BEGIN",
        _PLAIN_SQL,
        *[statement for _ in widths for statement in (_SET_WIDTH, _PLAIN_SQL)],
        "COMMIT",
    ]
    assert [
        call.args[1]
        for call in conn.execute.await_args_list
        if call.args[0] == _SET_WIDTH
    ] == widths
    # Every asking is the same query with the same values.
    assert [call.args for call in conn.fetch.await_args_list] == [
        (
            _PLAIN_SQL,
            "[0.5,0.25]",
            "mapper-x",
            "model-y",
            3,
            200,
            ["open"],
            *_READER_VALUES,
        ),
    ] * len(counts)
    # The offered width is read only once the first query has fallen short.
    if counts[0] < 3:
        conn.fetchval.assert_awaited_once_with("SHOW hnsw.ef_search")
    else:
        conn.fetchval.assert_not_awaited()
    assert len(hits) == counts[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [frozenset[str](), frozenset({"partner"})])
async def test_a_reader_of_every_session_is_scanned_plain_without_asking_the_database(
    extra: frozenset[str],
) -> None:
    conn = make_conn()
    reader = SessionReader(
        audiences=AUDIENCES | extra,
        every_session=True,
        every_chat=True,
        oversight=True,
    )

    scan = await _semantic_scan(
        _engine(conn),
        cast("Conn", conn),
        FeedScope(reader=reader),
    )

    assert scan == "plain"
    conn.fetchval.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope",
    [
        FeedScope(reader=replace(_EVERYTHING, audiences=_OPEN)),
        FeedScope(reader=replace(_EVERYTHING, every_session=False)),
        FeedScope(reader=replace(_EVERYTHING, every_chat=False)),
        FeedScope(reader=replace(_EVERYTHING, oversight=False)),
        FeedScope(reader=replace(_EVERYTHING, hidden=False)),
        # It reads every session, but keeps only the caller's own.
        FeedScope(reader=_EVERYTHING, account="ann@lab.example"),
    ],
    ids=["open", "org", "no chats", "no oversight", "listing", "mine"],
)
@pytest.mark.parametrize(
    ("version", "scan"),
    [
        ("0.8.0", "iterative"),
        ("0.8.1", "iterative"),
        ("0.10.2", "iterative"),
        ("1.0.0", "iterative"),
        ("0.7.4", "widening"),
        ("0.5.1", "widening"),
        (None, "widening"),
        ("unreleased", "widening"),
    ],
)
async def test_a_reader_of_fewer_sessions_is_scanned_as_the_installed_pgvector_allows(
    scope: FeedScope,
    version: str | None,
    scan: str,
) -> None:
    conn = make_conn()
    conn.fetchval.return_value = version

    chosen = await _semantic_scan(_engine(conn), cast("Conn", conn), scope)

    assert chosen == scan
    conn.fetchval.assert_awaited_once_with(_VERSION)


@pytest.mark.asyncio
async def test_the_installed_version_is_read_once_for_each_engine() -> None:
    current, older = make_conn(), make_conn()
    current.fetchval.return_value = "0.8.1"
    older.fetchval.return_value = "0.7.4"
    asked = [(_engine(current), current), (_engine(older), older)]

    scans = [
        await _semantic_scan(engine, cast("Conn", conn), FeedScope(reader=_OPEN_READER))
        for engine, conn in (*asked, *asked)
    ]

    assert scans == ["iterative", "widening", "iterative", "widening"]
    assert (current.fetchval.await_count, older.fetchval.await_count) == (1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reader", "statements"),
    [
        pytest.param(_EVERYTHING, [_PLAIN_SQL], id="every session"),
        pytest.param(
            _OPEN_READER,
            ["BEGIN", _ITERATE, _PLAIN_SQL, "COMMIT"],
            id="lacking staff",
        ),
    ],
)
async def test_the_search_scans_each_reader_as_what_it_reads_calls_for(
    reader: SessionReader,
    statements: list[str],
) -> None:
    conn = make_conn()
    conn.fetchval.return_value = "0.8.1"
    conn.fetch.return_value = _unit_rows(3)

    hits = await search_session_records(
        _engine(conn),
        query_vector=[0.5, 0.25],
        query_text="",
        mapper="mapper-x",
        model="model-y",
        dim=2,
        limit=3,
        scope=FeedScope(reader=reader),
    )

    assert executed_sql(conn) == statements
    assert len(hits) == 3


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
