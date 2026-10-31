"""Device login routes, end to end over PGlite.

The whole app answers each request, so the exception handlers, the request logger and
the real API-key resolver are on the path. A test acts as the approving person through
the identity override the fixtures install; the one test that needs the key to work
clears it and sends the key as a bearer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import asyncio
import hashlib
import re
import uuid

from treekle import from_plain

import pytest
import pytest_asyncio

from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import (
    TEST_API_KEY_ID,
    TEST_USER_EMAIL,
    TEST_USER_ID,
    clear_identity_override,
    install_identity,
    make_test_identity,
    patch_state,
)
from trackinizer.server.api.device_limits import CodeRequests, WrongEntries
from trackinizer.server.config import Config
from trackinizer.wire.wire_device import (
    DEVICE_APPROVE_PATH,
    DEVICE_CODE_PATH,
    DEVICE_CODE_TTL_SEC,
    DEVICE_DENY_PATH,
    DEVICE_LOOKUP_PATH,
    DEVICE_POLL_PATH,
    DEVICE_POLL_SEC,
    USER_CODE_ALPHABET,
    DeviceCode,
    DeviceLoginView,
    DeviceToken,
)
from trackinizer.wire.wire_machine_host import (
    ENROLLMENT_PREFIX,
    JOIN_PATH,
    JoinResponse,
)
from trackinizer.wire.wire_machines import MACHINES_PATH


if TYPE_CHECKING:
    from collections.abc import Iterator

    import httpx2

    from trackinizer.server.auth import Role
    from trackinizer.server.store.core import Store


pytestmark = [
    pytest.mark.db_pglite,
    pytest.mark.asyncio(loop_scope="session"),
]


_HERE: Final = {"Origin": "http://testserver"}
_OTHER_APPROVER: Final = uuid.UUID("33333333-3333-3333-3333-333333333333")
_INSTANCE: Final = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


class _Clock:
    """A clock a test moves by hand."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest_asyncio.fixture(loop_scope="session", autouse=True)
async def _accounts(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Seed the account the fixture's identity stands for, and a second person."""
    _, store = pglite_route_client
    async with store.engine.acquire() as conn:
        for user_id, email in (
            (TEST_USER_ID, TEST_USER_EMAIL),
            (_OTHER_APPROVER, "other@example.com"),
        ):
            await conn.execute(
                "INSERT INTO users (id, email, name, role, status) "
                "VALUES ($1, $2, 'person', 'writer', 'active')",
                user_id,
                email,
            )


@pytest.fixture(autouse=True)
def clock() -> Iterator[_Clock]:
    """Give each test its own lock state, on a clock it can move."""
    ticking = _Clock()
    app.state.device_wrong_entries = WrongEntries(clock=ticking)
    app.state.device_code_requests = CodeRequests(clock=ticking)
    yield ticking
    del app.state.device_wrong_entries
    del app.state.device_code_requests


async def _approver(store: Store, role: Role = "writer") -> None:
    """Act as the test person, a session (no key) holding ``role``."""
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE users SET role = $2, status = 'active' WHERE id = $1",
            TEST_USER_ID,
            role,
        )
    install_identity(make_test_identity(role=role, api_key_id=None))


async def _code(
    client: httpx2.AsyncClient,
    *,
    machine: str | None = None,
    host: str = "devbox",
) -> DeviceCode:
    body = {"client": "machine-host", "host": host}
    if machine is not None:
        body["machine"] = machine
    response = await client.post(DEVICE_CODE_PATH, json=body)
    assert response.status_code == 201, response.text
    return DeviceCode.model_validate_json(response.text)


async def _poll(client: httpx2.AsyncClient, code: DeviceCode) -> DeviceToken:
    response = await client.post(
        DEVICE_POLL_PATH,
        json={"device_code": code.device_code},
    )
    assert response.status_code == 200, response.text
    return DeviceToken.model_validate_json(response.text)


async def _ready_to_poll(store: Store) -> None:
    """Move every code's last poll past the interval, as waiting would."""
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE device_logins SET last_polled_at = last_polled_at "
            "- make_interval(secs => $1)",
            DEVICE_POLL_SEC * 2,
        )


async def _decide(
    client: httpx2.AsyncClient,
    code: DeviceCode,
    *,
    path: str = DEVICE_APPROVE_PATH,
    headers: dict[str, str] = _HERE,
) -> httpx2.Response:
    return await client.post(
        path,
        json={"user_code": code.user_code},
        headers=headers,
    )


