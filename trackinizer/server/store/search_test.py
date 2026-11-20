"""The session search read path against real Postgres (pglite).

Seeds session records and the scoped tsvector exactly as production does, and
fills the ``vector_search`` slot with a fake semantic arm that ranks the records
it is given, so what is pinned is core's part: both arms and their RRF merge, and
the reader's test core hands the semantic arm. The properties are the database's:
``websearch_to_tsquery`` matching and the fused rank.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, cast, override
from uuid import UUID, uuid4

import pytest

from trackinizer.conftest import FakeEngine, executed_sql, make_conn
from trackinizer.server.access_policy import (
    DefaultPolicy,
    Principal,
    inquiry_read_filter,
)
from trackinizer.server.audience import SessionReader, visible_sql
from trackinizer.server.notify import tx
from trackinizer.server.store.search import (
    _INQUIRY_KEYWORDS,
    _NOTE_KEYWORDS,
    INQUIRY_TEXT_COLUMNS,
    RRF_K,
    InquirySearchHit,
    SessionSearchHit,
    VectorHit,
    _ArmHit,
    _fts_arm,
    _InquiryReader,
    _merge,
    _semantic_arm,
    search_inquiries,
    search_sessions,
)
from trackinizer.server.store.session_feed import FeedScope
from trackinizer.server.values import vetted_sql
from trackinizer.wire.audience import AUDIENCES


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn, DatabaseEngine
    from trackinizer.server.access_policy import ResourceKind
    from trackinizer.server.store.core import Store
    from trackinizer.server.store.search import Kept
    from trackinizer.wire.wire_sessions import SearchMode


# Reads every open session; the sessions these tests seed are all open.
_ANYONE: Final = FeedScope(
    reader=SessionReader(audiences=frozenset({"open"}), every_session=True),
)
# No record holds it, so a search for it reaches no record by full text.
_UNMATCHED: Final = "unmatchedqueryword"


@pytest.fixture
def store(integ_store: Store) -> Store:
    """Return the store on the shared Postgres database, freshly bootstrapped."""
    return integ_store


async def _session(
    store: Store,
    *,
    audience: str = "open",
    account: str = "tester@example.com",
) -> UUID:
    session_id = uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title, "
            "agentsession_audience) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "$2, 'search test', $3)",
            session_id,
            account,
            audience,
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


def _unit(owner_id: UUID, *, idx: int, chunk: int = 0) -> VectorHit:
    """Return chunk ``chunk`` of record ``idx`` of part 0."""
    return VectorHit(owner_id=owner_id, part=0, idx=idx, field="content", chunk=chunk)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_semantic_only_finds_the_nearest_unit(store: Store) -> None:
    """The semantic arm's units come back as record hits, in its order.

    Each carries its record's head and its session's title; a unit whose record
    is gone is dropped.
    """
    session_id = await _session(store)
    await _record(store, session_id, idx=0, text="x" * 300)
    await _record(store, session_id, idx=1, text="the cat napped on the sofa")

    hits, searched = await search_sessions(
        store.engine,
        query=_UNMATCHED,
        vectors=_Fixed(
            hits=[
                _unit(session_id, idx=0),
                _unit(uuid4(), idx=0),
                _unit(session_id, idx=1),
            ],
        ),
        scope=_ANYONE,
    )

    assert searched
    assert all(h.source == "semantic" for h in hits)
    assert [(h.session_id, h.idx, h.snippet, h.title) for h in hits] == [
        (session_id, 0, "x" * 200, "search test"),
        (session_id, 1, "the cat napped on the sofa", "search test"),
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_fts_only_matches_the_term(store: Store) -> None:
    """A text query with no semantic arm returns the term-matching records."""
    session_id = await _session(store)
    await _record(store, session_id, idx=0, text="advisory lock acquired cleanly")
    await _record(store, session_id, idx=1, text="unrelated prose about weather")

    hits, searched = await search_sessions(
        store.engine,
        query="advisory lock",
        vectors=None,
        scope=_ANYONE,
    )
    unloaded, loading = await search_sessions(
        store.engine,
        query="advisory lock",
        vectors=_Fixed(hits=None),
        scope=_ANYONE,
    )

    assert [(h.session_id, h.idx) for h in hits] == [(session_id, 0)]
    assert hits[0].source == "fts"
    assert "advisory" in hits[0].snippet.lower()
    assert (unloaded, searched, loading) == (hits, False, False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "asked", "statements"),
    [
        pytest.param("keyword", [], ["ts_headline"], id="keyword"),
        pytest.param("vector", [("q", True, 5)], ["left(r.text"], id="vector"),
    ],
)
async def test_each_mode_returns_only_its_own_arm(
    mode: SearchMode,
    asked: list[tuple[str, bool, int]],
    statements: list[str],
) -> None:
    conn = make_conn()
    position = {"session_id": UUID(int=1), "part": 0, "idx": 0}
    conn.fetch.return_value = [{**position, "snippet": "s", "title": "t"}]
    arm = _Fixed(hits=[_unit(UUID(int=1), idx=0)])

    hits, searched = await search_sessions(
        cast("DatabaseEngine", FakeEngine(conn)),
        query="q",
        mode=mode,
        vectors=arm,
        limit=5,
    )

    assert arm.asked == asked
    assert [
        next(each for each in statements if each in sql) for sql in executed_sql(conn)
    ] == statements
    assert [(h.idx, h.source) for h in hits] == [
        (0, "fts" if mode == "keyword" else "semantic"),
    ]
    assert searched is (mode == "vector")


@pytest.mark.asyncio
async def test_fused_merges_both_arms() -> None:
    conn = make_conn()
    position = {"session_id": UUID(int=1), "part": 0, "idx": 0}
    conn.fetch.return_value = [{**position, "snippet": "s", "title": "t"}]
    arm = _Fixed(hits=[_unit(UUID(int=1), idx=0)])
    engine = cast("DatabaseEngine", FakeEngine(conn))

    hits, searched = await search_sessions(engine, query="q", vectors=arm)

    assert [(h.idx, h.source) for h in hits] == [(0, "both")]
    assert (searched, arm.asked) == (True, [("q", True, 20)])
    assert [(each, "v.field = 'content'" in test) for each, test, _ in arm.kept] == [
        (engine, True),
    ]
    assert len(executed_sql(conn)) == 2
    assert conn.fetch.await_args is not None
    assert conn.fetch.await_args.args[1:3] == ("q", 20)


@pytest.mark.asyncio
async def test_record_kinds_do_not_narrow_a_search() -> None:
    searched: list[object] = []
    for scope in (FeedScope(), FeedScope(kinds=("message",))):
        conn = make_conn()
        conn.fetch.return_value = []
        arm = _Fixed(hits=[])
        _ = await search_sessions(
            cast("DatabaseEngine", FakeEngine(conn)),
            query="q",
            vectors=arm,
            scope=scope,
        )
        searched.append(
            ([call.args for call in conn.fetch.await_args_list], arm.kept[0][1:]),
        )

    assert searched[0] == searched[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 200])
async def test_a_limit_at_either_bound_is_searched_and_cuts_the_hits(
    limit: int,
) -> None:
    conn = make_conn()
    conn.fetch.return_value = [
        {"session_id": UUID(int=1), "part": 0, "idx": idx, "snippet": "s", "title": "t"}
        for idx in range(2)
    ]

    hits, _searched = await search_sessions(
        cast("DatabaseEngine", FakeEngine(conn)),
        query="q",
        vectors=None,
        mode="keyword",
        limit=limit,
    )

    assert len(hits) == min(limit, 2)


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

    hits, _searched = await search_sessions(
        store.engine,
        query="advisory lock",
        vectors=_Fixed(hits=[_unit(session_id, idx=0), _unit(session_id, idx=2)]),
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

    hits, _searched = await search_sessions(
        store.engine,
        query="lock",
        vectors=None,
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

    hits, _searched = await search_sessions(
        store.engine,
        query=_UNMATCHED,
        vectors=_Fixed(
            hits=[_unit(session_id, idx=0, chunk=1), _unit(session_id, idx=0)],
        ),
        scope=_ANYONE,
    )

    positions = [(h.session_id, h.part, h.idx, h.chunk) for h in hits]
    assert positions == [(session_id, 0, 0, 1)]  # One hit, not one-per-chunk.


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_empty_query_raises(store: Store) -> None:
    """A blank query is a caller error, not an empty result."""
    with pytest.raises(ValueError, match="query must not be blank"):
        _ = await search_sessions(store.engine, query="", vectors=None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limit", "message"),
    [
        (0, r"limit must be in \[1, 200\]"),
        (201, r"limit must be in \[1, 200\]"),
    ],
    ids=["limit-zero", "limit-over"],
)
async def test_a_malformed_search_is_refused_before_any_read(
    limit: int,
    message: str,
) -> None:
    engine = FakeEngine()
    arm = _Fixed(hits=[])

    with pytest.raises(ValueError, match=message):
        _ = await search_sessions(
            cast("DatabaseEngine", engine),
            query="deadlock",
            vectors=arm,
            limit=limit,
        )

    assert (engine.conn.mock_calls, arm.asked) == ([], [])


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
    surfaces a transcript that no longer exists. Seeds 3 records, then shrinks the
    manifest to 1 -- idx 1 and 2 are now stale tail.
    """
    session_id = await _session(store)
    await _record(store, session_id, idx=0, text="live prefix deploy release")
    await _record(store, session_id, idx=1, text="stale tail deploy release")
    await _record(store, session_id, idx=2, text="stale tail advisory lock")
    await _bound_part(store, session_id, records=1)  # The compaction shrink.

    semantic, _searched = await search_sessions(
        store.engine,
        query=_UNMATCHED,
        vectors=_Records(first=session_id),
        scope=_ANYONE,
    )
    fts, _searched = await search_sessions(
        store.engine,
        query="advisory lock",
        vectors=None,
        scope=_ANYONE,
    )

    assert {(h.session_id, h.idx) for h in semantic} == {(session_id, 0)}
    # The fts term appears ONLY in the stale idx-2 row -> excluded entirely.
    assert all((h.session_id, h.idx) != (session_id, 2) for h in fts)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_reader_never_gets_a_session_hit_it_may_not_read(store: Store) -> None:
    """Both arms keep the reader's test before their limit: a hidden best hit is gone.

    The hidden session ranks first in each arm, and the limit is one; a test applied
    after the limit would leave nothing, and none at all would show the hidden one.
    """
    hidden = await _session(store, audience="private", account="ann@lab.example")
    shown = await _session(store)
    await _record(store, hidden, idx=0, text="deadlock deadlock deadlock")
    await _record(store, shown, idx=0, text="a deadlock once")
    arm = _Records(first=hidden)

    hits, searched = await search_sessions(
        store.engine,
        query="deadlock",
        vectors=arm,
        limit=1,
        scope=_ANYONE,
    )

    assert [(hit.session_id, hit.source) for hit in hits] == [(shown, "both")]
    assert searched
    assert arm.filtered == [True]


