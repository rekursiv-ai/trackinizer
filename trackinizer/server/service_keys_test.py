"""Contracts for the service keys Trackinizer keeps, on a fake connection.

``config_test.py`` reads the setting.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, cast

import stat
import uuid

import pytest

from trackinizer.server.auth import TOKEN_PREFIX_LEN, Role, hash_secret
from trackinizer.server.config import ServiceKeys
from trackinizer.server.service_keys import ensure_service_keys


if TYPE_CHECKING:
    from pathlib import Path

    from trackinizer.lib.postgres import Conn


_ACCOUNT: Final = uuid.UUID("11111111-1111-1111-1111-111111111111")
_KEY: Final = uuid.UUID("22222222-2222-2222-2222-222222222222")
_HELD: Final = "trax_held-key-0123456789"
_LOCK: Final = "SELECT pg_advisory_lock(hashtext('trackinizer.service_keys'))"
_UNLOCK: Final = "SELECT pg_advisory_unlock(hashtext('trackinizer.service_keys'))"


class _Conn:
    """Answers with one users row and the account's keys, and records every statement."""

    def __init__(
        self,
        *,
        account: dict[str, object] | None,
        keys: list[dict[str, object]],
    ) -> None:
        self.account = account
        self.keys = keys
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.fetched: list[tuple[object, ...]] = []

    async def execute(self, sql: str, *args: object) -> str:
        self.executed.append((sql, args))
        return "UPDATE 0"

    async def fetchrow(self, sql: str, *args: object) -> dict[str, object] | None:
        del sql, args
        return self.account

    async def fetch(self, sql: str, *args: object) -> list[dict[str, object]]:
        del sql
        self.fetched.append(args)
        return self.keys

    async def fetchval(self, sql: str, *args: object) -> str:
        del sql, args
        return "writer"

    def statements(self, start: str) -> list[tuple[object, ...]]:
        """Return the arguments of each statement that begins with ``start``."""
        return [args for sql, args in self.executed if sql.startswith(start)]


class _Told:
    """Stands in for the access policy, noting each account it is told of."""

    def __init__(self) -> None:
        self.users: list[tuple[Conn, str, Role]] = []

    async def on_user(self, conn: Conn, *, email: str, role: Role) -> None:
        self.users.append((conn, email, role))


