"""The session routes on a science chat: its title, sends, drains and ends.

A split of ``sessions_routes_test`` that seeds chats through ``api/private``'s helpers,
which that file does not import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import uuid

from treekle import from_plain, json_loads

import pytest

from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import (
    TEST_API_KEY_ID,
    TEST_USER_EMAIL,
    refusal_detail_code,
    session_row,
)
from trackinizer.server.api.private.conftest import (
    canvas_with_chat,
    seed_helper_chat,
    serve_assistant,
    switch_canvas_to,
)
from trackinizer.server.api.session_test_support import (
    ASSISTANT,
    act_as_other_agent,
    act_as_user_agent,
    browser,
    drain,
    seed_accounts,
    seed_session,
)
from trackinizer.server.inbound import Inbound, InboundQueue
from trackinizer.types.inquiries import AgentSession
from trackinizer.wire.wire_science_chat import CHAT_TITLE, chat_session_id
from trackinizer.wire.wire_sessions import RUN_ENDING_REFUSALS


if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    import httpx2

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.private.chat_hub import ChatHub
    from trackinizer.server.store.core import Store


@pytest.fixture
def assistant_served(monkeypatch: pytest.MonkeyPatch) -> ChatHub:
    """Serve with scout configured, and a fresh poller table and event hub."""
    return serve_assistant(monkeypatch)


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
                    account=TEST_USER_EMAIL,
                    owner="scientist",
                    cli="assistant",
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

    def test_an_agent_key_cannot_send_to_another_accounts_chat(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, store, engine = route_client
        act_as_other_agent("writer")
        unpolled_id, unpolled = self._chat_at(
            store,
            monkeypatch,
            engine=engine,
            polled=False,
        )
        skipped = client.post("/api/messages", json={"actor": "scientist", "text": "x"})
        session_id, inbound = self._chat_at(
            store,
            monkeypatch,
            engine=engine,
            polled=True,
        )
        refused = client.post("/api/messages", json={"actor": "scientist", "text": "x"})

        for response in (skipped, refused):
            assert response.status_code == 403, response.text
            assert json_loads(response.content, dict[str, object]) == {
                "detail": {"code": "session_audience", "audience": "open"},
            }
        assert (unpolled.pending(unpolled_id), inbound.pending(session_id)) == (0, 0)


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
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_second_run_on_a_chat_with_a_live_run_is_refused(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """One run drains a chat at a time; the next may take it once the first is gone."""
    client, store = pglite_route_client
    await seed_accounts(store)
    _, chat = await canvas_with_chat(store, provider="claude")
    act_as_user_agent("writer")
    first, second = "a" * 32, "b" * 32

    async def drains(instance: str) -> tuple[int, str | None]:
        answered = await drain(client, session_id=chat, cli="claude", instance=instance)
        return answered.status_code, (
            None if answered.status_code == 200 else refusal_detail_code(answered)
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
@pytest.mark.parametrize("chosen", ["codex", "assistant"])
async def test_the_exit_of_a_run_a_switch_left_does_not_end_the_chat(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    chosen: str,
) -> None:
    """A switch stops the old run, whose exit must not end the session the chat uses."""
    client, store = pglite_route_client
    await seed_accounts(store)
    conversation, chat = await canvas_with_chat(store, provider="claude")
    act_as_user_agent("writer")
    assert (await drain(client, session_id=chat, cli="claude")).status_code == 200
    await switch_canvas_to(store, conversation_id=conversation, provider=chosen)
    inbound = cast("InboundQueue", app.state.inbound)
    _ = inbound.enqueue(chat, Inbound(text="pineapple"))

    left = await client.post(f"/api/sessions/{chat}/end", json={"cli": "claude"})

    assert left.status_code == 200, left.text
    assert (await session_row(store, session_id=chat)).ended is None
    assert inbound.pending(chat) == 1


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    ("cli", "said"),
    [("codex", "exited with status 1: ImportError: boom"), ("claude", None)],
)
async def test_a_run_that_ended_before_it_was_seen_says_why_in_its_chat(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    cli: str,
    said: str | None,
) -> None:
    """A run that ends before it asks for its lines was seen nowhere: its end says why.

    Chat waits for the run it started to go live, and says why it did not from the
    canvas's ``refused``. The end of a run a switch left says nothing: it was stopped.
    """
    client, store = pglite_route_client
    await seed_accounts(store)
    _, chat = await canvas_with_chat(store, provider="codex")
    act_as_user_agent("writer")

    ended = await client.post(
        f"/api/sessions/{chat}/end",
        json={"cli": cli, "failed": "exited with status 1: ImportError: boom"},
    )

    assert ended.status_code == 200, ended.text
    refusal = cast("InboundQueue", app.state.inbound).refusal(chat)
    assert (None if refusal is None else refusal.reason) == said


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
    titles: set[str] = set()
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
            json={"cli": "codex", "cli_session_id": chat, "title": "my title"},
        )
        assert started.status_code == 201, started.text
        later = await session_row(
            store,
            session_id=uuid.UUID(
                from_plain(json_loads(started.content, dict[str, object])["id"], str),
            ),
        )
        opened[audience] = str(later.audience)
        titles.add(later.title)

    assert opened == {"staff": "staff", "private": "private", "open": "open"}
    # Every key reads a chat's row, so no caller's title rides on it.
    assert titles == {CHAT_TITLE}


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_run_draining_a_chat_that_was_closed_is_told_the_session_ended(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Closing a chat ends its session; the run still polling it is turned away for good."""
    client, store = pglite_route_client
    await seed_accounts(store)
    _, chat = await canvas_with_chat(store, provider="claude")
    act_as_user_agent("writer")
    assert (await drain(client, session_id=chat, cli="claude")).status_code == 200

    ended = await client.post(f"/api/sessions/{chat}/end", json={"actor": "scientist"})
    assert ended.status_code == 200, ended.text
    turned_away = await drain(client, session_id=chat, cli="claude")

    assert (turned_away.status_code, refusal_detail_code(turned_away)) == (
        409,
        "session_ended",
    )
    assert "session_ended" in RUN_ENDING_REFUSALS
    assert not cast("InboundQueue", app.state.inbound).has_poller(chat)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
