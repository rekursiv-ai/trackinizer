"""Station creates racing one another on real PostgreSQL.

PGlite has one connection, so its requests never overlap: only a second backend
shows two creates choosing the same name, or two first opens inserting the first
station.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio

from treekle import from_plain, json_loads

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
from trackinizer.server.inbound import InboundQueue


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trackinizer.server.store.core import Store


pytestmark = [
    pytest.mark.db_postgres,
    pytest.mark.asyncio(loop_scope="session"),
]

_RACERS = 4


@pytest_asyncio.fixture(loop_scope="session")
async def served(integ_store: Store) -> AsyncIterator[httpx2.AsyncClient]:
    """Serve the whole app on the real engine, as a browser writer who has an account."""
    async with integ_store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, $2, 'writer', 'writer', 'active')",
            TEST_USER_ID,
            TEST_USER_EMAIL,
        )
    app.state.engine = integ_store.engine
    app.state.store = integ_store
    app.state.inbound = InboundQueue()
    install_identity(make_test_identity(api_key_id=None))
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
            await conn.execute("DELETE FROM users WHERE id = $1", TEST_USER_ID)
        del app.state.engine, app.state.store, app.state.inbound


async def _post_all(client: httpx2.AsyncClient, path: str) -> list[dict[str, object]]:
    """Post to ``path`` from several requests at once; return what each answered."""
    answers = await asyncio.gather(*(client.post(path) for _ in range(_RACERS)))
    assert [a.status_code for a in answers] == [200] * _RACERS
    return [json_loads(a.content, dict[str, object]) for a in answers]


async def test_racing_first_opens_make_one_station(
    served: httpx2.AsyncClient,
) -> None:
    stations = await _post_all(served, "/api/workspaces")

    listed = from_plain(
        json_loads((await served.get("/api/workspaces")).content),
        list[dict[str, object]],
    )
    assert {s["id"] for s in stations} == {s["id"] for s in listed}
    assert [s["name"] for s in listed] == ["unnamed-workstream#1"]
    assert {s["name"] for s in stations} == {"unnamed-workstream#1"}


async def test_racing_creates_take_distinct_names(
    served: httpx2.AsyncClient,
) -> None:
    stations = await _post_all(served, "/api/workspaces/new")

    assert sorted(from_plain(s["name"], str) for s in stations) == sorted(
        f"unnamed-workstream#{n}" for n in range(1, _RACERS + 1)
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