@pytest.fixture(autouse=True)
def cheap_scrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hash secrets at the smallest accepted cost so a test pays no scrypt round."""
    monkeypatch.setattr("trackinizer.server.auth._SCRYPT_N", 2)


@pytest.fixture
def named(tmp_path: Path) -> Path:
    """Where the one account the tests name, ``chat`` at ``writer``, keeps its key."""
    return tmp_path


def _active(role: str = "writer", status: str = "active") -> dict[str, object]:
    return {"id": _ACCOUNT, "role": role, "status": status}


def _held_key(secret: str = _HELD) -> dict[str, object]:
    """Return the api_keys row of ``secret``: live, unpinned, at ``writer``."""
    return {
        "id": _KEY,
        "secret_hash": hash_secret(secret),
        "role": "writer",
        "open_into": None,
        "revoked_at": None,
    }


async def _ensure(conn: _Conn, directory: Path, told: _Told | None = None) -> None:
    await ensure_service_keys(
        cast("Conn", conn),
        keys=ServiceKeys(accounts=(("chat", "writer"),), directory=directory),
        policy=told or _Told(),
    )


@pytest.mark.asyncio
async def test_a_missing_account_is_made_told_and_keyed(
    named: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    conn = _Conn(account=None, keys=[])
    told = _Told()

    await _ensure(conn, named, told)

    assert (conn.executed[0], conn.executed[-1]) == ((_LOCK, ()), (_UNLOCK, ()))
    [(account_id, email, name, role)] = conn.statements("INSERT INTO users")
    assert (email, name, role) == ("chat@service.invalid", "chat", "writer")
    assert isinstance(account_id, uuid.UUID)
    assert told.users == [(conn, "chat@service.invalid", "writer")]
    [minted] = conn.statements("INSERT INTO api_keys")
    assert minted[1:3] == (account_id, "chat-service")
    assert minted[5:] == ("writer", None, None)
    [revoke] = conn.statements("UPDATE api_keys SET revoked_at")
    assert revoke == (account_id, minted[0])
    key = (named / "chat").read_text()
    assert key.startswith(cast(str, minted[4]))
    assert key.endswith("\n")
    assert stat.S_IMODE((named / "chat").stat().st_mode) == 0o600
    assert conn.fetched == []
    assert caplog.messages == [
        (
            "event=trackinizer_service_key account_email='chat@service.invalid' "
            f"key_id={minted[0]} minted=True revoked=0"
        ),
    ]


@pytest.mark.asyncio
async def test_a_working_key_is_kept_and_every_other_revoked(
    named: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    (named / "chat").write_text(f"{_HELD}\n")
    conn = _Conn(account=_active(), keys=[_held_key()])
    told = _Told()

    await _ensure(conn, named, told)

    assert (named / "chat").read_text() == f"{_HELD}\n"
    assert told.users == []
    assert conn.statements("INSERT") == []
    assert conn.statements("UPDATE api_keys SET revoked_at") == [(_ACCOUNT, _KEY)]
    assert conn.fetched == [(_ACCOUNT, _HELD[:TOKEN_PREFIX_LEN])]
    assert caplog.messages == [
        (
            "event=trackinizer_service_key account_email='chat@service.invalid' "
            f"key_id={_KEY} minted=False revoked=0"
        ),
    ]


@pytest.mark.parametrize(
    ("secret", "changed"),
    [
        (_HELD, {"revoked_at": datetime(2026, 11, 7, tzinfo=UTC)}),
        (_HELD, {"open_into": "canvas:0"}),
        (_HELD, {"role": "viewer"}),
        (_HELD + "other", {}),
    ],
    ids=["revoked", "pinned", "another-role", "only-the-prefix"],
)
@pytest.mark.asyncio
async def test_a_key_in_the_file_that_does_not_work_is_replaced(
    named: Path,
    secret: str,
    changed: dict[str, object],
) -> None:
    (named / "chat").write_text(f"{_HELD}\n")
    conn = _Conn(account=_active(), keys=[{**_held_key(secret), **changed}])

    await _ensure(conn, named)

    [minted] = conn.statements("INSERT INTO api_keys")
    assert (named / "chat").read_text().startswith(cast(str, minted[4]))
    assert conn.statements("UPDATE api_keys SET revoked_at") == [(_ACCOUNT, minted[0])]


@pytest.mark.asyncio
async def test_a_second_start_keeps_the_key_the_first_wrote(named: Path) -> None:
    first = _Conn(account=None, keys=[])
    await _ensure(first, named)
    [(key_id, _user, _name, secret_hash, _prefix, role, pin, _machine)] = (
        first.statements("INSERT INTO api_keys")
    )
    key = (named / "chat").read_text()
    second = _Conn(
        account=_active(),
        keys=[
            {
                "id": key_id,
                "secret_hash": secret_hash,
                "role": role,
                "open_into": pin,
                "revoked_at": None,
            },
        ],
    )
    told = _Told()

    await _ensure(second, named, told)

    assert (named / "chat").read_text() == key
    assert second.statements("INSERT") == []
    assert told.users == []


@pytest.mark.parametrize(
    ("role", "status"),
    [("viewer", "active"), ("writer", "disabled")],
)
@pytest.mark.asyncio
async def test_an_account_not_as_named_is_refused(
    named: Path,
    role: str,
    status: str,
) -> None:
    conn = _Conn(account=_active(role, status), keys=[])

    with pytest.raises(ValueError, match=f"is {status} with role {role}, not active"):
        await _ensure(conn, named)

    assert not (named / "chat").exists()
    assert conn.statements("INSERT") == []
    assert conn.executed[-1] == (_UNLOCK, ())


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
