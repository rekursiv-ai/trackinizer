"""Canvases over the app: many canvases per user, each named, each with one chat."""

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
    USER_AGENT,
    act_as_user_agent,
    browser,
    open_canvas,
    revision_of,
    seed_accounts,
    seed_session,
    send_chat,
)
from trackinizer.server.api.conftest import TEST_USER_ID
from trackinizer.server.asks_test_support import open_ask
from trackinizer.server.chat_hub import ChatHub
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.visuals.canvas_store import next_canvas_name
from trackinizer.server.visuals.canvases import UpdateCanvas
from trackinizer.wire.wire_canvas_keys import canvas_key
from trackinizer.wire.wire_science_chat import chat_session_id


if TYPE_CHECKING:
    import httpx2

    from trackinizer.server.store.core import Store


type _Client = tuple[httpx2.AsyncClient, Store]

FIRST: Final = "unnamed-canvas#1"


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> ChatHub:
    """Serve with scout configured, a fresh poller table and a fresh event hub."""
    hub = ChatHub()
    monkeypatch.setattr(app.state, "config", ASSISTANT_CONFIG, raising=False)
    monkeypatch.setattr(app.state, "inbound", InboundQueue(), raising=False)
    monkeypatch.setattr(app.state, "hub", hub, raising=False)
    return hub


async def _canvas(
    client: httpx2.AsyncClient,
    method: str,
    path: str,
    *,
    body: dict[str, str] | None = None,
) -> dict[str, object]:
    """Call a canvas route that answers with a canvas; return it."""
    response = await client.request(method, path, json=body)
    assert response.status_code == 200, response.text
    return json_loads(response.content, dict[str, object])


async def _listed(client: httpx2.AsyncClient) -> list[dict[str, object]]:
    """Read the canvas list."""
    response = await client.get("/api/canvases")
    assert response.status_code == 200, response.text
    return from_plain(json_loads(response.content), list[dict[str, object]])


