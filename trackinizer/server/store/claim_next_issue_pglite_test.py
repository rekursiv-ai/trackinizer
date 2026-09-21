"""Acceptance tests for atomic issue acquisition on the PGlite substrate.

``store.claim_next_issue`` selects and claims one Issue in a single
statement (:data:`CLAIM_NEXT_ISSUE_SQL`), so N callers racing it never
duplicate an assignment the way a read-then-write claim would. PGlite is
single-writer, so it cannot exercise genuine OS-level parallelism (see
``claim_next_issue_pg_test.py`` for that); what it does prove is that the
claim's *logic* -- eligibility, ordering, replay, rollback -- is correct
when many callers are interleaved on one connection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import asyncio
import uuid

import pytest
import pytest_asyncio

from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.store.change_id_slot import set_client_change_id
from trackinizer.server.store.core import Store
from trackinizer.types.errors import ConflictError, NotFoundError
from trackinizer.types.inquiries import Issue
from trackinizer.wire.bodies import SubmitIssue


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trackinizer.lib.postgres import PGliteEngine


@pytest_asyncio.fixture(loop_scope="session")
async def store(pglite_engine: PGliteEngine) -> AsyncIterator[Store]:
    """Return a bootstrapped Store over the session's shared PGlite engine."""
    await reset_schema(pglite_engine)
    store = Store(pglite_engine, embed=StubEmbedder())
    await store.bootstrap()
    yield store


async def _submit_issue(
    store: Store,
    title: str,
    *,
    priority: int | None = None,
    owner: str | None = None,
    status: Issue.Status | None = None,
) -> uuid.UUID:
    return await store.submit_issue(
        SubmitIssue(
            account="tester@example.com",
            title=title,
            priority=priority,
            owner=owner,
            status=status,
        ),
    )


