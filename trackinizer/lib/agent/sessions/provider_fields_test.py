"""Tests for :mod:`trackinizer.lib.agent.sessions.provider_fields`."""

from __future__ import annotations

import pytest

from trackinizer.lib.agent.sessions.provider_fields import read_or_default


@pytest.mark.parametrize("value", [None, "failed", ["a"], 3])
def test_a_missing_or_mistyped_object_reads_as_the_default(value: object) -> None:
    assert read_or_default(value, dict[str, object], default=dict[str, object]()) == {}


def test_a_present_value_of_the_target_type_is_read() -> None:
    assert read_or_default(
        {"a": 1},
        dict[str, object],
        default=dict[str, object](),
    ) == {"a": 1}
    assert read_or_default("x", str, default=None) == "x"


def test_a_mistyped_value_reads_as_none_when_the_default_is_none() -> None:
    assert read_or_default(7, str, default=None) is None


def test_a_missing_value_is_the_default_even_where_none_would_fit() -> None:
    """``object`` takes ``None`` as it is, yet a null field still reads as absent."""
    assert read_or_default(None, object, default="absent") == "absent"


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
