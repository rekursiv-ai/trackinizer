"""Service accounts, end to end over PGlite.

Real keys are resolved by the real ``current_user`` against the real tables, so each
security property is shown on the rows and the answers a client sees: who may create a
service account and mint its tokens, that none is ever an admin, a person's sign-in or
a token manager, that a minted token stays inside its account and its minter, and that
disabling or deleting an account ends every token it holds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import uuid

from fastapi import Response
from treekle import from_plain

import pytest

from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import clear_identity_override
from trackinizer.server.auth import TOKEN_PREFIX_LEN, create_api_key
from trackinizer.server.config import Config
from trackinizer.server.session import SESSION_COOKIE_NAME, set_session_cookie


if TYPE_CHECKING:
    from collections.abc import Mapping

    import httpx2

    from trackinizer.server.store.core import Store


type _Client = tuple[httpx2.AsyncClient, Store]

pytestmark = [
    pytest.mark.db_pglite,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.usefixtures("cheap_scrypt"),
]

_ACCOUNTS = "/api/admin/service-accounts"


@pytest.fixture
def cheap_scrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hash secrets at the smallest accepted cost so a test pays no scrypt round."""
    monkeypatch.setattr("trackinizer.server.auth._SCRYPT_N", 2)


def _serve(monkeypatch: pytest.MonkeyPatch, *staff: str, session: str = "") -> None:
    """Serve the app with Canvas members of ``example.com`` and these entries as staff."""
    config = Config(
        staff=("*@example.com", *staff),
        session_secret=session,
        session_max_age_seconds=600,
    )
    monkeypatch.setattr(app.state, "config", config, raising=False)


async def _person(
    store: Store,
    email: str,
    *,
    role: str = "writer",
    hosted_domain: str | None = None,
) -> uuid.UUID:
    """Insert an active person, as a sign-in would."""
    user_id = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status, hosted_domain) "
            "VALUES ($1, $2, 'n', $3, 'active', $4)",
            user_id,
            email,
            role,
            hosted_domain,
        )
    return user_id


async def _key(
    store: Store,
    user_id: uuid.UUID,
    *,
    open_into: str | None = None,
) -> str:
    """Mint a key for a user straight in the store; return its secret."""
    async with store.engine.acquire() as conn:
        _, secret, _, _ = await create_api_key(
            conn,
            user_id=user_id,
            name="k",
            ceiling="admin",
            open_into=open_into,
        )
    return secret


async def _admin(
    store: Store,
    email: str = "boss@example.com",
    *,
    staff: bool = True,
    open_into: str | None = None,
) -> tuple[uuid.UUID, str]:
    """Insert an admin, a Canvas member of the staff domain when ``staff``."""
    user_id = await _person(
        store,
        email,
        role="admin",
        hosted_domain="example.com" if staff else None,
    )
    secret = await _key(store, user_id, open_into=open_into)
    return user_id, secret


async def _call(
    client: httpx2.AsyncClient,
    method: str,
    path: str,
    *,
    secret: str | None = None,
    body: Mapping[str, object] | None = None,
    cookie: str | None = None,
) -> httpx2.Response:
    """Send one request through the real authentication."""
    clear_identity_override()
    headers: dict[str, str] = {}
    if secret is not None:
        headers["Authorization"] = f"Bearer {secret}"
    if cookie is not None:
        headers["Cookie"] = f"{SESSION_COOKIE_NAME}={cookie}"
    return await client.request(method, path, json=body, headers=headers)


def _json(response: httpx2.Response, status: int) -> dict[str, object]:
    """Return the body of a response that answered ``status``."""
    assert response.status_code == status, response.text
    return from_plain(response.json(), dict[str, object])


