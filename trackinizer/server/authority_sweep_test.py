"""The authority sweep loop coalesces edge-change bursts and stays alive."""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio
import logging

from treekle import from_plain

import pytest

from trackinizer.conftest import make_store
from trackinizer.server import authority_sweep
from trackinizer.server.authority_sweep import (
    authority_sweep_loop,
    edge_change_count,
)
from trackinizer.wire.bodies import SubmitBelief, SubmitPaper


if TYPE_CHECKING:
    from collections.abc import Awaitable

    from trackinizer.server.store.core import Store


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
async def test_loop_recomputes_on_startup_then_can_be_cancelled(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = await store.submit_belief(SubmitBelief(account="t@e.com", title="Claim"))
    paper = await store.submit_paper(SubmitPaper(account="t@e.com", title="Paper"))
    await store.add_edge(
        from_id=paper,
        to_id=claim,
        edge_kind="proves",
        actor="t",
        valence=0.9,
    )

    recomputed = asyncio.Event()
    recompute = store.recompute_authority

    monkeypatch.setattr(
        store,
        "recompute_authority",
        lambda: _signalled(recomputed, recompute()),
    )
    # A long interval proves the FIRST pass is unconditional (not interval-gated):
    # the loop must write a score before its first sleep, then park.
    task = asyncio.create_task(
        authority_sweep_loop(store, min_interval_sec=3600.0, max_interval_sec=3600.0),
    )
    await asyncio.wait_for(recomputed.wait(), 5)
    async with store.engine.acquire() as conn:
        raw = await conn.fetchval(
            "SELECT proves_authority FROM inquiries WHERE id = $1",
            claim,
        )
    score = from_plain(raw, float) if raw is not None else None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert score is not None


@pytest.mark.asyncio
async def test_a_failed_sweep_is_logged_and_the_loop_retries(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, _engine = make_store()
    outcomes: list[int | BaseException] = [
        RuntimeError("database blip"),
        3,
        asyncio.CancelledError(),
    ]

    async def recompute_authority() -> int:
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(store, "recompute_authority", recompute_authority)
    caplog.set_level(logging.INFO, logger=authority_sweep.__name__)

    with pytest.raises(asyncio.CancelledError):
        await authority_sweep_loop(store, min_interval_sec=0.0, max_interval_sec=0.0)

    assert outcomes == []
    assert [r.getMessage() for r in caplog.records] == [
        "authority sweep failed; retrying next cycle",
        "authority sweep wrote 3 scores",
    ]


async def _signalled(done: asyncio.Event, work: Awaitable[int]) -> int:
    """Await ``work``, then set ``done`` however it ended."""
    try:
        return await work
    finally:
        done.set()


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
