"""Placing a `trax run` session in a workstream and a station, over the whole app."""

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
    open_workspace,
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


async def _workstream(client: httpx2.AsyncClient, title: str) -> uuid.UUID:
    """Make a workstream as the browser user."""
    browser()
    made = await client.post("/api/workstreams", json={"title": title})
    assert made.status_code == 200, made.text
    return uuid.UUID(from_plain(json_loads(made.content, dict[str, object])["id"], str))


async def _edges(store: Store, session: uuid.UUID) -> list[uuid.UUID]:
    """Return the workstreams a session is produced by."""
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


async def _stations(store: Store) -> list[str]:
    """Return the ids of the stations, in id order."""
    async with store.engine.acquire() as conn:
        rows = await conn.fetch("SELECT id FROM visual_workspaces ORDER BY id")
    return [str(from_plain(row["id"], uuid.UUID)) for row in rows]


async def _station_workstream(store: Store, workspace: object) -> str | None:
    """Return a station's stored workstream id."""
    async with store.engine.acquire() as conn:
        found = await conn.fetchval(
            "SELECT station->>'workstream_id' FROM visual_workspaces WHERE id = $1",
            workspace,
        )
    return None if found is None else from_plain(found, str)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_session_with_nothing_else_lands_in_scratch_in_a_new_station(
    pglite_route_client: _Client,
) -> None:
    """Rule 4: Scratch, one new station named for the session, one edge, one label."""
    client, store = pglite_route_client
    await seed_accounts(store)
    session = await seed_session(store, USER_AGENT, actor="Agent", cli="claude")

    placed = await _place(client, session)

    workspace = from_plain(placed["workspace_id"], str)
    assert await _edges(store, session) == [
        uuid.UUID(from_plain(placed["workstream_id"], str)),
    ]
    assert await _labels(store, session) == [f"station:{workspace}"]
    assert await _station_workstream(store, workspace) == placed["workstream_id"]
    async with store.engine.acquire() as conn:
        station = await conn.fetchrow(
            "SELECT station->>'name' AS name, station->>'session_id' AS made_for "
            "FROM visual_workspaces WHERE id = $1",
            uuid.UUID(workspace),
        )
    assert station is not None
    assert (station["name"], station["made_for"]) == ("Agent claude", str(session))


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_placing_a_placed_session_returns_its_placement(
    pglite_route_client: _Client,
) -> None:
    """Idempotent: no second station, no second edge."""
    client, store = pglite_route_client
    await seed_accounts(store)
    session = await seed_session(store, USER_AGENT)

    first = await _place(client, session)
    second = await _place(client, session, _MAIN)

    assert second == first
    assert await _stations(store) == [first["workspace_id"]]
    assert await _edges(store, session) == [
        uuid.UUID(from_plain(first["workstream_id"], str)),
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_run_started_from_a_station_joins_it_beside_the_sessions_it_holds(
    pglite_route_client: _Client,
) -> None:
    """Rule 1: the station and its workstream, however many sessions already sit on it."""
    client, store = pglite_route_client
    await seed_accounts(store)
    workspace = await open_workspace(client)
    _ = await seed_session(
        store,
        USER_AGENT,
        actor="a",
        labels=[f"station:{workspace}"],
    )
    session = await seed_session(store, USER_AGENT, actor="b")

    placed = await _place(client, session, {"workspace": str(workspace)})

    assert placed["workspace_id"] == str(workspace)
    assert placed["workstream_id"] == await _station_workstream(store, workspace)
    assert await _stations(store) == [str(workspace)]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_another_users_station_id_is_never_joined(
    pglite_route_client: _Client,
) -> None:
    """Rule 1 needs the caller's own station; otherwise the session is placed afresh."""
    client, store = pglite_route_client
    await seed_accounts(store)
    theirs = await open_workspace(client, user_id=OTHER_USER_ID, email=OTHER_EMAIL)
    session = await seed_session(store, USER_AGENT)

    placed = await _place(client, session, {"workspace": str(theirs)})

    assert placed["workspace_id"] != str(theirs)
    assert await _labels(store, session) == [f"station:{placed['workspace_id']}"]


async def _earlier_on_branch(store: Store, workstream: uuid.UUID) -> None:
    """Seed an earlier session of the user's on the `main` branch, in ``workstream``."""
    earlier = await seed_session(
        store,
        USER_AGENT,
        actor="earlier",
        labels=[_LABEL],
    )
    _ = await store.add_edge(
        from_id=earlier,
        to_id=workstream,
        edge_kind="produced_by",
        actor=USER_AGENT.email,
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_same_branch_returns_to_the_workstream_of_its_last_session(
    pglite_route_client: _Client,
) -> None:
    """Rule 3: a new station in the workstream of the earlier session on the branch."""
    client, store = pglite_route_client
    await seed_accounts(store)
    workstream = await _workstream(client, "Branch work")
    await _earlier_on_branch(store, workstream)
    later = await seed_session(store, USER_AGENT, actor="b")

    placed = await _place(client, later, _MAIN)

    assert placed["workstream_id"] == str(workstream)
    assert await _edges(store, later) == [workstream]
    assert await _stations(store) == [placed["workspace_id"]]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_another_branch_does_not_return_to_it(
    pglite_route_client: _Client,
) -> None:
    """Rule 3 needs the same origin and branch; else Scratch."""
    client, store = pglite_route_client
    await seed_accounts(store)
    workstream = await _workstream(client, "Branch work")
    await _earlier_on_branch(store, workstream)
    other = await seed_session(store, USER_AGENT, actor="c")

    placed = await _place(
        client,
        other,
        {"git": {"origin": "https://github.com/o/r.git", "branch": "dev"}},
    )

    assert placed["workstream_id"] != str(workstream)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("has_earlier", [False, True])
async def test_a_backfilled_session_inherits_only_from_earlier_branch_history(
    pglite_route_client: _Client,
    has_earlier: bool,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    recent = await _workstream(client, "Recent branch work")
    historical = await _workstream(client, "Historical branch work")
    candidates = [(2022, recent)]
    if has_earlier:
        candidates.append((2019, historical))
    for year, workstream in candidates:
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
            to_id=workstream,
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
        assert placed["workstream_id"] == str(historical)
    else:
        assigned = await store.get_inquiry(uuid.UUID(str(placed["workstream_id"])))
        assert assigned is not None
        assert assigned.title == "Scratch"
    assert placed["workstream_id"] != str(recent)


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
        ("trax-helper", None, None),
        ("claude", "chat:00000000-0000-0000-0000-000000000001", None),
        ("claude", None, "bot@service.invalid"),
    ],
)
async def test_helpers_chats_and_service_accounts_are_not_placed(
    pglite_route_client: _Client,
    cli: str,
    cli_session_id: str | None,
    account: str | None,
) -> None:
    """Their placement is null and they leave no station, edge or label."""
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
        "workstream_id": None,
        "workspace_id": None,
    }
    assert (await _stations(store), await _edges(store, session)) == ([], [])


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
            f"/api/sessions/{session}/workstream",
            json={"workstream_id": str(uuid.uuid4())},
        )
    ).status_code == 403
    assert (
        await client.post(f"/api/sessions/{uuid.uuid4()}/place", json={})
    ).status_code == 404
    assert await _stations(store) == []


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_moving_a_session_takes_the_station_made_for_it_when_it_is_alone(
    pglite_route_client: _Client,
) -> None:
    """The edge is replaced and the lone session's station follows."""
    client, store = pglite_route_client
    await seed_accounts(store)
    target = await _workstream(client, "Target")
    session = await seed_session(store, USER_AGENT)
    station = (await _place(client, session))["workspace_id"]
    act_as_user_agent()

    moved = await client.put(
        f"/api/sessions/{session}/workstream",
        json={"workstream_id": str(target)},
    )

    assert moved.status_code == 204
    assert await _edges(store, session) == [target]
    assert await _station_workstream(store, station) == str(target)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_moving_a_session_leaves_a_station_another_session_sits_on(
    pglite_route_client: _Client,
) -> None:
    """Only the moved session's edge changes."""
    client, store = pglite_route_client
    await seed_accounts(store)
    target = await _workstream(client, "Target")
    session = await seed_session(store, USER_AGENT, actor="a")
    station = (await _place(client, session))["workspace_id"]
    _ = await seed_session(
        store,
        USER_AGENT,
        actor="b",
        labels=[f"station:{station}"],
    )
    act_as_user_agent()

    moved = await client.put(
        f"/api/sessions/{session}/workstream",
        json={"workstream_id": str(target)},
    )

    assert moved.status_code == 204
    assert await _edges(store, session) == [target]
    assert await _station_workstream(store, station) != str(target)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_session_is_not_moved_to_a_workstream_that_is_not_the_callers(
    pglite_route_client: _Client,
) -> None:
    """An unknown destination is 404 and changes nothing."""
    client, store = pglite_route_client
    await seed_accounts(store)
    session = await seed_session(store, USER_AGENT)
    act_as_user_agent()

    refused = await client.put(
        f"/api/sessions/{session}/workstream",
        json={"workstream_id": str(uuid.uuid4())},
    )

    assert refused.status_code == 404
    assert await _edges(store, session) == []


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_moving_a_station_moves_every_session_placed_on_it(
    pglite_route_client: _Client,
) -> None:
    """Each session's workstream edge is replaced, so station and sessions agree."""
    client, store = pglite_route_client
    await seed_accounts(store)
    target = await _workstream(client, "Target")
    first = await seed_session(store, USER_AGENT, actor="a")
    station = (await _place(client, first))["workspace_id"]
    second = await seed_session(
        store,
        USER_AGENT,
        actor="b",
        labels=[f"station:{station}"],
    )
    browser()

    moved = await client.put(
        f"/api/workspaces/{station}/workstream",
        json={"workstream_id": str(target)},
    )

    assert moved.status_code == 200
    assert await _edges(store, first) == [target]
    assert await _edges(store, second) == [target]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_placed_sessions_key_may_operate_its_station_and_no_other_users_key(
    pglite_route_client: _Client,
) -> None:
    """A label naming a station gives its key nothing unless the key is the owner's."""
    client, store = pglite_route_client
    await seed_accounts(store)
    session = await seed_session(store, USER_AGENT)
    station = (await _place(client, session))["workspace_id"]
    _ = await seed_session(
        store,
        OTHER_AGENT,
        actor="liar",
        labels=[f"station:{station}"],
    )

    act_as_user_agent()
    allowed = await client.get(f"/api/workspaces/{station}")
    act_as_other_agent()
    labelled_by_another_user = await client.get(f"/api/workspaces/{station}")

    assert (allowed.status_code, labelled_by_another_user.status_code) == (200, 404)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_ended_sessions_key_no_longer_operates_its_station(
    pglite_route_client: _Client,
) -> None:
    """Only a live session's key may use the station."""
    client, store = pglite_route_client
    await seed_accounts(store)
    session = await seed_session(store, USER_AGENT)
    station = (await _place(client, session))["workspace_id"]
    ended = await client.post(f"/api/sessions/{session}/end", json={})

    after_end = await client.get(f"/api/workspaces/{station}")

    assert (ended.status_code, after_end.status_code) == (200, 403)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_app_does_not_open_on_an_agents_station(
    pglite_route_client: _Client,
) -> None:
    """A station made for a session is skipped by `open latest`."""
    client, store = pglite_route_client
    await seed_accounts(store)
    mine = await open_workspace(client)
    session = await seed_session(store, USER_AGENT)
    await _place(client, session)

    browser()
    opened = await client.post("/api/workspaces")

    assert from_plain(json_loads(opened.content, dict[str, object])["id"], str) == str(
        mine,
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_replayed_operation_answers_with_the_stations_current_workstream_and_session(
    pglite_route_client: _Client,
) -> None:
    """The stored receipt is stale; the binding fields come from the station now."""
    client, store = pglite_route_client
    await seed_accounts(store)
    workspace = await open_workspace(client)
    session = await seed_session(
        store,
        USER_AGENT,
        labels=[f"station:{workspace}"],
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
    path = f"/api/workspaces/{workspace}/operations"
    assert (await client.post(path, json=operation, headers=headers)).status_code == 200
    moved, made_for = uuid.uuid4(), str(session)
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE visual_workspaces SET station = station || "
            "jsonb_build_object('workstream_id', $2::text, 'session_id', $3::text) "
            "WHERE id = $1",
            workspace,
            str(moved),
            made_for,
        )

    replay = json_loads(
        (await client.post(path, json=operation, headers=headers)).content,
        dict[str, object],
    )

    assert (replay["workstream_id"], replay["session_id"]) == (str(moved), made_for)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
