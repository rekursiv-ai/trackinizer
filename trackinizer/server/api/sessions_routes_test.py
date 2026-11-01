"""Tests for the agent-session ingest and messaging routes.

Covers the ``POST /api/messages`` idempotency contract (a send that
reaches no live session must not poison the idempotency cache) and the
``POST /api/sessions/{id}/end`` atomicity contract (a failed close must
not drain the session's inbound queue).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from functools import partial, partialmethod
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import asyncio
import re
import uuid

from fastapi import FastAPI
from treekle import from_plain, json_loads

import httpx2
import pytest

from trackinizer.conftest import set_field_row
from trackinizer.sagent.session.types import UserMessage
from trackinizer.server.api import sessions_routes
from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import (
    TEST_API_KEY_ID,
    TEST_USER_EMAIL,
    TEST_USER_ID,
    install_identity,
    make_test_identity,
    patch_state,
)
from trackinizer.server.api.session_test_support import (
    ASSISTANT_CONFIG,
    KB_ACTOR,
    USER_AGENT,
    act_as_assistant,
    act_as_other_agent,
    act_as_user_agent,
    browser,
    drain,
    seed_accounts,
    seed_session,
    start_session,
)
from trackinizer.server.asks import open_ask_ids
from trackinizer.server.asks_test_support import open_ask
from trackinizer.server.auth import current_user
from trackinizer.server.config import Config
from trackinizer.server.inbound import Inbound, InboundQueue, PollerRun
from trackinizer.server.session_reaper import (
    STALE_AFTER,
    reap_silent_sessions,
)
from trackinizer.types.inquiries import AgentSession
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.wire_session_ir import ManifestBody, RecordBody
from trackinizer.wire.wire_sessions import (
    RUN_NAME_PATTERN,
    CanvasMessageContext,
)


if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.auth import AuthIdentity, Role
    from trackinizer.server.store.core import Store


# A real instance, not a renamed stand-in: ``_require_session`` gates with
# ``isinstance``, so the row must be the canonical class. Defaults its opening
# credential to ``TEST_API_KEY_ID`` -- the key the default test identity presents -- so
# a route's owner-scope check passes for a same-credential caller; foreign-credential
# tests pass an explicit other id.
def live_session(
    opened_by_api_key_id: uuid.UUID | None = TEST_API_KEY_ID,
) -> AgentSession:
    """Return a minimal live ``AgentSession`` (``ended`` is ``None``)."""
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
        assert from_plain(r1.json(), dict[str, object])["delivered"] == []

        # The session is now live: the same key must deliver, not replay [].
        session_id = uuid.uuid4()
        monkeypatch.setattr(
            store,
            "resolve_live_sessions",
            AsyncMock(return_value=[(session_id, ("sear",))]),
        )
        monkeypatch.setattr(
            store,
            "get_inquiry",
            AsyncMock(return_value=live_session()),
        )
        app.state.inbound.mark_poller(session_id)
        r2 = client.post(
            "/api/messages",
            json={"actor": "scientist", "text": "hello", "room": "sear"},
            headers={"Idempotency-Key": key},
        )
        assert r2.status_code == 200, r2.text
        assert from_plain(r2.json(), dict[str, object])["delivered"] == [
            str(session_id),
        ]

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
            store,
            "get_inquiry",
            AsyncMock(return_value=live_session()),
        )
        app.state.inbound.mark_poller(session_id)

        r1 = client.post(
            "/api/messages",
            json={"actor": "scientist", "text": "hi", "room": "sear"},
            headers={"Idempotency-Key": key},
        )
        assert from_plain(r1.json(), dict[str, object])["delivered"] == [
            str(session_id),
        ]
        # Replay: the recorded receipt comes back; the queue is not
        # enqueued a second time.
        r2 = client.post(
            "/api/messages",
            json={"actor": "scientist", "text": "hi", "room": "sear"},
            headers={"Idempotency-Key": key},
        )
        assert from_plain(r2.json(), dict[str, object])["delivered"] == [
            str(session_id),
        ]
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
            store,
            "get_inquiry",
            AsyncMock(return_value=live_session()),
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
            store,
            "get_inquiry",
            AsyncMock(return_value=live_session()),
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

    @pytest.mark.parametrize("original_id", [None, "vendor-old"])
    @pytest.mark.parametrize("owns_session", [True, False])
    def test_vendor_id_finalization_preserves_shared_writer_access(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
        original_id: str | None,
        owns_session: bool,
    ) -> None:
        client, store, engine = route_client
        engine.conn.fetchval.return_value = True
        session = replace(
            live_session(TEST_API_KEY_ID if owns_session else uuid.uuid4()),
            cli_session_id=original_id,
            audience="open",
        )
        set_field_row(
            engine.conn,
            {
                "kind": "AgentSession",
                "cli_session_id": original_id,
                "opener": session.opened_by_api_key_id,
            },
        )
        monkeypatch.setattr(store, "get_inquiry", AsyncMock(return_value=session))
        end = AsyncMock(return_value=datetime(2026, 1, 1, tzinfo=UTC))
        monkeypatch.setattr(store, "end_session", end)

        response = client.post(
            f"/api/sessions/{session.id}/end",
            json={"cli_session_id": "vendor-final"},
        )

        assert response.status_code == 200, response.text
        call = end.await_args
        assert call is not None
        end.assert_awaited_once_with(
            session.id,
            ended=call.kwargs["ended"],
            cli_session_id="vendor-final",
            api_key_id=TEST_API_KEY_ID,
            actor=TEST_USER_EMAIL,
        )


class TestInboundHoldEndsWithTheSession:
    def test_an_end_during_the_poll_leaves_nothing_parked(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A session ended while its poll awaits the store must not hold that poll.

        The end released only the holds already parked, so a hold this poll took
        afterwards would keep the exiting run waiting out all of it.
        """
        client, store, _engine = route_client
        inbound = InboundQueue()
        app.state.inbound = inbound
        session_id = uuid.uuid4()
        hold = AsyncMock(return_value=[])
        monkeypatch.setattr(InboundQueue, "await_messages", hold)
        monkeypatch.setattr(
            store,
            "get_inquiry",
            AsyncMock(return_value=live_session()),
        )
        monkeypatch.setattr(
            store,
            "record_session_seen",
            partial(_seen_as_the_session_ends, inbound, session_id),
        )
        r = client.get(
            f"/api/sessions/{session_id}/inbound",
            params={"wait_sec": 25},
        )
        assert r.status_code == 200, r.text
        hold.assert_not_awaited()


