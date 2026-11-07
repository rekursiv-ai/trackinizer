"""Machine secrets: the shape they mint, what parses, and the ``last_used`` throttle."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import hashlib
import uuid

from fastapi import FastAPI, HTTPException
from starlette.requests import Request

import pytest

from trackinizer.conftest import FakeEngine, make_conn, set_field_row
from trackinizer.server import auth
from trackinizer.server.auth import LAST_USED_BUMP_INTERVAL_SEC
from trackinizer.server.machine_auth import (
    MachineIdentity,
    MintedSecret,
    current_machine,
    mint_secret,
    parse_credential,
    parse_enrollment,
    should_bump_last_used,
)
from trackinizer.types.errors import NotFoundError
from trackinizer.wire.wire_machine_host import (
    CREDENTIAL_PREFIX,
    ENROLLMENT_PREFIX,
)


if TYPE_CHECKING:
    from trackinizer.mock_test_support import MockConnection


def test_a_minted_credential_parses_to_its_id_and_the_digest_of_its_secret() -> None:
    minted = mint_secret(CREDENTIAL_PREFIX)

    parsed = parse_credential(minted.token)

    assert parsed is not None
    assert parsed.id == minted.id
    assert parsed.digest == minted.digest
    secret = minted.token[-43:]
    assert minted.digest == hashlib.sha256(secret.encode()).digest()
    assert minted.token == f"{CREDENTIAL_PREFIX}{minted.id.hex}_{secret}"
    assert len(minted.token) == len(CREDENTIAL_PREFIX) + 32 + 1 + 43


def test_a_minted_enrollment_token_parses_only_as_an_enrollment() -> None:
    minted = mint_secret(ENROLLMENT_PREFIX)

    assert parse_enrollment(minted.token) is not None
    assert parse_credential(minted.token) is None
    assert parse_enrollment(mint_secret(CREDENTIAL_PREFIX).token) is None


def test_two_secrets_never_share_an_id_or_a_digest() -> None:
    first = mint_secret(CREDENTIAL_PREFIX)
    second = mint_secret(CREDENTIAL_PREFIX)

    assert first.id != second.id
    assert first.digest != second.digest


def test_the_secret_does_not_appear_in_a_repr() -> None:
    minted = mint_secret(CREDENTIAL_PREFIX)

    assert minted.token not in repr(minted)


_ID = "0" * 32
_SECRET = "A" * 43


@pytest.mark.parametrize(
    "text",
    [
        "",
        f"trax_{_ID}_{_SECRET}",
        f"{CREDENTIAL_PREFIX}{_ID}_{_SECRET}\n",
        f"{CREDENTIAL_PREFIX}{_ID}_{_SECRET} ",
        f" {CREDENTIAL_PREFIX}{_ID}_{_SECRET}",
        f"{CREDENTIAL_PREFIX}{'A' * 32}_{_SECRET}",
        f"{CREDENTIAL_PREFIX}{'0' * 31}_{_SECRET}",
        f"{CREDENTIAL_PREFIX}{_ID}_{'A' * 42}",
        f"{CREDENTIAL_PREFIX}{_ID}_{'A' * 44}",
        f"{CREDENTIAL_PREFIX}{_ID}_{'A' * 42}.",
        f"{CREDENTIAL_PREFIX}{_ID}-{_SECRET}",
        f"{CREDENTIAL_PREFIX}{_ID}_{_SECRET}_{_SECRET}",
    ],
)
def test_a_malformed_credential_does_not_parse(text: str) -> None:
    assert parse_credential(text) is None


def test_a_credential_in_the_exact_shape_parses() -> None:
    parsed = parse_credential(f"{CREDENTIAL_PREFIX}{_ID}_{_SECRET}")

    assert parsed is not None
    assert parsed.id == uuid.UUID(int=0)


def test_last_used_is_due_on_the_first_request_and_once_per_interval() -> None:
    bumped: dict[uuid.UUID, float] = {}
    credential = uuid.uuid4()
    interval = LAST_USED_BUMP_INTERVAL_SEC

    first = should_bump_last_used(bumped, credential, now=100.0)
    inside = should_bump_last_used(bumped, credential, now=100.0 + interval - 1)
    past = should_bump_last_used(bumped, credential, now=100.0 + interval)
    after_past = should_bump_last_used(bumped, credential, now=100.0 + interval + 1)

    assert [first, inside, past, after_past] == [True, False, True, False]


def test_last_used_is_tracked_per_credential() -> None:
    bumped: dict[uuid.UUID, float] = {}

    one = should_bump_last_used(bumped, uuid.uuid4(), now=1.0)
    other = should_bump_last_used(bumped, uuid.uuid4(), now=1.0)

    assert (one, other) == (True, True)


def test_the_throttle_forgets_everything_rather_than_grow_without_bound() -> None:
    bumped: dict[uuid.UUID, float] = {}
    sizes: list[int] = []
    for _ in range(2_000):
        should_bump_last_used(bumped, uuid.uuid4(), now=1.0)
        sizes.append(len(bumped))

    assert max(sizes) == 1_024


_MACHINE = uuid.UUID(int=5)


def _host(conn: MockConnection, *, bearer: str) -> Request:
    """Return a host request presenting ``bearer`` to an app over ``conn``."""
    app = FastAPI()
    app.state.engine = FakeEngine(conn)
    headers = [(b"authorization", f"Bearer {bearer}".encode())]
    return Request({"type": "http", "app": app, "headers": headers})


def _credential_row(
    minted: MintedSecret,
    *,
    machine_id: uuid.UUID = _MACHINE,
    revoked_at: datetime | None = None,
) -> dict[str, object]:
    """Return the row the credential lookup reads for ``minted``."""
    return {
        "secret_sha256": minted.digest,
        "revoked_at": revoked_at,
        "machine_id": machine_id,
        "name": "gx10",
    }


class TestCurrentMachine:
    @pytest.mark.asyncio
    async def test_a_live_credential_names_its_machine_and_stamps_last_used_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(auth, "monotonic_clock", lambda: 100.0)
        minted = mint_secret(CREDENTIAL_PREFIX)
        conn = make_conn()
        set_field_row(conn, _credential_row(minted))
        request = _host(conn, bearer=minted.token)

        first = await current_machine(request, _MACHINE)
        again = await current_machine(request, _MACHINE)

        assert (
            first
            == again
            == MachineIdentity(
                machine_id=_MACHINE,
                name="gx10",
                credential_id=minted.id,
            )
        )
        assert conn.fetchrow.await_args is not None
        assert conn.fetchrow.await_args.args[1] == minted.id
        conn.execute.assert_awaited_once_with(
            "UPDATE machine_credentials SET last_used = now() WHERE id = $1",
            minted.id,
        )

    @pytest.mark.asyncio
    async def test_a_malformed_bearer_is_401_before_any_lookup(self) -> None:
        conn = make_conn()

        with pytest.raises(HTTPException) as refused:
            await current_machine(_host(conn, bearer="trax_nope"), _MACHINE)

        assert (refused.value.status_code, refused.value.detail) == (
            401,
            "invalid machine credential",
        )
        conn.fetchrow.assert_not_awaited()

    @pytest.mark.parametrize("known", [False, True], ids=["unknown", "wrong secret"])
    @pytest.mark.asyncio
    async def test_an_unknown_or_wrong_secret_credential_is_the_same_401(
        self,
        *,
        known: bool,
    ) -> None:
        minted = mint_secret(CREDENTIAL_PREFIX)
        conn = make_conn()
        set_field_row(
            conn,
            _credential_row(mint_secret(CREDENTIAL_PREFIX)) if known else None,
        )

        with pytest.raises(HTTPException) as refused:
            await current_machine(_host(conn, bearer=minted.token), _MACHINE)

        assert (refused.value.status_code, refused.value.detail) == (
            401,
            "invalid machine credential",
        )
        conn.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_revoked_credential_is_410(self) -> None:
        minted = mint_secret(CREDENTIAL_PREFIX)
        conn = make_conn()
        set_field_row(
            conn,
            _credential_row(minted, revoked_at=datetime(2026, 1, 1, tzinfo=UTC)),
        )

        with pytest.raises(HTTPException) as refused:
            await current_machine(_host(conn, bearer=minted.token), _MACHINE)

        assert (refused.value.status_code, refused.value.detail) == (
            410,
            "machine_revoked",
        )

    @pytest.mark.asyncio
    async def test_a_credential_on_another_machines_path_is_not_found(self) -> None:
        minted = mint_secret(CREDENTIAL_PREFIX)
        conn = make_conn()
        set_field_row(conn, _credential_row(minted, machine_id=uuid.UUID(int=6)))

        with pytest.raises(NotFoundError, match="machine is not registered"):
            await current_machine(_host(conn, bearer=minted.token), _MACHINE)

        conn.execute.assert_not_awaited()


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
