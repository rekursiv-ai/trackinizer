"""Machine routes, end to end over PGlite.

The whole app answers each request, so the exception handlers and the request
logger are on the path, and the table's own checks stand behind the routes'.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import quote

import pytest

from trackinizer.lib.codec import from_plain
from trackinizer.server.api.conftest import (
    TEST_USER_EMAIL,
    install_identity,
    make_test_identity,
)
from trackinizer.wire.wire_machines import (
    MACHINE_LABELS_PATH,
    MACHINE_PATH,
    MACHINES_PATH,
    MAX_HOW_CHARS,
    RESERVED_NAMES,
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


async def test_a_writer_lists_and_reads_machines_sorted_by_name(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    _as("admin")
    for name in ("b-box", "ab-box", "a-box", "9lives"):
        assert (await client.put(_path(name), json={})).status_code == 204
    await client.put(_path("a-box"), json={"role": "dev", "how": "ssh a-box"})
    _as("writer")

    listed = await _listed(client)
    one = await _one(client, "a-box")

    assert [m.name for m in listed.machines] == ["9lives", "a-box", "ab-box", "b-box"]
    assert (one.role, one.how, one.labels, one.updated_by) == (
        "dev",
        "ssh a-box",
        [],
        TEST_USER_EMAIL,
    )


async def test_a_viewer_cannot_read(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    _as("admin")
    await client.put(_path("dev-1"), json={})
    _as("viewer")

    assert (await client.get(MACHINES_PATH)).status_code == 403
    assert (await client.get(_path("dev-1"))).status_code == 403


async def test_only_an_admin_can_write_or_delete(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    _as("admin")
    await client.put(_path("dev-1"), json={"role": "dev"})
    _as("writer")

    put = await client.put(_path("dev-1"), json={"role": "ops"})
    labels = await client.patch(_labels_path("dev-1"), json={"add": ["gpu"]})
    delete = await client.delete(_path("dev-1"))

    assert (put.status_code, labels.status_code, delete.status_code) == (403,) * 3
    one = await _one(client, "dev-1")
    assert (one.role, one.labels) == ("dev", [])


async def test_a_put_creates_then_a_partial_put_keeps_the_other_field(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    _as("admin")

    created = await client.put(_path("dev-1"), json={"role": "dev"})
    only_how = await client.put(_path("dev-1"), json={"how": "ssh dev-1"})
    nothing = await client.put(_path("dev-1"), json={})

    assert (created.status_code, created.content) == (204, b"")
    assert (only_how.status_code, nothing.status_code) == (204, 204)
    one = await _one(client, "dev-1")
    assert (one.role, one.how) == ("dev", "ssh dev-1")


async def test_an_empty_string_clears_a_field_and_a_put_records_who(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    _as("admin")
    await client.put(_path("dev-1"), json={"role": "dev", "how": "ssh dev-1"})
    async with store.engine.acquire() as conn:
        before = await conn.fetchrow("SELECT id, created, updated FROM machines")
    install_identity(
        make_test_identity(role="admin", email="other@example.com", api_key_id=None),
    )

    await client.put(_path("dev-1"), json={"how": ""})

    one = await _one(client, "dev-1")
    assert (one.role, one.how, one.updated_by) == ("dev", "", "other@example.com")
    async with store.engine.acquire() as conn:
        (after,) = await conn.fetch("SELECT id, created, updated FROM machines")
    assert before is not None
    assert (after["id"], after["created"]) == (before["id"], before["created"])
    assert from_plain(after["updated"], datetime) > from_plain(
        before["updated"],
        datetime,
    )
    await client.put(_path("dev-1"), json={"role": ""})
    assert (await _one(client, "dev-1")).role == ""


async def test_the_longest_name_role_and_how_are_stored(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    _as("admin")
    name = "a" * 63

    put = await client.put(
        _path(name),
        json={"role": "r" * 32, "how": "é" * MAX_HOW_CHARS},
    )

    assert put.status_code == 204
    one = await _one(client, name)
    assert (len(one.role), len(one.how)) == (32, MAX_HOW_CHARS)


@pytest.mark.parametrize(
    "body",
    [
        {"role": "Dev"},
        {"role": "r" * 33},
        {"how": "h" * (MAX_HOW_CHARS + 1)},
        {"how": "a\x00b"},
        {"unknown": "x"},
    ],
    ids=["upper", "long-role", "long-how", "nul", "extra"],
)
async def test_a_bad_body_is_refused_and_stores_nothing(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    body: dict[str, str],
) -> None:
    client, _ = pglite_route_client
    _as("admin")

    response = await client.put(_path("dev-1"), json=body)

    assert response.status_code == 422
    assert (await _listed(client)).machines == []


@pytest.mark.parametrize(
    "name",
    ["Upper", "-lead", "a" * 64, "a_b", "a b", "dev\n", *sorted(RESERVED_NAMES)],
)
async def test_a_malformed_or_reserved_name_is_refused_and_stores_nothing(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    name: str,
) -> None:
    client, _ = pglite_route_client
    _as("admin")

    put = await client.put(_path(name), json={"role": "dev"})
    labels = await client.patch(_labels_path(name), json={"add": ["gpu"]})
    get = await client.get(_path(name))
    delete = await client.delete(_path(name))

    assert [r.status_code for r in (put, labels, get, delete)] == [422] * 4
    assert (await _listed(client)).machines == []


async def test_labels_are_added_then_removed_idempotently(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    _as("admin")
    await client.put(_path("dev-1"), json={})

    first = await client.patch(
        _labels_path("dev-1"),
        json={"add": [" gpu ", "a100", "gpu"]},
    )
    again = await client.patch(_labels_path("dev-1"), json={"add": ["gpu"]})
    added_then_removed = await client.patch(
        _labels_path("dev-1"),
        json={"add": ["tmp", "spot"], "remove": ["tmp", "absent"]},
    )
    removed = await client.patch(_labels_path("dev-1"), json={"remove": ["a100"]})
    removed_again = await client.patch(
        _labels_path("dev-1"),
        json={"remove": ["a100"]},
    )

    assert (first.status_code, first.content) == (204, b"")
    assert [
        r.status_code for r in (again, added_then_removed, removed, removed_again)
    ] == [204] * 4
    assert (await _one(client, "dev-1")).labels == ["gpu", "spot"]


async def test_a_change_with_nothing_in_it_is_accepted(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    _as("admin")
    await client.put(_path("dev-1"), json={})
    await client.patch(_labels_path("dev-1"), json={"add": ["gpu"]})

    response = await client.patch(_labels_path("dev-1"), json={})

    assert response.status_code == 204
    assert (await _one(client, "dev-1")).labels == ["gpu"]


@pytest.mark.parametrize(
    "body",
    [{"add": [""]}, {"add": ["  "]}, {"remove": [" "]}, {"add": ["a\x00b"]}],
    ids=["empty", "blank", "blank-remove", "nul"],
)
async def test_a_bad_label_is_refused_and_changes_nothing(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    body: dict[str, list[str]],
) -> None:
    client, _ = pglite_route_client
    _as("admin")
    await client.put(_path("dev-1"), json={})
    await client.patch(_labels_path("dev-1"), json={"add": ["gpu"]})

    response = await client.patch(_labels_path("dev-1"), json=body)

    assert response.status_code == 422
    assert (await _one(client, "dev-1")).labels == ["gpu"]


async def test_labels_of_an_absent_machine_are_not_found(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    _as("admin")

    response = await client.patch(_labels_path("nope"), json={"add": ["gpu"]})

    assert response.status_code == 404
    assert (await _listed(client)).machines == []


async def test_a_machine_is_not_found_until_registered_and_after_delete(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    _as("admin")
    before = await client.get(_path("dev-1"))
    await client.put(_path("dev-1"), json={"role": "dev"})

    deleted = await client.delete(_path("dev-1"))
    after = await client.get(_path("dev-1"))
    deleted_again = await client.delete(_path("dev-1"))

    assert before.status_code == 404
    assert (deleted.status_code, deleted.content) == (204, b"")
    assert (after.status_code, deleted_again.status_code) == (404, 404)
    assert (await _listed(client)).machines == []


async def test_a_machine_can_be_registered_again_after_delete(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    _as("admin")
    await client.put(_path("dev-1"), json={"role": "dev", "how": "ssh dev-1"})
    await client.patch(_labels_path("dev-1"), json={"add": ["gpu"]})
    await client.delete(_path("dev-1"))

    await client.put(_path("dev-1"), json={})

    one = await _one(client, "dev-1")
    assert (one.role, one.how, one.labels) == ("", "", [])


def _path(name: str) -> str:
    return MACHINE_PATH.format(name=quote(name, safe=""))


def _labels_path(name: str) -> str:
    return MACHINE_LABELS_PATH.format(name=quote(name, safe=""))


def _as(role: Role) -> None:
    install_identity(make_test_identity(role=role, api_key_id=None))


async def _listed(client: httpx2.AsyncClient) -> MachineList:
    response = await client.get(MACHINES_PATH)
    assert response.status_code == 200
    return MachineList.model_validate_json(response.text)


async def _one(client: httpx2.AsyncClient, name: str) -> MachineDetail:
    response = await client.get(_path(name))
    assert response.status_code == 200, from_plain(response.text, str)
    return MachineDetail.model_validate_json(response.text)


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