_OPEN_READER: Final = SessionReader(audiences=frozenset({"open"}))
_EVERYTHING: Final = SessionReader(
    audiences=AUDIENCES,
    every_session=True,
    oversight=True,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "filtered"),
    [
        pytest.param(FeedScope(reader=_EVERYTHING), False, id="every session"),
        pytest.param(
            FeedScope(reader=_EVERYTHING, account="ann@lab.example"),
            True,
            id="mine",
        ),
        pytest.param(FeedScope(reader=_OPEN_READER), True, id="lacking staff"),
    ],
)
async def test_the_search_scans_each_reader_as_what_it_reads_calls_for(
    scope: FeedScope,
    filtered: bool,
) -> None:
    conn = make_conn()
    conn.fetch.return_value = []
    arm = _Fixed(hits=[])

    _ = await search_sessions(
        cast("DatabaseEngine", FakeEngine(conn)),
        query="q",
        vectors=arm,
        limit=3,
        scope=scope,
    )

    assert arm.asked == [("q", filtered, 3)]


@pytest.mark.asyncio
async def test_the_semantic_arm_reads_each_units_head_and_title_in_its_order() -> None:
    conn = make_conn()
    first, gone = UUID(int=3), UUID(int=4)
    conn.fetch.return_value = [
        {"session_id": first, "part": 2, "idx": 5, "snippet": "b", "title": "t2"},
        {"session_id": first, "part": 2, "idx": 4, "snippet": "a", "title": "t1"},
    ]
    units = [
        VectorHit(owner_id=first, part=2, idx=4, field="content", chunk=1),
        VectorHit(owner_id=gone, part=0, idx=0, field="content", chunk=0),
        VectorHit(owner_id=first, part=2, idx=5, field="content", chunk=0),
    ]

    hits = await _semantic_arm(cast("Conn", conn), units)

    assert [(h.idx, h.chunk, h.snippet, h.title) for h in hits] == [
        (4, 1, "a", "t1"),
        (5, 0, "b", "t2"),
    ]
    assert {(h.session_id, h.part, h.field) for h in hits} == {(first, 2, "content")}
    assert conn.fetch.await_args is not None
    assert conn.fetch.await_args.args == (
        (
            "SELECT r.session_id, r.part, r.idx, left(r.text, $4) AS snippet, "
            "i.title AS title FROM session_records r "
            "JOIN inquiries i ON i.id = r.session_id "
            "WHERE (r.session_id, r.part, r.idx) IN "
            "(SELECT * FROM unnest($1::uuid[], $2::int[], $3::int[]))"
        ),
        [first, gone, first],
        [2, 0, 2],
        [4, 0, 5],
        200,
    )


