"""Who holds which audience, end to end over PGlite.

A real bearer key is resolved by the real ``current_user`` against the real tables, and
``GET /api/me/profile`` reports what it reads and opens into. The mocked unit tests pin
each rule; this proves the SQL that feeds them names columns that exist.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import uuid

from treekle import from_plain

import pytest

from trackinizer.server.api.conftest import clear_identity_override, patch_state
from trackinizer.server.auth import create_api_key
from trackinizer.server.config import Config


if TYPE_CHECKING:
    import httpx2

    from trackinizer.server.store.core import Store


pytestmark = [
    pytest.mark.db_pglite,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.usefixtures("cheap_scrypt"),
]


@pytest.fixture
def cheap_scrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hash secrets at the smallest accepted cost so a test pays no scrypt round."""
    monkeypatch.setattr("trackinizer.server.auth._SCRYPT_N", 2)


@pytest.fixture
def staff_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve the app with staff set to a Workspace domain and one literal address."""
    config = Config(staff=("*@example.com", "ada@other.org"))
    patch_state(monkeypatch, "config", config)


async def _user(
    store: Store,
    email: str,
    *,
    hosted_domain: str | None = None,
) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status, hosted_domain) "
            "VALUES ($1, $2, 'n', 'writer', 'active', $3)",
            user_id,
            email,
            hosted_domain,
        )
    return user_id


async def _key(
    store: Store,
    user_id: uuid.UUID,
    *,
    name: str = "k",
    open_into: str | None = None,
) -> str:
    async with store.engine.acquire() as conn:
        _, secret, _, _ = await create_api_key(
            conn,
            user_id=user_id,
            name=name,
            ceiling="admin",
            open_into=open_into,
        )
    return secret


async def _profile(client: httpx2.AsyncClient, secret: str) -> dict[str, object]:
    clear_identity_override()
    response = await client.get(
        "/api/me/profile",
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert response.status_code == 200, response.text
    return from_plain(response.json(), dict[str, object])


@pytest.mark.usefixtures("staff_setting")
async def test_a_canvas_member_and_a_literal_address_are_staff_and_nobody_else(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    member = await _user(store, "lee@example.com", hosted_domain="example.com")
    consumer = await _user(store, "kim@example.com")
    literal = await _user(store, "ada@other.org")
    outsider = await _user(store, "bob@other.org", hosted_domain="other.org")

    held = {
        email: (await _profile(client, await _key(store, user)))["read_audiences"]
        for email, user in (
            ("member", member),
            ("consumer", consumer),
            ("literal", literal),
            ("outsider", outsider),
        )
    }

    assert held == {
        "member": ["open", "private", "staff"],
        "consumer": ["open", "private"],
        "literal": ["open", "private", "staff"],
        "outsider": ["open", "private"],
    }


async def test_unset_staff_leaves_every_key_with_open_only(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = pglite_route_client
    patch_state(monkeypatch, "config", Config())
    user = await _user(store, "lee@example.com", hosted_domain="example.com")

    profile = await _profile(client, await _key(store, user))

    assert profile["read_audiences"] == ["open", "private"]
    assert profile["open_audiences"] == ["open", "private"]
    assert profile["pinned_audience"] is None


@pytest.mark.usefixtures("staff_setting")
async def test_a_pinned_staff_key_reads_what_its_owner_reads_and_opens_into_its_pin(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    user = await _user(store, "lee@example.com", hosted_domain="example.com")

    plain = await _profile(client, await _key(store, user))
    pinned = await _profile(client, await _key(store, user, open_into="staff"))

    assert (plain["read_audiences"], plain["open_audiences"]) == (
        ["open", "private", "staff"],
        ["open", "private", "staff"],
    )
    assert (pinned["read_audiences"], pinned["open_audiences"]) == (
        ["open", "private", "staff"],
        ["staff"],
    )
    assert pinned["pinned_audience"] == "staff"


@pytest.mark.usefixtures("staff_setting")
async def test_an_owner_who_loses_staff_loses_it_on_every_key_and_the_pin_opens_nothing(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    user = await _user(store, "lee@example.com", hosted_domain="example.com")
    secret = await _key(store, user, open_into="staff")
    assert (await _profile(client, secret))["open_audiences"] == ["staff"]

    async with store.engine.acquire() as conn:
        await conn.execute("UPDATE users SET hosted_domain = NULL WHERE id = $1", user)
    store.forget_bearer_identities(user)
    profile = await _profile(client, secret)

    assert profile["read_audiences"] == ["open", "private"]
    assert profile["open_audiences"] == []
    assert profile["pinned_audience"] == "staff"


@pytest.mark.usefixtures("staff_setting")
async def test_a_minted_key_round_trips_its_pin_and_its_children_inherit_it(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    user = await _user(store, "lee@example.com", hosted_domain="example.com")
    parent = await _key(store, user)
    clear_identity_override()

    minted = await client.post(
        "/api/me/tokens",
        headers={"Authorization": f"Bearer {parent}"},
        json={"name": "campaign", "open_into": "staff"},
    )
    assert minted.status_code == 200, minted.text
    campaign = from_plain(from_plain(minted.json(), dict[str, object])["secret"], str)
    child = await client.post(
        "/api/me/tokens",
        headers={"Authorization": f"Bearer {campaign}"},
        json={"name": "child"},
    )
    assert child.status_code == 200, child.text
    refused = await client.post(
        "/api/me/tokens",
        headers={"Authorization": f"Bearer {campaign}"},
        json={"name": "wider", "open_into": "open"},
    )

    assert refused.status_code == 403
    inherited = await _profile(
        client,
        from_plain(from_plain(child.json(), dict[str, object])["secret"], str),
    )
    assert inherited["pinned_audience"] == "staff"
    assert await _stored_pins(store) == {
        "k": None,
        "campaign": "staff",
        "child": "staff",
    }


@pytest.mark.usefixtures("staff_setting")
async def test_an_unpinned_key_mints_a_key_that_still_reads_staff_later(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The child stores no pin, so it follows the owner when staff is configured."""
    client, store = pglite_route_client
    patch_state(monkeypatch, "config", Config())
    user = await _user(store, "lee@example.com", hosted_domain="example.com")
    parent = await _key(store, user)
    clear_identity_override()

    minted = await client.post(
        "/api/me/tokens",
        headers={"Authorization": f"Bearer {parent}"},
        json={"name": "child"},
    )
    assert minted.status_code == 200, minted.text
    child = from_plain(from_plain(minted.json(), dict[str, object])["secret"], str)
    assert (await _profile(client, child))["read_audiences"] == ["open", "private"]

    patch_state(monkeypatch, "config", Config(staff=("*@example.com",)))
    store.forget_bearer_identities(user)

    assert (await _profile(client, child))["read_audiences"] == [
        "open",
        "private",
        "staff",
    ]
    assert (await _stored_pins(store))["child"] is None


