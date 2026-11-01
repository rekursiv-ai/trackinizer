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

from fastapi import FastAPI, HTTPException, Request
from treekle import from_plain, json_loads

import httpx2
import pytest

from trackinizer.conftest import set_field_row
from trackinizer.sagent.session.types import UserMessage
from trackinizer.server.api import sessions_routes
from trackinizer.server.api.app import app
from trackinizer.server.api.canvas_test_support import (
    ASSISTANT,
    ASSISTANT_CONFIG,
    KB_ACTOR,
    KB_EMAIL,
    OTHER_EMAIL,
    OTHER_USER_ID,
    USER_AGENT,
    act_as_assistant,
    act_as_other_agent,
    act_as_user_agent,
    browser,
    drain,
    open_canvas,
    receipt_conversation,
    seed_accounts,
    seed_science_chat,
    seed_session,
    send_chat,
    start_session,
)
from trackinizer.server.api.conftest import (
    TEST_API_KEY_ID,
    TEST_USER_EMAIL,
    TEST_USER_ID,
    install_identity,
    make_test_identity,
    patch_state,
)
from trackinizer.server.api.science_chat_routes_test import (
    seed_helper_chat,
    start_unpolled,
)
from trackinizer.server.asks import open_ask_ids
from trackinizer.server.asks_test_support import open_ask
from trackinizer.server.auth import current_user
from trackinizer.server.chat_hub import ChatHub
from trackinizer.server.config import Config
from trackinizer.server.inbound import Inbound, InboundQueue, PollerRun
from trackinizer.server.session_reaper import (
    STALE_AFTER,
    reap_silent_sessions,
)
from trackinizer.types.inquiries import AgentSession
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.wire_science_chat import (
    ChatPartial,
    ChatPartialState,
    chat_session_id,
)
from trackinizer.wire.wire_session_ir import ManifestBody, RecordBody
from trackinizer.wire.wire_sessions import (
    RUN_ENDING_REFUSALS,
    RUN_NAME_PATTERN,
    CanvasMessageContext,
)


if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.auth import AuthIdentity, Role
    from trackinizer.server.store.core import Store
    from trackinizer.wire.wire_science_chat import ChatProvider


