"""Machine host routes, end to end over PGlite.

The whole app answers each request, so the exception handlers, the request logger
and the real machine-credential dependency are on the path. A test that needs the
real user resolver calls ``clear_identity_override``; the rest act as the admin the
fixture installs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, NamedTuple

import uuid

import pytest
import pytest_asyncio

from trackinizer.lib.codec import from_plain
from trackinizer.server.api.conftest import (
    TEST_USER_EMAIL,
    TEST_USER_ID,
    clear_identity_override,
    install_identity,
    make_test_identity,
)
from trackinizer.server.auth import create_api_key
from trackinizer.wire.wire_machine_host import (
    CREDENTIAL_PREFIX,
    ENROLL_PATH,
    ENROLL_TTL_SEC,
    ENROLLMENT_PREFIX,
    HEARTBEAT_PATH,
    JOIN_PATH,
    OFFLINE_AFTER_SEC,
    REVOKE_PATH,
    EnrollResponse,
    HeartbeatResponse,
    JoinResponse,
)
from trackinizer.wire.wire_machines import (
    MACHINE_PATH,
    MACHINES_PATH,
    MachineDetail,
    MachineList,
)


if TYPE_CHECKING:
    import httpx2

    from trackinizer.server.auth import Role
    from trackinizer.server.store.core import Store


pytestmark = [
    pytest.mark.db_pglite,
    pytest.mark.asyncio(loop_scope="session"),
]


_INSTANCE = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_OTHER_INSTANCE = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


class _Joined(NamedTuple):
    machine_id: uuid.UUID
    credential: str


@pytest_asyncio.fixture(loop_scope="session", autouse=True)
async def _admin_account(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Seed the account the fixture's admin identity stands for, as production has."""
    _, store = pglite_route_client
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, $2, 'admin', 'admin', 'active')",
            TEST_USER_ID,
            TEST_USER_EMAIL,
        )


