"""The vector arm: its index per settings key, its scans, and its answer before load.

The index tests run on Postgres: whether the planner can use a partial HNSW index
for the arm's query is the database's to say. The scan tests run on a scripted
connection, since what they pin is the statements sent.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Final, cast, override
from uuid import UUID

import asyncio

import pytest
import pytest_asyncio

from trackinizer.addons.embedding import search
from trackinizer.addons.embedding.fakes import HashEmbedder, vector_store
from trackinizer.addons.embedding.search import (
    Nearest,
    drop_other_indexes,
    ensure_index,
)
from trackinizer.addons.embedding.units import Settings
from trackinizer.conftest import FakeEngine, executed_sql, make_conn, rolled_back
from trackinizer.server.store.search import VectorHit
from trackinizer.server.values import vec_to_text


if TYPE_CHECKING:
    from unittest.mock import AsyncMock

    from trackinizer.lib.postgres import Conn, DatabaseEngine, PostgresEngine
    from trackinizer.server.store.core import Store


_SETTINGS: Final = Settings(
    key="0123456789abcdef",
    dim=2,
    chunk_chars=1_500,
    overlap_chars=200,
    session_kinds=("UserMessage",),
)
_SQL: Final = (
    "SELECT v.owner_id, v.part, v.idx, v.field, v.chunk "
    "FROM embedding.vectors v "
    "WHERE v.settings = '0123456789abcdef' "
    "AND (SELECT v.idx = $3 FROM inquiries i WHERE i.id = v.owner_id) "
    "ORDER BY (v.embedding::halfvec(2)) <=> $1::halfvec(2) LIMIT $2"
)
_SET_WIDTH: Final = "SELECT set_config('hnsw.ef_search', $1, true)"
_ITERATE: Final = "SET LOCAL hnsw.iterative_scan = strict_order"
_VERSION: Final = "SELECT extversion FROM pg_extension WHERE extname = 'vector'"


@pytest_asyncio.fixture(loop_scope="session")
async def store(integ_engine: PostgresEngine) -> Store:
    """Return a store whose database holds the vector table, every table empty."""
    return await vector_store(integ_engine)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_search_answers_none_until_the_model_is_loaded(store: Store) -> None:
    model = _Counting(dim=2)
    arm = Nearest(model=model, settings=_SETTINGS, loaded=asyncio.Event())

    before = await _ask(arm, store.engine)
    arm.loaded.set()
    after = await _ask(arm, store.engine)

    assert (before, after, model.queries) == (None, [], 1)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_index_matches_the_key_and_width(store: Store) -> None:
    """The arm's query is served by its key's partial index, not a sequential scan."""
    async with store.engine.acquire() as conn:
        await ensure_index(conn, _SETTINGS)
        await ensure_index(conn, _SETTINGS)
        definition = await conn.fetchval(
            "SELECT indexdef FROM pg_indexes WHERE indexname = $1",
            "vectors_hnsw_0123456789abcdef",
        )
        # With no scan or sort to fall back on, only an eligible index orders rows.
        async with rolled_back(conn):
            for plan_kind in ("seqscan", "bitmapscan", "sort"):
                await conn.execute(f"SET LOCAL enable_{plan_kind} = off")
            plan = await conn.fetch(
                "EXPLAIN " + search._sql(_SETTINGS, "TRUE"),
                "[0.5,0.5]",
                5,
            )

    assert isinstance(definition, str)
    assert "((embedding)::halfvec(2)) halfvec_cosine_ops" in definition
    assert "WHERE (settings = '0123456789abcdef'::text)" in definition
    assert "vectors_hnsw_0123456789abcdef" in "\n".join(
        str(row["QUERY PLAN"]) for row in plan
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_only_the_configured_keys_vector_index_survives(store: Store) -> None:
    other = replace(_SETTINGS, key="fedcba9876543210")
    async with store.engine.acquire() as conn:
        await ensure_index(conn, _SETTINGS)
        await ensure_index(conn, other)
        await drop_other_indexes(conn, _SETTINGS)
        names = await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'embedding' "
            "ORDER BY indexname",
        )

    assert [row["indexname"] for row in names] == [
        "vectors_hnsw_0123456789abcdef",
        "vectors_pkey",
        "vectors_settings",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settings",
    [replace(_SETTINGS, key="bad'; DROP TABLE x; --"), replace(_SETTINGS, dim=0)],
    ids=["unsafe-key", "zero-dim"],
)
async def test_a_bad_key_or_width_is_refused_before_any_sql(settings: Settings) -> None:
    """Neither can be a bound parameter, so the check is all that keeps the SQL safe."""
    conn = make_conn()

    with pytest.raises(ValueError, match=r"Expected a 16-hex-digit key and a width"):
        await ensure_index(cast("Conn", conn), settings)
    with pytest.raises(ValueError, match=r"Expected a 16-hex-digit key and a width"):
        _ = search._sql(settings, "TRUE")

    assert executed_sql(conn) == []


@pytest.mark.asyncio
async def test_the_index_is_made_for_the_key_at_its_width() -> None:
    conn = make_conn()

    await ensure_index(cast("Conn", conn), _SETTINGS)

    assert executed_sql(conn) == [
        (
            "CREATE INDEX IF NOT EXISTS vectors_hnsw_0123456789abcdef "
            "ON embedding.vectors USING hnsw ((embedding::halfvec(2)) halfvec_cosine_ops) "
            "WHERE settings = '0123456789abcdef'"
        ),
    ]


@pytest.mark.asyncio
async def test_each_other_keys_index_is_dropped_by_its_name() -> None:
    conn = make_conn()
    conn.fetch.return_value = [{"indexname": "vectors_hnsw_fedcba9876543210"}]

    await drop_other_indexes(cast("Conn", conn), _SETTINGS)

    assert conn.fetch.await_args is not None
    assert conn.fetch.await_args.args == (
        (
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'embedding' "
            "AND indexname ~ '^vectors_hnsw_[0-9a-f]{16}$' AND indexname <> $1"
        ),
        "vectors_hnsw_0123456789abcdef",
    )
    assert conn.execute.await_args is not None
    assert conn.execute.await_args.args == (
        "DROP INDEX IF EXISTS embedding.vectors_hnsw_fedcba9876543210",
    )


@pytest.mark.asyncio
async def test_an_unfiltered_search_runs_the_one_statement() -> None:
    conn = make_conn()
    conn.fetch.return_value = _unit_rows(2)

    hits = await _ask(_loaded(), _engine(conn), filtered=False, limit=7)

    assert executed_sql(conn) == [_SQL]
    assert conn.fetch.await_args is not None
    assert conn.fetch.await_args.args[1:] == (await _query_text(), 7, 0)
    assert hits == [
        VectorHit(owner_id=UUID(int=1), part=0, idx=idx, field="content", chunk=1)
        for idx in range(2)
    ]
    conn.fetchval.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_iterative_scan_is_set_for_one_transaction() -> None:
    """The statement is the plain one; only the transaction around it differs."""
    conn = make_conn()
    conn.fetchval.return_value = "0.8.1"
    conn.fetch.return_value = _unit_rows(3)

    hits = await _ask(_loaded(), _engine(conn), filtered=True, limit=3)

    assert executed_sql(conn) == ["BEGIN", _ITERATE, _SQL, "COMMIT"]
    assert conn.fetch.await_args is not None
    assert conn.fetch.await_args.args[1:] == (await _query_text(), 3, 0)
    assert len(hits or []) == 3


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
    conn.fetchval.side_effect = ["0.7.4", offered]

    hits = await _ask(_loaded(), _engine(conn), filtered=True, limit=3)

    assert executed_sql(conn) == [
        "BEGIN",
        _SQL,
        *[statement for _ in widths for statement in (_SET_WIDTH, _SQL)],
        "COMMIT",
    ]
    assert [
        call.args[1]
        for call in conn.execute.await_args_list
        if call.args[0] == _SET_WIDTH
    ] == widths
    # Every asking is the same query with the same values.
    assert [call.args for call in conn.fetch.await_args_list] == [
        (_SQL, await _query_text(), 3, 0),
    ] * len(counts)
    # The offered width is read only once the first query has fallen short.
    asked = [call.args for call in conn.fetchval.await_args_list]
    assert asked == [
        (_VERSION,),
        *([("SHOW hnsw.ef_search",)] if counts[0] < 3 else []),
    ]
    assert len(hits or []) == counts[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version", "statements"),
    [
        ("0.8.0", ["BEGIN", _ITERATE, _SQL, "COMMIT"]),
        ("0.10.2", ["BEGIN", _ITERATE, _SQL, "COMMIT"]),
        ("1.0.0", ["BEGIN", _ITERATE, _SQL, "COMMIT"]),
        ("0.7.4", ["BEGIN", _SQL, "COMMIT"]),
        ("0.5.1", ["BEGIN", _SQL, "COMMIT"]),
        (None, ["BEGIN", _SQL, "COMMIT"]),
        ("unreleased", ["BEGIN", _SQL, "COMMIT"]),
    ],
)
async def test_a_filtered_search_is_scanned_as_the_installed_pgvector_allows(
    version: str | None,
    statements: list[str],
) -> None:
    conn = make_conn()
    conn.fetchval.return_value = version
    conn.fetch.return_value = _unit_rows(3)

    _ = await _ask(_loaded(), _engine(conn), filtered=True, limit=3)

    assert executed_sql(conn) == statements
    assert conn.fetchval.await_args_list[0].args == (_VERSION,)


@pytest.mark.asyncio
async def test_the_installed_version_is_read_once_for_each_engine() -> None:
    current, older = make_conn(), make_conn()
    current.fetchval.return_value = "0.8.1"
    older.fetchval.return_value = "1000"
    current.fetch.return_value = older.fetch.return_value = _unit_rows(3)
    engines = [_engine(current), _engine(older)]

    for engine in (*engines, *engines):
        _ = await _ask(_loaded(), engine, filtered=True, limit=3)

    assert (current.fetchval.await_count, older.fetchval.await_count) == (1, 1)
    assert _ITERATE in executed_sql(current)
    assert _ITERATE not in executed_sql(older)


class _Counting(HashEmbedder):
    """A hash model that counts the queries it embeds."""

    queries = 0

    @override
    async def embed_query(self, text: str) -> list[float]:
        """Count, then embed."""
        self.queries += 1
        return await super().embed_query(text)


def _loaded() -> Nearest:
    """Return the arm over a loaded 2-wide hash model."""
    loaded = asyncio.Event()
    loaded.set()
    return Nearest(model=HashEmbedder(dim=2), settings=_SETTINGS, loaded=loaded)


async def _ask(
    arm: Nearest,
    engine: DatabaseEngine,
    *,
    filtered: bool = False,
    limit: int = 5,
) -> list[VectorHit] | None:
    """Ask ``arm`` for the query ``q``, keeping units at index 0."""
    return await arm.nearest(
        engine,
        query="q",
        kept=_first_records,
        filtered=filtered,
        limit=limit,
    )


def _first_records(params: list[object]) -> str:
    """Keep the units of each part's first record."""
    params.append(0)
    return f"v.idx = ${len(params)}"


async def _query_text() -> str:
    """Return the query ``q``'s vector as the arm binds it."""
    return vec_to_text(await HashEmbedder(dim=2).embed_query("q"))


def _engine(conn: AsyncMock) -> DatabaseEngine:
    """Return an engine over ``conn``, which the scan choice tells apart by identity."""
    return cast("DatabaseEngine", FakeEngine(conn))


def _unit_rows(count: int) -> list[dict[str, object]]:
    """Return ``count`` rows shaped as the arm's query answers."""
    return [
        {"owner_id": UUID(int=1), "part": 0, "idx": idx, "field": "content", "chunk": 1}
        for idx in range(count)
    ]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