class TestTheKeyThatDrainsAChatLineHoldsItsTurn:
    def test_the_drain_names_its_key_as_the_lines_taker(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, store, _engine = route_client
        inbound = InboundQueue()
        app.state.inbound = inbound
        session_id = uuid.uuid4()
        context = CanvasMessageContext(
            canvas_id=uuid.uuid4(),
            visible_visuals=[],
            conversation_id=uuid.uuid4(),
        )
        _ = inbound.enqueue(
            session_id,
            Inbound(text="hi", source="ada@example.com", context=context),
        )
        monkeypatch.setattr(
            store,
            "get_inquiry",
            AsyncMock(return_value=live_session()),
        )
        monkeypatch.setattr(store, "record_session_seen", AsyncMock())

        r = client.get(f"/api/sessions/{session_id}/inbound")

        assert r.status_code == 200, r.text
        assert inbound.took_line(TEST_API_KEY_ID, poster="ada@example.com")


class TestTheDrainSaysWhereItsRunIs:
    """A run names its detached host, machine and folder on each drain, for Chat."""

    @pytest.fixture
    def drain_at(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> tuple[TestClient, InboundQueue]:
        client, store, _engine = route_client
        inbound = InboundQueue()
        app.state.inbound = inbound
        monkeypatch.setattr(
            store,
            "get_inquiry",
            AsyncMock(return_value=live_session()),
        )
        monkeypatch.setattr(store, "record_session_seen", AsyncMock())
        return client, inbound

    def test_the_lease_keeps_what_the_drain_said(
        self,
        drain_at: tuple[TestClient, InboundQueue],
    ) -> None:
        client, inbound = drain_at
        detached, foreground, older = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

        said = [
            client.get(f"/api/sessions/{session_id}/inbound", params=params)
            for session_id, params in (
                (
                    detached,
                    {"run": "chat-1a2b3c4d", "machine": "gx10", "cwd": "~/work"},
                ),
                (foreground, {"machine": "gx10", "cwd": "/srv/work"}),
                (older, dict[str, str]()),
            )
        ]

        assert [r.status_code for r in said] == [200, 200, 200], [r.text for r in said]
        assert inbound.poller_run(detached) == PollerRun(
            run="chat-1a2b3c4d",
            machine="gx10",
            cwd="~/work",
        )
        assert inbound.poller_run(foreground) == PollerRun(
            machine="gx10",
            cwd="/srv/work",
        )
        assert inbound.poller_run(older) == PollerRun()

    @pytest.mark.parametrize(
        "params",
        [
            {"run": "-leading-dash"},
            {"run": "has/slash"},
            {"run": "a" * 129},
            {"run": "claude\n"},
            {"machine": "GX10"},
            {"machine": "gx10.local"},
            {"machine": "x" * 64},
            {"machine": "gx10\n"},
            {"cwd": "~/work\n"},
            {"cwd": "~/\x1b[31mred"},
            {"cwd": "~/" + "d" * 1_023},
        ],
    )
    def test_a_malformed_value_is_422_and_takes_no_lease(
        self,
        drain_at: tuple[TestClient, InboundQueue],
        params: dict[str, str],
    ) -> None:
        client, inbound = drain_at
        session_id = uuid.uuid4()

        r = client.get(f"/api/sessions/{session_id}/inbound", params=params)

        assert r.status_code == 422, r.text
        assert not inbound.has_poller(session_id)

    # A detached run names its host by the same rule, so a name it takes never
    # breaks its own drain.
    @pytest.mark.parametrize(
        "name",
        ["claude", "scientist#2", "ada@gx10", "a.b+c=d:e-f_g", "a" * 128, "a" * 129],
    )
    def test_a_run_name_is_taken_exactly_when_detach_takes_it(
        self,
        drain_at: tuple[TestClient, InboundQueue],
        name: str,
    ) -> None:
        client, _inbound = drain_at

        r = client.get(f"/api/sessions/{uuid.uuid4()}/inbound", params={"run": name})

        assert (r.status_code == 200) is (
            re.fullmatch(RUN_NAME_PATTERN, name) is not None
        )


async def _seen_as_the_session_ends(
    inbound: InboundQueue,
    session_id: uuid.UUID,
    *args: object,
    **kwargs: object,
) -> None:
    """Stand in for the liveness write, with the session's end landing during it."""
    del args, kwargs
    inbound.forget_poller(session_id)


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
            store,
            "get_inquiry",
            AsyncMock(return_value=live_session()),
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
            store,
            "get_inquiry",
            AsyncMock(return_value=live_session()),
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
            store,
            "get_inquiry",
            AsyncMock(return_value=live_session()),
        )
        inbound.mark_poller(session_id)
        r = client.post(
            f"/api/sessions/{session_id}/inbound",
            json={"text": "check the logs"},
        )
        assert r.status_code == 200, r.text
        # The stored sender is the route principal, never a body value.
        (msg,) = inbound.drain(session_id)
        assert msg.source == "test-user@example.com"

    @pytest.mark.parametrize("role", ["viewer", "writer", "admin"])
    def test_enqueue_attests_the_principals_role(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
        role: Role,
    ) -> None:
        client, store, _engine = route_client
        inbound = InboundQueue()
        app.state.inbound = inbound
        session_id = uuid.uuid4()
        monkeypatch.setattr(
            store,
            "get_inquiry",
            AsyncMock(return_value=live_session()),
        )
        inbound.mark_poller(session_id)
        install_identity(make_test_identity(role=role))
        r = client.post(
            f"/api/sessions/{session_id}/inbound",
            json={"text": "check the logs"},
        )
        # A viewer may not enqueue; the others are queued with their own role.
        assert r.status_code == (403 if role == "viewer" else 200), r.text
        assert [m.source_role for m in inbound.drain(session_id)] == (
            [] if role == "viewer" else [role]
        )

    @pytest.mark.parametrize("role", ["writer", "admin"])
    def test_a_routed_send_attests_the_principals_role(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
        role: Role,
    ) -> None:
        client, store, _engine = route_client
        inbound = InboundQueue()
        app.state.inbound = inbound
        session_id = uuid.uuid4()
        monkeypatch.setattr(
            store,
            "resolve_live_sessions",
            AsyncMock(return_value=[(session_id, ("sear",))]),
        )
        monkeypatch.setattr(
            store,
            "get_inquiry",
            AsyncMock(return_value=live_session()),
        )
        inbound.mark_poller(session_id)
        install_identity(make_test_identity(role=role))
        r = client.post(
            "/api/messages",
            json={"actor": "scientist", "text": "hello", "room": "sear"},
        )
        assert r.status_code == 200, r.text
        assert [(m.source, m.source_role) for m in inbound.drain(session_id)] == [
            (TEST_USER_EMAIL, role),
        ]

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
            store,
            "get_inquiry",
            AsyncMock(return_value=live_session()),
        )
        inbound.mark_poller(session_id)
        key = str(uuid.uuid4())
        first = client.post(
            f"/api/sessions/{session_id}/inbound",
            json={"text": "ping"},
            headers={"Idempotency-Key": key},
        )
        assert first.status_code == 200, first.text
        assert from_plain(first.json(), dict[str, object])["queued"] == 1
        # Same key -> deduped: still exactly one message queued.
        retry = client.post(
            f"/api/sessions/{session_id}/inbound",
            json={"text": "ping"},
            headers={"Idempotency-Key": key},
        )
        assert retry.status_code == 200, retry.text
        assert from_plain(retry.json(), dict[str, object])["queued"] == 1
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
        detail = from_plain(r.json(), dict[str, object])["detail"]
        assert isinstance(detail, str)
        assert "not an active user" in detail

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
            store,
            "get_inquiry",
            AsyncMock(return_value=live_session()),
        )
        r = client.post(
            "/api/sessions/start",
            json={"cli": "claude", "cli_session_id": "abc"},
        )
        assert r.status_code == 201, r.text
        # The account threaded into the submit body is the authenticated
        # creator's email, not left for the Store to default to the handle.
        assert captured["account"] == TEST_USER_EMAIL

    def test_start_refuses_another_accounts_service_address(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, store, _engine = route_client
        start = AsyncMock(return_value=(uuid.uuid4(), "scientist", 0))
        monkeypatch.setattr(store, "start_session", start)

        r = client.post(
            "/api/sessions/start",
            json={
                "cli": "claude",
                "cli_session_id": "abc",
                "account": "campaign@service.invalid",
            },
        )

        assert r.status_code == 403, r.text
        start.assert_not_called()


class TestViewerOwnedSessionLifecycle:
    def test_viewer_starts_own_session(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, store, _engine = route_client
        install_identity(make_test_identity(role="viewer"))
        session_id = uuid.uuid4()
        start = AsyncMock(return_value=(session_id, "scientist", 0))
        monkeypatch.setattr(store, "start_session", start)
        monkeypatch.setattr(
            store,
            "get_inquiry",
            AsyncMock(return_value=live_session()),
        )

        response = client.post("/api/sessions/start", json={"cli": "codex"})

        assert response.status_code == 201, response.text
        assert from_plain(response.json(), dict[str, object])["id"] == str(session_id)
        call = start.await_args
        assert call is not None
        assert call.kwargs["api_key_id"] == TEST_API_KEY_ID

    def test_viewer_cannot_start_for_another_account(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, store, _engine = route_client
        install_identity(make_test_identity(role="viewer"))
        start = AsyncMock()
        monkeypatch.setattr(store, "start_session", start)

        response = client.post(
            "/api/sessions/start",
            json={"cli": "codex", "account": "other@example.com"},
        )

        assert response.status_code == 403, response.text
        start.assert_not_awaited()

    def test_viewer_cannot_receive_foreign_start_replay(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, store, _engine = route_client
        install_identity(make_test_identity(role="viewer"))
        monkeypatch.setattr(
            store,
            "start_session",
            AsyncMock(return_value=(uuid.uuid4(), "scientist", 0)),
        )
        monkeypatch.setattr(
            store,
            "get_inquiry",
            AsyncMock(return_value=live_session(uuid.uuid4())),
        )

        response = client.post("/api/sessions/start", json={"cli": "codex"})

        assert response.status_code == 403, response.text

    @pytest.mark.parametrize("owns_session", [True, False])
    def test_viewer_ends_only_own_session(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
        owns_session: bool,
    ) -> None:
        client, store, _engine = route_client
        install_identity(make_test_identity(role="viewer"))
        monkeypatch.setattr(
            store,
            "get_inquiry",
            AsyncMock(
                return_value=live_session(
                    TEST_API_KEY_ID if owns_session else uuid.uuid4(),
                ),
            ),
        )
        end = AsyncMock(return_value=datetime(2026, 1, 1, tzinfo=UTC))
        monkeypatch.setattr(store, "end_session", end)

        response = client.post(
            f"/api/sessions/{uuid.uuid4()}/end",
            json={"actor": "scientist"},
        )

        assert response.status_code == (200 if owns_session else 403), response.text
        assert end.await_count == int(owns_session)


_SLASH_ONLY = {
    "slash_commands": [{"timestamp": "2026-10-03T12:30:00Z", "command": "exit"}],
}


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_dead_run_is_closed_and_a_returning_one_reopened(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The reaper closes a silent session; a poll or an upload brings it back."""
    client, store = pglite_route_client
    inbound = InboundQueue()
    app.state.inbound = inbound
    await _active_user(store)
    session_id = await _start(client)

    polled = await client.get(f"/api/sessions/{session_id}/inbound")
    assert polled.status_code == 200, polled.text
    last_seen = await _last_seen(store, session_id=session_id)
    assert await reap(store, inbound=inbound, after=REAP_WINDOW) == 1
    closed = await session_row(store, session_id=session_id)
    assert (closed.status, closed.ended) == ("complete", last_seen)

    # The run was only cut off: its next poll gets the session back.
    polled = await client.get(f"/api/sessions/{session_id}/inbound")
    assert polled.status_code == 200, polled.text
    reopened = await session_row(store, session_id=session_id)
    assert (reopened.status, reopened.ended) == ("active", None)
    assert inbound.has_poller(session_id)

    # Closed again, it comes back through an upload too -- and the upload, which
    # an ended session would refuse, lands.
    assert await reap(store, inbound=inbound, after=REAP_WINDOW) == 1
    uploaded = await client.post(
        f"/api/sessions/{session_id}/records",
        json=_SLASH_ONLY,
    )
    assert uploaded.status_code == 200, uploaded.text
    assert json_loads(uploaded.content, dict[str, object])["slash_commands"] == 1
    assert (await session_row(store, session_id=session_id)).status == "active"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_upload_keeps_a_polling_session_open(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A bridge polls only when its agent is ready; its uploads say it is alive."""
    client, store = pglite_route_client
    inbound = InboundQueue()
    app.state.inbound = inbound
    await _active_user(store)
    session_id = await _start(client)

    _ = await client.get(f"/api/sessions/{session_id}/inbound")
    await _poll_was_ago(store, session_id=session_id, ago=timedelta(minutes=10))
    uploaded = await client.post(
        f"/api/sessions/{session_id}/records",
        json=_SLASH_ONLY,
    )
    assert uploaded.status_code == 200, uploaded.text
    # Silent for the whole window as of the poll, but not as of the upload.
    after = STALE_AFTER - timedelta(minutes=5)
    assert await reap(store, inbound=inbound, after=after) == 0
    assert (await session_row(store, session_id=session_id)).status == "active"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_session_that_never_polls_is_never_closed(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A capture-only producer has no poller whose silence could mean anything."""
    client, store = pglite_route_client
    inbound = InboundQueue()
    app.state.inbound = inbound
    await _active_user(store)
    session_id = await _start(client)

    uploaded = await client.post(
        f"/api/sessions/{session_id}/records",
        json=_SLASH_ONLY,
    )
    assert uploaded.status_code == 200, uploaded.text
    assert await reap(store, inbound=inbound, after=timedelta(days=1)) == 0
    assert (await session_row(store, session_id=session_id)).status == "active"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_cleanly_ended_session_stays_ended(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Only the reaper's own closes are undone by a return."""
    client, store = pglite_route_client
    inbound = InboundQueue()
    app.state.inbound = inbound
    await _active_user(store)
    session_id = await _start(client)

    _ = await client.get(f"/api/sessions/{session_id}/inbound")
    ended = await client.post(
        f"/api/sessions/{session_id}/end",
        json={"actor": "scientist"},
    )
    assert ended.status_code == 200, ended.text
    assert await reap(store, inbound=inbound, after=timedelta(days=1)) == 0
    _ = await client.get(f"/api/sessions/{session_id}/inbound")
    assert (await session_row(store, session_id=session_id)).status == "complete"


REAP_WINDOW = STALE_AFTER + timedelta(minutes=1)
"""Long enough from now that anything seen so far counts as silent."""


async def reap(store: Store, *, inbound: InboundQueue, after: timedelta) -> int:
    """Run one reaper pass as of ``after`` from now."""
    now = datetime.now(UTC) + after
    return await reap_silent_sessions(store, inbound=inbound, now=now)


async def _last_seen(store: Store, *, session_id: uuid.UUID) -> datetime:
    """Return when ``session_id`` was last recorded as heard from."""
    async with store.engine.acquire() as conn:
        seen = await conn.fetchval(
            "SELECT last_seen FROM session_liveness WHERE session_id = $1",
            session_id,
        )
    assert isinstance(seen, datetime)
    return seen


async def _poll_was_ago(
    store: Store,
    *,
    session_id: uuid.UUID,
    ago: timedelta,
) -> None:
    """Move the session's last sighting ``ago`` into the past."""
    seen = await _last_seen(store, session_id=session_id)
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE session_liveness SET last_seen = $2 WHERE session_id = $1",
            session_id,
            seen - ago,
        )


async def _active_user(store: Store) -> None:
    """Create the test principal as an active user, which a session start requires."""
    install_identity(make_test_identity(api_key_id=None))
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, 'test-user@example.com', 'Test', 'writer', 'active') "
            "ON CONFLICT DO NOTHING",
            TEST_USER_ID,
        )


async def _start(client: httpx2.AsyncClient) -> uuid.UUID:
    """Open a session through the route and return its id."""
    started = await client.post(
        "/api/sessions/start",
        json={"cli": "claude", "actor": "scientist"},
    )
    assert started.status_code == 201, started.text
    return uuid.UUID(
        from_plain(
            json_loads(started.content, dict[str, object])["id"],
            str,
        ),
    )


async def session_row(store: Store, *, session_id: uuid.UUID) -> AgentSession:
    """Return the session's row."""
    row = await store.get_inquiry(session_id)
    assert isinstance(row, AgentSession)
    return row


@pytest.fixture
def assistant_served(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve with scout configured, and a fresh poller table."""
    patch_state(monkeypatch, "config", ASSISTANT_CONFIG)
    patch_state(monkeypatch, "inbound", InboundQueue())


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_without_an_assistant_a_writer_still_drains_any_session(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal is the assistant's alone: on one organisation, the writer reads it."""
    client, store = pglite_route_client
    patch_state(monkeypatch, "config", Config(chat_orgs="single"))
    patch_state(monkeypatch, "inbound", InboundQueue())
    await seed_accounts(store)
    act_as_assistant()
    kb = await start_session(client, actor=KB_ACTOR)
    act_as_other_agent("writer")
    assert (await drain(client, session_id=kb)).status_code == 200


def refusal_code(response: httpx2.Response) -> str:
    """Return the ``code`` of a refusal's detail."""
    detail = from_plain(
        json_loads(response.content, dict[str, object])["detail"],
        dict[str, object],
    )
    return from_plain(detail["code"], str)


def refusal_message(response: httpx2.Response) -> str:
    """Return the ``message`` of a refusal's detail."""
    detail = from_plain(
        json_loads(response.content, dict[str, object])["detail"],
        dict[str, object],
    )
    return from_plain(detail["message"], str)


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_run_that_names_its_cli_ends_a_session_that_is_no_chat(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Every run's end names its CLI; only a chat has a provider it can differ from."""
    client, store = pglite_route_client
    await seed_accounts(store)
    session_id = await seed_session(store, USER_AGENT, actor="run-1", cli="codex")
    act_as_user_agent("writer")

    ended = await client.post(f"/api/sessions/{session_id}/end", json={"cli": "claude"})

    assert ended.status_code == 200, ended.text
    assert (await session_row(store, session_id=session_id)).ended is not None


@pytest.mark.parametrize("instance", ["", "A" * 32, "a" * 31, "g" * 32, "a" * 33])
def test_a_run_instance_is_32_lowercase_hex_digits(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
    instance: str,
) -> None:
    """The id a run names itself by is bounded, as it is kept in memory per lease."""
    client, store, _engine = route_client
    app.state.inbound = InboundQueue()
    monkeypatch.setattr(store, "get_inquiry", AsyncMock(return_value=live_session()))
    monkeypatch.setattr(store, "record_session_seen", AsyncMock())

    r = client.get(
        f"/api/sessions/{uuid.uuid4()}/inbound",
        params={"cli": "claude", "instance": instance},
    )

    assert r.status_code == 422, r.text


AWAIT_MESSAGES = InboundQueue.await_messages
"""The wait itself, as it is before a test wraps it."""


@dataclass(slots=True, kw_only=True)
class Holds:
    """The requests that parked on the inbound queue to wait for a message."""

    parked: asyncio.Event = field(default_factory=asyncio.Event)
    """Set once a request has begun waiting."""

    asked_sec: list[float] = field(default_factory=list)
    """How long each request asked to wait."""


async def parked_wait(
    queue: InboundQueue,
    holds: Holds,
    session_id: uuid.UUID,
    *,
    timeout_sec: float,
) -> list[Inbound]:
    """Wait on ``queue`` as it does, noting that the wait began."""
    holds.asked_sec.append(timeout_sec)
    holds.parked.set()
    return await AWAIT_MESSAGES(queue, session_id, timeout_sec=timeout_sec)


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_held_drain_returns_as_soon_as_a_line_arrives(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run long-polls its session, and a line sent to it ends the wait at once."""
    client, store = pglite_route_client
    await seed_accounts(store)
    session_id = await seed_session(store, USER_AGENT, actor="run-1")
    holds = Holds()
    monkeypatch.setattr(
        InboundQueue,
        "await_messages",
        partialmethod(parked_wait, holds),
    )

    async def held() -> httpx2.Response:
        act_as_user_agent()
        return await client.get(
            f"/api/sessions/{session_id}/inbound",
            params={"wait_sec": 10},
        )

    waiting = asyncio.ensure_future(held())
    await holds.parked.wait()
    assert not waiting.done()
    assert holds.asked_sec == [10.0]
    browser()
    sent = await client.post(
        f"/api/sessions/{session_id}/inbound",
        json={"text": "wake up"},
    )
    response = await asyncio.wait_for(waiting, 5)

    assert sent.status_code == 200, sent.text
    assert response.status_code == 200
    messages = from_plain(
        json_loads(response.content, dict[str, object])["messages"],
        list[dict[str, object]],
    )
    assert [m["text"] for m in messages] == ["wake up"]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_private_start_opens_a_hidden_session_and_says_so(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A ``--private`` run reads the audience back to know it was hidden."""
    client, store = pglite_route_client
    await _active_user(store)

    answers = [
        await client.post(
            "/api/sessions/start",
            json={"cli": "claude", **({"private": True} if private else {})},
        )
        for private in (True, False)
    ]

    bodies = [json_loads(each.content, dict[str, object]) for each in answers]
    assert [each.status_code for each in answers] == [201, 201]
    assert [body["audience"] for body in bodies] == ["private", "open"]
    hidden = await session_row(store, session_id=uuid.UUID(str(bodies[0]["id"])))
    assert hidden.audience == "private"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_private_resume_hides_an_open_run_and_leaves_a_staff_one_ended(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The key that opened a run is its owner, who hides it; a staff run no one hides.

    The staff run is refused before it is re-opened: its end stays as it was.
    """
    client, store = pglite_route_client
    await _active_user(store)
    runs: dict[str, uuid.UUID] = {}
    for audience in ("open", "staff"):
        started = await client.post(
            "/api/sessions/start",
            json={"cli": "claude", "cli_session_id": f"run-{audience}"},
        )
        runs[audience] = uuid.UUID(
            from_plain(json_loads(started.content, dict[str, object])["id"], str),
        )
        async with store.engine.acquire() as conn:
            await conn.execute(
                "UPDATE inquiries SET agentsession_audience = $2, status = 'complete', "
                "agentsession_ended = clock_timestamp() WHERE id = $1",
                runs[audience],
                audience,
            )

    answers = {
        audience: await client.post(
            "/api/sessions/start",
            json={
                "cli": "claude",
                "cli_session_id": f"run-{audience}",
                "private": True,
            },
        )
        for audience in runs
    }

    hidden = await session_row(store, session_id=runs["open"])
    staff = await session_row(store, session_id=runs["staff"])
    assert answers["open"].status_code == 201, answers["open"].text
    assert (
        json_loads(answers["open"].content, dict[str, object])["audience"] == "private"
    )
    assert (hidden.audience, hidden.ended) == ("private", None)
    assert answers["staff"].status_code == 409
    assert staff.audience == "staff"
    assert staff.ended is not None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_session_starts_on_an_app_built_by_hand_with_no_hub_or_config(
    store: Store,
) -> None:
    """A harness that wires only engine, store and inbound, as the integration test does."""
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, $2, 'T', 'writer', 'active')",
            TEST_USER_ID,
            TEST_USER_EMAIL,
        )
        await conn.execute(
            "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
            "VALUES ($1, $2, 'k', 'h', 'trax_hand', 'writer')",
            TEST_API_KEY_ID,
            TEST_USER_ID,
        )
    bespoke = FastAPI()
    bespoke.include_router(sessions_routes.router)
    bespoke.state.engine = store.engine
    bespoke.state.store = store
    bespoke.state.inbound = InboundQueue()

    async def identity() -> AuthIdentity:
        return make_test_identity(role="viewer")

    bespoke.dependency_overrides[current_user] = identity
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=bespoke, raise_app_exceptions=False),
        base_url="http://hand",
    ) as client:
        started = await client.post("/api/sessions/start", json={"cli": "codex"})
        assert started.status_code == 201
        session_id = json_loads(started.content, dict[str, object])["id"]
        polled = await client.get(f"/api/sessions/{session_id}/inbound")
        assert polled.status_code == 200
        ended = await client.post(
            f"/api/sessions/{session_id}/end",
            json={"status": "completed"},
        )
        assert ended.status_code == 200


class TestInboundKinds:
    def test_a_kind_and_its_payload_reach_the_drain(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An answer is drained as one, and a bare line as a ``line`` with no payload."""
        client, store, _engine = route_client
        inbound = InboundQueue()
        app.state.inbound = inbound
        session_id = uuid.uuid4()
        monkeypatch.setattr(
            store,
            "get_inquiry",
            AsyncMock(return_value=live_session()),
        )
        monkeypatch.setattr(store, "record_session_seen", AsyncMock())
        inbound.mark_poller(session_id)
        answer: dict[str, object] = {
            "prompt_id": "p1",
            "decision": "deny",
            "updated_input": None,
            "message": "not now",
        }

        sent = client.post(
            f"/api/sessions/{session_id}/inbound",
            json={"text": "No: not now", "kind": "answer", "payload": answer},
        )
        plain = client.post(f"/api/sessions/{session_id}/inbound", json={"text": "hi"})
        drained = client.get(f"/api/sessions/{session_id}/inbound")

        assert (sent.status_code, plain.status_code) == (200, 200), sent.text
        messages = from_plain(
            from_plain(drained.json(), dict[str, object])["messages"],
            list[dict[str, object]],
        )
        assert [(m["text"], m["kind"], m["payload"]) for m in messages] == [
            ("No: not now", "answer", answer),
            ("hi", "line", None),
        ]

    def test_a_kind_the_server_does_not_know_is_422(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, store, _engine = route_client
        inbound = InboundQueue()
        app.state.inbound = inbound
        session_id = uuid.uuid4()
        monkeypatch.setattr(
            store,
            "get_inquiry",
            AsyncMock(return_value=live_session()),
        )
        inbound.mark_poller(session_id)

        refused = client.post(
            f"/api/sessions/{session_id}/inbound",
            json={"text": "x", "kind": "keys"},
        )

        assert refused.status_code == 422, refused.text
        assert inbound.pending(session_id) == 0


def test_a_session_the_old_chat_helper_opened_takes_messages_like_any_other(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session an old helper opened is refused neither a send nor another writer's drain.

    The helper is gone, and with it the refusals keyed on its CLI name.
    """
    client, store, engine = route_client
    inbound = InboundQueue()
    app.state.inbound = inbound
    # The database answers the transcript test: the sender reads the session.
    engine.conn.fetchval.return_value = True
    session_id = uuid.uuid4()
    monkeypatch.setattr(
        store,
        "get_inquiry",
        AsyncMock(
            return_value=replace(
                live_session(uuid.uuid4()),
                cli="trax-helper",
                audience="open",
            ),
        ),
    )
    monkeypatch.setattr(
        store,
        "resolve_live_sessions",
        AsyncMock(return_value=[(session_id, ())]),
    )
    monkeypatch.setattr(store, "record_session_seen", AsyncMock())
    inbound.mark_poller(session_id)

    direct = client.post(f"/api/sessions/{session_id}/inbound", json={"text": "a"})
    routed = client.post("/api/messages", json={"actor": "helper", "text": "b"})
    drained = client.get(f"/api/sessions/{session_id}/inbound")

    assert [r.status_code for r in (direct, routed, drained)] == [200] * 3, direct.text
    messages = from_plain(
        from_plain(drained.json(), dict[str, object])["messages"],
        list[dict[str, object]],
    )
    assert [m["text"] for m in messages] == ["a", "b"]


def opens_a_part(name: str) -> dict[str, object]:
    """Return the body that opens the part ``name`` with one line."""
    record = RecordBody.of(
        SessionRecordRow.of(
            session_id=uuid.UUID(int=0),
            part=0,
            idx=0,
            record=UserMessage(content="hi"),
        ),
    )
    manifest = ManifestBody(name=name, ir_id=uuid.uuid4(), format="claude", records=1)
    return {
        "name": name,
        "manifest": manifest.model_dump(mode="json"),
        "records": [record.model_dump(mode="json")],
    }


async def attach_run(
    client: httpx2.AsyncClient,
    *,
    session_id: uuid.UUID,
) -> httpx2.Response:
    """Start a claude run that re-opens ``session_id``."""
    return await client.post(
        "/api/sessions/start",
        json={"cli": "claude", "attach": str(session_id)},
    )


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_run_attaches_to_an_ended_session_and_adds_the_next_part(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The session keeps its id and is live again; an unknown id is 404."""
    client, store = pglite_route_client
    await seed_accounts(store)
    session_id = await seed_session(
        store,
        USER_AGENT,
        actor="run-1",
        cli_session_id="vendor-1",
    )
    act_as_user_agent("writer")
    written = await client.post(
        f"/api/sessions/{session_id}/records",
        json=opens_a_part("codex.jsonl"),
    )
    ended = await client.post(f"/api/sessions/{session_id}/end", json={})

    attached = await attach_run(client, session_id=session_id)
    appended = await client.post(
        f"/api/sessions/{session_id}/records",
        json=opens_a_part("claude.jsonl"),
    )
    unknown = await attach_run(client, session_id=uuid.uuid4())

    assert (written.status_code, ended.status_code) == (200, 200)
    assert attached.status_code == 201, attached.text
    body = json_loads(attached.content, dict[str, object])
    assert (body["id"], body["cli_session_id"]) == (str(session_id), "vendor-1")
    row = await session_row(store, session_id=session_id)
    assert (row.ended, row.cli_session_id) == (None, "vendor-1")
    assert appended.status_code == 200, appended.text
    assert json_loads(appended.content, dict[str, object])["part"] == 1
    assert unknown.status_code == 404, unknown.text


async def _unpolled_run(store: Store) -> uuid.UUID:
    """Seed the test user's live session that no run has drained yet."""
    return await seed_session(
        store,
        USER_AGENT,
        actor="run-1",
        cli="claude",
        polled=False,
    )


async def report_state(
    client: httpx2.AsyncClient,
    *,
    session_id: uuid.UUID,
    state: str,
    instance: str,
) -> httpx2.Response:
    """Report a run's state as the installed identity."""
    return await client.post(
        f"/api/sessions/{session_id}/run-state",
        json={"state": state, "instance": instance},
    )


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_run_reports_its_state_and_its_next_drains_keep_it(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The state is the run's word; a drain renews the lease and leaves it be."""
    client, store = pglite_route_client
    await seed_accounts(store)
    session_id = await _unpolled_run(store)
    act_as_user_agent("writer")
    run = "a" * 32
    inbound = cast("InboundQueue", app.state.inbound)
    assert (
        await drain(client, session_id=session_id, cli="claude", instance=run)
    ).status_code == 200

    reported = await report_state(
        client,
        session_id=session_id,
        state="requires_action",
        instance=run,
    )
    assert (
        await drain(client, session_id=session_id, cli="claude", instance=run)
    ).status_code == 200

    assert reported.status_code == 204, reported.text
    held = inbound.poller_run(session_id)
    assert held is not None
    assert held.state == "requires_action"


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_report_from_another_run_or_one_with_no_lease_changes_nothing(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Both are answered 204: the run is told nothing it could act on."""
    client, store = pglite_route_client
    await seed_accounts(store)
    session_id = await _unpolled_run(store)
    act_as_user_agent("writer")
    inbound = cast("InboundQueue", app.state.inbound)
    first, second = "a" * 32, "b" * 32

    unleased = await report_state(
        client,
        session_id=session_id,
        state="running",
        instance=first,
    )
    assert (
        await drain(client, session_id=session_id, cli="claude", instance=first)
    ).status_code == 200
    other = await report_state(
        client,
        session_id=session_id,
        state="taken_over",
        instance=second,
    )

    assert (unleased.status_code, other.status_code) == (204, 204)
    held = inbound.poller_run(session_id)
    assert held is not None
    assert held.state == "idle"


@pytest.mark.parametrize(
    "body",
    [
        {"state": "napping", "instance": "a" * 32},
        {"state": "running", "instance": "A" * 32},
        {"state": "running"},
        {"state": "running", "instance": "a" * 32, "extra": 1},
    ],
)
def test_a_run_state_report_must_be_well_formed(
    route_client: tuple[TestClient, Store, FakeEngine],
    body: dict[str, object],
) -> None:
    client, _store, _engine = route_client

    refused = client.post(f"/api/sessions/{uuid.uuid4()}/run-state", json=body)

    assert refused.status_code == 422, refused.text


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_stopped_run_is_refused_with_a_403_and_a_new_run_is_not(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A 403 ends every run ever shipped, where a 409 code it does not know is retried."""
    client, store = pglite_route_client
    await seed_accounts(store)
    session_id = await _unpolled_run(store)
    act_as_user_agent("writer")
    old, new = "a" * 32, "b" * 32
    inbound = cast("InboundQueue", app.state.inbound)
    first = await drain(
        client,
        session_id=session_id,
        cli="claude",
        instance=old,
        machine="box",
    )
    assert first.status_code == 200
    assert list(inbound.runs_on("box", account=TEST_USER_EMAIL)) == [session_id]

    inbound.stop_run(session_id)
    turned_away = await drain(client, session_id=session_id, cli="claude", instance=old)
    taken = await drain(client, session_id=session_id, cli="claude", instance=new)

    assert (turned_away.status_code, refusal_code(turned_away)) == (403, "run_stopped")
    assert taken.status_code == 200


async def open_asks(store: Store, session_id: uuid.UUID) -> list[str]:
    """Return the asks open in a session."""
    async with store.engine.acquire() as conn:
        return await open_ask_ids(conn, session_id=session_id)


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_the_end_of_a_run_releases_the_asks_it_left_open(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A card with no run behind it is ended before the session closes to records."""
    client, store = pglite_route_client
    await seed_accounts(store)
    session_id = await _unpolled_run(store)
    act_as_user_agent("writer")
    assert (await drain(client, session_id=session_id, cli="claude")).status_code == 200
    await open_ask(store, session_id, ask_id="a1")

    ended = await client.post(f"/api/sessions/{session_id}/end", json={"cli": "claude"})

    assert ended.status_code == 200, ended.text
    assert (await session_row(store, session_id=session_id)).ended is not None
    assert await open_asks(store, session_id) == []


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_the_exit_of_a_run_a_reattach_replaced_leaves_the_new_run_alone(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The replaced run may quit before it learns so; its end must not end the session."""
    client, store = pglite_route_client
    await seed_accounts(store)
    session_id = await _unpolled_run(store)
    act_as_user_agent("writer")
    old, new = "a" * 32, "b" * 32
    inbound = cast("InboundQueue", app.state.inbound)
    assert (
        await drain(client, session_id=session_id, cli="claude", instance=old)
    ).status_code == 200
    inbound.retire_run(session_id)
    assert (
        await drain(client, session_id=session_id, cli="claude", instance=new)
    ).status_code == 200
    await open_ask(store, session_id, ask_id="a1")

    left = await client.post(
        f"/api/sessions/{session_id}/end",
        json={"cli": "claude", "instance": old},
    )

    assert left.status_code == 200, left.text
    assert (await session_row(store, session_id=session_id)).ended is None
    held = inbound.poller_run(session_id)
    assert held is not None
    assert held.instance == new
    assert await open_asks(store, session_id) == ["a1"]


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_the_exit_of_the_run_that_holds_the_lease_ends_its_session(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A run that names its own instance still ends its session."""
    client, store = pglite_route_client
    await seed_accounts(store)
    session_id = await _unpolled_run(store)
    act_as_user_agent("writer")
    own = "a" * 32
    assert (
        await drain(client, session_id=session_id, cli="claude", instance=own)
    ).status_code == 200
    await open_ask(store, session_id, ask_id="a1")

    ended = await client.post(
        f"/api/sessions/{session_id}/end",
        json={"cli": "claude", "instance": own},
    )

    assert ended.status_code == 200, ended.text
    assert (await session_row(store, session_id=session_id)).ended is not None
    assert await open_asks(store, session_id) == []


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