@pytest.mark.asyncio
async def test_blank_search_is_refused_without_acquiring_a_connection() -> None:
    engine = FakeEngine()
    with pytest.raises(ValueError, match=r"^query must not be blank$"):
        await search_sessions(
            cast("DatabaseEngine", engine),
            query=" \n ",
            vectors=None,
        )
    assert engine.conn.mock_calls == []


@pytest.mark.asyncio
async def test_full_text_arm_projects_a_record_hit_and_binds_scope_before_limit() -> (
    None
):
    conn = make_conn()
    conn.fetch.return_value = [
        {
            "session_id": UUID(int=3),
            "part": 2,
            "idx": 4,
            "snippet": "a <b>lock</b>",
            "title": "debugging",
        },
    ]
    scope = FeedScope(actors=("agent",), reader=_OPEN_READER)

    hits = await _fts_arm(cast("Conn", conn), "lock", limit=2, scope=scope)

    assert hits == [
        _ArmHit(
            session_id=UUID(int=3),
            part=2,
            idx=4,
            field="",
            chunk=0,
            snippet="a <b>lock</b>",
            title="debugging",
        ),
    ]
    assert conn.fetch.await_args is not None
    sql, *params = conn.fetch.await_args.args
    assert "r.idx < m.records" in str(sql)
    assert "i.owner = ANY($3::text[])" in str(sql)
    assert "ORDER BY ts_rank(r.search, q) DESC" in str(sql)
    assert params[:3] == ["lock", 2, ["agent"]]


