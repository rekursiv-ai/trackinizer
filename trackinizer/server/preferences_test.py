"""Tests for ``server/preferences.py``: reading a user's stored preferences."""

from __future__ import annotations

import pydantic
import pytest

from trackinizer.server.preferences import Preferences, stored_preferences


_DEFAULTS = {
    "theme": "dark",
    "chat_style": "default",
    "chat_side": "left",
    "highlight_mentions": True,
}


def test_nothing_stored_reads_as_the_defaults() -> None:
    assert stored_preferences(None).model_dump() == _DEFAULTS


def test_the_keys_set_override_their_defaults_and_unknown_ones_are_dropped() -> None:
    read = stored_preferences(
        {"theme": "system", "highlight_mentions": False, "retired": "x"},
    )
    assert read.model_dump() == {
        **_DEFAULTS,
        "theme": "system",
        "highlight_mentions": False,
    }


def test_a_value_its_key_does_not_take_is_refused() -> None:
    with pytest.raises(pydantic.ValidationError):
        Preferences.model_validate({"chat_side": "middle"})


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
