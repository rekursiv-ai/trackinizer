"""Machine host routes racing one another on real PostgreSQL.

PGlite has one connection, so it cannot show a lock-order deadlock or a write that
lands after a concurrent commit. Each test here holds or races a transaction on a
second backend, and every route that writes a machine must take the machine row
first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio
import uuid

import httpx2
import pytest
import pytest_asyncio

from trackinizer.lib.codec import from_plain
from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import (
    TEST_USER_EMAIL,
    TEST_USER_ID,
    clear_identity_override,
    install_identity,
    make_test_identity,
)
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.notify import tx
from trackinizer.wire.wire_machine_host import (
    ENROLL_PATH,
    ENROLLMENT_PREFIX,
    HEARTBEAT_PATH,
    JOIN_PATH,
    REVOKE_PATH,
    EnrollResponse,
    JoinResponse,
)
from trackinizer.wire.wire_machines import MACHINE_PATH


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trackinizer.lib.postgres import Conn
    from trackinizer.server.store.core import Store


pytestmark = [
    pytest.mark.db_postgres,
    pytest.mark.asyncio(loop_scope="session"),
]


_INSTANCE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

# Long enough for a request on the main loop to reach the lock it waits on.
_SETTLE_SEC = 0.3

# Long enough for a request that never waits on a lock to answer on a loaded host.
_ANSWER_SEC = 5

_ROUNDS = 8


@pytest_asyncio.fixture(loop_scope="session")
async def served(integ_store: Store) -> AsyncIterator[httpx2.AsyncClient]:
    """Serve the whole app on the real engine, as an admin who has an account."""
    async with integ_store.engine.acquire() as conn:
        await conn.execute("TRUNCATE machines CASCADE")
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, $2, 'admin', 'admin', 'active')",
            TEST_USER_ID,
            TEST_USER_EMAIL,
        )
    app.state.engine = integ_store.engine
    app.state.store = integ_store
    app.state.inbound = InboundQueue()
    install_identity(make_test_identity(role="admin", api_key_id=None))
    transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as http:
            yield http
    finally:
        clear_identity_override()
        async with integ_store.engine.acquire() as conn:
            await conn.execute("TRUNCATE machines CASCADE")
        del app.state.engine, app.state.store, app.state.inbound


async def test_join_waits_for_a_route_holding_the_machine_row_instead_of_deadlocking(
    served: httpx2.AsyncClient,
    integ_store: Store,
) -> None:
    token = await _enroll(served, "dev-1")

    async with integ_store.engine.acquire() as holder, tx(holder):
        machine_id = await _lock_machine(holder, "dev-1")
        joining = asyncio.create_task(_join(served, "dev-1", token))
        await asyncio.sleep(_SETTLE_SEC)
        # What revoke does next: close the open enrollment.
        await holder.execute(
            "UPDATE machine_enrollments SET used_at = now() WHERE machine_id = $1",
            machine_id,
        )

    assert (await joining).status_code == 401


async def test_join_with_a_junk_token_is_401_without_waiting_for_the_machine_row(
    served: httpx2.AsyncClient,
    integ_store: Store,
) -> None:
    token = await _enroll(served, "dev-1")
    junk = [
        # An id no enrollment has, and a real id with the wrong secret.
        f"{ENROLLMENT_PREFIX}{uuid.uuid4().hex}_{'A' * 43}",
        f"{token[:-43]}{'A' * 43}",
    ]

    async with integ_store.engine.acquire() as holder, tx(holder):
        await _lock_machine(holder, "dev-1")
        joining = [asyncio.create_task(_join(served, "dev-1", bad)) for bad in junk]
        # Judged while the lock is still held: a join queued behind it never answers.
        answered, _ = await asyncio.wait(joining, timeout=_ANSWER_SEC)
    responses = await asyncio.gather(*joining)

    assert (len(answered), [r.status_code for r in responses]) == (2, [401, 401])


async def test_two_joins_with_one_token_that_both_passed_the_check_make_one_credential(
    served: httpx2.AsyncClient,
    integ_store: Store,
) -> None:
    token = await _enroll(served, "dev-1")

    async with integ_store.engine.acquire() as holder, tx(holder):
        await _lock_machine(holder, "dev-1")
        joining = [asyncio.create_task(_join(served, "dev-1", token)) for _ in range(2)]
        # Both read the token as open, and now queue for the row.
        await asyncio.sleep(_SETTLE_SEC)
    responses = await asyncio.gather(*joining)

    async with integ_store.engine.acquire() as conn:
        credentials = await conn.fetchval("SELECT count(*) FROM machine_credentials")
    assert (sorted(r.status_code for r in responses), credentials) == ([201, 401], 1)


@pytest.mark.parametrize("other", ["enroll", "revoke", "delete"])
async def test_join_racing_another_route_on_the_machine_is_never_a_500(
    served: httpx2.AsyncClient,
    other: str,
) -> None:
    for round_number in range(_ROUNDS):
        name = f"dev-{round_number}"
        token = await _enroll(served, name)

        joined, raced = await asyncio.gather(
            _join(served, name, token),
            _race(served, other, name),
        )

        assert joined.status_code in {201, 401}, (other, joined.text)
        assert raced.status_code in {201, 204, 409}, (other, raced.text)


async def test_enroll_racing_a_delete_of_its_machine_is_never_a_500(
    served: httpx2.AsyncClient,
) -> None:
    for round_number in range(_ROUNDS):
        name = f"dev-{round_number}"
        await _enroll(served, name)

        enrolled, deleted = await asyncio.gather(
            _race(served, "enroll", name),
            _delete(served, name),
        )

        assert (enrolled.status_code, deleted.status_code) == (201, 204), (
            enrolled.text,
            deleted.text,
        )


async def test_heartbeat_in_flight_during_a_revoke_is_410_and_writes_nothing(
    served: httpx2.AsyncClient,
    integ_store: Store,
) -> None:
    joined = await _joined(served, "dev-1")

    async with integ_store.engine.acquire() as holder, tx(holder):
        machine_id = await _lock_machine(holder, "dev-1")
        # The credential row is revoked and the host cleared, and not yet committed.
        await holder.execute(
            "UPDATE machine_credentials SET revoked_at = now() WHERE machine_id = $1",
            machine_id,
        )
        await holder.execute(
            "UPDATE machines SET host_instance = NULL WHERE id = $1",
            machine_id,
        )
        beating = asyncio.create_task(_heartbeat(served, joined))
        await asyncio.sleep(_SETTLE_SEC)

    response = await beating
    async with integ_store.engine.acquire() as conn:
        host_instance = await conn.fetchval(
            "SELECT host_instance FROM machines WHERE id = $1",
            machine_id,
        )
    assert (response.status_code, host_instance) == (410, None)


async def test_delete_racing_a_join_that_commits_first_is_409(
    served: httpx2.AsyncClient,
    integ_store: Store,
) -> None:
    await _enroll(served, "dev-1")

    async with integ_store.engine.acquire() as holder, tx(holder):
        machine_id = await _lock_machine(holder, "dev-1")
        deleting = asyncio.create_task(_delete(served, "dev-1"))
        await asyncio.sleep(_SETTLE_SEC)
        # What a join commits: a live credential for the machine.
        await holder.execute(
            "INSERT INTO machine_credentials (machine_id, secret_sha256) "
            "VALUES ($1, $2)",
            machine_id,
            bytes(32),
        )

    assert (await deleting).status_code == 409
    async with integ_store.engine.acquire() as conn:
        kept = await conn.fetchval("SELECT id FROM machines WHERE id = $1", machine_id)
    assert kept == machine_id


async def _lock_machine(conn: Conn, name: str) -> uuid.UUID:
    """Take the machine row's lock in the open transaction, as each writer does."""
    return from_plain(
        await conn.fetchval(
            "SELECT id FROM machines WHERE name = $1 FOR UPDATE",
            name,
        ),
        uuid.UUID,
    )


