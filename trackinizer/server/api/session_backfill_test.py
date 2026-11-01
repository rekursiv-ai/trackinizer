"""Historical imports: a date the database cannot hold is refused before any write."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import uuid

from treekle import from_plain, json_loads

import pytest

from trackinizer.server.api.session_test_support import seed_accounts


if TYPE_CHECKING:
    import httpx2

    from trackinizer.server.store.core import Store


type _Client = tuple[httpx2.AsyncClient, Store]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize(
    "created",
    [
        "0001-01-01T00:00:00Z",
        "0001-01-01T01:00:00+01:00",
        "0001-01-01T00:00:00+01:00",
    ],
)
async def test_unrepresentable_backfill_is_rejected_before_writing(
    pglite_route_client: _Client,
    created: str,
    batch: bool,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    async with store.engine.acquire() as conn:
        before_rows = await conn.fetch("SELECT * FROM inquiries ORDER BY id")
        before_changes = await conn.fetch("SELECT * FROM change_log ORDER BY id")
    item = {
        "kind": "Issue",
        "title": "Unrepresentable history",
        "created": created,
        "idempotency_key": str(uuid.uuid4()),
    }
    if batch:
        response = await client.post(
            "/api/inquiries/batch",
            json={
                "items": [
                    {
                        "kind": "Issue",
                        "title": "Must not be partially created",
                        "idempotency_key": str(uuid.uuid4()),
                    },
                    item,
                ],
            },
        )
    else:
        response = await client.post("/api/inquiries/issue", json=item)

    assert response.status_code == 422, response.text
    async with store.engine.acquire() as conn:
        assert await conn.fetch("SELECT * FROM inquiries ORDER BY id") == before_rows
        assert (
            await conn.fetch("SELECT * FROM change_log ORDER BY id") == before_changes
        )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    "created",
    [
        "0001-01-01T00:00:00.000001Z",
        "0001-01-01T01:00:00.000001+01:00",
        "2020-01-01T00:00:00Z",
    ],
)
async def test_finite_historical_dates_preserve_normal_edge_creation(
    pglite_route_client: _Client,
    created: str,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    ids: list[uuid.UUID] = []
    for title, instant in (("Historical", created), ("Current", None)):
        response = await client.post(
            "/api/inquiries/issue",
            json={"title": title, "created": instant},
        )
        assert response.status_code == 201, response.text
        ids.append(
            uuid.UUID(
                from_plain(json_loads(response.content, dict[str, object])["id"], str),
            ),
        )

    linked = await client.post(f"/api/edges/{ids[1]}/requires/{ids[0]}", json={})

    assert linked.status_code == 200, linked.text
    historical = await store.get_inquiry(ids[0])
    current = await store.get_inquiry(ids[1])
    assert historical is not None
    assert current is not None
    assert historical.created == datetime.fromisoformat(created)
    assert historical.created.tzinfo is not None
    assert [edge.id for edge in current.produced_by] == [historical.id]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
