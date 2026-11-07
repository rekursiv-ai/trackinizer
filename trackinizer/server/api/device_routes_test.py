"""Device login's code lookup and public address, over a fake connection.

The routes end to end are in ``device_routes_pglite_test.py`` over PGlite. These tests
need no database, so they pin each branch of the lookup that decides whether a typed
code is open, and which address the page link names.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TypedDict, cast
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import secrets

from fastapi import FastAPI, HTTPException, Response
from starlette.requests import Request
from treekle import from_plain

import pytest

from trackinizer.conftest import FakeEngine, executed_sql, make_conn, set_field_row
from trackinizer.mock_test_support import MockConnection
from trackinizer.server.api import device_routes
from trackinizer.server.api.conftest import make_test_identity
from trackinizer.server.api.device_limits import (
    CodeRequests,
    device_code_requests,
    device_wrong_entries,
)
from trackinizer.server.machine_auth import MintedSecret
from trackinizer.wire.wire_device import (
    DEVICE_CODE_TTL_SEC,
    DEVICE_POLL_SEC,
    WRONG_ENTRIES_LOCK,
    DeviceCodeRequest,
    DeviceDecision,
    DeviceTokenRequest,
)


class _Refusal(TypedDict):
    code: str
    message: str


pytestmark = pytest.mark.asyncio

_IDENTITY = make_test_identity()
_LOGIN = UUID("9f1c2e3a-0000-4000-8000-0000000000aa")


def _request(*, path: str = "/") -> Request:
    return Request(
        {
            "type": "http",
            "app": FastAPI(),
            "scheme": "https",
            "server": ("testserver", 443),
            "path": path,
            "root_path": "",
            "headers": [(b"host", b"testserver")],
        },
    )


def _row(status: str) -> dict[str, object]:
    return {
        "id": _LOGIN,
        "user_code": "BCDFGHJK",
        "client": "trax",
        "host": "box",
        "machine": None,
        "status": status,
    }


async def _open(
    request: Request,
    row: dict[str, object] | None,
    typed: str = "bcdf-ghjk",
) -> tuple[MockConnection, device_routes._Login | HTTPException]:
    raw = AsyncMock()
    conn = cast(MockConnection, raw)
    conn.fetchrow.return_value = row
    try:
        return conn, await device_routes._open_login(
            request,
            raw,
            identity=_IDENTITY,
            body=DeviceDecision(user_code=typed),
        )
    except HTTPException as refused:
        return conn, refused


def _entries(request: Request) -> int:
    return len(device_wrong_entries(request).failures.get(_IDENTITY.user_id, []))


async def test_an_open_code_returns_its_login_and_forgives_the_entry() -> None:
    request = _request()
    assert device_wrong_entries(request).reserve(_IDENTITY.user_id)

    conn, login = await _open(request, _row("pending"))

    assert login == device_routes._Login(
        id=_LOGIN,
        client="trax",
        host="box",
        machine=None,
        status="pending",
        user_code="BCDFGHJK",
    )
    assert _entries(request) == 0
    assert conn.fetchrow.await_args is not None
    sql, code = conn.fetchrow.await_args.args
    assert sql == (
        "SELECT id, user_code, client, host, machine, status FROM device_logins "
        "WHERE user_code = $1 AND expires_at > now() FOR UPDATE"
    )
    assert code == "BCDFGHJK"


async def test_an_unknown_code_is_a_404_and_stays_counted() -> None:
    request = _request()
    assert device_wrong_entries(request).reserve(_IDENTITY.user_id)

    _, refused = await _open(request, None)

    assert isinstance(refused, HTTPException)
    assert refused.status_code == 404
    assert refused.detail == {
        "code": "device_code_unknown",
        "message": "that code is not open; check it, or start the login again",
    }
    assert _entries(request) == 1


@pytest.mark.parametrize("status", ["approved", "denied"])
async def test_a_decided_login_is_a_409_and_forgives_the_entry(status: str) -> None:
    request = _request()
    assert device_wrong_entries(request).reserve(_IDENTITY.user_id)

    _, refused = await _open(request, _row(status))

    assert isinstance(refused, HTTPException)
    assert refused.status_code == 409
    assert refused.detail == {
        "code": "device_already_decided",
        "message": "this login was already decided",
    }
    assert _entries(request) == 0


@pytest.mark.parametrize(
    ("public_url", "sign_in", "expected"),
    [
        ("https://trackinizer.example/hub", "", "https://trackinizer.example/hub"),
        ("https://trackinizer.example/hub/", "", "https://trackinizer.example/hub"),
        (
            "https://trackinizer.example",
            "https://sign-in.example/hub",
            "https://trackinizer.example",
        ),
        ("", "https://sign-in.example/hub", "https://sign-in.example/hub"),
        ("", "", "https://testserver"),
    ],
    ids=["set", "trailing-slash", "set-over-sign-in", "sign-in", "unset"],
)
async def test_the_public_address_is_the_deployments_the_sign_ins_or_the_requests(
    monkeypatch: pytest.MonkeyPatch,
    public_url: str,
    sign_in: str,
    expected: str,
) -> None:
    monkeypatch.setenv("TRACKINIZER_PUBLIC_URL", public_url)
    request = _request()
    if sign_in:
        cast(FastAPI, request.app).state.sign_in_base = sign_in

    assert device_routes._public_base(request) == expected


@pytest.mark.parametrize(("machine", "has_client"), [(None, False), ("gx10", True)])
async def test_device_code_stores_only_digest_and_returns_public_verification_link(
    monkeypatch: pytest.MonkeyPatch,
    machine: str | None,
    has_client: bool,
) -> None:
    request, engine = _unit_device_request(has_client=has_client)
    engine.conn.fetchval.return_value = 999
    monkeypatch.setenv("TRACKINIZER_PUBLIC_URL", "https://public.example/hub/")
    monkeypatch.setattr(
        secrets,
        "token_urlsafe",
        Mock(return_value="poll-secret"),
    )
    monkeypatch.setattr(secrets, "choice", Mock(return_value="B"))
    response = Response()
    body = DeviceCodeRequest(client="trax", host="box", machine=machine)

    code = await device_routes.device_code_route(body, request, response)

    assert code.device_code == "poll-secret"
    assert code.user_code == "BBBB-BBBB"
    assert code.verification_uri == "https://public.example/hub/auth/device"
    assert code.verification_uri_complete == (
        "https://public.example/hub/auth/device?user_code=BBBB-BBBB"
    )
    assert (code.expires_in, code.interval) == (DEVICE_CODE_TTL_SEC, DEVICE_POLL_SEC)
    assert response.headers["Cache-Control"] == "no-store"
    engine.conn.execute.assert_any_await(
        "INSERT INTO device_logins "
        "(device_sha256, user_code, client, host, machine, expires_at) "
        "VALUES ($1, $2, $3, $4, $5, now() + make_interval(secs => $6))",
        device_routes._digest("poll-secret"),
        "BBBBBBBB",
        "trax",
        "box",
        machine,
        DEVICE_CODE_TTL_SEC,
    )
    assert executed_sql(engine.conn)[0:2] == [
        "BEGIN",
        "DELETE FROM device_logins WHERE expires_at <= now()",
    ]
    assert executed_sql(engine.conn)[-1] == "COMMIT"
    assert set(device_code_requests(request).asks) == (
        {"203.0.113.1"} if has_client else {""}
    )


@pytest.mark.parametrize("reason", ["disabled", "address", "busy"])
async def test_device_code_refusals_do_not_insert_a_login(
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    request, engine = _unit_device_request()
    app = cast(FastAPI, request.app)
    monkeypatch.setattr(
        device_routes,
        "auth_is_disabled",
        Mock(return_value=reason == "disabled"),
    )
    if reason == "address":
        app.state.device_code_requests = CodeRequests(limit=0)
    engine.conn.fetchval.return_value = 1_000
    with pytest.raises(HTTPException) as refused:
        await device_routes.device_code_route(
            DeviceCodeRequest(client="trax"),
            request,
            Response(),
        )
    expected = {
        "disabled": (403, "device_auth_disabled"),
        "address": (429, "device_rate_limited"),
        "busy": (429, "device_codes_busy"),
    }
    assert (
        refused.value.status_code,
        from_plain(refused.value.detail, _Refusal)["code"],
    ) == expected[reason]
    assert all("INSERT INTO" not in sql for sql in executed_sql(engine.conn))
    if reason == "busy":
        assert executed_sql(engine.conn)[-1] == "ROLLBACK"
    else:
        engine.conn.fetchval.assert_not_awaited()


@pytest.mark.parametrize(
    ("status", "too_soon", "expected"),
    [
        (None, False, "expired_token"),
        ("pending", False, "pending"),
        ("denied", False, "denied"),
        ("approved", True, "slow_down"),
    ],
)
async def test_device_poll_returns_wait_denial_expiry_or_slow_down(
    status: str | None,
    too_soon: bool,
    expected: str,
) -> None:
    request, engine = _unit_device_request()
    set_field_row(
        engine.conn,
        None
        if status is None
        else {
            **_row(status),
            "too_soon": too_soon,
        },
    )
    response = Response()

    token = await device_routes.device_token_route(
        DeviceTokenRequest(device_code="poll-secret"),
        request,
        response,
    )

    assert token.status == expected
    assert token.issued is None
    assert response.headers["Cache-Control"] == "no-store"
    assert engine.conn.fetchrow.await_args is not None
    assert engine.conn.fetchrow.await_args.args[1:] == (
        device_routes._digest("poll-secret"),
        DEVICE_POLL_SEC,
    )
    updates = [
        sql for sql in executed_sql(engine.conn) if "UPDATE device_logins" in sql
    ]
    assert len(updates) == (0 if status is None else 1)
    assert executed_sql(engine.conn)[-1] == "COMMIT"


@pytest.mark.parametrize("role", ["viewer", "admin"])
async def test_device_lookup_formats_code_and_caps_the_proposed_key_role(
    role: str,
) -> None:
    request, engine = _unit_device_request()
    identity = make_test_identity(
        api_key_id=None,
        role="admin" if role == "admin" else "viewer",
    )
    set_field_row(engine.conn, _row("pending"))

    view = await device_routes.device_lookup_route(
        DeviceDecision(user_code="bcdf-ghjk"),
        request,
        identity,
    )

    assert (
        view.user_code,
        view.account,
        view.client,
        view.host,
        view.machine,
        view.role,
    ) == (
        "BCDF-GHJK",
        identity.email,
        "trax",
        "box",
        None,
        "writer" if role == "admin" else "viewer",
    )
    assert identity.user_id not in device_wrong_entries(request).failures


@pytest.mark.parametrize("machine", [None, "gx10"])
@pytest.mark.parametrize("viewer", [False, True])
async def test_device_approval_claims_only_a_writers_requested_machine(
    monkeypatch: pytest.MonkeyPatch,
    machine: str | None,
    viewer: bool,
) -> None:
    request, engine = _unit_device_request()
    set_field_row(engine.conn, {**_row("pending"), "machine": machine})
    identity = make_test_identity(
        api_key_id=None,
        role="viewer" if viewer else "writer",
    )
    claim = AsyncMock(return_value=UUID(int=3))
    monkeypatch.setattr(device_routes, "claim_machine", claim)
    body = DeviceDecision(user_code="BCDF-GHJK")
    if viewer and machine is not None:
        with pytest.raises(HTTPException) as refused:
            await device_routes.device_approve_route(body, request, identity)
        assert refused.value.status_code == 403
        assert (
            from_plain(refused.value.detail, _Refusal)["code"]
            == "device_machine_needs_writer"
        )
        claim.assert_not_awaited()
        assert executed_sql(engine.conn)[-1] == "ROLLBACK"
        assert all("SET status" not in sql for sql in executed_sql(engine.conn))
    else:
        assert await device_routes.device_approve_route(body, request, identity) is None
        if machine is None:
            claim.assert_not_awaited()
        else:
            claim.assert_awaited_once_with(
                engine.conn,
                name=machine,
                email=identity.email,
                admin=False,
                confirmed=True,
            )
        engine.conn.execute.assert_any_await(
            "UPDATE device_logins SET status = $2, approver = $3 WHERE id = $1",
            _LOGIN,
            "approved",
            identity.user_id,
        )
        assert executed_sql(engine.conn)[-1] == "COMMIT"


async def test_device_denial_records_the_person_without_claiming_a_machine() -> None:
    request, engine = _unit_device_request()
    identity = make_test_identity(api_key_id=None)
    set_field_row(engine.conn, {**_row("pending"), "machine": "gx10"})
    assert (
        await device_routes.device_deny_route(
            DeviceDecision(user_code="BCDF-GHJK"),
            request,
            identity,
        )
        is None
    )
    engine.conn.execute.assert_any_await(
        "UPDATE device_logins SET status = $2, approver = $3 WHERE id = $1",
        _LOGIN,
        "denied",
        identity.user_id,
    )
    assert executed_sql(engine.conn)[-1] == "COMMIT"


@pytest.mark.parametrize("reason", ["disabled", "key", "service", "foreign", "locked"])
async def test_device_person_gate_rejects_untrusted_callers_and_locks_out_guesses(
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    request, _engine = _unit_device_request(
        origin="https://foreign.example"
        if reason == "foreign"
        else "https://testserver",
    )
    identity = make_test_identity(
        api_key_id=UUID(int=1) if reason == "key" else None,
        email="robot@service.invalid" if reason == "service" else "ada@example.com",
    )
    monkeypatch.setattr(
        device_routes,
        "auth_is_disabled",
        Mock(return_value=reason == "disabled"),
    )
    if reason == "locked":
        for _ in range(WRONG_ENTRIES_LOCK):
            assert device_wrong_entries(request).reserve(identity.user_id)
    with pytest.raises(HTTPException) as refused:
        device_routes._require_person(request, identity)
    assert (
        refused.value.status_code,
        from_plain(refused.value.detail, _Refusal)["code"],
    ) == ((429, "device_locked") if reason == "locked" else (403, "device_forbidden"))
    assert len(device_wrong_entries(request).failures.get(identity.user_id, [])) == (
        WRONG_ENTRIES_LOCK if reason == "locked" else 0
    )


@pytest.mark.parametrize(("machine", "host"), [(None, ""), ("gx10", "box")])
async def test_approved_poll_mints_key_and_optional_enrollment_then_consumes_login(
    monkeypatch: pytest.MonkeyPatch,
    machine: str | None,
    host: str,
) -> None:
    request, engine = _unit_device_request()
    approver = UUID(int=2)
    machine_id = UUID(int=3)
    engine.conn.fetchrow.side_effect = [
        {**_row("approved"), "approver": approver, "machine": machine, "host": host},
        {"email": "ada@example.com"},
    ]
    engine.conn.fetchval.return_value = machine_id
    key = AsyncMock(return_value=(UUID(int=4), "api-secret", "prefix", "viewer"))
    enrollment_secret = secrets.token_hex(16)
    minted = MintedSecret(id=UUID(int=5), token=enrollment_secret, digest=b"digest")
    enrollment = AsyncMock(return_value=(minted, datetime(2026, 1, 1, tzinfo=UTC)))
    monkeypatch.setattr(device_routes, "create_api_key", key)
    monkeypatch.setattr(device_routes, "issue_enrollment", enrollment)

    token = await device_routes.device_token_route(
        DeviceTokenRequest(device_code="poll-secret"),
        request,
        Response(),
    )

    assert token.status == "approved"
    assert token.issued is not None
    assert (token.issued.email, token.issued.role, token.issued.api_key) == (
        "ada@example.com",
        "viewer",
        "api-secret",
    )
    assert token.issued.machine_token == ("" if machine is None else enrollment_secret)
    key.assert_awaited_once_with(
        engine.conn,
        user_id=approver,
        name="trax@box" if host else "trax",
        ceiling="writer",
        machine_id=None if machine is None else machine_id,
    )
    if machine is None:
        enrollment.assert_not_awaited()
    else:
        enrollment.assert_awaited_once_with(
            engine.conn,
            machine_id=machine_id,
            created_by="ada@example.com",
        )
        engine.conn.fetchval.assert_awaited_once_with(
            "SELECT id FROM machines WHERE name = $1 AND owner = $2 FOR UPDATE",
            "gx10",
            "ada@example.com",
        )
    engine.conn.execute.assert_any_await(
        "DELETE FROM device_logins WHERE id = $1",
        _LOGIN,
    )
    assert executed_sql(engine.conn)[-1] == "COMMIT"


async def test_approved_login_with_no_active_approver_expires_and_is_consumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = make_conn()
    key = AsyncMock()
    monkeypatch.setattr(device_routes, "create_api_key", key)
    token = await device_routes._issue(
        conn,
        login=device_routes._Login(
            id=_LOGIN,
            client="trax",
            host="",
            machine=None,
            status="approved",
            approver=UUID(int=2),
        ),
    )
    assert token.status == "expired_token"
    assert token.issued is None
    key.assert_not_awaited()
    conn.execute.assert_awaited_once_with(
        "DELETE FROM device_logins WHERE id = $1",
        _LOGIN,
    )


async def test_approved_login_refuses_a_deleted_machine_before_minting_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = make_conn()
    set_field_row(conn, {"email": "ada@example.com"})
    key, enrollment = AsyncMock(), AsyncMock()
    monkeypatch.setattr(device_routes, "create_api_key", key)
    monkeypatch.setattr(device_routes, "issue_enrollment", enrollment)
    with pytest.raises(HTTPException) as refused:
        await device_routes._issue(
            conn,
            login=device_routes._Login(
                id=_LOGIN,
                client="trax",
                host="",
                machine="gx10",
                status="approved",
                approver=UUID(int=2),
            ),
        )
    assert refused.value.status_code == 409
    assert from_plain(refused.value.detail, _Refusal)["code"] == "device_machine_gone"
    key.assert_not_awaited()
    enrollment.assert_not_awaited()
    conn.execute.assert_not_awaited()


async def test_device_digest_matches_the_sha256_known_answer() -> None:
    assert device_routes._digest("abc") == bytes.fromhex(
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    )


def _unit_device_request(
    *,
    has_client: bool = True,
    origin: str = "https://testserver",
) -> tuple[Request, FakeEngine]:
    app = FastAPI()
    engine = FakeEngine()
    app.state.engine = engine
    request = Request(
        {
            "type": "http",
            "app": app,
            "scheme": "https",
            "server": ("testserver", 443),
            "path": "/",
            "root_path": "",
            "client": ("203.0.113.1", 123) if has_client else None,
            "headers": [(b"host", b"testserver"), (b"origin", origin.encode())],
        },
    )
    return request, engine


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
