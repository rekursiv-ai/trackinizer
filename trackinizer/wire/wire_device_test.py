"""Device login wire models: the codes people read, and what a poll may carry."""

from __future__ import annotations

import pydantic
import pytest

from trackinizer.wire.wire_device import (
    USER_CODE_ALPHABET,
    USER_CODE_LENGTH,
    DeviceCode,
    DeviceCodeRequest,
    DeviceToken,
    DeviceTokenRequest,
    IssuedKey,
    format_user_code,
    normalize_user_code,
)


_KEY = IssuedKey(api_key="trax_x", role="writer", email="jp@example.com")


def test_a_code_is_eight_consonants_shown_in_two_groups() -> None:
    assert USER_CODE_LENGTH == 8
    assert set(USER_CODE_ALPHABET).isdisjoint("AEIOU01")
    assert format_user_code("BCDFGHJK") == "BCDF-GHJK"


@pytest.mark.parametrize(
    "typed",
    ["bcdf-ghjk", " BCDF GHJK ", "BCDFGHJK", "b-c-d-f-g-h-j-k"],
)
def test_a_typed_code_is_stored_without_case_dashes_or_spaces(typed: str) -> None:
    assert normalize_user_code(typed) == "BCDFGHJK"


def test_a_key_comes_with_an_approval_and_only_with_one() -> None:
    assert DeviceToken(status="approved", issued=_KEY).issued == _KEY
    with pytest.raises(ValueError, match="a key comes with an approval"):
        DeviceToken(status="approved")
    with pytest.raises(ValueError, match="a key comes with an approval"):
        DeviceToken(status="pending", issued=_KEY)


def test_a_rejected_poll_secret_is_not_quoted() -> None:
    with pytest.raises(pydantic.ValidationError) as raised:
        DeviceTokenRequest(device_code="SECRETMARKER" * 20)

    assert "SECRETMARKER" not in str(raised.value)


def test_the_secrets_stay_out_of_reprs() -> None:
    assert "trax_x" not in repr(_KEY)
    assert "trax_x" not in repr(DeviceToken(status="approved", issued=_KEY))


@pytest.mark.parametrize(
    "fields",
    [
        {"client": "Bad"},
        {"client": "ok", "host": "-x"},
        {"client": "ok", "machine": "A"},
    ],
)
def test_a_code_request_is_checked_field_by_field(fields: dict[str, str]) -> None:
    with pytest.raises(pydantic.ValidationError):
        DeviceCodeRequest.model_validate(fields)


def test_a_login_answer_ignores_a_field_a_newer_server_added() -> None:
    code = DeviceCode.model_validate(
        {
            "device_code": "d",
            "user_code": "BCDF-GHJK",
            "verification_uri": "https://x.y/auth/device",
            "verification_uri_complete": "https://x.y/auth/device?code=BCDFGHJK",
            "expires_in": 600,
            "interval": 5,
            "lane": 1,
        },
    )
    issued = {**_KEY.model_dump(), "lane": 1}
    token = DeviceToken.model_validate(
        {"status": "approved", "issued": issued, "lane": 1},
    )

    assert (code.user_code, token.issued) == ("BCDF-GHJK", _KEY)
    with pytest.raises(pydantic.ValidationError, match="Extra inputs"):
        DeviceCodeRequest.model_validate({"client": "ok", "lane": 1})


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