async def _approved_key(
    client: httpx2.AsyncClient,
    store: Store,
    code: DeviceCode,
) -> DeviceToken:
    """Approve ``code`` and poll once the interval has passed."""
    assert (await _poll(client, code)).status == "pending"
    approved = await _decide(client, code)
    assert approved.status_code == 204, approved.text
    await _ready_to_poll(store)
    return await _poll(client, code)


def _problem(response: httpx2.Response) -> str:
    return from_plain(response.json(), dict[str, dict[str, str]])["detail"]["code"]


def _enrollment_secret(token: str) -> str:
    """Return the secret of ``enr_<id>_<secret>``; the secret may itself hold ``_``."""
    return token.split("_", 2)[2]


async def test_the_enrollment_secret_is_read_whole_when_it_holds_an_underscore() -> (
    None
):
    token = f"{ENROLLMENT_PREFIX}{uuid.uuid4().hex}_ab_c"

    assert _enrollment_secret(token) == "ab_c"
    assert token.rsplit("_", 1)[-1] == "c"


async def _stored(store: Store, table: str) -> str:
    async with store.engine.acquire() as conn:
        rows = await conn.fetch(f"SELECT * FROM {table}")  # noqa: S608 -- Test table name.
    return "\n".join(str(dict(row)) for row in rows)