async def _change_log_count(store: Store, *, subject_id: uuid.UUID) -> int:
    async with store.engine.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT count(*) AS n FROM change_log WHERE subject_id = $1"
            " AND kind = 'owner'",
            subject_id,
        )
    assert row is not None
    return cast(int, row["n"])


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_concurrent_claimants_never_double_claim_an_issue(
    store: Store,
) -> None:
    """4 callers racing 4 issues each get a distinct one; nothing collides."""
    issues = [await _submit_issue(store, f"Task {i}") for i in range(4)]

    async def claim(owner: str) -> uuid.UUID | None:
        claimed = await store.claim_next_issue(owner=owner, actor=owner)
        return claimed.id if claimed is not None else None

    results = await asyncio.gather(*(claim(f"worker-{i}") for i in range(4)))

    assert None not in results
    assert set(results) == set(issues)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_extra_claimants_beyond_available_issues_get_none(
    store: Store,
) -> None:
    """More claimants than issues: the excess get "nothing claimable", not an error."""
    issues = [await _submit_issue(store, f"Task {i}") for i in range(2)]

    async def claim(owner: str) -> uuid.UUID | None:
        claimed = await store.claim_next_issue(owner=owner, actor=owner)
        return claimed.id if claimed is not None else None

    results = await asyncio.gather(*(claim(f"worker-{i}") for i in range(5)))

    won = [r for r in results if r is not None]
    assert set(won) == set(issues)
    assert results.count(None) == 3


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_zero_issues_returns_none_without_error(store: Store) -> None:
    """No eligible issue at all: claim returns None cleanly."""
    claimed = await store.claim_next_issue(owner="worker-1", actor="worker-1")
    assert claimed is None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_already_owned_issue_is_never_claimed(store: Store) -> None:
    """An Issue created with an explicit owner is not "unowned" work."""
    await _submit_issue(store, "Pre-owned", owner="someone-else")
    claimed = await store.claim_next_issue(owner="worker-1", actor="worker-1")
    assert claimed is None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_inactive_issue_is_never_claimed(store: Store) -> None:
    """A closed/blocked Issue is not eligible work, unowned or not."""
    await _submit_issue(store, "Already done", status="complete")
    claimed = await store.claim_next_issue(owner="worker-1", actor="worker-1")
    assert claimed is None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_claim_honors_priority_order(store: Store) -> None:
    """Among eligible issues, the lower priority number is claimed first (P1 outranks P99)."""
    low = await _submit_issue(store, "Low priority", priority=99)
    high = await _submit_issue(store, "High priority", priority=1)

    first = await store.claim_next_issue(owner="worker-1", actor="worker-1")
    assert first is not None
    assert first.id == high

    second = await store.claim_next_issue(owner="worker-2", actor="worker-2")
    assert second is not None
    assert second.id == low


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_claim_replay_returns_same_issue_without_a_second_audit_row(
    store: Store,
) -> None:
    """A retry under the same key gets the original issue, not a new claim."""
    issue = await _submit_issue(store, "Task")
    key = uuid.uuid4()

    set_client_change_id(key)
    try:
        first = await store.claim_next_issue(owner="worker-1", actor="worker-1")
    finally:
        set_client_change_id(None)
    assert first is not None
    assert first.id == issue

    set_client_change_id(key)
    try:
        replay = await store.claim_next_issue(owner="worker-1", actor="worker-1")
    finally:
        set_client_change_id(None)
    assert replay is not None
    assert replay.id == issue

    assert await _change_log_count(store, subject_id=issue) == 1


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_replay_with_a_different_actor_raises_conflict(
    store: Store,
) -> None:
    """The key names one acquisition; replaying it as someone else is reuse."""
    await _submit_issue(store, "Task")
    key = uuid.uuid4()

    set_client_change_id(key)
    try:
        first = await store.claim_next_issue(owner="worker-1", actor="worker-1")
    finally:
        set_client_change_id(None)
    assert first is not None

    set_client_change_id(key)
    try:
        with pytest.raises(ConflictError, match="already used"):
            await store.claim_next_issue(owner="worker-1", actor="worker-2")
    finally:
        set_client_change_id(None)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_replay_after_release_raises_conflict_without_substituting(
    store: Store,
) -> None:
    """Owner cleared after the claim: replay reports the drift, not a new issue."""
    await _submit_issue(store, "Task")
    key = uuid.uuid4()

    set_client_change_id(key)
    try:
        first = await store.claim_next_issue(owner="worker-1", actor="worker-1")
    finally:
        set_client_change_id(None)
    assert first is not None

    await store.set_owner(first.id, None, actor="admin")

    set_client_change_id(key)
    try:
        with pytest.raises(ConflictError, match="no longer active and owned"):
            await store.claim_next_issue(owner="worker-1", actor="worker-1")
    finally:
        set_client_change_id(None)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_replay_after_reassignment_raises_conflict(store: Store) -> None:
    """Owner handed to someone else after the claim: replay does not paper over it."""
    await _submit_issue(store, "Task")
    key = uuid.uuid4()

    set_client_change_id(key)
    try:
        first = await store.claim_next_issue(owner="worker-1", actor="worker-1")
    finally:
        set_client_change_id(None)
    assert first is not None

    await store.set_owner(first.id, "worker-2", actor="admin")

    set_client_change_id(key)
    try:
        with pytest.raises(ConflictError, match="no longer active and owned"):
            await store.claim_next_issue(owner="worker-1", actor="worker-1")
    finally:
        set_client_change_id(None)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_replay_after_completion_raises_conflict(store: Store) -> None:
    """Issue completed after the claim: replay reports it, doesn't hide it."""
    await _submit_issue(store, "Task")
    key = uuid.uuid4()

    set_client_change_id(key)
    try:
        first = await store.claim_next_issue(owner="worker-1", actor="worker-1")
    finally:
        set_client_change_id(None)
    assert first is not None

    await store.set_status(first.id, "complete", actor="worker-1")

    set_client_change_id(key)
    try:
        with pytest.raises(ConflictError, match="no longer active and owned"):
            await store.claim_next_issue(owner="worker-1", actor="worker-1")
    finally:
        set_client_change_id(None)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_replay_after_deletion_raises_not_found(store: Store) -> None:
    """Issue deleted after the claim: replay reports it as gone, not reissued."""
    await _submit_issue(store, "Task")
    key = uuid.uuid4()

    set_client_change_id(key)
    try:
        first = await store.claim_next_issue(owner="worker-1", actor="worker-1")
    finally:
        set_client_change_id(None)
    assert first is not None

    # purge() refuses an owned row, so release it first -- same as any real
    # worker finishing up before its issue is cleaned out.
    await store.set_owner(first.id, None, actor="worker-1")
    await store.purge(first.id, actor="admin")

    set_client_change_id(key)
    try:
        with pytest.raises(NotFoundError, match="no longer exists"):
            await store.claim_next_issue(owner="worker-1", actor="worker-1")
    finally:
        set_client_change_id(None)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_temporarily_locked_issue_becomes_claimable_again(
    store: Store,
) -> None:
    """SKIP LOCKED only skips a row while its lock is held, not permanently.

    A claim that aborts before commit releases its row lock along with its
    write, so the issue returns to the pool for the next caller -- it is not
    left stranded because one caller merely glanced at it.
    """
    issue = await _submit_issue(store, "Task")

    with (
        patch.object(
            Store,
            "_emit_field_change",
            side_effect=RuntimeError("injected"),
        ),
        pytest.raises(RuntimeError, match="injected"),
    ):
        await store.claim_next_issue(owner="worker-1", actor="worker-1")

    claimed = await store.claim_next_issue(owner="worker-2", actor="worker-2")
    assert claimed is not None
    assert claimed.id == issue


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_failure_before_commit_rolls_back_the_owner_write(
    store: Store,
) -> None:
    """An error mid-claim leaves the row unowned, not half-claimed."""
    issue = await _submit_issue(store, "Task")

    with (
        patch.object(
            Store,
            "_emit_field_change",
            side_effect=RuntimeError("injected"),
        ),
        pytest.raises(RuntimeError, match="injected"),
    ):
        await store.claim_next_issue(owner="worker-1", actor="worker-1")

    row = await store.get_inquiry(issue)
    assert isinstance(row, Issue)
    assert row.owner is None


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
