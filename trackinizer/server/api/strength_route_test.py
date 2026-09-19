"""``GET /api/inquiries/{target_id}/strength`` -- the HTTP surface of
belief-strength.

Ranking correctness against a real substrate is covered in
``store/strength_for_pglite_test.py``; this file covers what only the route
layer can get wrong: the 404 mapping, the response shape, and registration
order (a static suffix declared after ``/{kind}/{seq}`` would have its
target id parsed as a ``seq`` and rejected).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import uuid


if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.store.core import Store


def test_returns_the_strength_key(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    """A row with no evidence answers the neutral base score."""
    client, _store, engine = route_client
    target_id = uuid.uuid4()
    engine.conn.fetchval.side_effect = [1]  # existence check
    engine.conn.fetch.side_effect = [[]]  # no proving edges

    response = client.get(f"/api/inquiries/{target_id}/strength")

    assert response.status_code == 200
    assert response.json() == {"strength": 0.5}


def test_unknown_id_is_a_404(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    """Nothing at that id is a 404, not a 200 with a made-up score."""
    client, _store, engine = route_client
    engine.conn.fetchval.side_effect = [None]  # existence check misses

    response = client.get(f"/api/inquiries/{uuid.uuid4()}/strength")

    assert response.status_code == 404


def test_literal_suffix_is_not_shadowed_by_the_seq_route(
    route_client: tuple[TestClient, Store, FakeEngine],
) -> None:
    """Registration order is load-bearing.

    ``/api/inquiries/{kind}/{seq}`` is declared later but would match
    ``/api/inquiries/{target_id}/strength`` first if this route moved below
    it, parsing "strength" as a ``seq`` and answering 422.
    """
    client, _store, engine = route_client
    target_id = uuid.uuid4()
    engine.conn.fetchval.side_effect = [1]
    engine.conn.fetch.side_effect = [[]]

    response = client.get(f"/api/inquiries/{target_id}/strength")

    assert response.status_code == 200, (
        "the literal /strength suffix was shadowed by the {kind}/{seq} route"
    )