# A real instance, not a renamed stand-in: ``_require_session`` gates with
# ``isinstance``, so the row must be the canonical class. Defaults its opening
# credential to ``TEST_API_KEY_ID`` -- the key the default test identity presents -- so
# a route's owner-scope check passes for a same-credential caller; foreign-credential
# tests pass an explicit other id.
def _live_session(
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
            AsyncMock(return_value=_live_session()),
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
            AsyncMock(return_value=_live_session()),
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


class TestSendToAScienceChat:
    """A chat is reached through its canvas: a browser's Console send is refused."""

    @classmethod
    def _chat_at(
        cls,
        store: Store,
        monkeypatch: pytest.MonkeyPatch,
        *,
        engine: FakeEngine,
        polled: bool,
    ) -> tuple[uuid.UUID, InboundQueue]:
        """Make ``scientist`` resolve to a live science chat the sender is a member of."""
        inbound = InboundQueue()
        app.state.inbound = inbound
        # The database answers the chat's membership test: the sender may read it.
        engine.conn.fetchval.return_value = True
        session_id = uuid.uuid4()
        monkeypatch.setattr(
            store,
            "resolve_live_sessions",
            AsyncMock(return_value=[(session_id, ())]),
        )
        monkeypatch.setattr(
            store,
            "get_inquiry",
            AsyncMock(
                return_value=AgentSession(
                    owner="scientist",
                    cli="sagent",
                    cli_session_id=chat_session_id(uuid.uuid4()),
                    opened_by_api_key_id=TEST_API_KEY_ID,
                    audience="open",
                ),
            ),
        )
        if polled:
            inbound.mark_poller(session_id)
        return session_id, inbound

    def test_a_browser_is_told_to_open_the_chats_canvas_polled_or_not(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, store, engine = route_client
        browser()
        for polled in (True, False):
            session_id, inbound = self._chat_at(
                store,
                monkeypatch,
                engine=engine,
                polled=polled,
            )

            refused = client.post(
                "/api/messages",
                json={"actor": "scientist", "text": "stop"},
            )

            assert refused.status_code == 409, refused.text
            assert from_plain(refused.json(), dict[str, object]) == {
                "detail": "Open its canvas to talk to this chat",
            }
            assert inbound.pending(session_id) == 0

    def test_an_agent_key_keeps_todays_rules(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, store, engine = route_client
        act_as_other_agent("writer")
        _ = self._chat_at(store, monkeypatch, engine=engine, polled=False)
        skipped = client.post("/api/messages", json={"actor": "scientist", "text": "x"})
        session_id, inbound = self._chat_at(
            store,
            monkeypatch,
            engine=engine,
            polled=True,
        )
        refused = client.post("/api/messages", json={"actor": "scientist", "text": "x"})

        assert (skipped.status_code, skipped.json()) == (200, {"delivered": []})
        assert refused.status_code == 409
        assert "POST /api/chats" in from_plain(refused.json(), dict[str, str])["detail"]
        assert inbound.pending(session_id) == 0


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
            AsyncMock(return_value=_live_session()),
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
            AsyncMock(return_value=_live_session()),
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
            _live_session(TEST_API_KEY_ID if owns_session else uuid.uuid4()),
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

    @pytest.mark.parametrize("final_id", ["omitted", "null", "same"])
    def test_chat_close_preserves_its_existing_id(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
        final_id: str,
    ) -> None:
        client, store, engine = route_client
        named = chat_session_id(uuid.uuid4())
        session = replace(_live_session(), cli_session_id=named)
        set_field_row(
            engine.conn,
            {
                "kind": "AgentSession",
                "cli_session_id": named,
                "opener": session.opened_by_api_key_id,
            },
        )
        monkeypatch.setattr(store, "get_inquiry", AsyncMock(return_value=session))
        end = AsyncMock(return_value=datetime(2026, 1, 1, tzinfo=UTC))
        monkeypatch.setattr(store, "end_session", end)
        body: dict[str, object] = {}
        if final_id != "omitted":
            body["cli_session_id"] = named if final_id == "same" else None

        response = client.post(f"/api/sessions/{session.id}/end", json=body)

        assert response.status_code == 200, response.text
        call = end.await_args
        assert call is not None
        assert call.kwargs["cli_session_id"] == (named if final_id == "same" else None)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_historical_session_cannot_join_a_chat_when_it_ends(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Late vendor-id finalization cannot backdate a chat's starter or drain a queue."""
    client, store = pglite_route_client
    await seed_accounts(store)
    conversation = uuid.uuid4()
    _ = await seed_helper_chat(store, conversation_id=conversation)
    act_as_user_agent("writer")
    created = await client.post(
        "/api/inquiries/agentsession",
        json={
            "title": "historical import",
            "created": "2020-01-01T00:00:00Z",
            "cli_session_id": "vendor-history",
        },
    )
    assert created.status_code == 201, created.text
    session_id = uuid.UUID(
        from_plain(json_loads(created.content, dict[str, object])["id"], str),
    )
    inbound = InboundQueue()
    app.state.inbound = inbound
    message = Inbound(text="undelivered steering")
    inbound.mark_poller(session_id)
    _ = inbound.enqueue(session_id, message)
    async with store.engine.acquire() as conn:
        before_rows = await conn.fetch("SELECT * FROM inquiries ORDER BY id")
        before_changes = await conn.fetch("SELECT * FROM change_log ORDER BY id")

    refused = await client.post(
        f"/api/sessions/{session_id}/end",
        json={"cli_session_id": chat_session_id(conversation)},
    )

    assert refused.status_code == 403, refused.text
    async with store.engine.acquire() as conn:
        assert await conn.fetch("SELECT * FROM inquiries ORDER BY id") == before_rows
        assert (
            await conn.fetch("SELECT * FROM change_log ORDER BY id") == before_changes
        )
    assert inbound.has_poller(session_id)
    assert inbound.drain(session_id) == [message]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("include_id", [False, True])
async def test_a_chat_close_with_an_unchanged_id_replays_its_original_receipt(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    include_id: bool,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    conversation = uuid.uuid4()
    session_id = await seed_helper_chat(store, conversation_id=conversation)
    act_as_user_agent()
    body = {"cli_session_id": chat_session_id(conversation)} if include_id else {}
    headers = {"Idempotency-Key": str(uuid.uuid4())}

    first = await client.post(
        f"/api/sessions/{session_id}/end",
        json=body,
        headers=headers,
    )
    assert first.status_code == 200, first.text
    async with store.engine.acquire() as conn:
        after_first = await conn.fetch("SELECT * FROM change_log ORDER BY id")
    retry = await client.post(
        f"/api/sessions/{session_id}/end",
        json=body,
        headers=headers,
    )

    assert retry.status_code == 200, retry.text
    assert retry.json() == first.json()
    async with store.engine.acquire() as conn:
        assert await conn.fetch("SELECT * FROM change_log ORDER BY id") == after_first


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
            AsyncMock(return_value=_live_session()),
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
            AsyncMock(return_value=_live_session()),
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
            AsyncMock(return_value=_live_session()),
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
            AsyncMock(return_value=_live_session()),
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
            AsyncMock(return_value=_live_session()),
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
            AsyncMock(return_value=_live_session()),
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
            AsyncMock(return_value=_live_session()),
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
            AsyncMock(return_value=_live_session()),
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
            AsyncMock(return_value=_live_session()),
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
            AsyncMock(return_value=_live_session()),
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
            AsyncMock(return_value=_live_session()),
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
            AsyncMock(return_value=_live_session(uuid.uuid4())),
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
                return_value=_live_session(
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
    assert await _reap(store, inbound=inbound, after=_WINDOW) == 1
    closed = await _session(store, session_id=session_id)
    assert (closed.status, closed.ended) == ("complete", last_seen)

    # The run was only cut off: its next poll gets the session back.
    polled = await client.get(f"/api/sessions/{session_id}/inbound")
    assert polled.status_code == 200, polled.text
    reopened = await _session(store, session_id=session_id)
    assert (reopened.status, reopened.ended) == ("active", None)
    assert inbound.has_poller(session_id)

    # Closed again, it comes back through an upload too -- and the upload, which
    # an ended session would refuse, lands.
    assert await _reap(store, inbound=inbound, after=_WINDOW) == 1
    uploaded = await client.post(
        f"/api/sessions/{session_id}/records",
        json=_SLASH_ONLY,
    )
    assert uploaded.status_code == 200, uploaded.text
    assert json_loads(uploaded.content, dict[str, object])["slash_commands"] == 1
    assert (await _session(store, session_id=session_id)).status == "active"


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
    assert await _reap(store, inbound=inbound, after=after) == 0
    assert (await _session(store, session_id=session_id)).status == "active"


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
    assert await _reap(store, inbound=inbound, after=timedelta(days=1)) == 0
    assert (await _session(store, session_id=session_id)).status == "active"


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
    assert await _reap(store, inbound=inbound, after=timedelta(days=1)) == 0
    _ = await client.get(f"/api/sessions/{session_id}/inbound")
    assert (await _session(store, session_id=session_id)).status == "complete"


_WINDOW = STALE_AFTER + timedelta(minutes=1)
"""Long enough from now that anything seen so far counts as silent."""


async def _reap(store: Store, *, inbound: InboundQueue, after: timedelta) -> int:
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


async def _session(store: Store, *, session_id: uuid.UUID) -> AgentSession:
    """Return the session's row."""
    row = await store.get_inquiry(session_id)
    assert isinstance(row, AgentSession)
    return row


@pytest.fixture
def assistant_served(monkeypatch: pytest.MonkeyPatch) -> ChatHub:
    """Serve with scout configured, and a fresh poller table and event hub."""
    hub = ChatHub()
    patch_state(monkeypatch, "config", ASSISTANT_CONFIG)
    patch_state(monkeypatch, "inbound", InboundQueue())
    patch_state(monkeypatch, "hub", hub)
    return hub


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_only_the_opening_key_drains_the_assistants_session(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Its queue holds every user's Chat, so a writer who did not open it is refused."""
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_assistant()
    kb = await start_session(client, actor=KB_ACTOR)
    act_as_user_agent()
    own = await start_session(client)

    for role in ("viewer", "writer", "admin"):
        for act in (act_as_user_agent, act_as_other_agent):
            act(role)
            assert (await drain(client, session_id=kb)).status_code == 403
        act_as_assistant(role)
        assert (await drain(client, session_id=kb)).status_code == 200
    browser()
    assert (await drain(client, session_id=kb)).status_code == 403

    # An ordinary session keeps its shared writer access.
    act_as_other_agent("writer")
    assert (await drain(client, session_id=own)).status_code == 200


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


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_only_the_opening_key_drains_a_science_chats_queue(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Its queue holds its posters' lines, so a writer who did not open it is refused.

    The id alone decides: a session that only carries a `chat:` id under another
    account's key is refused to every other key too, and its own key, which is not
    the assistant's, is told the chat is the assistant's.
    """
    client, store = pglite_route_client
    await seed_accounts(store)
    conversation = uuid.uuid4()
    act_as_assistant()
    chat = await start_session(
        client,
        actor="chat-1",
        cli_session_id=chat_session_id(conversation),
    )
    act_as_user_agent()
    squatter = await start_unpolled(
        client,
        actor="chat-2",
        cli_session_id=chat_session_id(uuid.uuid4()),
    )

    for act in (act_as_user_agent, act_as_other_agent):
        for role in ("viewer", "writer", "admin"):
            act(role)
            assert (await drain(client, session_id=chat)).status_code == 403
    browser()
    assert (await drain(client, session_id=chat)).status_code == 403
    act_as_assistant()
    assert (await drain(client, session_id=chat)).status_code == 200

    act_as_other_agent("writer")
    assert (await drain(client, session_id=squatter)).status_code == 403
    act_as_user_agent()
    refused = await drain(client, session_id=squatter)
    assert (refused.status_code, _code(refused)) == (409, "provider_is_assistant")


def _code(response: httpx2.Response) -> str:
    """Return the ``code`` of a refusal's detail."""
    detail = from_plain(
        json_loads(response.content, dict[str, object])["detail"],
        dict[str, object],
    )
    return from_plain(detail["code"], str)


def _message(response: httpx2.Response) -> str:
    """Return the ``message`` of a refusal's detail."""
    detail = from_plain(
        json_loads(response.content, dict[str, object])["detail"],
        dict[str, object],
    )
    return from_plain(detail["message"], str)


async def _canvas_on(
    store: Store,
    *,
    provider: str,
    conversation_id: uuid.UUID,
) -> None:
    """Give the test user a canvas that holds ``conversation_id`` on ``provider``."""
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO visual_canvases (id, user_id, metadata, state) "
            "VALUES (gen_random_uuid(), $1, $2, $3)",
            TEST_USER_ID,
            {"name": f"on-{provider}", "conversation_id": str(conversation_id)},
            {"visuals": [], "provider": provider},
        )


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_chat_on_a_cli_is_drained_by_its_starters_run_and_never_the_assistant(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The canvas's choice holds at the drain: two drainers never share one queue."""
    client, store = pglite_route_client
    await seed_accounts(store)
    conversation = uuid.uuid4()
    chat = await seed_session(
        store,
        ASSISTANT,
        actor="chat-1",
        cli_session_id=chat_session_id(conversation),
        account=TEST_USER_EMAIL,
    )
    await _canvas_on(store, provider="claude", conversation_id=conversation)

    act_as_assistant()
    refused = await drain(client, session_id=chat)
    assert (refused.status_code, _code(refused)) == (409, "provider_is_cli")
    act_as_user_agent("writer")
    assert (await drain(client, session_id=chat, cli="claude")).status_code == 200
    act_as_other_agent("writer")
    assert (await drain(client, session_id=chat, cli="claude")).status_code == 403


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_refused_drain_of_the_chats_own_run_is_kept_for_its_canvas(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A stranger's refused drain is kept for no one; a drain let in forgets it."""
    client, store = pglite_route_client
    await seed_accounts(store)
    conversation = uuid.uuid4()
    chat = await seed_session(
        store,
        ASSISTANT,
        actor="chat-1",
        cli_session_id=chat_session_id(conversation),
        account=TEST_USER_EMAIL,
    )
    await _canvas_on(store, provider="claude", conversation_id=conversation)
    inbound = cast("InboundQueue", app.state.inbound)

    act_as_other_agent("writer")
    assert (await drain(client, session_id=chat, cli="claude")).status_code == 403
    assert inbound.refusal(chat) is None
    act_as_user_agent("writer")
    refused = await drain(client, session_id=chat, cli="codex")
    assert (refused.status_code, _code(refused)) == (409, "provider_is_other_cli")
    kept = inbound.refusal(chat)
    assert kept is not None
    assert kept.reason == _message(refused)
    assert (await drain(client, session_id=chat, cli="claude")).status_code == 200
    assert inbound.refusal(chat) is None


async def _canvas_with_chat(
    store: Store,
    *,
    provider: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed the test user's canvas on ``provider`` and its chat's unpolled session."""
    conversation = uuid.uuid4()
    chat = await seed_session(
        store,
        USER_AGENT,
        actor="chat-1",
        cli=provider,
        cli_session_id=chat_session_id(conversation),
        polled=False,
    )
    await _canvas_on(store, provider=provider, conversation_id=conversation)
    return conversation, chat


async def _switch_canvas_to(
    store: Store,
    *,
    conversation_id: uuid.UUID,
    provider: str,
) -> None:
    """Store ``provider`` as the choice of the canvas that holds the chat."""
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE visual_canvases SET state = state || "
            "jsonb_build_object('provider', $2::text) "
            "WHERE metadata ->> 'conversation_id' = $1",
            str(conversation_id),
            provider,
        )


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_after_a_switch_to_codex_only_codex_drains_the_chat(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A claude run left polling is told so, and a line queued for codex stays queued."""
    client, store = pglite_route_client
    await seed_accounts(store)
    conversation, chat = await _canvas_with_chat(store, provider="claude")
    act_as_user_agent("writer")
    assert (await drain(client, session_id=chat, cli="claude")).status_code == 200
    await _switch_canvas_to(store, conversation_id=conversation, provider="codex")
    inbound = cast("InboundQueue", app.state.inbound)
    _ = inbound.enqueue(chat, Inbound(text="pineapple"))

    stale = await drain(client, session_id=chat, cli="claude")
    assert (stale.status_code, _code(stale)) == (409, "provider_is_other_cli")
    assert inbound.pending(chat) == 1
    fresh = await drain(client, session_id=chat, cli="codex")
    assert fresh.status_code == 200
    assert [
        from_plain(each, dict[str, object])["text"]
        for each in from_plain(
            json_loads(fresh.content, dict[str, object])["messages"],
            list[object],
        )
    ] == ["pineapple"]
    assert inbound.poller_run(chat) == PollerRun(cli="codex")


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_chat_drain_that_names_no_cli_is_told_to_update_trax(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A `trax` from before the drain named its CLI cannot be told apart, so it waits."""
    client, store = pglite_route_client
    await seed_accounts(store)
    _, chat = await _canvas_with_chat(store, provider="claude")
    act_as_user_agent("writer")

    refused = await drain(client, session_id=chat)

    assert (refused.status_code, _code(refused)) == (426, "cli_required")
    assert "Update trax" in _message(refused)
    assert not cast("InboundQueue", app.state.inbound).has_poller(chat)


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_second_run_on_a_chat_with_a_live_run_is_refused(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """One run drains a chat at a time; the next may take it once the first is gone."""
    client, store = pglite_route_client
    await seed_accounts(store)
    _, chat = await _canvas_with_chat(store, provider="claude")
    act_as_user_agent("writer")
    first, second = "a" * 32, "b" * 32

    async def drains(instance: str) -> tuple[int, str | None]:
        answered = await drain(client, session_id=chat, cli="claude", instance=instance)
        return answered.status_code, (
            None if answered.status_code == 200 else _code(answered)
        )

    assert await drains(first) == (200, None)
    assert await drains(second) == (409, "chat_run_live")
    assert await drains(first) == (200, None)
    cast("InboundQueue", app.state.inbound).forget_poller(chat)
    assert await drains(second) == (200, None)
    assert await drains(first) == (409, "chat_run_live")


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_refused_second_run_leaves_the_first_runs_lease_and_lines(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The refusal takes no lease; only the refused run's own end could cost the first."""
    client, store = pglite_route_client
    await seed_accounts(store)
    _, chat = await _canvas_with_chat(store, provider="claude")
    act_as_user_agent("writer")
    inbound = cast("InboundQueue", app.state.inbound)
    first, second = "a" * 32, "b" * 32
    assert (
        await drain(client, session_id=chat, cli="claude", instance=first)
    ).status_code == 200
    _ = inbound.enqueue(chat, Inbound(text="pineapple"))

    refused = await drain(client, session_id=chat, cli="claude", instance=second)

    assert (refused.status_code, _code(refused)) == (409, "chat_run_live")
    held = inbound.poller_run(chat)
    assert held is not None
    assert (held.cli, held.instance) == ("claude", first)
    assert inbound.pending(chat) == 1
    kept = await drain(client, session_id=chat, cli="claude", instance=first)
    assert kept.status_code == 200
    assert [
        from_plain(each, dict[str, object])["text"]
        for each in from_plain(
            json_loads(kept.content, dict[str, object])["messages"],
            list[object],
        )
    ] == ["pineapple"]


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("chosen", ["codex", "assistant"])
async def test_the_exit_of_a_run_a_switch_left_does_not_end_the_chat(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    chosen: str,
) -> None:
    """A switch stops the old run, whose exit must not end the session the chat uses."""
    client, store = pglite_route_client
    await seed_accounts(store)
    conversation, chat = await _canvas_with_chat(store, provider="claude")
    act_as_user_agent("writer")
    assert (await drain(client, session_id=chat, cli="claude")).status_code == 200
    await _switch_canvas_to(store, conversation_id=conversation, provider=chosen)
    inbound = cast("InboundQueue", app.state.inbound)
    _ = inbound.enqueue(chat, Inbound(text="pineapple"))

    left = await client.post(f"/api/sessions/{chat}/end", json={"cli": "claude"})

    assert left.status_code == 200, left.text
    assert (await _session(store, session_id=chat)).ended is None
    assert inbound.pending(chat) == 1


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_the_exit_of_the_chats_own_run_ends_it(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Only a run of a CLI the chat left keeps it open; the chosen one's exit ends it."""
    client, store = pglite_route_client
    await seed_accounts(store)
    _, chat = await _canvas_with_chat(store, provider="codex")
    act_as_user_agent("writer")
    assert (await drain(client, session_id=chat, cli="codex")).status_code == 200

    ended = await client.post(f"/api/sessions/{chat}/end", json={"cli": "codex"})

    assert ended.status_code == 200, ended.text
    assert (await _session(store, session_id=chat)).ended is not None
    assert not cast("InboundQueue", app.state.inbound).has_poller(chat)


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_browser_closes_only_its_own_chat(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The starter closes the chat from a browser, which has no key; a stranger cannot."""
    client, store = pglite_route_client
    await seed_accounts(store)
    _, chat = await _canvas_with_chat(store, provider="claude")

    browser(OTHER_USER_ID, email=OTHER_EMAIL)
    from_a_stranger = await client.post(f"/api/sessions/{chat}/end", json={})
    assert (await _session(store, session_id=chat)).ended is None
    browser()
    from_the_starter = await client.post(f"/api/sessions/{chat}/end", json={})

    assert from_a_stranger.status_code == 403
    assert from_the_starter.status_code == 200, from_the_starter.text
    assert (await _session(store, session_id=chat)).ended is not None


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
    assert (await _session(store, session_id=session_id)).ended is not None


@pytest.mark.parametrize("instance", ["", "A" * 32, "a" * 31, "g" * 32, "a" * 33])
def test_a_run_instance_is_32_lowercase_hex_digits(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
    instance: str,
) -> None:
    """The id a run names itself by is bounded, as it is kept in memory per lease."""
    client, store, _engine = route_client
    app.state.inbound = InboundQueue()
    monkeypatch.setattr(store, "get_inquiry", AsyncMock(return_value=_live_session()))
    monkeypatch.setattr(store, "record_session_seen", AsyncMock())

    r = client.get(
        f"/api/sessions/{uuid.uuid4()}/inbound",
        params={"cli": "claude", "instance": instance},
    )

    assert r.status_code == 422, r.text


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_chat_back_on_the_assistant_refuses_its_starters_run(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A run left attached after a switch back takes none of the assistant's lines."""
    client, store = pglite_route_client
    await seed_accounts(store)
    conversation = uuid.uuid4()
    run = await seed_session(
        store,
        USER_AGENT,
        actor="chat-1",
        cli="claude",
        cli_session_id=chat_session_id(conversation),
    )
    await _canvas_on(store, provider="assistant", conversation_id=conversation)

    act_as_user_agent("writer")
    refused = await drain(client, session_id=run)
    assert (refused.status_code, _code(refused)) == (409, "provider_is_assistant")


def _served(config: Config) -> Request:
    """Return a request to an app configured with ``config``."""
    served = FastAPI()
    served.state.config = config
    return Request({"type": "http", "app": served})


@pytest.mark.parametrize(
    ("provider", "email", "refusal"),
    [
        (None, KB_EMAIL, None),
        (None, TEST_USER_EMAIL, None),
        ("claude", TEST_USER_EMAIL, None),
        ("assistant", KB_EMAIL, None),
        ("assistant", KB_EMAIL.upper(), None),
        (
            "claude",
            KB_EMAIL,
            {
                "code": "provider_is_cli",
                "message": "This chat's canvas answers it with claude",
            },
        ),
        (
            "codex",
            KB_EMAIL.upper(),
            {
                "code": "provider_is_cli",
                "message": "This chat's canvas answers it with codex",
            },
        ),
        (
            "assistant",
            TEST_USER_EMAIL,
            {
                "code": "provider_is_assistant",
                "message": "This chat's canvas answers it with the assistant",
            },
        ),
    ],
)
def test_a_chats_drain_is_refused_to_a_key_its_provider_is_not(
    provider: ChatProvider | None,
    email: str,
    refusal: dict[str, str] | None,
) -> None:
    """The assistant's account is one drainer, everyone else's run the other."""
    identity = make_test_identity(email=email)
    if refusal is None:
        sessions_routes._refuse_the_other_provider(
            _served(ASSISTANT_CONFIG),
            identity,
            provider=provider,
        )
        return
    with pytest.raises(HTTPException) as refused:
        sessions_routes._refuse_the_other_provider(
            _served(ASSISTANT_CONFIG),
            identity,
            provider=provider,
        )
    assert (refused.value.status_code, refused.value.detail) == (409, refusal)


@pytest.mark.parametrize("provider", ["assistant", "claude"])
def test_without_an_assistant_no_key_is_refused_for_a_provider(
    provider: ChatProvider,
) -> None:
    """With no assistant configured there is no second drainer to keep out."""
    sessions_routes._refuse_the_other_provider(
        _served(Config()),
        make_test_identity(email=KB_EMAIL),
        provider=provider,
    )


async def _post_to_the_assistant(
    client: httpx2.AsyncClient,
    *,
    store: Store,
    cli_session_id: str | None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Start the assistant, post a line it has not drained, and end its session."""
    await seed_accounts(store)
    act_as_assistant()
    kb = await start_session(client, actor=KB_ACTOR, cli_session_id=cli_session_id)
    canvas_id = await open_canvas(client)
    sent = await send_chat(
        client,
        canvas_id=canvas_id,
        text="posted while it deployed",
    )
    assert sent.status_code == 200
    act_as_assistant()
    ended = await client.post(f"/api/sessions/{kb}/end", json={"status": "completed"})
    assert ended.status_code == 200
    return kb, canvas_id


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_line_posted_before_the_assistant_restarted_is_heard_when_it_resumes(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A post answered 200 survives the end of the service session it was queued for.

    The assistant resumes the same session by its ``cli_session_id``, and nothing
    else would answer the line: no chat session holds it yet.
    """
    client, store = pglite_route_client
    kb, _ = await _post_to_the_assistant(
        client,
        store=store,
        cli_session_id="scout-service",
    )

    resumed = await start_unpolled(
        client,
        actor=KB_ACTOR,
        cli_session_id="scout-service",
    )

    assert resumed == kb
    act_as_assistant()
    drained = json_loads(
        (await drain(client, session_id=resumed)).content,
        dict[str, object],
    )
    [heard] = from_plain(drained["messages"], list[dict[str, object]])
    assert heard["text"] == "posted while it deployed"


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_line_dropped_with_a_service_session_that_cannot_resume_is_logged(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A session with no ``cli_session_id`` cannot be resumed, so the drop is said."""
    client, store = pglite_route_client
    with caplog.at_level("WARNING", logger=sessions_routes.__name__):
        _ = await _post_to_the_assistant(client, store=store, cli_session_id=None)

    assert TEST_USER_EMAIL in caplog.text
    assert "dropped 1 chat line(s)" in caplog.text


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_session_starting_ending_or_taking_a_lease_nudges_the_streams(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    assistant_served: ChatHub,
) -> None:
    """Open canvases recheck their partner when it may have changed."""
    client, store = pglite_route_client
    await seed_accounts(store)
    canvas_id = uuid.uuid4()
    act_as_assistant()
    with assistant_served.subscribe(canvas_id) as stream:
        started = await client.post("/api/sessions/start", json={"cli": "codex"})
        assert started.status_code == 201
        assert stream.nudged.is_set()
        stream.nudged.clear()
        session_id = json_loads(started.content, dict[str, object])["id"]
        assert (
            await client.get(f"/api/sessions/{session_id}/inbound")
        ).status_code == 200
        assert stream.nudged.is_set()
        stream.nudged.clear()
        assert (
            await client.get(f"/api/sessions/{session_id}/inbound")
        ).status_code == 200
        assert not stream.nudged.is_set()
        ended = await client.post(
            f"/api/sessions/{session_id}/end",
            json={"status": "completed"},
        )
        assert ended.status_code == 200
        assert stream.nudged.is_set()


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_drained_chat_message_carries_its_senders_role(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The partner reads the role of whoever sent each line, as the server saw it."""
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_assistant()
    kb = await start_session(client, actor=KB_ACTOR)
    canvas_id = await open_canvas(client)
    for role in ("writer", "admin"):
        install_identity(make_test_identity(api_key_id=None, role=role))
        sent = await send_chat(
            client,
            canvas_id=canvas_id,
            text=f"as {role}",
        )
        assert sent.status_code == 200
    act_as_assistant()
    drained = json_loads(
        (await drain(client, session_id=kb)).content,
        dict[str, object],
    )
    assert [
        (from_plain(m["text"], str), from_plain(m["source_role"], str))
        for m in from_plain(drained["messages"], list[dict[str, object]])
    ] == [("as writer", "writer"), ("as admin", "admin")]


_AWAIT_MESSAGES = InboundQueue.await_messages
"""The wait itself, as it is before a test wraps it."""


@dataclass(slots=True, kw_only=True)
class _Holds:
    """The requests that parked on the inbound queue to wait for a message."""

    parked: asyncio.Event = field(default_factory=asyncio.Event)
    """Set once a request has begun waiting."""

    asked_sec: list[float] = field(default_factory=list)
    """How long each request asked to wait."""


async def _parked(
    queue: InboundQueue,
    holds: _Holds,
    session_id: uuid.UUID,
    *,
    timeout_sec: float,
) -> list[Inbound]:
    """Wait on ``queue`` as it does, noting that the wait began."""
    holds.asked_sec.append(timeout_sec)
    holds.parked.set()
    return await _AWAIT_MESSAGES(queue, session_id, timeout_sec=timeout_sec)


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_the_assistant_long_polls_and_a_chat_message_reaches_it_at_once(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The way scout drains: a held request that returns when a message arrives."""
    client, store = pglite_route_client
    await seed_accounts(store)
    kb = await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    canvas_id = await open_canvas(client)
    holds = _Holds()
    monkeypatch.setattr(
        InboundQueue,
        "await_messages",
        partialmethod(_parked, holds),
    )

    async def held() -> httpx2.Response:
        act_as_assistant()
        return await client.get(f"/api/sessions/{kb}/inbound", params={"wait_sec": 10})

    waiting = asyncio.ensure_future(held())
    await holds.parked.wait()
    assert not waiting.done()
    assert holds.asked_sec == [10.0]
    browser()
    sent = await send_chat(
        client,
        canvas_id=canvas_id,
        text="wake up",
    )
    response = await asyncio.wait_for(waiting, 5)
    assert response.status_code == 200
    messages = from_plain(
        json_loads(response.content, dict[str, object])["messages"],
        list[object],
    )
    assert [from_plain(m, dict[str, object])["text"] for m in messages] == ["wake up"]
    context = from_plain(
        from_plain(messages[0], dict[str, object])["context"],
        dict[str, object],
    )
    assert context["conversation_id"] == str(receipt_conversation(sent))


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
    hidden = await _session(store, session_id=uuid.UUID(str(bodies[0]["id"])))
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

    hidden = await _session(store, session_id=runs["open"])
    staff = await _session(store, session_id=runs["staff"])
    assert answers["open"].status_code == 201, answers["open"].text
    assert (
        json_loads(answers["open"].content, dict[str, object])["audience"] == "private"
    )
    assert (hidden.audience, hidden.ended) == ("private", None)
    assert answers["staff"].status_code == 409
    assert staff.audience == "staff"
    assert staff.ended is not None


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_session_under_a_staff_chat_id_opens_staff(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A chat stays staff, or private, whichever key opens its next session."""
    client, store = pglite_route_client
    await seed_accounts(store)
    opened: dict[str, str] = {}
    for audience in ("staff", "private", "open"):
        chat = chat_session_id(uuid.uuid4())
        # The prior session under the chat id is scaffolding, not the subject: seed it
        # straight in the store (as the assistant opened it) rather than through two
        # requests, and stamp its audience.
        first = await seed_session(
            store,
            agent=ASSISTANT,
            actor=f"first-{audience}",
            cli_session_id=chat,
            account=TEST_USER_EMAIL,
            polled=False,
        )
        async with store.engine.acquire() as conn:
            await conn.execute(
                "UPDATE inquiries SET agentsession_audience = $2 WHERE id = $1",
                first,
                audience,
            )
        act_as_other_agent("writer")
        # The next key opening a session under the chat id is the subject, so this goes
        # through the real start route; the audience it is given is read back from the
        # row, with no poll the inheritance does not depend on.
        started = await client.post(
            "/api/sessions/start",
            json={"cli": "codex", "cli_session_id": chat},
        )
        assert started.status_code == 201, started.text
        later = uuid.UUID(
            from_plain(json_loads(started.content, dict[str, object])["id"], str),
        )
        opened[audience] = str((await _session(store, session_id=later)).audience)

    assert opened == {"staff": "staff", "private": "private", "open": "open"}


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_new_chats_first_session_opens_as_its_canvas_chose(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The assistant opens a new chat as the canvas of its first line chose."""
    client, store = pglite_route_client
    await seed_accounts(store)
    # The assistant's service session, live, so a first line is heard.
    _ = await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    opened: dict[str | None, object] = {}
    for choice in (None, "private"):
        canvas_id = await open_canvas(client, new=True)
        if choice is not None:
            chosen = await client.patch(
                f"/api/canvases/{canvas_id}",
                json={"opens_as": choice},
            )
            assert chosen.status_code == 200, chosen.text
        conversation_id = receipt_conversation(
            await send_chat(client, canvas_id=canvas_id, text="hello"),
        )
        act_as_assistant("writer")
        started = await client.post(
            "/api/sessions/start",
            json={
                "cli": "codex",
                "actor": f"chat-{conversation_id.hex[:12]}",
                "cli_session_id": chat_session_id(conversation_id),
                "account": TEST_USER_EMAIL,
            },
        )
        assert started.status_code == 201, started.text
        opened[choice] = json_loads(started.content, dict[str, object])["audience"]

    assert opened == {None: "open", "private": "private"}


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
            AsyncMock(return_value=_live_session()),
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
            AsyncMock(return_value=_live_session()),
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
                _live_session(uuid.uuid4()),
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


def _opens_a_part(name: str) -> dict[str, object]:
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


async def _ended_chat(
    client: httpx2.AsyncClient,
    store: Store,
    *,
    conversation_id: uuid.UUID,
) -> uuid.UUID:
    """Open a chat as the assistant, write its first part, and end it."""
    await seed_accounts(store)
    chat = await seed_science_chat(store, conversation_id=conversation_id)
    act_as_assistant("writer")
    written = await client.post(
        f"/api/sessions/{chat}/records",
        json=_opens_a_part("assistant.jsonl"),
    )
    assert written.status_code == 200, written.text
    ended = await client.post(f"/api/sessions/{chat}/end", json={})
    assert ended.status_code == 200, ended.text
    return chat


async def _attach(
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
async def test_the_starters_run_attaches_to_its_chat_and_adds_the_next_part(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The chat keeps ``chat:<id>``, is live again, and the run's file is part 1."""
    client, store = pglite_route_client
    conversation = uuid.uuid4()
    chat = await _ended_chat(client, store, conversation_id=conversation)

    act_as_user_agent("writer")
    attached = await _attach(client, session_id=chat)
    appended = await client.post(
        f"/api/sessions/{chat}/records",
        json=_opens_a_part("claude.jsonl"),
    )

    assert attached.status_code == 201, attached.text
    body = json_loads(attached.content, dict[str, object])
    assert (body["id"], body["cli_session_id"]) == (
        str(chat),
        chat_session_id(conversation),
    )
    row = await _session(store, session_id=chat)
    assert (row.ended, row.cli_session_id) == (None, chat_session_id(conversation))
    assert appended.status_code == 200, appended.text
    assert json_loads(appended.content, dict[str, object])["part"] == 1


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_stranger_attaches_to_no_chat_and_an_unknown_id_is_404(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A refused attach leaves the chat as it was: ended."""
    client, store = pglite_route_client
    chat = await _ended_chat(client, store, conversation_id=uuid.uuid4())

    act_as_other_agent("writer")
    refused = await _attach(client, session_id=chat)
    unknown = await _attach(client, session_id=uuid.uuid4())

    assert refused.status_code == 403, refused.text
    assert (await _session(store, session_id=chat)).ended is not None
    assert unknown.status_code == 404, unknown.text


async def _report(
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
@pytest.mark.asyncio(loop_scope="session")
async def test_only_a_state_that_changed_nudges_the_streams(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    assistant_served: ChatHub,
) -> None:
    """A run says its state again while it holds; open canvases recheck only a change."""
    client, store = pglite_route_client
    await seed_accounts(store)
    _, chat = await _canvas_with_chat(store, provider="claude")
    act_as_user_agent("writer")
    run = "a" * 32
    assert (
        await drain(client, session_id=chat, cli="claude", instance=run)
    ).status_code == 200
    with assistant_served.subscribe(uuid.uuid4()) as stream:
        _ = await _report(client, session_id=chat, state="taken_over", instance=run)
        changed = stream.nudged.is_set()
        stream.nudged.clear()
        _ = await _report(client, session_id=chat, state="taken_over", instance=run)
        repeated = stream.nudged.is_set()

    assert (changed, repeated) == (True, False)


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_run_reports_its_state_and_its_next_drains_keep_it(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The state is the run's word; a drain renews the lease and leaves it be."""
    client, store = pglite_route_client
    await seed_accounts(store)
    _, chat = await _canvas_with_chat(store, provider="claude")
    act_as_user_agent("writer")
    run = "a" * 32
    inbound = cast("InboundQueue", app.state.inbound)
    assert (
        await drain(client, session_id=chat, cli="claude", instance=run)
    ).status_code == 200

    reported = await _report(
        client,
        session_id=chat,
        state="requires_action",
        instance=run,
    )
    assert (
        await drain(client, session_id=chat, cli="claude", instance=run)
    ).status_code == 200

    assert reported.status_code == 204, reported.text
    held = inbound.poller_run(chat)
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
    _, chat = await _canvas_with_chat(store, provider="claude")
    act_as_user_agent("writer")
    inbound = cast("InboundQueue", app.state.inbound)
    first, second = "a" * 32, "b" * 32

    unleased = await _report(client, session_id=chat, state="running", instance=first)
    assert (
        await drain(client, session_id=chat, cli="claude", instance=first)
    ).status_code == 200
    other = await _report(client, session_id=chat, state="taken_over", instance=second)

    assert (unleased.status_code, other.status_code) == (204, 204)
    held = inbound.poller_run(chat)
    assert held is not None
    assert held.state == "idle"


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_only_the_chats_own_run_reports_its_state(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The report is held to the drain's rules: its starter's key, never the assistant's."""
    client, store = pglite_route_client
    await seed_accounts(store)
    _, chat = await _canvas_with_chat(store, provider="claude")
    run = "a" * 32

    browser()
    from_a_browser = await _report(
        client,
        session_id=chat,
        state="running",
        instance=run,
    )
    act_as_other_agent("writer")
    from_a_stranger = await _report(
        client,
        session_id=chat,
        state="running",
        instance=run,
    )
    act_as_assistant("writer")
    from_the_assistant = await _report(
        client,
        session_id=chat,
        state="running",
        instance=run,
    )
    act_as_user_agent("writer")
    unknown = await _report(
        client,
        session_id=uuid.uuid4(),
        state="running",
        instance=run,
    )

    assert from_a_browser.status_code == 403
    assert from_a_stranger.status_code == 403
    assert (from_the_assistant.status_code, _code(from_the_assistant)) == (
        409,
        "provider_is_cli",
    )
    assert unknown.status_code == 404


async def _put_output(
    client: httpx2.AsyncClient,
    *,
    session_id: uuid.UUID,
    instance: str,
    text: str,
) -> httpx2.Response:
    """Post a clicked command's output so far as the installed identity."""
    return await client.post(
        f"/api/sessions/{session_id}/runs/{'c' * 32}/output",
        json={"instance": instance, "text": text},
    )


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_running_commands_output_is_held_from_the_chats_run_for_its_readers(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Only the run holding the lease writes it; a post from another changes nothing."""
    client, store = pglite_route_client
    await seed_accounts(store)
    _, chat = await _canvas_with_chat(store, provider="claude")
    act_as_user_agent("writer")
    run = "a" * 32
    assert (
        await drain(client, session_id=chat, cli="claude", instance=run)
    ).status_code == 200

    held = await _put_output(client, session_id=chat, instance=run, text="tick 1")
    other = await _put_output(client, session_id=chat, instance="b" * 32, text="gone")
    browser()
    read = await client.get(f"/api/sessions/{chat}/runs/{'c' * 32}/output")
    none = await client.get(f"/api/sessions/{chat}/runs/{'d' * 32}/output")

    assert (held.status_code, other.status_code) == (204, 204)
    assert read.status_code == 200, read.text
    state = ChatPartialState.model_validate(read.json())
    assert state.partial == ChatPartial(turn="c" * 32, text="tick 1")
    assert state.seq > 0
    assert ChatPartialState.model_validate(none.json()) == ChatPartialState(
        seq=0,
        partial=None,
    )


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_only_the_chats_own_run_puts_its_commands_output(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The post is held to the drain's rules, as a run's state report is."""
    client, store = pglite_route_client
    await seed_accounts(store)
    _, chat = await _canvas_with_chat(store, provider="claude")
    run = "a" * 32

    browser()
    from_a_browser = await _put_output(client, session_id=chat, instance=run, text="x")
    act_as_other_agent("writer")
    from_a_stranger = await _put_output(client, session_id=chat, instance=run, text="x")
    unknown = await client.get(f"/api/sessions/{uuid.uuid4()}/runs/{'c' * 32}/output")

    assert (from_a_browser.status_code, from_a_stranger.status_code) == (403, 403)
    assert unknown.status_code == 404


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
async def test_a_replaced_run_is_turned_away_and_its_successor_is_not(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The run the server replaced cannot take the lease back from the run replacing it."""
    client, store = pglite_route_client
    await seed_accounts(store)
    _, chat = await _canvas_with_chat(store, provider="claude")
    act_as_user_agent("writer")
    old, new = "a" * 32, "b" * 32
    inbound = cast("InboundQueue", app.state.inbound)
    assert (
        await drain(client, session_id=chat, cli="claude", instance=old)
    ).status_code == 200

    inbound.retire_run(chat)
    turned_away = await drain(client, session_id=chat, cli="claude", instance=old)
    taken = await drain(client, session_id=chat, cli="claude", instance=new)

    assert (turned_away.status_code, _code(turned_away)) == (409, "chat_run_live")
    assert taken.status_code == 200
    held = inbound.poller_run(chat)
    assert held is not None
    assert held.instance == new


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_stopped_run_is_refused_with_a_403_and_a_new_run_is_not(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A 403 ends every run ever shipped, where a 409 code it does not know is retried."""
    client, store = pglite_route_client
    await seed_accounts(store)
    _, chat = await _canvas_with_chat(store, provider="claude")
    act_as_user_agent("writer")
    old, new = "a" * 32, "b" * 32
    inbound = cast("InboundQueue", app.state.inbound)
    first = await drain(
        client,
        session_id=chat,
        cli="claude",
        instance=old,
        machine="box",
    )
    assert first.status_code == 200
    assert list(inbound.runs_on("box", account=TEST_USER_EMAIL)) == [chat]

    inbound.stop_run(chat)
    turned_away = await drain(client, session_id=chat, cli="claude", instance=old)
    taken = await drain(client, session_id=chat, cli="claude", instance=new)

    assert (turned_away.status_code, _code(turned_away)) == (403, "run_stopped")
    assert taken.status_code == 200


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_run_draining_a_chat_that_was_closed_is_told_the_session_ended(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Closing a chat ends its session; the run still polling it is turned away for good."""
    client, store = pglite_route_client
    await seed_accounts(store)
    _, chat = await _canvas_with_chat(store, provider="claude")
    act_as_user_agent("writer")
    assert (await drain(client, session_id=chat, cli="claude")).status_code == 200

    ended = await client.post(f"/api/sessions/{chat}/end", json={"actor": "scientist"})
    assert ended.status_code == 200, ended.text
    turned_away = await drain(client, session_id=chat, cli="claude")

    assert (turned_away.status_code, _code(turned_away)) == (409, "session_ended")
    assert "session_ended" in RUN_ENDING_REFUSALS
    assert not cast("InboundQueue", app.state.inbound).has_poller(chat)


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_chat_run_closed_for_silence_is_revived_by_its_drain_not_turned_away(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Only a session its own end or a close ended is refused; the reaper's close is undone."""
    client, store = pglite_route_client
    await seed_accounts(store)
    _, chat = await _canvas_with_chat(store, provider="claude")
    act_as_user_agent("writer")
    inbound = cast("InboundQueue", app.state.inbound)
    assert (await drain(client, session_id=chat, cli="claude")).status_code == 200
    assert await _reap(store, inbound=inbound, after=_WINDOW) == 1

    back = await drain(client, session_id=chat, cli="claude")

    assert back.status_code == 200, back.text
    assert (await _session(store, session_id=chat)).ended is None


async def _open_asks(store: Store, session_id: uuid.UUID) -> list[str]:
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
    _, chat = await _canvas_with_chat(store, provider="claude")
    act_as_user_agent("writer")
    assert (await drain(client, session_id=chat, cli="claude")).status_code == 200
    await open_ask(store, chat, ask_id="a1")

    ended = await client.post(f"/api/sessions/{chat}/end", json={"cli": "claude"})

    assert ended.status_code == 200, ended.text
    assert (await _session(store, session_id=chat)).ended is not None
    assert await _open_asks(store, chat) == []


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_the_exit_of_a_run_a_switch_left_keeps_the_asks_for_the_loop(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """That exit changes nothing, so it releases nothing: the chat goes on."""
    client, store = pglite_route_client
    await seed_accounts(store)
    conversation, chat = await _canvas_with_chat(store, provider="claude")
    act_as_user_agent("writer")
    assert (await drain(client, session_id=chat, cli="claude")).status_code == 200
    await _switch_canvas_to(store, conversation_id=conversation, provider="codex")
    await open_ask(store, chat, ask_id="a1")

    left = await client.post(f"/api/sessions/{chat}/end", json={"cli": "claude"})

    assert left.status_code == 200, left.text
    assert await _open_asks(store, chat) == ["a1"]


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_the_exit_of_a_run_a_reattach_replaced_leaves_the_new_run_alone(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The replaced run may quit before it learns so; its end must not end the chat."""
    client, store = pglite_route_client
    await seed_accounts(store)
    _, chat = await _canvas_with_chat(store, provider="claude")
    act_as_user_agent("writer")
    old, new = "a" * 32, "b" * 32
    inbound = cast("InboundQueue", app.state.inbound)
    assert (
        await drain(client, session_id=chat, cli="claude", instance=old)
    ).status_code == 200
    inbound.retire_run(chat)
    assert (
        await drain(client, session_id=chat, cli="claude", instance=new)
    ).status_code == 200
    await open_ask(store, chat, ask_id="a1")

    left = await client.post(
        f"/api/sessions/{chat}/end",
        json={"cli": "claude", "instance": old},
    )

    assert left.status_code == 200, left.text
    assert (await _session(store, session_id=chat)).ended is None
    held = inbound.poller_run(chat)
    assert held is not None
    assert held.instance == new
    assert await _open_asks(store, chat) == ["a1"]


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_the_exit_of_the_run_that_holds_the_chat_ends_it(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A run that names its own instance still ends its chat."""
    client, store = pglite_route_client
    await seed_accounts(store)
    _, chat = await _canvas_with_chat(store, provider="claude")
    act_as_user_agent("writer")
    own = "a" * 32
    assert (
        await drain(client, session_id=chat, cli="claude", instance=own)
    ).status_code == 200
    await open_ask(store, chat, ask_id="a1")

    ended = await client.post(
        f"/api/sessions/{chat}/end",
        json={"cli": "claude", "instance": own},
    )

    assert ended.status_code == 200, ended.text
    assert (await _session(store, session_id=chat)).ended is not None
    assert await _open_asks(store, chat) == []


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
