"""Tests for ``seed_graph.py``: what a structure seeds as, and how cheaply."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import itertools
import uuid

import pytest

from trackinizer.lib.custom_json import convert, parse
from trackinizer.types.edges import EDGE_POLICIES
from trackinizer.types.inquiries import Inquiry
from trackinizer.web.scripts.graph_structure import (
    GRAPH_STRUCTURE,
    Link,
    Node,
    Structure,
    load,
)
from trackinizer.web.scripts.seed_graph import (
    GRAPH_CONTENTS,
    SeededGraph,
    Showcase,
    _titles,
    cascade_order,
    islands,
    seed_graph,
    showcase,
)
from trackinizer.wire.bodies import BATCH_MAX_ITEMS


if TYPE_CHECKING:
    from trackinizer.types.edges import Edge
    from trackinizer.wire.wire_sessions import SessionEnd


def test_islands_are_the_issues_atop_narrows_and_what_they_produced() -> None:
    found = islands(_STRUCTURE)
    assert found.roots == (6, 0)
    assert found.home == {0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 6: 6, 5: 6, 7: 6, 9: 6}


def test_a_node_sits_by_its_nearest_root_and_the_newest_of_those_as_near() -> None:
    structure = Structure(
        nodes=(*[Node(kind="Issue", status="active")] * 2, *_EXPERIMENTS),
        edges=(
            _link(2, 0, "produced_by"),
            _link(2, 1, "produced_by"),
            _link(3, 0, "produced_by"),
            _link(3, 2, "produced_by"),
        ),
    )
    assert islands(structure).home == {0: 0, 1: 1, 2: 1, 3: 0}


def test_the_showcase_is_the_island_most_argued_over_and_its_most_cited_belief() -> (
    None
):
    assert showcase(_STRUCTURE, islands(_STRUCTURE)).root == 0
    assert showcase(_STRUCTURE, islands(_STRUCTURE)).belief == 3


@pytest.mark.parametrize("structure", ["small", "committed"])
def test_nodes_are_created_in_order_and_every_edge_once_with_its_sign(
    structure: str,
) -> None:
    seeding = _STRUCTURE if structure == "small" else load(GRAPH_STRUCTURE.read_text())
    client = _FakeClient()
    seeded = seed_graph(client, seeding, actor="ada@example.com")
    assert [kind for kind, _ in client.items] == [n.kind for n in seeding.nodes]
    index = {node: n for n, node in enumerate(seeded.ids)}
    written = {
        (index[source], index[target], kind, _sign(valence))
        for source, kind, target, valence in client.edges
    }
    assert len(client.edges) == len(seeding.edges)
    assert written == {
        (link.from_index, link.to_index, link.kind, link.sign) for link in seeding.edges
    }


def test_complete_sessions_end_before_any_edge_and_each_has_its_own_owner() -> None:
    client, seeded = _seeded()
    assert client.ended == [seeded.ids[7]]
    assert client.calls.index(("session_end",)) < client.calls.index(("edges",))
    sessions = [body for kind, body in client.items if kind == "AgentSession"]
    assert {body["status"] for body in sessions} == {"active"}
    owners = [str(body["owner"]) for body in sessions]
    assert len(set(owners)) == len(owners)
    assert "@" not in "".join(owners)


def test_a_complete_session_that_carries_edges_ends_after_they_land() -> None:
    # The last node is made in the batch that adds the edges.
    structure = Structure(
        nodes=(*_STRUCTURE.nodes[:-1], Node(kind="AgentSession", status="complete")),
        edges=_STRUCTURE.edges,
    )
    client = _FakeClient()
    seeded = seed_graph(client, structure, actor="ada@example.com")
    assert client.ended == [seeded.ids[7], seeded.ids[9]]
    assert client.calls[-2:] == [("edges",), ("session_end",)]


def test_every_person_is_an_example_address() -> None:
    client, _ = _seeded()
    for kind, body in client.items:
        if kind != "AgentSession":
            assert str(body["owner"]).endswith("@example.com")


def test_roots_take_written_titles_and_the_showcase_its_own_text() -> None:
    client, _ = _seeded()
    contents = parse(GRAPH_CONTENTS.read_text(), dict[str, object])
    written = convert(contents.get("roots"), list[str], default=[])
    shown = convert(contents.get("showcase"), dict[str, object], default={})
    titles = [body["title"] for _, body in client.items]
    assert titles[6] == written[0]
    assert (
        convert(shown.get("root"), dict[str, object], default={}).items()
        <= client.items[0][1].items()
    )
    assert (
        convert(shown.get("belief"), dict[str, object], default={}).items()
        <= client.items[3][1].items()
    )
    assert all(titles)


def test_a_belief_is_judged_by_its_proves_edges() -> None:
    client, _ = _seeded()
    assert client.items[3][1]["judgement"] == "proven"
    assert client.items[9][1]["judgement"] == "unproven"


def test_a_beliefs_confidence_follows_its_verdict() -> None:
    structure = Structure(
        nodes=(*_STRUCTURE.nodes, *[Node(kind="Belief", status="active")] * 2),
        edges=(
            *_STRUCTURE.edges,
            _link(2, 10, "proves", -1),
            _link(4, 11, "proves", 1),
        ),
    )
    client = _FakeClient()
    seed_graph(client, structure, actor="ada@example.com")
    verdicts = {
        n: (body["judgement"], body["confidence"])
        for n, (kind, body) in enumerate(client.items)
        if kind == "Belief" and n != 3
    }
    assert verdicts == {
        9: ("unproven", 0.5),
        10: ("disproven", 0.2),
        11: ("proven", 0.9),
    }


def test_only_a_complete_session_is_created_active() -> None:
    client, _ = _seeded()
    assert [body["status"] for _, body in client.items] == [
        *(node.status for node in _STRUCTURE.nodes[:7]),
        "active",
        "active",
        "active",
    ]


def test_a_kind_takes_its_built_titles_in_order() -> None:
    client, _ = _seeded()
    kinds = convert(
        parse(GRAPH_CONTENTS.read_text(), dict[str, object]).get("kinds"),
        dict[str, object],
    )
    titles = _titles(convert(kinds.get("Experiment"), dict[str, object]))
    experiments = [body["title"] for kind, body in client.items if kind == "Experiment"]
    assert experiments == titles[:2]


def test_no_kind_repeats_a_built_title_before_it_has_used_every_one() -> None:
    # Lists show the newest rows of a kind together: a title repeated among them
    # reads as a placeholder.
    client = _FakeClient()
    seed_graph(client, load(GRAPH_STRUCTURE.read_text()), actor="ada@example.com")
    contents = parse(GRAPH_CONTENTS.read_text(), dict[str, object])
    shown = convert(contents.get("showcase"), dict[str, object], default={})
    written = {
        *convert(contents.get("roots"), list[str], default=[]),
        *(
            convert(
                convert(shown.get(part), dict[str, object], default={}).get("title"),
                str,
                default="",
            )
            for part in shown
        ),
    }
    built: dict[str, list[str]] = {}
    for kind, body in client.items:
        if (title := str(body["title"])) not in written:
            built.setdefault(kind, []).append(title)
    for titles in built.values():
        width = len(set(titles))
        for start in range(0, len(titles), width):
            run = titles[start : start + width]
            assert len(set(run)) == len(run)
    # Every pairing of the Artifacts' two word lists, eight words each.
    assert len(set(built["Artifact"])) == 64


def test_two_seeds_write_the_same() -> None:
    first, _ = _seeded()
    second, _ = _seeded()
    assert first.calls == second.calls
    assert first.items == second.items
    assert first.edges == second.edges


def test_in_cascade_order_the_committed_structure_walks_a_tenth_as_far() -> None:
    # In creation order the server's cascade walks 1,521,324 steps, which took half
    # an hour; in this order, measured 2026-10-03, 129,290.
    cascade = _Cascade()
    steps = sum(
        cascade.add(link) for link in cascade_order(load(GRAPH_STRUCTURE.read_text()))
    )
    assert steps < 150_000


def test_edges_no_batch_can_carry_are_refused_before_anything_is_written() -> None:
    """N1-09: 1001 edges into the newest node fit no batch; nothing is half-seeded."""
    count = BATCH_MAX_ITEMS + 1
    structure = Structure(
        nodes=(
            *_STRUCTURE.nodes,
            *[Node(kind="Experiment", status="complete")] * count,
            Node(kind="Belief", status="active"),
        ),
        edges=(
            *_STRUCTURE.edges,
            *(
                _link(
                    len(_STRUCTURE.nodes) + k,
                    len(_STRUCTURE.nodes) + count,
                    "proves",
                )
                for k in range(count)
            ),
        ),
    )
    client = _FakeClient()
    with pytest.raises(ValueError, match="no batch"):
        seed_graph(client, structure, actor="ada@example.com")
    assert client.calls == []


def test_more_edges_than_nodes_can_carry_are_refused_before_anything_is_written() -> (
    None
):
    """N1-09/S2: carriers never wrap to a negative node index."""
    structure = Structure(
        nodes=_STRUCTURE.nodes,
        edges=(
            *_STRUCTURE.edges,
            *(_link(1, 0, "narrows"),) * (BATCH_MAX_ITEMS * len(_STRUCTURE.nodes)),
        ),
    )
    client = _FakeClient()
    with pytest.raises(ValueError, match="no batch"):
        seed_graph(client, structure, actor="ada@example.com")
    assert client.calls == []


def test_the_showcase_counts_only_evidence_on_a_belief() -> None:
    """N1-10: signed edges to an Experiment do not make an island the showcase."""
    issue = Node(kind="Issue", status="active")
    structure = Structure(
        nodes=(
            issue,
            *_EXPERIMENTS,
            Node(kind="Belief", status="active"),
            issue,
            *_EXPERIMENTS,
            Node(kind="Experiment", status="complete"),
        ),
        edges=(
            _link(1, 0, "produced_by"),
            _link(2, 0, "produced_by"),
            _link(3, 0, "produced_by"),
            _link(1, 3, "proves", 1),
            _link(2, 3, "proves", -1),
            _link(5, 4, "produced_by"),
            _link(6, 4, "produced_by"),
            _link(7, 4, "produced_by"),
            _link(5, 7, "proves", 1),
            _link(6, 7, "proves", -1),
            _link(5, 6, "favors", -1),
        ),
    )
    assert showcase(structure, islands(structure)) == Showcase(root=0, belief=3)


def test_the_committed_structure_has_a_showcase_and_a_title_for_every_root() -> None:
    structure = load(GRAPH_STRUCTURE.read_text())
    found = islands(structure)
    picked = showcase(structure, found)
    assert found.home[picked.belief] == picked.root
    contents = parse(GRAPH_CONTENTS.read_text(), dict[str, object])
    assert len(convert(contents.get("roots"), list[str], default=[])) >= len(
        found.roots,
    )


_EXPERIMENTS = (
    Node(kind="Experiment", status="complete"),
    Node(kind="Experiment", status="complete"),
)


def _link(from_index: int, to_index: int, kind: Edge.Kind, sign: int = 0) -> Link:
    return Link(from_index=from_index, to_index=to_index, kind=kind, sign=sign)


_STRUCTURE = Structure(
    nodes=(
        Node(kind="Issue", status="active"),
        Node(kind="Issue", status="active"),
        Node(kind="Experiment", status="complete"),
        Node(kind="Belief", status="active"),
        Node(kind="Experiment", status="complete"),
        Node(kind="Paper", status="active"),
        Node(kind="Issue", status="active"),
        Node(kind="AgentSession", status="complete"),
        Node(kind="AgentSession", status="active"),
        Node(kind="Belief", status="active"),
    ),
    edges=(
        _link(1, 0, "narrows"),
        _link(2, 1, "produced_by"),
        _link(3, 1, "produced_by"),
        _link(4, 0, "produced_by"),
        _link(4, 3, "proves", 1),
        _link(2, 3, "proves", -1),
        _link(5, 3, "favors", -1),
        _link(5, 6, "produced_by"),
        _link(7, 6, "produced_by"),
        _link(9, 6, "produced_by"),
    ),
)


def _seeded() -> tuple[_FakeClient, SeededGraph]:
    client = _FakeClient()
    return client, seed_graph(client, _STRUCTURE, actor="ada@example.com")


@dataclass(slots=True, kw_only=True)
class _Cascade:
    """The server's change cascade as edges land, counting the steps it walks.

    An edge alerts the end its kind names dependent when the other changes. A new
    edge's change walks the alerts from its ``from`` end, a change-log row a
    step, and then, between two nodes without a ``produced_by``, a ``narrows``,
    ``requires`` or ``supersedes`` adds one, the younger produced by the older.
    """

    alerts: dict[int, list[int]] = field(default_factory=dict)
    stored: set[tuple[int, int, str]] = field(default_factory=set)
    produced: set[frozenset[int]] = field(default_factory=set)

    def add(self, link: Link) -> int:
        """Store ``link``; return the steps its change walked."""
        if (link.from_index, link.to_index, link.kind) in self.stored:
            return 0
        self._store(link.from_index, link.to_index, link.kind)
        steps = 0
        walked = {link.from_index}
        frontier = [link.from_index]
        for node in frontier:
            for target in self.alerts.get(node, ()):
                steps += 1
                if target not in walked:
                    walked.add(target)
                    frontier.append(target)
        pair = frozenset((link.from_index, link.to_index))
        if (
            link.kind in {"narrows", "requires", "supersedes"}
            and pair not in self.produced
        ):
            self._store(max(pair), min(pair), "produced_by")
        return steps

    def _store(self, from_index: int, to_index: int, kind: Edge.Kind) -> None:
        self.stored.add((from_index, to_index, kind))
        if kind == "produced_by":
            self.produced.add(frozenset((from_index, to_index)))
        dependent = EDGE_POLICIES[kind].cascade_dependent
        source, target = (
            (from_index, to_index) if dependent == "to" else (to_index, from_index)
        )
        self.alerts.setdefault(source, []).append(target)


def _sign(valence: object) -> int:
    if valence is None:
        return 0
    assert isinstance(valence, float)
    assert 0.3 <= abs(valence) <= 0.9
    return 1 if valence > 0 else -1


@dataclass(slots=True, kw_only=True)
class _FakeClient:
    """Records each write as the batch route takes it; ids count up from 1."""

    calls: list[tuple[str, ...]] = field(default_factory=list)
    items: list[tuple[Inquiry.InquiryKind, Mapping[str, object]]] = field(
        default_factory=list,
    )
    ended: list[uuid.UUID] = field(default_factory=list)
    edges: list[tuple[uuid.UUID, str, uuid.UUID, object]] = field(default_factory=list)
    _ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))
    _made: set[uuid.UUID] = field(default_factory=set)

    def submit_batch(
        self,
        items: Sequence[tuple[Inquiry.InquiryKind, Mapping[str, object]]],
        *,
        edges: Sequence[Mapping[str, object]] = (),
        actor: str | None = None,
    ) -> list[uuid.UUID]:
        assert actor
        assert 1 <= len(items) <= BATCH_MAX_ITEMS
        assert len(edges) <= BATCH_MAX_ITEMS
        self.calls.append(("edges",) if edges else ("nodes",))
        self.items.extend(items)
        ids = [uuid.UUID(int=next(self._ids)) for _ in items]
        self._made.update(ids)
        self.edges.extend(
            (
                self._end(edge, "from", ids),
                convert(edge["edge_kind"], str),
                self._end(edge, "to", ids),
                edge.get("valence"),
            )
            for edge in edges
        )
        return ids

    def session_end(
        self,
        session_id: uuid.UUID,
        body: SessionEnd | None = None,
    ) -> None:
        assert body is None
        assert session_id in self._made
        self.calls.append(("session_end",))
        self.ended.append(session_id)

    def _end(
        self,
        edge: Mapping[str, object],
        end: str,
        ids: Sequence[uuid.UUID],
    ) -> uuid.UUID:
        """Return the id an edge's end names: one its batch makes, or one made before."""
        index = edge.get(f"{end}_index")
        if isinstance(index, int):
            return ids[index]
        made = uuid.UUID(convert(edge[f"{end}_id"], str))
        assert made in self._made
        return made


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
