"""The routes a machine's owner sees and stops its live runs with, over PGlite."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import uuid

from treekle import json_loads

import pytest
import pytest_asyncio

from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import (
    TEST_API_KEY_ID,
    TEST_USER_EMAIL,
    TEST_USER_ID,
    install_identity,
    make_test_identity,
)
from trackinizer.server.api.session_test_support import (
    act_as_user_agent,
    drain,
    start_session,
)
from trackinizer.wire.wire_machine_host import (
    ENROLL_PATH,
    HEARTBEAT_PATH,
    JOIN_PATH,
    EnrollResponse,
    JoinResponse,
)
from trackinizer.wire.wire_machines import (
    MACHINE_DISCONNECT_PATH,
    MACHINE_LOGOUT_PATH,
    MACHINE_RUN_STOP_PATH,
    MACHINE_RUNS_PATH,
    MachineLogout,
    MachineRun,
    MachineRuns,
)
from trackinizer.wire.wire_terminals import (
    TERMINAL_MACHINES_PATH,
    TerminalMachines,
)


if TYPE_CHECKING:
    import httpx2

    from trackinizer.server.inbound import InboundQueue
    from trackinizer.server.store.core import Store


pytestmark = [
    pytest.mark.db_pglite,
    pytest.mark.asyncio(loop_scope="session"),
]


_INSTANCE = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_MINE = uuid.UUID("11111111-1111-1111-1111-111111111111")
_THEIRS = uuid.UUID("22222222-2222-2222-2222-222222222222")


@pytest_asyncio.fixture(loop_scope="session", autouse=True)
async def _owner_account(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Seed the admin the fixture acts as, who owns the machines it enrolls, and its key."""
    _, store = pglite_route_client
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, $2, 'admin', 'admin', 'active')",
            TEST_USER_ID,
            TEST_USER_EMAIL,
        )
        await conn.execute(
            "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
            "VALUES ($1, $2, 'agent', 'test-hash', 'trax_agent', 'writer')",
            TEST_API_KEY_ID,
            TEST_USER_ID,
        )
    install_identity(make_test_identity(role="admin", api_key_id=None))


