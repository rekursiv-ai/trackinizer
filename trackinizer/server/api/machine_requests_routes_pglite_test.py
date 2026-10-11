"""Machine request routes, end to end over PGlite, with real credentials.

The identity override is cleared, so every caller authenticates as production
does: a machine with its own credential, the provider with an admin API key, a
writer with a writer key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

import uuid

from treekle import from_plain

import pytest
import pytest_asyncio

from trackinizer.server.api.conftest import (
    TEST_USER_EMAIL,
    TEST_USER_ID,
    clear_identity_override,
    install_identity,
    make_test_identity,
)
from trackinizer.server.auth import create_api_key
from trackinizer.wire.wire_machine_host import (
    DESTROYED_PATH,
    ENROLL_PATH,
    JOIN_PATH,
    EnrollResponse,
    JoinResponse,
)
from trackinizer.wire.wire_machine_requests import (
    MACHINE_REQUEST_PATH,
    MACHINE_REQUEST_RESULT_PATH,
    MACHINE_REQUESTS_PATH,
    MachineRequest,
    MachineRequestList,
)


if TYPE_CHECKING:
    import httpx2

    from trackinizer.server.store.core import Store


pytestmark = [
    pytest.mark.db_pglite,
    pytest.mark.asyncio(loop_scope="session"),
]


class _Keys(NamedTuple):
    admin: str
    writer: str
    box_a: str
    box_b: str


@pytest_asyncio.fixture(loop_scope="session")
async def keys(pglite_route_client: tuple[httpx2.AsyncClient, Store]) -> _Keys:
    """Seed an admin and a writer with API keys, and join two machines."""
    client, store = pglite_route_client
    writer_id = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) VALUES "
            "($1, $2, 'admin', 'admin', 'active'), "
            "($3, 'writer@example.com', 'writer', 'writer', 'active')",
            TEST_USER_ID,
            TEST_USER_EMAIL,
            writer_id,
        )
        _, admin, _, _ = await create_api_key(
            conn,
            user_id=TEST_USER_ID,
            name="provider",
            ceiling="admin",
        )
        _, writer, _, _ = await create_api_key(
            conn,
            user_id=writer_id,
            name="agent",
            ceiling="writer",
        )
    install_identity(make_test_identity(role="admin", api_key_id=None))
    box_a = await _joined(client, "box-a")
    box_b = await _joined(client, "box-b")
    clear_identity_override()
    return _Keys(admin=admin, writer=writer, box_a=box_a, box_b=box_b)


async def test_a_request_round_trips_from_machine_to_provider_and_back(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    keys: _Keys,
) -> None:
    client, _ = pglite_route_client

    created = await _create(client, "box-a", keys.box_a, body={"ref": "agent/x"})
    pending = await _list(client, "box-a", keys.admin, status="pending")
    finished = await client.post(
        _result_path("box-a", created.id),
        json={"status": "done", "result": {"sha": "abc123"}},
        headers=_bearer(keys.admin),
    )
    seen = await client.get(_one_path("box-a", created.id), headers=_bearer(keys.box_a))

    assert (created.kind, created.body, created.status) == (
        "push",
        {"ref": "agent/x"},
        "pending",
    )
    assert (created.result, created.created_by) == ({}, "box-a")
    assert [r.id for r in pending.requests] == [created.id]
    assert finished.status_code == 200, finished.text
    done = MachineRequest.model_validate_json(seen.text)
    assert (done.status, done.result, done.body) == (
        "done",
        {"sha": "abc123"},
        {"ref": "agent/x"},
    )
    assert done.updated >= done.created
    assert MachineRequest.model_validate_json(finished.text) == done
    assert (await _list(client, "box-a", keys.admin, status="pending")).requests == []


async def test_a_result_is_recorded_once_and_a_second_is_409(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    keys: _Keys,
) -> None:
    client, _ = pglite_route_client
    created = await _create(client, "box-a", keys.box_a)
    answer = {"status": "failed", "result": {"error": "not fast-forward"}}

    first = await client.post(
        _result_path("box-a", created.id),
        json=answer,
        headers=_bearer(keys.admin),
    )
    replay = await client.post(
        _result_path("box-a", created.id),
        json=answer,
        headers=_bearer(keys.admin),
    )
    other = await client.post(
        _result_path("box-a", created.id),
        json={"status": "done"},
        headers=_bearer(keys.admin),
    )

    assert (first.status_code, replay.status_code, other.status_code) == (
        200,
        409,
        409,
    )
    assert from_plain(replay.json(), dict[str, str])["code"] == "request_finished"
    kept = MachineRequest.model_validate_json(
        (
            await client.get(
                _one_path("box-a", created.id),
                headers=_bearer(keys.admin),
            )
        ).text,
    )
    assert (kept.status, kept.result) == ("failed", {"error": "not fast-forward"})


async def test_requests_list_oldest_first_and_filter_by_status(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    keys: _Keys,
) -> None:
    client, _ = pglite_route_client
    made = [await _create(client, "box-a", keys.box_a, kind=k) for k in "abc"]
    await client.post(
        _result_path("box-a", made[1].id),
        json={"status": "done"},
        headers=_bearer(keys.admin),
    )

    every = await _list(client, "box-a", keys.box_a)
    pending = await _list(client, "box-a", keys.box_a, status="pending")
    done = await _list(client, "box-a", keys.admin, status="done")
    bad = await client.get(
        MACHINE_REQUESTS_PATH.format(name="box-a"),
        params={"status": "running"},
        headers=_bearer(keys.admin),
    )

    assert [r.kind for r in every.requests] == ["a", "b", "c"]
    assert [r.kind for r in pending.requests] == ["a", "c"]
    assert [r.kind for r in done.requests] == ["b"]
    assert bad.status_code == 422


async def test_a_machine_never_reaches_another_machine_s_requests(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    keys: _Keys,
) -> None:
    client, _ = pglite_route_client
    theirs = await _create(client, "box-b", keys.box_b)
    a = _bearer(keys.box_a)

    responses = [
        await client.post(
            MACHINE_REQUESTS_PATH.format(name="box-b"),
            json={"kind": "push"},
            headers=a,
        ),
        await client.get(MACHINE_REQUESTS_PATH.format(name="box-b"), headers=a),
        await client.get(_one_path("box-b", theirs.id), headers=a),
        # Its own path, another machine's request id.
        await client.get(_one_path("box-a", theirs.id), headers=a),
    ]

    assert [r.status_code for r in responses] == [404] * 4
    assert len((await _list(client, "box-b", keys.admin)).requests) == 1


async def test_only_the_machine_creates_and_only_an_admin_answers(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    keys: _Keys,
) -> None:
    client, _ = pglite_route_client
    created = await _create(client, "box-a", keys.box_a)
    path = MACHINE_REQUESTS_PATH.format(name="box-a")

    admin_create = await client.post(
        path,
        json={"kind": "push"},
        headers=_bearer(keys.admin),
    )
    writer_list = await client.get(path, headers=_bearer(keys.writer))
    writer_one = await client.get(
        _one_path("box-a", created.id),
        headers=_bearer(keys.writer),
    )
    machine_answer = await client.post(
        _result_path("box-a", created.id),
        json={"status": "done"},
        headers=_bearer(keys.box_a),
    )
    writer_answer = await client.post(
        _result_path("box-a", created.id),
        json={"status": "done"},
        headers=_bearer(keys.writer),
    )
    nobody = await client.get(path)

    assert [
        r.status_code
        for r in (admin_create, writer_list, writer_one, machine_answer, writer_answer)
    ] == [401, 403, 403, 401, 403]
    assert nobody.status_code == 401
    still = await _list(client, "box-a", keys.admin)
    assert [(r.id, r.status) for r in still.requests] == [(created.id, "pending")]


async def test_absent_machines_and_requests_are_404(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    keys: _Keys,
) -> None:
    client, _ = pglite_route_client
    admin = _bearer(keys.admin)
    missing = uuid.uuid4()

    responses = [
        await client.get(MACHINE_REQUESTS_PATH.format(name="nope"), headers=admin),
        await client.get(_one_path("box-a", missing), headers=admin),
        await client.post(
            _result_path("box-a", missing),
            json={"status": "done"},
            headers=admin,
        ),
        await client.post(
            _result_path("nope", missing),
            json={"status": "done"},
            headers=admin,
        ),
    ]

    assert [r.status_code for r in responses] == [404] * 4


async def test_a_destroyed_machine_can_no_longer_ask_but_its_requests_stay(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    keys: _Keys,
) -> None:
    client, _ = pglite_route_client
    created = await _create(client, "box-a", keys.box_a)

    destroyed = await client.post(
        DESTROYED_PATH.format(name="box-a"),
        headers=_bearer(keys.admin),
    )
    refused = await client.post(
        MACHINE_REQUESTS_PATH.format(name="box-a"),
        json={"kind": "push"},
        headers=_bearer(keys.box_a),
    )
    read_back = await client.get(
        MACHINE_REQUESTS_PATH.format(name="box-a"),
        headers=_bearer(keys.box_a),
    )

    assert (destroyed.status_code, refused.status_code, read_back.status_code) == (
        204,
        410,
        410,
    )
    kept = await _list(client, "box-a", keys.admin)
    assert [r.id for r in kept.requests] == [created.id]


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "Push"},
        {"kind": "push", "body": []},
        {"kind": "push", "body": {"k": "a\x00b"}},
        {"kind": "push", "body": {"k": "v" * 70_000}},
        {"kind": "push", "extra": 1},
    ],
    ids=["kind", "list", "nul", "too-large", "extra"],
)
async def test_a_request_the_table_cannot_hold_is_422_and_stores_nothing(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    keys: _Keys,
    body: dict[str, object],
) -> None:
    client, _ = pglite_route_client

    response = await client.post(
        MACHINE_REQUESTS_PATH.format(name="box-a"),
        json=body,
        headers=_bearer(keys.box_a),
    )

    assert response.status_code == 422
    assert (await _list(client, "box-a", keys.admin)).requests == []


def _bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def _one_path(name: str, request_id: uuid.UUID) -> str:
    return MACHINE_REQUEST_PATH.format(name=name, request_id=request_id)


def _result_path(name: str, request_id: uuid.UUID) -> str:
    return MACHINE_REQUEST_RESULT_PATH.format(name=name, request_id=request_id)


async def _joined(client: httpx2.AsyncClient, name: str) -> str:
    """Enroll and join ``name``, returning its machine credential."""
    enrolled = await client.post(ENROLL_PATH, json={"name": name})
    assert enrolled.status_code == 201, enrolled.text
    joined = await client.post(
        JOIN_PATH,
        json={
            "name": name,
            "token": EnrollResponse.model_validate_json(enrolled.text).token,
            "instance": str(uuid.uuid4()),
            "host_version": "0.1",
        },
    )
    assert joined.status_code == 201, joined.text
    return JoinResponse.model_validate_json(joined.text).credential


async def _create(
    client: httpx2.AsyncClient,
    name: str,
    credential: str,
    *,
    kind: str = "push",
    body: dict[str, object] | None = None,
) -> MachineRequest:
    response = await client.post(
        MACHINE_REQUESTS_PATH.format(name=name),
        json={"kind": kind, "body": body or {}},
        headers=_bearer(credential),
    )
    assert response.status_code == 201, response.text
    return MachineRequest.model_validate_json(response.text)


async def _list(
    client: httpx2.AsyncClient,
    name: str,
    bearer: str,
    *,
    status: str | None = None,
) -> MachineRequestList:
    response = await client.get(
        MACHINE_REQUESTS_PATH.format(name=name),
        params={} if status is None else {"status": status},
        headers=_bearer(bearer),
    )
    assert response.status_code == 200, response.text
    return MachineRequestList.model_validate_json(response.text)


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