async def _names(client: httpx2.AsyncClient) -> list[str]:
    """Name the listed canvases, the one changed last first."""
    return [from_plain(each["name"], str) for each in await _listed(client)]


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_racing_first_opens_make_one_canvas(
    pglite_route_client: _Client,
) -> None:
    """However many first requests race, the user has one canvas, named first."""
    client, store = pglite_route_client
    await seed_accounts(store)
    browser()

    answers = await asyncio.gather(*(client.post("/api/canvases") for _ in range(4)))

    assert {a.status_code for a in answers} == {200}
    assert {json_loads(a.content, dict[str, object])["id"] for a in answers} == {
        each["id"] for each in await _listed(client)
    }
    assert await _names(client) == [FIRST]


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_the_app_opens_the_canvas_changed_last_and_names_count_up(
    pglite_route_client: _Client,
) -> None:
    """A new canvas, or a page stored, moves it to the front; names count up."""
    client, store = pglite_route_client
    await seed_accounts(store)
    browser()
    first = await _canvas(client, "POST", "/api/canvases")
    second = await _canvas(client, "POST", "/api/canvases/new")
    third = await _canvas(client, "POST", "/api/canvases/new")
    assert [first["name"], second["name"], third["name"]] == [
        FIRST,
        "unnamed-canvas#2",
        "unnamed-canvas#3",
    ]
    assert (await _canvas(client, "POST", "/api/canvases"))["id"] == third["id"]

    paged = await client.put(
        f"/api/canvases/{first['id']}/page",
        json={"route": "#/graph"},
    )

    assert paged.status_code == 204
    assert (await _canvas(client, "POST", "/api/canvases"))["id"] == first["id"]
    assert await _names(client) == [
        FIRST,
        "unnamed-canvas#3",
        "unnamed-canvas#2",
    ]
    renamed = await client.patch(
        f"/api/canvases/{second['id']}",
        json={"name": "tails"},
    )
    assert renamed.status_code == 200
    assert (await _canvas(client, "POST", "/api/canvases/new"))["name"] == (
        "unnamed-canvas#2"
    )


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_list_holds_only_the_callers_canvases_with_their_fields(
    pglite_route_client: _Client,
) -> None:
    """Each summary names its chat and page; another user's canvases are not listed."""
    client, store = pglite_route_client
    await seed_accounts(store)
    _ = await open_canvas(client, user_id=OTHER_USER_ID, email=OTHER_EMAIL)
    browser()
    made = await _canvas(client, "POST", "/api/canvases")

    [only] = await _listed(client)

    assert set(only) == {
        "id",
        "name",
        "conversation_id",
        "page",
        "created_at",
        "modified_at",
        "hidden",
        "pinned",
        "workspace_id",
        "suggestion",
        "tabs",
        "tab",
        "state",
    }
    assert (only["id"], only["conversation_id"], only["page"]) == (
        made["id"],
        None,
        None,
    )
    assert (only["hidden"], only["pinned"]) == (False, False)


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_key_finds_its_canvas_for_anyone_and_refuses_a_key_of_two(
    pglite_route_client: _Client,
) -> None:
    """Another person's canvas is found by its key; none is 404, two are 409."""
    client, store = pglite_route_client
    await seed_accounts(store)
    theirs = await open_canvas(client, user_id=OTHER_USER_ID, email=OTHER_EMAIL)
    browser()
    key = canvas_key(theirs)

    found = await client.get(f"/api/canvases/key/{key}")

    assert found.status_code == 200, found.text
    assert json_loads(found.content, dict[str, object]) == {"id": str(theirs)}
    assert (await client.get("/api/canvases/key/000000000000")).status_code == 404
    assert (await client.get(f"/api/canvases/key/{theirs}")).status_code == 422
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO visual_canvases (id, user_id, state) VALUES ($1, $2, '{}')",
            uuid.UUID(key + "f" * 20),
            TEST_USER_ID,
        )
    assert (await client.get(f"/api/canvases/key/{key}")).status_code == 409


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_rename_is_trimmed_bounded_unique_and_the_owners_alone(
    pglite_route_client: _Client,
) -> None:
    """Blank and long names are 422, a taken one 409, a foreign canvas 404."""
    client, store = pglite_route_client
    await seed_accounts(store)
    other_canvas = await open_canvas(
        client,
        user_id=OTHER_USER_ID,
        email=OTHER_EMAIL,
    )
    browser()
    first = await _canvas(client, "POST", "/api/canvases")
    second = await _canvas(client, "POST", "/api/canvases/new")

    trimmed = await _canvas(
        client,
        "PATCH",
        f"/api/canvases/{first['id']}",
        body={"name": "  tails  "},
    )

    assert trimmed["name"] == "tails"

    async def rename(canvas: object, name: str) -> int:
        patched = await client.patch(
            f"/api/canvases/{canvas}",
            json={"name": name},
        )
        return patched.status_code

    assert await rename(first["id"], "   ") == 422
    assert await rename(first["id"], "x" * 121) == 422
    assert await rename(first["id"], "x" * 120) == 200
    assert await rename(second["id"], "x" * 120) == 409
    assert await rename(first["id"], "x" * 120) == 200
    assert await rename(other_canvas, "tails") == 404
    browser(OTHER_USER_ID, email=OTHER_EMAIL)
    assert await rename(other_canvas, "x" * 120) == 200


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
    other_canvas = await open_canvas(
        client,
        user_id=OTHER_USER_ID,
        email=OTHER_EMAIL,
    )
    browser()
    canvas = await _canvas(client, "POST", "/api/canvases")
    canvas_id = uuid.UUID(from_plain(canvas["id"], str))
    revision = await revision_of(client, canvas_id=canvas_id)
    path = f"/api/canvases/{canvas_id}/page"

    with served.subscribe(canvas_id) as stream:
        stored = await client.put(path, json={"route": "#/issue/7"})
        assert stream.queue.empty()

    assert stored.status_code == 204
    state = json_loads(
        (await client.get(f"/api/canvases/{canvas_id}")).content,
        dict[str, object],
    )
    assert (state["page"], state["revision"]) == ("#/issue/7", revision)
    assert (await _listed(client))[0]["page"] == "#/issue/7"
    assert (await client.put(path, json={"route": "issue/7"})).status_code == 422
    assert (await client.put(path, json={"route": "#/a b"})).status_code == 422
    assert (await client.put(path, json={"route": "#/" + "a" * 511})).status_code == 422
    foreign = f"/api/canvases/{other_canvas}/page"
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
    other_canvas = await open_canvas(
        client,
        user_id=OTHER_USER_ID,
        email=OTHER_EMAIL,
    )
    browser()
    canvas = await _canvas(client, "POST", "/api/canvases")
    assert "layout" not in canvas or canvas["layout"] is None
    canvas_id = uuid.UUID(from_plain(canvas["id"], str))
    revision = await revision_of(client, canvas_id=canvas_id)
    path = f"/api/canvases/{canvas_id}/layout"
    tile = {"collapsed": True, "place": {"left": 0.25, "top": 0.5}}

    with served.subscribe(canvas_id) as stream:
        first = await client.put(
            path,
            json={
                "columns": {"side": 0.3},
                "chat_ch": 80,
                "tiles": {"trax.browse": tile},
            },
        )
        second = await client.put(path, json={"columns": {"left": 0.2}})
        tabs = await client.put(
            path,
            json={
                "tabs": [{"id": "a", "name": "Tab 1", "page": "#/graph"}],
                "tab": "a",
            },
        )
        assert stream.queue.empty()

    assert [r.status_code for r in (first, second, tabs)] == [204] * 3
    state = json_loads(
        (await client.get(f"/api/canvases/{canvas_id}")).content,
        dict[str, object],
    )
    assert state["revision"] == revision
    layout = from_plain(state["layout"], dict[str, object])
    assert {k: v for k, v in layout.items() if v is not None} == {
        "columns": {"left": 0.2, "side": None},
        "chat_ch": 80.0,
        "tiles": {"trax.browse": tile},
        "tabs": [
            {
                "id": "a",
                "name": "Tab 1",
                "page": "#/graph",
                "visuals": [],
                "columns": None,
                "floating": None,
                "tiles": None,
                "share": None,
            },
        ],
        "tab": "a",
    }
    foreign = f"/api/canvases/{other_canvas}/layout"
    assert (await client.put(foreign, json={"chat_ch": 80})).status_code == 404


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_the_list_names_each_canvases_tabs_and_the_open_one(
    pglite_route_client: _Client,
) -> None:
    """The top bar shows every canvas's tabs without reading each canvas."""
    client, store = pglite_route_client
    await seed_accounts(store)
    browser()
    plain = await _canvas(client, "POST", "/api/canvases")
    tabbed = await _canvas(client, "POST", "/api/canvases/new")
    response = await client.put(
        f"/api/canvases/{from_plain(tabbed['id'], str)}/layout",
        json={
            "tabs": [
                {"id": "a", "name": "Graph", "page": "#/graph"},
                {"id": "b", "name": "Issue#7", "page": "#/issues/7"},
            ],
            "tab": "b",
        },
    )
    assert response.status_code == 204, response.text

    listed = {from_plain(each["id"], str): each for each in await _listed(client)}

    assert listed[from_plain(tabbed["id"], str)]["tabs"] == [
        {"id": "a", "name": "Graph"},
        {"id": "b", "name": "Issue#7"},
    ]
    assert listed[from_plain(tabbed["id"], str)]["tab"] == "b"
    assert listed[from_plain(plain["id"], str)]["tabs"] == []
    assert listed[from_plain(plain["id"], str)]["tab"] is None


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
    canvas = await _canvas(client, "POST", "/api/canvases")
    path = f"/api/canvases/{from_plain(canvas['id'], str)}/layout"

    refused = [
        {"columns": {"side": 420}},
        {"columns": {"side": 0}},
        {"chat_ch": 5},
        {"tiles": {"trax.browse": {"place": {"left": 1.5, "top": 0}}}},
        {"floating": {"trax.browse": {"width": 0, "height": 0.5}}},
        {"floating": {f"t{n}": {"width": 0.5, "height": 0.5} for n in range(65)}},
        {"tabs": [{"id": "a", "name": "One"}, {"id": "a", "name": "Two"}]},
        {"pixels": 1},
    ]

    codes = [(await client.put(path, json=body)).status_code for body in refused]
    assert codes == [422] * len(refused)


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_for_chat_opens_the_canvas_that_holds_the_chat_or_makes_one(
    pglite_route_client: _Client,
) -> None:
    """A conversation has one canvas per user, found again however often asked."""
    client, store = pglite_route_client
    await seed_accounts(store)
    browser()
    conversation = uuid.uuid4()
    path = f"/api/canvases/for-chat/{conversation}"

    made = await _canvas(client, "POST", path)

    assert (made["conversation_id"], made["name"]) == (str(conversation), FIRST)
    later = await _canvas(client, "POST", "/api/canvases/new")
    assert (await _canvas(client, "POST", "/api/canvases"))["id"] == later["id"]
    again = await _canvas(client, "POST", path)
    assert again["id"] == made["id"]
    # Finding it changes nothing, so the canvas changed last is still the newer one.
    assert (await _canvas(client, "POST", "/api/canvases"))["id"] == later["id"]
    assert len(await _listed(client)) == 2
    browser(OTHER_USER_ID, email=OTHER_EMAIL)
    theirs = await _canvas(client, "POST", path)
    assert theirs["id"] != made["id"]


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_canvases_first_line_binds_its_chat_and_no_other_is_taken(
    pglite_route_client: _Client,
) -> None:
    """The line binds its conversation; another conversation is 409, and so is a held one."""
    client, store = pglite_route_client
    await seed_accounts(store)
    _ = await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    canvas_id = await open_canvas(client)
    chat = uuid.uuid4()

    async def say(
        canvas: uuid.UUID,
        conversation: uuid.UUID | None,
    ) -> httpx2.Response:
        return await send_chat(
            client,
            canvas_id=canvas,
            text="hello",
            conversation_id=conversation,
        )

    bound = await say(canvas_id, chat)

    assert bound.status_code == 200
    assert json_loads(bound.content, dict[str, object])["canvas_id"] == str(
        canvas_id,
    )
    state = json_loads(
        (await client.get(f"/api/canvases/{canvas_id}")).content,
        dict[str, object],
    )
    assert state["conversation_id"] == str(chat)
    elsewhere = await say(canvas_id, uuid.uuid4())
    assert elsewhere.status_code == 409
    assert "another conversation" in elsewhere.text
    continued = await say(canvas_id, None)
    assert continued.status_code == 200
    assert json_loads(continued.content, dict[str, object])["conversation_id"] == str(
        chat,
    )
    other_id = await open_canvas(client, new=True)
    taken = await say(other_id, chat)
    assert taken.status_code == 409
    assert "Another canvas" in taken.text
    unbound = json_loads(
        (await client.get(f"/api/canvases/{other_id}")).content,
        dict[str, object],
    )
    assert unbound["conversation_id"] is None


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_refused_line_leaves_its_canvas_without_a_chat(
    pglite_route_client: _Client,
) -> None:
    """The binding commits with the queued line, so no assistant means no binding."""
    client, store = pglite_route_client
    await seed_accounts(store)
    canvas_id = await open_canvas(client)

    refused = await send_chat(
        client,
        canvas_id=canvas_id,
        text="anyone there?",
        conversation_id=uuid.uuid4(),
    )

    assert refused.status_code == 409
    state = json_loads(
        (await client.get(f"/api/canvases/{canvas_id}")).content,
        dict[str, object],
    )
    assert state["conversation_id"] is None


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_canvas_routes_refuse_an_api_key(
    pglite_route_client: _Client,
) -> None:
    """Canvases are the signed-in browser's own; a key is 403 on each route."""
    client, store = pglite_route_client
    await seed_accounts(store)
    canvas = await open_canvas(client)
    base = f"/api/canvases/{canvas}"
    act_as_user_agent("writer")

    answers = [
        await client.post("/api/canvases"),
        await client.get("/api/canvases"),
        await client.post("/api/canvases/new"),
        await client.post(f"/api/canvases/for-chat/{uuid.uuid4()}"),
        await client.patch(base, json={"name": "mine"}),
        await client.put(f"{base}/page", json={"route": "#/x"}),
        await client.put(f"{base}/layout", json={"chat_ch": 80}),
    ]

    assert [a.status_code for a in answers] == [403] * 7


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_the_canvas_list_says_which_chat_waits_on_its_person(
    pglite_route_client: _Client,
) -> None:
    """A tab's badge: an ask open, or the run taken over; idle where nothing runs."""
    client, store = pglite_route_client
    await seed_accounts(store)
    waiting = await open_canvas(client)
    quiet = await open_canvas(client, new=True)
    conversation = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE visual_canvases SET state = state || "
            "jsonb_build_object('provider', 'claude'), metadata = metadata || "
            "jsonb_build_object('conversation_id', $2::text) WHERE id = $1",
            waiting,
            str(conversation),
        )
    chat = await seed_session(
        store,
        USER_AGENT,
        actor="chat-1",
        cli="claude",
        cli_session_id=chat_session_id(conversation),
        polled=False,
    )
    inbound = app.state.inbound
    assert isinstance(inbound, InboundQueue)
    run = "a" * 32
    inbound.mark_poller(chat, cli="claude", instance=run)
    browser()

    async def states() -> dict[str, object]:
        return {
            from_plain(each["id"], str): each["state"] for each in await _listed(client)
        }

    assert await states() == {str(waiting): "idle", str(quiet): "idle"}
    await open_ask(store, chat, ask_id="a1")
    assert await states() == {str(waiting): "requires_action", str(quiet): "idle"}
    assert inbound.report_state(chat, instance=run, state="taken_over")
    assert await states() == {str(waiting): "taken_over", str(quiet): "idle"}
    inbound.forget_poller(chat)
    assert await states() == {str(waiting): "idle", str(quiet): "idle"}


def test_a_new_canvas_takes_the_lowest_free_number() -> None:
    """Gaps are filled, other names are ignored, and the count starts at one."""
    assert next_canvas_name([]) == FIRST
    assert next_canvas_name([FIRST, "unnamed-canvas#3"]) == "unnamed-canvas#2"
    assert next_canvas_name(
        ["tails", "unnamed-canvas#x", "unnamed-canvas#0"],
    ) == (FIRST)
    assert next_canvas_name(["unnamed-canvas#1", "unnamed-canvas#2"]) == (
        "unnamed-canvas#3"
    )


def test_a_rename_is_trimmed_and_bounded() -> None:
    """The body holds a name of 1 to 120 characters once trimmed."""
    assert UpdateCanvas(name=" a ").name == "a"
    assert UpdateCanvas(hidden=False).hidden is False
    with pytest.raises(ValueError, match="At least one"):
        _ = UpdateCanvas()
    for bad in ("", "   ", "x" * 121):
        with pytest.raises(ValueError, match="1 to 120"):
            _ = UpdateCanvas(name=bad)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