async def test_a_code_request_returns_the_codes_and_keeps_only_a_hash(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client

    response = await client.post(
        DEVICE_CODE_PATH,
        json={"client": "machine-host", "host": "devbox"},
    )

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    code = DeviceCode.model_validate_json(response.text)
    assert code.expires_in == DEVICE_CODE_TTL_SEC
    assert code.interval == DEVICE_POLL_SEC
    assert re.fullmatch(
        f"[{USER_CODE_ALPHABET}]{{4}}-[{USER_CODE_ALPHABET}]{{4}}",
        code.user_code,
    )
    assert code.verification_uri == "http://testserver/auth/device"
    assert code.verification_uri_complete == (
        f"http://testserver/auth/device?user_code={code.user_code}"
    )
    async with store.engine.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM device_logins")
    assert row is not None
    assert (
        from_plain(row["device_sha256"], bytes)
        == hashlib.sha256(code.device_code.encode()).digest()
    )
    assert code.device_code not in await _stored(store, "device_logins")
    assert (row["client"], row["host"], row["machine"], row["status"]) == (
        "machine-host",
        "devbox",
        None,
        "pending",
    )


@pytest.mark.parametrize(
    "body",
    [
        {"client": "Rai Shell"},
        {"client": "machine-host", "machine": "Upper"},
        {"client": "machine-host", "machine": "enroll"},
        {"client": "machine-host", "host": "a b"},
        {"client": "machine-host", "surprise": 1},
    ],
    ids=["client", "machine", "reserved", "host", "extra"],
)
async def test_a_code_request_that_names_nothing_usable_is_422(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    body: dict[str, str | int],
) -> None:
    client, store = pglite_route_client

    response = await client.post(DEVICE_CODE_PATH, json=body)

    assert response.status_code == 422
    assert await _stored(store, "device_logins") == ""


async def test_a_poll_before_approval_is_pending(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    code = await _code(client)

    polled = await _poll(client, code)

    assert polled == DeviceToken(status="pending")


async def test_a_poll_inside_the_interval_is_told_to_slow_down(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    code = await _code(client)
    assert (await _poll(client, code)).status == "pending"

    assert (await _poll(client, code)).status == "slow_down"

    await _ready_to_poll(store)
    assert (await _poll(client, code)).status == "pending"


async def test_an_approved_code_returns_a_key_that_signs_in_as_the_approver(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    await _approver(store)
    code = await _code(client)

    token = await _approved_key(client, store, code)

    assert token.status == "approved"
    assert token.issued is not None
    assert (token.issued.role, token.issued.email) == ("writer", TEST_USER_EMAIL)
    assert token.issued.machine_token == ""
    assert token.issued.api_key.startswith("trax_")
    clear_identity_override()
    me = await client.get(
        MACHINES_PATH,
        headers={"Authorization": f"Bearer {token.issued.api_key}"},
    )
    assert me.status_code == 200
    async with store.engine.acquire() as conn:
        key = await conn.fetchrow("SELECT name, role, user_id FROM api_keys")
    assert key is not None
    assert (key["name"], key["role"], key["user_id"]) == (
        "machine-host@devbox",
        "writer",
        TEST_USER_ID,
    )


async def test_the_key_is_never_stored_and_the_row_is_consumed_with_it(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    await _approver(store)
    code = await _code(client)

    token = await _approved_key(client, store, code)

    assert token.issued is not None
    assert await _stored(store, "device_logins") == ""
    assert token.issued.api_key not in await _stored(store, "api_keys")


async def test_a_second_poll_after_the_key_was_issued_is_expired(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    await _approver(store)
    code = await _code(client)
    await _approved_key(client, store, code)

    again = await _poll(client, code)

    assert again == DeviceToken(status="expired_token")
    async with store.engine.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM api_keys") == 1


@pytest.mark.parametrize(
    ("role", "given"),
    [("viewer", "viewer"), ("writer", "writer"), ("admin", "writer")],
)
async def test_a_key_holds_writer_at_most_and_the_approvers_role_at_most(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    role: Role,
    given: str,
) -> None:
    client, store = pglite_route_client
    await _approver(store, role)
    code = await _code(client)

    token = await _approved_key(client, store, code)

    assert token.issued is not None
    assert token.issued.role == given


async def test_a_denied_code_reads_denied_and_buys_no_key(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    await _approver(store)
    code = await _code(client)

    denied = await _decide(client, code, path=DEVICE_DENY_PATH)

    assert denied.status_code == 204
    await _poll(client, code)
    await _ready_to_poll(store)
    assert await _poll(client, code) == DeviceToken(status="denied")
    async with store.engine.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM api_keys") == 0


async def test_a_decided_code_cannot_be_decided_again(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    await _approver(store)
    code = await _code(client)
    assert (await _decide(client, code)).status_code == 204

    for path in (DEVICE_APPROVE_PATH, DEVICE_DENY_PATH):
        again = await _decide(client, code, path=path)
        assert again.status_code == 409
        assert _problem(again) == "device_already_decided"


async def test_an_expired_code_cannot_be_polled_or_approved(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    await _approver(store)
    code = await _code(client)
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE device_logins SET expires_at = now() - interval '1 s'",
        )

    assert await _poll(client, code) == DeviceToken(status="expired_token")
    refused = await _decide(client, code)
    assert refused.status_code == 404
    assert _problem(refused) == "device_code_unknown"


async def test_a_code_that_expired_after_approval_buys_no_key(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    await _approver(store)
    code = await _code(client)
    assert (await _decide(client, code)).status_code == 204
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE device_logins SET expires_at = now() - interval '1 s'",
        )

    assert await _poll(client, code) == DeviceToken(status="expired_token")
    async with store.engine.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM api_keys") == 0


async def test_a_poll_with_an_unknown_device_code_reads_as_expired(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client

    response = await client.post(DEVICE_POLL_PATH, json={"device_code": "nope"})

    assert DeviceToken.model_validate_json(response.text) == DeviceToken(
        status="expired_token",
    )


async def test_the_next_request_clears_the_codes_that_expired(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    old = await _code(client)
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE device_logins SET expires_at = now() - interval '1 s'",
        )

    fresh = await _code(client)

    async with store.engine.acquire() as conn:
        codes = await conn.fetch("SELECT user_code FROM device_logins")
    assert [row["user_code"] for row in codes] == [fresh.user_code.replace("-", "")]
    assert old.user_code != fresh.user_code


async def test_too_many_open_codes_are_refused(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO device_logins (device_sha256, user_code, client, expires_at) "
            "SELECT decode(md5(n::text) || md5(n::text), 'hex'), "
            "translate(lpad(n::text, 8, '0'), '0123456789', 'BCDFGHJKLM'), "
            "'machine-host', now() + interval '5 min' FROM generate_series(1, 1000) n",
        )

    response = await client.post(DEVICE_CODE_PATH, json={"client": "machine-host"})

    assert response.status_code == 429
    assert _problem(response) == "device_codes_busy"


async def test_one_address_cannot_use_up_the_open_codes_for_the_others(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    clock: _Clock,
) -> None:
    client, store = pglite_route_client
    app.state.device_code_requests = CodeRequests(clock=clock, limit=3)
    for _ in range(3):
        await _code(client)

    refused = await client.post(DEVICE_CODE_PATH, json={"client": "machine-host"})

    assert refused.status_code == 429
    assert _problem(refused) == "device_rate_limited"
    async with store.engine.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM device_logins") == 3
    clock.now += DEVICE_CODE_TTL_SEC + 1
    await _code(client)


async def test_a_server_with_authentication_off_hands_out_no_codes(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    app.state.config = Config(auth_disabled=True)
    try:
        refused = await client.post(DEVICE_CODE_PATH, json={"client": "machine-host"})
    finally:
        del app.state.config

    assert refused.status_code == 403
    assert _problem(refused) == "device_auth_disabled"
    assert await _stored(store, "device_logins") == ""


@pytest.mark.parametrize(
    ("public_url", "sign_in", "page"),
    [
        (
            "https://trackinizer.example/hub",
            "",
            "https://trackinizer.example/hub/auth/device",
        ),
        (
            "https://trackinizer.example",
            "",
            "https://trackinizer.example/auth/device",
        ),
        (
            "https://trackinizer.example/hub/",
            "",
            "https://trackinizer.example/hub/auth/device",
        ),
        (
            "",
            "https://trackinizer.example/hub",
            "https://trackinizer.example/hub/auth/device",
        ),
        ("", "", "http://testserver/auth/device"),
    ],
    ids=["hub-prefix", "root", "trailing-slash", "sign-in", "unset"],
)
async def test_the_page_address_is_the_public_one_the_deployment_names(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
    public_url: str,
    sign_in: str,
    page: str,
) -> None:
    client, _ = pglite_route_client
    monkeypatch.setenv("TRACKINIZER_PUBLIC_URL", public_url)
    if sign_in:
        patch_state(monkeypatch, "sign_in_base", sign_in)

    code = await _code(client)

    assert code.verification_uri == page
    assert code.verification_uri_complete == f"{page}?user_code={code.user_code}"


async def test_the_code_is_read_without_case_or_dash(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    await _approver(store)
    code = await _code(client)

    typed = await client.post(
        DEVICE_APPROVE_PATH,
        json={"user_code": code.user_code.replace("-", " ").lower()},
        headers=_HERE,
    )

    assert typed.status_code == 204


async def test_a_lookup_names_the_account_the_client_the_machine_and_the_role_of_the_key(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    await _approver(store, "admin")
    code = await _code(client, machine="dev-box")

    response = await _decide(client, code, path=DEVICE_LOOKUP_PATH)

    assert response.status_code == 200
    # The account signed in is the one the key and the machine become: the page names it.
    assert DeviceLoginView.model_validate_json(response.text) == DeviceLoginView(
        user_code=code.user_code,
        account=TEST_USER_EMAIL,
        client="machine-host",
        host="devbox",
        machine="dev-box",
        role="writer",
    )
    async with store.engine.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM machines") == 0


async def test_five_wrong_codes_lock_the_person_out_until_the_window_passes(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    clock: _Clock,
) -> None:
    client, store = pglite_route_client
    await _approver(store)
    code = await _code(client)

    for _ in range(5):
        wrong = await client.post(
            DEVICE_APPROVE_PATH,
            json={"user_code": "BBBB-BBBB"},
            headers=_HERE,
        )
        assert wrong.status_code == 404
        assert _problem(wrong) == "device_code_unknown"

    for path in (DEVICE_APPROVE_PATH, DEVICE_DENY_PATH, DEVICE_LOOKUP_PATH):
        locked = await _decide(client, code, path=path)
        assert locked.status_code == 429
        assert _problem(locked) == "device_locked"
    async with store.engine.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM device_logins") == "pending"
    clock.now += DEVICE_CODE_TTL_SEC + 1
    assert (await _decide(client, code)).status_code == 204


async def test_a_burst_of_guesses_is_stopped_at_five_not_checked_one_by_one(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    await _approver(store)
    code = await _code(client)

    answers = await asyncio.gather(
        *(
            client.post(
                DEVICE_APPROVE_PATH,
                json={"user_code": "BBBB-BBBB"},
                headers=_HERE,
            )
            for _ in range(20)
        ),
    )

    statuses = sorted(answer.status_code for answer in answers)
    assert statuses == [404] * 5 + [429] * 15
    assert (await _decide(client, code)).status_code == 429


async def test_right_codes_never_add_up_to_a_lock(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    await _approver(store)
    code = await _code(client)

    for _ in range(12):
        assert (await _decide(client, code, path=DEVICE_LOOKUP_PATH)).status_code == 200

    assert (await _decide(client, code)).status_code == 204


async def test_four_wrong_codes_lock_nobody_and_another_person_is_not_locked(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    await _approver(store)
    code = await _code(client)
    for _ in range(4):
        await client.post(
            DEVICE_APPROVE_PATH,
            json={"user_code": "BBBB-BBBB"},
            headers=_HERE,
        )

    assert (await _decide(client, code, path=DEVICE_LOOKUP_PATH)).status_code == 200
    wrongly = await client.post(
        DEVICE_APPROVE_PATH,
        json={"user_code": "BBBB-BBBB"},
        headers=_HERE,
    )
    assert wrongly.status_code == 404
    assert (await _decide(client, code)).status_code == 429
    install_identity(
        make_test_identity(
            user_id=_OTHER_APPROVER,
            email="other@example.com",
            api_key_id=None,
        ),
    )
    assert (await _decide(client, code)).status_code == 204


@pytest.mark.parametrize(
    "path",
    [DEVICE_APPROVE_PATH, DEVICE_DENY_PATH, DEVICE_LOOKUP_PATH],
)
async def test_an_api_key_decides_nothing(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    path: str,
) -> None:
    client, store = pglite_route_client
    await _approver(store)
    code = await _code(client)
    install_identity(make_test_identity(api_key_id=TEST_API_KEY_ID))

    refused = await _decide(client, code, path=path)

    assert refused.status_code == 403
    assert _problem(refused) == "device_forbidden"
    async with store.engine.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM device_logins") == "pending"


async def test_a_service_account_decides_nothing(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    await _approver(store)
    code = await _code(client)
    install_identity(
        make_test_identity(email="bot@service.invalid", api_key_id=None),
    )

    assert (await _decide(client, code)).status_code == 403


async def test_a_server_with_authentication_off_decides_nothing(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    await _approver(store, "admin")
    code = await _code(client)
    app.state.config = Config(auth_disabled=True)
    try:
        refused = await _decide(client, code)
    finally:
        del app.state.config

    assert refused.status_code == 403


@pytest.mark.parametrize(
    "headers",
    [{}, {"Origin": "https://evil.example"}, {"Referer": "http://testserver/x"}],
    ids=["no-origin", "foreign-origin", "referer-only"],
)
@pytest.mark.parametrize(
    "path",
    [DEVICE_APPROVE_PATH, DEVICE_DENY_PATH, DEVICE_LOOKUP_PATH],
)
async def test_a_cross_origin_decision_is_refused(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    path: str,
    headers: dict[str, str],
) -> None:
    client, store = pglite_route_client
    await _approver(store)
    code = await _code(client)

    refused = await _decide(client, code, path=path, headers=headers)

    assert refused.status_code == 403
    assert _problem(refused) == "device_forbidden"
    async with store.engine.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM device_logins") == "pending"


async def test_nobody_signed_out_decides_anything(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    code = await _code(client)
    clear_identity_override()

    assert (await _decide(client, code)).status_code == 401


async def test_a_named_machine_becomes_the_approvers_confirmed_personal_machine(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    await _approver(store)
    code = await _code(client, machine="dev-box")

    token = await _approved_key(client, store, code)

    assert token.issued is not None
    assert token.issued.machine_token.startswith(ENROLLMENT_PREFIX)
    async with store.engine.acquire() as conn:
        machine = await conn.fetchrow(
            "SELECT owner, role, confirmed_at IS NOT NULL AS confirmed "
            "FROM machines WHERE name = 'dev-box'",
        )
        enrollments = await conn.fetchval(
            "SELECT count(*) FROM machine_enrollments WHERE used_at IS NULL "
            "AND created_by = $1",
            TEST_USER_EMAIL,
        )
    assert machine is not None
    assert (machine["owner"], machine["role"], machine["confirmed"]) == (
        TEST_USER_EMAIL,
        "personal",
        True,
    )
    assert enrollments == 1
    assert _enrollment_secret(token.issued.machine_token) not in (
        await _stored(store, "machine_enrollments")
    )


async def test_the_key_a_login_makes_names_the_machine_it_logged_in_and_no_other(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Logging the machine out revokes the keys it logged in with, so a key names it."""
    client, store = pglite_route_client
    await _approver(store)
    await _approved_key(client, store, await _code(client, machine="dev-box"))
    await _approved_key(client, store, await _code(client))

    async with store.engine.acquire() as conn:
        named = await conn.fetch(
            "SELECT machines.name FROM api_keys LEFT JOIN machines "
            "ON machines.id = api_keys.machine_id WHERE api_keys.user_id = $1 "
            "AND api_keys.name LIKE 'machine-host%' ORDER BY api_keys.created_at",
            TEST_USER_ID,
        )
    assert [row["name"] for row in named] == ["dev-box", None]


async def test_the_machine_token_joins_the_machine(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    await _approver(store)
    token = await _approved_key(client, store, await _code(client, machine="laptop"))
    assert token.issued is not None

    joined = await client.post(
        JOIN_PATH,
        json={
            "name": "laptop",
            "token": token.issued.machine_token,
            "instance": str(_INSTANCE),
            "host_version": "0.1",
            "facts": {},
        },
    )

    assert joined.status_code == 201, joined.text
    assert JoinResponse.model_validate_json(joined.text).credential.startswith(
        "trax_machine_",
    )


async def test_an_approval_of_an_existing_machine_of_theirs_confirms_it(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    await _approver(store)
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO machines (name, updated_by, owner, role) "
            "VALUES ('laptop', $1, $1, 'personal')",
            TEST_USER_EMAIL,
        )

    token = await _approved_key(client, store, await _code(client, machine="laptop"))

    assert token.issued is not None
    assert token.issued.machine_token.startswith(ENROLLMENT_PREFIX)
    async with store.engine.acquire() as conn:
        assert await conn.fetchval(
            "SELECT confirmed_at IS NOT NULL FROM machines WHERE name = 'laptop'",
        )


async def test_a_machine_another_person_owns_is_refused_and_the_code_stays_open(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    await _approver(store)
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO machines (name, updated_by, owner, role) "
            "VALUES ('other-box', 'mal@x.y', 'mal@x.y', 'personal')",
        )
    code = await _code(client, machine="other-box")

    refused = await _decide(client, code)

    assert refused.status_code == 403
    async with store.engine.acquire() as conn:
        machine = await conn.fetchrow(
            "SELECT owner, confirmed_at FROM machines WHERE name = 'other-box'",
        )
        status = await conn.fetchval("SELECT status FROM device_logins")
    assert machine is not None
    assert (machine["owner"], machine["confirmed_at"], status) == (
        "mal@x.y",
        None,
        "pending",
    )


async def test_a_viewer_cannot_approve_a_login_that_names_a_machine(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    await _approver(store, "viewer")
    code = await _code(client, machine="laptop")

    refused = await _decide(client, code)

    assert refused.status_code == 403
    assert _problem(refused) == "device_machine_needs_writer"
    async with store.engine.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM machines") == 0
    assert (await _decide(client, await _code(client))).status_code == 204


async def test_a_machine_that_vanished_after_approval_keeps_the_key_unissued(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    await _approver(store)
    code = await _code(client, machine="laptop")
    assert (await _decide(client, code)).status_code == 204
    async with store.engine.acquire() as conn:
        await conn.execute("DELETE FROM machines WHERE name = 'laptop'")

    response = await client.post(
        DEVICE_POLL_PATH,
        json={"device_code": code.device_code},
    )

    assert response.status_code == 409
    assert _problem(response) == "device_machine_gone"
    async with store.engine.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM api_keys") == 0
        assert await conn.fetchval("SELECT count(*) FROM device_logins") == 1


async def test_an_approver_who_was_disabled_meanwhile_buys_no_key(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    await _approver(store)
    code = await _code(client)
    assert (await _decide(client, code)).status_code == 204
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE users SET status = 'disabled' WHERE id = $1",
            TEST_USER_ID,
        )

    assert await _poll(client, code) == DeviceToken(status="expired_token")
    async with store.engine.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM api_keys") == 0


async def test_two_polls_at_once_after_an_approval_get_one_key_between_them(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    await _approver(store)
    code = await _code(client)
    assert (await _poll(client, code)).status == "pending"
    assert (await _decide(client, code)).status_code == 204
    await _ready_to_poll(store)

    polled = await asyncio.gather(_poll(client, code), _poll(client, code))

    assert sorted(token.status for token in polled) == ["approved", "expired_token"]
    async with store.engine.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM api_keys") == 1


async def test_a_poll_is_never_cached(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, _ = pglite_route_client
    code = await _code(client)

    response = await client.post(
        DEVICE_POLL_PATH,
        json={"device_code": code.device_code},
    )

    assert response.headers["cache-control"] == "no-store"


async def test_the_routes_stay_out_of_the_api_schema() -> None:
    paths = from_plain(app.openapi()["paths"], dict[str, object])

    assert [path for path in paths if path.startswith("/auth/device")] == []


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
