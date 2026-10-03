"""The neighbourhood of one inquiry, read on PGlite.

Every test reads the one small graph the ``graph`` fixture builds, drawn below
with each node's ``created`` minute. An arrow is a stored ``requires`` edge,
``from_id`` to ``to_id``; the double arrow is two, ``requires`` and ``narrows``.
``B`` and ``C`` were made in the same minute. ``U`` links to nothing::

    A(1) --> F(5) --> B(3) --> E(0)
    ^         ^^
    D(9)      C(3)
    ^
    G(7)                U(8)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.store.core import Store
from trackinizer.server.store.graph_focus import read_neighbourhood


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trackinizer.lib.postgres import PGliteEngine


# Ids in name order, so a tie in ``created`` goes to the later letter.
A, B, C, D, E, F, G, U = (
    UUID(f"00000000-0000-4000-8000-{n:012d}") for n in range(1, 9)
)
_MINUTE: Final = {A: 1, B: 3, C: 3, D: 9, E: 0, F: 5, G: 7, U: 8}
_EDGES: Final = (
    (A, F, "requires"),
    (F, B, "requires"),
    (C, F, "requires"),
    (C, F, "narrows"),
    (D, A, "requires"),
    (B, E, "requires"),
    (G, D, "requires"),
)


@pytest_asyncio.fixture(loop_scope="session")
async def graph(pglite_engine: PGliteEngine) -> AsyncIterator[PGliteEngine]:
    """Return a PGlite engine holding the graph the module docstring draws."""
    await reset_schema(pglite_engine)
    await Store(pglite_engine, embed=StubEmbedder()).bootstrap()
    async with pglite_engine.acquire() as conn:
        for seq, (node, minute) in enumerate(_MINUTE.items(), start=1):
            await conn.execute(
                "INSERT INTO inquiries (id, kind, seq, account, title, created)"
                " VALUES ($1, 'Issue', $2, 'tester@example.com', $3, $4)",
                node,
                seq,
                f"node {seq}",
                datetime(2026, 10, 2, 0, minute, tzinfo=UTC),
            )
        for from_id, to_id, edge_kind in _EDGES:
            await conn.execute(
                "INSERT INTO edges (from_id, from_kind, to_id, to_kind, edge_kind)"
                " VALUES ($1, 'Issue', $2, 'Issue', $3)",
                from_id,
                to_id,
                edge_kind,
            )
    yield pglite_engine


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_all_of_one_hop_comes_before_the_next_newest_first_within_each(
    graph: PGliteEngine,
) -> None:
    # D is the newest node of all, yet every hop-1 node comes before it.
    assert await _walk(graph, F, hops=3, limit=100) == [
        (F, 0),
        (C, 1),
        (B, 1),
        (A, 1),
        (D, 2),
        (E, 2),
        (G, 3),
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_tie_in_created_goes_to_the_larger_id(graph: PGliteEngine) -> None:
    # B and C were made in the same minute; the cut keeps C, the larger id.
    assert await _walk(graph, F, hops=1, limit=2) == [(F, 0), (C, 1)]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_limit_counts_the_focus_and_cuts_the_farthest_then_the_oldest(
    graph: PGliteEngine,
) -> None:
    assert await _walk(graph, F, hops=3, limit=1) == [(F, 0)]
    assert await _walk(graph, F, hops=3, limit=3) == [(F, 0), (C, 1), (B, 1)]
    assert await _walk(graph, F, hops=3, limit=5) == [
        (F, 0),
        (C, 1),
        (B, 1),
        (A, 1),
        (D, 2),
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_neighbour_joined_by_two_edges_takes_one_place(
    graph: PGliteEngine,
) -> None:
    # C, first in hop 1, joins F twice; B still makes the cut at 3.
    assert await _walk(graph, F, hops=1, limit=3) == [(F, 0), (C, 1), (B, 1)]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_hops_bounds_the_walk(graph: PGliteEngine) -> None:
    assert await _walk(graph, F, hops=1, limit=100) == [
        (F, 0),
        (C, 1),
        (B, 1),
        (A, 1),
    ]
    assert await _walk(graph, F, hops=2, limit=100) == [
        (F, 0),
        (C, 1),
        (B, 1),
        (A, 1),
        (D, 2),
        (E, 2),
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_edges_lead_both_ways(graph: PGliteEngine) -> None:
    # F -> B arrives at B, and B -> E leaves it.
    assert await _walk(graph, B, hops=1, limit=100) == [(B, 0), (F, 1), (E, 1)]
    # E's one edge arrives; walking on from B leaves along F's two arrivals.
    assert await _walk(graph, E, hops=3, limit=100) == [
        (E, 0),
        (B, 1),
        (F, 2),
        (C, 3),
        (A, 3),
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_focus_with_no_edges_is_its_own_neighbourhood(
    graph: PGliteEngine,
) -> None:
    assert await _walk(graph, U, hops=3, limit=100) == [(U, 0)]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_unknown_focus_has_no_neighbourhood(graph: PGliteEngine) -> None:
    assert await _walk(graph, uuid4(), hops=3, limit=100) == []


async def _walk(
    engine: PGliteEngine,
    focus: UUID,
    *,
    hops: int,
    limit: int,
) -> list[tuple[UUID, int]]:
    """Return ``focus``'s neighbourhood as ``(id, hops)`` pairs, in its order."""
    async with engine.acquire() as conn:
        found = await read_neighbourhood(conn, focus, hops=hops, limit=limit)
    return list(found.items())


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
