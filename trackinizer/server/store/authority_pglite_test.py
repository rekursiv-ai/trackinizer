"""``recompute_authority`` ranks each relation graph, against PGlite.

The sweep's promise -- one score column per relation, NULL where the graph does
not reach, a depended-on node outranking its dependents -- only holds against a
real edge graph and the real column writes, so it is exercised end to end here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
import pytest_asyncio

from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.store.core import Store
from trackinizer.wire.bodies import SubmitBelief, SubmitIssue, SubmitPaper


if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from uuid import UUID

    from trackinizer.lib.postgres import PGliteEngine


@pytest_asyncio.fixture(loop_scope="session")
async def store(pglite_engine: PGliteEngine) -> AsyncIterator[Store]:
    """Return a bootstrapped Store over the session's shared PGlite engine."""
    await reset_schema(pglite_engine)
    built = Store(pglite_engine, embed=StubEmbedder())
    await built.bootstrap()
    yield built


async def _authority(store: Store, target_id: UUID, column: str) -> float | None:
    async with store.engine.acquire() as conn:
        return cast(
            "float | None",
            await conn.fetchval(
                f"SELECT {column} FROM inquiries WHERE id = $1",  # noqa: S608 -- column from a fixed test literal.
                target_id,
            ),
        )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_cited_belief_outranks_its_citers(store: Store) -> None:
    claim = await store.submit_belief(
        SubmitBelief(account="t@example.com", title="Load-bearing claim"),
    )
    citers = [
        await store.submit_paper(SubmitPaper(account="t@example.com", title=f"P{i}"))
        for i in range(3)
    ]
    for paper in citers:
        await store.add_edge(
            from_id=paper,
            to_id=claim,
            edge_kind="proves",
            actor="t",
            valence=0.8,
        )

    await store.recompute_authority()

    claim_score = await _authority(store, claim, "proves_authority")
    assert claim_score is not None
    for paper in citers:
        citer_score = await _authority(store, paper, "proves_authority")
        # A citer with no inbound proves edge is not reached, so it is NULL.
        assert citer_score is None
    assert claim_score > 0.0


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_unreached_nodes_are_null(store: Store) -> None:
    lonely = await store.submit_belief(
        SubmitBelief(account="t@example.com", title="Uncited"),
    )
    await store.recompute_authority()
    assert await _authority(store, lonely, "proves_authority") is None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_issue_authority_unions_requires_and_narrows(store: Store) -> None:
    prereq = await store.submit_issue(
        SubmitIssue(account="t@example.com", title="Base"),
    )
    dependent = await store.submit_issue(
        SubmitIssue(account="t@example.com", title="Needs base"),
    )
    await store.add_edge(
        from_id=dependent,
        to_id=prereq,
        edge_kind="requires",
        actor="t",
    )

    await store.recompute_authority()

    # The prerequisite is depended upon, so it carries issue authority; the
    # score lands in the shared issue_authority column, not a proves one.
    assert await _authority(store, prereq, "issue_authority") is not None
    assert await _authority(store, prereq, "proves_authority") is None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_authority_is_stable_under_unrelated_rows(store: Store) -> None:
    """A cited node's score must not collapse as unrelated rows accrue.

    Regression for the dilution bug: ranking the whole inquiry table spread the
    teleport mass over every row, so a well-cited node's score shrank toward
    zero purely from table growth. Ranking the relation's own subgraph keeps it
    stable.
    """
    claim = await store.submit_belief(
        SubmitBelief(account="t@example.com", title="Cited under noise"),
    )
    paper = await store.submit_paper(
        SubmitPaper(account="t@example.com", title="Citer under noise"),
    )
    await store.add_edge(
        from_id=paper,
        to_id=claim,
        edge_kind="proves",
        actor="t",
        valence=0.8,
    )
    await store.recompute_authority()
    before = await _authority(store, claim, "proves_authority")

    for i in range(40):
        await store.submit_issue(
            SubmitIssue(account="t@example.com", title=f"unrelated noise {i}"),
        )
    await store.recompute_authority()
    after = await _authority(store, claim, "proves_authority")

    assert before is not None
    assert after == pytest.approx(before)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_losing_last_edge_resets_score_to_null(store: Store) -> None:
    """A node that drops its last inbound edge falls back to NULL next sweep."""
    claim = await store.submit_belief(
        SubmitBelief(account="t@example.com", title="Transiently cited"),
    )
    paper = await store.submit_paper(
        SubmitPaper(account="t@example.com", title="Fleeting citer"),
    )
    await store.add_edge(
        from_id=paper,
        to_id=claim,
        edge_kind="proves",
        actor="t",
        valence=0.5,
    )
    await store.recompute_authority()
    assert await _authority(store, claim, "proves_authority") is not None

    await store.remove_edge(
        from_id=paper,
        to_id=claim,
        edge_kind="proves",
        actor="t",
    )
    await store.recompute_authority()
    assert await _authority(store, claim, "proves_authority") is None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_recompute_is_idempotent(store: Store) -> None:
    claim = await store.submit_belief(
        SubmitBelief(account="t@example.com", title="Stable claim"),
    )
    paper = await store.submit_paper(SubmitPaper(account="t@example.com", title="Cite"))
    await store.add_edge(
        from_id=paper,
        to_id=claim,
        edge_kind="proves",
        actor="t",
        valence=0.5,
    )

    await store.recompute_authority()
    first = await _authority(store, claim, "proves_authority")
    await store.recompute_authority()
    second = await _authority(store, claim, "proves_authority")

    assert first == pytest.approx(second)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
