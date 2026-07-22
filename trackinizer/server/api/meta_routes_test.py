"""Tests for the unauthenticated meta routes: ``/api/version``,
``/api/meta/enums``, ``/api/meta/edges``, and the SPA-vs-server drift guards.
"""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import re
import subprocess

from fastapi import FastAPI
from fastapi.testclient import TestClient

import pytest

from trackinizer.server.api import meta_routes
from trackinizer.server.version import build_sha
from trackinizer.types.edges import (
    Edge,
    edge_labels,
    edge_topology,
)
from trackinizer.types.inquiries import Belief, Inquiry, Issue, Paper
from trackinizer.wire.routes import field_owner_kind


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(meta_routes.router)
    return TestClient(app)


def test_version_route_returns_sha_without_auth(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
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
    body = r.json()
    assert body["status"] == list(get_args(Issue.Status.__value__))
    assert body["judgement"] == list(get_args(Belief.Judgement.__value__))
    assert body["issue_kind"] == list(get_args(Issue.Kind.__value__))
    assert body["publication_type"] == list(get_args(Paper.PublicationType.__value__))
    assert body["edge_kind"] == list(get_args(Edge.Kind.__value__))
    assert body["inquiry_kind_all"] == list(get_args(Inquiry.InquiryKind.__value__))


def test_fields_route_matches_server_route_table(client: TestClient) -> None:
    """``/api/meta/fields`` equals ``wire.routes.field_owner_kind`` exactly.

    The SPA builds its per-field edit URL from this; a hand-typed copy in the
    page had drifted (the AgentSession fields ``cli`` / ``cli_session_id`` /
    ``started`` / ``ended`` were missing, so editing them hit the wrong URL).
    Pin the route to the server's authoritative map so it cannot lag again.
    """
    r = client.get("/api/meta/fields")
    assert r.status_code == 200
    assert r.json() == field_owner_kind()
    # The fields that actually drifted must be present and correctly owned.
    # ``ended`` is intentionally NOT a field route: it is stamped only by
    # ``end_session`` (with ``status``), so the lifecycle CHECK can't desync.
    for f in ("cli", "cli_session_id", "started", "rooms"):
        assert r.json()[f] == "agentsession"
    assert "ended" not in r.json()


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
    body = r.json()
    # The payload is topology merged with labels, one entry per kind.
    for kind, topo in edge_topology().items():
        assert body[kind]["from_kinds"] == topo["from_kinds"]
        assert body[kind]["to_kinds"] == topo["to_kinds"]
    for kind, lab in edge_labels().items():
        assert body[kind]["forward"] == lab["forward"]
        assert body[kind]["inverse"] == lab["inverse"]
    # Citations are Artifact -> {Belief, Experiment}; both directions agree.
    for kind in ("proves", "favors"):
        assert body[kind]["to_kinds"] == ["Belief", "Experiment"]
        assert "Paper" in body[kind]["from_kinds"]
    # cites_paper labels are the CLI aliases, not the raw storage kind.
    assert body["cites_paper"]["forward"] == "cites"
    assert body["cites_paper"]["inverse"] == "cited_by"
    # The dropped dis-edge kinds carry no entry (valence sign now).
    for gone in ("disproves", "disfavors", "refutes_experiment"):
        assert gone not in body


def test_spa_does_not_hardcode_enum_lists() -> None:
    """index.html must source its enum VALUES arrays from the route, not a copy.

    Guards the maintenance burden the route removes: if a future edit pastes a
    publication-type / judgement list back into the page, this fails. The five
    ``*_VALUES`` / ``EDGE_KINDS`` arrays must stay declared EMPTY (filled from
    ``/api/meta/enums`` at boot); a non-empty literal is a re-pasted copy.

    Scoped to the array declarations only -- lone semantic uses of a member
    (a ``switch`` case, an equality guard, a button action) are fine and do not
    desync a dropdown, so they are not flagged.
    """
    html = (Path(__file__).resolve().parents[1] / "assets" / "index.html").read_text()
    arrays = (
        "STATUS_VALUES",
        "JUDGEMENT_VALUES",
        "ISSUE_KIND_VALUES",
        "PUBLICATION_TYPE_VALUES",
        "EDGE_KINDS",
        "ALL_KINDS",
    )
    for name in arrays:
        match = re.search(rf"\b{name}\s*=\s*\[(.*?)\]", html, re.DOTALL)
        assert match is not None, f"{name} declaration not found in index.html"
        body = match.group(1).strip()
        assert body == "", (
            f"{name} is hardcoded in index.html; declare it empty and fill it "
            f"from /api/meta/enums (got: [{body[:60]}...])"
        )
        # A boot-filled array with no read site is dead weight: it pays a
        # blocking-XHR cost at boot for nothing. Each array must appear beyond
        # its declaration and its single `.push(...)` fill (>2 mentions).
        mentions = len(re.findall(rf"\b{name}\b", html))
        assert mentions > 2, (
            f"{name} is filled at boot but never read ({mentions} mentions); "
            "drop the array and its server enum key if it has no consumer"
        )
    # FIELD_OWNER_KIND is an object literal, derived from /api/meta/fields. It
    # had drifted (missing AgentSession fields); pin it empty so it stays
    # server-sourced.
    fok = re.search(r"\bFIELD_OWNER_KIND\s*=\s*(\{.*?\})", html, re.DOTALL)
    assert fok is not None, "FIELD_OWNER_KIND declaration not found"
    assert fok.group(1).strip() == "{}", (
        "FIELD_OWNER_KIND is hardcoded in index.html; declare it empty and fill "
        "it from /api/meta/fields so it cannot lag the server route table"
    )
    assert "/api/meta/fields" in html, "SPA must fetch /api/meta/fields at boot"


def test_spa_derives_edge_topology_from_route() -> None:
    """index.html must source the edge topology from ``/api/meta/edges``.

    A citation-direction change can break the SPA when edge directions are a
    hand-typed copy of the schema. The SPA now declares ``EDGE_TOPOLOGY`` empty
    and fills it from the route; a non-empty literal is a re-pasted copy that
    could drift again.
    """
    html = (Path(__file__).resolve().parents[1] / "assets" / "index.html").read_text()
    match = re.search(r"\bEDGE_TOPOLOGY\s*=\s*(\{.*?\})", html, re.DOTALL)
    assert match is not None, "EDGE_TOPOLOGY declaration not found in index.html"
    assert match.group(1).strip() == "{}", (
        "EDGE_TOPOLOGY is hardcoded in index.html; declare it empty and fill it "
        "from /api/meta/edges so a direction flip cannot desync the picker"
    )
    assert "/api/meta/edges" in html, "SPA must fetch /api/meta/edges at boot"


def test_spa_drops_removed_websearch_results_wiring() -> None:
    """index.html must not reference the removed ``WebSearch.results`` field.

    The Python removal left the SPA wiring stale (submit schema, owner map,
    detail view, edit/submit branches), so the WebSearch form 422'd. This pins
    that none of those references return.
    """
    html = (Path(__file__).resolve().parents[1] / "assets" / "index.html").read_text()
    for leaked in ('"typed-results"', 'results: "websearch"', 'field === "results"'):
        assert leaked not in html, (
            f"{leaked} is stale WebSearch.results wiring in index.html; "
            "findings are produces edges now, not a column"
        )


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
