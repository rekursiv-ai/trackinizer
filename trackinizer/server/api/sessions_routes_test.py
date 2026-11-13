"""Tests for the agent-session ingest and messaging routes.

Covers the ``POST /api/messages`` idempotency contract (a send that
reaches no live session must not poison the idempotency cache), who may
drive a session, and the session lifecycle routes.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal, cast, override
from unittest.mock import AsyncMock, Mock

import uuid

from fastapi import FastAPI
from treekle import from_plain, json_loads

import httpx2
import pytest

from trackinizer.conftest import set_field_row
from trackinizer.server.api import sessions_routes
from trackinizer.server.api.conftest import (
    REAP_WINDOW,
    TEST_API_KEY_ID,
    TEST_USER_EMAIL,
    TEST_USER_ID,
    attach_run,
    install_identity,
    live_session,
    make_test_identity,
    opens_a_part,
    patch_state,
    reap,
    session_row,
)
from trackinizer.server.api.session_test_support import (
    ASSISTANT_CONFIG,
    USER_AGENT,
    act_as_user_agent,
    attached_process,
    seed_accounts,
    seed_session,
)
from trackinizer.server.auth import ProcessKey, current_user
from trackinizer.server.session_reaper import (
    STALE_AFTER,
)
from trackinizer.server.session_relay import (
    InputRefusedError,
    NotLiveError,
    SessionRelay,
)
from trackinizer.types.inquiries import AgentSession, Issue
from trackinizer.wire.wire_session_relay import AgentProcess


if TYPE_CHECKING:
    from collections.abc import Mapping

    from fastapi.testclient import TestClient

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.auth import AuthIdentity, Role
    from trackinizer.server.store.core import Store
    from trackinizer.types.inquiries import Inquiry
    from trackinizer.wire.json_types import JSON
    from trackinizer.wire.wire_session_relay import NewInput
    from trackinizer.wire.wire_sessions import MessageKind


type _State = Literal["starting", "attached", "detached"]
"""A process's state, as the relay holds it."""


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
        _ = _relayed(monkeypatch, {session_id: "attached"})
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
        # original receipt rather than sending twice.
        client, store, _engine = route_client
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
        relay = _relayed(monkeypatch, {session_id: "attached"})

        r1 = client.post(
            "/api/messages",
            json={"actor": "scientist", "text": "hi", "room": "sear"},
            headers={"Idempotency-Key": key},
        )
        assert from_plain(r1.json(), dict[str, object])["delivered"] == [
            str(session_id),
        ]
        # Replay: the recorded receipt comes back; nothing is sent a second time.
        r2 = client.post(
            "/api/messages",
            json={"actor": "scientist", "text": "hi", "room": "sear"},
            headers={"Idempotency-Key": key},
        )
        assert from_plain(r2.json(), dict[str, object])["delivered"] == [
            str(session_id),
        ]
        assert len(relay.sent) == 1


