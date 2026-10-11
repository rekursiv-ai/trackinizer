"""Provisioning racing itself on real PostgreSQL.

PGlite has one connection, so concurrent provisions there run one after another
and cannot show two transactions each counting the other out. Here every request
gets its own backend, and the limit must still hold exactly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio

from treekle import from_plain

import httpx2
import pytest
import pytest_asyncio

from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import (
    clear_identity_override,
    install_identity,
    make_test_identity,
)
from trackinizer.server.config import Config
from trackinizer.server.inbound import InboundQueue
from trackinizer.wire.wire_machines import MACHINES_PATH


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trackinizer.server.store.core import Store


pytestmark = [
    pytest.mark.db_postgres,
    pytest.mark.asyncio(loop_scope="session"),
]


_LIMIT = 3

_RACERS = 12

_ROUNDS = 4


@pytest_asyncio.fixture(loop_scope="session")
async def served(
    integ_store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[httpx2.AsyncClient]:
    """Serve the whole app on the real engine, as a writer, with a small limit."""
    async with integ_store.engine.acquire() as conn:
        await conn.execute("TRUNCATE machines CASCADE")
    app.state.engine = integ_store.engine
    app.state.store = integ_store
    app.state.inbound = InboundQueue()
    monkeypatch.setattr(
        app.state,
        "config",
        Config(machine_limit=_LIMIT),
        raising=False,
    )
    install_identity(make_test_identity(role="writer", api_key_id=None))
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


async def test_concurrent_provisions_never_pass_the_limit(
    served: httpx2.AsyncClient,
    integ_store: Store,
) -> None:
    for round_number in range(_ROUNDS):
        async with integ_store.engine.acquire() as conn:
            await conn.execute("TRUNCATE machines CASCADE")

        responses = await asyncio.gather(
            *(
                served.post(MACHINES_PATH, json={"provider": "cloud"})
                for _ in range(_RACERS)
            ),
        )

        async with integ_store.engine.acquire() as conn:
            stored = await conn.fetchval("SELECT count(*) FROM machines")
        codes = sorted(r.status_code for r in responses)
        assert (codes, stored) == (
            [201] * _LIMIT + [409] * (_RACERS - _LIMIT),
            _LIMIT,
        ), round_number
        refused = [r for r in responses if r.status_code == 409]
        assert {from_plain(r.json(), dict[str, str])["code"] for r in refused} == {
            "machine_limit",
        }


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
