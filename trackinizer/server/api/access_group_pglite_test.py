"""Core serves the builtin AccessGroup with no access policy installed, over PGlite.

The whole app answers each request: a group is created like any row, read by its
id and its ``AccessGroup#N`` ref, linked only by the edges its kind takes, and its
controls are written by no field route. No route purges one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

import uuid

from treekle import json_loads

import pytest
import pytest_asyncio

from trackinizer.server.api.session_test_support import seed_accounts


if TYPE_CHECKING:
    import httpx2

    from trackinizer.server.store.core import Store


type _Client = tuple[httpx2.AsyncClient, Store]


@pytest_asyncio.fixture(loop_scope="session")
async def group_client(pglite_route_client: _Client) -> _Client:
    """Serve the whole app to a keyless writer whose account is an active user.

    A row must be attributed to an active account, so the route tests' three
    accounts are signed up over the session's build.
    """
    _, store = pglite_route_client
    await seed_accounts(store)
    return pglite_route_client


class _Row(TypedDict):
    id: str
    kind: str
    seq: int
    read_group: str
    write_group: str
    system: bool
    domain: str | None


class _Made(TypedDict):
    id: str


async def _create(client: httpx2.AsyncClient, kind: str, title: str) -> str:
    made = await client.post(f"/api/inquiries/{kind}", json={"title": title})
    assert made.status_code == 201, made.text
    return json_loads(made.text, _Made)["id"]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_group_is_created_listed_and_read_by_id_and_by_ref(
    group_client: _Client,
) -> None:
    client, _ = group_client

    first = await _create(client, "accessgroup", "reviewers")
    second = await _create(client, "accessgroup", "editors")

    listed = await client.get("/api/inquiries", params={"kind": "AccessGroup"})
    by_ref = await client.get("/api/inquiries/AccessGroup/1")
    by_id = await client.get(f"/api/inquiries/{first}")
    assert listed.status_code == 200, listed.text
    assert [(row["id"], row["seq"]) for row in json_loads(listed.text, list[_Row])] == [
        (second, 2),
        (first, 1),
    ]
    assert by_ref.status_code == 200, by_ref.text
    assert json_loads(by_ref.text, _Row)["id"] == first
    shown = json_loads(by_id.text, _Row)
    assert (shown["kind"], shown["read_group"], shown["write_group"]) == (
        "AccessGroup",
        first,
        first,
    )
    assert (shown["system"], shown["domain"]) == (False, None)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_group_takes_provenance_and_supersession_edges_only(
    group_client: _Client,
) -> None:
    client, _ = group_client
    group = await _create(client, "accessgroup", "reviewers")
    successor = await _create(client, "accessgroup", "reviewers, merged")
    issue = await _create(client, "issue", "the work")

    produced = await client.post(f"/api/edges/{group}/produced_by/{issue}", json={})
    superseded = await client.post(
        f"/api/edges/{successor}/supersedes/{group}",
        json={},
    )
    narrowed = await client.post(f"/api/edges/{group}/narrows/{issue}", json={})

    assert produced.status_code == 200, produced.text
    assert superseded.status_code == 200, superseded.text
    assert narrowed.status_code == 409, narrowed.text


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_group_cites_no_claim(group_client: _Client) -> None:
    client, _ = group_client
    group = await _create(client, "accessgroup", "reviewers")
    claim = await _create(client, "belief", "a claim")

    cited = [
        await client.post(f"/api/edges/{group}/{edge_kind}/{claim}", json={})
        for edge_kind in ("proves", "favors")
    ]
    inline = await client.post(
        "/api/inquiries/belief",
        json={
            "title": "another claim",
            "proved_by": [{"artifact_id": group, "artifact_kind": "AccessGroup"}],
        },
    )

    assert [response.status_code for response in cited] == [409, 409]
    assert inline.status_code == 422, inline.text


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_no_route_writes_a_control_or_purges_a_group(
    group_client: _Client,
) -> None:
    client, _ = group_client
    group = await _create(client, "accessgroup", "reviewers")
    forged = await client.post(
        "/api/inquiries/accessgroup",
        json={"title": "forged", "read_group": str(uuid.uuid4())},
    )

    writes = [
        await client.put(f"/api/inquiries/{group}/{name}", json={"value": value})
        for name, value in (
            ("read_group", str(uuid.uuid4())),
            ("accessgroup_write_group", str(uuid.uuid4())),
            ("system", True),
            ("domain", "example.com"),
        )
    ]
    purged = await client.request("DELETE", f"/api/inquiries/{group}", json={})

    assert forged.status_code == 422, forged.text
    assert [write.status_code for write in writes] == [405] * 4
    assert purged.status_code == 409, purged.text
    assert "retired by status" in purged.text
    shown = json_loads((await client.get(f"/api/inquiries/{group}")).text, _Row)
    assert (shown["read_group"], shown["write_group"]) == (group, group)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("turn", ["first", "second"])
async def test_each_test_meets_only_its_own_rows(
    group_client: _Client,
    turn: str,
) -> None:
    client, _ = group_client
    made = await _create(client, "accessgroup", f"{turn} group")

    listed = await client.get("/api/inquiries", params={"kind": "AccessGroup"})
    issues = await client.get("/api/inquiries", params={"kind": "Issue"})

    assert [(row["id"], row["seq"]) for row in json_loads(listed.text, list[_Row])] == [
        (made, 1),
    ], turn
    assert json_loads(issues.text, list[_Row]) == [], turn


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