class TestSessionEnd:
    @pytest.mark.parametrize("original_id", [None, "vendor-old"])
    @pytest.mark.parametrize("owns_session", [True, False])
    def test_vendor_id_finalization_takes_only_what_the_end_takes(
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
            account=TEST_USER_EMAIL,
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


@pytest.mark.parametrize("role", ["writer", "admin"])
@pytest.mark.parametrize(
    "act",
    [
        "end",
        "append",
        "attach",
    ],
)
def test_reading_another_users_run_does_not_let_a_caller_drive_it(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
    role: Role,
    act: str,
) -> None:
    client, store, engine = route_client
    session = replace(
        live_session(uuid.uuid4()),
        account="owner@example.com",
        cli_session_id="vendor-1",
        audience="open",
    )
    engine.conn.fetchval.return_value = True
    monkeypatch.setattr(store, "get_inquiry", AsyncMock(return_value=session))
    ended = AsyncMock()
    monkeypatch.setattr(store, "end_session", ended)
    install_identity(make_test_identity(role=role))
    url = f"/api/sessions/{session.id}"

    response = {
        "end": partial(client.post, f"{url}/end", json={}),
        "append": partial(client.post, f"{url}/records", json=opens_a_part("a.jsonl")),
        "attach": partial(
            client.post,
            "/api/sessions/start",
            json={"cli": "claude", "attach": str(session.id)},
        ),
    }[act]()

    assert response.status_code == 403, response.text
    ended.assert_not_awaited()


@pytest.mark.parametrize("scoped", [False, True])
def test_a_routed_send_with_a_foreign_target_sends_nothing(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
    *,
    scoped: bool,
) -> None:
    client, store, engine = route_client
    patch_state(monkeypatch, "config", ASSISTANT_CONFIG)
    own = replace(live_session(), account=TEST_USER_EMAIL, audience="open")
    other = replace(
        live_session(uuid.uuid4()),
        account="owner@example.com",
        audience="open",
    )
    engine.conn.fetchval.return_value = True
    monkeypatch.setattr(store, "get_inquiry", AsyncMock(side_effect=[own, other]))
    monkeypatch.setattr(
        store,
        "resolve_live_sessions",
        AsyncMock(return_value=[(own.id, ("ops",)), (other.id, ("ops",))]),
    )
    relay = _relayed(monkeypatch, {own.id: "attached", other.id: "attached"})

    response = client.post(
        "/api/messages",
        json={"actor": "scientist", "text": "hello", "room": "ops" if scoped else None},
    )

    assert response.status_code == 403, response.text
    assert relay.sent == []


@pytest.mark.parametrize("key", [None, uuid.UUID(int=10)])
def test_the_session_account_can_send_with_a_browser_or_another_key(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
    key: uuid.UUID | None,
) -> None:
    client, store, _engine = route_client
    session = replace(live_session(), account=TEST_USER_EMAIL.upper(), audience="staff")
    monkeypatch.setattr(store, "get_inquiry", AsyncMock(return_value=session))
    monkeypatch.setattr(
        store,
        "resolve_live_sessions",
        AsyncMock(return_value=[(session.id, ())]),
    )
    relay = _relayed(monkeypatch, {session.id: "attached"})
    install_identity(make_test_identity(api_key_id=key))

    routed = client.post("/api/messages", json={"actor": "scientist", "text": "routed"})

    assert routed.status_code == 200, routed.text
    assert [(sent[0], sent[2]) for sent in relay.sent] == [
        (session.id, TEST_USER_EMAIL),
    ]


def test_a_routed_send_skips_a_gone_an_ended_and_an_unserved_session(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store, _engine = route_client
    ended = replace(live_session(), account=TEST_USER_EMAIL, ended=datetime.now(UTC))
    unserved = replace(live_session(), account=TEST_USER_EMAIL)
    gone = uuid.uuid4()
    monkeypatch.setattr(
        store,
        "get_inquiry",
        AsyncMock(side_effect=[None, ended, unserved]),
    )
    monkeypatch.setattr(
        store,
        "resolve_live_sessions",
        AsyncMock(return_value=[(gone, ()), (ended.id, ()), (unserved.id, ())]),
    )
    relay = _relayed(monkeypatch, {ended.id: "attached"})

    sent = client.post("/api/messages", json={"actor": "scientist", "text": "hi"})

    assert sent.status_code == 200, sent.text
    assert from_plain(sent.json(), dict[str, object])["delivered"] == []
    assert relay.sent == []


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

    def test_start_names_the_account_that_holds_the_session(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, store, _engine = route_client
        monkeypatch.setattr(
            store,
            "start_session",
            AsyncMock(return_value=(uuid.uuid4(), "scientist", 0)),
        )
        held = replace(live_session(), account="owner@example.com")
        monkeypatch.setattr(store, "get_inquiry", AsyncMock(return_value=held))

        response = client.post("/api/sessions/start", json={"cli": "claude"})

        assert response.status_code == 201, response.text
        assert (
            json_loads(response.content, dict[str, object])["account"] == held.account
        )

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


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_dead_run_is_closed_and_a_returning_one_reopened(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The reaper closes a silent session; an upload brings it back."""
    client, store = pglite_route_client
    await _active_user(store)
    session_id = await _start(client)

    await _watched(store, session_id=session_id)
    last_seen = await _last_seen(store, session_id=session_id)
    assert await reap(store, after=REAP_WINDOW) == 1
    closed = await session_row(store, session_id=session_id)
    assert (closed.status, closed.ended) == ("complete", last_seen)

    # The run was only cut off: its next upload gets the session back -- and the
    # upload, which an ended session would refuse, lands.
    uploaded = await client.post(
        f"/api/sessions/{session_id}/records",
        json=opens_a_part("a.jsonl"),
    )
    assert uploaded.status_code == 200, uploaded.text
    assert json_loads(uploaded.content, dict[str, object])["written"] == 1
    assert (await session_row(store, session_id=session_id)).status == "active"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_upload_keeps_a_watched_session_open(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A run's uploads say it is alive between the signs its process gives."""
    client, store = pglite_route_client
    await _active_user(store)
    session_id = await _start(client)

    await _watched(store, session_id=session_id)
    await _seen_ago(store, session_id=session_id, ago=timedelta(minutes=10))
    uploaded = await client.post(
        f"/api/sessions/{session_id}/records",
        json=opens_a_part("a.jsonl"),
    )
    assert uploaded.status_code == 200, uploaded.text
    # Silent for the whole window as of the watch, but not as of the upload.
    after = STALE_AFTER - timedelta(minutes=5)
    assert await reap(store, after=after) == 0
    assert (await session_row(store, session_id=session_id)).status == "active"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_session_nothing_watches_is_never_closed(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A capture-only producer has no process whose silence could mean anything."""
    client, store = pglite_route_client
    await _active_user(store)
    session_id = await _start(client)

    uploaded = await client.post(
        f"/api/sessions/{session_id}/records",
        json=opens_a_part("a.jsonl"),
    )
    assert uploaded.status_code == 200, uploaded.text
    assert await reap(store, after=timedelta(days=1)) == 0
    assert (await session_row(store, session_id=session_id)).status == "active"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_cleanly_ended_session_stays_ended(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Only the reaper's own closes are undone by a return."""
    client, store = pglite_route_client
    await _active_user(store)
    session_id = await _start(client)

    await _watched(store, session_id=session_id)
    ended = await client.post(
        f"/api/sessions/{session_id}/end",
        json={"actor": "scientist"},
    )
    assert ended.status_code == 200, ended.text
    assert await reap(store, after=timedelta(days=1)) == 0
    _ = await client.post(
        f"/api/sessions/{session_id}/records",
        json=opens_a_part("a.jsonl"),
    )
    assert (await session_row(store, session_id=session_id)).status == "complete"


async def _watched(store: Store, *, session_id: uuid.UUID) -> None:
    """Start the reaper's watch of a session, as an attached process does."""
    await store.record_session_seen(session_id, at=datetime.now(UTC), polled=True)


async def _last_seen(store: Store, *, session_id: uuid.UUID) -> datetime:
    """Return when ``session_id`` was last recorded as heard from."""
    async with store.engine.acquire() as conn:
        seen = await conn.fetchval(
            "SELECT last_seen FROM session_liveness WHERE session_id = $1",
            session_id,
        )
    assert isinstance(seen, datetime)
    return seen


async def _seen_ago(
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
async def test_a_private_start_that_keeps_the_audience_hides_only_a_run_it_mints(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """An upload of a finished run leaves the run the server holds to its readers."""
    client, store = pglite_route_client
    await _active_user(store)
    held = {"cli": "claude", "cli_session_id": "run-held"}
    keeps = {"private": True, "keep_audience": True}

    answers = [
        await client.post("/api/sessions/start", json=body)
        for body in (
            held,
            {**held, **keeps},
            {"cli": "claude", "cli_session_id": "run-new", **keeps},
        )
    ]

    bodies = [json_loads(each.content, dict[str, object]) for each in answers]
    assert [each.status_code for each in answers] == [201, 201, 201]
    assert [body["audience"] for body in bodies] == ["open", "open", "private"]
    assert bodies[1]["id"] == bodies[0]["id"]
    kept = await session_row(store, session_id=uuid.UUID(str(bodies[0]["id"])))
    assert kept.audience == "open"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_session_starts_on_an_app_built_by_hand_with_no_hub_or_config(
    store: Store,
) -> None:
    """A harness that wires only engine and store, as the integration test does."""
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
        ended = await client.post(
            f"/api/sessions/{session_id}/end",
            json={"status": "completed"},
        )
        assert ended.status_code == 200


def test_a_session_the_old_chat_helper_opened_takes_messages_like_any_other(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The helper's name grants no input rights, as for every other CLI."""
    client, store, engine = route_client
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
    relay = _relayed(monkeypatch, {session_id: "attached"})

    routed = client.post("/api/messages", json={"actor": "helper", "text": "b"})

    assert routed.status_code == 403, routed.text
    assert relay.sent == []


@pytest.fixture
def assistant_served(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve with scout configured."""
    patch_state(monkeypatch, "config", ASSISTANT_CONFIG)


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


# -- A message to one agent of a session, through whichever way it takes messages --

_NUMBER = 412
_SENDER_NUMBER = 415


class _Relay(SessionRelay):
    """The relay as the send route meets it: who serves each session, on which key."""

    def __init__(
        self,
        *,
        serving: dict[uuid.UUID, _State],
        keys: dict[uuid.UUID, uuid.UUID],
        gone: frozenset[uuid.UUID] = frozenset(),
        full: frozenset[uuid.UUID] = frozenset(),
    ) -> None:
        super().__init__()
        self.serving = serving
        self.keys = keys
        self.gone = gone
        self.full = full
        self.sent: list[tuple[uuid.UUID, JSON, str, str, MessageKind | None]] = []

    @override
    def process(self, session_id: uuid.UUID) -> AgentProcess | None:
        state = self.serving.get(session_id)
        return (
            None
            if state is None
            else AgentProcess(
                session_id=session_id,
                process_id=uuid.UUID(int=1),
                host="lab",
                state=state,
                since=datetime(2026, 11, 12, tzinfo=UTC),
            )
        )

    @override
    def socket_key(self, session_id: uuid.UUID) -> uuid.UUID | None:
        return self.keys.get(session_id)

    @override
    def send_input(
        self,
        session_id: uuid.UUID,
        new: NewInput,
        *,
        author: str,
        recipient: str = "",
        booked: MessageKind | None = None,
    ) -> uuid.UUID:
        if session_id in self.gone:
            raise NotLiveError(f"no agent process serves session {session_id}")
        if session_id in self.full:
            raise InputRefusedError("the agent holds all it may")
        self.sent.append((session_id, new.payload, author, recipient, booked))
        return new.id


def _numbered(seq: int, *, ended: datetime | None = None) -> AgentSession:
    """Return a session of the caller's account, numbered ``seq``, live unless ``ended``."""
    return replace(live_session(), account=TEST_USER_EMAIL, seq=seq, ended=ended)


def _held(
    monkeypatch: pytest.MonkeyPatch,
    store: Store,
    *sessions: Inquiry,
) -> None:
    """Have the store hold ``sessions`` alone, by id and by number."""
    by_id = {session.id: session for session in sessions}
    by_number = {
        session.seq: session.id
        for session in sessions
        if isinstance(session, AgentSession)
    }

    async def numbered(kind: str, seq: int) -> uuid.UUID | None:
        assert kind == "AgentSession"
        return by_number.get(seq)

    async def found(target_id: uuid.UUID | None) -> Inquiry | None:
        # The route reads a session by its id alone, never by no id.
        assert target_id is not None
        return by_id.get(target_id)

    monkeypatch.setattr(store, "get_inquiry", found)
    monkeypatch.setattr(store, "inquiry_id", numbered)


def _relayed(
    monkeypatch: pytest.MonkeyPatch,
    serving: dict[uuid.UUID, _State],
    *,
    keys: Mapping[uuid.UUID, uuid.UUID] = MappingProxyType({}),
    gone: frozenset[uuid.UUID] = frozenset(),
    full: frozenset[uuid.UUID] = frozenset(),
) -> _Relay:
    relay = _Relay(serving=serving, keys=dict(keys), gone=gone, full=full)
    patch_state(monkeypatch, "session_relay", relay)
    return relay


def _to(session: object, label: str = "lead", **sender: object) -> dict[str, object]:
    """Return a body that sends ``label`` of ``session`` the word ``status?``."""
    return {"to": {"label": label, "session": session}, "text": "status?", **sender}


def _delivered(response: httpx2.Response) -> list[object]:
    assert response.status_code == 200, response.text
    return from_plain(response.json(), dict[str, list[object]])["delivered"]


_STATUS: Final = {"py/object": "UserMessage", "content": "status?"}


@pytest.mark.parametrize(
    ("named", "state"),
    [("number", "attached"), ("uuid", "starting")],
)
def test_an_agent_of_a_session_is_sent_to_by_its_number_or_its_uuid(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
    named: str,
    state: _State,
) -> None:
    """A process still starting holds it (``session_relay_test`` proves the hold)."""
    client, store, _ = route_client
    lead = _numbered(_NUMBER)
    _held(monkeypatch, store, lead)
    relay = _relayed(monkeypatch, {lead.id: state})

    sent = client.post(
        "/api/messages",
        json=_to(_NUMBER if named == "number" else str(lead.id)),
    )

    assert _delivered(sent) == [str(lead.id)]
    assert relay.sent == [(lead.id, _STATUS, TEST_USER_EMAIL, "lead", None)]


@pytest.mark.parametrize(
    "named",
    [_NUMBER + 1, str(uuid.UUID(int=99)), "issue"],
    ids=["a number that is no session", "a uuid that is no session", "an issue"],
)
def test_a_target_that_is_no_session_is_404_and_sends_nothing(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
    named: object,
) -> None:
    client, store, _ = route_client
    issue = Issue(title="not a session")
    _held(monkeypatch, store, _numbered(_NUMBER), issue)
    relay = _relayed(monkeypatch, {})

    sent = client.post(
        "/api/messages",
        json=_to(str(issue.id) if named == "issue" else named),
    )

    assert sent.status_code == 404, sent.text
    assert json_loads(sent.content, dict[str, dict[str, str]])["detail"] == {
        "code": "no_session",
        "message": f"no session {issue.id if named == 'issue' else named}",
    }
    assert relay.sent == []


@pytest.mark.parametrize(
    ("ended", "state"),
    [(True, "attached"), (False, None), (False, "detached")],
    ids=["an ended session", "one nothing serves", "one whose process is gone"],
)
def test_a_session_that_takes_no_message_now_is_sent_none(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
    ended: bool,
    state: _State | None,
) -> None:
    client, store, _ = route_client
    lead = _numbered(_NUMBER, ended=datetime.now(UTC) if ended else None)
    _held(monkeypatch, store, lead)
    relay = _relayed(monkeypatch, {} if state is None else {lead.id: state})

    assert _delivered(client.post("/api/messages", json=_to(_NUMBER))) == []
    assert relay.sent == []


def test_a_process_that_went_since_it_was_found_is_sent_none(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store, _ = route_client
    lead = _numbered(_NUMBER)
    _held(monkeypatch, store, lead)
    _ = _relayed(monkeypatch, {lead.id: "starting"}, gone=frozenset({lead.id}))

    assert _delivered(client.post("/api/messages", json=_to(_NUMBER))) == []


def test_a_process_that_holds_all_the_inputs_it_may_is_sent_none(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store, _ = route_client
    lead = _numbered(_NUMBER)
    _held(monkeypatch, store, lead)
    relay = _relayed(monkeypatch, {lead.id: "attached"}, full=frozenset({lead.id}))

    assert _delivered(client.post("/api/messages", json=_to(_NUMBER))) == []
    assert relay.sent == []


def test_a_session_the_caller_may_not_post_to_refuses_the_whole_send(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store, _ = route_client
    theirs = replace(
        live_session(opened_by_api_key_id=uuid.uuid4()),
        account="bob@x.y",
        seq=_NUMBER,
    )
    _held(monkeypatch, store, theirs)
    relay = _relayed(monkeypatch, {theirs.id: "attached"})

    sent = client.post("/api/messages", json=_to(_NUMBER))

    assert sent.status_code == 403, sent.text
    assert relay.sent == []


def test_a_name_sends_to_the_root_of_each_session_an_agent_process_serves(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replay answers the first call's receipt and sends nothing again."""
    client, store, _ = route_client
    served, unserved = _numbered(_NUMBER), _numbered(_NUMBER + 1)
    _held(monkeypatch, store, served, unserved)
    resolved = AsyncMock(
        return_value=[(served.id, ("sear", "ops")), (unserved.id, ("sear",))],
    )
    monkeypatch.setattr(store, "resolve_live_sessions", resolved)
    relay = _relayed(monkeypatch, {served.id: "attached"})
    key = {"Idempotency-Key": str(uuid.uuid4())}
    body = {"actor": "scientist", "room": "sear", "text": "status?"}

    first = _delivered(client.post("/api/messages", json=body, headers=key))
    again = _delivered(client.post("/api/messages", json=body, headers=key))

    assert first == again == [str(served.id)]
    assert [(*call.args, call.kwargs["room"]) for call in resolved.await_args_list] == [
        ("scientist", "sear"),
    ] * 2
    assert relay.sent == [(served.id, _STATUS, TEST_USER_EMAIL, "", None)]


def test_a_session_whose_process_went_meanwhile_leaves_the_rest_sent(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store, _ = route_client
    gone, served = _numbered(_NUMBER), _numbered(_NUMBER + 1)
    _held(monkeypatch, store, gone, served)
    monkeypatch.setattr(
        store,
        "resolve_live_sessions",
        AsyncMock(return_value=[(gone.id, ()), (served.id, ())]),
    )
    relay = _relayed(
        monkeypatch,
        {gone.id: "attached", served.id: "attached"},
        gone=frozenset({gone.id}),
    )

    sent = client.post("/api/messages", json={"actor": "scientist", "text": "status?"})

    assert _delivered(sent) == [str(served.id)]
    assert [session_id for session_id, *_ in relay.sent] == [served.id]


def test_a_process_key_sends_as_its_own_session_and_two_on_one_key_as_theirs(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store, _ = route_client
    lead, worker, other = (
        _numbered(_NUMBER),
        _numbered(_SENDER_NUMBER),
        _numbered(_SENDER_NUMBER + 1),
    )
    _held(monkeypatch, store, lead, worker, other)
    relay = _relayed(
        monkeypatch,
        {lead.id: "attached"},
        keys={worker.id: TEST_API_KEY_ID, other.id: TEST_API_KEY_ID},
    )

    for session, label in ((worker, "Agent_0"), (other, "Agent")):
        sent = client.post(
            "/api/messages",
            json=_to(
                _NUMBER,
                from_session=str(session.id),
                from_label=label,
                booked="ask",
            ),
        )
        assert _delivered(sent) == [str(lead.id)]

    assert [(author, booked) for *_, author, _, booked in relay.sent] == [
        (f"Agent_0#{_SENDER_NUMBER}", "ask"),
        (f"Agent#{_SENDER_NUMBER + 1}", "ask"),
    ]


@pytest.mark.parametrize(
    ("caller", "holder"),
    [(TEST_API_KEY_ID, uuid.UUID(int=77)), (TEST_API_KEY_ID, None), (None, None)],
    ids=[
        "a session whose socket another key holds",
        "a session with no socket attached",
        "a browser, which holds none",
    ],
)
def test_a_sending_session_is_refused_unless_its_process_holds_the_calling_key(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
    caller: uuid.UUID | None,
    holder: uuid.UUID | None,
) -> None:
    client, store, _ = route_client
    lead, worker = _numbered(_NUMBER), _numbered(_SENDER_NUMBER)
    _held(monkeypatch, store, lead, worker)
    relay = _relayed(
        monkeypatch,
        {lead.id: "attached"},
        keys={} if holder is None else {worker.id: holder},
    )
    install_identity(make_test_identity(api_key_id=caller))

    sent = client.post(
        "/api/messages",
        json=_to(
            _NUMBER,
            from_session=str(worker.id),
            from_label="Agent",
            booked="ask",
        ),
    )

    assert sent.status_code == 403, sent.text
    assert json_loads(sent.content, dict[str, dict[str, str]])["detail"] == {
        "code": "not_its_process",
        "message": "this key holds the socket of no agent process of that session",
    }
    assert relay.sent == []


def test_a_process_key_that_names_no_session_of_its_own_sends_nothing(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """As its person it would put a line in any session of theirs, signed as theirs."""
    client, store, _ = route_client
    lead = _numbered(_NUMBER)
    _held(monkeypatch, store, lead)
    relay = _relayed(monkeypatch, {lead.id: "attached"})
    install_identity(
        replace(
            make_test_identity(),
            process=ProcessKey(
                process_id=uuid.UUID(int=5),
                session_id=uuid.UUID(int=6),
                host_id=uuid.UUID(int=7),
            ),
        ),
    )

    sent = client.post("/api/messages", json=_to(_NUMBER))

    assert sent.status_code == 403, sent.text
    assert json_loads(sent.content, dict[str, dict[str, str]])["detail"] == {
        "code": "not_its_process",
        "message": "an agent process's key sends as an agent of its session; name it",
    }
    assert relay.sent == []


@pytest.mark.parametrize(
    "named",
    [
        {"sender": "Agent#415"},
        {"source": "ada@x.y"},
        {"from_label": "Agent", "booked": "ask"},
    ],
    ids=["a sender", "a source", "a label with no session"],
)
def test_a_body_that_names_its_sender_is_refused_not_ignored(
    route_client: tuple[TestClient, Store, FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
    named: dict[str, str],
) -> None:
    client, store, _ = route_client
    _held(monkeypatch, store, _numbered(_NUMBER))
    relay = _relayed(monkeypatch, {})

    assert client.post("/api/messages", json=_to(_NUMBER, **named)).status_code == 422
    assert relay.sent == []


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_two_processes_on_one_key_reach_a_session_by_its_number_as_themselves(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real store numbers the sessions; the relay holds the key each socket opened."""
    client, store = pglite_route_client
    await seed_accounts(store)
    lead, first, second = [
        await seed_session(store, USER_AGENT, actor=actor, polled=False)
        for actor in ("lead", "first", "second")
    ]
    numbers = {
        session: (await session_row(store, session_id=session)).seq
        for session in (lead, first, second)
    }
    relay = SessionRelay()
    patch_state(monkeypatch, "session_relay", relay)
    sent = Mock(wraps=relay.send_input)
    monkeypatch.setattr(relay, "send_input", sent)
    act_as_user_agent("writer")

    async with (
        attached_process(relay, lead),
        attached_process(relay, first, key=TEST_API_KEY_ID),
        attached_process(relay, second, key=TEST_API_KEY_ID),
    ):
        answers = [
            await client.post(
                "/api/messages",
                json=_to(
                    numbers[lead],
                    from_session=str(sender),
                    from_label="Agent",
                    booked="ask",
                ),
            )
            for sender in (first, second)
        ]
        stranger = await client.post(
            "/api/messages",
            json=_to(
                numbers[lead],
                from_session=str(lead),
                from_label="Agent",
                booked="ask",
            ),
        )

    assert [_delivered(answer) for answer in answers] == [[str(lead)], [str(lead)]]
    assert stranger.status_code == 403, stranger.text
    assert [(call.args[0], call.kwargs["author"]) for call in sent.call_args_list] == [
        (lead, f"Agent#{numbers[first]}"),
        (lead, f"Agent#{numbers[second]}"),
    ]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