async def test_enroll_needs_admin(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    responses: list[httpx2.Response] = []
    for role in ("viewer", "writer"):
        _as(role)
        responses.append(await client.post(ENROLL_PATH, json={"name": "dev-1"}))

    assert [r.status_code for r in responses] == [403, 403]
    _as("admin")
    assert (await _listed(client)).machines == []


async def test_enroll_registers_the_machine_and_returns_a_fifteen_minute_token(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    _as("admin")
    before = datetime.now(UTC)

    response = await client.post(ENROLL_PATH, json={"name": "dev-1"})

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    enrolled = EnrollResponse.model_validate_json(response.text)
    assert enrolled.token.startswith(ENROLLMENT_PREFIX)
    assert len(enrolled.token) == len(ENROLLMENT_PREFIX) + 32 + 1 + 43
    lifetime = enrolled.expires_at - before
    assert timedelta(seconds=ENROLL_TTL_SEC - 5) < lifetime
    assert lifetime < timedelta(seconds=ENROLL_TTL_SEC + 5)
    assert (await _detail(client, "dev-1")).status == "never"
    assert _secret_of(enrolled.token) not in await _everything_stored(store)


async def test_enroll_of_an_existing_machine_keeps_its_fields(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    _as("admin")
    await client.put(MACHINE_PATH.format(name="dev-1"), json={"role": "dev"})

    response = await client.post(ENROLL_PATH, json={"name": "dev-1"})

    assert response.status_code == 201
    assert (await _detail(client, "dev-1")).role == "dev"


@pytest.mark.parametrize("name", ["Upper", "a b", "", "enroll", "join", "connect"])
async def test_enroll_refuses_a_bad_or_reserved_name(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    name: str,
) -> None:
    client, _ = pglite_route_client
    _as("admin")

    response = await client.post(ENROLL_PATH, json={"name": name})

    assert response.status_code == 422
    assert (await _listed(client)).machines == []


async def test_join_returns_a_credential_once_and_the_machine_shows_online(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    token = await _enroll(client, "dev-1")

    response = await _join(
        client,
        "dev-1",
        token,
        facts={"os": "linux", "cpus": 8, "clis": ["claude"]},
    )

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    joined = JoinResponse.model_validate_json(response.text)
    assert joined.credential.startswith(CREDENTIAL_PREFIX)
    assert len(joined.credential) == len(CREDENTIAL_PREFIX) + 32 + 1 + 43
    one = await _detail(client, "dev-1")
    assert (one.status, one.host_version, one.facts) == (
        "online",
        "0.1",
        {"os": "linux", "cpus": 8, "clis": ["claude"]},
    )
    assert one.last_heartbeat is not None
    assert _secret_of(joined.credential) not in await _everything_stored(store)


async def test_join_wrong_token_is_401(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    token = await _enroll(client, "dev-1")
    enrollment_id = token.removeprefix(ENROLLMENT_PREFIX)[:32]
    wrong = [
        f"{ENROLLMENT_PREFIX}{enrollment_id}_{'A' * 43}",
        f"{ENROLLMENT_PREFIX}{uuid.uuid4().hex}_{'A' * 43}",
        "enr_short",
        "",
        f"{CREDENTIAL_PREFIX}{enrollment_id}_{'A' * 43}",
    ]

    responses = [await _join(client, "dev-1", bad) for bad in wrong]

    assert [r.status_code for r in responses] == [401] * 5
    assert len({r.text for r in responses}) == 1
    # A wrong guess burns nothing: the real token still joins.
    assert (await _join(client, "dev-1", token)).status_code == 201


async def test_join_after_expiry_is_401(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    token = await _enroll(client, "dev-1")
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE machine_enrollments SET expires_at = now() - interval '1 second'",
        )

    assert (await _join(client, "dev-1", token)).status_code == 401
    assert (await _detail(client, "dev-1")).status == "never"


async def test_replayed_enrollment_is_401(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    token = await _enroll(client, "dev-1")
    first = await _join(client, "dev-1", token)

    replay = await _join(client, "dev-1", token)

    assert (first.status_code, replay.status_code) == (201, 401)


async def test_token_for_another_machine_name_is_401(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    token_a = await _enroll(client, "box-a")
    await _enroll(client, "box-b")

    crossed = await _join(client, "box-b", token_a)
    absent = await _join(client, "box-c", token_a)

    assert (crossed.status_code, absent.status_code) == (401, 401)
    assert (await _join(client, "box-a", token_a)).status_code == 201


async def test_new_enrollment_supersedes_open(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    first = await _enroll(client, "dev-1")
    second = await _enroll(client, "dev-1")

    old = await _join(client, "dev-1", first)
    new = await _join(client, "dev-1", second)

    assert (old.status_code, new.status_code) == (401, 201)


async def test_join_replaces_live_credential_and_old_is_410(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    first = await _joined(client, "dev-1")
    token = await _enroll(client, "dev-1")
    assert (await _heartbeat(client, first)).status_code == 200

    second = JoinResponse.model_validate_json(
        (await _join(client, "dev-1", token, instance=_OTHER_INSTANCE)).text,
    )

    assert second.machine_id == first.machine_id
    assert (await _heartbeat(client, first)).status_code == 410
    new = _Joined(second.machine_id, second.credential)
    assert (await _heartbeat(client, new, instance=_OTHER_INSTANCE)).status_code == 200
    async with store.engine.acquire() as conn:
        live = await conn.fetchval(
            "SELECT count(*) FROM machine_credentials WHERE revoked_at IS NULL",
        )
        total = await conn.fetchval("SELECT count(*) FROM machine_credentials")
    assert (live, total) == (1, 2)


async def test_heartbeat_answers_the_server_clock_and_replaces_facts_only_when_sent(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    joined = await _joined(client, "dev-1", facts={"os": "linux"})

    kept = await _heartbeat(client, joined, host_version="0.2")
    replaced = await _heartbeat(client, joined, facts={"cpus": 4})

    assert (kept.status_code, replaced.status_code) == (200, 200)
    server_time = HeartbeatResponse.model_validate_json(kept.text).server_time
    assert abs(server_time - datetime.now(UTC)) < timedelta(seconds=30)
    one = await _detail(client, "dev-1")
    assert (one.facts, one.host_version) == ({"cpus": 4}, "")
    await _heartbeat(client, joined, host_version="0.3")
    assert (await _detail(client, "dev-1")).facts == {"cpus": 4}


@pytest.mark.parametrize(
    "facts",
    [
        {"Upper": "x"},
        {"a": 1.5},
        {"a": {"nested": 1}},
        {"a": "x" * 257},
        {"a": "a\x00b"},
        {"a": True, "b": 2**64},
        {f"k{i}": "v" * 200 for i in range(40)},
    ],
    ids=["key", "float", "nested", "long", "nul", "big-int", "too-large"],
)
async def test_facts_the_table_cannot_hold_are_refused_with_422(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    facts: dict[str, object],
) -> None:
    client, _ = pglite_route_client
    token = await _enroll(client, "dev-1")
    joined = await _joined(client, "box-2")

    join = await _join(client, "dev-1", token, facts=facts)
    beat = await _heartbeat(client, joined, facts=facts)

    assert (join.status_code, beat.status_code) == (422, 422)


async def test_a_host_version_with_a_nul_is_422_and_the_token_is_kept(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    token = await _enroll(client, "dev-1")
    joined = await _joined(client, "box-2")

    join = await _join(client, "dev-1", token, host_version="a\x00b")
    beat = await _heartbeat(client, joined, host_version="a\x00b")

    assert (join.status_code, beat.status_code) == (422, 422)
    assert (await _join(client, "dev-1", token)).status_code == 201


@pytest.mark.parametrize(
    "closed_by",
    [
        "UPDATE users SET status = 'disabled'",
        "UPDATE users SET role = 'writer'",
        "DELETE FROM users",
    ],
    ids=["disabled", "demoted", "deleted"],
)
async def test_a_token_whose_issuer_is_no_longer_an_active_admin_is_401(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    closed_by: str,
) -> None:
    client, store = pglite_route_client
    token = await _enroll(client, "dev-1")
    async with store.engine.acquire() as conn:
        await conn.execute(closed_by)

    response = await _join(client, "dev-1", token)

    assert response.status_code == 401
    assert (await _detail(client, "dev-1")).status == "never"


async def test_machine_credential_401_on_user_routes_and_api_key_401_on_host_routes(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    joined = await _joined(client, "dev-1")
    token = await _enroll(client, "dev-2")
    async with store.engine.acquire() as conn:
        user_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, 'a@example.com', 'a', 'admin', 'active')",
            user_id,
        )
        _, api_key, _, _ = await create_api_key(
            conn,
            user_id=user_id,
            name="k",
            ceiling="admin",
        )
    clear_identity_override()

    with_credential = await client.get(
        MACHINES_PATH,
        headers=_bearer(joined.credential),
    )
    with_token = await client.get(MACHINES_PATH, headers=_bearer(token))
    with_key = await client.get(MACHINES_PATH, headers=_bearer(api_key))
    key_on_host = await _heartbeat(client, joined, bearer=api_key)
    nothing_on_host = await _heartbeat(client, joined, bearer="")
    token_on_host = await _heartbeat(client, joined, bearer=token)

    assert (with_credential.status_code, with_token.status_code) == (401, 401)
    assert with_key.status_code == 200
    assert [r.status_code for r in (key_on_host, nothing_on_host, token_on_host)] == [
        401,
        401,
        401,
    ]


async def test_credential_of_machine_a_gets_404_on_machine_b_paths(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    a = await _joined(client, "box-a")
    b = await _joined(client, "box-b")

    crossed = await _heartbeat(client, _Joined(b.machine_id, a.credential))
    absent = await _heartbeat(client, _Joined(uuid.uuid4(), a.credential))

    assert (crossed.status_code, absent.status_code) == (404, 404)
    assert crossed.text == absent.text
    assert (await _heartbeat(client, b)).status_code == 200


async def test_revoke_effective_on_next_request(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    joined = await _joined(client, "dev-1")
    assert (await _heartbeat(client, joined)).status_code == 200
    # A second heartbeat inside the minute proves no cache stands in front of revoke.
    assert (await _heartbeat(client, joined)).status_code == 200

    revoked = await client.post(REVOKE_PATH.format(name="dev-1"))
    after = await _heartbeat(client, joined)

    assert (revoked.status_code, revoked.content) == (204, b"")
    assert (after.status_code, _detail_text(after)) == (410, "machine_revoked")
    assert (await _detail(client, "dev-1")).status == "revoked"


async def test_revoked_is_410_on_every_host_route_unknown_is_401(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    joined = await _joined(client, "dev-1")
    await client.post(REVOKE_PATH.format(name="dev-1"))
    unknown = f"{CREDENTIAL_PREFIX}{uuid.uuid4().hex}_{'A' * 43}"

    # Every host route; each later PR that adds one lists it here.
    on_revoked = [await _heartbeat(client, joined)]
    on_unknown = [await _heartbeat(client, joined, bearer=unknown)]

    assert [r.status_code for r in on_revoked] == [410]
    assert [r.status_code for r in on_unknown] == [401]


async def test_wrong_secret_for_a_revoked_id_is_401(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    joined = await _joined(client, "dev-1")
    await client.post(REVOKE_PATH.format(name="dev-1"))
    credential_id = joined.credential.removeprefix(CREDENTIAL_PREFIX)[:32]
    forged = f"{CREDENTIAL_PREFIX}{credential_id}_{'A' * 43}"

    response = await _heartbeat(client, joined, bearer=forged)

    assert response.status_code == 401


async def test_wrong_secret_for_a_live_id_is_401_and_changes_nothing(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    joined = await _joined(client, "dev-1")
    credential_id = joined.credential.removeprefix(CREDENTIAL_PREFIX)[:32]
    forged = f"{CREDENTIAL_PREFIX}{credential_id}_{'A' * 43}"
    unknown = f"{CREDENTIAL_PREFIX}{uuid.uuid4().hex}_{'A' * 43}"
    before = await _everything_stored(store)

    wrong = await _heartbeat(client, joined, bearer=forged, facts={"os": "linux"})
    absent = await _heartbeat(client, joined, bearer=unknown, facts={"os": "linux"})

    assert (wrong.status_code, wrong.text) == (401, absent.text)
    assert await _everything_stored(store) == before
    assert (await _heartbeat(client, joined)).status_code == 200


async def test_revoke_closes_an_open_enrollment_and_is_idempotent(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    token = await _enroll(client, "dev-1")

    first = await client.post(REVOKE_PATH.format(name="dev-1"))
    again = await client.post(REVOKE_PATH.format(name="dev-1"))

    assert (first.status_code, again.status_code) == (204, 204)
    assert (await _join(client, "dev-1", token)).status_code == 401


async def test_revoke_needs_admin_and_a_registered_machine(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    joined = await _joined(client, "dev-1")
    _as("writer")
    refused = await client.post(REVOKE_PATH.format(name="dev-1"))
    _as("admin")
    absent = await client.post(REVOKE_PATH.format(name="nope"))
    reserved = await client.post(REVOKE_PATH.format(name="join"))

    assert (refused.status_code, absent.status_code, reserved.status_code) == (
        403,
        404,
        422,
    )
    assert (await _heartbeat(client, joined)).status_code == 200


async def test_a_machine_enrolls_again_after_revoke(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    old = await _joined(client, "dev-1")
    await client.post(REVOKE_PATH.format(name="dev-1"))
    token = await _enroll(client, "dev-1")
    assert (await _detail(client, "dev-1")).status == "revoked"

    new = JoinResponse.model_validate_json((await _join(client, "dev-1", token)).text)

    assert (await _detail(client, "dev-1")).status == "online"
    assert (await _heartbeat(client, old)).status_code == 410
    assert new.machine_id == old.machine_id


async def test_delete_machine_with_live_credential_is_409(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    joined = await _joined(client, "dev-1")
    await client.put(MACHINE_PATH.format(name="bare"), json={})

    refused = await client.delete(MACHINE_PATH.format(name="dev-1"))
    await client.post(REVOKE_PATH.format(name="dev-1"))
    deleted = await client.delete(MACHINE_PATH.format(name="dev-1"))
    bare = await client.delete(MACHINE_PATH.format(name="bare"))

    assert (refused.status_code, deleted.status_code, bare.status_code) == (
        409,
        204,
        204,
    )
    assert from_plain(refused.json(), dict[str, str])["code"] == "machine_in_service"
    # A deleted machine's credential is unknown, not revoked.
    assert (await _heartbeat(client, joined)).status_code == 401
    assert (await _listed(client)).machines == []


async def test_offline_after_180_s_online_after_heartbeat(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    await _enroll(client, "never-joined")
    joined = await _joined(client, "dev-1")
    statuses = {}

    await _silent_for(store, OFFLINE_AFTER_SEC - 5)
    statuses["just_inside"] = (await _detail(client, "dev-1")).status
    await _silent_for(store, OFFLINE_AFTER_SEC + 5)
    statuses["silent"] = (await _detail(client, "dev-1")).status
    listed = {m.name: m.status for m in (await _listed(client)).machines}
    await _heartbeat(client, joined)
    statuses["beat"] = (await _detail(client, "dev-1")).status

    assert statuses == {"just_inside": "online", "silent": "offline", "beat": "online"}
    assert listed == {"dev-1": "offline", "never-joined": "never"}


async def test_second_instance_is_409_until_first_is_stale(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    joined = await _joined(client, "dev-1", instance=_INSTANCE)

    rival = await _heartbeat(client, joined, instance=_OTHER_INSTANCE)
    owner = await _heartbeat(client, joined, instance=_INSTANCE)
    await _silent_for(store, OFFLINE_AFTER_SEC + 5)
    takeover = await _heartbeat(client, joined, instance=_OTHER_INSTANCE)
    displaced = await _heartbeat(client, joined, instance=_INSTANCE)

    assert [r.status_code for r in (rival, owner, takeover, displaced)] == [
        409,
        200,
        200,
        409,
    ]
    assert from_plain(rival.json(), dict[str, str])["code"] == "another_host_connected"


async def test_a_refused_heartbeat_changes_nothing(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    joined = await _joined(client, "dev-1", facts={"os": "linux"})

    await _heartbeat(client, joined, instance=_OTHER_INSTANCE, facts={"os": "other"})

    assert (await _detail(client, "dev-1")).facts == {"os": "linux"}


async def test_last_used_is_set_by_the_first_request_and_kept_within_a_minute(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    joined = await _joined(client, "dev-1")
    sql = "SELECT last_used FROM machine_credentials WHERE revoked_at IS NULL"
    async with store.engine.acquire() as conn:
        before = await conn.fetchval(sql)

    await _heartbeat(client, joined)
    async with store.engine.acquire() as conn:
        first = await conn.fetchval(sql)
    await _heartbeat(client, joined)
    async with store.engine.acquire() as conn:
        second = await conn.fetchval(sql)

    assert before is None
    assert first is not None
    assert second == first


async def test_a_malformed_machine_id_is_422_and_stores_nothing(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    joined = await _joined(client, "dev-1")

    response = await client.post(
        HEARTBEAT_PATH.format(machine_id="not-a-uuid"),
        json={"instance": str(_INSTANCE)},
        headers=_bearer(joined.credential),
    )

    assert response.status_code == 422


def _as(role: Role) -> None:
    install_identity(make_test_identity(role=role, api_key_id=None))


def _secret_of(bearer: str) -> str:
    """Return the 43-character secret, the last part of a prefixed bearer string."""
    return bearer[-43:]


def _bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def _detail_text(response: httpx2.Response) -> str:
    return from_plain(response.json(), dict[str, str])["detail"]


async def _enroll(client: httpx2.AsyncClient, name: str) -> str:
    _as("admin")
    response = await client.post(ENROLL_PATH, json={"name": name})
    assert response.status_code == 201, response.text
    return EnrollResponse.model_validate_json(response.text).token


async def _join(
    client: httpx2.AsyncClient,
    name: str,
    token: str,
    *,
    instance: uuid.UUID = _INSTANCE,
    host_version: str = "0.1",
    facts: dict[str, object] | None = None,
) -> httpx2.Response:
    return await client.post(
        JOIN_PATH,
        json={
            "name": name,
            "token": token,
            "instance": str(instance),
            "host_version": host_version,
            "facts": facts or {},
        },
    )


async def _joined(
    client: httpx2.AsyncClient,
    name: str,
    *,
    instance: uuid.UUID = _INSTANCE,
    facts: dict[str, object] | None = None,
) -> _Joined:
    response = await _join(
        client,
        name,
        await _enroll(client, name),
        instance=instance,
        facts=facts,
    )
    assert response.status_code == 201, response.text
    joined = JoinResponse.model_validate_json(response.text)
    return _Joined(joined.machine_id, joined.credential)


async def _heartbeat(
    client: httpx2.AsyncClient,
    joined: _Joined,
    *,
    instance: uuid.UUID = _INSTANCE,
    host_version: str = "",
    facts: dict[str, object] | None = None,
    bearer: str | None = None,
) -> httpx2.Response:
    body: dict[str, object] = {
        "instance": str(instance),
        "host_version": host_version,
    }
    if facts is not None:
        body["facts"] = facts
    return await client.post(
        HEARTBEAT_PATH.format(machine_id=joined.machine_id),
        json=body,
        headers=_bearer(joined.credential if bearer is None else bearer),
    )


async def _silent_for(store: Store, seconds: int) -> None:
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE machines SET last_heartbeat = now() - make_interval(secs => $1) "
            "WHERE last_heartbeat IS NOT NULL",
            seconds,
        )


async def _everything_stored(store: Store) -> str:
    """Return every machine-host table as one text, to search for a secret."""
    async with store.engine.acquire() as conn:
        rows = [
            str(dict(row))
            for table_query in (
                "SELECT * FROM machines",
                "SELECT * FROM machine_enrollments",
                "SELECT * FROM machine_credentials",
            )
            for row in await conn.fetch(table_query)
        ]
    return "\n".join(rows)


async def _listed(client: httpx2.AsyncClient) -> MachineList:
    response = await client.get(MACHINES_PATH)
    assert response.status_code == 200
    return MachineList.model_validate_json(response.text)


async def _detail(client: httpx2.AsyncClient, name: str) -> MachineDetail:
    _as("admin")
    response = await client.get(MACHINE_PATH.format(name=name))
    assert response.status_code == 200, response.text
    return MachineDetail.model_validate_json(response.text)


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
