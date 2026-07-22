"""Tests for ``ChangeIdMiddleware`` and end-to-end replay via the Idempotency-Key header."""

from __future__ import annotations

from typing import TYPE_CHECKING

import uuid


if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.store.core import Store


class TestMiddleware:
    def test_valid_uuid_header_accepted(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """A well-formed Idempotency-Key passes through and reaches the route."""
        client, _store, _engine = route_client
        resp = client.post(
            "/api/inquiries/issue",
            json={"title": "t"},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        assert resp.status_code == 201

    def test_malformed_uuid_header_returns_400(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """A non-UUID Idempotency-Key is rejected before the route runs."""
        client, _store, _engine = route_client
        resp = client.post(
            "/api/inquiries/issue",
            json={"title": "t"},
            headers={"Idempotency-Key": "not-a-uuid"},
        )
        assert resp.status_code == 400
        assert "Idempotency-Key" in resp.json()["detail"]

    def test_missing_header_falls_back_to_server_mint(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """Requests without Idempotency-Key work as before (server mints uuids)."""
        client, _store, _engine = route_client
        resp = client.post("/api/inquiries/issue", json={"title": "t"})
        assert resp.status_code == 201

    def test_empty_header_returns_400(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        """An empty Idempotency-Key is rejected: the client sent something invalid."""
        client, _store, _engine = route_client
        resp = client.post(
            "/api/inquiries/issue",
            json={"title": "t"},
            headers={"Idempotency-Key": ""},
        )
        assert resp.status_code == 400


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
