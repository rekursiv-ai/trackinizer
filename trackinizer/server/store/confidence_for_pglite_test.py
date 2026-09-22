"""``confidence_for`` folds the currently-true ``proves`` graph, against PGlite.

Exercised against a real engine rather than a mock: the currency rule, the
recursion into claimable citers, and the log-odds fold are what the store
promises, and only a real ``proves`` graph exercises them together. Each
assertion is pinned to the reference fold in
:mod:`trackinizer.types.belief_confidence` so a formula change surfaces here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import pytest_asyncio

from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.store.core import Store
from trackinizer.types.belief_confidence import fold_confidence
from trackinizer.wire.bodies import (
    SubmitBelief,
    SubmitExperiment,
    SubmitIssue,
    SubmitPaper,
)


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


async def _proven_belief(store: Store, title: str) -> UUID:
    """Submit a Belief and mark it proven so it counts as a currently-true citer."""
    belief_id = await store.submit_belief(
        SubmitBelief(account="tester@example.com", title=title),
    )
    await store.set_judgement(belief_id, "proven", actor="tester")
    return belief_id


async def _complete_experiment(store: Store, title: str) -> UUID:
    """Submit an Experiment and complete it so it counts as a currently-true citer."""
    experiment_id = await store.submit_experiment(
        SubmitExperiment(account="tester@example.com", title=title),
    )
    await store.set_status(experiment_id, "complete", actor="tester")
    return experiment_id


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_missing_row_is_none(store: Store) -> None:
    assert await store.confidence_for(uuid4()) is None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_non_claimable_kind_is_none_not_a_bogus_neutral(store: Store) -> None:
    """An Issue has no inbound proves graph, so confidence is undefined (None).

    Regression: the walk only checked existence, so a non-claimable target
    (Issue, Paper, ...) folded an empty graph into a misleading 0.5 -- a real
    number for a question that does not apply to that kind.
    """
    issue_id = await store.submit_issue(
        SubmitIssue(account="tester@example.com", title="Not a claim"),
    )
    assert await store.confidence_for(issue_id) is None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_no_evidence_is_neutral(store: Store) -> None:
    belief_id = await _proven_belief(store, "Unsupported claim")

    assert await store.confidence_for(belief_id) == pytest.approx(0.5)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_positive_citation_lifts_confidence(store: Store) -> None:
    claim = await _proven_belief(store, "Claim with support")
    evidence = await _complete_experiment(store, "Supporting run")
    await store.add_edge(
        from_id=evidence,
        to_id=claim,
        edge_kind="proves",
        actor="tester",
        valence=0.8,
    )

    # Experiment citer is claimable, so its own (evidence-free) confidence is
    # neutral 0.5, weighting the 0.8 valence: log-odds = 0.5 * 0.8.
    assert await store.confidence_for(claim) == pytest.approx(
        fold_confidence(0.5 * 0.8),
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_symmetric_support_and_attack_cancel(store: Store) -> None:
    claim = await _proven_belief(store, "Contested claim")
    pro = await _complete_experiment(store, "For")
    con = await _complete_experiment(store, "Against")
    await store.add_edge(
        from_id=pro,
        to_id=claim,
        edge_kind="proves",
        actor="tester",
        valence=0.6,
    )
    await store.add_edge(
        from_id=con,
        to_id=claim,
        edge_kind="proves",
        actor="tester",
        valence=-0.6,
    )

    assert await store.confidence_for(claim) == pytest.approx(0.5)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_leaf_artifact_citer_counts_at_full_weight(store: Store) -> None:
    """A non-claimable citer (Paper) contributes valence * 1.0, not * its own score."""
    claim = await _proven_belief(store, "Claim cited by a paper")
    paper = await store.submit_paper(
        SubmitPaper(account="tester@example.com", title="Cited paper"),
    )
    await store.add_edge(
        from_id=paper,
        to_id=claim,
        edge_kind="proves",
        actor="tester",
        valence=0.7,
    )

    assert await store.confidence_for(claim) == pytest.approx(
        fold_confidence(1.0 * 0.7),
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_confidence_recurses_into_a_claimable_citer(store: Store) -> None:
    """A Belief citing a Belief folds the citer's own derived confidence in."""
    leaf = await _proven_belief(store, "Leaf claim")
    paper = await store.submit_paper(
        SubmitPaper(account="tester@example.com", title="Backing paper"),
    )
    await store.add_edge(
        from_id=paper,
        to_id=leaf,
        edge_kind="proves",
        actor="tester",
        valence=0.9,
    )
    root = await _proven_belief(store, "Root claim")
    await store.add_edge(
        from_id=leaf,
        to_id=root,
        edge_kind="proves",
        actor="tester",
        valence=0.5,
    )

    leaf_confidence = fold_confidence(1.0 * 0.9)
    assert await store.confidence_for(root) == pytest.approx(
        fold_confidence(leaf_confidence * 0.5),
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
