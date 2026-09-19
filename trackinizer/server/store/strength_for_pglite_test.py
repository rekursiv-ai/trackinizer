"""``Store.strength_for`` over the in-process PGlite substrate.

Expected values are precomputed with a plain Python re-implementation of the
same Euler-based formula (``1 - (1 - w**2) / (1 + w*exp(E))``, Amgoud &
Ben-Naim, IJCAI 2018) rather than hand-rounded by eye, so a test failure means
the implementation disagrees with the formula, not that someone's arithmetic
was off by a rounding step.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import math
import uuid

import pytest
import pytest_asyncio

from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.embedder import StubEmbedder


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trackinizer.lib.postgres import PGliteEngine
from trackinizer.server.store.core import Store
from trackinizer.wire.bodies import SubmitBelief, SubmitExperiment, SubmitPaper


def _ebs(support: float, attack: float, *, base: float = 0.5) -> float:
    """Compute reference Euler-based strength, independent of the implementation."""
    energy = support - attack
    return 1 - (1 - base * base) / (1 + base * math.exp(energy))


@pytest_asyncio.fixture(loop_scope="session")
async def store(pglite_engine: PGliteEngine) -> AsyncIterator[Store]:
    await reset_schema(pglite_engine)
    store = Store(pglite_engine, embed=StubEmbedder())
    await store.bootstrap()
    yield store


async def _belief(store: Store, title: str = "A claim") -> uuid.UUID:
    return await store.submit_belief(
        SubmitBelief(
            title=title,
            account="tester@example.com",
            idempotency_key=uuid.uuid4(),
        ),
        actor="tester",
    )


async def _paper(store: Store, title: str = "A paper") -> uuid.UUID:
    return await store.submit_paper(
        SubmitPaper(
            title=title,
            account="tester@example.com",
            idempotency_key=uuid.uuid4(),
        ),
        actor="tester",
    )


async def _complete_experiment(store: Store, title: str = "An experiment") -> uuid.UUID:
    experiment_id = await store.submit_experiment(
        SubmitExperiment(
            title=title,
            account="tester@example.com",
            outcome="n/a",
            idempotency_key=uuid.uuid4(),
        ),
        actor="tester",
    )
    await store.set_status(experiment_id, "complete", actor="tester")
    return experiment_id


@pytest.mark.asyncio(loop_scope="session")
async def test_no_evidence_is_neutral(store: Store) -> None:
    """A claim with nothing citing it sits exactly at the base score."""
    belief_id = await _belief(store)

    result = await store.strength_for(belief_id)

    assert result is not None
    assert result.strength == pytest.approx(0.5)


@pytest.mark.asyncio(loop_scope="session")
async def test_equal_proof_and_disproof_from_symmetric_citers_cancel(
    store: Store,
) -> None:
    """Two Papers (both non-claimable, so both count at face value) with
    opposite valence of equal magnitude net to exactly neutral.
    """
    belief_id = await _belief(store)
    paper_for = await _paper(store, "Motivating paper")
    paper_against = await _paper(store, "Contradicting paper")
    await store.add_edge(
        from_id=paper_for,
        to_id=belief_id,
        edge_kind="proves",
        valence=0.5,
        actor="tester",
    )
    await store.add_edge(
        from_id=paper_against,
        to_id=belief_id,
        edge_kind="proves",
        valence=-0.5,
        actor="tester",
    )

    result = await store.strength_for(belief_id)

    assert result is not None
    assert result.strength == pytest.approx(_ebs(0.5, 0.5))
    assert result.strength == pytest.approx(0.5)


@pytest.mark.asyncio(loop_scope="session")
async def test_real_example_sh_paper_and_experiment_mix(store: Store) -> None:
    """Real ``example.sh`` shape: Paper proves, Experiment disproves.

    Not exactly neutral, unlike the symmetric-Papers case above: Experiment
    is itself a claimable kind (the schema lets a proves/favors edge target
    it, same as Belief), so a bare Experiment with no evidence of its own
    recurses to the neutral base score (0.5) rather than counting at face
    value like a Paper does. The disproof is therefore only half as heavy
    as the proof even though both edges carry |valence| = 0.5.
    """
    belief_id = await _belief(store)
    paper_id = await _paper(store)
    experiment_id = await _complete_experiment(store)
    await store.add_edge(
        from_id=paper_id,
        to_id=belief_id,
        edge_kind="proves",
        valence=0.5,
        actor="tester",
    )
    await store.add_edge(
        from_id=experiment_id,
        to_id=belief_id,
        edge_kind="proves",
        valence=-0.5,
        actor="tester",
    )

    result = await store.strength_for(belief_id)

    assert result is not None
    # support = 0.5 * 1.0 (Paper, face value); attack = 0.5 * 0.5 (Experiment
    # recurses to its own neutral base, having no evidence of its own).
    assert result.strength == pytest.approx(_ebs(0.5 * 1.0, 0.5 * _ebs(0, 0)))


@pytest.mark.asyncio(loop_scope="session")
async def test_two_proofs_stack(store: Store) -> None:
    """Real ``example.sh`` shape: two proofs, no attacks.

    Both citers are bare Experiments (no evidence of their own), so each
    recurses to the neutral base score 0.5 before its valence is applied.
    """
    belief_id = await _belief(store)
    experiment_1 = await _complete_experiment(store, "First experiment")
    experiment_2 = await _complete_experiment(store, "Second experiment")
    await store.add_edge(
        from_id=experiment_1,
        to_id=belief_id,
        edge_kind="proves",
        valence=0.5,
        actor="tester",
    )
    await store.add_edge(
        from_id=experiment_2,
        to_id=belief_id,
        edge_kind="proves",
        valence=0.95,
        actor="tester",
    )

    result = await store.strength_for(belief_id)

    assert result is not None
    bare_experiment_strength = _ebs(0, 0)
    assert result.strength == pytest.approx(
        _ebs((0.5 + 0.95) * bare_experiment_strength, 0),
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_favors_edges_never_count(store: Store) -> None:
    """``favors`` is context, not a vote -- it must not move the score."""
    belief_id = await _belief(store)
    paper_id = await _paper(store)
    await store.add_edge(
        from_id=paper_id,
        to_id=belief_id,
        edge_kind="favors",
        valence=0.99,
        actor="tester",
    )

    result = await store.strength_for(belief_id)

    assert result is not None
    assert result.strength == pytest.approx(0.5)


@pytest.mark.asyncio(loop_scope="session")
async def test_citing_beliefs_own_strength_is_folded_in_recursively(
    store: Store,
) -> None:
    """A proof resting on a well-evidenced Belief should count more.

    Belief A has two proofs of its own (strength ~0.76). Belief B is proved
    by A alone at the same edge weight (0.5) a flat, non-recursive citer
    would carry. If A's own computed strength is folded in, B lands well
    below what a flat weight-1.0 citer would produce -- the two are
    precomputed far enough apart that only a real recursive walk passes.
    """
    belief_a = await _belief(store, "Belief A")
    belief_b = await _belief(store, "Belief B")
    experiment_1 = await _complete_experiment(store, "Proves A, first")
    experiment_2 = await _complete_experiment(store, "Proves A, second")
    await store.add_edge(
        from_id=experiment_1,
        to_id=belief_a,
        edge_kind="proves",
        valence=0.5,
        actor="tester",
    )
    await store.add_edge(
        from_id=experiment_2,
        to_id=belief_a,
        edge_kind="proves",
        valence=0.95,
        actor="tester",
    )
    # Only a proven Belief counts as a currently-true citer.
    await store.set_judgement(belief_a, "proven", actor="tester")
    await store.add_edge(
        from_id=belief_a,
        to_id=belief_b,
        edge_kind="proves",
        valence=0.5,
        actor="tester",
    )

    result_a = await store.strength_for(belief_a)
    result_b = await store.strength_for(belief_b)

    assert result_a is not None
    bare_experiment_strength = _ebs(0, 0)
    assert result_a.strength == pytest.approx(
        _ebs((0.5 + 0.95) * bare_experiment_strength, 0),
    )
    assert result_b is not None
    assert result_b.strength == pytest.approx(_ebs(0.5 * result_a.strength, 0))
    # Distinct from what a flat (non-recursive) weight-1.0 citer would give,
    # so a regression to "always contribute 1.0" fails this assertion too.
    assert result_b.strength != pytest.approx(_ebs(0.5, 0))


@pytest.mark.asyncio(loop_scope="session")
async def test_unproven_citing_belief_does_not_count_yet(store: Store) -> None:
    """A Belief citing another only votes once it is itself proven."""
    belief_a = await _belief(store, "Not yet proven")
    belief_b = await _belief(store, "Cited by A")
    await store.add_edge(
        from_id=belief_a,
        to_id=belief_b,
        edge_kind="proves",
        valence=0.9,
        actor="tester",
    )

    result = await store.strength_for(belief_b)

    assert result is not None
    assert result.strength == pytest.approx(0.5)


@pytest.mark.asyncio(loop_scope="session")
async def test_superseded_citer_is_excluded(store: Store) -> None:
    """A superseded Experiment's citation stops counting, same rule as
    ``proves_belief``.
    """
    belief_id = await _belief(store, "Superseded-evidence claim")
    old_experiment = await _complete_experiment(store, "Old measurement")
    new_experiment = await _complete_experiment(store, "Corrected measurement")
    await store.add_edge(
        from_id=old_experiment,
        to_id=belief_id,
        edge_kind="proves",
        valence=0.9,
        actor="tester",
    )

    before = await store.strength_for(belief_id)
    assert before is not None
    # old_experiment is a bare Experiment (claimable, no evidence of its
    # own), so it recurses to the neutral base score before its valence
    # is applied -- same as the other Experiment-sourced tests above.
    assert before.strength == pytest.approx(_ebs(0.9 * _ebs(0, 0), 0))

    await store.add_edge(
        from_id=new_experiment,
        to_id=old_experiment,
        edge_kind="supersedes",
        actor="tester",
    )

    after = await store.strength_for(belief_id)
    assert after is not None
    assert after.strength == pytest.approx(0.5)


@pytest.mark.asyncio(loop_scope="session")
async def test_unknown_id_returns_none(store: Store) -> None:
    """Nothing at that id is a normal state, not a failure."""
    assert await store.strength_for(uuid.uuid4()) is None


@pytest.mark.asyncio(loop_scope="session")
async def test_accepts_a_caller_supplied_connection(store: Store) -> None:
    """PGlite is single-connection: re-entrant acquire raises, so callers
    already holding it must be able to pass it through.
    """
    belief_id = await _belief(store)

    async with store.engine.acquire() as conn:
        result = await store.strength_for(belief_id, conn=conn)

    assert result is not None
    assert result.strength == pytest.approx(0.5)
