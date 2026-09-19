"""``Store.find_similar`` over the in-process PGlite substrate.

A real embedding model is not a dependency (``pyproject.toml``: "NEVER ADD MORE
DEPENDENCIES"), and a network one could not run here anyway. So these tests use
:class:`_PlacedEmbedder`, which maps known strings to hand-placed unit vectors:
deterministic, offline, and -- unlike ``StubEmbedder``'s hash -- semantically
ordered, so "is the nearest row the right one" is a meaningful assertion.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import math
import uuid

import pytest
import pytest_asyncio

from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.embedder import StubEmbedder
from trackinizer.server.store.core import Store
from trackinizer.server.store.shared import embeddable_text
from trackinizer.wire.bodies import SubmitBelief, SubmitExperiment


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trackinizer.lib.postgres import PGliteEngine


def _unit(angle: float) -> list[float]:
    """Return a unit vector on the first two axes at ``angle`` radians.

    Placing every vector on one circle makes cosine distance a pure function
    of angular separation, so a test can state "these two are near, that one
    is far" and have the arithmetic agree.
    """
    vec = [0.0] * StubEmbedder.dim
    vec[0] = math.cos(angle)
    vec[1] = math.sin(angle)
    return vec


class _PlacedEmbedder:
    """Deterministic, semantically ordered embedder for tests.

    Each entry in ``places`` maps a substring to an angle; text is embedded at
    the angle of the first substring it contains. Text matching nothing lands
    at a far angle, so an unrelated query cannot accidentally rank first.
    """

    name = "placed"
    dim = StubEmbedder.dim
    is_semantic = True

    def __init__(self, places: dict[str, float]) -> None:
        self._places = places

    async def embed(self, text: str) -> list[float]:
        lowered = text.lower()
        for needle, angle in self._places.items():
            if needle in lowered:
                return _unit(angle)
        return _unit(math.pi)  # antipodal to angle 0: maximally far


# "momentum" and "trend following" are near-synonyms here, so a query using
# one must retrieve a row titled with the other -- which substring search
# cannot do. "volatility" sits a quarter turn away as a genuine distractor.
PLACES = {
    "momentum": 0.00,
    "trend following": 0.05,
    "transaction cost": 1.20,
    "fees": 1.25,
    "volatility": math.pi / 2,
}


@pytest_asyncio.fixture(loop_scope="session")
async def store(pglite_engine: PGliteEngine) -> AsyncIterator[Store]:
    await reset_schema(pglite_engine)
    store = Store(pglite_engine, embed=_PlacedEmbedder(PLACES))
    await store.bootstrap()
    yield store


async def _belief(
    store: Store,
    title: str,
    description: str | None = None,
) -> uuid.UUID:
    return await store.submit_belief(
        SubmitBelief(
            title=title,
            description=description,
            account="tester@example.com",
            idempotency_key=uuid.uuid4(),
        ),
        actor="tester",
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_paraphrase_retrieves_the_row_substring_search_misses(
    store: Store,
) -> None:
    """A query sharing NO words with the title still ranks it first."""
    await _belief(store, "Momentum factor decays in small caps")
    await _belief(store, "Volatility clustering persists across regimes")

    hits = await store.find_similar("trend following stopped working", limit=2)

    assert hits, "expected at least one match"
    top_title = hits[0][0].title
    assert "Momentum" in top_title, f"ranked {top_title!r} first"
    # The premise: the shipped ILIKE path cannot do this.
    assert "trend following" not in top_title.lower()


@pytest.mark.asyncio(loop_scope="session")
async def test_distance_is_ascending_and_cosine_bounded(
    store: Store,
) -> None:
    """Results come back nearest-first with distances in pgvector's range."""
    await _belief(store, "Momentum factor decays in small caps")
    await _belief(store, "Volatility clustering persists across regimes")

    hits = await store.find_similar("momentum", limit=2)

    distances = [d for _, d in hits]
    assert distances == sorted(distances), f"not nearest-first: {distances}"
    assert all(0.0 <= d <= 2.0 for d in distances), distances


