"""A picture of the canvas: asked for by the partner key, taken by the owner's page."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import asyncio
import base64
import uuid

from treekle import from_plain, json_loads

import pytest

from trackinizer.server.api.app import app
from trackinizer.server.api.canvas_test_support import (
    ASSISTANT,
    ASSISTANT_CONFIG,
    KB_ACTOR,
    act_as_assistant,
    act_as_other_agent,
    act_as_user_agent,
    browser,
    open_workspace,
    seed_accounts,
    seed_converse,
    seed_session,
)
from trackinizer.server.capture_relay import (
    MAX_ANSWER_BYTES,
    CaptureAnswer,
    CaptureRelay,
)
from trackinizer.server.chat_hub import CaptureFrame, ChatHub
from trackinizer.server.inbound import InboundQueue


if TYPE_CHECKING:
    import httpx2

    from trackinizer.server.chat_hub import Subscription
    from trackinizer.server.store.core import Store


type _Client = tuple[httpx2.AsyncClient, Store]

_PNG: Final = base64.b64encode(b"\x89PNG\r\n\x1a\n-").decode()
_BOX: Final = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> ChatHub:
    """Serve with scout configured, a fresh event hub and a relay that waits briefly."""
    hub = ChatHub()
    monkeypatch.setattr(app.state, "config", ASSISTANT_CONFIG, raising=False)
    monkeypatch.setattr(app.state, "inbound", InboundQueue(), raising=False)
    monkeypatch.setattr(app.state, "hub", hub, raising=False)
    monkeypatch.setattr(
        app.state,
        "relay",
        CaptureRelay(timeout_sec=0.05),
        raising=False,
    )
    return hub


def _picture(*, mime: str = "image/png") -> dict[str, object]:
    return {
        "image": {
            "width": 40,
            "height": 30,
            "mime": mime,
            "data": _PNG,
            "not_captured": ["the graph"],
            "boxes": [{"id": str(_BOX), "author": "person"}],
            "tiles": [
                {"id": str(_BOX), "left": 0.0, "top": 0.1, "right": 0.5, "bottom": 1.0},
            ],
        },
    }


async def _canvas(client: httpx2.AsyncClient, *, store: Store) -> uuid.UUID:
    """Open a canvas whose chat the assistant holds, so its key is the partner."""
    await seed_accounts(store)
    await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    workspace_id = await open_workspace(client)
    _ = await seed_converse(client, store, workspace_id=workspace_id, text="hi")
    return workspace_id


async def _requested(stream: Subscription) -> uuid.UUID:
    """Wait for the capture frame the pages are sent, and return its request id."""
    frame = await asyncio.wait_for(stream.queue.get(), 1)
    assert isinstance(frame, CaptureFrame)
    return frame.request


def _ask(
    client: httpx2.AsyncClient,
    workspace_id: uuid.UUID,
) -> asyncio.Task[httpx2.Response]:
    """Ask as the partner key; the request waits for a page."""
    act_as_assistant()
    return asyncio.create_task(client.post(f"/api/workspaces/{workspace_id}/captures"))


async def _answer(
    client: httpx2.AsyncClient,
    workspace_id: uuid.UUID,
    request: uuid.UUID,
    *,
    body: dict[str, object] | bytes,
) -> httpx2.Response:
    """Answer as the signed-in owner."""
    browser()
    path = f"/api/workspaces/{workspace_id}/captures/{request}"
    if isinstance(body, bytes):
        return await client.put(path, content=body)
    return await client.put(path, json=body)


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_the_partner_gets_the_picture_the_owners_page_sends(
    pglite_route_client: _Client,
    served: ChatHub,
) -> None:
    client, store = pglite_route_client
    workspace_id = await _canvas(client, store=store)
    with served.subscribe(workspace_id) as stream:
        asked = _ask(client, workspace_id)
        request = await _requested(stream)
        assert stream.queue.empty()
        sent = await _answer(client, workspace_id, request, body=_picture())
        got = await asked

    assert sent.status_code == 204
    assert got.status_code == 200
    assert json_loads(got.content, dict[str, object]) == _picture()["image"]


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_only_the_canvass_partner_key_may_ask(
    pglite_route_client: _Client,
    served: ChatHub,
) -> None:
    client, store = pglite_route_client
    workspace_id = await _canvas(client, store=store)
    path = f"/api/workspaces/{workspace_id}/captures"
    with served.subscribe(workspace_id) as stream:
        browser()
        by_browser = await client.post(path)
        act_as_user_agent()
        by_owners_key = await client.post(path)
        act_as_other_agent()
        by_stranger = await client.post(path)
        assert stream.queue.empty()

    assert by_browser.status_code == 403
    assert by_owners_key.status_code == 403
    assert "Only the canvas's Chat partner" in by_owners_key.text
    assert by_stranger.status_code == 404


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_only_the_owners_browser_may_answer(
    pglite_route_client: _Client,
    served: ChatHub,
) -> None:
    client, store = pglite_route_client
    workspace_id = await _canvas(client, store=store)
    with served.subscribe(workspace_id) as stream:
        asked = _ask(client, workspace_id)
        request = await _requested(stream)
        path = f"/api/workspaces/{workspace_id}/captures/{request}"
        act_as_assistant()
        by_key = await client.put(path, json=_picture())
        browser(uuid.uuid4(), email="stranger@example.com")
        by_stranger = await client.put(path, json=_picture())
        by_owner = await _answer(client, workspace_id, request, body=_picture())
        _ = await asked

    assert by_key.status_code == 403
    assert by_stranger.status_code == 404
    assert by_owner.status_code == 204


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_the_first_answer_wins_and_a_later_one_is_refused(
    pglite_route_client: _Client,
    served: ChatHub,
) -> None:
    client, store = pglite_route_client
    workspace_id = await _canvas(client, store=store)
    with served.subscribe(workspace_id) as stream:
        asked = _ask(client, workspace_id)
        request = await _requested(stream)
        first = await _answer(client, workspace_id, request, body={"error": "first"})
        second = await _answer(client, workspace_id, request, body=_picture())
        got = await asked

    assert (first.status_code, second.status_code) == (204, 404)
    assert got.status_code == 424
    assert "first" in got.text


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_no_page_open_or_none_answering_is_a_424(
    pglite_route_client: _Client,
    served: ChatHub,
) -> None:
    client, store = pglite_route_client
    workspace_id = await _canvas(client, store=store)
    act_as_assistant()
    path = f"/api/workspaces/{workspace_id}/captures"
    none_open = await client.post(path)
    with served.subscribe(workspace_id) as stream:
        silent = await client.post(path)
        assert isinstance(stream.queue.get_nowait(), CaptureFrame)

    assert (none_open.status_code, silent.status_code) == (424, 424)
    assert "No open page answered" in none_open.text
    assert "No open page answered" in silent.text


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_an_answer_over_the_cap_is_refused_and_the_request_keeps_waiting(
    pglite_route_client: _Client,
    served: ChatHub,
) -> None:
    client, store = pglite_route_client
    workspace_id = await _canvas(client, store=store)
    with served.subscribe(workspace_id) as stream:
        asked = _ask(client, workspace_id)
        request = await _requested(stream)
        huge = await _answer(
            client,
            workspace_id,
            request,
            body=b" " * (MAX_ANSWER_BYTES + 1),
        )
        later = await _answer(client, workspace_id, request, body=_picture())
        got = await asked

    assert huge.status_code == 413
    assert later.status_code == 204
    assert got.status_code == 200


@pytest.mark.db_pglite
@pytest.mark.usefixtures("served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_malformed_answer_is_refused_without_echoing_the_picture(
    pglite_route_client: _Client,
    served: ChatHub,
) -> None:
    client, store = pglite_route_client
    workspace_id = await _canvas(client, store=store)
    bad = _picture(mime="image/jpeg")
    with served.subscribe(workspace_id) as stream:
        asked = _ask(client, workspace_id)
        request = await _requested(stream)
        refused = await _answer(client, workspace_id, request, body=bad)
        unknown = await _answer(client, workspace_id, uuid.uuid4(), body=_picture())
        _ = await _answer(client, workspace_id, request, body={"error": "done"})
        _ = await asked

    assert refused.status_code == 422
    assert _PNG not in refused.text
    assert unknown.status_code == 404


def test_the_answer_body_the_schema_declares_is_the_one_the_route_reads() -> None:
    """The web app's typed client is built from the declared body, stated by hand."""
    served: object = app.openapi()
    declared = from_plain(
        from_plain(served, dict[str, object])["paths"],
        dict[str, object],
    )
    put = from_plain(
        declared["/api/workspaces/{workspace_id}/captures/{capture_id}"],
        dict[str, object],
    )["put"]
    body = from_plain(
        from_plain(put, dict[str, object])["requestBody"],
        dict[str, object],
    )
    schema = from_plain(
        from_plain(
            from_plain(body["content"], dict[str, object])["application/json"],
            dict[str, object],
        )["schema"],
        dict[str, object],
    )
    properties = from_plain(schema["properties"], dict[str, object])

    assert set(properties) == set(CaptureAnswer.model_fields)
    assert properties["image"] == {"$ref": "#/components/schemas/CaptureImage"}


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
