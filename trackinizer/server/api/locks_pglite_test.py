"""A locked row changes only for an admin, on every write path, over PGlite.

The whole app answers each request, so the route dependencies and the domain-error
handlers are on the path. Each case is one write that touches a locked Issue: a
writer is refused with 403 and leaves the change log as it was, and an admin's
identical request succeeds.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Final, NamedTuple

import uuid

import pytest
import pytest_asyncio

from trackinizer.lib.codec import from_plain
from trackinizer.server.api.canvas_test_support import seed_accounts
from trackinizer.server.api.conftest import install_identity, make_test_identity
from trackinizer.server.sql import schema_migrations


if TYPE_CHECKING:
    import httpx2

    from trackinizer.server.auth import Role
    from trackinizer.server.store.core import Store


type _Client = tuple[httpx2.AsyncClient, Store]

_LOCKED_MESSAGE: Final = "is locked"


class _Rows(NamedTuple):
    """The Issues and the Artifact a case acts on."""

    locked: uuid.UUID
    """Locked once the edge ``child -> locked`` exists."""

    child: uuid.UUID
    """Narrows ``locked``; not locked itself."""

    other: uuid.UUID
    """Unrelated and not locked."""

    artifact: uuid.UUID
    """Published on ``other`` and locked."""


class _Write(NamedTuple):
    """One write request."""

    method: str
    path: str
    body: dict[str, object]
    keyed: bool = False
    """Whether the request carries a fresh ``Idempotency-Key``."""


type _Build = Callable[[_Rows], _Write]

_EDGE: Final = "/api/edges/{}/narrows/{}"
_FIELD: Final = "/api/inquiries/{}/{}"

_CASES: Final[dict[str, _Build]] = {
    "set a field": lambda r: _Write(
        "PUT",
        _FIELD.format(r.locked, "title"),
        {"value": "renamed"},
    ),
    "add a list element": lambda r: _Write(
        "PATCH",
        _FIELD.format(r.locked, "labels"),
        {"op": "add", "value": "x"},
    ),
    "compare-and-set a field": lambda r: _Write(
        "PUT",
        _FIELD.format(r.locked, "owner"),
        {"value": "w", "mode": "cas", "expected": None},
    ),
    "clear a field": lambda r: _Write(
        "DELETE",
        _FIELD.format(r.locked, "owner"),
        {},
    ),
    "add an edge out of it": lambda r: _Write(
        "POST",
        _EDGE.format(r.locked, r.other),
        {},
    ),
    "add an edge into it": lambda r: _Write(
        "POST",
        f"/api/edges/{r.other}/requires/{r.locked}",
        {},
    ),
    "annotate an edge": lambda r: _Write(
        "PUT",
        _EDGE.format(r.child, r.locked) + "/note",
        {"value": "n"},
    ),
    "clear an edge annotation": lambda r: _Write(
        "DELETE",
        _EDGE.format(r.child, r.locked) + "/note",
        {},
    ),
    "label an edge": lambda r: _Write(
        "PATCH",
        _EDGE.format(r.child, r.locked) + "/labels",
        {"op": "add", "value": "x"},
    ),
    "remove an edge": lambda r: _Write(
        "DELETE",
        _EDGE.format(r.child, r.locked),
        {},
    ),
    "add edges in a batch": lambda r: _Write(
        "POST",
        "/api/edges/batch",
        {
            "items": [
                {
                    "from_id": str(r.other),
                    "to_id": str(r.locked),
                    "edge_kind": "narrows",
                },
            ],
        },
    ),
    "create a row that narrows it": lambda r: _Write(
        "POST",
        "/api/inquiries/issue",
        {"title": "sub", "narrows": [[str(r.locked), None]]},
    ),
    "create a row that requires it": lambda r: _Write(
        "POST",
        "/api/inquiries/issue",
        {"title": "sub", "requires": [str(r.locked)]},
    ),
    "publish an Artifact for it": lambda r: _Write(
        "POST",
        "/api/artifacts/content",
        _publication(r.locked),
        keyed=True,
    ),
    "publish a revision of its Artifact": lambda r: _Write(
        "POST",
        "/api/artifacts/content",
        _publication(r.other) | {"previous_artifact_id": str(r.artifact)},
        keyed=True,
    ),
    "create rows in a batch linked to it": lambda r: _Write(
        "POST",
        "/api/inquiries/batch",
        {
            "items": [
                {"kind": "Issue", "title": "sub", "idempotency_key": str(uuid.uuid4())},
            ],
            "edges": [
                {"from_index": 0, "to_id": str(r.locked), "edge_kind": "narrows"},
            ],
        },
    ),
    # An admin's run deletes rows, so the purges come last.
    "purge the row": lambda r: _Write("DELETE", f"/api/inquiries/{r.locked}", {}),
    "purge a row linked to it": lambda r: _Write(
        "DELETE",
        f"/api/inquiries/{r.child}",
        {},
    ),
}


# One write per route family: the admin bypass is a single early return, so each
# family shows it is reached and the rest add only requests.
_ADMIN_CASES: Final = (
    "set a field",
    "add an edge out of it",
    "publish a revision of its Artifact",
    "create a row that narrows it",
    "purge the row",
)


@pytest_asyncio.fixture(loop_scope="session")
async def rows(pglite_route_client: _Client) -> _Rows:
    """Seed three Issues, the edge ``child -> locked`` and an Artifact; lock both."""
    client, store = pglite_route_client
    await seed_accounts(store)
    made = [await _issue(client, title) for title in ("locked", "child", "other")]
    _as("writer")
    edge = await client.post(_EDGE.format(made[1], made[0]), json={})
    assert edge.status_code == 200, edge.text
    published = await client.post(
        "/api/artifacts/content",
        json=_publication(made[2]),
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert published.status_code == 201, published.text
    artifact = uuid.UUID(
        from_plain(from_plain(published.json(), dict[str, object])["artifact_id"], str),
    )
    _as("admin")
    for target in (made[0], artifact):
        lock = await client.put(
            f"/api/admin/inquiries/{target}/lock",
            json={"locked": True},
        )
        assert lock.status_code == 200, lock.text
    return _Rows(locked=made[0], child=made[1], other=made[2], artifact=artifact)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_writer_cannot_change_a_locked_row(
    pglite_route_client: _Client,
    rows: _Rows,
) -> None:
    client, store = pglite_route_client
    before = await _changes(store)
    _as("writer")

    answers = {name: await _send(client, build(rows)) for name, build in _CASES.items()}

    assert {name: r.status_code for name, r in answers.items()} == dict.fromkeys(
        _CASES,
        403,
    )
    for response in answers.values():
        detail = from_plain(response.json(), dict[str, object])["detail"]
        assert _LOCKED_MESSAGE in from_plain(detail, str)
    assert await _changes(store) == before


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_admin_can_change_a_locked_row(
    pglite_route_client: _Client,
    rows: _Rows,
) -> None:
    client, _ = pglite_route_client
    _as("admin")

    answers = {name: await _send(client, _CASES[name](rows)) for name in _ADMIN_CASES}

    assert {name: r.status_code for name, r in answers.items()} == {
        "set a field": 200,
        "add an edge out of it": 200,
        "publish a revision of its Artifact": 201,
        "create a row that narrows it": 201,
        "purge the row": 200,
    }


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_writer_still_changes_rows_that_are_not_locked(
    pglite_route_client: _Client,
    rows: _Rows,
) -> None:
    client, _ = pglite_route_client
    _as("writer")

    renamed = await client.put(_FIELD.format(rows.other, "title"), json={"value": "ok"})
    linked = await client.post(_EDGE.format(rows.other, rows.child), json={})
    created = await client.post("/api/inquiries/issue", json={"title": "free"})

    assert (renamed.status_code, linked.status_code, created.status_code) == (
        200,
        200,
        201,
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_lock_is_admin_only_and_clears(
    pglite_route_client: _Client,
    rows: _Rows,
) -> None:
    client, store = pglite_route_client
    path = f"/api/admin/inquiries/{rows.other}/lock"
    _as("writer")
    refused = await client.put(path, json={"locked": True})
    _as("admin")
    set_ = await client.put(path, json={"locked": True})
    cleared = await client.put(path, json={"locked": False})
    missing = await client.put(
        f"/api/admin/inquiries/{uuid.uuid4()}/lock",
        json={"locked": True},
    )
    _as("writer")
    editable = await client.put(_FIELD.format(rows.other, "title"), json={"value": "t"})

    admin = make_test_identity(role="admin").email
    assert await _lock_log(store, rows.other) == [(True, admin), (False, admin)]
    assert refused.status_code == 403
    assert set_.json() == {"id": str(rows.other), "locked": True}
    assert cleared.json() == {"id": str(rows.other), "locked": False}
    assert missing.status_code == 404
    assert editable.status_code == 200


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_migration_locks_nothing(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    rules = await _issue(client, "Rules")
    later = await _issue(client, "Later")
    migration = dict(schema_migrations())["schema.038.sql"]

    async with store.engine.acquire() as conn:
        await conn.execute(migration)
        locked = await conn.fetch("SELECT id FROM inquiries WHERE locked")

    assert locked == []
    assert await _lock_log(store, rules) == []
    assert await _lock_log(store, later) == []


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_claiming_the_next_issue_skips_a_locked_one(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    _as("admin")
    only = await _issue(client, "only")
    _as("admin")
    await client.put(f"/api/admin/inquiries/{only}/lock", json={"locked": True})
    claim = {"owner": "worker"}

    skipped = await client.post(
        "/api/inquiries/next_issue",
        json=claim,
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    await client.put(f"/api/admin/inquiries/{only}/lock", json={"locked": False})
    taken = await client.post(
        "/api/inquiries/next_issue",
        json=claim,
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )

    assert skipped.json() is None
    assert from_plain(taken.json(), dict[str, object])["id"] == str(only)


def _as(role: Role) -> None:
    install_identity(make_test_identity(role=role, api_key_id=None))


async def _issue(client: httpx2.AsyncClient, title: str) -> uuid.UUID:
    _as("writer")
    response = await client.post("/api/inquiries/issue", json={"title": title})
    assert response.status_code == 201, response.text
    body = from_plain(response.json(), dict[str, object])
    return uuid.UUID(from_plain(body["id"], str))


async def _send(client: httpx2.AsyncClient, write: _Write) -> httpx2.Response:
    headers = {"Idempotency-Key": str(uuid.uuid4())} if write.keyed else {}
    return await client.request(
        write.method,
        write.path,
        json=write.body,
        headers=headers,
    )


def _publication(issue: uuid.UUID) -> dict[str, object]:
    """Build the smallest HTML publication for ``issue``."""
    return {
        "issue_id": str(issue),
        "title": "Report",
        "summary": "A report.",
        "format": "html",
        "html": "<p>report</p>",
    }


async def _lock_log(store: Store, target: uuid.UUID) -> list[tuple[bool, str]]:
    """List who locked or unlocked ``target``, oldest first."""
    async with store.engine.acquire() as conn:
        found = await conn.fetch(
            "SELECT locked, actor FROM inquiry_lock_log WHERE inquiry_id = $1 "
            "ORDER BY id",
            target,
        )
    return [(from_plain(r["locked"], bool), from_plain(r["actor"], str)) for r in found]


async def _changes(store: Store) -> set[uuid.UUID]:
    async with store.engine.acquire() as conn:
        rows = await conn.fetch("SELECT id FROM change_log")
    return {from_plain(row["id"], uuid.UUID) for row in rows}


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
