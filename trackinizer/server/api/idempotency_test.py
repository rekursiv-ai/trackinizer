"""Tests for ``ChangeIdMiddleware`` and end-to-end replay via the Idempotency-Key header."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import uuid

from fastapi.responses import JSONResponse
from starlette.requests import Request
from treekle import from_plain

import pytest

from trackinizer.server.api.idempotency import ChangeIdMiddleware
from trackinizer.server.store.change_id_slot import _peek_client_change_id


if TYPE_CHECKING:
    from fastapi.testclient import TestClient
    from starlette.types import Message, Receive, Scope, Send

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
        body = from_plain(resp.json(), dict[str, object])
        assert body == {"detail": "Idempotency-Key is not a valid UUID"}

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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [[], [(b"idempotency-key", str(uuid.uuid4()).encode())]],
    ids=["no key", "a key"],
)
async def test_the_app_behind_it_hears_that_its_caller_has_gone(
    headers: list[tuple[bytes, bytes]],
) -> None:
    """A held wait asks so before it hands a start out, lest a dead daemon take it."""
    heard: list[bool] = []
    keys: list[object] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        keys.extend(
            [_peek_client_change_id(), cast(object, request.state.idempotency_key)],
        )
        # The first read takes the request's (empty) body; the next its caller's end.
        heard.extend([await request.is_disconnected() for _ in range(2)])
        await JSONResponse({})(scope, receive, send)

    sent: list[Message] = []
    said = [
        {"type": "http.request", "body": b"", "more_body": False},
        {"type": "http.disconnect"},
    ]

    async def receive() -> Message:
        return said.pop(0) if said else {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    await ChangeIdMiddleware(app)(
        {"type": "http", "method": "GET", "path": "/", "headers": headers},
        receive,
        send,
    )

    assert heard == [False, True]
    assert keys == [uuid.UUID(headers[0][1].decode()) if headers else None] * 2
    # Cleared once the request is done, so nothing after it reads the key.
    assert _peek_client_change_id() is None


@pytest.mark.asyncio
async def test_a_websocket_passes_through_untouched() -> None:
    seen: list[tuple[Scope, Receive, Send]] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append((scope, receive, send))

    async def nothing() -> Message:
        return {}

    async def drop(message: Message) -> None:
        del message

    scope: Scope = {"type": "websocket", "headers": [(b"idempotency-key", b"x")]}
    await ChangeIdMiddleware(app)(scope, nothing, drop)

    assert seen == [(scope, nothing, drop)]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
