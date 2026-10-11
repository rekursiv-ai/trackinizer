"""Machine routes, end to end over PGlite.

The whole app answers each request, so the exception handlers and the request
logger are on the path, and the table's own checks stand behind the routes'.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import quote

import json
import re

from treekle import from_plain

import pytest

from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import (
    TEST_USER_EMAIL,
    install_identity,
    make_test_identity,
)
from trackinizer.server.config import Config
from trackinizer.wire.wire_machine_host import DESTROYED_PATH
from trackinizer.wire.wire_machines import (
    MACHINE_DOWN_PATH,
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


async def test_a_writer_provisions_a_machine_that_reads_provisioning(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    _as("writer")
    before = datetime.now(UTC)

    response = await client.post(
        MACHINES_PATH,
        json={
            "provider": "cloud",
            "ttl_sec": 600,
            "spec": {"repos": [{"url": "https://example.com/r.git", "sha": "abc"}]},
            "labels": [" gpu ", "gpu"],
        },
    )

    assert response.status_code == 201, response.text
    made = MachineDetail.model_validate_json(response.text)
    assert re.fullmatch(r"cloud-[a-z0-9]{8}", made.name)
    assert (made.status, made.provider, made.requested_by, made.labels) == (
        "provisioning",
        "cloud",
        TEST_USER_EMAIL,
        ["gpu"],
    )
    assert made.spec == {"repos": [{"url": "https://example.com/r.git", "sha": "abc"}]}
    assert made.expires_at is not None
    assert abs(made.expires_at - before - timedelta(seconds=600)) < timedelta(
        seconds=30,
    )
    assert (made.down_at, made.destroyed_at) == (None, None)
    assert await _one(client, made.name) == made
    listed = (await _listed(client)).machines
    assert [(m.name, m.status, m.provider) for m in listed] == [
        (made.name, "provisioning", "cloud"),
    ]


async def test_a_machine_without_a_ttl_lives_the_server_s_longest(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = pglite_route_client
    monkeypatch.setattr(
        app.state,
        "config",
        Config(machine_max_ttl_sec=7_200),
        raising=False,
    )
    _as("writer")
    before = datetime.now(UTC)

    made = await _provision(client)
    too_long = await client.post(
        MACHINES_PATH,
        json={"provider": "cloud", "ttl_sec": 7_201},
    )

    assert made.expires_at is not None
    lifetime = made.expires_at - before
    assert timedelta(seconds=7_170) < lifetime < timedelta(seconds=7_230)
    assert too_long.status_code == 422


async def test_the_limit_counts_live_provisioned_machines_only(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = pglite_route_client
    monkeypatch.setattr(app.state, "config", Config(machine_limit=2), raising=False)
    _as("admin")
    await client.put(_path("hand-made"), json={})
    first = await _provision(client)
    await _provision(client)

    refused = await client.post(MACHINES_PATH, json={"provider": "cloud"})
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE machines SET destroyed_at = now() WHERE name = $1",
            first.name,
        )
    after_destroy = await client.post(MACHINES_PATH, json={"provider": "cloud"})

    assert refused.status_code == 409
    assert from_plain(refused.json(), dict[str, str])["code"] == "machine_limit"
    assert after_destroy.status_code == 201
    statuses = sorted(m.status for m in (await _listed(client)).machines)
    assert statuses == ["destroyed", "never", "provisioning", "provisioning"]


async def test_down_sets_down_at_once_and_refuses_a_hand_registered_machine(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    _as("admin")
    await client.put(_path("hand-made"), json={})
    _as("writer")
    made = await _provision(client)

    first = await client.post(MACHINE_DOWN_PATH.format(name=made.name))
    down_at = (await _one(client, made.name)).down_at
    again = await client.post(MACHINE_DOWN_PATH.format(name=made.name))
    hand = await client.post(MACHINE_DOWN_PATH.format(name="hand-made"))
    absent = await client.post(MACHINE_DOWN_PATH.format(name="nope"))

    assert (first.status_code, first.content, again.status_code) == (204, b"", 204)
    assert down_at is not None
    assert (await _one(client, made.name)).down_at == down_at
    assert (hand.status_code, absent.status_code) == (409, 404)
    assert from_plain(hand.json(), dict[str, str])["code"] == "not_provisioned"
    assert (await _one(client, "hand-made")).down_at is None


async def test_down_after_destroyed_stays_an_idempotent_204(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    _as("writer")
    made = await _provision(client)
    _as("admin")
    destroyed = await client.post(DESTROYED_PATH.format(name=made.name))
    _as("writer")

    first = await client.post(MACHINE_DOWN_PATH.format(name=made.name))
    down_at = (await _one(client, made.name)).down_at
    again = await client.post(MACHINE_DOWN_PATH.format(name=made.name))

    assert [r.status_code for r in (destroyed, first, again)] == [204, 204, 204]
    one = await _one(client, made.name)
    assert (one.status, one.down_at) == ("destroyed", down_at)
    assert down_at is not None


async def test_a_viewer_can_neither_provision_nor_take_down(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    _as("writer")
    made = await _provision(client)
    _as("viewer")

    up = await client.post(MACHINES_PATH, json={"provider": "cloud"})
    down = await client.post(MACHINE_DOWN_PATH.format(name=made.name))

    assert (up.status_code, down.status_code) == (403, 403)
    _as("writer")
    assert len((await _listed(client)).machines) == 1


@pytest.mark.parametrize(
    "spec",
    [{"k": "\x00"}, {"k": "v" * 9_000}, {"n": float("inf")}],
    ids=["nul", "too-large", "inf"],
)
async def test_a_spec_the_table_cannot_hold_is_422_and_stores_nothing(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    spec: dict[str, object],
) -> None:
    client, _ = pglite_route_client
    _as("writer")

    response = await client.post(
        MACHINES_PATH,
        content=json.dumps({"provider": "cloud", "spec": spec}),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert (await _listed(client)).machines == []


async def _provision(client: httpx2.AsyncClient) -> MachineDetail:
    response = await client.post(MACHINES_PATH, json={"provider": "cloud"})
    assert response.status_code == 201, response.text
    return MachineDetail.model_validate_json(response.text)


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
