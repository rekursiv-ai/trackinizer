"""Genuine-concurrency acceptance tests for atomic issue acquisition.

PGlite is single-writer, so ``claim_next_issue_pglite_test.py`` proves the
claim's *logic* is correct but cannot prove ``SKIP LOCKED`` matters: a naive
read-then-write claim could pass there by accident, since two callers are
never truly inside the query at the same instant. These tests run against
real PostgreSQL with a wide-enough connection pool that ``asyncio.gather``
produces truly simultaneous backends, so a regression to blind claim-by-PUT
would show up here as duplicate/lost claims even though the PGlite suite
stayed green.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio

import pytest
import pytest_asyncio

from trackinizer.lib import postgres
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.notify import NOTIFY_CHANNEL
from trackinizer.server.store.core import Store
from trackinizer.wire.bodies import SubmitIssue


if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from uuid import UUID


# Wide enough that 32 claimants each get their own backend connection --
# the shared ``integ_engine`` fixture's pool (max_size=8) would force most
# of them to queue, collapsing exactly the parallelism this file exists to
# exercise.
_WIDE_POOL_SIZE = 40


@pytest_asyncio.fixture(loop_scope="session")
async def wide_store(pg_dsn: str) -> AsyncIterator[Store]:
    """Return a Store on its own wide-pool engine, sharing the session's Postgres."""
    async with postgres.PostgresEngine(
        dsn=pg_dsn,
        listen_channel=NOTIFY_CHANNEL,
        max_size=_WIDE_POOL_SIZE,
    ) as engine:
        store = Store(engine, embed=StubEmbedder())
        await store.bootstrap()  # Idempotent: CREATE TABLE IF NOT EXISTS.
        async with engine.acquire() as conn:
            await conn.execute("TRUNCATE inquiries, change_log CASCADE")
        yield store


async def _submit_issue(store: Store, title: str) -> UUID:
    return await store.submit_issue(
        SubmitIssue(account="tester@example.com", title=title),
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_32_true_concurrent_claimants_on_32_issues_collide_on_none(
    wide_store: Store,
) -> None:
    """32 backends racing 32 rows at once: 32 winners, all distinct."""
    issues = [await _submit_issue(wide_store, f"Task {i}") for i in range(32)]

    async def claim(owner: str) -> UUID | None:
        claimed = await wide_store.claim_next_issue(owner=owner, actor=owner)
        return claimed.id if claimed is not None else None

    results = await asyncio.gather(*(claim(f"worker-{i}") for i in range(32)))

    assert None not in results
    assert len(set(results)) == 32
    assert set(results) == set(issues)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_32_true_concurrent_claimants_on_4_issues_exactly_4_win(
    wide_store: Store,
) -> None:
    """Far more concurrent claimants than issues: exactly as many winners as rows."""
    issues = [await _submit_issue(wide_store, f"Task {i}") for i in range(4)]

    async def claim(owner: str) -> UUID | None:
        claimed = await wide_store.claim_next_issue(owner=owner, actor=owner)
        return claimed.id if claimed is not None else None

    results = await asyncio.gather(*(claim(f"worker-{i}") for i in range(32)))

    won = [r for r in results if r is not None]
    assert len(won) == 4
    assert set(won) == set(issues)
    assert results.count(None) == 28


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
