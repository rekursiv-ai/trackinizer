"""Tests for the agent-session ingest and messaging routes.

Covers the ``POST /api/messages`` idempotency contract (a send that
reaches no live session must not poison the idempotency cache) and the
``POST /api/sessions/{id}/end`` atomicity contract (a failed close must
not drain the session's inbound queue).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import uuid

import pytest

from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import (
    TEST_API_KEY_ID,
    TEST_USER_EMAIL,
)
from trackinizer.server.inbound import Inbound, InboundQueue
from trackinizer.types.inquiries import AgentSession


if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.store.core import Store


def _live_session(
    opened_by_api_key_id: uuid.UUID | None = TEST_API_KEY_ID,
) -> AgentSession:
    """A minimal live ``AgentSession`` (``ended`` is ``None``).

    A real instance, not a renamed stand-in: ``_require_session`` gates with
    ``isinstance``, so the row must be the canonical class. Defaults its
    opening credential to ``TEST_API_KEY_ID`` -- the key the default test
    identity presents -- so a route's owner-scope check passes for a
    same-credential caller; foreign-credential tests pass an explicit other id.
    """
    return AgentSession(
        owner="scientist",
        cli="claude",
        opened_by_api_key_id=opened_by_api_key_id,
    )


class TestSendMessageIdempotency:
    def test_empty_delivery_does_not_poison_idempotency(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # First send: no live session matches, so ``delivered`` is empty.
        # That empty receipt must NOT be recorded against the
        # Idempotency-Key: a retry once the session is live has to still
        # deliver, not replay the empty result.
        client, store, _engine = route_client
        app.state.inbound = InboundQueue()
        key = str(uuid.uuid4())

        monkeypatch.setattr(store, "resolve_live_sessions", AsyncMock(return_value=[]))
        r1 = client.post(
            "/api/messages",
            json={"actor": "scientist", "text": "hello"},
            headers={"Idempotency-Key": key},
        )
        assert r1.status_code == 200, r1.text
        assert r1.json()["delivered"] == []

        # The session is now live: the same key must deliver, not replay [].
        session_id = uuid.uuid4()
        monkeypatch.setattr(
            store,
            "resolve_live_sessions",
            AsyncMock(return_value=[(session_id, ("sear",))]),
        )
        monkeypatch.setattr(
            store, "get_inquiry", AsyncMock(return_value=_live_session())
        )
        r2 = client.post(
            "/api/messages",
            json={"actor": "scientist", "text": "hello", "room": "sear"},
            headers={"Idempotency-Key": key},
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["delivered"] == [str(session_id)]

    def test_nonempty_delivery_is_recorded_for_replay(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A non-empty delivery is recorded so a genuine retry replays the
        # original receipt rather than enqueuing twice.
        client, store, _engine = route_client
        app.state.inbound = InboundQueue()
        key = str(uuid.uuid4())
        session_id = uuid.uuid4()
        monkeypatch.setattr(
            store,
            "resolve_live_sessions",
            AsyncMock(return_value=[(session_id, ("sear",))]),
        )
        monkeypatch.setattr(
            store, "get_inquiry", AsyncMock(return_value=_live_session())
        )

        r1 = client.post(
            "/api/messages",
            json={"actor": "scientist", "text": "hi", "room": "sear"},
            headers={"Idempotency-Key": key},
        )
        assert r1.json()["delivered"] == [str(session_id)]
        # Replay: the recorded receipt comes back; the queue is not
        # enqueued a second time.
        r2 = client.post(
            "/api/messages",
            json={"actor": "scientist", "text": "hi", "room": "sear"},
            headers={"Idempotency-Key": key},
        )
        assert r2.json()["delivered"] == [str(session_id)]
        assert app.state.inbound.pending(session_id) == 1


class TestSessionEndAtomicity:
    def test_failed_end_does_not_drain_inbound(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # If the atomic close fails, the route must not have drained the
        # session's inbound queue -- otherwise a retry loses messages that
        # were never delivered. The drain must run only after a clean end.
        client, store, _engine = route_client
        inbound = InboundQueue()
        app.state.inbound = inbound
        session_id = uuid.uuid4()
        inbound.enqueue(session_id, Inbound(text="pending steering"))

        monkeypatch.setattr(
            store, "get_inquiry", AsyncMock(return_value=_live_session())
        )
        monkeypatch.setattr(
            store,
            "end_session",
            AsyncMock(side_effect=RuntimeError("status write boom")),
        )
        # The close raises; TestClient re-raises server-side exceptions.
        with pytest.raises(RuntimeError, match="status write boom"):
            client.post(
                f"/api/sessions/{session_id}/end",
                json={"actor": "scientist"},
            )
        # The queued message survives the failed close -- the drain runs
        # only after a clean end, so nothing was discarded.
        assert inbound.pending(session_id) == 1

    def test_successful_end_drains_inbound(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, store, _engine = route_client
        inbound = InboundQueue()
        app.state.inbound = inbound
        session_id = uuid.uuid4()
        inbound.enqueue(session_id, Inbound(text="pending steering"))

        monkeypatch.setattr(
            store, "get_inquiry", AsyncMock(return_value=_live_session())
        )
        # ``end_session`` now returns the committed ``ended`` the route echoes.
        monkeypatch.setattr(
            store,
            "end_session",
            AsyncMock(return_value=datetime(2026, 1, 1, tzinfo=UTC)),
        )
        r = client.post(
            f"/api/sessions/{session_id}/end",
            json={"actor": "scientist"},
        )
        assert r.status_code == 200, r.text
        # A clean close releases the now-dead session's queue.
        assert inbound.pending(session_id) == 0


class TestInboundEnqueueRejectsSource:
    def test_client_sent_source_is_422(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The enqueue request model carries no ``source`` -- the sender is
        # attested by the route from the principal. A client that sends one
        # is rejected (422), not silently ignored: the request and response
        # shapes must not share one model with two meanings (API-05/32).
        client, store, _engine = route_client
        app.state.inbound = InboundQueue()
        session_id = uuid.uuid4()
        monkeypatch.setattr(
            store, "get_inquiry", AsyncMock(return_value=_live_session())
        )
        r = client.post(
            f"/api/sessions/{session_id}/inbound",
            json={"text": "check the logs", "source": "forged"},
        )
        assert r.status_code == 422, r.text

    def test_client_sent_room_is_422(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The enqueue route has no room semantics -- it discards body.room. A
        # client-sent ``room`` is rejected (422) rather than accepted then
        # silently dropped (extra="forbid"; the request shape carries only what
        # the server uses).
        client, store, _engine = route_client
        app.state.inbound = InboundQueue()
        session_id = uuid.uuid4()
        monkeypatch.setattr(
            store, "get_inquiry", AsyncMock(return_value=_live_session())
        )
        r = client.post(
            f"/api/sessions/{session_id}/inbound",
            json={"text": "check the logs", "room": "lab"},
        )
        assert r.status_code == 422, r.text

    def test_enqueue_without_source_attests_principal(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, store, _engine = route_client
        inbound = InboundQueue()
        app.state.inbound = inbound
        session_id = uuid.uuid4()
        monkeypatch.setattr(
            store, "get_inquiry", AsyncMock(return_value=_live_session())
        )
        r = client.post(
            f"/api/sessions/{session_id}/inbound",
            json={"text": "check the logs"},
        )
        assert r.status_code == 200, r.text
        # The stored sender is the route principal, never a body value.
        (msg,) = inbound.drain(session_id)
        assert msg.source == "test-user@example.com"

    def test_enqueue_same_idempotency_key_is_deduped(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # /inbound now routes through send_once like /api/messages: a retry
        # reusing the Idempotency-Key is a no-op, not a double-injection.
        client, store, _engine = route_client
        inbound = InboundQueue()
        app.state.inbound = inbound
        session_id = uuid.uuid4()
        monkeypatch.setattr(
            store, "get_inquiry", AsyncMock(return_value=_live_session())
        )
        key = str(uuid.uuid4())
        first = client.post(
            f"/api/sessions/{session_id}/inbound",
            json={"text": "ping"},
            headers={"Idempotency-Key": key},
        )
        assert first.status_code == 200, first.text
        assert first.json()["queued"] == 1
        # Same key -> deduped: still exactly one message queued.
        retry = client.post(
            f"/api/sessions/{session_id}/inbound",
            json={"text": "ping"},
            headers={"Idempotency-Key": key},
        )
        assert retry.status_code == 200, retry.text
        assert retry.json()["queued"] == 1
        assert inbound.pending(session_id) == 1


class TestSessionStartAccountValidation:
    """``/api/sessions/start`` attributes the row to a validated active account.

    Session-start mints an ``AgentSession`` like any other inquiry, so it must
    run the same account-active gate the submit / edit routes do -- otherwise an
    AgentSession row is attributed to an unvalidated account (the routing
    handle).
    """

    def test_start_validates_account_active(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
    ) -> None:
        client, _store, engine = route_client

        # Force the account-active probe to "no active row"; start must 422
        # before minting the session.
        async def fetchval(sql: str, *args: object) -> object:
            del sql, args
            return None

        engine.conn.fetchval.side_effect = fetchval
        r = client.post(
            "/api/sessions/start",
            json={"cli": "claude", "cli_session_id": "abc"},
        )
        assert r.status_code == 422, r.text
        assert "not an active user" in r.json()["detail"]

    def test_start_passes_creator_account_into_submit(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, store, _engine = route_client
        captured: dict[str, object] = {}

        async def start_session(req: object, **_: object) -> tuple[uuid.UUID, str, int]:
            captured["account"] = cast(AgentSession, req).account
            return uuid.uuid4(), "scientist", 0

        monkeypatch.setattr(store, "start_session", start_session)
        monkeypatch.setattr(
            store, "get_inquiry", AsyncMock(return_value=_live_session())
        )
        r = client.post(
            "/api/sessions/start",
            json={"cli": "claude", "cli_session_id": "abc"},
        )
        assert r.status_code == 201, r.text
        # The account threaded into the submit body is the authenticated
        # creator's email, not left for the Store to default to the handle.
        assert captured["account"] == TEST_USER_EMAIL


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
