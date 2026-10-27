"""Placing an agent run's session in a workspace and a canvas, over the whole app."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import uuid

from treekle import from_plain, json_loads

import pytest

from trackinizer.server.api.app import app
from trackinizer.server.api.canvas_test_support import (
    ASSISTANT_CONFIG,
    OTHER_AGENT,
    OTHER_EMAIL,
    OTHER_USER_ID,
    USER_AGENT,
    act_as_other_agent,
    act_as_user_agent,
    browser,
    open_canvas,
    seed_accounts,
    seed_session,
)
from trackinizer.server.chat_hub import ChatHub
from trackinizer.server.inbound import InboundQueue
from trackinizer.wire.bodies import SubmitAgentSession


if TYPE_CHECKING:
    from collections.abc import Mapping

    import httpx2

    from trackinizer.server.store.core import Store


type _Client = tuple[httpx2.AsyncClient, Store]

_LABEL = "git:https://github.com/o/r.git@main"
_MAIN = {"git": {"origin": "https://github.com/o/r.git", "branch": "main"}}


@pytest.fixture(autouse=True)
def served(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve with a fresh poller table and event hub."""
    monkeypatch.setattr(app.state, "config", ASSISTANT_CONFIG, raising=False)
    monkeypatch.setattr(app.state, "inbound", InboundQueue(), raising=False)
    monkeypatch.setattr(app.state, "hub", ChatHub(), raising=False)


