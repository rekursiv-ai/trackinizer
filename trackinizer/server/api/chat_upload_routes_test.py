"""Chat uploads over the whole app: who may ask for links, and the assistant's two legs."""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio
import uuid

from treekle import from_plain, json_loads

import pytest

from trackinizer.server.api.canvas_test_support import (
    ASSISTANT_CONFIG,
    KB_ACTOR,
    OTHER_EMAIL,
    OTHER_USER_ID,
    act_as_assistant,
    act_as_user_agent,
    browser,
    drain,
    open_canvas,
    open_science_chat,
    seed_accounts,
    start_session,
)
from trackinizer.server.api.conftest import (
    TEST_USER_EMAIL,
    TEST_USER_ID,
    install_identity,
    make_test_identity,
    patch_state,
)
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.upload_relay import UploadRelay
from trackinizer.server.visuals.workspaces import open_default_workspace
from trackinizer.wire.wire_chat_uploads import (
    CHAT_FILE_LINK_PATH,
    CHAT_FILE_REMOVE_PATH,
    CHAT_UPLOADS_PATH,
    CHAT_UPLOADS_WAIT_PATH,
    ChatUploadFile,
    ChatUploadSlot,
    UploadAnswer,
    UploadAsk,
)


if TYPE_CHECKING:
    import httpx2

    from trackinizer.server.store.core import Store


type _Client = tuple[httpx2.AsyncClient, Store]

_FILES = [{"name": "shot.png", "size": 4}, {"name": "notes.md", "size": 9}]


def _slot(name: str) -> ChatUploadSlot:
    return ChatUploadSlot(
        name=name,
        path=f"/home/user/uploads/{name}",
        url=f"https://box.invalid/files?path=/home/user/uploads/{name}",
    )


@pytest.fixture(autouse=True)
def uploads_served(monkeypatch: pytest.MonkeyPatch) -> UploadRelay:
    """Serve with the assistant configured, a fresh queue, and a relay of our own."""
    relay = UploadRelay(timeout_sec=5)
    patch_state(monkeypatch, "config", ASSISTANT_CONFIG)
    patch_state(monkeypatch, "inbound", InboundQueue())
    patch_state(monkeypatch, "uploads", relay)
    return relay


async def _ask(
    client: httpx2.AsyncClient,
    *,
    canvas_id: uuid.UUID,
    conversation_id: uuid.UUID | None = None,
    files: list[dict[str, object]] | None = None,
) -> httpx2.Response:
    return await client.post(
        CHAT_UPLOADS_PATH,
        json={
            "canvas_id": str(canvas_id),
            "conversation_id": None
            if conversation_id is None
            else str(conversation_id),
            "files": _FILES if files is None else files,
        },
    )


async def _answered(
    client: httpx2.AsyncClient,
    relay: UploadRelay,
    *,
    canvas_id: uuid.UUID,
    answer: UploadAnswer | None = None,
    conversation_id: uuid.UUID | None = None,
) -> tuple[httpx2.Response, UploadAsk]:
    """Ask as the page while the assistant takes the ask from the relay and answers."""
    page = asyncio.create_task(
        _ask(client, canvas_id=canvas_id, conversation_id=conversation_id),
    )
    taken = await relay.wait(wait_sec=5)
    assert taken is not None
    assert relay.answer(
        taken.request_id,
        answer or UploadAnswer(files=[_slot(each.name) for each in taken.files]),
    )
    return await page, taken


def _code(response: httpx2.Response) -> object:
    detail = json_loads(response.content, dict[str, object])["detail"]
    return from_plain(detail, dict[str, object]).get("code")


