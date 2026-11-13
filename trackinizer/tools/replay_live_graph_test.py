"""Tests for live graph replay diagnostics."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict, cast, override

import itertools
import logging
import sys
import uuid

import httpx2
import pytest


if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from trackinizer.types.inquiries import Inquiry

from treekle import PlainTree, from_plain, json_loads

from trackinizer.client.client import Client
from trackinizer.client.errors import ClientError
from trackinizer.tools import replay_live_graph
from trackinizer.trax.profile import Profile


class _FailingInsertClient(Client):
    """Client seam for replay insert diagnostics."""

    def __init__(self) -> None:
        self.base_url = "http://target.test"
        self.submitted: list[tuple[str, object]] = []

    @override
    def submit_batch(
        self,
        items: Sequence[tuple[Inquiry.InquiryKind, Mapping[str, object]]],
        *,
        edges: Sequence[Mapping[str, object]] = (),
        actor: Inquiry.Actor | None = None,
    ) -> list[uuid.UUID]:
        del items, edges, actor
        raise ClientError("POST /api/inquiries/batch failed: [Errno 61]")

    @override
    def submit(
        self,
        kind: Inquiry.InquiryKind,
        body: Mapping[str, object],
    ) -> uuid.UUID:
        self.submitted.append((kind, body))
        raise ClientError(f"POST /api/inquiries/{kind.lower()} failed: [Errno 61]")


class _MixedInsertClient(_FailingInsertClient):
    @override
    def submit(
        self,
        kind: Inquiry.InquiryKind,
        body: Mapping[str, object],
    ) -> uuid.UUID:
        self.submitted.append((kind, body))
        if kind == "Issue":
            return uuid.UUID("11111111-1111-1111-1111-111111111111")
        raise ClientError(f"POST /api/inquiries/{kind.lower()} failed: [Errno 61]")


def _detail(
    *,
    kind: str,
    node_id: str,
    seq: int,
    title: str,
) -> dict[str, object]:
    return {
        "self": {
            "id": node_id,
            "kind": kind,
            "seq": seq,
            "title": title,
        },
        "edges": {},
        "backlinks": {},
    }


def test_insert_chunk_logs_batch_and_row_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fallback logs enough context to identify the poisoned replay chunk."""
    chunk = [
        _detail(
            kind="WebSearch",
            node_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            seq=12,
            title="fast api search",
        ),
        _detail(
            kind="Paper",
            node_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            seq=7,
            title="attention paper",
        ),
    ]
    caplog.set_level(logging.WARNING, logger=replay_live_graph.__name__)

    replay_live_graph._insert_chunk(_FailingInsertClient(), chunk, {}, set())

    log_text = caplog.text
    assert "batch insert failed" in log_text
    assert "target=http://target.test" in log_text
    assert "WebSearch#12" in log_text
    assert "Paper#7" in log_text
    assert "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" in log_text
    assert "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb" in log_text
    assert "fast api search" in log_text
    assert "attention paper" in log_text


def test_insert_chunk_keeps_successes_after_diagnostic_fallback() -> None:
    """Diagnostics do not change fallback semantics for rows that still insert."""
    target = _MixedInsertClient()
    id_map: dict[str, str] = {}

    replay_live_graph._insert_chunk(
        target,
        [
            _detail(
                kind="Issue",
                node_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                seq=1,
                title="ok",
            ),
            _detail(
                kind="Paper",
                node_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                seq=2,
                title="bad",
            ),
        ],
        id_map,
        set(),
    )

    assert id_map == {
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa": "11111111-1111-1111-1111-111111111111",
    }


