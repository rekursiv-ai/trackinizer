"""Tests for the service-account wire contract: the address form and the bodies."""

from __future__ import annotations

import re

from pydantic import ValidationError

import pytest

from trackinizer.wire.wire_service_accounts import (
    NAME_PATTERN,
    SERVICE_ACCOUNT_KEY_REVOKE_PATH,
    SERVICE_ACCOUNT_TOKENS_PATH,
    SERVICE_ACCOUNTS_PATH,
    SERVICE_DOMAIN,
    ServiceAccountCreate,
    ServiceTokenCreate,
    is_service_address,
    service_address,
)


def test_a_service_address_is_the_name_at_the_reserved_domain() -> None:
    assert SERVICE_DOMAIN == "service.invalid"
    assert service_address("campaign") == "campaign@service.invalid"


@pytest.mark.parametrize(
    "address",
    [
        "campaign@service.invalid",
        "CAMPAIGN@SERVICE.INVALID",
        "  campaign@service.invalid  ",
        "*@service.invalid",
        "a@b@service.invalid",
        "@service.invalid",
    ],
)
def test_an_address_at_the_service_domain_is_a_service_address(address: str) -> None:
    assert is_service_address(address)


@pytest.mark.parametrize(
    "address",
    [
        "",
        "campaign",
        "service.invalid",
        "campaign@example.com",
        "campaign@service.invalid.example.com",
        "campaign@notservice.invalid",
        "campaign@service.invalid.",
        "campaign@invalid",
        "service.invalid@example.com",
    ],
)
def test_a_lookalike_is_not_a_service_address(address: str) -> None:
    assert not is_service_address(address)


@pytest.mark.parametrize("name", ["ab", "campaign", "a-b-c", "a" + "0" * 30])
def test_a_name_is_two_to_thirty_one_lowercase_characters_from_a_letter(
    name: str,
) -> None:
    assert re.fullmatch(NAME_PATTERN, name)
    assert ServiceAccountCreate(name=name, role="writer").name == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "a",
        "a" + "0" * 31,
        "Campaign",
        "1abc",
        "-abc",
        "ab_c",
        "ab.c",
        "ab c",
        "ab\n",
        "ab@service.invalid",
    ],
)
def test_a_name_outside_the_pattern_is_refused(name: str) -> None:
    assert not re.fullmatch(NAME_PATTERN, name)
    with pytest.raises(ValidationError):
        _ = ServiceAccountCreate(name=name, role="writer")


@pytest.mark.parametrize("role", ["admin", "superuser", ""])
def test_a_service_account_is_created_as_a_viewer_or_a_writer(role: str) -> None:
    with pytest.raises(ValidationError):
        _ = ServiceAccountCreate.model_validate({"name": "campaign", "role": role})
    for allowed in ("viewer", "writer"):
        made = ServiceAccountCreate.model_validate(
            {"name": "campaign", "role": allowed},
        )
        assert made.role == allowed


@pytest.mark.parametrize(
    "extra",
    [
        {"kind": "human"},
        {"email": "boss@example.com"},
        {"staff": True},
        {"status": "disabled"},
        {"hosted_domain": "example.com"},
    ],
)
def test_a_create_body_naming_a_field_it_does_not_define_is_refused(
    extra: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _ = ServiceAccountCreate.model_validate(
            {"name": "campaign", "role": "writer", **extra},
        )


def test_a_token_body_defaults_to_the_accounts_role_and_no_pin() -> None:
    body = ServiceTokenCreate(name="host")
    assert (body.role, body.open_into) == (None, None)


def test_a_token_body_names_only_an_accepted_audience_and_defined_fields() -> None:
    assert ServiceTokenCreate(name="host", open_into="staff").open_into == "staff"
    for bad in (
        {"open_into": "gold"},
        {"open_into": "Staff"},
        {"read_audiences": ["open"]},
        {"role": "superuser"},
        {"name": ""},
        {"kind": "human"},
    ):
        with pytest.raises(ValidationError):
            _ = ServiceTokenCreate.model_validate({"name": "host", **bad})


def test_the_paths_are_under_the_admin_prefix() -> None:
    assert SERVICE_ACCOUNTS_PATH == "/api/admin/service-accounts"
    assert f"{SERVICE_ACCOUNTS_PATH}/{{user_id}}/tokens" == SERVICE_ACCOUNT_TOKENS_PATH
    assert (
        f"{SERVICE_ACCOUNT_TOKENS_PATH}/{{key_id}}/revoke"
        == SERVICE_ACCOUNT_KEY_REVOKE_PATH
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