async def _cli_canvas(
    store: Store,
    *,
    provider: str,
    workspace_id: uuid.UUID | None = None,
) -> uuid.UUID:
    metadata: dict[str, object] = {"name": f"on-{provider}"}
    if workspace_id is not None:
        metadata["workspace_id"] = str(workspace_id)
    async with store.engine.acquire() as conn:
        found = await conn.fetchval(
            "INSERT INTO visual_canvases (id, user_id, metadata, state) "
            "VALUES (gen_random_uuid(), $1, $2, $3) RETURNING id",
            TEST_USER_ID,
            metadata,
            {"visuals": [], "provider": provider},
        )
    return from_plain(found, uuid.UUID)


# -- The page's ask.


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_writer_is_given_the_links_the_assistant_signs_for_them(
    pglite_route_client: _Client,
    uploads_served: UploadRelay,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    canvas_id = await open_canvas(client)

    granted, taken = await _answered(client, uploads_served, canvas_id=canvas_id)

    assert granted.status_code == 200
    assert json_loads(granted.content, dict[str, object]) == {
        "files": [_slot("shot.png").model_dump(), _slot("notes.md").model_dump()],
    }
    assert (taken.canvas_id, taken.conversation_id) == (canvas_id, None)
    assert (taken.asker, taken.asker_role) == (TEST_USER_EMAIL, "writer")
    assert taken.files == [ChatUploadFile.model_validate(each) for each in _FILES]
    # A canvas with no workspace uploads to the box of the asker's default one, the
    # same on every ask.
    _, again = await _answered(client, uploads_served, canvas_id=canvas_id)
    assert again.workspace_id == taken.workspace_id


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_canvass_workspace_names_the_box_the_links_are_for(
    pglite_route_client: _Client,
    uploads_served: UploadRelay,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    browser()
    made = await client.post("/api/workspaces", json={"title": "Mine"})
    assert made.status_code == 200, made.text
    workspace = uuid.UUID(
        from_plain(json_loads(made.content, dict[str, object])["id"], str),
    )
    canvas_id = await _cli_canvas(store, provider="assistant", workspace_id=workspace)

    granted, taken = await _answered(client, uploads_served, canvas_id=canvas_id)

    assert granted.status_code == 200
    assert taken.workspace_id == workspace


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_canvas_holding_another_users_workspace_asks_for_the_askers_box(
    pglite_route_client: _Client,
    uploads_served: UploadRelay,
) -> None:
    """Links are signed for the asker's own default, never another person's box."""
    client, store = pglite_route_client
    await seed_accounts(store)
    theirs = await open_default_workspace(
        store.engine,
        store=store,
        user_id=OTHER_USER_ID,
        email=OTHER_EMAIL,
    )
    canvas_id = await _cli_canvas(store, provider="assistant", workspace_id=theirs)
    browser()

    granted, taken = await _answered(client, uploads_served, canvas_id=canvas_id)

    assert granted.status_code == 200
    assert taken.workspace_id == await open_default_workspace(
        store.engine,
        store=store,
        user_id=TEST_USER_ID,
        email=TEST_USER_EMAIL,
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_starter_asks_for_links_in_their_own_open_chat(
    pglite_route_client: _Client,
    uploads_served: UploadRelay,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    canvas_id = await open_canvas(client)
    conversation = uuid.uuid4()
    await open_science_chat(client, store, conversation_id=conversation)

    granted, taken = await _answered(
        client,
        uploads_served,
        canvas_id=canvas_id,
        conversation_id=conversation,
    )

    assert granted.status_code == 200
    assert taken.conversation_id == conversation


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_chat_not_shared_with_the_asker_gets_no_links_and_no_ask(
    pglite_route_client: _Client,
    uploads_served: UploadRelay,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    canvas_id = await open_canvas(client)
    conversation = uuid.uuid4()
    await open_science_chat(
        client,
        store,
        conversation_id=conversation,
        account=OTHER_EMAIL,
    )

    refused = await _ask(client, canvas_id=canvas_id, conversation_id=conversation)

    assert (refused.status_code, _code(refused)) == (403, "chat_not_shared")
    assert await uploads_served.wait(wait_sec=0.01) is None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_chat_other_than_the_canvass_own_gets_no_links(
    pglite_route_client: _Client,
    uploads_served: UploadRelay,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_assistant()
    _ = await start_session(client, actor=KB_ACTOR)
    canvas_id = await open_canvas(client)
    sent = await client.post(
        "/api/chats",
        json={"canvas_id": str(canvas_id), "text": "first"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert sent.status_code == 200

    refused = await _ask(client, canvas_id=canvas_id, conversation_id=uuid.uuid4())

    assert refused.status_code == 409
    assert await uploads_served.wait(wait_sec=0.01) is None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_viewer_a_key_and_a_foreign_canvas_get_no_links(
    pglite_route_client: _Client,
    uploads_served: UploadRelay,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    canvas_id = await open_canvas(client)

    install_identity(make_test_identity(api_key_id=None, role="viewer"))
    assert (await _ask(client, canvas_id=canvas_id)).status_code == 403
    act_as_user_agent("writer")
    assert (await _ask(client, canvas_id=canvas_id)).status_code == 403
    browser()
    assert (await _ask(client, canvas_id=uuid.uuid4())).status_code == 404
    assert await uploads_served.wait(wait_sec=0.01) is None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_claude_or_codex_canvas_takes_no_uploads_yet(
    pglite_route_client: _Client,
    uploads_served: UploadRelay,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    for provider in ("claude", "codex"):
        canvas_id = await _cli_canvas(store, provider=provider)
        browser()

        refused = await _ask(client, canvas_id=canvas_id)

        assert (refused.status_code, _code(refused)) == (409, "uploads_unsupported")
    assert await uploads_served.wait(wait_sec=0.01) is None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_no_assistant_taking_the_ask_is_unavailable(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    canvas_id = await open_canvas(client)
    patch_state(monkeypatch, "uploads", UploadRelay(timeout_sec=0.02))

    refused = await _ask(client, canvas_id=canvas_id)

    assert (refused.status_code, _code(refused)) == (503, "uploads_unavailable")


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_ask_past_the_most_waiting_is_busy(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    canvas_id = await open_canvas(client)
    patch_state(monkeypatch, "uploads", UploadRelay(max_waiting=0))

    refused = await _ask(client, canvas_id=canvas_id)

    assert (refused.status_code, _code(refused)) == (503, "uploads_busy")


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_assistants_reason_reaches_the_page(
    pglite_route_client: _Client,
    uploads_served: UploadRelay,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    canvas_id = await open_canvas(client)

    refused, _ = await _answered(
        client,
        uploads_served,
        canvas_id=canvas_id,
        answer=UploadAnswer(error="This workspace has no box."),
    )

    assert refused.status_code == 409
    assert json_loads(refused.content, dict[str, object])["detail"] == {
        "code": "upload_refused",
        "message": "This workspace has no box.",
    }


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_slots_that_are_not_the_files_asked_for_are_a_bad_gateway(
    pglite_route_client: _Client,
    uploads_served: UploadRelay,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    canvas_id = await open_canvas(client)

    for slots in ([_slot("shot.png")], [_slot("notes.md"), _slot("shot.png")]):
        wrong, _ = await _answered(
            client,
            uploads_served,
            canvas_id=canvas_id,
            answer=UploadAnswer(files=slots),
        )

        assert wrong.status_code == 502


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_ask_naming_no_file_or_a_folder_is_refused(
    pglite_route_client: _Client,
    uploads_served: UploadRelay,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    canvas_id = await open_canvas(client)

    refused: tuple[list[dict[str, object]], ...] = (
        [],
        [{"name": "a/b.txt", "size": 1}],
    )
    for files in refused:
        assert (await _ask(client, canvas_id=canvas_id, files=files)).status_code == 422
    assert await uploads_served.wait(wait_sec=0.01) is None


# -- A file the poster uploaded: a link to it, and its removal.

_UPLOADED = "/home/user/uploads/0a1b2c3d/shot.png"


async def _about(
    client: httpx2.AsyncClient,
    relay: UploadRelay,
    *,
    route: str,
    canvas_id: uuid.UUID,
    answer: UploadAnswer,
) -> tuple[httpx2.Response, UploadAsk]:
    """Ask about the uploaded file as the page while the assistant answers."""
    page = asyncio.create_task(
        client.post(route, json={"canvas_id": str(canvas_id), "path": _UPLOADED}),
    )
    taken = await relay.wait(wait_sec=5)
    assert taken is not None
    assert relay.answer(taken.request_id, answer)
    return await page, taken


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_poster_gets_a_download_link_to_a_file_they_uploaded(
    pglite_route_client: _Client,
    uploads_served: UploadRelay,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    canvas_id = await open_canvas(client)
    url = f"https://box.invalid/files?path={_UPLOADED}"

    linked, taken = await _about(
        client,
        uploads_served,
        route=CHAT_FILE_LINK_PATH,
        canvas_id=canvas_id,
        answer=UploadAnswer(url=url),
    )

    assert linked.status_code == 200
    assert json_loads(linked.content, dict[str, object]) == {"url": url}
    assert (taken.action, taken.path, taken.asker) == (
        "link",
        _UPLOADED,
        TEST_USER_EMAIL,
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_poster_removes_a_file_they_uploaded(
    pglite_route_client: _Client,
    uploads_served: UploadRelay,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    canvas_id = await open_canvas(client)

    removed, taken = await _about(
        client,
        uploads_served,
        route=CHAT_FILE_REMOVE_PATH,
        canvas_id=canvas_id,
        answer=UploadAnswer(removed=True),
    )

    assert (removed.status_code, removed.content) == (204, b"")
    assert (taken.action, taken.path) == ("remove", _UPLOADED)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_link_or_removal_the_assistant_refuses_or_answers_wrongly_says_so(
    pglite_route_client: _Client,
    uploads_served: UploadRelay,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    canvas_id = await open_canvas(client)

    refused, _ = await _about(
        client,
        uploads_served,
        route=CHAT_FILE_LINK_PATH,
        canvas_id=canvas_id,
        answer=UploadAnswer(error="Not one of your uploads."),
    )
    wrong_link, _ = await _about(
        client,
        uploads_served,
        route=CHAT_FILE_LINK_PATH,
        canvas_id=canvas_id,
        answer=UploadAnswer(removed=True),
    )
    wrong_removal, _ = await _about(
        client,
        uploads_served,
        route=CHAT_FILE_REMOVE_PATH,
        canvas_id=canvas_id,
        answer=UploadAnswer(url=f"https://box.invalid/files?path={_UPLOADED}"),
    )

    assert (refused.status_code, _code(refused)) == (409, "upload_refused")
    assert (wrong_link.status_code, wrong_removal.status_code) == (502, 502)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_link_or_removal_follows_the_rule_an_upload_does(
    pglite_route_client: _Client,
    uploads_served: UploadRelay,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    claude = await _cli_canvas(store, provider="claude")
    browser()

    for route in (CHAT_FILE_LINK_PATH, CHAT_FILE_REMOVE_PATH):
        foreign = await client.post(
            route,
            json={"canvas_id": str(uuid.uuid4()), "path": _UPLOADED},
        )
        cli = await client.post(
            route,
            json={"canvas_id": str(claude), "path": _UPLOADED},
        )
        bad = await client.post(
            route,
            json={"canvas_id": str(claude), "path": "uploads/shot.png"},
        )

        assert foreign.status_code == 404
        assert (cli.status_code, _code(cli)) == (409, "uploads_unsupported")
        assert bad.status_code == 422
    assert await uploads_served.wait(wait_sec=0.01) is None


# -- The assistant's legs.


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_assistant_waits_for_an_ask_and_answers_it(
    pglite_route_client: _Client,
    uploads_served: UploadRelay,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_assistant()
    asked = asyncio.create_task(
        uploads_served.ask(
            canvas_id=uuid.UUID(int=7),
            conversation_id=None,
            workspace_id=uuid.UUID(int=8),
            asker=TEST_USER_EMAIL,
            asker_role="writer",
            files=[ChatUploadFile(name="shot.png", size=4)],
        ),
    )

    waited = await client.get(CHAT_UPLOADS_WAIT_PATH, params={"wait_sec": 5})

    assert waited.status_code == 200
    ask = UploadAsk.model_validate_json(waited.content)
    assert (ask.canvas_id, ask.asker) == (uuid.UUID(int=7), TEST_USER_EMAIL)
    answered = await client.post(
        f"/api/chats/uploads/{ask.request_id}",
        json={"files": [_slot("shot.png").model_dump()]},
    )
    assert answered.status_code == 204
    assert await asked == UploadAnswer(files=[_slot("shot.png")])
    late = await client.post(
        f"/api/chats/uploads/{ask.request_id}",
        json={"error": "late"},
    )
    assert late.status_code == 404


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_wait_with_no_ask_answers_null(pglite_route_client: _Client) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_assistant()

    waited = await client.get(CHAT_UPLOADS_WAIT_PATH, params={"wait_sec": 0})

    assert (waited.status_code, waited.content) == (200, b"null")


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_only_the_assistants_key_waits_or_answers(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)

    for act in (browser, lambda: act_as_user_agent("admin")):
        act()
        waited = await client.get(CHAT_UPLOADS_WAIT_PATH, params={"wait_sec": 0})
        answered = await client.post(
            f"/api/chats/uploads/{uuid.uuid4()}",
            json={"error": "no"},
        )

        assert (waited.status_code, answered.status_code) == (403, 403)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_wait_past_the_most_is_refused(pglite_route_client: _Client) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_assistant()

    for wait_sec in (-1, 26):
        waited = await client.get(CHAT_UPLOADS_WAIT_PATH, params={"wait_sec": wait_sec})

        assert waited.status_code == 422


# -- A line's attachments.


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_lines_attachments_reach_the_assistant_in_its_context(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_assistant()
    kb = await start_session(client, actor=KB_ACTOR)
    canvas_id = await open_canvas(client)

    sent = await client.post(
        "/api/chats",
        json={
            "canvas_id": str(canvas_id),
            "text": "what is in this screenshot?",
            "attachments": ["/home/user/uploads/shot.png"],
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )

    assert sent.status_code == 200
    act_as_assistant()
    [heard] = from_plain(
        json_loads((await drain(client, session_id=kb)).content, dict[str, object])[
            "messages"
        ],
        list[dict[str, object]],
    )
    context = from_plain(heard["context"], dict[str, object])
    assert context["attachments"] == ["/home/user/uploads/shot.png"]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_retry_with_other_attachments_is_another_line(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_assistant()
    _ = await start_session(client, actor=KB_ACTOR)
    canvas_id = await open_canvas(client)
    key = str(uuid.uuid4())

    async def post(paths: list[str]) -> int:
        sent = await client.post(
            "/api/chats",
            json={"canvas_id": str(canvas_id), "text": "see", "attachments": paths},
            headers={"Idempotency-Key": key},
        )
        return sent.status_code

    assert await post(["/home/user/uploads/a.png"]) == 200
    assert await post(["/home/user/uploads/b.png"]) == 409


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_attachments_on_a_cli_canvas_are_refused_until_it_takes_uploads(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    canvas_id = await _cli_canvas(store, provider="claude")
    browser()

    refused = await client.post(
        "/api/chats",
        json={
            "canvas_id": str(canvas_id),
            "text": "see",
            "attachments": ["/home/user/uploads/a.png"],
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )

    assert (refused.status_code, _code(refused)) == (409, "uploads_unsupported")


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
