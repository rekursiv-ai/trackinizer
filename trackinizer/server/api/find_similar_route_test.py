"""``GET /api/inquiries/similar`` -- the HTTP surface of embedding search.

Covers what only the route layer can get wrong: query-param validation, the
400 mapping for a store that cannot rank, the ``distance`` key the handler
adds on top of the inquiry's own fields, and registration order (a literal
path segment declared after ``/{target_id}`` would be swallowed by it).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import uuid

from fastapi.testclient import TestClient

import pytest

from trackinizer.conftest import FakeEngine, make_store
from trackinizer.lib.custom_json import DictCodec, ListCodec
from trackinizer.lib.postgres import DatabaseEngine
from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import (
    answer_account_active,
    clear_identity_override,
    install_identity,
    make_test_identity,
)
from trackinizer.server.embedder import StubEmbedder
from trackinizer.server.store.core import Store


if TYPE_CHECKING:
    from collections.abc import Iterator

    from trackinizer.types.inquiries import Inquiry


class _SemanticEmbedder:
    """Declares itself meaning-bearing so the route's guard lets it through.

    The vector is constant: the FakeEngine returns canned rows regardless of
    the query, so what is asserted here is the HTTP contract, not ranking
    quality. Ranking is covered against a real substrate in
    ``store/find_similar_pglite_test.py``.
    """

    name = "fake-semantic"
    dim = StubEmbedder.dim
    is_semantic = True

    async def embed(self, text: str) -> list[float]:
        del text
        return [1.0] + [0.0] * (StubEmbedder.dim - 1)


def _row(
    target_id: uuid.UUID,
    *,
    kind: Inquiry.InquiryKind = "Belief",
    distance: float = 0.25,
) -> dict[str, object]:
    """One ``inquiries`` row joined with its embedding distance."""
    now = datetime.now(UTC)
    return {
        "id": target_id,
        "kind": kind,
        "seq": 1,
        "owner": "alice",
        "account": "alice",
        "status": "active",
        "title": "a belief",
        "description": "",
        "labels": [],
        "subscribers": [],
        "created": now,
        "modified": now,
        "marginal_cost_agent_usd": 0.0,
        "marginal_cost_resource_usd": 0.0,
        "distance": distance,
    }


@pytest.fixture
def semantic_client() -> Iterator[tuple[TestClient, FakeEngine]]:
    """``route_client``'s shape, but with an embedder that can rank.

    The shared fixture builds its Store with ``StubEmbedder``, which the route
    deliberately refuses, so the success paths need their own store.
    """
    _stub_store, engine = make_store()
    answer_account_active(engine)
    store = Store(cast(DatabaseEngine, engine), embed=_SemanticEmbedder())
    prev_engine = getattr(app.state, "engine", None)
    prev_store = getattr(app.state, "store", None)
    app.state.engine = engine
    app.state.store = store
    install_identity(make_test_identity())
    try:
        yield TestClient(app), engine
    finally:
        clear_identity_override()
        if prev_engine is None:
            del app.state.engine
        else:
            app.state.engine = prev_engine
        if prev_store is None:
            del app.state.store
        else:
            app.state.store = prev_store


def test_returns_rows_with_a_distance_key(
    semantic_client: tuple[TestClient, FakeEngine],
) -> None:
    """The handler adds ``distance`` alongside the inquiry's own fields."""
    client, engine = semantic_client
    target_id = uuid.uuid4()
    # find_similar: the join, then the bulk outbound / inbound edge fetches.
    engine.conn.fetch.side_effect = [[_row(target_id)], [], []]

    response = client.get("/api/inquiries/similar", params={"q": "momentum"})

    assert response.status_code == 200
    rows = ListCodec.coerce(response.json(), object)
    first = DictCodec.coerce(rows[0])
    assert first["kind"] == "Belief"
    assert first["distance"] == 0.25


def test_kind_filter_reaches_the_query(
    semantic_client: tuple[TestClient, FakeEngine],
) -> None:
    """``kind`` must be bound into the SQL, not silently dropped."""
    client, engine = semantic_client
    engine.conn.fetch.side_effect = [[], [], []]

    response = client.get(
        "/api/inquiries/similar",
        params={"q": "momentum", "kind": "Belief"},
    )

    assert response.status_code == 200
    sql, *params = engine.conn.fetch.call_args_list[0].args
    assert "i.kind" in str(sql)
    assert "Belief" in [str(p) for p in params]


def test_rejects_an_unknown_kind(
    semantic_client: tuple[TestClient, FakeEngine],
) -> None:
    """``kind`` is a closed set; a typo is a 422, not an empty result."""
    client, _engine = semantic_client

    response = client.get(
        "/api/inquiries/similar",
        params={"q": "momentum", "kind": "Belef"},
    )

    assert response.status_code == 422


def test_requires_a_non_empty_query(
    semantic_client: tuple[TestClient, FakeEngine],
) -> None:
    """An absent or blank ``q`` cannot produce a meaningful ranking."""
    client, _engine = semantic_client

    assert client.get("/api/inquiries/similar").status_code == 422
    assert client.get("/api/inquiries/similar", params={"q": ""}).status_code == 422


def test_unknown_model_is_a_400_not_a_500(
    semantic_client: tuple[TestClient, FakeEngine],
) -> None:
    """Naming an unregistered embedder is a caller error."""
    client, _engine = semantic_client

    response = client.get(
        "/api/inquiries/similar",
        params={"q": "momentum", "model": "not-registered"},
    )

    assert response.status_code == 400
    assert "no embedder named" in response.text


def test_stub_embedder_is_a_400_with_an_explanation(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    """The default store cannot rank, and must say so rather than guess.

    Uses the shared fixture precisely because it builds with
    ``StubEmbedder`` -- the configuration every deployment runs today.
    """
    client, _store, _engine = route_client

    response = client.get("/api/inquiries/similar", params={"q": "momentum"})

    assert response.status_code == 400
    assert "does not support semantic search" in response.text


def test_literal_path_is_not_shadowed_by_the_uuid_route(
    semantic_client: tuple[TestClient, FakeEngine],
) -> None:
    """Registration order is load-bearing.

    ``/api/inquiries/{target_id}`` is declared later but would match
    ``/api/inquiries/similar`` first if this route moved below it, parsing
    "similar" as a UUID and answering 422. Starlette matches in registration
    order, so this pins the ordering rather than trusting it.
    """
    client, engine = semantic_client
    engine.conn.fetch.side_effect = [[], [], []]

    response = client.get("/api/inquiries/similar", params={"q": "momentum"})

    assert response.status_code == 200, (
        "the literal /similar path was shadowed by a dynamic route"
    )
