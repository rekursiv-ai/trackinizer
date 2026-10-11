"""The audience vocabulary: which values exist and what a value may look like."""

from __future__ import annotations

from pydantic import BaseModel, ValidationError

import pytest

from trackinizer.wire.audience import (
    AUDIENCE_SHAPE,
    AUDIENCES,
    OPEN,
    STAFF,
    AudienceName,
    validate_audience,
)


class _Holder(BaseModel):
    audience: AudienceName


def test_the_accepted_values_are_open_and_staff() -> None:
    assert frozenset({OPEN, STAFF}) == AUDIENCES
    assert (OPEN, STAFF) == ("open", "staff")


@pytest.mark.parametrize("value", sorted(AUDIENCES))
def test_an_accepted_value_passes_through(value: str) -> None:
    assert validate_audience(value) == value
    assert _Holder(audience=value).audience == value


@pytest.mark.parametrize(
    "value",
    ["", "Open", "staff ", "gold", "0open", "a" * 33, "o.pen"],
)
def test_any_other_value_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="audience"):
        validate_audience(value)
    with pytest.raises(ValidationError):
        _Holder(audience=value)


def test_the_shape_names_what_a_later_value_may_look_like() -> None:
    """The column check admits a shape; the accepted set is a constant, not a CHECK."""
    assert AUDIENCE_SHAPE == "^[a-z][a-z0-9_-]{0,31}$"


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
