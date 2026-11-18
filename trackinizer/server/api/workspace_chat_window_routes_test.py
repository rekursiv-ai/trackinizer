"""An agent hides and shows the browser tab's Chat window, as an event."""

from __future__ import annotations

from typing import TYPE_CHECKING

import uuid

from treekle import json_loads

import pytest

from trackinizer.server.api.app import app
from trackinizer.server.api.canvas_test_support import (
    ASSISTANT,
    ASSISTANT_CONFIG,
    KB_ACTOR,
    act_as_assistant,
    browser,
    open_workspace,
    revision_of,
    seed_accounts,
    seed_converse,
    seed_session,
)
from trackinizer.server.chat_hub import ChatFrame, ChatHub, Frame
from trackinizer.server.inbound import InboundQueue


if TYPE_CHECKING:
    import httpx2

    from trackinizer.server.chat_hub import Subscription
    from trackinizer.server.store.core import Store


type _Client = tuple[httpx2.AsyncClient, Store]


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


def _chat(*, hidden: bool) -> dict[str, object]:
    """Build the body of a Chat window operation."""
    return {"revision": 0, "operation": {"kind": "chat", "hidden": hidden}}


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_only_an_agent_key_may_hide_chat(
    pglite_route_client: _Client,
) -> None:
    """A browser cookie is refused; the owner closes their own window themselves."""
    client, store = pglite_route_client
    await seed_accounts(store)
    await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    workspace_id = await open_workspace(client)
    await seed_converse(
        client,
        store,
        workspace_id=workspace_id,
        text="hi",
    )
    browser()

    refused = await client.post(
        f"/api/workspaces/{workspace_id}/operations",
        json=_chat(hidden=True),
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )

    assert refused.status_code == 422
    assert "Only an agent" in refused.text


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_hiding_chat_pushes_one_frame_and_changes_no_revision(
    pglite_route_client: _Client,
    served: ChatHub,
) -> None:
    """The frame carries the flag; a replay is silent and a late one returns current."""
    client, store = pglite_route_client
    await seed_accounts(store)
    await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    workspace_id = await open_workspace(client)
    await seed_converse(
        client,
        store,
        workspace_id=workspace_id,
        text="hi",
    )
    revision = await revision_of(client, workspace_id=workspace_id)
    path = f"/api/workspaces/{workspace_id}/operations"
    key = str(uuid.uuid4())

    with served.subscribe(workspace_id) as stream:
        act_as_assistant()
        first = await client.post(
            path,
            json=_chat(hidden=True),
            headers={"Idempotency-Key": key},
        )
        replay = await client.post(
            path,
            json=_chat(hidden=True),
            headers={"Idempotency-Key": key},
        )
        frames = _frames(stream)

    assert (first.status_code, replay.status_code) == (200, 200)
    assert json_loads(first.content, dict[str, object])["revision"] == revision
    [frame] = frames
    assert isinstance(frame, ChatFrame)
    assert set(frame.model_dump(mode="json")) == {"t", "type", "hidden"}
    assert (frame.type, frame.hidden) == ("chat", True)
    browser()
    assert await revision_of(client, workspace_id=workspace_id) == revision
    act_as_assistant()
    other = await client.post(
        path,
        json=_chat(hidden=False),
        headers={"Idempotency-Key": key},
    )
    assert other.status_code == 409


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
