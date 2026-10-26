"""An admin submits rows with their historical ``created`` and ``modified``, over PGlite.

The whole app answers each request, so the role check, the body validation and the
store's insert are all on the path. Each backdated row must read back with the times
sent, even after the edges written in the same request touch it. Its ``created``
change keeps the server clock, as for any backfill, so a cursor still finds it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import datetime as dt
import uuid

from treekle import from_plain

import pytest

from trackinizer.server.api.canvas_test_support import seed_accounts
from trackinizer.server.api.conftest import install_identity, make_test_identity


if TYPE_CHECKING:
    from collections.abc import Mapping

    import httpx2

    from trackinizer.server.auth import Role
    from trackinizer.server.store.core import Store


type _Client = tuple[httpx2.AsyncClient, Store]

_CREATED = dt.datetime(2024, 3, 1, 12, 30, tzinfo=dt.UTC)
# PGlite's clock ticks in whole milliseconds, so a stamp can read up to 1 ms before a
# microsecond Python reading taken just ahead of it.
_TICK = dt.timedelta(milliseconds=1)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_admin_backdates_one_row(pglite_route_client: _Client) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    broader = await _post(client, "issue", {"title": "broader"}, role="writer")
    modified = _CREATED + dt.timedelta(days=3)
    body = {
        "title": "old",
        "created": _CREATED.isoformat(),
        "modified": modified.isoformat(),
        # The narrows edge audits on the new row after its insert.
        "narrows": [[str(broader), None]],
        "idempotency_key": str(uuid.uuid4()),
    }
    before = dt.datetime.now(dt.UTC) - _TICK

    made = await _post(client, "issue", body, role="admin")
    retried = await _post(client, "issue", body, role="admin")

    assert retried == made
    assert await _times(store, made) == (_CREATED, modified)
    assert before <= await _created_change_time(store, made)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_admin_backdates_a_batch(pglite_route_client: _Client) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    later = _CREATED + dt.timedelta(hours=5)
    finished = later + dt.timedelta(days=1)
    batch = {
        "items": [
            {
                "kind": "Issue",
                "title": "direction",
                "created": _CREATED.isoformat(),
                "idempotency_key": str(uuid.uuid4()),
            },
            {
                "kind": "Experiment",
                "title": "run",
                "created": later.isoformat(),
                "modified": finished.isoformat(),
                "idempotency_key": str(uuid.uuid4()),
            },
        ],
        # Edges between the new rows are written after every item.
        "edges": [{"from_index": 1, "to_index": 0, "edge_kind": "produced_by"}],
    }

    install_identity(make_test_identity(role="admin", api_key_id=None))
    before = dt.datetime.now(dt.UTC) - _TICK
    first = await client.post("/api/inquiries/batch", json=batch)
    retry = await client.post("/api/inquiries/batch", json=batch)

    assert first.status_code == 200, first.text
    assert retry.json() == first.json()
    direction, run = (
        uuid.UUID(row_id)
        for row_id in from_plain(
            from_plain(first.json(), dict[str, object])["ids"],
            list[str],
        )
    )
    created, modified = await _times(store, direction)
    assert created == _CREATED
    assert before <= modified
    assert await _times(store, run) == (later, finished)
    assert before <= await _created_change_time(store, direction)
    assert before <= await _created_change_time(store, run)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_retried_batch_keeps_a_later_edit(pglite_route_client: _Client) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    batch = {
        "items": [
            {
                "kind": "Issue",
                "title": "old",
                "created": _CREATED.isoformat(),
                "modified": (_CREATED + dt.timedelta(days=1)).isoformat(),
                "idempotency_key": str(uuid.uuid4()),
            },
        ],
    }
    install_identity(make_test_identity(role="admin", api_key_id=None))
    first = await client.post("/api/inquiries/batch", json=batch)
    (made,) = from_plain(from_plain(first.json(), dict[str, object])["ids"], list[str])
    edit = await client.put(f"/api/inquiries/{made}/title", json={"value": "new"})
    _, edited = await _times(store, uuid.UUID(made))

    retry = await client.post("/api/inquiries/batch", json=batch)

    assert (first.status_code, edit.status_code, retry.status_code) == (200, 200, 200)
    assert edited > _CREATED
    assert await _times(store, uuid.UUID(made)) == (_CREATED, edited)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_writer_cannot_set_modified(pglite_route_client: _Client) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    install_identity(make_test_identity(role="writer", api_key_id=None))
    times = {
        "created": _CREATED.isoformat(),
        "modified": (_CREATED + dt.timedelta(days=1)).isoformat(),
    }

    single = await client.post("/api/inquiries/issue", json={"title": "t", **times})
    batch = await client.post(
        "/api/inquiries/batch",
        json={
            "items": [
                {"kind": "Issue", "title": "t", "idempotency_key": str(uuid.uuid4())},
                {
                    "kind": "Issue",
                    "title": "u",
                    "idempotency_key": str(uuid.uuid4()),
                    **times,
                },
            ],
        },
    )

    assert (single.status_code, batch.status_code) == (403, 403)
    async with store.engine.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM inquiries") == 0


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_no_times_stamp_the_wall_clock(pglite_route_client: _Client) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    before = dt.datetime.now(dt.UTC) - _TICK

    made = await _post(client, "issue", {"title": "now"}, role="writer")

    after = dt.datetime.now(dt.UTC) + _TICK
    created, modified = await _times(store, made)
    change_time = await _created_change_time(store, made)
    assert before <= created <= modified <= after
    assert created <= change_time <= after


async def _post(
    client: httpx2.AsyncClient,
    kind: str,
    body: Mapping[str, object],
    *,
    role: Role,
) -> uuid.UUID:
    install_identity(make_test_identity(role=role, api_key_id=None))
    response = await client.post(f"/api/inquiries/{kind}", json=body)
    assert response.status_code == 201, response.text
    return uuid.UUID(
        from_plain(from_plain(response.json(), dict[str, object])["id"], str),
    )


async def _times(
    store: Store,
    row_id: uuid.UUID,
) -> tuple[dt.datetime, dt.datetime]:
    """Read back one row's ``created`` and ``modified``."""
    row = await store.get_inquiry(row_id)
    assert row is not None
    return row.created, row.modified


async def _created_change_time(store: Store, row_id: uuid.UUID) -> dt.datetime:
    """Read the time on ``row_id``'s ``created`` change."""
    async with store.engine.acquire() as conn:
        found = await conn.fetchval(
            "SELECT created FROM change_log WHERE subject_id = $1 AND kind = 'created'",
            row_id,
        )
    return from_plain(found, dt.datetime)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
