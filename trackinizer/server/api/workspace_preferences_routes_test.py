"""The station's agent changes its owner's preferences; every tab of theirs is told."""

from __future__ import annotations

from typing import TYPE_CHECKING

import uuid

from treekle import from_plain, json_loads

import pytest

from trackinizer.server.api.app import app
from trackinizer.server.api.canvas_test_support import (
    ASSISTANT,
    ASSISTANT_CONFIG,
    KB_ACTOR,
    KB_USER_ID,
    act_as_assistant,
    browser,
    open_workspace,
    revision_of,
    seed_accounts,
    seed_converse,
    seed_session,
)
from trackinizer.server.api.conftest import TEST_USER_ID
from trackinizer.server.chat_hub import ChatHub, Frame, PreferencesFrame
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.preferences import ASSISTANT_KEYS, stored_preferences
from trackinizer.server.visuals import partners, workspace_store


if TYPE_CHECKING:
    import httpx2

    from trackinizer.server.chat_hub import Subscription
    from trackinizer.server.store.core import Store


type _Client = tuple[httpx2.AsyncClient, Store]

pytestmark = [
    pytest.mark.db_pglite,
    pytest.mark.asyncio(loop_scope="session"),
]

_DEFAULTS = {
    "theme": "dark",
    "skin": "default",
    "chat_side": "left",
    "highlight_mentions": True,
    "terminal_font_size": "13px",
    "home": "graph",
}


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> ChatHub:
    """Serve with scout configured, a fresh poller table and a fresh event hub."""
    hub = ChatHub()
    monkeypatch.setattr(app.state, "config", ASSISTANT_CONFIG, raising=False)
    monkeypatch.setattr(app.state, "inbound", InboundQueue(), raising=False)
    monkeypatch.setattr(app.state, "hub", hub, raising=False)
    return hub


def _frames(stream: Subscription) -> list[Frame | None]:
    """Take every frame queued for a subscriber."""
    frames: list[Frame | None] = []
    while not stream.queue.empty():
        frames.append(stream.queue.get_nowait())
    return frames


def _set(change: dict[str, object]) -> dict[str, object]:
    """Build the body of a preferences operation."""
    return {"revision": 0, "operation": {"kind": "preferences", "change": change}}


async def _paired_station(client: httpx2.AsyncClient, store: Store) -> uuid.UUID:
    """Open the owner's station with a chat the assistant answers, as a browser."""
    await seed_accounts(store)
    await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    workspace_id = await open_workspace(client)
    await seed_converse(client, store, workspace_id=workspace_id, text="hi")
    return workspace_id


async def _stored(store: Store, user_id: uuid.UUID) -> object:
    async with store.engine.acquire() as conn:
        return await conn.fetchval(
            "SELECT preferences FROM users WHERE id = $1",
            user_id,
        )


async def _second_station(client: httpx2.AsyncClient) -> uuid.UUID:
    browser()
    made = await client.post("/api/workspaces/new")
    assert made.status_code == 200, made.text
    return uuid.UUID(from_plain(json_loads(made.content, dict[str, object])["id"], str))


@pytest.mark.usefixtures("served")
async def test_only_an_agent_key_may_set_preferences_through_a_station(
    pglite_route_client: _Client,
) -> None:
    """A browser sets its user's own preferences with PATCH /api/me/preferences instead."""
    client, store = pglite_route_client
    workspace_id = await _paired_station(client, store)
    browser()

    refused = await client.post(
        f"/api/workspaces/{workspace_id}/operations",
        json=_set({"theme": "light"}),
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )

    assert refused.status_code == 422
    assert "Only an agent" in refused.text
    assert await _stored(store, TEST_USER_ID) is None