async def _stored_pins(store: Store) -> dict[str, object]:
    """Return each key's stored pin, by name."""
    async with store.engine.acquire() as conn:
        rows = await conn.fetch("SELECT name, open_into FROM api_keys")
    return {str(row["name"]): row["open_into"] for row in rows}


async def _key_ids(store: Store) -> dict[str, uuid.UUID]:
    """Return each key's id, by name."""
    async with store.engine.acquire() as conn:
        rows = await conn.fetch("SELECT id, name FROM api_keys")
    return {str(row["name"]): from_plain(str(row["id"]), uuid.UUID) for row in rows}


@pytest.mark.usefixtures("staff_setting")
async def test_a_pinned_key_cannot_revoke_or_retier_an_unpinned_key(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A campaign key reaches its own key and narrower ones, never the operator's."""
    client, store = pglite_route_client
    user = await _user(store, "lee@example.com", hosted_domain="example.com")
    operator = await _key(store, user, name="operator")
    campaign = await _key(store, user, name="campaign", open_into="staff")
    ids = await _key_ids(store)
    clear_identity_override()
    headers = {"Authorization": f"Bearer {campaign}"}

    revoked = await client.post(
        f"/api/me/tokens/{ids['operator']}/revoke",
        headers=headers,
    )
    retiered = await client.put(
        f"/api/me/tokens/{ids['operator']}/role",
        headers=headers,
        json={"role": "viewer"},
    )

    assert (revoked.status_code, retiered.status_code) == (404, 404)
    assert (await _profile(client, operator))["read_audiences"] == [
        "open",
        "private",
        "staff",
    ]

    own = await client.put(
        f"/api/me/tokens/{ids['campaign']}/role",
        headers=headers,
        json={"role": "viewer"},
    )
    assert own.status_code == 200, own.text
    by_operator = await client.post(
        f"/api/me/tokens/{ids['campaign']}/revoke",
        headers={"Authorization": f"Bearer {operator}"},
    )
    assert by_operator.status_code == 200, by_operator.text


@pytest.mark.usefixtures("staff_setting")
async def test_a_pinned_key_may_revoke_a_key_pinned_inside_its_open_set(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    user = await _user(store, "lee@example.com", hosted_domain="example.com")
    campaign = await _key(store, user, open_into="staff")
    await _key(store, user, name="sibling", open_into="staff")
    ids = await _key_ids(store)
    clear_identity_override()

    revoked = await client.post(
        f"/api/me/tokens/{ids['sibling']}/revoke",
        headers={"Authorization": f"Bearer {campaign}"},
    )

    assert revoked.status_code == 200, revoked.text


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
