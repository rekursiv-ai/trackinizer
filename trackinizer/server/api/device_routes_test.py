"""Device login's code lookup and public address, over a fake connection.

The routes end to end are in ``device_routes_pglite_test.py`` over PGlite. These tests
need no database, so they pin each branch of the lookup that decides whether a typed
code is open, and which address the page link names.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID

from fastapi import FastAPI, HTTPException
from starlette.requests import Request

import pytest

from trackinizer.server.api import device_routes
from trackinizer.server.api.conftest import make_test_identity
from trackinizer.server.api.device_limits import device_wrong_entries
from trackinizer.wire.wire_device import DeviceDecision


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
) -> tuple[AsyncMock, device_routes._Login | HTTPException]:
    conn = AsyncMock()
    conn.fetchrow.return_value = row
    try:
        return conn, await device_routes._open_login(
            request,
            conn,
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
    ("public_url", "expected"),
    [
        ("https://trackinizer.example/hub", "https://trackinizer.example/hub"),
        ("https://trackinizer.example/hub/", "https://trackinizer.example/hub"),
        ("", "https://testserver"),
    ],
    ids=["set", "trailing-slash", "unset"],
)
async def test_the_public_address_is_the_deployments_or_the_requests_own(
    monkeypatch: pytest.MonkeyPatch,
    public_url: str,
    expected: str,
) -> None:
    monkeypatch.setenv("TRACKINIZER_PUBLIC_URL", public_url)

    assert device_routes._public_base(_request()) == expected


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
