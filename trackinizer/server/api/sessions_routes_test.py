"""Tests for the agent-session ingest and messaging routes.

Covers the ``POST /api/messages`` idempotency contract (a send that
reaches no live session must not poison the idempotency cache) and the
``POST /api/sessions/{id}/end`` atomicity contract (a failed close must
not drain the session's inbound queue).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial, partialmethod
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import asyncio
import uuid

from fastapi import FastAPI

import httpx2
import pytest

from trackinizer.lib.codec import from_plain, loads
from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.api import sessions_routes
from trackinizer.server.api.app import app
from trackinizer.server.api.canvas_test_support import (
    ASSISTANT,
    ASSISTANT_CONFIG,
    KB_ACTOR,
    USER_AGENT,
    act_as_assistant,
    act_as_other_agent,
    act_as_user_agent,
    browser,
    conversation_of,
    drain,
    open_canvas,
    open_workspace,
    revision_of,
    seed_accounts,
    seed_session,
    send_chat,
    show_chat,
    start_session,
)
from trackinizer.server.api.conftest import (
    TEST_API_KEY_ID,
    TEST_USER_EMAIL,
    TEST_USER_ID,
    install_identity,
    make_test_identity,
)
from trackinizer.server.api.science_chat_routes_test import (
    seed_helper_chat,
    start_unpolled,
)
from trackinizer.server.auth import current_user
from trackinizer.server.chat_hub import ChatHub
from trackinizer.server.config import Config
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.inbound import Inbound, InboundQueue
from trackinizer.server.session_reaper import (
    STALE_AFTER,
    reap_silent_sessions,
)
from trackinizer.server.store.core import Store
from trackinizer.types.inquiries import AgentSession
from trackinizer.wire.wire_science_chat import CHAT_HELPER_CLI, chat_session_id


if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from trackinizer.conftest import FakeEngine
    from trackinizer.lib.postgres import PGliteEngine
    from trackinizer.server.auth import AuthIdentity, Role


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
    assert from_plain(loads(uploaded.content), dict[str, object])["slash_commands"] == 1
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
            from_plain(loads(started.content), dict[str, object])["id"],
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
    monkeypatch.setattr(app.state, "config", ASSISTANT_CONFIG, raising=False)
    monkeypatch.setattr(app.state, "inbound", InboundQueue(), raising=False)
    monkeypatch.setattr(app.state, "hub", hub, raising=False)
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
    """The refusal is the assistant's alone."""
    client, store = pglite_route_client
    monkeypatch.setattr(app.state, "config", Config(), raising=False)
    monkeypatch.setattr(app.state, "inbound", InboundQueue(), raising=False)
    await seed_accounts(store)
    act_as_assistant()
    kb = await start_session(client, actor=KB_ACTOR)
    act_as_other_agent("writer")
    assert (await drain(client, session_id=kb)).status_code == 200


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_chat_helper_takes_messages_only_through_chat(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A Console send by name, or a direct one, to a `trax helper` session is refused.

    An assistant that runs as anything else keeps its `trax send` surface: only
    the helper, which answers nothing outside Chat, is closed.
    """
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_user_agent()
    helper = await start_session(client, actor="helper", cli=CHAT_HELPER_CLI)
    act_as_assistant()
    kb = await start_session(client, actor=KB_ACTOR)

    act_as_other_agent("writer")
    direct = await client.post(f"/api/sessions/{helper}/inbound", json={"text": "hi"})
    routed = await client.post("/api/messages", json={"actor": "helper", "text": "hi"})
    for refused in (direct, routed):
        assert refused.status_code == 403, refused.text
        assert "only through Chat" in refused.text
    # Nothing reached its queue.
    act_as_user_agent()
    drained = from_plain(
        loads((await drain(client, session_id=helper)).content),
        dict[str, object],
    )
    assert drained["messages"] == []

    # Any other session, the assistant included, still takes both.
    act_as_other_agent("writer")
    direct = await client.post(f"/api/sessions/{kb}/inbound", json={"text": "hi"})
    routed = await client.post("/api/messages", json={"actor": KB_ACTOR, "text": "hi"})
    assert (direct.status_code, routed.status_code) == (200, 200), routed.text


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_only_the_opening_key_drains_a_science_chats_queue(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Its queue holds its posters' lines, so a writer who did not open it is refused.

    The id alone decides: a session that only carries a `chat:` id under another
    account's key is refused to every other key too, and its own key drains it.
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
    squatter = await start_session(
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
    assert (await drain(client, session_id=squatter)).status_code == 200


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_only_the_opening_key_drains_a_users_own_helper_sessions(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A helper's service session and its chats are its owner's, assistant or not.

    A drain by another writer would take the owner's lines, canvas context
    included, and its poll would make the chat a destination the helper never reads.
    """
    client, store = pglite_route_client
    await seed_accounts(store)
    service = await seed_session(
        store,
        USER_AGENT,
        actor="helper",
        cli=CHAT_HELPER_CLI,
    )
    chat = await seed_helper_chat(store, conversation_id=uuid.uuid4())

    act_as_other_agent("writer")
    for session in (service, chat):
        assert (await drain(client, session_id=session)).status_code == 403
    act_as_user_agent("writer")
    for session in (service, chat):
        assert (await drain(client, session_id=session)).status_code == 200


async def _post_to_the_assistant(
    client: httpx2.AsyncClient,
    *,
    store: Store,
    cli_session_id: str | None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Start the assistant, post a line it has not drained, and end its session."""
    await seed_accounts(store)
    act_as_assistant()
    kb = await start_session(client, actor=KB_ACTOR, cli_session_id=cli_session_id)
    workspace_id = await open_workspace(client)
    chat_id = await show_chat(client, workspace_id=workspace_id)
    sent = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="posted while it deployed",
    )
    assert sent.status_code == 200
    act_as_assistant()
    ended = await client.post(f"/api/sessions/{kb}/end", json={"status": "completed"})
    assert ended.status_code == 200
    return kb, workspace_id, chat_id


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
    kb, _, _ = await _post_to_the_assistant(
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
    drained = from_plain(
        loads((await drain(client, session_id=resumed)).content),
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
async def test_a_helper_service_session_keeps_its_unread_lines_when_it_resumes(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A `trax helper` that restarts hears the line posted while it was down."""
    client, store = pglite_route_client
    await seed_accounts(store)
    helper = await seed_session(
        store,
        USER_AGENT,
        actor="helper",
        cli=CHAT_HELPER_CLI,
        cli_session_id="trax-helper:helper",
    )
    workspace_id, chat_id = await open_canvas(client)
    chosen = await client.post(
        f"/api/workspaces/{workspace_id}/operations",
        json={
            "revision": await revision_of(client, workspace_id=workspace_id),
            "operation": {"kind": "partner", "choice": "local"},
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert chosen.status_code == 200, chosen.text
    sent = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="before the restart",
    )
    assert sent.status_code == 200, sent.text
    act_as_user_agent()
    ended = await client.post(f"/api/sessions/{helper}/end", json={})
    assert ended.status_code == 200

    resumed = await start_unpolled(
        client,
        actor="helper",
        cli=CHAT_HELPER_CLI,
        cli_session_id="trax-helper:helper",
    )

    assert resumed == helper
    drained = from_plain(
        loads((await drain(client, session_id=resumed)).content),
        dict[str, object],
    )
    [heard] = from_plain(drained["messages"], list[dict[str, object]])
    assert heard["text"] == "before the restart"


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
    workspace_id = uuid.uuid4()
    act_as_assistant()
    with assistant_served.subscribe(workspace_id) as stream:
        started = await client.post("/api/sessions/start", json={"cli": "codex"})
        assert started.status_code == 201
        assert stream.nudged.is_set()
        stream.nudged.clear()
        session_id = from_plain(loads(started.content), dict[str, object])["id"]
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
    workspace_id = await open_workspace(client)
    chat_id = await show_chat(client, workspace_id=workspace_id)
    for role in ("writer", "admin"):
        install_identity(make_test_identity(api_key_id=None, role=role))
        sent = await send_chat(
            client,
            workspace_id=workspace_id,
            chat_id=chat_id,
            text=f"as {role}",
        )
        assert sent.status_code == 200
    act_as_assistant()
    drained = from_plain(
        loads((await drain(client, session_id=kb)).content),
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
    workspace_id = await open_workspace(client)
    chat_id = await show_chat(client, workspace_id=workspace_id)
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
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="wake up",
    )
    response = await asyncio.wait_for(waiting, 5)
    assert response.status_code == 200
    messages = from_plain(
        from_plain(loads(response.content), dict[str, object])["messages"],
        list[object],
    )
    assert [from_plain(m, dict[str, object])["text"] for m in messages] == ["wake up"]
    context = from_plain(
        from_plain(messages[0], dict[str, object])["context"],
        dict[str, object],
    )
    assert context["conversation_id"] == str(conversation_of(sent))


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_session_starts_on_an_app_built_by_hand_with_no_hub_or_config(
    pglite_engine: PGliteEngine,
) -> None:
    """A harness that wires only engine, store and inbound, as the integration test does."""
    await reset_schema(pglite_engine)
    store = Store(pglite_engine, embed=StubEmbedder())
    await store.bootstrap()
    async with pglite_engine.acquire() as conn:
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
    bespoke.state.engine = pglite_engine
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
        session_id = from_plain(loads(started.content), dict[str, object])["id"]
        polled = await client.get(f"/api/sessions/{session_id}/inbound")
        assert polled.status_code == 200
        ended = await client.post(
            f"/api/sessions/{session_id}/end",
            json={"status": "completed"},
        )
        assert ended.status_code == 200


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
