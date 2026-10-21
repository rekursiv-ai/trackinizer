"""The terminal routes' machine lookup, over a fake connection, one branch at a time.

The routes end to end are in ``terminal_routes_pglite_test.py`` over PGlite. These tests
need no database, so they pin each branch of the lookup that decides whether a terminal
may open, and which CLIs its machine offers.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import uuid

from fastapi import HTTPException
from starlette.requests import Request
from treekle import from_plain

import pytest

from trackinizer.conftest import FakeEngine
from trackinizer.server.api import terminal_routes
from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import make_test_identity
from trackinizer.types.errors import NotFoundError
from trackinizer.wire.wire_machine_host import OFFLINE_AFTER_SEC


pytestmark = pytest.mark.asyncio

_MACHINE = uuid.UUID("9f1c2e3a-0000-4000-8000-000000000001")
_IDENTITY = make_test_identity()


def _request(conn: AsyncMock, monkeypatch: pytest.MonkeyPatch) -> Request:
    monkeypatch.setattr(app.state, "engine", FakeEngine(conn), raising=False)
    return Request({"type": "http", "app": app})


async def test_a_confirmed_machine_is_returned_with_the_clis_its_facts_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": str(_MACHINE),
        "facts": {"clis": ["claude", "git"]},
        "confirmed": True,
    }

    found = await terminal_routes._owned_machine(
        _request(conn, monkeypatch),
        _IDENTITY,
        name="gx10",
    )

    assert found == (_MACHINE, ["claude", "git"])


async def test_the_lookup_asks_for_an_online_machine_of_the_caller_with_a_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": str(_MACHINE),
        "facts": dict[str, object](),
        "confirmed": True,
    }

    _ = await terminal_routes._owned_machine(
        _request(conn, monkeypatch),
        _IDENTITY,
        name="gx10",
    )

    # The whole query, since any changed word in it lets an unscoped machine, or one
    # whose host is gone, be offered a terminal.
    assert from_plain(conn.fetchrow.call_args.args[0], str) == (
        "SELECT m.id, m.facts, m.confirmed_at IS NOT NULL AS confirmed "
        "FROM machines m WHERE m.name = $1 AND m.owner = $2 "
        "AND m.host_instance IS NOT NULL "
        "AND m.last_heartbeat > now() - make_interval(secs => $3) "
        "AND EXISTS (SELECT 1 FROM machine_credentials c "
        "WHERE c.machine_id = m.id AND c.revoked_at IS NULL)"
    )
    assert conn.fetchrow.call_args.args[1:] == (
        "gx10",
        _IDENTITY.email,
        OFFLINE_AFTER_SEC,
    )


async def test_an_unconfirmed_machine_is_refused_until_its_owner_confirms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": str(_MACHINE),
        "facts": dict[str, object](),
        "confirmed": False,
    }

    with pytest.raises(HTTPException) as refused:
        _ = await terminal_routes._owned_machine(
            _request(conn, monkeypatch),
            _IDENTITY,
            name="gx10",
        )

    detail = from_plain(refused.value.detail, dict[str, str])
    assert (refused.value.status_code, detail) == (
        409,
        {"code": "terminal_unconfirmed", "message": "confirm this machine first"},
    )


async def test_a_machine_the_caller_does_not_own_or_that_is_offline_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = None

    with pytest.raises(NotFoundError) as missing:
        _ = await terminal_routes._owned_machine(
            _request(conn, monkeypatch),
            _IDENTITY,
            name="gx10",
        )

    assert str(missing.value) == "no terminal-capable machine gx10"


async def test_facts_without_a_cli_list_offer_no_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": str(_MACHINE),
        "facts": {"os": "linux", "cpus": 20},
        "confirmed": True,
    }

    _, clis = await terminal_routes._owned_machine(
        _request(conn, monkeypatch),
        _IDENTITY,
        name="gx10",
    )

    assert clis == []


@pytest.mark.parametrize("listed", ["claude", True, 3])
async def test_facts_whose_cli_list_is_not_a_list_offer_no_cli(
    listed: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": str(_MACHINE),
        "facts": {"clis": listed},
        "confirmed": True,
    }

    _, clis = await terminal_routes._owned_machine(
        _request(conn, monkeypatch),
        _IDENTITY,
        name="gx10",
    )

    assert clis == []


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