async def _race(http: httpx2.AsyncClient, route: str, name: str) -> httpx2.Response:
    """Send the named route's request for one machine."""
    if route == "enroll":
        return await http.post(ENROLL_PATH, json={"name": name})
    if route == "revoke":
        return await http.post(REVOKE_PATH.format(name=name))
    return await _delete(http, name)


async def _delete(http: httpx2.AsyncClient, name: str) -> httpx2.Response:
    return await http.delete(MACHINE_PATH.format(name=name))


async def _enroll(http: httpx2.AsyncClient, name: str) -> str:
    response = await http.post(ENROLL_PATH, json={"name": name})
    assert response.status_code == 201, response.text
    return EnrollResponse.model_validate_json(response.text).token


async def _join(http: httpx2.AsyncClient, name: str, token: str) -> httpx2.Response:
    return await http.post(
        JOIN_PATH,
        json={
            "name": name,
            "token": token,
            "instance": _INSTANCE,
            "host_version": "0.1",
            "facts": {},
        },
    )


async def _joined(http: httpx2.AsyncClient, name: str) -> JoinResponse:
    response = await _join(http, name, await _enroll(http, name))
    assert response.status_code == 201, response.text
    return JoinResponse.model_validate_json(response.text)


async def _heartbeat(http: httpx2.AsyncClient, joined: JoinResponse) -> httpx2.Response:
    return await http.post(
        HEARTBEAT_PATH.format(machine_id=joined.machine_id),
        json={"instance": _INSTANCE, "host_version": "0.1"},
        headers={"Authorization": f"Bearer {joined.credential}"},
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