async def test_a_machine_lists_its_owners_live_runs_alone(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    await _enroll(client, "box")
    _live_runs()

    listed = await client.get(MACHINE_RUNS_PATH.format(name="box"))
    machines = await client.get(TERMINAL_MACHINES_PATH)

    assert listed.status_code == 200, listed.text
    assert MachineRuns.model_validate_json(listed.text).runs == [_MY_RUN]
    (machine,) = TerminalMachines.model_validate_json(machines.text).machines
    assert (machine.status, machine.runs) == ("never", [_MY_RUN])


async def test_stopping_a_run_ends_its_lease_and_refuses_its_drains(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    await _enroll(client, "box")
    _live_runs()

    stopped = await client.post(_stop_path("box", _MINE))
    again = await client.post(_stop_path("box", _MINE))
    theirs = await client.post(_stop_path("box", _THEIRS))

    assert stopped.status_code == 204, stopped.text
    assert (again.status_code, theirs.status_code) == (404, 404)
    assert _inbound().stopped(_MINE, instance="a" * 32)
    assert list(_inbound().runs_on("box", account="eve@example.com")) == [_THEIRS]


async def test_another_persons_machine_reads_as_none(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    await _enroll(client, "box")
    async with store.engine.acquire() as conn:
        await conn.execute("UPDATE machines SET owner = 'eve@example.com'")
    _live_runs()

    listed = await client.get(MACHINE_RUNS_PATH.format(name="box"))
    stopped = await client.post(_stop_path("box", _THEIRS))
    unknown = await client.get(MACHINE_RUNS_PATH.format(name="nowhere"))

    assert (listed.status_code, stopped.status_code, unknown.status_code) == (
        404,
        404,
        404,
    )
    assert not _inbound().stopped(_THEIRS, instance="b" * 32)


async def test_disconnect_revokes_the_host_and_stops_its_owners_runs(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    machine_id, credential = await _joined(client, "box")
    _live_runs()

    disconnected = await client.post(MACHINE_DISCONNECT_PATH.format(name="box"))
    beat = await client.post(
        HEARTBEAT_PATH.format(machine_id=machine_id),
        json={"instance": str(_INSTANCE), "host_version": "0.1"},
        headers={"Authorization": f"Bearer {credential}"},
    )

    assert disconnected.status_code == 200, disconnected.text
    assert MachineRuns.model_validate_json(disconnected.text).runs == [_MY_RUN]
    assert beat.status_code == 410
    assert _inbound().stopped(_MINE, instance="a" * 32)
    assert not _inbound().stopped(_THEIRS, instance="b" * 32)


async def test_a_machines_past_runs_outlive_their_leases(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    await _enroll(client, "box")
    act_as_user_agent("writer")
    session = await start_session(client, cli="claude")
    polled = await drain(
        client,
        session_id=session,
        cli="claude",
        instance="d" * 32,
        machine="box",
    )
    assert polled.status_code == 200, polled.text
    _inbound().forget_poller(session)
    install_identity(make_test_identity(role="admin", api_key_id=None))

    listed = await client.get(MACHINE_RUNS_PATH.format(name="box"))

    assert listed.status_code == 200, listed.text
    runs = MachineRuns.model_validate_json(listed.text)
    assert runs.runs == []
    assert [(run.session_id, run.cli) for run in runs.past] == [(session, "claude")]
    assert runs.past[0].last_seen is not None


async def test_logout_disconnects_and_revokes_the_keys_its_logins_made_alone(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    machine_id, credential = await _joined(client, "box")
    linked, other = uuid.uuid4(), uuid.uuid4()
    async with store.engine.acquire() as conn:
        for key, machine in ((linked, machine_id), (other, None)):
            await conn.execute(
                "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role, "
                "machine_id) VALUES ($1, $2, 'trax@box', 'hash', $3, 'writer', $4)",
                key,
                TEST_USER_ID,
                f"trax_{key.hex[:6]}",
                machine,
            )
    _live_runs()

    out = await client.post(MACHINE_LOGOUT_PATH.format(name="box"))
    beat = await client.post(
        HEARTBEAT_PATH.format(machine_id=machine_id),
        json={"instance": str(_INSTANCE), "host_version": "0.1"},
        headers={"Authorization": f"Bearer {credential}"},
    )

    assert out.status_code == 200, out.text
    logout = MachineLogout.model_validate_json(out.text)
    assert (logout.runs, logout.keys) == ([_MY_RUN], 1)
    assert beat.status_code == 410
    async with store.engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, revoked_at IS NOT NULL AS revoked FROM api_keys WHERE id = ANY($1)",
            [linked, other],
        )
    assert {row["id"]: row["revoked"] for row in rows} == {linked: True, other: False}


async def test_a_key_names_the_machine_whose_logout_revokes_it(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    machine_id, _ = await _joined(client, "box")
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE api_keys SET machine_id = $1 WHERE id = $2",
            machine_id,
            TEST_API_KEY_ID,
        )
    named: list[object] = []
    for key in (TEST_API_KEY_ID, None):
        install_identity(make_test_identity(role="admin", api_key_id=key))
        profile = await client.get("/api/me/profile")
        assert profile.status_code == 200, profile.text
        named.append(json_loads(profile.text, dict[str, object]).get("machine"))

    assert named == ["box", None]


_MY_RUN = MachineRun(session_id=_MINE, cli="claude", run="chat-11111111", cwd="~/w")
"""What the owner's run on ``box`` says of itself."""


def _live_runs() -> None:
    """Renew the owner's run on ``box``, another person's there, and one elsewhere."""
    inbound = _inbound()
    inbound.mark_poller(
        _MINE,
        run=_MY_RUN.run,
        machine="box",
        cwd=_MY_RUN.cwd,
        cli="claude",
        instance="a" * 32,
        account=TEST_USER_EMAIL,
    )
    inbound.mark_poller(
        _THEIRS,
        machine="box",
        cli="codex",
        instance="b" * 32,
        account="eve@example.com",
    )
    inbound.mark_poller(
        uuid.uuid4(),
        machine="gx10",
        cli="claude",
        instance="c" * 32,
        account=TEST_USER_EMAIL,
    )


def _inbound() -> InboundQueue:
    return cast("InboundQueue", app.state.inbound)


def _stop_path(name: str, session_id: uuid.UUID) -> str:
    return MACHINE_RUN_STOP_PATH.format(name=name, session_id=session_id)


async def _enroll(client: httpx2.AsyncClient, name: str) -> str:
    response = await client.post(ENROLL_PATH, json={"name": name})
    assert response.status_code == 201, response.text
    return EnrollResponse.model_validate_json(response.text).token


async def _joined(client: httpx2.AsyncClient, name: str) -> tuple[uuid.UUID, str]:
    response = await client.post(
        JOIN_PATH,
        json={
            "name": name,
            "token": await _enroll(client, name),
            "instance": str(_INSTANCE),
            "host_version": "0.1",
            "facts": {},
        },
    )
    assert response.status_code == 201, response.text
    joined = JoinResponse.model_validate_json(response.text)
    return joined.machine_id, joined.credential


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
