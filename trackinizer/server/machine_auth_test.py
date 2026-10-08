"""Machine secrets: the shape they mint, what parses, and the ``last_used`` throttle."""

from __future__ import annotations

import hashlib
import uuid

import pytest

from trackinizer.server.auth import LAST_USED_BUMP_INTERVAL_SEC
from trackinizer.server.machine_auth import (
    mint_secret,
    parse_credential,
    parse_enrollment,
    should_bump_last_used,
)
from trackinizer.wire.wire_machine_host import (
    CREDENTIAL_PREFIX,
    ENROLLMENT_PREFIX,
)


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


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
