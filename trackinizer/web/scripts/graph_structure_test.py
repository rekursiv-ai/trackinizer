"""Tests for ``graph_structure.py``: only the graph's shape leaves the server."""

from __future__ import annotations

import pytest

from trackinizer.web.scripts.graph_structure import (
    GRAPH_STRUCTURE,
    Link,
    Node,
    Structure,
    dump,
    load,
    scrub,
)


def test_scrub_keeps_kinds_statuses_edges_and_creation_order() -> None:
    assert scrub(_GRAPH) == Structure(
        nodes=(
            Node(kind="Issue", status="active"),
            Node(kind="Experiment", status="complete"),
            Node(kind="Belief", status="active"),
        ),
        edges=(
            Link(from_index=1, to_index=0, kind="produced_by", sign=0),
            Link(from_index=1, to_index=2, kind="proves", sign=-1),
        ),
    )


def test_nothing_but_the_shape_is_written() -> None:
    written = dump(scrub(_GRAPH))
    for secret in (
        "Secret plan",
        "ada@corp.internal",
        "2026-07",
        "11111111",
        "4242",
        "proven",
        "0.37",
        "-0.8",
    ):
        assert secret not in written


def test_a_word_outside_the_vocabulary_is_refused() -> None:
    graph = {"nodes": [{**_NODES[0], "kind": "Secret plan"}], "edges": []}
    with pytest.raises(ValueError, match="is not one of"):
        scrub(graph)


def test_load_reads_what_dump_writes() -> None:
    structure = scrub(_GRAPH)
    assert load(dump(structure)) == structure


def test_load_refuses_an_edge_to_no_node() -> None:
    text = dump(scrub(_GRAPH)).replace("[1,2,3,-1]", "[1,9,3,-1]")
    with pytest.raises(ValueError, match="is not one of 3 nodes"):
        load(text)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ('"nodes": [[0,0]', '"nodes": [[-1,0]'),
        ('"nodes": [[0,0]', '"nodes": [[99,0]'),
        ('"nodes": [[0,0]', '"nodes": [[0,-1]'),
        ('"nodes": [[0,0]', '"nodes": [[0,99]'),
        ("[1,2,3,-1]", "[1,2,-1,-1]"),
        ("[1,2,3,-1]", "[1,2,99,-1]"),
        ("[1,2,3,-1]", "[-1,2,3,-1]"),
    ],
)
def test_load_refuses_every_index_outside_its_list(before: str, after: str) -> None:
    """N1-07: a negative index never wraps to a word, nor a large one IndexErrors."""
    text = dump(scrub(_GRAPH))
    assert before in text
    with pytest.raises(ValueError, match="is not one of"):
        load(text.replace(before, after))


def test_a_zero_valence_has_no_sign() -> None:
    """N1-08: a neutral valence is neither for nor against."""
    graph = {
        "nodes": _NODES,
        "edges": [{**_GRAPH["edges"][0], "valence": 0.0}],
    }
    assert [link.sign for link in scrub(graph).edges] == [0]


def test_nodes_made_at_the_same_moment_keep_the_servers_order() -> None:
    made = "2026-07-01T00:00:00+00:00"
    graph = {
        "nodes": [
            {**_NODES[0], "created": made},
            {**_NODES[1], "created": made},
        ],
        "edges": [],
    }
    assert scrub(graph).nodes == (
        Node(kind="Belief", status="active"),
        Node(kind="Issue", status="active"),
    )


def test_the_committed_structure_loads() -> None:
    structure = load(GRAPH_STRUCTURE.read_text())
    assert len(structure.nodes) > 4000
    assert {link.sign for link in structure.edges} == {-1, 0, 1}


_NODES = [
    {
        "id": "33333333-3333-3333-3333-333333333333",
        "kind": "Belief",
        "seq": 4242,
        "title": "Secret plan",
        "status": "active",
        "created": "2026-07-03T00:00:00+00:00",
        "judgement": "proven",
        "confidence": 0.37,
    },
    {
        "id": "11111111-1111-1111-1111-111111111111",
        "kind": "Issue",
        "seq": 7,
        "title": "Ask ada@corp.internal",
        "status": "active",
        "created": "2026-07-01T00:00:00+00:00",
    },
    {
        "id": "22222222-2222-2222-2222-222222222222",
        "kind": "Experiment",
        "seq": 8,
        "title": "Secret run",
        "status": "complete",
        "created": "2026-07-02T00:00:00+00:00",
    },
]
_GRAPH = {
    "nodes": _NODES,
    "edges": [
        {
            "from_id": "22222222-2222-2222-2222-222222222222",
            "to_id": "33333333-3333-3333-3333-333333333333",
            "edge_kind": "proves",
            "valence": -0.8,
        },
        {
            "from_id": "22222222-2222-2222-2222-222222222222",
            "to_id": "11111111-1111-1111-1111-111111111111",
            "edge_kind": "produced_by",
        },
    ],
}


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
