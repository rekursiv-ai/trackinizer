"""Tests for ``seed_screenshots.py``: what it writes, and that it writes the same each time."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import itertools
import re
import uuid

from trackinizer.lib.codec import from_plain
from trackinizer.types.edges import EDGE_POLICIES, kind_group_members
from trackinizer.types.inquiries import Inquiry
from trackinizer.web.scripts.graph_structure import Link, Node, Structure
from trackinizer.web.scripts.seed_screenshots import Shots, seed
from trackinizer.wire.wire_metrics import MetricPoint
from trackinizer.wire.wire_session_ir import RecordBody
from trackinizer.wire.wire_sessions import SessionStart, SessionStartResponse


if TYPE_CHECKING:
    from trackinizer.lib.codec import PlainTree
    from trackinizer.wire.wire_session_ir import ManifestBody
    from trackinizer.wire.wire_sessions import SessionEnd


def test_the_graph_is_a_few_rooted_subgraphs_with_nothing_left_unrooted() -> None:
    # The structure's one island, then the three efforts'.
    client, _ = _seeded()
    roots = _roots(client)
    assert len(set(roots.values())) == 4
    assert set(roots) == set(client.kinds)


def test_the_structure_is_seeded_first_and_its_showcase_named() -> None:
    client, shots = _seeded()
    first = list(client.kinds)[: len(_STRUCTURE.nodes)]
    assert [client.kinds[node] for node in first] == [n.kind for n in _STRUCTURE.nodes]
    assert (shots.root, shots.cited) == (first[0], first[1])


def test_the_artifact_is_an_html_report_produced_by_the_cache_issue() -> None:
    client, shots = _seeded()
    published = client.artifacts[shots.artifact]
    assert published["format"] == "html"
    assert "<svg" in from_plain(published["html"], str)
    issue = uuid.UUID(from_plain(published["issue_id"], str))
    assert client.bodies[issue]["title"] == "Cache merge ranks between calls"


def test_the_demo_messages_a_live_session_of_its_agent() -> None:
    client, shots = _seeded()
    start, records = client.sessions[shots.session]
    assert start.actor == shots.agent
    assert records


def test_every_email_is_an_example_address_and_no_home_path_is_written() -> None:
    client, _ = _seeded()
    written = repr(client.calls)
    emails = [
        found.group() for found in re.finditer(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*", written)
    ]
    assert emails
    for email in emails:
        assert email.endswith("@example.com"), email
    assert re.search(r"/Users/|/home/|~/", written) is None


def test_the_belief_has_evidence_for_and_against() -> None:
    client, shots = _seeded()
    assert client.kinds[shots.belief] == "Belief"
    evidence = {
        (kind, valence > 0)
        for _, kind, target, valence in client.edges
        if target == shots.belief and valence is not None
    }
    assert {("proves", True), ("favors", True), ("favors", False)} <= evidence


def test_the_paper_has_an_abstract_authors_and_citations_both_ways() -> None:
    client, shots = _seeded()
    paper = client.bodies[shots.paper]
    assert paper["abstract"]
    assert paper["authors"]
    cites = [(s, t) for s, kind, t, _ in client.edges if kind == "cites_paper"]
    assert len([t for s, t in cites if s == shots.paper]) >= 2
    assert [s for s, t in cites if t == shots.paper]


def test_the_experiment_has_an_outcome_labels_metrics_and_proves_a_belief() -> None:
    client, shots = _seeded()
    experiment = client.bodies[shots.experiment]
    assert experiment["outcome"]
    assert experiment["labels"]
    assert len({point.key for point in client.metrics[shots.experiment]}) >= 2
    proved = [
        t for s, kind, t, _ in client.edges if (s, kind) == (shots.experiment, "proves")
    ]
    assert [client.kinds[t] for t in proved] == ["Belief"]


def test_every_edge_is_one_the_server_accepts() -> None:
    # The fake takes any edge; the server refuses an unknown kind, an end its kind
    # does not admit, and a valence outside [-1, 1].
    client, _ = _seeded()
    policies = {str(kind): policy for kind, policy in EDGE_POLICIES.items()}
    for source, kind, target, valence in client.edges:
        assert kind in policies, kind
        policy = policies[kind]
        assert client.kinds[source] in kind_group_members(policy.from_kinds), kind
        assert client.kinds[target] in kind_group_members(policy.to_kinds), kind
        assert valence is None or -1 <= valence <= 1, (kind, valence)


def test_each_experiments_chart_bears_out_its_outcome() -> None:
    # The Experiment screenshot shows a chart beside the outcome it supports.
    client, _ = _seeded()
    charts = {
        from_plain(client.bodies[experiment]["title"], str): {
            key: [(point.step, point.value) for point in points if point.key == key]
            for key in dict.fromkeys(point.key for point in points)
        }
        for experiment, points in client.metrics.items()
    }
    # "1.9x throughput: 1.04 M to 1.98 M tokens/s", the cache filling as it runs.
    cache = charts["Merge-rank cache benchmark"]
    assert list(cache) == ["throughput_mtok_s", "cache_hit_rate"]
    for series in cache.values():
        assert [step for step, _ in series] == list(range(24))
        values = [value for _, value in series]
        assert values == sorted(values)
        assert all(value == round(value, 3) for value in values)
    throughput = [value for _, value in cache["throughput_mtok_s"]]
    assert (throughput[0], round(throughput[-1], 2)) == (1.04, 1.98)
    hits = [value for _, value in cache["cache_hit_rate"]]
    assert (hits[0], hits[-1]) == (0.0, 0.97)
    # The cache warms over the run: the first step brings less than half the gain.
    for values in (throughput, hits):
        assert values[1] - values[0] < (values[-1] - values[0]) / 2
    # "Spread fell from 1.8 to 0.2 points (71.3 to 71.5)" over ten reruns.
    reruns = charts["Ten reruns with fixed seeds"]
    assert list(reruns) == ["score"]
    scores = reruns["score"]
    assert [step for step, _ in scores] == list(range(10))
    assert (min(value for _, value in scores), max(value for _, value in scores)) == (
        71.3,
        71.5,
    )
    # "Block-sparse recalls 94% of needles at 64k tokens, dense 97%", recall
    # falling as the context grows; steps are lengths in thousands of tokens.
    needle = charts["Needle recall at 64k: block-sparse against dense"]
    assert list(needle) == ["recall_block_sparse", "recall_dense"]
    for series in needle.values():
        assert [step for step, _ in series] == [8, 16, 32, 64]
        values = [value for _, value in series]
        assert values == sorted(values, reverse=True)
        assert all(0 <= value <= 1 for value in values)
    assert (needle["recall_block_sparse"][-1][1], needle["recall_dense"][-1][1]) == (
        0.94,
        0.97,
    )


def test_each_session_is_a_conversation_in_a_room() -> None:
    client, _ = _seeded()
    assert len(client.sessions) >= 3
    for start, records in client.sessions.values():
        rooms = start.rooms or []
        assert rooms
        assert start.actor is not None
        kinds = [record.kind for record in records]
        assert {"UserMessage", "AssistantMessage"} <= set(kinds)
        assert [record.idx for record in records] == list(range(len(records)))


def test_turns_reach_the_server_in_the_order_they_happened_across_sessions() -> None:
    # The console orders turns by when the server stored them, not by their stamps.
    client, _ = _seeded()
    appends = [call for call in client.calls if call[0] == "append_records"]
    records = [
        record for call in appends for record in cast("Sequence[RecordBody]", call[4])
    ]
    stamps = [record.timestamp for record in records if record.timestamp]
    assert len(stamps) == len(records)
    assert stamps == sorted(stamps)
    sessions = [call[1] for call in appends]
    switches = sum(a != b for a, b in itertools.pairwise(sessions))
    assert switches > len(client.sessions)


def test_the_last_turn_is_stamped_when_the_seed_runs() -> None:
    # The console's timeline counts records by when the server stored them, while
    # each line shows the record's own stamp: stamped on another day, they disagree.
    client, _ = _seeded()
    stamps = [
        record.timestamp
        for call in client.calls
        if call[0] == "append_records"
        for record in cast("Sequence[RecordBody]", call[4])
    ]
    assert max(stamp for stamp in stamps if stamp) == _NOW


def test_two_seeds_write_the_same() -> None:
    first, first_shots = _seeded()
    second, second_shots = _seeded()
    assert first.calls == second.calls
    assert first_shots == second_shots


_NOW = datetime(2026, 10, 2, 22, 37, tzinfo=UTC)


# The smallest structure with a showcase: an Issue, and a Belief and an Experiment it
# produced, the Experiment arguing against the Belief.
_STRUCTURE = Structure(
    nodes=(
        Node(kind="Issue", status="active"),
        Node(kind="Belief", status="active"),
        Node(kind="Experiment", status="complete"),
    ),
    edges=(
        Link(from_index=1, to_index=0, kind="produced_by", sign=0),
        Link(from_index=2, to_index=0, kind="produced_by", sign=0),
        Link(from_index=2, to_index=1, kind="proves", sign=-1),
    ),
)


def _seeded() -> tuple[_FakeClient, Shots]:
    client = _FakeClient()
    return client, seed(client, now=_NOW, structure=_STRUCTURE)


def _roots(client: _FakeClient) -> dict[uuid.UUID, uuid.UUID]:
    """Each node's root, as the graph's Group by root finds them; a node under two fails."""
    below: dict[uuid.UUID, list[uuid.UUID]] = {}
    narrowed = {s for s, kind, _, _ in client.edges if kind == "narrows"}
    for source, kind, target, _ in client.edges:
        if kind in {"narrows", "produced_by"}:
            below.setdefault(target, []).append(source)
    roots = [
        node
        for node, kind in client.kinds.items()
        if kind == "Issue" and node not in narrowed and node in below
    ]
    home: dict[uuid.UUID, uuid.UUID] = {}
    for root in roots:
        walk = [root]
        for node in walk:
            assert node not in home, f"{node} sits under two roots"
            home[node] = root
            walk.extend(below.get(node, []))
    return home


@dataclass(slots=True, kw_only=True)
class _FakeClient:
    """Records each write; ids count up from 1, so two seeds get the same ones."""

    calls: list[tuple[object, ...]] = field(default_factory=list)
    kinds: dict[uuid.UUID, Inquiry.InquiryKind] = field(default_factory=dict)
    bodies: dict[uuid.UUID, Mapping[str, object]] = field(default_factory=dict)
    edges: list[tuple[uuid.UUID, str, uuid.UUID, float | None]] = field(
        default_factory=list,
    )
    metrics: dict[uuid.UUID, list[MetricPoint]] = field(default_factory=dict)
    sessions: dict[uuid.UUID, tuple[SessionStart, list[RecordBody]]] = field(
        default_factory=dict,
    )
    artifacts: dict[uuid.UUID, Mapping[str, object]] = field(default_factory=dict)
    _ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))

    def submit_batch(
        self,
        items: Sequence[tuple[Inquiry.InquiryKind, Mapping[str, object]]],
        *,
        edges: Sequence[Mapping[str, object]] = (),
        actor: str | None = None,
    ) -> list[uuid.UUID]:
        self.calls.append(("submit_batch", items, edges, actor))
        ids = [self._id() for _ in items]
        for new, (kind, body) in zip(ids, items, strict=True):
            self.kinds[new] = kind
            self.bodies[new] = body
        for edge in edges:
            valence = edge.get("valence")
            self.edges.append(
                (
                    _end(edge, "from", ids),
                    str(edge["edge_kind"]),
                    _end(edge, "to", ids),
                    valence if isinstance(valence, float) else None,
                ),
            )
        return ids

    def log_metrics(
        self,
        experiment_id: uuid.UUID,
        points: Sequence[MetricPoint],
    ) -> None:
        self.calls.append(("log_metrics", experiment_id, points))
        self.metrics.setdefault(experiment_id, []).extend(points)

    def session_start(self, body: SessionStart) -> SessionStartResponse:
        self.calls.append(("session_start", body))
        session = self._id()
        self.kinds[session] = "AgentSession"
        self.sessions[session] = (body, [])
        return SessionStartResponse(id=session, seq=0, actor=body.actor)

    def append_records(
        self,
        session_id: uuid.UUID,
        *,
        name: str = "",
        manifest: ManifestBody | None = None,
        records: Sequence[RecordBody] = (),
    ) -> None:
        self.calls.append(("append_records", session_id, name, manifest, records))
        held = self.sessions[session_id][1]
        held.extend(records)
        assert manifest is not None
        assert manifest.records == len(held), "a manifest counts all the part holds"

    def add_edge(
        self,
        from_id: uuid.UUID,
        to_id: uuid.UUID,
        edge_kind: str,
        *,
        actor: str,
        valence: float | None = None,
    ) -> None:
        self.calls.append(("add_edge", from_id, to_id, edge_kind, actor, valence))
        self.edges.append((from_id, edge_kind, to_id, valence))

    def session_end(
        self,
        session_id: uuid.UUID,
        body: SessionEnd | None = None,
    ) -> None:
        self.calls.append(("session_end", session_id, body))

    def post(self, path: str, *, body: object = None) -> PlainTree:
        """Publish an Artifact; answer with its id, as the server does."""
        assert path == "/api/artifacts/content"
        self.calls.append(("post", path, body))
        artifact = self._id()
        self.artifacts[artifact] = from_plain(body, dict[str, object])
        return {"artifact_id": str(artifact)}

    def _id(self) -> uuid.UUID:
        return uuid.UUID(int=next(self._ids))


def _end(edge: Mapping[str, object], end: str, ids: Sequence[uuid.UUID]) -> uuid.UUID:
    """Return the id an edge's end names: an item of its batch, or a row by id."""
    index = edge.get(f"{end}_index")
    if isinstance(index, int):
        return ids[index]
    return uuid.UUID(from_plain(edge[f"{end}_id"], str))


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
