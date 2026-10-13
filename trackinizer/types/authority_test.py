"""Unit tests for the pure-Python PageRank authority computation."""

from __future__ import annotations

from uuid import UUID

import pytest

from trackinizer.types.authority import Edge, pagerank, relation_edges


def _uuid(n: int) -> UUID:
    return UUID(int=n)


def test_empty_graph_is_empty() -> None:
    assert pagerank([], []) == {}


def test_scores_sum_to_one() -> None:
    nodes = [_uuid(i) for i in range(4)]
    edges = [
        Edge(dependent=nodes[0], dependency=nodes[1], weight=1.0),
        Edge(dependent=nodes[2], dependency=nodes[1], weight=1.0),
        Edge(dependent=nodes[3], dependency=nodes[2], weight=1.0),
    ]
    scores = pagerank(nodes, edges)
    assert sum(scores.values()) == pytest.approx(1.0)


def test_isolated_nodes_are_uniform() -> None:
    nodes = [_uuid(i) for i in range(3)]
    scores = pagerank(nodes, [])
    for value in scores.values():
        assert value == pytest.approx(1.0 / 3.0)


def test_a_depended_on_node_outranks_its_dependents() -> None:
    """Three nodes all depend on one; the one carries the load."""
    hub = _uuid(0)
    spokes = [_uuid(i) for i in range(1, 4)]
    edges = [Edge(dependent=s, dependency=hub, weight=1.0) for s in spokes]
    scores = pagerank([hub, *spokes], edges)
    for spoke in spokes:
        assert scores[hub] > scores[spoke]


def test_heavier_edges_confer_more_authority() -> None:
    """Between two otherwise-symmetric sinks, the heavier-cited one ranks higher."""
    src, heavy, light = _uuid(0), _uuid(1), _uuid(2)
    edges = [
        Edge(dependent=src, dependency=heavy, weight=0.9),
        Edge(dependent=src, dependency=light, weight=0.1),
    ]
    scores = pagerank([src, heavy, light], edges)
    assert scores[heavy] > scores[light]


def test_ignores_edges_touching_unknown_nodes() -> None:
    a, b, ghost = _uuid(0), _uuid(1), _uuid(99)
    edges = [
        Edge(dependent=a, dependency=b, weight=1.0),
        Edge(dependent=a, dependency=ghost, weight=1.0),
    ]
    scores = pagerank([a, b], edges)
    assert set(scores) == {a, b}
    assert sum(scores.values()) == pytest.approx(1.0)


def test_zero_weight_edges_are_dropped() -> None:
    a, b = _uuid(0), _uuid(1)
    scores = pagerank([a, b], [Edge(dependent=a, dependency=b, weight=0.0)])
    # No effective edge: both nodes stay uniform.
    assert scores[a] == pytest.approx(scores[b])


def test_relation_edges_uses_abs_valence_when_weighted() -> None:
    rows = [
        {"from_id": _uuid(1), "to_id": _uuid(0), "valence": -0.7},
        {"from_id": _uuid(2), "to_id": _uuid(0), "valence": 0.4},
    ]
    edges = relation_edges(rows, weighted=True)
    assert [e.weight for e in edges] == [pytest.approx(0.7), pytest.approx(0.4)]
    assert edges[0].dependency == _uuid(0)


def test_relation_edges_unweighted_is_unit() -> None:
    rows = [{"from_id": _uuid(1), "to_id": _uuid(0), "valence": None}]
    edges = relation_edges(rows, weighted=False)
    assert edges[0].weight == 1.0


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
