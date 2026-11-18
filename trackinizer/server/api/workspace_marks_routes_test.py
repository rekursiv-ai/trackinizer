"""Marks on a canvas: drawn by a person or an agent, pushed, and heard with the next line."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import uuid

from treekle import from_plain, json_loads

import pytest

from trackinizer.server.api.app import app
from trackinizer.server.api.canvas_test_support import (
    ASSISTANT,
    ASSISTANT_CONFIG,
    KB_ACTOR,
    OTHER_EMAIL,
    act_as_assistant,
    browser,
    drain,
    open_workspace,
    revision_of,
    seed_accounts,
    seed_converse,
    seed_session,
    send_chat,
)
from trackinizer.server.api.conftest import TEST_USER_EMAIL
from trackinizer.server.chat_hub import ChatHub, Frame, WorkspaceFrame
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.primitives import insert_inquiry


if TYPE_CHECKING:
    import httpx2

    from trackinizer.server.chat_hub import Subscription
    from trackinizer.server.store.core import Store


type _Client = tuple[httpx2.AsyncClient, Store]

_BOX: Final = {"points": [{"x": 0.1, "y": 0.2}, {"x": 0.4, "y": 0.5}]}


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


async def _draw(
    client: httpx2.AsyncClient,
    *,
    workspace_id: uuid.UUID,
    mark: dict[str, object] | None = None,
    revision: int = 0,
    key: uuid.UUID | None = None,
) -> dict[str, object]:
    """Post one mark as whoever is installed; return the canvas it answers."""
    response = await client.post(
        f"/api/workspaces/{workspace_id}/operations",
        json={
            "revision": revision,
            "operation": {"kind": "mark", "mark": mark or _BOX},
        },
        headers={"Idempotency-Key": str(key or uuid.uuid4())},
    )
    assert response.status_code == 200, response.text
    return json_loads(response.content, dict[str, object])


async def _operate(
    client: httpx2.AsyncClient,
    *,
    workspace_id: uuid.UUID,
    operation: dict[str, object],
    key: uuid.UUID | None = None,
) -> dict[str, object]:
    """Post one operation as whoever is installed; return the canvas it answers."""
    response = await client.post(
        f"/api/workspaces/{workspace_id}/operations",
        json={"revision": 0, "operation": operation},
        headers={"Idempotency-Key": str(key or uuid.uuid4())},
    )
    assert response.status_code == 200, response.text
    return json_loads(response.content, dict[str, object])


def _marks_of(state: dict[str, object]) -> list[dict[str, object]]:
    """Return the boxes a canvas answer holds."""
    return from_plain(state["marks"], list[dict[str, object]])


async def _canvas(
    client: httpx2.AsyncClient,
    *,
    store: Store,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Open a canvas whose chat the assistant holds, so its agent key may draw."""
    await seed_accounts(store)
    await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    workspace_id = await open_workspace(client)
    session_id = await seed_converse(
        client,
        store,
        workspace_id=workspace_id,
        text="hi",
    )
    return workspace_id, session_id


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_person_and_an_agent_each_sign_their_marks_and_a_stale_revision_still_draws(
    pglite_route_client: _Client,
) -> None:
    """Marks commute, so a stale revision applies; the author is the caller's."""
    client, store = pglite_route_client
    workspace_id, _ = await _canvas(client, store=store)
    revision = await revision_of(client, workspace_id=workspace_id)

    browser()
    mine = await _draw(client, workspace_id=workspace_id)
    act_as_assistant()
    theirs = await _draw(client, workspace_id=workspace_id)

    assert from_plain(mine["revision"], int) == revision + 1
    marks = from_plain(theirs["marks"], list[dict[str, object]])
    assert [(m["author"], m["seq"]) for m in marks] == [
        (TEST_USER_EMAIL, 1),
        ("agent", 2),
    ]
    assert from_plain(theirs["revision"], int) == revision + 2


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_mark_is_pushed_to_open_tabs_once_and_a_replay_adds_nothing(
    pglite_route_client: _Client,
    served: ChatHub,
) -> None:
    """The workspace frame carries the mark; the same key again draws and pushes nothing."""
    client, store = pglite_route_client
    workspace_id, _ = await _canvas(client, store=store)
    key = uuid.uuid4()
    browser()

    with served.subscribe(workspace_id) as stream:
        first = await _draw(client, workspace_id=workspace_id, key=key)
        replay = await _draw(client, workspace_id=workspace_id, key=key)
        frames = _frames(stream)

    [frame] = frames
    assert isinstance(frame, WorkspaceFrame)
    stored = [
        (m["id"], m["seq"]) for m in from_plain(first["marks"], list[dict[str, object]])
    ]
    assert [(str(m.id), m.seq) for m in frame.state.marks] == stored
    assert [
        (m["id"], m["seq"])
        for m in from_plain(replay["marks"], list[dict[str, object]])
    ] == stored
    assert [seq for _, seq in stored] == [1]


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_person_and_the_agent_each_remove_the_others_box_and_a_gone_one_is_no_change(
    pglite_route_client: _Client,
) -> None:
    """Any box goes by id to whoever asks; removing it again changes nothing."""
    client, store = pglite_route_client
    workspace_id, _ = await _canvas(client, store=store)
    browser()
    mine = _marks_of(await _draw(client, workspace_id=workspace_id))[-1]["id"]
    act_as_assistant()
    state = await _draw(client, workspace_id=workspace_id)
    theirs = _marks_of(state)[-1]["id"]

    after_agent = await _operate(
        client,
        workspace_id=workspace_id,
        operation={"kind": "remove_mark", "id": mine},
    )
    browser()
    after_person = await _operate(
        client,
        workspace_id=workspace_id,
        operation={"kind": "remove_mark", "id": theirs},
    )
    again = await _operate(
        client,
        workspace_id=workspace_id,
        operation={"kind": "remove_mark", "id": theirs},
    )

    assert [m["id"] for m in _marks_of(after_agent)] == [theirs]
    assert _marks_of(after_person) == []
    assert _marks_of(again) == []


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_the_agent_draws_lists_and_clears_a_page_or_all_boxes(
    pglite_route_client: _Client,
) -> None:
    """Clear with a page takes that page's boxes, the person's too; without, every box."""
    client, store = pglite_route_client
    workspace_id, _ = await _canvas(client, store=store)
    browser()
    moved = await client.put(
        f"/api/workspaces/{workspace_id}/page",
        json={"route": "#/graph"},
    )
    assert moved.status_code == 204
    await _draw(client, workspace_id=workspace_id)
    act_as_assistant()
    drawn = await _draw(client, workspace_id=workspace_id)
    assert [m["author"] for m in _marks_of(drawn)] == [TEST_USER_EMAIL, "agent"]

    elsewhere = await _operate(
        client,
        workspace_id=workspace_id,
        operation={"kind": "clear_marks", "page": "#/elsewhere"},
    )
    on_page = await _operate(
        client,
        workspace_id=workspace_id,
        operation={"kind": "clear_marks", "page": "#/graph"},
    )
    await _draw(client, workspace_id=workspace_id)
    everything = await _operate(
        client,
        workspace_id=workspace_id,
        operation={"kind": "clear_marks"},
    )

    assert [m["id"] for m in _marks_of(elsewhere)] == [
        m["id"] for m in _marks_of(drawn)
    ]
    assert _marks_of(on_page) == []
    assert _marks_of(everything) == []


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_mark_belongs_to_the_page_the_station_was_on_when_it_was_drawn(
    pglite_route_client: _Client,
) -> None:
    """The mark keeps the station's page, whatever page it is on afterwards."""
    client, store = pglite_route_client
    workspace_id, _ = await _canvas(client, store=store)
    browser()
    for route in ("#/list/Issue", "#/graph"):
        moved = await client.put(
            f"/api/workspaces/{workspace_id}/page",
            json={"route": route},
        )
        assert moved.status_code == 204
        await _draw(client, workspace_id=workspace_id)

    state = json_loads(
        (await client.get(f"/api/workspaces/{workspace_id}")).content,
        dict[str, object],
    )
    pages = [m["page"] for m in from_plain(state["marks"], list[dict[str, object]])]
    assert pages == ["#/list/Issue", "#/graph"]


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_the_next_line_carries_the_marks_drawn_since_the_last_one_with_their_page(
    pglite_route_client: _Client,
) -> None:
    """A line names each new mark, its page and the record under it, and only once."""
    client, store = pglite_route_client
    workspace_id, session_id = await _canvas(client, store=store)
    experiment_id = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title) VALUES "
            "($1, 'Experiment', 4242, 'active', $2, 'Measured tails')",
            experiment_id,
            TEST_USER_EMAIL,
        )
    browser()
    moved = await client.put(
        f"/api/workspaces/{workspace_id}/page",
        json={"route": "#/list/Experiment"},
    )
    assert moved.status_code == 204
    covers = {"records": [str(experiment_id), str(uuid.uuid4())]}
    first_id = _marks_of(
        await _draw(
            client,
            workspace_id=workspace_id,
            mark={**_BOX, "covers": covers},
        ),
    )[-1]["id"]
    second_id = _marks_of(await _draw(client, workspace_id=workspace_id))[-1]["id"]

    await send_chat(client, workspace_id=workspace_id, text="what is this?")
    await send_chat(client, workspace_id=workspace_id, text="and now?")

    act_as_assistant()
    response = await drain(client, session_id=session_id)
    heard = from_plain(
        json_loads(response.content, dict[str, object])["messages"],
        list[dict[str, object]],
    )
    one, two = (
        from_plain(
            from_plain(line["context"], dict[str, object])["marks"],
            list[dict[str, object]],
        )
        for line in heard
    )
    assert [(m["id"], m["page"]) for m in one] == [
        (first_id, "#/list/Experiment"),
        (second_id, "#/list/Experiment"),
    ]
    assert [
        r["seq"] for r in from_plain(one[0]["records"], list[dict[str, object]])
    ] == [4242]
    assert two == []


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_box_keeps_the_tile_it_was_drawn_on(
    pglite_route_client: _Client,
) -> None:
    """The tile is stored with the box and read back with it; a loose box has none."""
    client, store = pglite_route_client
    workspace_id, _ = await _canvas(client, store=store)
    tile = str(uuid.uuid4())

    browser()
    _ = await _draw(client, workspace_id=workspace_id, mark={**_BOX, "tile": tile})
    state = await _draw(client, workspace_id=workspace_id)

    assert [m["tile"] for m in _marks_of(state)] == [tile, None]