async def _place(
    client: httpx2.AsyncClient,
    session: uuid.UUID,
    body: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Place a session as its own key; return the answer."""
    act_as_user_agent()
    answer = await client.post(f"/api/sessions/{session}/place", json=body or {})
    assert answer.status_code == 200, answer.text
    return json_loads(answer.content, dict[str, object])


async def _workspace(client: httpx2.AsyncClient, title: str) -> uuid.UUID:
    """Make a workspace as the browser user."""
    browser()
    made = await client.post("/api/workspaces", json={"title": title})
    assert made.status_code == 200, made.text
    return uuid.UUID(from_plain(json_loads(made.content, dict[str, object])["id"], str))


async def _edges(store: Store, session: uuid.UUID) -> list[uuid.UUID]:
    """Return the workspaces a session is produced by."""
    async with store.engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT to_id FROM edges WHERE from_id = $1 AND edge_kind = 'produced_by'",
            session,
        )
    return [from_plain(row["to_id"], uuid.UUID) for row in rows]


async def _labels(store: Store, session: uuid.UUID) -> list[str]:
    """Return a session's labels."""
    async with store.engine.acquire() as conn:
        labels = await conn.fetchval(
            "SELECT labels FROM inquiries WHERE id = $1",
            session,
        )
    return sorted(from_plain(labels, list[str]))


async def _canvases(store: Store) -> list[str]:
    """Return the ids of the canvases, in id order."""
    async with store.engine.acquire() as conn:
        rows = await conn.fetch("SELECT id FROM visual_canvases ORDER BY id")
    return [str(from_plain(row["id"], uuid.UUID)) for row in rows]


async def _canvas_workspace(store: Store, canvas: object) -> str | None:
    """Return a canvas's stored workspace id."""
    async with store.engine.acquire() as conn:
        found = await conn.fetchval(
            "SELECT metadata->>'workspace_id' FROM visual_canvases WHERE id = $1",
            canvas,
        )
    return None if found is None else from_plain(found, str)


async def _defaults(store: Store) -> int:
    """Count the Issues marked as someone's default workspace."""
    async with store.engine.acquire() as conn:
        return from_plain(
            await conn.fetchval(
                "SELECT count(*) FROM inquiries "
                "WHERE labels @> ARRAY['workspace', 'scratch']::text[]",
            ),
            int,
        )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_session_with_nothing_else_lands_in_the_default_in_a_new_canvas(
    pglite_route_client: _Client,
) -> None:
    """Rule 4: the default workspace, one new canvas for the session, one edge, label."""
    client, store = pglite_route_client
    await seed_accounts(store)
    session = await seed_session(store, USER_AGENT, actor="Agent", cli="claude")

    placed = await _place(client, session)

    canvas = from_plain(placed["canvas_id"], str)
    default = await store.get_inquiry(
        uuid.UUID(from_plain(placed["workspace_id"], str)),
    )
    assert default is not None
    assert default.title == "Untitled workspace"
    assert await _edges(store, session) == [default.id]
    assert await _labels(store, session) == [f"canvas:{canvas}"]
    assert await _canvas_workspace(store, canvas) == placed["workspace_id"]
    async with store.engine.acquire() as conn:
        canvas = await conn.fetchrow(
            "SELECT metadata->>'name' AS name, metadata->>'session_id' AS made_for "
            "FROM visual_canvases WHERE id = $1",
            uuid.UUID(canvas),
        )
    assert canvas is not None
    assert (canvas["name"], canvas["made_for"]) == ("Agent claude", str(session))


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_session_landing_in_a_hidden_default_shows_it_again(
    pglite_route_client: _Client,
) -> None:
    """Nothing lands out of sight: the hidden default is back in the bar."""
    client, store = pglite_route_client
    await seed_accounts(store)
    first = await _place(client, await seed_session(store, USER_AGENT, actor="a"))
    default = from_plain(first["workspace_id"], str)
    browser()
    hidden = await client.patch(f"/api/workspaces/{default}", json={"hidden": True})

    later = await _place(client, await seed_session(store, USER_AGENT, actor="b"))

    browser()
    bar = json_loads((await client.get("/api/workspaces")).content, list[object])
    assert hidden.status_code == 200, hidden.text
    assert later["workspace_id"] == default
    assert [from_plain(entry, dict[str, object])["id"] for entry in bar] == [default]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_placing_a_placed_session_returns_its_placement(
    pglite_route_client: _Client,
) -> None:
    """Idempotent: no second canvas, no second edge."""
    client, store = pglite_route_client
    await seed_accounts(store)
    session = await seed_session(store, USER_AGENT)

    first = await _place(client, session)
    second = await _place(client, session, _MAIN)

    assert second == first
    assert await _canvases(store) == [first["canvas_id"]]
    assert await _edges(store, session) == [
        uuid.UUID(from_plain(first["workspace_id"], str)),
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_run_started_from_a_canvas_joins_it_beside_the_sessions_it_holds(
    pglite_route_client: _Client,
) -> None:
    """Rule 1: the canvas and its workspace, however many sessions already sit on it."""
    client, store = pglite_route_client
    await seed_accounts(store)
    canvas = await open_canvas(client)
    _ = await seed_session(
        store,
        USER_AGENT,
        actor="a",
        labels=[f"canvas:{canvas}"],
    )
    session = await seed_session(store, USER_AGENT, actor="b")

    placed = await _place(client, session, {"canvas": str(canvas)})

    assert placed["canvas_id"] == str(canvas)
    assert placed["workspace_id"] == await _canvas_workspace(store, canvas)
    assert await _canvases(store) == [str(canvas)]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_another_users_canvas_id_is_never_joined(
    pglite_route_client: _Client,
) -> None:
    """Rule 1 needs the caller's own canvas; otherwise the session is placed afresh."""
    client, store = pglite_route_client
    await seed_accounts(store)
    theirs = await open_canvas(client, user_id=OTHER_USER_ID, email=OTHER_EMAIL)
    session = await seed_session(store, USER_AGENT)

    placed = await _place(client, session, {"canvas": str(theirs)})

    assert placed["canvas_id"] != str(theirs)
    assert await _labels(store, session) == [f"canvas:{placed['canvas_id']}"]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_edge_to_another_users_workspace_never_places_a_session_in_it(
    pglite_route_client: _Client,
) -> None:
    """Rule 2 keeps only an owned workspace; the caller's default replaces the edge."""
    client, store = pglite_route_client
    await seed_accounts(store)
    browser(OTHER_USER_ID, email=OTHER_EMAIL)
    made = await client.post("/api/workspaces", json={"title": "Theirs"})
    assert made.status_code == 200, made.text
    theirs = uuid.UUID(
        from_plain(json_loads(made.content, dict[str, object])["id"], str),
    )
    session = await seed_session(store, USER_AGENT)
    _ = await store.add_edge(
        from_id=session,
        to_id=theirs,
        edge_kind="produced_by",
        actor=USER_AGENT.email,
    )

    placed = await _place(client, session)

    workspace = uuid.UUID(from_plain(placed["workspace_id"], str))
    default = await store.get_inquiry(workspace)
    assert default is not None
    assert default.owner == USER_AGENT.email
    assert await _canvas_workspace(store, placed["canvas_id"]) == str(workspace)
    assert await _edges(store, session) == [workspace]


async def _earlier_on_branch(store: Store, workspace: uuid.UUID) -> None:
    """Seed an earlier session of the user's on the `main` branch, in ``workspace``."""
    earlier = await seed_session(
        store,
        USER_AGENT,
        actor="earlier",
        labels=[_LABEL],
    )
    _ = await store.add_edge(
        from_id=earlier,
        to_id=workspace,
        edge_kind="produced_by",
        actor=USER_AGENT.email,
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_same_branch_returns_to_the_workspace_of_its_last_session(
    pglite_route_client: _Client,
) -> None:
    """Rule 3: a new canvas in the workspace of the earlier session on the branch.

    No default workspace is made: only a session that lands in it makes one.
    """
    client, store = pglite_route_client
    await seed_accounts(store)
    workspace = await _workspace(client, "Branch work")
    await _earlier_on_branch(store, workspace)
    later = await seed_session(store, USER_AGENT, actor="b")

    placed = await _place(client, later, _MAIN)

    assert placed["workspace_id"] == str(workspace)
    assert await _edges(store, later) == [workspace]
    assert await _defaults(store) == 0
    assert await _canvases(store) == [placed["canvas_id"]]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_another_branch_does_not_return_to_it(
    pglite_route_client: _Client,
) -> None:
    """Rule 3 needs the same origin and branch; else the default workspace."""
    client, store = pglite_route_client
    await seed_accounts(store)
    workspace = await _workspace(client, "Branch work")
    await _earlier_on_branch(store, workspace)
    other = await seed_session(store, USER_AGENT, actor="c")

    placed = await _place(
        client,
        other,
        {"git": {"origin": "https://github.com/o/r.git", "branch": "dev"}},
    )

    assert placed["workspace_id"] != str(workspace)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("has_earlier", [False, True])
async def test_a_backfilled_session_inherits_only_from_earlier_branch_history(
    pglite_route_client: _Client,
    has_earlier: bool,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    recent = await _workspace(client, "Recent branch work")
    historical = await _workspace(client, "Historical branch work")
    candidates = [(2022, recent)]
    if has_earlier:
        candidates.append((2019, historical))
    for year, workspace in candidates:
        session_id = await store.submit_agentsession(
            SubmitAgentSession(
                title=f"Session from {year}",
                account=USER_AGENT.email,
                owner=f"session-{year}",
                cli="codex",
                created=datetime(year, 1, 1, tzinfo=UTC),
                labels=[_LABEL],
            ),
            audience="private",
            api_key_id=USER_AGENT.key_id,
        )
        await store.add_edge(
            from_id=session_id,
            to_id=workspace,
            edge_kind="produced_by",
            actor=USER_AGENT.email,
        )
    backfilled = await store.submit_agentsession(
        SubmitAgentSession(
            title="Session from 2020",
            account=USER_AGENT.email,
            owner="session-2020",
            cli="codex",
            created=datetime(2020, 1, 1, tzinfo=UTC),
        ),
        audience="private",
        api_key_id=USER_AGENT.key_id,
    )

    placed = await _place(client, backfilled, _MAIN)

    if has_earlier:
        assert placed["workspace_id"] == str(historical)
    else:
        assigned = await store.get_inquiry(uuid.UUID(str(placed["workspace_id"])))
        assert assigned is not None
        assert assigned.title == "Untitled workspace"
    assert placed["workspace_id"] != str(recent)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_git_label_drops_credentials_from_the_origin(
    pglite_route_client: _Client,
) -> None:
    """A URL with userinfo never reaches a label."""
    client, store = pglite_route_client
    await seed_accounts(store)
    session = await seed_session(store, USER_AGENT)

    await _place(
        client,
        session,
        {"git": {"origin": "https://user:token@github.com/o/r.git", "branch": "main"}},
    )

    assert [
        label for label in await _labels(store, session) if label.startswith("git:")
    ] == [
        "git:https://github.com/o/r.git@main",
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    ("cli", "cli_session_id", "account"),
    [
        ("claude", "chat:00000000-0000-0000-0000-000000000001", None),
        ("claude", None, "bot@service.invalid"),
    ],
)
async def test_chats_and_service_accounts_are_not_placed(
    pglite_route_client: _Client,
    cli: str,
    cli_session_id: str | None,
    account: str | None,
) -> None:
    """Their placement is null and they leave no canvas, edge or label."""
    client, store = pglite_route_client
    await seed_accounts(store)
    session = await seed_session(
        store,
        USER_AGENT,
        cli=cli,
        cli_session_id=cli_session_id,
        account=account,
    )
    act_as_user_agent()
    answer = await client.post(f"/api/sessions/{session}/place", json={})
    if account is not None:
        assert answer.status_code == 403
        return

    assert json_loads(answer.content, dict[str, object]) == {
        "workspace_id": None,
        "canvas_id": None,
    }
    assert (await _canvases(store), await _edges(store, session)) == ([], [])


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_only_the_sessions_own_account_places_or_moves_it(
    pglite_route_client: _Client,
) -> None:
    """Another user's key gets 403, and an unknown session 404."""
    client, store = pglite_route_client
    await seed_accounts(store)
    session = await seed_session(store, USER_AGENT)
    act_as_other_agent()

    assert (
        await client.post(f"/api/sessions/{session}/place", json={})
    ).status_code == 403
    assert (
        await client.put(
            f"/api/sessions/{session}/workspace",
            json={"workspace_id": str(uuid.uuid4())},
        )
    ).status_code == 403
    assert (
        await client.post(f"/api/sessions/{uuid.uuid4()}/place", json={})
    ).status_code == 404
    assert await _canvases(store) == []


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_moving_a_session_takes_the_canvas_made_for_it_when_it_is_alone(
    pglite_route_client: _Client,
) -> None:
    """The edge is replaced and the lone session's canvas follows."""
    client, store = pglite_route_client
    await seed_accounts(store)
    target = await _workspace(client, "Target")
    session = await seed_session(store, USER_AGENT)
    canvas = (await _place(client, session))["canvas_id"]
    act_as_user_agent()

    moved = await client.put(
        f"/api/sessions/{session}/workspace",
        json={"workspace_id": str(target)},
    )

    assert moved.status_code == 204
    assert await _edges(store, session) == [target]
    assert await _canvas_workspace(store, canvas) == str(target)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_moving_a_session_leaves_a_canvas_another_session_sits_on(
    pglite_route_client: _Client,
) -> None:
    """Only the moved session's edge changes."""
    client, store = pglite_route_client
    await seed_accounts(store)
    target = await _workspace(client, "Target")
    session = await seed_session(store, USER_AGENT, actor="a")
    canvas = (await _place(client, session))["canvas_id"]
    _ = await seed_session(
        store,
        USER_AGENT,
        actor="b",
        labels=[f"canvas:{canvas}"],
    )
    act_as_user_agent()

    moved = await client.put(
        f"/api/sessions/{session}/workspace",
        json={"workspace_id": str(target)},
    )

    assert moved.status_code == 204
    assert await _edges(store, session) == [target]
    assert await _canvas_workspace(store, canvas) != str(target)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_session_is_not_moved_to_a_workspace_that_is_not_the_callers(
    pglite_route_client: _Client,
) -> None:
    """An unknown destination is 404 and changes nothing."""
    client, store = pglite_route_client
    await seed_accounts(store)
    session = await seed_session(store, USER_AGENT)
    act_as_user_agent()

    refused = await client.put(
        f"/api/sessions/{session}/workspace",
        json={"workspace_id": str(uuid.uuid4())},
    )

    assert refused.status_code == 404
    assert await _edges(store, session) == []


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_moving_a_canvas_moves_every_session_placed_on_it(
    pglite_route_client: _Client,
) -> None:
    """Each session's workspace edge is replaced, so canvas and sessions agree."""
    client, store = pglite_route_client
    await seed_accounts(store)
    target = await _workspace(client, "Target")
    first = await seed_session(store, USER_AGENT, actor="a")
    canvas = (await _place(client, first))["canvas_id"]
    second = await seed_session(
        store,
        USER_AGENT,
        actor="b",
        labels=[f"canvas:{canvas}"],
    )
    browser()

    moved = await client.put(
        f"/api/canvases/{canvas}/workspace",
        json={"workspace_id": str(target)},
    )

    assert moved.status_code == 200
    assert await _edges(store, first) == [target]
    assert await _edges(store, second) == [target]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_placed_sessions_key_may_operate_its_canvas_and_no_other_users_key(
    pglite_route_client: _Client,
) -> None:
    """A label naming a canvas gives its key nothing unless the key is the owner's."""
    client, store = pglite_route_client
    await seed_accounts(store)
    session = await seed_session(store, USER_AGENT)
    canvas = (await _place(client, session))["canvas_id"]
    _ = await seed_session(
        store,
        OTHER_AGENT,
        actor="liar",
        labels=[f"canvas:{canvas}"],
    )

    act_as_user_agent()
    allowed = await client.get(f"/api/canvases/{canvas}")
    act_as_other_agent()
    labelled_by_another_user = await client.get(f"/api/canvases/{canvas}")

    assert (allowed.status_code, labelled_by_another_user.status_code) == (200, 404)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_ended_sessions_key_no_longer_operates_its_canvas(
    pglite_route_client: _Client,
) -> None:
    """Only a live session's key may use the canvas."""
    client, store = pglite_route_client
    await seed_accounts(store)
    session = await seed_session(store, USER_AGENT)
    canvas = (await _place(client, session))["canvas_id"]
    ended = await client.post(f"/api/sessions/{session}/end", json={})

    after_end = await client.get(f"/api/canvases/{canvas}")

    assert (ended.status_code, after_end.status_code) == (200, 403)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_app_does_not_open_on_an_agents_canvas(
    pglite_route_client: _Client,
) -> None:
    """A canvas made for a session is skipped by `open latest`."""
    client, store = pglite_route_client
    await seed_accounts(store)
    mine = await open_canvas(client)
    session = await seed_session(store, USER_AGENT)
    await _place(client, session)

    browser()
    opened = await client.post("/api/canvases")

    assert from_plain(json_loads(opened.content, dict[str, object])["id"], str) == str(
        mine,
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_replayed_operation_answers_with_the_canvases_current_workspace_and_session(
    pglite_route_client: _Client,
) -> None:
    """The stored receipt is stale; the binding fields come from the canvas now."""
    client, store = pglite_route_client
    await seed_accounts(store)
    canvas = await open_canvas(client)
    session = await seed_session(
        store,
        USER_AGENT,
        labels=[f"canvas:{canvas}"],
    )
    operation = {
        "revision": 0,
        "operation": {
            "kind": "show",
            "visual_type": "trax.subgraph",
            "record_id": str(uuid.uuid4()),
            "placement": "side",
        },
    }
    headers = {"Idempotency-Key": str(uuid.uuid4())}
    act_as_user_agent()
    path = f"/api/canvases/{canvas}/operations"
    assert (await client.post(path, json=operation, headers=headers)).status_code == 200
    moved, made_for = uuid.uuid4(), str(session)
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE visual_canvases SET metadata = metadata || "
            "jsonb_build_object('workspace_id', $2::text, 'session_id', $3::text) "
            "WHERE id = $1",
            canvas,
            str(moved),
            made_for,
        )

    replay = json_loads(
        (await client.post(path, json=operation, headers=headers)).content,
        dict[str, object],
    )

    assert (replay["workspace_id"], replay["session_id"]) == (str(moved), made_for)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
