"""The timeline projection's lead climb and anchor rule, over a scripted graph."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from trackinizer.server.visuals.timeline import load_timeline


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn


_WHEN = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_lead_climb_stops_after_three_lookups_on_an_endless_chain() -> None:
    """Each level costs one lookup; a deeper chain is cut at three."""
    graph = _Graph()
    chain = [graph.add("Issue", title=f"Level {index}") for index in range(7)]
    for child, parent in zip(chain[1:], chain, strict=False):
        graph.parent[child] = parent
    body = await graph.load(chain[-1])
    assert body is not None
    assert _titles(body) == ["Level 3", "Level 4", "Level 5"]
    assert graph.narrows_lookups == 3


@pytest.mark.asyncio
async def test_a_non_issue_anchor_is_its_own_nearest_lead_and_takes_a_level() -> None:
    """A Belief's anchor Issue ends the leads and leaves two levels for its parents."""
    graph = _Graph()
    chain = [graph.add("Issue", title=f"Level {index}") for index in range(5)]
    for child, parent in zip(chain[1:], chain, strict=False):
        graph.parent[child] = parent
    belief = graph.add("Belief", title="Claim")
    graph.producer[belief] = chain[-1]
    body = await graph.load(belief)
    assert body is not None
    assert _titles(body) == ["Level 2", "Level 3", "Level 4"]
    assert graph.narrows_lookups == 2
    assert body["root_results"] == []


@pytest.mark.asyncio
async def test_a_cycle_lists_each_issue_once() -> None:
    """A `narrows` loop ends at the first repeat and never lists the record."""
    graph = _Graph()
    first, second = (
        graph.add("Issue", title="First"),
        graph.add("Issue", title="Second"),
    )
    graph.parent[first] = second
    graph.parent[second] = first
    body = await graph.load(first)
    assert body is not None
    assert _titles(body) == ["Second"]


@pytest.mark.asyncio
async def test_an_unlinked_record_is_alone_and_a_missing_one_is_none() -> None:
    """A Paper with no producer Issue issues no lead or direction lookup."""
    graph = _Graph()
    paper = graph.add("Paper", title="A paper")
    body = await graph.load(paper)
    assert body is not None
    assert (body["issue"], body["leads"], body["directions"]) == (None, [], [])
    assert graph.narrows_lookups == 0
    graph.conn.fetch.assert_not_called()
    assert await graph.load(uuid4()) is None


@pytest.mark.asyncio
async def test_an_experiment_rides_on_its_producer_and_is_the_selected_result() -> None:
    """The Experiment stays the target; its producer Issue is the anchor."""
    graph = _Graph()
    lead, producer = (
        graph.add("Issue", title="Lead"),
        graph.add("Issue", title="Producer"),
    )
    graph.parent[producer] = lead
    experiment = graph.add("Experiment", title="Run")
    graph.producer[experiment] = producer
    body = await graph.load(experiment)
    assert body is not None
    assert cast(dict[str, object], body["target"])["title"] == "Run"
    assert cast(dict[str, object], body["issue"])["title"] == "Producer"
    assert _titles(body) == ["Lead"]
    selected = cast(dict[str, object], body["selected_result"])
    assert cast(dict[str, object], selected["record"])["title"] == "Run"
    assert selected["evidence"] == []


class _Graph:
    """A few records, their `narrows` parent and `produced_by` Issue, as a Conn."""

    def __init__(self) -> None:
        self.rows: dict[UUID, dict[str, object]] = {}
        self.parent: dict[UUID, UUID] = {}
        self.producer: dict[UUID, UUID] = {}
        self.narrows_lookups = 0
        self.conn = AsyncMock()
        self.conn.fetchrow.side_effect = self._fetchrow
        self.conn.fetch.return_value = []

    def add(self, kind: str, *, title: str) -> UUID:
        record_id = uuid4()
        self.rows[record_id] = {
            "id": record_id,
            "kind": kind,
            "seq": len(self.rows) + 1,
            "title": title,
            "status": "active",
            "created": _WHEN,
            "modified": _WHEN,
            "description": None,
            "outcome": None,
        }
        return record_id

    async def load(self, record_id: UUID) -> dict[str, object] | None:
        return await load_timeline(
            cast("Conn", self.conn),
            record_id,
            direction_limit=2,
            results_per_direction=1,
        )

    async def _fetchrow(self, sql: str, *args: object) -> dict[str, object] | None:
        source = cast(UUID, args[0])
        if "edge_kind='narrows'" in sql:
            self.narrows_lookups += 1
            return self.rows.get(self.parent.get(source, uuid4()))
        if "edge_kind = 'produced_by'" in sql:
            produced = self.producer.get(source)
            return None if produced is None else {"id": produced}
        return self.rows.get(source)


def _titles(body: dict[str, object]) -> list[object]:
    return [
        cast(dict[str, object], lead)["title"]
        for lead in cast(list[object], body["leads"])
    ]


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
