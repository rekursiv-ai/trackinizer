"""The welcome flow's agreement, recorded per user against the rules version.

The rules version is Issue#1's last change, so editing the rules makes a recorded
agreement stale. The whole app answers each request over PGlite.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from trackinizer.lib.codec import from_plain
from trackinizer.server.api.canvas_test_support import seed_accounts
from trackinizer.server.api.conftest import install_identity, make_test_identity


if TYPE_CHECKING:
    import httpx2

    from trackinizer.server.auth import Role
    from trackinizer.server.store.core import Store


type _Client = tuple[httpx2.AsyncClient, Store]

pytestmark = [
    pytest.mark.db_pglite,
    pytest.mark.asyncio(loop_scope="session"),
]


async def test_a_new_user_has_agreed_to_nothing(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)

    profile = await _profile(client)

    assert profile["acknowledged_at"] is None
    assert profile["acknowledged_rules_version"] is None
    assert profile["rules_issue_id"] is None
    assert profile["rules_version"] == "none"


async def test_agreeing_records_the_current_rules_version(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    rules = await _rules_issue(client)

    agreed = await _agree(client)
    profile = await _profile(client)

    assert agreed.status_code == 200, agreed.text
    assert profile["rules_issue_id"] == str(rules)
    assert profile["acknowledged_rules_version"] == profile["rules_version"]
    assert profile["acknowledged_rules_version"] != "none"
    assert isinstance(profile["acknowledged_at"], str)
    assert from_plain(agreed.json(), dict[str, object]) == {
        "acknowledged_at": profile["acknowledged_at"],
        "acknowledged_rules_version": profile["rules_version"],
    }


async def test_editing_the_rules_makes_the_agreement_stale(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    rules = await _rules_issue(client)
    await _agree(client)
    agreed = await _profile(client)

    edit = await client.put(
        f"/api/inquiries/{rules}/description",
        json={"value": "new rules"},
    )
    stale = await _profile(client)

    assert edit.status_code == 200, edit.text
    assert stale["rules_version"] != agreed["rules_version"]
    assert stale["acknowledged_rules_version"] == agreed["rules_version"]


async def test_locking_the_rules_leaves_the_version_alone(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    rules = await _rules_issue(client)
    before = await _profile(client)

    _as("admin")
    locked = await client.put(
        f"/api/admin/inquiries/{rules}/lock",
        json={"locked": True},
    )
    after = await _profile(client)

    assert locked.status_code == 200, locked.text
    assert after["rules_version"] == before["rules_version"]


async def test_editing_a_child_of_the_rules_leaves_the_version_alone(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    rules = await _rules_issue(client)
    child = await client.post(
        "/api/inquiries/issue",
        json={"title": "child", "narrows": [[rules, None]]},
    )
    assert child.status_code == 201, child.text
    child_id = from_plain(from_plain(child.json(), dict[str, object])["id"], str)
    before = await _profile(client)

    edit = await client.put(
        f"/api/inquiries/{child_id}/title",
        json={"value": "renamed"},
    )
    after = await _profile(client)

    assert edit.status_code == 200, edit.text
    assert after["rules_version"] == before["rules_version"]


async def test_agreeing_to_rules_that_changed_since_they_were_read_is_refused(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    rules = await _rules_issue(client)
    read = await _profile(client)
    await client.put(f"/api/inquiries/{rules}/description", json={"value": "new"})

    refused = await client.put(
        "/api/me/acknowledge",
        json={"rules_version": read["rules_version"]},
    )
    profile = await _profile(client)

    assert refused.status_code == 409, refused.text
    assert profile["acknowledged_rules_version"] is None


async def test_a_key_cannot_agree_for_its_user(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    install_identity(make_test_identity(role="writer"))

    refused = await client.put(
        "/api/me/acknowledge",
        json={"rules_version": "none"},
    )

    assert refused.status_code == 403


def _as(role: Role) -> None:
    install_identity(make_test_identity(role=role, api_key_id=None))


async def _rules_issue(client: httpx2.AsyncClient) -> str:
    """Create the first Issue, which is the rules."""
    made = await client.post("/api/inquiries/issue", json={"title": "Rules"})
    assert made.status_code == 201, made.text
    return from_plain(from_plain(made.json(), dict[str, object])["id"], str)


async def _agree(client: httpx2.AsyncClient) -> httpx2.Response:
    """Agree to the rules version the caller's profile shows."""
    shown = await _profile(client)
    return await client.put(
        "/api/me/acknowledge",
        json={"rules_version": shown["rules_version"]},
    )


async def _profile(client: httpx2.AsyncClient) -> dict[str, object]:
    response = await client.get("/api/me/profile")
    assert response.status_code == 200, response.text
    return from_plain(response.json(), dict[str, object])


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