async def _private_session(store: Store, *, account: str) -> uuid.UUID:
    """Insert a private session of ``account`` and return its id."""
    session_id = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await insert_inquiry(
            conn,
            session_id,
            "AgentSession",
            values={
                "title": f"private of {account}",
                "owner": account,
                "account": account,
                "labels": (),
                "subscribers": (),
                "agentsession_audience": "private",
                "agentsession_cli": "claude",
                "agentsession_cli_session_id": str(session_id),
                "agentsession_rooms": (),
            },
        )
    return session_id


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_mark_names_a_session_only_to_a_reader_of_its_transcript(
    pglite_route_client: _Client,
) -> None:
    """Another person's private session leaves no label and no record in the line."""
    client, store = pglite_route_client
    workspace_id, session_id = await _canvas(client, store=store)
    theirs = await _private_session(store, account=OTHER_EMAIL)
    mine = await _private_session(store, account=TEST_USER_EMAIL)
    browser()
    covers = {"records": [str(theirs), str(mine)]}

    drawn = await _draw(
        client,
        workspace_id=workspace_id,
        mark={**_BOX, "covers": covers},
    )
    read = json_loads(
        (await client.get(f"/api/workspaces/{workspace_id}")).content,
        dict[str, object],
    )
    await send_chat(client, workspace_id=workspace_id, text="what is this?")

    for state in (drawn, read):
        labels = from_plain(state["record_labels"], dict[str, str])
        assert list(labels) == [str(mine)]
    act_as_assistant()
    response = await drain(client, session_id=session_id)
    [heard] = from_plain(
        json_loads(response.content, dict[str, object])["messages"],
        list[dict[str, object]],
    )
    [sent] = from_plain(
        from_plain(heard["context"], dict[str, object])["marks"],
        list[dict[str, object]],
    )
    assert [r["id"] for r in from_plain(sent["records"], list[dict[str, object]])] == [
        str(mine),
    ]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
