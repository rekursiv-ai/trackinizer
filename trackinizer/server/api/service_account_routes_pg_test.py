"""Minting a service account's key racing a disable or a delete, on real PostgreSQL.

PGlite has one connection, so it cannot show a write that lands after a concurrent
commit. Here a mint is held between its account read and its key insert while the
account is disabled or deleted on another backend: the mint must hold the account row
so the other statement waits, and no key outlives it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio
import uuid

from treekle import from_plain

import httpx2
import pytest
import pytest_asyncio

from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import (
    TEST_USER_EMAIL,
    TEST_USER_ID,
    clear_identity_override,
    install_identity,
    make_test_identity,
)
from trackinizer.server.auth import create_api_key
from trackinizer.server.inbound import InboundQueue


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trackinizer.lib.postgres import Conn
    from trackinizer.server.auth import Role
    from trackinizer.server.store.core import Store


pytestmark = [
    pytest.mark.db_postgres,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.usefixtures("cheap_scrypt"),
]

_ACCOUNTS = "/api/admin/service-accounts"

# Long enough for a request on the main loop to reach the lock it waits on.
_SETTLE_SEC = 0.5


@pytest.fixture
def cheap_scrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hash secrets at the smallest accepted cost so a test pays no scrypt round."""
    monkeypatch.setattr("trackinizer.server.auth._SCRYPT_N", 2)


@pytest_asyncio.fixture(loop_scope="session")
async def served(integ_store: Store) -> AsyncIterator[httpx2.AsyncClient]:
    """Serve the whole app on the real engine, as an admin who has an account."""
    async with integ_store.engine.acquire() as conn:
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
        del app.state.engine, app.state.store, app.state.inbound


@pytest.mark.parametrize("other", ["disable", "delete"])
async def test_a_disable_or_delete_in_flight_with_a_mint_leaves_no_live_key(
    served: httpx2.AsyncClient,
    integ_store: Store,
    monkeypatch: pytest.MonkeyPatch,
    other: str,
) -> None:
    created = await served.post(_ACCOUNTS, json={"name": "racer", "role": "writer"})
    account_id = str(from_plain(created.json(), dict[str, object])["id"])
    reached = asyncio.Event()
    release = asyncio.Event()

    async def paused_create(
        conn: Conn,
        *,
        user_id: uuid.UUID,
        name: str,
        role: Role,
        ceiling: Role,
        open_into: str | None,
    ) -> tuple[uuid.UUID, str, str, Role]:
        reached.set()
        await release.wait()
        return await create_api_key(
            conn,
            user_id=user_id,
            name=name,
            role=role,
            ceiling=ceiling,
            open_into=open_into,
        )

    monkeypatch.setattr(
        "trackinizer.server.api.service_account_routes.create_api_key",
        paused_create,
    )
    minting = asyncio.create_task(
        served.post(f"{_ACCOUNTS}/{account_id}/tokens", json={"name": "t"}),
    )
    await reached.wait()
    path = f"/api/admin/users/{account_id}"
    racing = asyncio.create_task(
        served.delete(path) if other == "delete" else served.post(f"{path}/disable"),
    )
    await asyncio.sleep(_SETTLE_SEC)
    waited = not racing.done()
    release.set()
    minted, raced = await asyncio.gather(minting, racing)

    async with integ_store.engine.acquire() as conn:
        live = await conn.fetchval(
            "SELECT count(*) FROM api_keys WHERE user_id = $1 AND revoked_at IS NULL",
            uuid.UUID(account_id),
        )
    assert waited
    assert minted.status_code == 201, minted.text
    assert raced.status_code in {200, 204}, raced.text
    assert live == 0


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