@pytest.mark.asyncio(loop_scope="session")
async def test_kind_filter_excludes_the_experiment_that_proves_a_belief(
    store: Store,
) -> None:
    """Cross-kind near-duplicates are by design, so the filter is load-bearing.

    A Belief and the Experiment measuring it describe the same thing, so
    they embed close together. Searching for comparable *Beliefs* must not
    surface the Experiment.
    """
    await _belief(store, "Momentum factor decays in small caps")
    await store.submit_experiment(
        SubmitExperiment(
            title="Momentum decay measured 2020-2025",
            account="tester@example.com",
            outcome="sharpe 0.9 -> 0.1",
            idempotency_key=uuid.uuid4(),
        ),
        actor="tester",
    )

    unfiltered = await store.find_similar("momentum", limit=5)
    beliefs = await store.find_similar("momentum", kind="Belief", limit=5)

    assert {type(i).__name__ for i, _ in unfiltered} == {"Belief", "Experiment"}
    assert {type(i).__name__ for i, _ in beliefs} == {"Belief"}


@pytest.mark.asyncio(loop_scope="session")
async def test_description_is_indexed_not_only_title(
    store: Store,
) -> None:
    """``design.md`` promises title/description; the row must match on either."""
    await _belief(
        store,
        "Signal profitability is eroded",
        description="Net of fees the alpha disappears entirely.",
    )
    await _belief(store, "Volatility clustering persists across regimes")

    # "fees" appears ONLY in the description.
    hits = await store.find_similar("fees", kind="Belief", limit=1)

    assert hits
    assert "profitability" in hits[0][0].title


@pytest.mark.asyncio(loop_scope="session")
async def test_editing_the_description_re_embeds_the_row(
    store: Store,
) -> None:
    """A stale vector after a description edit would silently break search."""
    target = await _belief(store, "Some claim", description="About volatility.")
    await _belief(store, "Another unrelated claim", description="About fees.")

    # Re-point the description at a different concept.
    await store.set_description(target, "Now about momentum.", actor="tester")

    hits = await store.find_similar("momentum", kind="Belief", limit=1)
    assert hits
    assert hits[0][0].id == target


@pytest.mark.asyncio(loop_scope="session")
async def test_refuses_a_non_semantic_embedder(
    pglite_engine: PGliteEngine,
) -> None:
    """Ranking hash vectors would look like a result while being noise."""
    stub_store = Store(pglite_engine, embed=StubEmbedder())

    with pytest.raises(ValueError, match="does not support semantic search"):
        await stub_store.find_similar("anything")


@pytest.mark.asyncio(loop_scope="session")
async def test_unknown_model_name_is_rejected(store: Store) -> None:
    """Silently falling back to another embedder would mix vector spaces."""
    with pytest.raises(ValueError, match="no embedder named"):
        await store.find_similar("momentum", model="not-registered")


@pytest.mark.asyncio(loop_scope="session")
async def test_empty_table_returns_empty_not_an_error(
    store: Store,
) -> None:
    """Nothing embedded yet is a normal state, not a failure."""
    assert await store.find_similar("momentum") == []


@pytest.mark.asyncio(loop_scope="session")
async def test_accepts_a_caller_supplied_connection(
    store: Store,
) -> None:
    """PGlite is single-connection: re-entrant acquire raises, so callers
    already holding it must be able to pass it through.
    """
    await _belief(store, "Momentum factor decays in small caps")

    async with store.engine.acquire() as conn:
        hits = await store.find_similar("momentum", limit=1, conn=conn)

    assert hits


def test_embeddable_text_joins_title_and_description() -> None:
    """One composer, so every write path indexes a row identically."""
    assert embeddable_text("A claim", "The detail.") == "A claim. The detail."


def test_embeddable_text_is_title_only_when_description_is_blank() -> None:
    """An absent or whitespace-only description contributes nothing."""
    assert embeddable_text("A claim", None) == "A claim"
    assert embeddable_text("A claim", "   ") == "A claim"


def test_embeddable_text_does_not_double_punctuate() -> None:
    """A title already ending in punctuation keeps its own terminator."""
    assert embeddable_text("A claim?", "Detail.") == "A claim? Detail."