async def test_the_stations_agent_sets_its_owners_preferences_and_every_station_is_told(
    pglite_route_client: _Client,
    served: ChatHub,
) -> None:
    """The owner's change, not the agent's; one frame per station; a replay is silent."""
    client, store = pglite_route_client
    workspace_id = await _paired_station(client, store)
    other_station = await _second_station(client)
    revision = await revision_of(client, workspace_id=workspace_id)
    path = f"/api/workspaces/{workspace_id}/operations"
    key = str(uuid.uuid4())

    with (
        served.subscribe(workspace_id) as here,
        served.subscribe(other_station) as there,
    ):
        act_as_assistant()
        first = await client.post(
            path,
            json=_set({"theme": "light", "chat_side": "right"}),
            headers={"Idempotency-Key": key},
        )
        replay = await client.post(
            path,
            json=_set({"theme": "light", "chat_side": "right"}),
            headers={"Idempotency-Key": key},
        )
        told = (_frames(here), _frames(there))

    assert (first.status_code, replay.status_code) == (200, 200)
    state = json_loads(first.content, dict[str, object])
    preferences = from_plain(state["preferences"], dict[str, object])
    assert state["revision"] == revision
    assert preferences == {
        **_DEFAULTS,
        "theme": "light",
        "chat_side": "right",
    }
    assert "workstreams" not in preferences
    stored = await _stored(store, TEST_USER_ID)
    assert stored == {"theme": "light", "chat_side": "right"}
    for frames in told:
        [frame] = frames
        # The owner's browser is sent every key, its own data too; the agent, the UI's.
        assert isinstance(frame, PreferencesFrame)
        assert frame.preferences == stored_preferences(stored)
        assert frame.preferences.model_dump(include=set(ASSISTANT_KEYS)) == preferences
    # The assistant's own account is untouched: it works for the station's owner.
    assert await _stored(store, KB_USER_ID) is None
    browser()
    profile = await client.get("/api/me/profile")
    assert json_loads(profile.content, dict[str, object])["preferences"] == (
        stored_preferences(stored).model_dump(mode="json")
    )


@pytest.mark.usefixtures("served")
@pytest.mark.parametrize(
    ("change", "named"),
    [
        ({"colour": "red"}, "colour"),
        ({"workstreams": {"order": [], "pinned": [], "hidden": []}}, "workstreams"),
        ({"theme": "blue"}, "theme"),
        ({"theme": "light", "chat_side": "up"}, "chat_side"),
    ],
)
async def test_a_key_or_value_the_preferences_do_not_take_is_refused_and_nothing_is_written(
    pglite_route_client: _Client,
    change: dict[str, object],
    named: str,
) -> None:
    client, store = pglite_route_client
    workspace_id = await _paired_station(client, store)
    act_as_assistant()

    refused = await client.post(
        f"/api/workspaces/{workspace_id}/operations",
        json=_set(change),
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )

    assert refused.status_code == 422
    assert named in refused.text
    assert await _stored(store, TEST_USER_ID) is None


@pytest.mark.usefixtures("served")
async def test_the_agent_sees_and_sets_only_the_ui_preferences_it_is_allowed(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keys beyond ``ASSISTANT_KEYS`` (the user's own data) are neither shown nor set."""
    for module in (workspace_store, partners):
        monkeypatch.setattr(module, "ASSISTANT_KEYS", frozenset({"theme"}))
    client, store = pglite_route_client
    workspace_id = await _paired_station(client, store)
    await client.patch("/api/me/preferences", json={"chat_side": "right"})
    act_as_assistant()
    path = f"/api/workspaces/{workspace_id}/operations"

    refused = await client.post(
        path,
        json=_set({"theme": "light", "chat_side": "left"}),
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    allowed = await client.post(
        path,
        json=_set({"theme": "light"}),
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )

    assert refused.status_code == 422
    assert "may not set: chat_side" in refused.text
    assert allowed.status_code == 200, allowed.text
    assert json_loads(allowed.content, dict[str, object])["preferences"] == {
        "theme": "light",
    }
    assert await _stored(store, TEST_USER_ID) == {
        "theme": "light",
        "chat_side": "right",
    }


async def test_the_users_own_change_reaches_every_station_of_theirs(
    pglite_route_client: _Client,
    served: ChatHub,
) -> None:
    """Changed on one tab, PATCH /api/me/preferences tells the user's other tabs."""
    client, store = pglite_route_client
    workspace_id = await _paired_station(client, store)
    other_station = await _second_station(client)

    with (
        served.subscribe(workspace_id) as here,
        served.subscribe(other_station) as there,
    ):
        changed = await client.patch("/api/me/preferences", json={"chat_side": "right"})
        told = (_frames(here), _frames(there))

    assert changed.status_code == 200, changed.text
    for frames in told:
        [frame] = frames
        assert isinstance(frame, PreferencesFrame)
        assert frame.preferences.chat_side == "right"


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
