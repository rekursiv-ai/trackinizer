"""Stations over the app: many canvases per user, each named, each with one chat."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import asyncio
import uuid

from treekle import from_plain, json_loads

import pytest

from trackinizer.server.api.app import app
from trackinizer.server.api.canvas_test_support import (
    ASSISTANT,
    ASSISTANT_CONFIG,
    KB_ACTOR,
    OTHER_EMAIL,
    OTHER_USER_ID,
    act_as_user_agent,
    browser,
    open_workspace,
    revision_of,
    seed_accounts,
    seed_session,
    send_chat,
)
from trackinizer.server.chat_hub import ChatHub
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.visuals.workspace_store import next_station_name
from trackinizer.server.visuals.workspaces import RenameWorkspace


if TYPE_CHECKING:
    import httpx2

    from trackinizer.server.store.core import Store


type _Client = tuple[httpx2.AsyncClient, Store]

FIRST: Final = "unnamed-workstream#1"


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> ChatHub:
    """Serve with scout configured, a fresh poller table and a fresh event hub."""
    hub = ChatHub()
    monkeypatch.setattr(app.state, "config", ASSISTANT_CONFIG, raising=False)
    monkeypatch.setattr(app.state, "inbound", InboundQueue(), raising=False)
    monkeypatch.setattr(app.state, "hub", hub, raising=False)
    return hub


async def _station(
    client: httpx2.AsyncClient,
    method: str,
    path: str,
    *,
    body: dict[str, str] | None = None,
) -> dict[str, object]:
    """Call a station route that answers with a station; return it."""
    response = await client.request(method, path, json=body)
    assert response.status_code == 200, response.text
    return json_loads(response.content, dict[str, object])


async def _listed(client: httpx2.AsyncClient) -> list[dict[str, object]]:
    """Read the station list."""
    response = await client.get("/api/workspaces")
    assert response.status_code == 200, response.text
    return from_plain(json_loads(response.content), list[dict[str, object]])


async def _names(client: httpx2.AsyncClient) -> list[str]:
    """Name the listed stations, the one changed last first."""
    return [from_plain(each["name"], str) for each in await _listed(client)]


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_racing_first_opens_make_one_station(
    pglite_route_client: _Client,
) -> None:
    """However many first requests race, the user has one station, named first."""
    client, store = pglite_route_client
    await seed_accounts(store)
    browser()

    answers = await asyncio.gather(*(client.post("/api/workspaces") for _ in range(4)))

    assert {a.status_code for a in answers} == {200}
    assert {json_loads(a.content, dict[str, object])["id"] for a in answers} == {
        each["id"] for each in await _listed(client)
    }
    assert await _names(client) == [FIRST]


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_the_app_opens_the_station_changed_last_and_names_count_up(
    pglite_route_client: _Client,
) -> None:
    """A new station, or a page stored, moves it to the front; names count up."""
    client, store = pglite_route_client
    await seed_accounts(store)
    browser()
    first = await _station(client, "POST", "/api/workspaces")
    second = await _station(client, "POST", "/api/workspaces/new")
    third = await _station(client, "POST", "/api/workspaces/new")
    assert [first["name"], second["name"], third["name"]] == [
        FIRST,
        "unnamed-workstream#2",
        "unnamed-workstream#3",
    ]
    assert (await _station(client, "POST", "/api/workspaces"))["id"] == third["id"]

    paged = await client.put(
        f"/api/workspaces/{first['id']}/page",
        json={"route": "#/graph"},
    )

    assert paged.status_code == 204
    assert (await _station(client, "POST", "/api/workspaces"))["id"] == first["id"]
    assert await _names(client) == [
        FIRST,
        "unnamed-workstream#3",
        "unnamed-workstream#2",
    ]
    renamed = await client.patch(
        f"/api/workspaces/{second['id']}",
        json={"name": "tails"},
    )
    assert renamed.status_code == 200
    assert (await _station(client, "POST", "/api/workspaces/new"))["name"] == (
        "unnamed-workstream#2"
    )


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_list_holds_only_the_callers_stations_with_their_fields(
    pglite_route_client: _Client,
) -> None:
    """Each summary names its chat and page; another user's stations are not listed."""
    client, store = pglite_route_client
    await seed_accounts(store)
    _ = await open_workspace(client, user_id=OTHER_USER_ID, email=OTHER_EMAIL)
    browser()
    made = await _station(client, "POST", "/api/workspaces")

    [only] = await _listed(client)

    assert set(only) == {
        "id",
        "name",
        "conversation_id",
        "page",
        "created_at",
    }
    assert (only["id"], only["conversation_id"], only["page"]) == (
        made["id"],
        None,
        None,
    )


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_rename_is_trimmed_bounded_unique_and_the_owners_alone(
    pglite_route_client: _Client,
) -> None:
    """Blank and long names are 422, a taken one 409, a foreign station 404."""
    client, store = pglite_route_client
    await seed_accounts(store)
    other_station = await open_workspace(
        client,
        user_id=OTHER_USER_ID,
        email=OTHER_EMAIL,
    )
    browser()
    first = await _station(client, "POST", "/api/workspaces")
    second = await _station(client, "POST", "/api/workspaces/new")

    trimmed = await _station(
        client,
        "PATCH",
        f"/api/workspaces/{first['id']}",
        body={"name": "  tails  "},
    )

    assert trimmed["name"] == "tails"

    async def rename(workspace: object, name: str) -> int:
        patched = await client.patch(
            f"/api/workspaces/{workspace}",
            json={"name": name},
        )
        return patched.status_code

    assert await rename(first["id"], "   ") == 422
    assert await rename(first["id"], "x" * 121) == 422
    assert await rename(first["id"], "x" * 120) == 200
    assert await rename(second["id"], "x" * 120) == 409
    assert await rename(first["id"], "x" * 120) == 200
    assert await rename(other_station, "tails") == 404
    browser(OTHER_USER_ID, email=OTHER_EMAIL)
    assert await rename(other_station, "x" * 120) == 200


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_the_page_is_remembered_with_no_revision_and_no_frame(
    pglite_route_client: _Client,
    served: ChatHub,
) -> None:
    """A page is a hash route; storing it changes nothing a viewer follows."""
    client, store = pglite_route_client
    await seed_accounts(store)
    other_station = await open_workspace(
        client,
        user_id=OTHER_USER_ID,
        email=OTHER_EMAIL,
    )
    browser()
    station = await _station(client, "POST", "/api/workspaces")
    workspace_id = uuid.UUID(from_plain(station["id"], str))
    revision = await revision_of(client, workspace_id=workspace_id)
    path = f"/api/workspaces/{workspace_id}/page"

    with served.subscribe(workspace_id) as stream:
        stored = await client.put(path, json={"route": "#/issue/7"})
        assert stream.queue.empty()

    assert stored.status_code == 204
    state = json_loads(
        (await client.get(f"/api/workspaces/{workspace_id}")).content,
        dict[str, object],
    )
    assert (state["page"], state["revision"]) == ("#/issue/7", revision)
    assert (await _listed(client))[0]["page"] == "#/issue/7"
    assert (await client.put(path, json={"route": "issue/7"})).status_code == 422
    assert (await client.put(path, json={"route": "#/a b"})).status_code == 422
    assert (await client.put(path, json={"route": "#/" + "a" * 511})).status_code == 422
    foreign = f"/api/workspaces/{other_station}/page"
    assert (await client.put(foreign, json={"route": "#/x"})).status_code == 404


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_the_layout_merges_by_key_with_no_revision_and_no_frame(
    pglite_route_client: _Client,
    served: ChatHub,
) -> None:
    """A write replaces the keys it names, keeps the rest, and races no operation."""
    client, store = pglite_route_client
    await seed_accounts(store)
    other_station = await open_workspace(
        client,
        user_id=OTHER_USER_ID,
        email=OTHER_EMAIL,
    )
    browser()
    station = await _station(client, "POST", "/api/workspaces")
    assert "layout" not in station or station["layout"] is None
    workspace_id = uuid.UUID(from_plain(station["id"], str))
    revision = await revision_of(client, workspace_id=workspace_id)
    path = f"/api/workspaces/{workspace_id}/layout"
    tile = {"collapsed": True, "place": {"left": 0.25, "top": 0.5}}

    with served.subscribe(workspace_id) as stream:
        first = await client.put(
            path,
            json={
                "columns": {"side": 0.3},
                "chat_ch": 80,
                "tiles": {"trax.browse": tile},
            },
        )
        second = await client.put(path, json={"columns": {"left": 0.2}})
        assert stream.queue.empty()

    assert (first.status_code, second.status_code) == (204, 204)
    state = json_loads(
        (await client.get(f"/api/workspaces/{workspace_id}")).content,
        dict[str, object],
    )
    assert state["revision"] == revision
    layout = from_plain(state["layout"], dict[str, object])
    assert {k: v for k, v in layout.items() if v is not None} == {
        "columns": {"left": 0.2, "side": None},
        "chat_ch": 80.0,
        "tiles": {"trax.browse": tile},
    }
    foreign = f"/api/workspaces/{other_station}/layout"
    assert (await client.put(foreign, json={"chat_ch": 80})).status_code == 404


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_layout_of_pixels_or_unknown_keys_is_refused(
    pglite_route_client: _Client,
) -> None:
    """Only fractions of the stage and Chat's width in ch are stored."""
    client, store = pglite_route_client
    await seed_accounts(store)
    browser()
    station = await _station(client, "POST", "/api/workspaces")
    path = f"/api/workspaces/{from_plain(station['id'], str)}/layout"

    refused = [
        {"columns": {"side": 420}},
        {"columns": {"side": 0}},
        {"chat_ch": 5},
        {"tiles": {"trax.browse": {"place": {"left": 1.5, "top": 0}}}},
        {"floating": {"trax.browse": {"width": 0, "height": 0.5}}},
        {"floating": {f"t{n}": {"width": 0.5, "height": 0.5} for n in range(65)}},
        {"pixels": 1},
    ]

    codes = [(await client.put(path, json=body)).status_code for body in refused]
    assert codes == [422] * len(refused)


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_for_chat_opens_the_station_that_holds_the_chat_or_makes_one(
    pglite_route_client: _Client,
) -> None:
    """A conversation has one station per user, found again however often asked."""
    client, store = pglite_route_client
    await seed_accounts(store)
    browser()
    conversation = uuid.uuid4()
    path = f"/api/workspaces/for-chat/{conversation}"

    made = await _station(client, "POST", path)

    assert (made["conversation_id"], made["name"]) == (str(conversation), FIRST)
    later = await _station(client, "POST", "/api/workspaces/new")
    assert (await _station(client, "POST", "/api/workspaces"))["id"] == later["id"]
    again = await _station(client, "POST", path)
    assert again["id"] == made["id"]
    # Finding it changes nothing, so the station changed last is still the newer one.
    assert (await _station(client, "POST", "/api/workspaces"))["id"] == later["id"]
    assert len(await _listed(client)) == 2
    browser(OTHER_USER_ID, email=OTHER_EMAIL)
    theirs = await _station(client, "POST", path)
    assert theirs["id"] != made["id"]


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_stations_first_line_binds_its_chat_and_no_other_is_taken(
    pglite_route_client: _Client,
) -> None:
    """The line binds its conversation; another conversation is 409, and so is a held one."""
    client, store = pglite_route_client
    await seed_accounts(store)
    _ = await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    workspace_id = await open_workspace(client)
    chat = uuid.uuid4()

    async def say(
        workspace: uuid.UUID,
        conversation: uuid.UUID | None,
    ) -> httpx2.Response:
        return await send_chat(
            client,
            workspace_id=workspace,
            text="hello",
            conversation_id=conversation,
        )

    bound = await say(workspace_id, chat)

    assert bound.status_code == 200
    assert json_loads(bound.content, dict[str, object])["workspace_id"] == str(
        workspace_id,
    )
    state = json_loads(
        (await client.get(f"/api/workspaces/{workspace_id}")).content,
        dict[str, object],
    )
    assert state["conversation_id"] == str(chat)
    elsewhere = await say(workspace_id, uuid.uuid4())
    assert elsewhere.status_code == 409
    assert "another conversation" in elsewhere.text
    continued = await say(workspace_id, None)
    assert continued.status_code == 200
    assert json_loads(continued.content, dict[str, object])["conversation_id"] == str(
        chat,
    )
    other_id = await open_workspace(client, new=True)
    taken = await say(other_id, chat)
    assert taken.status_code == 409
    assert "Another station" in taken.text
    unbound = json_loads(
        (await client.get(f"/api/workspaces/{other_id}")).content,
        dict[str, object],
    )
    assert unbound["conversation_id"] is None


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_refused_line_leaves_its_station_without_a_chat(
    pglite_route_client: _Client,
) -> None:
    """The binding commits with the queued line, so no assistant means no binding."""
    client, store = pglite_route_client
    await seed_accounts(store)
    workspace_id = await open_workspace(client)

    refused = await send_chat(
        client,
        workspace_id=workspace_id,
        text="anyone there?",
        conversation_id=uuid.uuid4(),
    )

    assert refused.status_code == 409
    state = json_loads(
        (await client.get(f"/api/workspaces/{workspace_id}")).content,
        dict[str, object],
    )
    assert state["conversation_id"] is None


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_station_routes_refuse_an_api_key(
    pglite_route_client: _Client,
) -> None:
    """Stations are the signed-in browser's own; a key is 403 on each route."""
    client, store = pglite_route_client
    await seed_accounts(store)
    station = await open_workspace(client)
    base = f"/api/workspaces/{station}"
    act_as_user_agent("writer")

    answers = [
        await client.post("/api/workspaces"),
        await client.get("/api/workspaces"),
        await client.post("/api/workspaces/new"),
        await client.post(f"/api/workspaces/for-chat/{uuid.uuid4()}"),
        await client.patch(base, json={"name": "mine"}),
        await client.put(f"{base}/page", json={"route": "#/x"}),
        await client.put(f"{base}/layout", json={"chat_ch": 80}),
    ]

    assert [a.status_code for a in answers] == [403] * 7


def test_a_new_station_takes_the_lowest_free_number() -> None:
    """Gaps are filled, other names are ignored, and the count starts at one."""
    assert next_station_name([]) == FIRST
    assert next_station_name([FIRST, "unnamed-workstream#3"]) == "unnamed-workstream#2"
    assert next_station_name(
        ["tails", "unnamed-workstream#x", "unnamed-workstream#0"],
    ) == (FIRST)
    assert next_station_name(["unnamed-workstream#1", "unnamed-workstream#2"]) == (
        "unnamed-workstream#3"
    )


def test_a_rename_is_trimmed_and_bounded() -> None:
    """The body holds a name of 1 to 120 characters once trimmed."""
    assert RenameWorkspace(name=" a ").name == "a"
    for bad in ("", "   ", "x" * 121):
        with pytest.raises(ValueError, match="1 to 120"):
            _ = RenameWorkspace(name=bad)


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