class _OwnPolicy(DefaultPolicy):
    """Reads only the rows of the reader's own account."""

    @override
    def read_filter(
        self,
        kind: ResourceKind,
        alias: str,
        *,
        params: list[object],
        principal: Principal,
    ) -> str:
        del kind
        params.append(principal.email)
        return f"{alias}.account = ${len(params)}"


_ME: Final = Principal(email="tester@example.com", role="viewer")


async def _inquiry(
    store: Store,
    kind: str,
    *,
    title: str = "untitled",
    account: str = "tester@example.com",
    **texts: str,
) -> UUID:
    """Insert an inquiry of ``kind`` with ``texts`` in its text columns; return its id."""
    inquiry_id = uuid4()
    columns = ("description", "paper_abstract")
    async with store.engine.acquire() as conn:
        await conn.execute(
            vetted_sql(
                "INSERT INTO inquiries (id, kind, seq, status, account, title, "
                "description, paper_abstract) VALUES ($1, $2, nextval('seq_",
                kind.lower(),
                "'), 'active', $3, $4, $5, $6)",
            ),
            inquiry_id,
            kind,
            account,
            title,
            *(texts.get(column) for column in columns),
        )
    return inquiry_id


async def _note(store: Store, from_id: UUID, to_id: UUID, note: str) -> str:
    """Insert a ``produced_by`` edge with ``note``; return its unit's field."""
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO edges (from_id, from_kind, to_id, to_kind, edge_kind, note) "
            "SELECT f.id, f.kind, t.id, t.kind, 'produced_by', $3 "
            "FROM inquiries f, inquiries t WHERE f.id = $1 AND t.id = $2",
            from_id,
            to_id,
            note,
        )
    return f"edge:produced_by:{to_id}"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_reader_never_gets_an_inquiry_hit_it_may_not_read(store: Store) -> None:
    """Both arms keep the reader's test before their limit; a note needs both ends.

    The hidden inquiry ranks first in each arm, then a note from a shown inquiry to
    it, and the limit is one: a test after the limit would leave nothing, and a note
    tested on its owner alone would be the hit. A session is no inquiry hit at all.
    """
    hidden = await _inquiry(
        store,
        "Issue",
        account="ann@lab.example",
        description="deadlock deadlock deadlock",
    )
    shown = await _inquiry(store, "Issue", description="a deadlock once")
    await _note(store, shown, hidden, "deadlock deadlock deadlock")
    session = await _session(store)
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET title = 'deadlock deadlock deadlock deadlock' "
            "WHERE id = $1",
            session,
        )
    arm = _Units(first=(session, hidden))

    hits, searched = await search_inquiries(
        store.engine,
        query="deadlock",
        vectors=arm,
        policy=_OwnPolicy(),
        principal=_ME,
        sessions=_ANYONE.reader,
        limit=1,
    )

    assert [(hit.inquiry_id, hit.field, hit.source) for hit in hits] == [
        (shown, "text", "both"),
    ]
    assert (searched, arm.filtered) == (True, [True])


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_reader_never_gets_a_note_of_a_session_it_may_not_read(
    store: Store,
) -> None:
    """A note with a session at either end needs the reader to read that session.

    Notes of a private session rank first in each arm, one from it and one to it,
    and the limit is one: the inquiry read filter alone would keep them.
    """
    hidden = await _session(store, audience="private", account="ann@lab.example")
    shown = await _inquiry(store, "Issue", description="a deadlock once")
    await _note(store, hidden, shown, "deadlock deadlock deadlock")
    await _note(store, shown, hidden, "deadlock deadlock deadlock")
    arm = _Units(first=(hidden,))

    hits, _searched = await search_inquiries(
        store.engine,
        query="deadlock",
        vectors=arm,
        policy=None,
        principal=None,
        sessions=_ANYONE.reader,
        limit=1,
    )

    assert [(hit.inquiry_id, hit.field, hit.source) for hit in hits] == [
        (shown, "text", "both"),
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_session_is_found_by_its_notes_alone(store: Store) -> None:
    session = await _session(store)
    issue = await _inquiry(store, "Issue")
    field = await _note(store, session, issue, "spawnword reason")

    found = [
        [
            (hit.inquiry_id, hit.field, hit.kind)
            for hit in (
                await search_inquiries(
                    store.engine,
                    query=query,
                    vectors=_Units(first=(session,)),
                    policy=None,
                    principal=None,
                    sessions=_ANYONE.reader,
                    mode="keyword",
                    limit=1,
                )
            )[0]
        ]
        for query in ("spawnword", "search test")
    ]
    vector, _searched = await search_inquiries(
        store.engine,
        query=_UNMATCHED,
        vectors=_Units(first=(session,)),
        policy=None,
        principal=None,
        sessions=_ANYONE.reader,
        mode="vector",
    )

    assert found == [[(session, field, "AgentSession")], []]
    assert [(hit.inquiry_id, hit.field) for hit in vector] == [
        (session, field),
        (issue, "text"),
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    ("kind", "column"),
    [("Issue", "description"), ("Belief", "title"), ("Paper", "paper_abstract")],
)
async def test_every_embedded_unit_is_keyword_searchable(
    store: Store,
    kind: str,
    column: str,
) -> None:
    """An inquiry text, by the column it is in, and an edge note, each by keyword."""
    texts = {"title": "untitled"} | {column: "findme words"}
    inquiry = await _inquiry(store, kind, **texts)
    other = await _inquiry(store, "Issue")
    field = await _note(store, other, inquiry, "notedword here")

    found = [
        [
            (hit.inquiry_id, hit.field, hit.kind, hit.source)
            for hit in (
                await search_inquiries(
                    store.engine,
                    query=query,
                    vectors=None,
                    policy=None,
                    principal=None,
                    sessions=_ANYONE.reader,
                    mode="keyword",
                )
            )[0]
        ]
        for query in ("findme", "notedword")
    ]

    assert found == [
        [(inquiry, "text", kind, "fts")],
        [(other, field, "Issue", "fts")],
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_keyword_expression_matches_its_index(store: Store) -> None:
    """The planner reads each keyword arm's index, so the two spellings agree."""
    plans: list[str] = []
    async with store.engine.acquire() as conn, tx(conn):
        await conn.execute("SET LOCAL enable_seqscan = off")
        for sql in (
            vetted_sql(
                "EXPLAIN SELECT 1 FROM inquiries i WHERE ",
                _INQUIRY_KEYWORDS,
                " @@ websearch_to_tsquery('simple', 'x')",
            ),
            vetted_sql(
                "EXPLAIN SELECT 1 FROM edges e WHERE e.note IS NOT NULL AND ",
                _NOTE_KEYWORDS,
                " @@ websearch_to_tsquery('simple', 'x')",
            ),
        ):
            rows = await conn.fetch(sql)
            plans.append("\n".join(str(row[0]) for row in rows))

    assert "idx_inquiries_search" in plans[0]
    assert "idx_edges_note_search" in plans[1]
    assert INQUIRY_TEXT_COLUMNS == ("title", "description", "paper_abstract")


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_inquiry_hit_carries_its_label_and_snippet(store: Store) -> None:
    """A vector hit gets its snippet after the merge; a unit gone since is dropped."""
    issue = await _inquiry(store, "Issue", title="the title", description="the body")
    other = await _inquiry(store, "Issue")
    field = await _note(store, issue, other, "the note")
    gone = VectorHit(owner_id=issue, part=0, idx=0, field="edge:narrows:x", chunk=0)
    arm = _Fixed(
        hits=[
            VectorHit(owner_id=issue, part=0, idx=0, field="text", chunk=1),
            gone,
            VectorHit(owner_id=issue, part=0, idx=0, field=field, chunk=0),
        ],
    )

    hits, _searched = await search_inquiries(
        store.engine,
        query=_UNMATCHED,
        vectors=arm,
        policy=None,
        principal=None,
        sessions=_ANYONE.reader,
        mode="vector",
        kind="Issue",
    )

    assert [(h.field, h.kind, h.title, h.snippet) for h in hits] == [
        ("text", "Issue", "the title", "the title the body "),
        (field, "Issue", "the title", "the note"),
    ]
    assert hits[0].seq > 0
    assert "i.kind = $" in arm.kept[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "asked", "statements"),
    [
        pytest.param("keyword", [], ["ts_rank", "ts_headline"], id="keyword"),
        pytest.param("vector", [("q", True, 5)], ["ts_headline"], id="vector"),
    ],
)
async def test_each_inquiry_mode_returns_only_its_own_arm(
    mode: SearchMode,
    asked: list[tuple[str, bool, int]],
    statements: list[str],
) -> None:
    conn = make_conn()
    owner = UUID(int=1)
    unit = {"owner_id": owner, "field": "text"}
    label = {"kind": "Issue", "seq": 7, "title": "t", "snippet": "s"}
    conn.fetch.side_effect = (
        [[unit], [unit | label]] if mode == "keyword" else [[unit | label]]
    )
    arm = _Fixed(hits=[VectorHit(owner_id=owner, part=0, idx=0, field="text", chunk=0)])

    hits, searched = await search_inquiries(
        cast("DatabaseEngine", FakeEngine(conn)),
        query="q",
        mode=mode,
        vectors=arm,
        policy=None,
        principal=None,
        sessions=_ANYONE.reader,
        limit=5,
    )

    assert arm.asked == asked
    assert [
        next(each for each in statements if each in sql) for sql in executed_sql(conn)
    ] == statements
    assert hits == [
        InquirySearchHit(
            inquiry_id=owner,
            kind="Issue",
            seq=7,
            title="t",
            field="text",
            score=1.0 / (RRF_K + 1),
            source="fts" if mode == "keyword" else "semantic",
            snippet="s",
        ),
    ]
    assert searched is (mode == "vector")


def test_a_unit_is_read_at_each_end_and_a_session_end_by_its_audience() -> None:
    """The vector arm's test, spelled out: the database tests above run what it means."""
    reader = _InquiryReader(
        policy=_OwnPolicy(),
        principal=_ME,
        sessions=_ANYONE.reader,
        kind="Issue",
    )
    params: list[object] = ["q"]

    test = reader.units(params)

    expected: list[object] = ["q", "Issue"]
    ends = [
        vetted_sql(
            inquiry_read_filter(
                reference,
                policy=_OwnPolicy(),
                principal=_ME,
                params=expected,
            ),
            " AND EXISTS (SELECT 1 FROM inquiries s WHERE s.id = ",
            reference,
            " AND (s.kind <> 'AgentSession' OR ",
            visible_sql("s", params=expected, reader=_ANYONE.reader),
            "))",
        )
        for reference in ("i.id", "NULLIF(split_part(v.field, ':', 3), '')::uuid")
    ]
    assert test == vetted_sql(
        "v.field <> 'content' AND i.kind = $2 AND ",
        ends[0],
        " AND CASE WHEN v.field = 'text' THEN i.kind <> 'AgentSession' ELSE ",
        ends[1],
        " END",
    )
    assert params == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "limit", "message"),
    [
        ("q", 0, r"limit must be in \[1, 200\]"),
        ("q", 201, r"limit must be in \[1, 200\]"),
        (" ", 1, "query must not be blank"),
    ],
)
async def test_a_malformed_inquiry_search_is_refused_before_any_read(
    query: str,
    limit: int,
    message: str,
) -> None:
    engine = FakeEngine()
    arm = _Fixed(hits=[])

    with pytest.raises(ValueError, match=message):
        _ = await search_inquiries(
            cast("DatabaseEngine", engine),
            query=query,
            vectors=arm,
            policy=None,
            principal=None,
            sessions=_ANYONE.reader,
            limit=limit,
        )

    assert (engine.conn.mock_calls, arm.asked) == ([], [])


