"""The authority sweep loop coalesces edge-change bursts and stays alive."""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio

import pytest
import pytest_asyncio

from trackinizer.lib.custom_json import convert
from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.authority_sweep import (
    authority_sweep_loop,
    edge_change_count,
)
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.store.core import Store
from trackinizer.wire.bodies import SubmitBelief, SubmitPaper


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trackinizer.lib.postgres import PGliteEngine


@pytest_asyncio.fixture(loop_scope="session")
async def store(pglite_engine: PGliteEngine) -> AsyncIterator[Store]:
    """Return a bootstrapped Store over the session's shared PGlite engine."""
    await reset_schema(pglite_engine)
    built = Store(pglite_engine, embed=StubEmbedder())
    await built.bootstrap()
    yield built


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_edge_change_count_tracks_edge_audits(store: Store) -> None:
    before = await edge_change_count(store)
    claim = await store.submit_belief(SubmitBelief(account="t@e.com", title="C"))
    paper = await store.submit_paper(SubmitPaper(account="t@e.com", title="P"))
    await store.add_edge(
        from_id=paper,
        to_id=claim,
        edge_kind="proves",
        actor="t",
        valence=0.5,
    )

    # add_edge emits an edge_added audit on both endpoints.
    assert await edge_change_count(store) > before


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_loop_recomputes_on_startup_then_can_be_cancelled(store: Store) -> None:
    claim = await store.submit_belief(SubmitBelief(account="t@e.com", title="Claim"))
    paper = await store.submit_paper(SubmitPaper(account="t@e.com", title="Paper"))
    await store.add_edge(
        from_id=paper,
        to_id=claim,
        edge_kind="proves",
        actor="t",
        valence=0.9,
    )

    # A long interval proves the FIRST pass is unconditional (not interval-gated):
    # the loop must write a score before its first sleep, then park.
    task = asyncio.create_task(
        authority_sweep_loop(store, min_interval_sec=3600.0, max_interval_sec=3600.0),
    )
    score: float | None = None
    for _ in range(50):
        await asyncio.sleep(0.02)
        async with store.engine.acquire() as conn:
            raw = await conn.fetchval(
                "SELECT proves_authority FROM inquiries WHERE id = $1",
                claim,
            )
        score = convert(raw, float) if raw is not None else None
        if score is not None:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert score is not None


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