def test_detail_order_key_uses_source_created_then_id() -> None:
    """Seed replay writes the crawled component in deterministic authoring order."""
    old_high_id = _detail(
        kind="Issue",
        node_id="ffffffff-ffff-ffff-ffff-ffffffffffff",
        seq=1,
        title="old high id",
    )
    old_low_id = _detail(
        kind="Issue",
        node_id="00000000-0000-0000-0000-000000000000",
        seq=2,
        title="old low id",
    )
    new = _detail(
        kind="Issue",
        node_id="11111111-1111-1111-1111-111111111111",
        seq=3,
        title="new",
    )
    old_high_self = old_high_id["self"]
    old_low_self = old_low_id["self"]
    new_self = new["self"]
    assert isinstance(old_high_self, dict)
    assert isinstance(old_low_self, dict)
    assert isinstance(new_self, dict)
    old_high_self["created"] = "2026-01-01T00:00:00+00:00"
    old_low_self["created"] = "2026-01-01T00:00:00+00:00"
    new_self["created"] = "2026-01-02T00:00:00+00:00"

    ordered = sorted(
        [new, old_high_id, old_low_id],
        key=replay_live_graph._detail_order_key,
    )

    ids = [
        from_plain(from_plain(detail["self"], dict[str, object])["id"], str)
        for detail in ordered
    ]
    assert ids == [
        "00000000-0000-0000-0000-000000000000",
        "ffffffff-ffff-ffff-ffff-ffffffffffff",
        "11111111-1111-1111-1111-111111111111",
    ]


@pytest.mark.parametrize("other", [["--traverse"], ["--limit", "2"]])
def test_seed_rejects_ignored_options(
    other: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["replay", "--target", "https://target.test", "--seed", "Issue#1", *other],
    )

    def connect(source: str) -> Client:
        del source
        pytest.fail("argument validation must precede connections")

    monkeypatch.setattr(replay_live_graph, "_source_client", connect)
    with pytest.raises(SystemExit, match="2"):
        replay_live_graph.main()


_SOURCE = "https://source.test"
_TARGET = "https://target.test"


class _Server:
    """An in-memory trackinizer: the routes the replay reads, and those it writes.

    The source graph is ``nodes`` and ``edges``; every write the replay sends lands
    in ``created`` and ``linked`` under the target's own fresh ids.
    """

    def __init__(
        self,
        nodes: Sequence[Mapping[str, PlainTree]] = (),
        edges: Sequence[tuple[str, str, str, float | None]] = (),
        *,
        refuse_edge: str = "",
    ) -> None:
        self.nodes = {from_plain(node["id"], str): dict(node) for node in nodes}
        self.edges = list(edges)
        self.refuse_edge = refuse_edge
        self.created: dict[str, dict[str, object]] = {}
        self.linked: list[tuple[str, str, str, float | None]] = []
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        parts = request.url.path.strip("/").split("/")[1:]
        if request.method == "GET":
            return httpx2.Response(200, json=self._read(parts, request.url.params))
        body = json_loads(request.content, dict[str, object])
        if parts[0] == "edges":
            return self._link(parts[1:], body)
        if parts == ["inquiries", "batch"]:
            items = from_plain(body["items"], list[dict[str, object]])
            if any(item.get("title") == "poison" for item in items):
                return httpx2.Response(422, json={"detail": "poison row"})
            return httpx2.Response(
                200,
                json={"ids": [self._create(item) for item in items]},
            )
        if body.get("title") == "poison":
            return httpx2.Response(422, json={"detail": "poison row"})
        return httpx2.Response(200, json={"id": self._create(body)})

    def titled(self) -> dict[str, str]:
        """Return each created node's title by its target id."""
        return {
            node_id: from_plain(node["title"], str)
            for node_id, node in self.created.items()
        }

    def _read(self, parts: list[str], params: httpx2.QueryParams) -> PlainTree:
        if parts == ["inquiries"]:
            rows: list[PlainTree] = [
                {key: value for key, value in node.items() if key != "kind"}
                for node in self.nodes.values()
                if node["kind"] == params["kind"] and not int(params["offset"])
            ]
            return rows
        if parts[0] == "inquiries":
            kind, seq = parts[1:]
            return next(
                {"id": node_id}
                for node_id, node in self.nodes.items()
                if node["kind"] == kind and str(node["seq"]) == seq
            )
        node_id = parts[-1]
        return {
            "self": {"id": node_id, **self.nodes[node_id]},
            "edges": self._peers(node_id, outbound=True),
            "backlinks": self._peers(node_id, outbound=False),
        }

    def _peers(self, node_id: str, *, outbound: bool) -> dict[str, PlainTree]:
        peers: dict[str, PlainTree] = {}
        for from_id, to_id, kind, valence in self.edges:
            near, far = (from_id, to_id) if outbound else (to_id, from_id)
            if near == node_id:
                row = cast(list[PlainTree], peers.setdefault(kind, []))
                row.append({"id": far, "valence": valence})
        return peers

    def _create(self, body: Mapping[str, object]) -> str:
        node_id = str(uuid.UUID(int=len(self.created) + 1))
        self.created[node_id] = {
            key: value
            for key, value in body.items()
            if key not in ("idempotency_key", "kind")
        }
        return node_id

    def _link(self, parts: list[str], body: Mapping[str, object]) -> httpx2.Response:
        from_id, kind, to_id = parts
        if kind == self.refuse_edge:
            return httpx2.Response(409, json={"detail": "provenance cycle"})
        valence = body.get("valence")
        self.linked.append(
            (
                from_id,
                to_id,
                kind,
                None if valence is None else from_plain(valence, float),
            ),
        )
        return httpx2.Response(200, json={"change_id": None, "created": True})