@dataclass(frozen=True, slots=True, kw_only=True)
class _Units:
    """A vector arm over inquiry texts and edge notes, ``first``'s owners ranked first.

    It applies ``kept`` in its WHERE before its LIMIT over a ``v`` made of the
    inquiries and noted edges, so the test it applies is core's own. Within an
    owner, a note ranks before its text.
    """

    first: tuple[UUID, ...]
    filtered: list[bool] = field(default_factory=list[bool])

    async def nearest(
        self,
        engine: DatabaseEngine,
        *,
        query: str,
        kept: Kept,
        filtered: bool,
        limit: int,
    ) -> list[VectorHit] | None:
        """Rank ``first``'s units first, then the rest; keep what ``kept`` keeps."""
        del query
        self.filtered.append(filtered)
        params: list[object] = [list(self.first), limit]
        test = kept(params)
        async with engine.acquire() as conn:
            rows = await conn.fetch(
                vetted_sql(
                    "SELECT v.owner_id, v.field FROM (SELECT id AS owner_id, 0 AS part, "
                    "0 AS idx, 'text' AS field FROM inquiries UNION ALL "
                    "SELECT from_id, 0, 0, 'edge:' || edge_kind || ':' || to_id "
                    "FROM edges WHERE note IS NOT NULL) v "
                    "JOIN inquiries i ON i.id = v.owner_id WHERE ",
                    test,
                    " ORDER BY v.owner_id = ANY($1::uuid[]) DESC, v.field LIMIT $2",
                ),
                *params,
            )
        return [
            VectorHit(
                owner_id=cast("UUID", row["owner_id"]),
                part=0,
                idx=0,
                field=cast("str", row["field"]),
                chunk=0,
            )
            for row in rows
        ]