async def _service(
    client: httpx2.AsyncClient,
    secret: str,
    name: str = "campaign",
    role: str = "writer",
) -> uuid.UUID:
    """Create a service account as this admin; return its user id."""
    created = _json(
        await _call(
            client,
            "POST",
            _ACCOUNTS,
            secret=secret,
            body={"name": name, "role": role},
        ),
        201,
    )
    return uuid.UUID(from_plain(created["id"], str))


async def _mint(
    client: httpx2.AsyncClient,
    secret: str,
    account: uuid.UUID,
    **body: object,
) -> httpx2.Response:
    """Mint a token for a service account as this admin."""
    return await _call(
        client,
        "POST",
        f"{_ACCOUNTS}/{account}/tokens",
        secret=secret,
        body={"name": "t", **body},
    )


async def _token(
    client: httpx2.AsyncClient,
    secret: str,
    account: uuid.UUID,
    **body: object,
) -> str:
    """Mint a token that must succeed; return its secret."""
    minted = _json(await _mint(client, secret, account, **body), 201)
    return from_plain(minted["secret"], str)


async def _profile(client: httpx2.AsyncClient, secret: str) -> dict[str, object]:
    """Read the profile of the credential ``secret``."""
    return _json(await _call(client, "GET", "/api/me/profile", secret=secret), 200)


async def _key_ids(store: Store, user_id: uuid.UUID) -> list[uuid.UUID]:
    """Return a user's key ids, oldest first."""
    async with store.engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id FROM api_keys WHERE user_id = $1 ORDER BY created_at, id",
            user_id,
        )
    return [from_plain(row["id"], uuid.UUID) for row in rows]


async def _id_of(store: Store, secret: str) -> uuid.UUID:
    """Return the id of the key a secret belongs to."""
    async with store.engine.acquire() as conn:
        key_id = await conn.fetchval(
            "SELECT id FROM api_keys WHERE prefix = $1",
            secret[:TOKEN_PREFIX_LEN],
        )
    return from_plain(key_id, uuid.UUID)


async def _scalar(store: Store, sql: str, *args: object) -> object:
    """Run one count query."""
    async with store.engine.acquire() as conn:
        return await conn.fetchval(sql, *args)


async def _snapshot(store: Store) -> tuple[object, ...]:
    """Return the counts of users, keys, revoked keys, service users and admins."""
    async with store.engine.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT (SELECT count(*) FROM users), (SELECT count(*) FROM api_keys), "
            "(SELECT count(*) FROM api_keys WHERE revoked_at IS NOT NULL), "
            "(SELECT count(*) FROM users WHERE email LIKE '%@service.invalid'), "
            "(SELECT count(*) FROM users WHERE role = 'admin')",
        )
    assert row is not None
    return tuple(row.values())


async def _attempts(
    client: httpx2.AsyncClient,
    secret: str | None,
    *,
    account: uuid.UUID,
    key: uuid.UUID,
) -> list[int]:
    """Try every service-account route with this credential; return the statuses."""
    tokens = f"{_ACCOUNTS}/{account}/tokens"
    sent = (
        ("POST", _ACCOUNTS, {"name": f"n{uuid.uuid4().hex[:8]}", "role": "viewer"}),
        ("POST", tokens, {"name": "t"}),
        ("GET", tokens, None),
        ("POST", f"{tokens}/{key}/revoke", None),
    )
    return [
        (await _call(client, method, path, secret=secret, body=body)).status_code
        for method, path, body in sent
    ]


