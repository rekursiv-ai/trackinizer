"""Tests for the agent-session ingest and messaging routes.

Covers the ``POST /api/messages`` idempotency contract (a send that
reaches no live session must not poison the idempotency cache) and the
``POST /api/sessions/{id}/end`` atomicity contract (a failed close must
not drain the session's inbound queue).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import uuid

import pytest

from trackinizer.lib.codec import from_plain, loads
from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import (
    TEST_API_KEY_ID,
    TEST_USER_EMAIL,
    TEST_USER_ID,
    install_identity,
    make_test_identity,
)
from trackinizer.server.inbound import Inbound, InboundQueue
from trackinizer.server.session_reaper import (
    STALE_AFTER,
    reap_silent_sessions,
)
from trackinizer.types.inquiries import AgentSession


if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    import httpx2

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.store.core import Store


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


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