@dataclass(frozen=True, slots=True, kw_only=True)
class _Fixed:
    """A semantic arm that answers the same units for every query, recording each ask."""

    hits: list[VectorHit] | None
    asked: list[tuple[str, bool, int]] = field(
        default_factory=list[tuple[str, bool, int]],
    )
    kept: list[tuple[object, str, list[object]]] = field(
        default_factory=list[tuple[object, str, list[object]]],
    )

    async def nearest(
        self,
        engine: DatabaseEngine,
        *,
        query: str,
        kept: Kept,
        filtered: bool,
        limit: int,
    ) -> list[VectorHit] | None:
        """Record the ask, its engine and the test ``kept`` writes; answer :attr:`hits`."""
        params: list[object] = []
        self.kept.append((engine, kept(params), params))
        self.asked.append((query, filtered, limit))
        return self.hits


@dataclass(frozen=True, slots=True, kw_only=True)
class _Records:
    """A semantic arm whose units are the session records, ``first``'s ranked first.

    It applies ``kept`` in its WHERE before its LIMIT, as the arm's contract says,
    over a ``v`` made of the records, so the test it applies is core's own.
    """

    first: UUID
    filtered: list[bool] = field(default_factory=list[bool])

    async def nearest(
        self,
        engine: DatabaseEngine,
        *,
        query: str,
        kept: Kept,
        filtered: bool,
        limit: int,
    ) -> list[VectorHit] | None:
        """Rank ``first``'s records first, then the rest; keep what ``kept`` keeps."""
        del query
        self.filtered.append(filtered)
        params: list[object] = [self.first, limit]
        test = kept(params)
        async with engine.acquire() as conn:
            rows = await conn.fetch(
                vetted_sql(
                    "SELECT v.owner_id, v.idx FROM (SELECT session_id AS owner_id, "
                    "part, idx, 'content'::text AS field FROM session_records) v "
                    "JOIN inquiries i ON i.id = v.owner_id WHERE ",
                    test,
                    " ORDER BY v.owner_id = $1 DESC, v.idx LIMIT $2",
                ),
                *params,
            )
        return [
            _unit(cast("UUID", row["owner_id"]), idx=cast("int", row["idx"]))
            for row in rows
        ]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
