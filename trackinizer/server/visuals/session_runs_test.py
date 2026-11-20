"""Tests for who opened a session."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast

import uuid

import pytest

from trackinizer.conftest import make_conn
from trackinizer.server.visuals.session_runs import opener_email


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn, PGliteEngine


_USER: Final = uuid.UUID("11111111-1111-1111-1111-111111111111")
_USER_KEY: Final = uuid.UUID("44444444-4444-4444-4444-444444444441")


async def _seed(engine: PGliteEngine) -> None:
    async with engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, 'user@example.com', 'T', 'writer', 'active')",
            _USER,
        )
        await conn.execute(
            "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
            "VALUES ($1, $2, 'k', 'h', 'trax_0441', 'writer')",
            _USER_KEY,
            _USER,
        )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_opener_email_names_the_account_of_a_key(
    pglite_db: PGliteEngine,
) -> None:
    """The opening key says whose session it is."""
    await _seed(pglite_db)
    async with pglite_db.acquire() as conn:
        assert await opener_email(conn, api_key_id=_USER_KEY) == "user@example.com"
        assert await opener_email(conn, api_key_id=uuid.uuid4()) is None
        assert await opener_email(conn, api_key_id=None) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("email", ["owner@example.com", None])
async def test_opener_email_returns_the_key_owner_or_absence(email: str | None) -> None:
    conn = make_conn()
    conn.fetchval.return_value = email
    assert await opener_email(cast("Conn", conn), api_key_id=None) is None
    conn.fetchval.assert_not_awaited()
    assert await opener_email(cast("Conn", conn), api_key_id=_USER_KEY) == email
    conn.fetchval.assert_awaited_once_with(
        "SELECT member.email FROM api_keys AS credential "
        "JOIN users AS member ON member.id = credential.user_id "
        "WHERE credential.id = $1",
        _USER_KEY,
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