def _node(seq: int, kind: str = "Issue", **fields: PlainTree) -> dict[str, PlainTree]:
    return {
        "id": str(uuid.UUID(int=1_000 + seq)),
        "kind": kind,
        "seq": seq,
        "title": f"{kind}#{seq}",
        "status": "active",
        "created": f"2026-01-01T00:00:{seq:02d}+00:00",
        **fields,
    }


def _edge(
    child: Mapping[str, PlainTree],
    kind: str,
    parent: Mapping[str, PlainTree],
    valence: float | None = None,
) -> tuple[str, str, str, float | None]:
    return (from_plain(child["id"], str), from_plain(parent["id"], str), kind, valence)


def _links(target: _Server) -> set[tuple[str, str, str, float | None]]:
    """Return the target's edges, each endpoint named by its title."""
    titles = target.titled()
    return {
        (titles[from_id], titles[to_id], kind, valence)
        for from_id, to_id, kind, valence in target.linked
    }


@pytest.fixture(autouse=True)
def _restore_httpx2_level() -> Iterator[None]:
    """Put back the httpx2 logger level, which ``main`` quiets for the process."""
    quieted = logging.getLogger("httpx2")
    level = quieted.level
    yield
    quieted.setLevel(level)


def _run(
    monkeypatch: pytest.MonkeyPatch,
    source: _Server,
    target: _Server,
    *flags: str,
    profile: Profile | None = None,
) -> int:
    """Run ``main`` against ``source`` and ``target`` through their own transports."""
    servers = {_SOURCE: source, _TARGET: target}

    def connect(base_url: str, *, author: str = "", api_key: str = "") -> Client:
        return Client(
            base_url,
            author=author,
            api_key=api_key,
            transport=httpx2.MockTransport(servers[base_url]),
        )

    monkeypatch.setattr(replay_live_graph, "Client", connect)
    monkeypatch.setattr(
        replay_live_graph,
        "load_profile",
        lambda: profile or Profile(url=_SOURCE, api_key="trax_key"),
    )
    monkeypatch.setattr(sys, "argv", ["replay", "--target", _TARGET, *flags])
    return replay_live_graph.main()