async def test_p1_no_caller_below_admin_can_create_a_service_account_or_mint_its_tokens(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = pglite_route_client
    _serve(monkeypatch)
    _, boss = await _admin(store)
    account = await _service(client, boss)
    own = await _token(client, boss, account)
    (key,) = await _key_ids(store, account)
    viewer = await _key(store, await _person(store, "v@example.com", role="viewer"))
    writer = await _key(store, await _person(store, "w@example.com", role="writer"))
    before = await _snapshot(store)

    answered = {
        "viewer": await _attempts(client, viewer, account=account, key=key),
        "writer": await _attempts(client, writer, account=account, key=key),
        "service key": await _attempts(client, own, account=account, key=key),
        "no credential": await _attempts(client, None, account=account, key=key),
    }

    assert answered == {
        "viewer": [403, 403, 403, 403],
        "writer": [403, 403, 403, 403],
        "service key": [403, 403, 403, 403],
        "no credential": [401, 401, 401, 401],
    }
    assert await _snapshot(store) == before
    assert (await _profile(client, own))["email"] == "campaign@service.invalid"


async def test_p2_a_service_account_never_holds_or_mints_admin(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = pglite_route_client
    _serve(monkeypatch)
    _, boss = await _admin(store)
    writer_account = await _service(client, boss, "campaign", "writer")
    viewer_account = await _service(client, boss, "watcher", "viewer")
    before = await _snapshot(store)

    by_creation = await _call(
        client,
        "POST",
        _ACCOUNTS,
        secret=boss,
        body={"name": "root", "role": "admin"},
    )
    by_role_change = await _call(
        client,
        "PUT",
        f"/api/admin/users/{writer_account}/role",
        secret=boss,
        body={"role": "admin"},
    )
    by_token_role = await _mint(client, boss, writer_account, role="admin")
    above_viewer = await _mint(client, boss, viewer_account, role="writer")

    assert [r.status_code for r in (by_creation, by_role_change)] == [422, 422]
    assert [r.status_code for r in (by_token_role, above_viewer)] == [403, 403]
    assert await _snapshot(store) == before
    assert (
        await _scalar(
            store,
            "SELECT role FROM users WHERE id = $1",
            writer_account,
        )
        == "writer"
    )
    # Each of the two roles a service account may hold still mints within it.
    assert (await _mint(client, boss, writer_account, role="viewer")).status_code == 201
    assert (await _mint(client, boss, viewer_account, role="viewer")).status_code == 201
    assert not await _scalar(
        store,
        "SELECT count(*) FROM api_keys k JOIN users u ON u.id = k.user_id "
        "WHERE u.email LIKE '%@service.invalid' AND k.role = 'admin'",
    )


async def test_p4_a_service_key_cannot_mint_re_role_or_revoke_tokens(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = pglite_route_client
    _serve(monkeypatch)
    _, boss = await _admin(store)
    account = await _service(client, boss)
    own = await _token(client, boss, account, name="host")
    spare = await _token(client, boss, account, name="spare")
    host_id, spare_id = await _id_of(store, own), await _id_of(store, spare)
    before = await _snapshot(store)

    minted = await _call(
        client,
        "POST",
        "/api/me/tokens",
        secret=own,
        body={"name": "x"},
    )
    re_roled = await _call(
        client,
        "PUT",
        f"/api/me/tokens/{host_id}/role",
        secret=own,
        body={"role": "viewer"},
    )
    revoked_own = await _call(
        client,
        "POST",
        f"/api/me/tokens/{host_id}/revoke",
        secret=own,
    )
    revoked_spare = await _call(
        client,
        "POST",
        f"/api/me/tokens/{spare_id}/revoke",
        secret=own,
    )

    assert [r.status_code for r in (minted, re_roled, revoked_own, revoked_spare)] == [
        403,
        403,
        403,
        403,
    ]
    assert await _snapshot(store) == before
    assert (
        await _scalar(
            store,
            "SELECT role FROM api_keys WHERE id = $1",
            host_id,
        )
        == "writer"
    )
    # Both keys still work, and the service key still reads its own profile and keys.
    assert (await _profile(client, spare))["email"] == "campaign@service.invalid"
    assert (await _profile(client, own))["email"] == "campaign@service.invalid"
    listed = _json(await _call(client, "GET", "/api/me/tokens", secret=own), 200)
    assert len(from_plain(listed["tokens"], list[object])) == 2


async def test_p5_a_token_never_exceeds_its_accounts_role(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = pglite_route_client
    _serve(monkeypatch)
    _, boss = await _admin(store)
    viewer_account = await _service(client, boss, "watcher", "viewer")
    writer_account = await _service(client, boss, "worker", "writer")

    refused = await _mint(client, boss, viewer_account, role="writer")
    default = await _token(client, boss, viewer_account)
    narrowed = await _token(client, boss, writer_account, role="viewer")
    inherited = await _token(client, boss, writer_account)

    assert refused.status_code == 403
    assert [
        (await _profile(client, secret))["role"]
        for secret in (default, narrowed, inherited)
    ] == ["viewer", "viewer", "writer"]
    # The viewer account's token cannot write, whatever its row says.
    written = await _call(
        client,
        "POST",
        "/api/inquiries/issue",
        secret=default,
        body={"title": "x"},
    )
    assert written.status_code == 403


async def test_p5_a_token_never_exceeds_its_accounts_audiences(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = pglite_route_client
    _serve(monkeypatch)
    _, boss = await _admin(store)
    account = await _service(client, boss, "assistant")

    refused = await _mint(client, boss, account, open_into="staff")
    named = _json(await _mint(client, boss, account, open_into="open"), 201)
    unpinned = await _token(client, boss, account)

    assert refused.status_code == 403
    assert named["open_into"] == "open"
    profile = await _profile(client, unpinned)
    assert (profile["read_audiences"], profile["open_audiences"]) == (
        ["open", "private"],
        ["open", "private"],
    )
    assert await _scalar(store, "SELECT count(*) FROM api_keys") == 3


async def test_p5_staff_is_held_only_through_an_exact_address_in_the_staff_setting(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = pglite_route_client
    _serve(monkeypatch, "campaign@service.invalid")
    _, boss = await _admin(store)
    campaign = await _service(client, boss, "campaign")
    assistant = await _service(client, boss, "assistant")

    host = await _token(client, boss, campaign, open_into="staff")
    unpinned = await _token(client, boss, campaign)
    neighbour = await _mint(client, boss, assistant, open_into="staff")

    host_profile = await _profile(client, host)
    assert (host_profile["read_audiences"], host_profile["pinned_audience"]) == (
        ["open", "private", "staff"],
        "staff",
    )
    assert host_profile["open_audiences"] == ["staff"]
    assert (await _profile(client, unpinned))["read_audiences"] == [
        "open",
        "private",
        "staff",
    ]
    assert neighbour.status_code == 403


async def test_p5_a_domain_pattern_never_gives_a_service_account_staff(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A service account has no hosted domain, so ``*@service.invalid`` matches none."""
    client, store = pglite_route_client
    _serve(monkeypatch, "*@service.invalid")
    _, boss = await _admin(store)
    campaign = await _service(client, boss, "campaign")

    refused = await _mint(client, boss, campaign, open_into="staff")
    unpinned = await _token(client, boss, campaign)

    assert refused.status_code == 403
    profile = await _profile(client, unpinned)
    assert (profile["read_audiences"], profile["open_audiences"]) == (
        ["open", "private"],
        ["open", "private"],
    )
    assert (
        await _scalar(
            store,
            "SELECT hosted_domain FROM users WHERE id = $1",
            campaign,
        )
        is None
    )


async def test_p5_a_token_never_exceeds_the_admin_that_mints_it(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = pglite_route_client
    _serve(monkeypatch, "campaign@service.invalid")
    _, staff_boss = await _admin(store, "staff@example.com")
    _, plain_boss = await _admin(store, "plain@example.com", staff=False)
    _, pinned_boss = await _admin(store, "pinned@example.com", open_into="open")
    campaign = await _service(client, staff_boss, "campaign")
    assistant = await _service(client, staff_boss, "assistant")

    by_plain_unpinned = await _mint(client, plain_boss, campaign)
    by_plain_pinned = await _mint(client, plain_boss, campaign, open_into="open")
    by_plain_naming_staff = await _mint(client, plain_boss, campaign, open_into="staff")
    by_pinned_naming_staff = await _mint(
        client,
        pinned_boss,
        campaign,
        open_into="staff",
    )
    by_pinned_default = await _token(client, pinned_boss, campaign)
    by_plain_for_open_account = await _token(client, plain_boss, assistant)

    assert [
        r.status_code
        for r in (
            by_plain_unpinned,
            by_plain_pinned,
            by_plain_naming_staff,
            by_pinned_naming_staff,
        )
    ] == [403, 403, 403, 403]
    inherited = await _profile(client, by_pinned_default)
    assert (inherited["pinned_audience"], inherited["open_audiences"]) == (
        "open",
        ["open"],
    )
    assert (await _profile(client, by_plain_for_open_account))["read_audiences"] == [
        "open",
        "private",
    ]


async def test_p5_an_admin_cannot_create_an_account_that_holds_more_than_it_does(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = pglite_route_client
    _serve(monkeypatch, "campaign@service.invalid")
    _, plain_boss = await _admin(store, "plain@example.com", staff=False)
    _, staff_boss = await _admin(store, "staff@example.com")

    refused = await _call(
        client,
        "POST",
        _ACCOUNTS,
        secret=plain_boss,
        body={"name": "campaign", "role": "writer"},
    )
    open_only = await _call(
        client,
        "POST",
        _ACCOUNTS,
        secret=plain_boss,
        body={"name": "assistant", "role": "writer"},
    )
    allowed = _json(
        await _call(
            client,
            "POST",
            _ACCOUNTS,
            secret=staff_boss,
            body={"name": "campaign", "role": "writer"},
        ),
        201,
    )

    assert (refused.status_code, open_only.status_code) == (403, 201)
    assert allowed["staff"] is True


async def test_p6_disable_stops_every_token_at_once_and_delete_removes_them(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = pglite_route_client
    _serve(monkeypatch)
    _, boss = await _admin(store)
    account = await _service(client, boss)
    tokens = [await _token(client, boss, account) for _ in range(3)]
    for secret in tokens:
        assert (await _profile(client, secret))["role"] == "writer"

    disabled = await _call(
        client,
        "POST",
        f"/api/admin/users/{account}/disable",
        secret=boss,
    )
    after_disable = [
        (await _call(client, "GET", "/api/me/profile", secret=secret)).status_code
        for secret in tokens
    ]
    enabled = await _call(
        client,
        "POST",
        f"/api/admin/users/{account}/enable",
        secret=boss,
    )
    after_enable = [
        (await _call(client, "GET", "/api/me/profile", secret=secret)).status_code
        for secret in tokens
    ]

    assert disabled.status_code == 200
    assert after_disable == [401, 401, 401]
    assert enabled.status_code == 200
    assert after_enable == [401, 401, 401]
    assert (
        await _scalar(
            store,
            "SELECT count(*) FROM api_keys WHERE user_id = $1 AND revoked_at IS NULL",
            account,
        )
        == 0
    )

    fresh = await _token(client, boss, account)
    assert (await _profile(client, fresh))["email"] == "campaign@service.invalid"
    deleted = await _call(client, "DELETE", f"/api/admin/users/{account}", secret=boss)
    assert deleted.status_code == 204
    assert (
        await _call(client, "GET", "/api/me/profile", secret=fresh)
    ).status_code == 401
    assert (
        await _scalar(
            store,
            "SELECT count(*) FROM api_keys WHERE user_id = $1",
            account,
        )
        == 0
    )


async def test_p6_a_new_account_of_the_same_name_does_not_revive_an_old_token(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = pglite_route_client
    _serve(monkeypatch)
    _, boss = await _admin(store)
    old = await _service(client, boss)
    old_token = await _token(client, boss, old)
    assert (await _profile(client, old_token))["role"] == "writer"
    await _call(client, "DELETE", f"/api/admin/users/{old}", secret=boss)

    new = await _service(client, boss)

    assert new != old
    answered = await _call(client, "GET", "/api/me/profile", secret=old_token)
    assert answered.status_code == 401
    listed = _json(
        await _call(client, "GET", f"{_ACCOUNTS}/{new}/tokens", secret=boss),
        200,
    )
    assert listed["tokens"] == []
    assert await _key_ids(store, new) == []


async def test_p6_a_disabled_account_keeps_its_name_and_takes_no_new_token(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = pglite_route_client
    _serve(monkeypatch)
    _, boss = await _admin(store)
    account = await _service(client, boss)
    await _call(client, "POST", f"/api/admin/users/{account}/disable", secret=boss)

    again = await _call(
        client,
        "POST",
        _ACCOUNTS,
        secret=boss,
        body={"name": "campaign", "role": "writer"},
    )
    minted = await _mint(client, boss, account)

    assert again.status_code == 409
    assert minted.status_code == 409
    assert await _key_ids(store, account) == []
    assert (
        await _scalar(
            store,
            "SELECT count(*) FROM users WHERE email LIKE '%@service.invalid'",
        )
        == 1
    )


async def test_p8_nothing_is_reachable_with_a_disabled_admin_or_a_revoked_admin_key(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = pglite_route_client
    _serve(monkeypatch)
    first, first_key = await _admin(store, "first@example.com")
    _, second_key = await _admin(store, "second@example.com")
    third, third_key = await _admin(store, "third@example.com")
    spare_key = await _key(store, third)
    account = await _service(client, second_key)
    await _token(client, second_key, account)
    (key,) = await _key_ids(store, account)
    # Each admin key works, and is cached, before it is taken away.
    for secret in (first_key, third_key):
        assert (await _attempts(client, secret, account=account, key=uuid.uuid4()))[
            :3
        ] == [201, 201, 200]

    disabled = await _call(
        client,
        "POST",
        f"/api/admin/users/{first}/disable",
        secret=second_key,
    )
    third_id = await _id_of(store, third_key)
    revoked = await _call(
        client,
        "POST",
        f"/api/me/tokens/{third_id}/revoke",
        secret=spare_key,
    )
    before = await _snapshot(store)

    answered = {
        "disabled admin": await _attempts(client, first_key, account=account, key=key),
        "revoked key": await _attempts(client, third_key, account=account, key=key),
    }

    assert (disabled.status_code, revoked.status_code) == (200, 200)
    assert answered == {
        "disabled admin": [401, 401, 401, 401],
        "revoked key": [401, 401, 401, 401],
    }
    assert await _snapshot(store) == before
    assert (await _attempts(client, spare_key, account=account, key=uuid.uuid4()))[
        1
    ] == 201


async def test_a_session_cookie_never_opens_a_service_account(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = pglite_route_client
    _serve(monkeypatch, session="session-signing-secret")
    _, boss = await _admin(store)
    account = await _service(client, boss)
    signed = Response()
    set_session_cookie(
        signed,
        user_id=str(account),
        secret="session-signing-secret",  # noqa: S106 -- test fixture.
        max_age_seconds=600,
    )
    cookie = signed.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]

    refused = await _call(client, "GET", "/api/me/profile", cookie=cookie)

    assert refused.status_code == 401


async def test_the_created_user_is_the_one_the_listing_shows(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = pglite_route_client
    _serve(monkeypatch)
    await _person(store, "lee@example.com", role="writer")
    boss_id, boss = await _admin(store)
    created = _json(
        await _call(
            client,
            "POST",
            _ACCOUNTS,
            secret=boss,
            body={"name": "campaign", "role": "writer"},
        ),
        201,
    )

    listed = _json(await _call(client, "GET", "/api/admin/users", secret=boss), 200)

    users = {
        from_plain(u["email"], str): u
        for u in from_plain(listed["users"], list[dict[str, object]])
    }
    assert set(users) == {
        "lee@example.com",
        "boss@example.com",
        "campaign@service.invalid",
    }
    assert users["campaign@service.invalid"] == created
    assert str(boss_id) == users["boss@example.com"]["id"]


async def test_a_person_is_never_reached_through_the_service_account_routes(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = pglite_route_client
    _serve(monkeypatch)
    _, boss = await _admin(store)
    lee = await _person(store, "lee@example.com", role="writer")
    lee_key = await _key(store, lee)
    (lee_key_id,) = await _key_ids(store, lee)
    before = await _snapshot(store)

    answered = [
        (await _mint(client, boss, lee)).status_code,
        (
            await _call(client, "GET", f"{_ACCOUNTS}/{lee}/tokens", secret=boss)
        ).status_code,
        (
            await _call(
                client,
                "POST",
                f"{_ACCOUNTS}/{lee}/tokens/{lee_key_id}/revoke",
                secret=boss,
            )
        ).status_code,
        (await _mint(client, boss, uuid.uuid4())).status_code,
    ]

    assert answered == [404, 404, 404, 404]
    assert await _snapshot(store) == before
    assert (await _profile(client, lee_key))["email"] == "lee@example.com"


async def test_an_admin_lists_and_revokes_the_tokens_of_a_service_account(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = pglite_route_client
    _serve(monkeypatch)
    _, boss = await _admin(store)
    account = await _service(client, boss)
    host = await _token(client, boss, account, name="host")
    spare = await _token(client, boss, account, name="spare")
    host_id, spare_id = await _id_of(store, host), await _id_of(store, spare)
    tokens = f"{_ACCOUNTS}/{account}/tokens"

    revoked = await _call(client, "POST", f"{tokens}/{host_id}/revoke", secret=boss)
    again = await _call(client, "POST", f"{tokens}/{host_id}/revoke", secret=boss)
    foreign = await _call(
        client,
        "POST",
        f"{tokens}/{uuid.uuid4()}/revoke",
        secret=boss,
    )
    listed = _json(await _call(client, "GET", tokens, secret=boss), 200)

    assert [r.status_code for r in (revoked, again, foreign)] == [200, 404, 404]
    assert (
        await _call(client, "GET", "/api/me/profile", secret=host)
    ).status_code == 401
    assert (await _profile(client, spare))["email"] == "campaign@service.invalid"
    rows = {
        from_plain(t["name"], str): t
        for t in from_plain(listed["tokens"], list[dict[str, object]])
    }
    assert rows["host"]["revoked_at"] is not None
    assert rows["spare"]["revoked_at"] is None
    assert str(spare_id) == rows["spare"]["id"]
    assert all("secret" not in t and "secret_hash" not in t for t in rows.values())


async def test_the_minted_secret_is_shown_once_and_only_its_hash_is_kept(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = pglite_route_client
    _serve(monkeypatch)
    _, boss = await _admin(store)
    account = await _service(client, boss)

    minted = await _mint(client, boss, account)

    secret = from_plain(_json(minted, 201)["secret"], str)
    assert minted.headers["cache-control"] == "no-store"
    stored = await _scalar(
        store,
        "SELECT secret_hash FROM api_keys WHERE user_id = $1",
        account,
    )
    assert isinstance(stored, str)
    assert secret not in stored
    listed = await _call(client, "GET", f"{_ACCOUNTS}/{account}/tokens", secret=boss)
    assert secret not in listed.text


async def test_a_created_account_has_no_google_identity(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = pglite_route_client
    _serve(monkeypatch)
    _, boss = await _admin(store)
    account = await _service(client, boss)
    assert await _scalar(
        store,
        "SELECT google_sub IS NULL AND hosted_domain IS NULL FROM users WHERE id = $1",
        account,
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
