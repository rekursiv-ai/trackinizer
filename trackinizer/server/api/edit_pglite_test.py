"""Field routes over the whole app and a real PGlite store."""

from __future__ import annotations

from typing import TYPE_CHECKING

from treekle import from_plain

import pytest

from trackinizer.server.api.session_test_support import seed_accounts


if TYPE_CHECKING:
    from collections.abc import Mapping

    import httpx2

    from trackinizer.server.store.core import Store


type _Client = tuple[httpx2.AsyncClient, Store]

pytestmark = [
    pytest.mark.db_pglite,
    pytest.mark.asyncio(loop_scope="session"),
]


async def test_any_rows_app_is_created_and_read_alike_on_both_routes(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)

    created = {"tracker": {"budget_usd": 20, "phase": "smoke"}}

    issue_id = await _row_with(client, kind="issue", app=created)

    assert await _row_app(client, issue_id, kind="Issue") == created


async def test_an_experiments_app_is_replaced_whole(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    experiment_id = await _row_with(
        client,
        kind="experiment",
        app={"tracker": {"verdict": "works"}},
    )
    replaced = {"tracker": {"verdict": "fails"}}

    put = await client.put(
        f"/api/inquiries/{experiment_id}/app",
        json={"value": replaced},
    )

    assert put.status_code == 200, put.text
    assert from_plain(put.json(), dict[str, str | None])["change_id"] is not None
    assert await _app_at(client, experiment_id=experiment_id) == replaced


async def test_an_experiments_app_is_cleared(pglite_route_client: _Client) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    experiment_id = await _row_with(
        client,
        kind="experiment",
        app={"tracker": {"verdict": "works"}},
    )

    cleared = await client.request(
        "DELETE",
        f"/api/inquiries/{experiment_id}/app",
        json={},
    )

    assert cleared.status_code == 200, cleared.text
    assert await _app_at(client, experiment_id=experiment_id) is None


async def test_an_app_that_is_not_one_object_of_at_most_64_kib_is_refused(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    posted = await client.post("/api/inquiries/experiment", json={"title": "run"})
    experiment_id = from_plain(posted.json(), dict[str, str])["id"]
    path = f"/api/inquiries/{experiment_id}/app"

    listed = await client.put(path, json={"value": ["works"]})
    oversized = await client.put(path, json={"value": {"b": "x" * 65_528}})
    created_oversized = await client.post(
        "/api/inquiries/experiment",
        json={"title": "run", "app": {"b": "x" * 65_528}},
    )

    assert listed.status_code == 422
    assert oversized.status_code == 409
    assert created_oversized.status_code == 409
    assert await _row_app(client, experiment_id) is None


async def test_an_apps_compare_and_set_refuses_a_stale_expectation(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    read = {"tracker": {"verdict": "works"}}
    corrected = {"tracker": {"verdict": "fails", "reason": "Within noise."}}
    posted = await client.post(
        "/api/inquiries/experiment",
        json={"title": "run", "app": read},
    )
    experiment_id = from_plain(posted.json(), dict[str, str])["id"]
    path = f"/api/inquiries/{experiment_id}/app"
    put = await client.put(path, json={"value": corrected})

    stale = await client.put(
        path,
        json={
            "value": {"tracker": {"verdict": "works", "patch": "p"}},
            "mode": "cas",
            "expected": read,
        },
    )
    after_stale = await _row_app(client, experiment_id)
    merged = {"tracker": {**corrected["tracker"], "patch": "p"}}
    fresh = await client.put(
        path,
        json={"value": merged, "mode": "cas", "expected": corrected},
    )

    assert put.status_code == 200, put.text
    assert stale.status_code == 409, stale.text
    assert after_stale == corrected
    assert fresh.status_code == 200, fresh.text
    assert await _row_app(client, experiment_id) == merged


async def _row_with(
    client: httpx2.AsyncClient,
    *,
    kind: str,
    app: Mapping[str, object],
) -> str:
    """File a row of ``kind`` holding ``app``; return its id."""
    posted = await client.post(
        f"/api/inquiries/{kind}",
        json={"title": "run", "app": app},
    )
    assert posted.status_code == 201, posted.text
    return from_plain(posted.json(), dict[str, str])["id"]


async def _app_at(client: httpx2.AsyncClient, *, experiment_id: str) -> object:
    """Read ``app`` from the inquiry route alone."""
    one = await client.get(f"/api/inquiries/{experiment_id}")
    return from_plain(one.json(), dict[str, object])["app"]


# The two must agree; the listing is the page a caller filters by kind.
async def _row_app(
    client: httpx2.AsyncClient,
    row_id: str,
    *,
    kind: str = "Experiment",
) -> object:
    """Read ``app`` from the inquiry route and from the listing of ``kind``."""
    one = await client.get(f"/api/inquiries/{row_id}")
    listed = await client.get("/api/inquiries", params={"kind": kind})
    [row] = [
        row
        for row in from_plain(listed.json(), list[dict[str, object]])
        if row["id"] == row_id
    ]
    app = from_plain(one.json(), dict[str, object])["app"]
    assert row["app"] == app
    return app


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