def _graph() -> tuple[
    list[dict[str, PlainTree]],
    list[tuple[str, str, str, float | None]],
]:
    """Return a small source graph: two issues, a belief, a paper, and their edges."""
    root = _node(1)
    child = _node(2)
    belief = _node(3, "Belief", judgement="true", confidence=0.8)
    paper = _node(4, "Paper", source="arxiv")
    edges = [
        _edge(child, "narrows", root),
        _edge(belief, "produced_by", child, valence=None),
        _edge(paper, "proves", belief, valence=0.5),
        _edge(child, "subscribes", root),
    ]
    return [root, child, belief, paper], edges


_GRAPH_LINKS = {
    ("Issue#2", "Issue#1", "narrows", None),
    ("Belief#3", "Issue#2", "produced_by", None),
    ("Paper#4", "Belief#3", "proves", 0.5),
}


@pytest.mark.parametrize("traverse", [False, True], ids=["bulk", "traverse"])
def test_main_replays_every_node_and_typed_edge_under_fresh_ids(
    traverse: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An edge kind the graph does not know is dropped; the rest are rewired."""
    nodes, edges = _graph()
    source = _Server(nodes, edges)
    target = _Server()
    flags = ["--traverse", "--delay", "0.0001"] if traverse else []

    assert _run(monkeypatch, source, target, *flags) == 0

    assert sorted(target.titled().values()) == [
        "Belief#3",
        "Issue#1",
        "Issue#2",
        "Paper#4",
    ]
    assert _links(target) == _GRAPH_LINKS
    belief = next(n for n in target.created.values() if n["title"] == "Belief#3")
    assert belief == {
        "title": "Belief#3",
        "status": "active",
        "judgement": "true",
        "confidence": 0.8,
    }
    assert {r.headers["Authorization"] for r in source.requests} == {
        "Bearer trax_key",
    }


def test_traverse_inserts_oldest_first_one_node_per_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Creation time orders the inserts, not id or listing order."""
    nodes, edges = _graph()
    nodes[0]["created"] = "2026-01-01T00:00:09+00:00"
    target = _Server()

    _run(monkeypatch, _Server(nodes[::-1], edges), target, "--traverse")

    assert list(target.titled().values()) == [
        "Issue#2",
        "Belief#3",
        "Paper#4",
        "Issue#1",
    ]
    assert not any(r.url.path.endswith("/batch") for r in target.requests)


def test_a_limit_samples_every_kind_in_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """A small cap takes one of each kind before a second of any."""
    nodes = [_node(1), _node(2), _node(3), _node(4, "Belief"), _node(5, "Paper")]
    target = _Server()

    _run(monkeypatch, _Server(nodes), target, "--limit", "4")

    assert sorted(target.titled().values()) == [
        "Belief#4",
        "Issue#1",
        "Issue#2",
        "Paper#5",
    ]


def test_a_limit_above_the_graph_takes_it_all(monkeypatch: pytest.MonkeyPatch) -> None:
    target = _Server()

    _run(monkeypatch, _Server([_node(1), _node(2, "Belief")]), target, "--limit", "9")

    assert sorted(target.titled().values()) == ["Belief#2", "Issue#1"]


def test_a_source_kind_without_graph_fields_replays_its_title_and_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _Server()

    _run(monkeypatch, _Server([_node(1, "AccessGroup", members=["ada"])]), target)

    assert list(target.created.values()) == [
        {"title": "AccessGroup#1", "status": "active"},
    ]


def test_seeds_stream_only_their_connected_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both edge directions are crawled; a node no seed reaches is never read."""
    nodes, edges = _graph()
    chain = [_node(seq) for seq in range(10, 16)]
    stranger = _node(30)
    edges += [_edge(chain[0], "requires", nodes[0])]
    edges += [_edge(b, "requires", a) for a, b in itertools.pairwise(chain)]
    source = _Server([*nodes, *chain, stranger], edges)
    target = _Server(refuse_edge="produced_by")
    root_id = from_plain(nodes[0]["id"], str)

    assert (
        _run(
            monkeypatch,
            source,
            target,
            "--seed",
            "Issue#2",
            "--seed",
            f" {root_id} ",
            "--seed",
            "Issue#1",
            "--delay",
            "0.0001",
        )
        == 0
    )

    assert sorted(target.titled().values()) == sorted(
        [
            "Belief#3",
            "Paper#4",
            *(f"Issue#{seq}" for seq in (1, 2, 10, 11, 12, 13, 14, 15)),
        ],
    )
    assert _links(target) == {
        ("Issue#2", "Issue#1", "narrows", None),
        ("Paper#4", "Belief#3", "proves", 0.5),
        ("Issue#10", "Issue#1", "requires", None),
        *(
            (f"Issue#{seq + 1}", f"Issue#{seq}", "requires", None)
            for seq in range(10, 15)
        ),
    }
    belief = next(n for n in target.created.values() if n["title"] == "Belief#3")
    assert (belief["judgement"], belief["confidence"]) == ("true", 0.8)
    assert f"/api/web/get/{stranger['id']}" not in {r.url.path for r in source.requests}
    batches = [r for r in target.requests if r.url.path.endswith("/batch")]
    assert len(batches) > 1


def test_a_poisoned_seed_batch_still_lands_its_good_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good = _node(1)
    bad = _node(2, title="poison")
    target = _Server()

    _run(
        monkeypatch,
        _Server([good, bad], [_edge(bad, "narrows", good)]),
        target,
        "--seed",
        "Issue#1",
        "--delay",
        "0",
    )

    assert list(target.titled().values()) == ["Issue#1"]
    assert target.linked == []


def test_a_large_pull_logs_its_progress(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    nodes = [_node(seq) for seq in range(100)]
    caplog.set_level(logging.INFO, logger=replay_live_graph.__name__)

    _run(monkeypatch, _Server(nodes), _Server(), "--traverse", "--delay", "0")

    assert "[replay]   edges: 100/100 nodes" in caplog.messages
    assert "[replay]   inserted 50/100 nodes" in caplog.messages
    assert "[replay]   inserted 100/100 nodes" in caplog.messages


def test_bulk_replay_batches_nodes_then_writes_each_edge() -> None:
    """Edges to a node that was not replayed are skipped, never sent."""
    nodes: list[dict[str, object]] = [
        {"id": "s1", "kind": "Issue", "created": None, "title": "a"},
        {"id": "s2", "kind": "Issue", "created": None, "title": "b"},
        {"id": "s3", "kind": "Issue", "created": None, "title": "c"},
    ]
    edges: list[tuple[str, str, str, float | None]] = [
        ("s2", "s1", "narrows", None),
        ("s3", "s2", "narrows", 0.25),
        ("s3", "gone", "narrows", None),
    ]
    target = _Server()
    with Client(_TARGET, transport=httpx2.MockTransport(target)) as client:
        replay_live_graph._replay(client, nodes, edges, batch=2)

    batches = [json_loads(r.content, _Batch) for r in target.requests[:2]]
    assert [len(batch["items"]) for batch in batches] == [2, 1]
    assert _links(target) == {("b", "a", "narrows", None), ("c", "b", "narrows", 0.25)}


def test_the_source_needs_a_key_and_an_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(SystemExit, match="no api_key in the active trax profile"):
        _run(monkeypatch, _Server(), _Server(), profile=Profile(url=_SOURCE))
    with pytest.raises(SystemExit, match="no --source given"):
        _run(monkeypatch, _Server(), _Server(), profile=Profile(url="", api_key="k"))


def test_source_flag_overrides_the_profile_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _Server([_node(1)])
    target = _Server()

    _run(
        monkeypatch,
        source,
        target,
        "--source",
        _SOURCE,
        profile=Profile(url="https://elsewhere.test", author="ada", api_key="k"),
    )

    assert list(target.titled().values()) == ["Issue#1"]
    assert source.requests


class _Batch(TypedDict):
    items: list[dict[str, object]]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
