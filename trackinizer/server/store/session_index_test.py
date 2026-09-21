"""Per-model partial HNSW index maintenance + the planner-uses-it guard.

The load-bearing test here is ``test_semantic_query_uses_the_partial_index``: the
cosine arm casts ``(embedding::halfvec(dim))`` so the planner can use a model's
partial index; if the cast ever drifts from the index's cast the query silently
seqscans 4.7M rows. Forcing ``enable_seqscan=off`` and reading EXPLAIN pins that
the index is actually eligible.

``index_name_for`` is unit-tested without a DB; the rest run on pglite.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import pytest_asyncio

from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.semantic_mapper_footprint import FootprintMapper
from trackinizer.server.store.core import Store
from trackinizer.server.store.session_embed import sweep_session_embeddings
from trackinizer.server.store.session_index import (
    ensure_model_index,
    index_name_for,
)


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trackinizer.lib.postgres import PostgresEngine


_MODEL = StubEmbedder(dim=1024).name  # "stub-1024"
_DIM = StubEmbedder(dim=1024).dim  # 1024.


def test_index_name_is_deterministic_and_sanitized() -> None:
    """Every non-alnum char becomes ``_``; the result is lowercased + prefixed."""
    assert index_name_for("stub-1024") == "idx_session_embeddings_hnsw_stub_1024"
    assert (
        index_name_for("qwen3-embedding-4b@1024")
        == "idx_session_embeddings_hnsw_qwen3_embedding_4b_1024"
    )
    assert (
        index_name_for("jina-embeddings-v5-text-nano@768")
        == "idx_session_embeddings_hnsw_jina_embeddings_v5_text_nano_768"
    )


def test_index_name_rejects_an_unsafe_slug() -> None:
    """A slug with quotes/spaces is a programming error, not a runtime input."""
    with pytest.raises(ValueError, match="not a valid index-name source"):
        index_name_for("bad'; DROP TABLE session_embeddings; --")


@pytest_asyncio.fixture(loop_scope="session")
async def store(integ_engine: PostgresEngine) -> AsyncIterator[Store]:
    """Bootstrapped store with the session tables emptied."""
    built = Store(integ_engine, embed=StubEmbedder())
    await built.bootstrap()
    async with built.engine.acquire() as conn:
        await conn.execute(
            "TRUNCATE session_embeddings, session_records, session_manifests, "
            "inquiries CASCADE",
        )
    yield built


async def _seed_rows(store: Store, count: int) -> None:
    session_id = uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "'tester@example.com', 'index test')",
            session_id,
        )
        for idx in range(count):
            await conn.execute(
                "INSERT INTO session_records "
                "(session_id, part, idx, kind, payload, text) "
                "VALUES ($1, 0, $2, 'UserMessage', '{}'::json, $3)",
                session_id,
                idx,
                f"indexed record number {idx} about deadlocks and locks",
            )
        # The sweep reads only the live manifest prefix (idx < records), so seed
        # a manifest covering all rows -- production writes both together.
        await conn.execute(
            "INSERT INTO session_manifests "
            "(session_id, part, name, metadata, ir_id, format, records) "
            "VALUES ($1, 0, 's.jsonl', '{}'::json, gen_random_uuid(), 'claude', $2)",
            session_id,
            count,
        )
    await sweep_session_embeddings(
        store.engine,
        mapper=FootprintMapper(),
        embedder=StubEmbedder(dim=1024),
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_ensure_model_index_is_idempotent(store: Store) -> None:
    """A second ``ensure_model_index`` for the same model no-ops (IF NOT EXISTS)."""
    async with store.engine.acquire() as conn:
        await ensure_model_index(conn, _MODEL, _DIM)
        await ensure_model_index(conn, _MODEL, _DIM)  # No error on the repeat.
        present = await conn.fetchval(
            "SELECT 1 FROM pg_indexes WHERE indexname = $1",
            index_name_for(_MODEL),
        )
    assert present == 1


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_ensure_creates_a_new_models_index(store: Store) -> None:
    """A model not in the baseline gets its partial index created on demand."""
    name = "qwen3-embedding-8b@1024"
    async with store.engine.acquire() as conn:
        await conn.execute("DROP INDEX IF EXISTS " + index_name_for(name))
        await ensure_model_index(conn, name, 1024)
        present = await conn.fetchval(
            "SELECT 1 FROM pg_indexes WHERE indexname = $1",
            index_name_for(name),
        )
    assert present == 1


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_semantic_query_uses_the_partial_index(store: Store) -> None:
    """The cosine cast query is served by the model's partial HNSW, not a seqscan.

    The regression guard: ``search_session_records`` casts
    ``(embedding::halfvec(dim)) <=> $q::halfvec(dim)`` to match the partial
    index's cast. If the cast drifts, the planner cannot use the index. Forcing
    ``enable_seqscan=off`` makes an unusable index surface as a non-index plan.
    """
    await _seed_rows(store, 25)
    query = "[" + ",".join("0.03125" for _ in range(_DIM)) + "]"
    async with store.engine.acquire() as conn:
        await ensure_model_index(conn, _MODEL, _DIM)
        await conn.execute("SET LOCAL enable_seqscan = off")
        plan_rows = await conn.fetch(
            f"EXPLAIN SELECT session_id, part, idx FROM session_embeddings "  # noqa: S608 -- _DIM is a validated int; _MODEL is a fixed test constant.
            f"WHERE model = '{_MODEL}' "
            f"ORDER BY (embedding::halfvec({_DIM})) <=> $1::halfvec({_DIM}) LIMIT 5",
            query,
        )
    plan = "\n".join(str(row["QUERY PLAN"]) for row in plan_rows)
    assert index_name_for(_MODEL) in plan, plan


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_dimensionless_column_holds_the_seeded_vectors(store: Store) -> None:
    """The dimension-free ``halfvec`` column stores the 1024-dim stub vectors."""
    await _seed_rows(store, 3)
    async with store.engine.acquire() as conn:
        stored = await conn.fetchval(
            "SELECT embedding::text FROM session_embeddings WHERE model = $1 LIMIT 1",
            _MODEL,
        )
    # A round-trip sanity check: the stored vector parses back to 1024 floats.
    assert isinstance(stored, str)
    assert stored.count(",") == _DIM - 1


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
