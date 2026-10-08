"""Tests for the variable wire models: name pattern, value rule, extra keys."""

from __future__ import annotations

from datetime import UTC, datetime

import re

import pydantic
import pytest

from trackinizer.wire.wire_variables import (
    NAME_PATTERN,
    Variable,
    VariableList,
    VariablePut,
)


@pytest.mark.parametrize(
    "name",
    ["A", "_", "_x", "region", "Region_2", "x" * 128],
    ids=["letter", "underscore", "leading-underscore", "lower", "digit", "longest"],
)
def test_a_valid_name_is_accepted_by_the_pattern_and_the_model(name: str) -> None:
    assert re.fullmatch(NAME_PATTERN, name)
    assert Variable.model_validate(_variable(name=name)).name == name


@pytest.mark.parametrize(
    "name",
    ["", "1A", "a-b", "a b", "a/b", "a.b", "é", "x" * 129, "A\n", "\nA"],
    ids=[
        "empty",
        "leading-digit",
        "dash",
        "space",
        "slash",
        "dot",
        "non-ascii",
        "too-long",
        "trailing-newline",
        "leading-newline",
    ],
)
def test_an_invalid_name_is_refused_by_the_pattern_and_the_model(name: str) -> None:
    assert re.fullmatch(NAME_PATTERN, name) is None
    with pytest.raises(pydantic.ValidationError, match="String should match pattern"):
        Variable.model_validate(_variable(name=name))


def test_a_put_defaults_to_a_plain_value() -> None:
    put = VariablePut(value="eu-west")
    assert (put.value, put.secret) == ("eu-west", False)


def test_a_put_takes_one_character_and_keeps_whitespace() -> None:
    assert VariablePut(value="x").value == "x"
    assert VariablePut(value=" \n").value == " \n"


def test_a_put_refuses_an_empty_value() -> None:
    with pytest.raises(pydantic.ValidationError, match="at least 1 character"):
        VariablePut(value="")


@pytest.mark.parametrize("secret", [False, True])
def test_a_put_round_trips_through_json(secret: bool) -> None:
    put = VariablePut(value="v", secret=secret)
    assert VariablePut.model_validate_json(put.model_dump_json()) == put


def test_a_put_refuses_an_extra_key() -> None:
    with pytest.raises(
        pydantic.ValidationError,
        match="Extra inputs are not permitted",
    ):
        VariablePut.model_validate({"value": "v", "layer": "user"})


def test_a_variable_refuses_an_extra_key() -> None:
    with pytest.raises(
        pydantic.ValidationError,
        match="Extra inputs are not permitted",
    ):
        Variable.model_validate(_variable(extra=1))


def test_a_variable_refuses_an_unknown_layer() -> None:
    with pytest.raises(pydantic.ValidationError, match="Input should be"):
        Variable.model_validate(_variable(layer="team"))


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"secret": True, "value": None},
        {"layer": "machine", "owner": "gpu-box"},
        {"layer": "user", "owner": "scout@example.com"},
    ],
    ids=["plain", "secret", "machine", "user"],
)
def test_a_variable_round_trips_through_json(fields: dict[str, object]) -> None:
    variable = Variable.model_validate(_variable(**fields))
    assert Variable.model_validate_json(variable.model_dump_json()) == variable


def test_a_variable_list_refuses_an_extra_key() -> None:
    with pytest.raises(
        pydantic.ValidationError,
        match="Extra inputs are not permitted",
    ):
        VariableList.model_validate({"variables": [], "next": None})


def _variable(**changes: object) -> dict[str, object]:
    """Return the fields of a plain org variable, with ``changes`` applied."""
    return {
        "layer": "org",
        "owner": "",
        "name": "REGION",
        "secret": False,
        "value": "eu-west",
        "updated_by": "scout",
        "updated": datetime(2026, 10, 6, tzinfo=UTC),
    } | changes


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
