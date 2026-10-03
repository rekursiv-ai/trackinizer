"""Tests for the unauthenticated meta routes.

``/api/version``, ``/api/meta/enums``, ``/api/meta/fields`` and ``/api/meta/edges``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast, get_args

import subprocess

from fastapi import FastAPI
from fastapi.testclient import TestClient

import pytest

from trackinizer.lib.custom_json import DictCodec, ListCodec, StrCodec, loads
from trackinizer.server.api import meta_routes
from trackinizer.server.version import build_sha
from trackinizer.types.columns import column_specs
from trackinizer.types.edges import (
    Edge,
    edge_labels,
    edge_topology,
)
from trackinizer.types.inquiries import Belief, Inquiry, Issue, Paper
from trackinizer.wire.bodies import SubmitBelief, SubmitIssue, SubmitPaper
from trackinizer.wire.routes import field_owner_kind


if TYPE_CHECKING:
    import uuid

    import httpx2

    from trackinizer.server.store.core import Store


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(meta_routes.router)
    return TestClient(app)


def test_version_route_returns_sha_without_auth(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe answers with the env SHA and needs no credentials."""
    build_sha.cache_clear()
    monkeypatch.setenv("TRACKINIZER_SHA", "deadbeef")
    try:
        r = client.get("/api/version")
    finally:
        build_sha.cache_clear()
    assert r.status_code == 200
    assert r.json() == {"sha": "deadbeef"}


def test_enums_route_reflects_the_type_literals(client: TestClient) -> None:
    """``/api/meta/enums`` returns every closed set straight from the types.

    The SPA fetches this to fill its ``<select>`` controls; the lists must be
    the type ``Literal`` members verbatim, so a new publication-type /
    issue-kind / edge cannot desync the UI from the server. This is the
    single-source-of-truth guarantee the route exists to provide.
    """
    r = client.get("/api/meta/enums")
    assert r.status_code == 200
    body = DictCodec.coerce(loads(r.content))
    assert ListCodec.coerce(body["status"], str) == list(
        map(str, get_args(cast(object, Issue.Status.__value__))),
    )
    assert ListCodec.coerce(body["judgement"], str) == list(
        map(str, get_args(cast(object, Belief.Judgement.__value__))),
    )
    assert ListCodec.coerce(body["issue_kind"], str) == list(
        map(str, get_args(cast(object, Issue.Kind.__value__))),
    )
    assert ListCodec.coerce(body["publication_type"], str) == list(
        map(str, get_args(cast(object, Paper.PublicationType.__value__))),
    )
    assert ListCodec.coerce(body["edge_kind"], str) == list(
        map(str, get_args(cast(object, Edge.Kind.__value__))),
    )
    assert ListCodec.coerce(body["inquiry_kind_all"], str) == list(
        map(str, get_args(cast(object, Inquiry.InquiryKind.__value__))),
    )


def test_fields_route_matches_server_route_table(client: TestClient) -> None:
    """``/api/meta/fields`` equals ``wire.routes.field_owner_kind`` exactly.

    The SPA builds its per-field edit URL from this; a hand-typed copy in the
    page had drifted (the AgentSession fields ``cli`` / ``cli_session_id`` /
    ``started`` / ``ended`` were missing, so editing them hit the wrong URL).
    Pin the route to the server's authoritative map so it cannot lag again.
    """
    r = client.get("/api/meta/fields")
    assert r.status_code == 200
    body = DictCodec.coerce(loads(r.content))
    assert body == field_owner_kind()
    # The fields that actually drifted must be present and correctly owned.
    # ``ended`` is intentionally NOT a field route: it is stamped only by
    # ``end_session`` (with ``status``), so the lifecycle CHECK can't desync.
    for f in ("cli", "cli_session_id", "started", "rooms"):
        assert body[f] == "agentsession"
    assert "ended" not in body


def test_edges_route_serves_topology_and_labels(client: TestClient) -> None:
    """``/api/meta/edges`` returns per-kind topology AND relation labels.

    The SPA fetches this to build its edge picker (``from_kinds``/``to_kinds``)
    and its ``edgeDisplayName`` labels (``forward``/``inverse``). Citations store
    Artifact -> {Belief, Experiment}, so ``from_kinds`` = artifacts and
    ``to_kinds`` = ``["Belief", "Experiment"]`` for both ``proves`` and
    ``favors``. For-vs-against is the sign of ``valence``, not a separate
    dis-edge kind, so the payload carries no disproves/disfavors entries.
    """
    r = client.get("/api/meta/edges")
    assert r.status_code == 200
    body = DictCodec.coerce(loads(r.content))
    # The payload is topology merged with labels, one entry per kind.
    for kind, topo in edge_topology().items():
        entry = DictCodec.coerce(body[kind])
        assert ListCodec.coerce(entry["from_kinds"], str) == topo["from_kinds"]
        assert ListCodec.coerce(entry["to_kinds"], str) == topo["to_kinds"]
    for kind, lab in edge_labels().items():
        entry = DictCodec.coerce(body[kind])
        assert StrCodec.coerce(entry["forward"]) == lab["forward"]
        assert StrCodec.coerce(entry["inverse"]) == lab["inverse"]
    # Citations are Artifact -> {Belief, Experiment}; both directions agree.
    for kind in ("proves", "favors"):
        entry = DictCodec.coerce(body[kind])
        assert ListCodec.coerce(entry["to_kinds"], str) == ["Belief", "Experiment"]
        assert "Paper" in ListCodec.coerce(entry["from_kinds"], str)
    # cites_paper labels are the CLI aliases, not the raw storage kind.
    cites_paper = DictCodec.coerce(body["cites_paper"])
    assert StrCodec.coerce(cites_paper["forward"]) == "cites"
    assert StrCodec.coerce(cites_paper["inverse"]) == "cited_by"
    # The dropped dis-edge kinds carry no entry (valence sign now).
    for gone in ("disproves", "disfavors", "refutes_experiment"):
        assert gone not in body


