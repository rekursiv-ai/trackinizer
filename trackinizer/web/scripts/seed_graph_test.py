"""Tests for ``seed_graph.py``: what a structure seeds as, and how cheaply."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import itertools
import uuid

from treekle import from_plain, json_loads

import pytest

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
    claims,
    cost_for,
    islands,
    seed_graph,
    showcase,
    spread,
    summary_for,
)
from trackinizer.wire.bodies import BATCH_MAX_ITEMS


if TYPE_CHECKING:
    from trackinizer.types.edges import Edge
    from trackinizer.wire.wire_sessions import SessionEnd


@pytest.fixture(scope="module")
def committed() -> Structure:
    """Return the committed structure, loaded once: a load takes ~150 ms."""
    return load(GRAPH_STRUCTURE.read_text())


@pytest.fixture(scope="module")
def committed_seed(committed: Structure) -> tuple[_FakeClient, SeededGraph]:
    """Seed the committed structure once; tests only read what it wrote."""
    client = _FakeClient()
    return client, seed_graph(client, committed, actor="ada@example.com")


def test_islands_are_the_issues_atop_narrows_and_what_they_produced() -> None:
    found = islands(_STRUCTURE)
    assert found.roots == (6, 0)
    assert found.home == {0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 6: 6, 5: 6, 7: 6, 9: 6}


def test_a_node_sits_by_its_nearest_root_and_the_newest_of_those_as_near() -> None:
    structure = Structure(
        nodes=(*[Node(kind="Issue", status="active")] * 2, *_EXPERIMENTS),
        edges=(
            Link(from_index=2, to_index=0, kind="produced_by", sign=0),
            Link(from_index=2, to_index=1, kind="produced_by", sign=0),
            Link(from_index=3, to_index=0, kind="produced_by", sign=0),
            Link(from_index=3, to_index=2, kind="produced_by", sign=0),
        ),
    )
    assert islands(structure).home == {0: 0, 1: 1, 2: 1, 3: 0}


def test_the_showcase_is_the_island_most_argued_over_and_its_most_cited_belief() -> (
    None
):
    assert showcase(_STRUCTURE, islands(_STRUCTURE)).root == 0
    assert showcase(_STRUCTURE, islands(_STRUCTURE)).belief == 3


def test_spread_cites_each_belief_once_from_another_island() -> None:
    # Belief 3's only foreign island's evidence already cites it, so it gains
    # nothing; belief 9 takes the Experiment of the island below its own.
    spread_structure = spread(_STRUCTURE)
    assert spread_structure.nodes == _STRUCTURE.nodes
    assert set(spread_structure.edges) - set(_STRUCTURE.edges) == {
        _link(2, 9, "favors", 1),
    }
    assert islands(spread_structure) == islands(_STRUCTURE)


def test_spread_sends_the_committed_newest_window_arcs_between_islands() -> None:
    # The grouped graph's mesh is cross-island evidence, and the default view
    # is the newest thousand: without a share of it there, clusters read alone.
    structure = spread(load(GRAPH_STRUCTURE.read_text()))
    assert (
        structure.edges
        == spread(
            load(GRAPH_STRUCTURE.read_text()),
        ).edges
    )
    found = islands(structure)
    newest = set(range(len(structure.nodes) - 1000, len(structure.nodes)))
    arcs = [
        link
        for link in structure.edges
        if link.kind in {"proves", "favors"}
        and link.from_index in newest
        and link.to_index in newest
        and found.home.get(link.from_index, link.from_index)
        != found.home.get(link.to_index, link.to_index)
    ]
    assert len(arcs) >= 15


def test_claims_gives_a_thin_island_its_ten_beliefs() -> None:
    work = (
        Node(kind="Issue", status="active"),
        *(Node(kind="Experiment", status="complete") for _ in range(23)),
        Node(kind="Belief", status="active"),
    )
    thin = Structure(
        nodes=work,
        edges=(
            *(_link(n, 0, "produced_by") for n in range(1, 24)),
            _link(24, 0, "produced_by"),
            _link(1, 24, "proves", 1),
        ),
    )
    filled = claims(thin)
    beliefs = [n for n, node in enumerate(filled.nodes) if node.kind == "Belief"]
    assert len(beliefs) == 10
    assert islands(filled).roots == (0,)
    for n in beliefs:
        # Each claim stands produced by an older node of its own thread.
        parents = [
            link.to_index
            for link in filled.edges
            if link.kind == "produced_by" and link.from_index == n
        ]
        assert parents
        assert all(parent < n for parent in parents)


def test_claims_fills_the_committed_thin_islands_without_turning_any_verdict() -> None:
    spread_structure = spread(load(GRAPH_STRUCTURE.read_text()))
    filled = claims(spread_structure)
    assert filled.nodes == claims(spread_structure).nodes
    found = islands(filled)
    members: dict[int, list[int]] = {}
    held: Counter[int] = Counter()
    for n, node in enumerate(filled.nodes):
        home = found.home.get(n)
        if home is not None:
            members.setdefault(home, []).append(n)
            if node.kind == "Belief":
                held[home] += 1
    assert all(held[home] >= 10 for home, row in members.items() if len(row) >= 24)
    # Only positive votes were added, so the demo's showcase island stands;
    # its indexes shift as claims interleave, so check the pick's shape.
    picked = showcase(filled, found)
    base = showcase(spread_structure, islands(spread_structure))
    assert picked.root == base.root
    assert filled.nodes[picked.belief].kind == "Belief"
    assert found.home[picked.belief] == picked.root


@pytest.mark.parametrize("structure", ["small", "committed"])
def test_nodes_are_created_in_order_and_every_edge_once_with_its_sign(
    structure: str,
    committed: Structure,
    committed_seed: tuple[_FakeClient, SeededGraph],
) -> None:
    if structure in {"small", "two_carriers"}:
        seeding, client = (
            (_STRUCTURE if structure == "small" else _TWO_CARRIERS),
            _FakeClient(),
        )
        seeded = seed_graph(client, seeding, actor="ada@example.com")
    else:
        seeding = committed
        client, seeded = committed_seed
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
    # The cost writes are a second pass over the finished graph, so they trail
    # every structural write; the ordering this checks is among the structural ones.
    structural = [call for call in client.calls if call != ("cost",)]
    assert structural[-2:] == [("edges",), ("session_end",)]


def test_every_person_is_an_example_address() -> None:
    client, _ = _seeded()
    for kind, body in client.items:
        if kind != "AgentSession":
            assert str(body["owner"]).endswith("@example.com")


def test_roots_take_written_titles_and_the_showcase_its_own_text() -> None:
    client, _ = _seeded()
    contents = json_loads(GRAPH_CONTENTS.read_text(), dict[str, object])
    written = from_plain(contents.get("roots"), list[str], default=list[str]())
    shown = from_plain(
        contents.get("showcase"),
        dict[str, object],
        default=dict[str, object](),
    )
    titles = [body["title"] for _, body in client.items]
    assert titles[6] == written[0]
    assert (
        from_plain(
            shown.get("root"),
            dict[str, object],
            default=dict[str, object](),
        ).items()
        <= client.items[0][1].items()
    )
    assert (
        from_plain(
            shown.get("belief"),
            dict[str, object],
            default=dict[str, object](),
        ).items()
        <= client.items[3][1].items()
    )
    assert all(titles)


def test_a_belief_is_judged_by_its_proves_edges() -> None:
    client, _ = _seeded()
    assert client.items[3][1]["judgement"] == "proven"
    assert client.items[9][1]["judgement"] == "unproven"


def test_every_record_is_seeded_with_its_summary_written() -> None:
    # A panel that opens on "add a summary" on nine hundred rows reads as an
    # empty tool; the demo is of a graph people have been keeping.
    client, _ = _seeded()
    assert all(str(body["description"]).strip() for _, body in client.items)


def test_a_summary_says_the_record_says() -> None:
    summary = summary_for(
        "Belief",
        "Label smoothing hurts recall on the held-out set",
        owner="ada@example.com",
        verdict="proven",
        confidence=0.8,
    )
    assert "{w" not in summary
    assert "0.80" in summary
    assert "label smoothing" in summary.lower()
    # A record with nothing to name gets no summary rather than a hole where the
    # name would have been.
    assert summary_for("Belief", "Label smoothing hurts recall") == ""
    assert "{owner}" not in summary_for("Paper", "Sparse warmup schedules for small-batch training")


def test_a_beliefs_confidence_follows_its_verdict() -> None:
    structure = Structure(
        nodes=(*_STRUCTURE.nodes, *[Node(kind="Belief", status="active")] * 2),
        edges=(
            *_STRUCTURE.edges,
            Link(from_index=2, to_index=10, kind="proves", sign=(-1)),
            Link(from_index=4, to_index=11, kind="proves", sign=1),
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
    kinds = from_plain(
        json_loads(GRAPH_CONTENTS.read_text(), dict[str, object]).get(
            "kinds",
        ),
        dict[str, object],
    )
    titles = _titles(from_plain(kinds.get("Experiment"), dict[str, object]))
    experiments = [body["title"] for kind, body in client.items if kind == "Experiment"]
    assert experiments == titles[:2]
    # Past the list, the titles come round again in the same order.
    many = _FakeClient()
    seed_graph(many, _TWO_CARRIERS, actor="ada@example.com")
    experiments = [body["title"] for kind, body in many.items if kind == "Experiment"]
    assert len(experiments) > len(titles)
    assert experiments == [titles[k % len(titles)] for k in range(len(experiments))]


def test_no_kind_repeats_a_built_title_before_it_has_used_every_one(
    committed_seed: tuple[_FakeClient, SeededGraph],
) -> None:
    # Lists show the newest rows of a kind together: a title repeated among them
    # reads as a placeholder.
    client, _ = committed_seed
    contents = json_loads(GRAPH_CONTENTS.read_text(), dict[str, object])
    shown = from_plain(
        contents.get("showcase"),
        dict[str, object],
        default=dict[str, object](),
    )
    written = {
        *from_plain(contents.get("roots"), list[str], default=list[str]()),
        *(
            from_plain(
                from_plain(
                    shown.get(part),
                    dict[str, object],
                    default=dict[str, object](),
                ).get("title"),
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
    assert first.costs == second.costs


def test_every_priced_kind_is_charged_on_both_axes() -> None:
    # A seed must give every kind that plausibly spends money a real cost on the
    # axis it spends on -- an Experiment its GPU-hours, an AgentSession its tokens --
    # while a claim and the question above it, which spend nothing to state, stay
    # off the ledger entirely.
    client, seeded = _seeded()
    kinds = {n.kind: seeded.ids[i] for i, n in enumerate(_STRUCTURE.nodes)}
    charged: dict[uuid.UUID, dict[str, float]] = {}
    for target_id, axis, value in client.costs:
        charged.setdefault(target_id, {})[axis] = value
    for kind in ("Experiment", "AgentSession", "Paper"):
        paid = charged.get(kinds[kind], {})
        assert paid["marginal_cost_agent_usd"] > 0
        assert paid["marginal_cost_resource_usd"] > 0
    # Issue and Belief are not priced at all: no write, so nothing on either axis.
    for kind in ("Issue", "Belief"):
        assert kinds[kind] not in charged


def test_an_experiment_costs_more_compute_than_a_paper() -> None:
    # The lab's whole story in one comparison: a run burns hours of GPU, a citation
    # burns a download. The resource axis is where that shows.
    experiment = cost_for("Experiment", "1.3B warmup sweep")["marginal_cost_resource_usd"]
    paper = cost_for("Paper", "Sparse warmup schedules for small-batch training")[
        "marginal_cost_resource_usd"
    ]
    assert experiment > paper
    assert cost_for("Issue", "Plan the Q4 compute budget") == {}
    assert cost_for("Belief", "Cosine decay beats linear past 20k steps") == {}


def test_a_cost_is_the_same_figure_every_time_its_title_is() -> None:
    # The value is drawn from a hash of the title, not a roll of a die, so the same
    # record charged twice lands on the same number and two seeds write alike.
    title = "Report: the warmup sweep"
    assert cost_for("Artifact", title) == cost_for("Artifact", title)


def test_cost_writes_follow_the_charges_of_a_full_seed() -> None:
    # Every one of the five thousand records that spends gets its writes in node
    # order, and none of the claims or asks do, so the count is the priced kinds.
    client = _FakeClient()
    structure = claims(spread(load(GRAPH_STRUCTURE.read_text())))
    seeded = seed_graph(client, structure, actor="ada@example.com")
    priced = {
        n: seeded.ids[n]
        for n, node in enumerate(structure.nodes)
        if node.kind in {"Experiment", "AgentSession", "CodeChange", "Artifact", "Paper", "WebResult", "WebSearch"}
    }
    written = {tid for tid, _axis, _value in client.costs}
    assert written == set(priced.values())
    # The order the writes land in is the order the nodes were seeded in.
    rank = {tid: n for n, tid in enumerate(seeded.ids)}
    order = [tid for tid, _axis, _value in client.costs]
    assert order == sorted(order, key=rank.__getitem__)


def test_in_cascade_order_the_committed_structure_walks_a_fifth_as_far() -> None:
    # In creation order the design's structure walks the server's cascade
    # 2,425,634 steps, which took fourteen minutes; in this order, measured
    # 2026-10-06 on the banded structure, 151,508 -- threads live in time
    # bands, so a chain never hangs across a band's whole span.
    cascade = _Cascade()
    steps = sum(
        cascade.add(link) for link in cascade_order(load(GRAPH_STRUCTURE.read_text()))
    )
    assert steps < 300_000


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
                Link(
                    from_index=(len(_STRUCTURE.nodes) + k),
                    to_index=(len(_STRUCTURE.nodes) + count),
                    kind="proves",
                    sign=0,
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
            *(Link(from_index=1, to_index=0, kind="narrows", sign=0),)
            * (BATCH_MAX_ITEMS * len(_STRUCTURE.nodes)),
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
            Link(from_index=1, to_index=0, kind="produced_by", sign=0),
            Link(from_index=2, to_index=0, kind="produced_by", sign=0),
            Link(from_index=3, to_index=0, kind="produced_by", sign=0),
            Link(from_index=1, to_index=3, kind="proves", sign=1),
            Link(from_index=2, to_index=3, kind="proves", sign=(-1)),
            Link(from_index=5, to_index=4, kind="produced_by", sign=0),
            Link(from_index=6, to_index=4, kind="produced_by", sign=0),
            Link(from_index=7, to_index=4, kind="produced_by", sign=0),
            Link(from_index=5, to_index=7, kind="proves", sign=1),
            Link(from_index=6, to_index=7, kind="proves", sign=(-1)),
            Link(from_index=5, to_index=6, kind="favors", sign=(-1)),
        ),
    )
    assert showcase(structure, islands(structure)) == Showcase(root=0, belief=3)


def test_a_graph_with_no_evidence_against_a_belief_has_no_showcase() -> None:
    structure = Structure(
        nodes=_STRUCTURE.nodes,
        edges=tuple(
            Link(
                from_index=link.from_index,
                to_index=link.to_index,
                kind=link.kind,
                sign=abs(link.sign),
            )
            for link in _STRUCTURE.edges
        ),
    )
    with pytest.raises(ValueError, match="no island has evidence against a Belief"):
        showcase(structure, islands(structure))


def test_the_committed_structure_has_a_showcase_and_a_title_for_every_root(
    committed: Structure,
) -> None:
    found = islands(committed)
    picked = showcase(committed, found)
    assert found.home[picked.belief] == picked.root
    contents = json_loads(GRAPH_CONTENTS.read_text(), dict[str, object])
    assert len(
        from_plain(contents.get("roots"), list[str], default=list[str]()),
    ) >= len(
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
        Link(from_index=1, to_index=0, kind="narrows", sign=0),
        Link(from_index=2, to_index=1, kind="produced_by", sign=0),
        Link(from_index=3, to_index=1, kind="produced_by", sign=0),
        Link(from_index=4, to_index=0, kind="produced_by", sign=0),
        Link(from_index=4, to_index=3, kind="proves", sign=1),
        Link(from_index=2, to_index=3, kind="proves", sign=(-1)),
        Link(from_index=5, to_index=3, kind="favors", sign=(-1)),
        Link(from_index=5, to_index=6, kind="produced_by", sign=0),
        Link(from_index=7, to_index=6, kind="produced_by", sign=0),
        Link(from_index=9, to_index=6, kind="produced_by", sign=0),
    ),
)


# More edges than one batch holds, so two carriers each take a share. The chain hangs
# off no root, so the islands and their showcase stay those of ``_STRUCTURE``.
_TWO_CARRIERS = Structure(
    nodes=(
        *_STRUCTURE.nodes,
        *[Node(kind="Experiment", status="complete")] * (BATCH_MAX_ITEMS + 1),
    ),
    edges=(
        *_STRUCTURE.edges,
        *(
            Link(
                from_index=len(_STRUCTURE.nodes) + k + 1,
                to_index=len(_STRUCTURE.nodes) + k,
                kind="produced_by",
                sign=0,
            )
            for k in range(BATCH_MAX_ITEMS)
        ),
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
    costs: list[tuple[uuid.UUID, str, float]] = field(default_factory=list)
    _ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))
    _made: set[uuid.UUID] = field(default_factory=set)

    def submit_batch(
        self,
        items: Sequence[tuple[Inquiry.InquiryKind, Mapping[str, object]]],
        *,
        edges: Sequence[Mapping[str, object]] = (),
        actor: str,
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
                from_plain(edge["edge_kind"], str),
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

    def add_cost(
        self,
        target_id: uuid.UUID,
        field: str,
        value: float,
        *,
        actor: str,
        reason: str = "",  # noqa: ARG002 -- the protocol's audit note; unused here.
    ) -> None:
        assert actor
        assert target_id in self._made
        assert field in {"marginal_cost_agent_usd", "marginal_cost_resource_usd"}
        self.calls.append(("cost",))
        self.costs.append((target_id, field, value))

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
        made = uuid.UUID(from_plain(edge[f"{end}_id"], str))
        assert made in self._made
        return made


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