def test_edges_route_adds_each_kinds_annotations(client: TestClient) -> None:
    """Each kind lists the annotations it takes, beside the keys it already had.

    The v2 UI guessed them from the topology (priority where an edge joins Issue
    to Issue); a new kind or a changed rule silently broke the guess. Which
    annotations each kind takes against the annotate route is pinned on PGlite
    below; here, the shape: additive, the ``Edge`` field names in field order.
    """
    body = DictCodec.coerce(loads(client.get("/api/meta/edges").content))
    columns = list(column_specs(Edge))
    for kind in edge_topology():
        entry = DictCodec.coerce(body[kind])
        assert set(entry) == {
            "from_kinds",
            "to_kinds",
            "forward",
            "inverse",
            "annotations",
        }
        taken = ListCodec.coerce(entry["annotations"], str)
        assert taken == [column for column in columns if column in taken], kind


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_served_annotations_are_what_the_annotate_route_takes_on_a_real_engine(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """``/api/meta/edges`` serves exactly the annotations each kind's route accepts.

    Every annotation is set on one edge of every kind through
    ``PUT /api/edges/.../<annotation>``: each accepted value must be a served
    annotation and each refusal (422) an unserved one, so the served policy and
    the enforced one cannot drift.
    """
    http, store = pglite_route_client
    edges = await _one_edge_per_kind(store)
    served = {
        kind: ListCodec.coerce(DictCodec.coerce(rule)["annotations"], str)
        for kind, rule in DictCodec.coerce(
            loads((await http.get("/api/meta/edges")).content),
        ).items()
    }
    values: dict[str, object] = {
        "priority": 10,
        "note": "why",
        "valence": 0.25,
        "labels": ["x"],
    }
    assert set(values) == set(column_specs(Edge))
    accepted: dict[str, list[str]] = {}
    for kind, (from_id, to_id) in edges.items():
        accepted[kind] = []
        for annotation, value in values.items():
            response = await http.put(
                f"/api/edges/{from_id}/{kind}/{to_id}/{annotation}",
                json={"value": value},
            )
            assert response.status_code in {200, 422}, response.text
            if response.status_code == 200:
                accepted[kind].append(annotation)
    assert accepted == served


async def _one_edge_per_kind(store: Store) -> dict[str, tuple[uuid.UUID, uuid.UUID]]:
    """Link one ``(from_id, to_id)`` pair per edge kind, each newer to older."""
    account = "alice@example.com"
    issue_old, issue_new = [
        await store.submit_issue(SubmitIssue(account=account, title=title))
        for title in ("older", "newer")
    ]
    belief = await store.submit_belief(SubmitBelief(account=account, title="claim"))
    paper_old, paper_new = [
        await store.submit_paper(SubmitPaper(account=account, title=title))
        for title in ("cited", "citing")
    ]
    edges: dict[str, tuple[uuid.UUID, uuid.UUID]] = {
        "narrows": (issue_new, issue_old),
        "requires": (issue_new, issue_old),
        "produced_by": (belief, issue_old),
        "proves": (paper_new, belief),
        "favors": (paper_old, belief),
        "supersedes": (paper_new, paper_old),
        "cites_paper": (paper_new, paper_old),
    }
    assert set(edges) == set(edge_topology())
    for kind, (from_id, to_id) in edges.items():
        await store.add_edge(
            from_id=from_id,
            to_id=to_id,
            edge_kind=cast(Edge.Kind, kind),
            actor="alice",
        )
    return edges


def test_build_sha_falls_back_to_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no env var and no resolvable git, the SHA is the literal 'unknown'.

    Patch the env away and force the git probe to fail, so the fallback
    branch is exercised deterministically regardless of the test host.
    """
    monkeypatch.delenv("TRACKINIZER_SHA", raising=False)

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise OSError("git unavailable")

    monkeypatch.setattr(subprocess, "run", _boom)
    build_sha.cache_clear()
    try:
        assert build_sha() == "unknown"
    finally:
        build_sha.cache_clear()


def test_build_sha_whitespace_env_falls_back_to_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whitespace-only ``TRACKINIZER_SHA`` resolves to "unknown", not "".

    The env is checked for truthiness, so ``"   "`` is truthy; stripping it
    afterward yields ``""``, breaking the docstring promise of a SHA or
    "unknown" (TRK-SRV-003). The git probe is forced to fail so the only
    way to reach a non-"unknown" answer would be the (blank) env value.
    """
    monkeypatch.setenv("TRACKINIZER_SHA", "   ")

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise OSError("git unavailable")

    monkeypatch.setattr(subprocess, "run", _boom)
    build_sha.cache_clear()
    try:
        assert build_sha() == "unknown"
    finally:
        build_sha.cache_clear()


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
