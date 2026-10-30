"""Tests for canvas keys: the id's first 12 hex digits, and the ids that start with them."""

from __future__ import annotations

import re
import uuid

from trackinizer.wire.wire_canvas_keys import (
    CANVAS_KEY_PATTERN,
    canvas_key,
    key_bounds,
)


_CANVAS = uuid.UUID("c103f7c7-0ef7-4ab9-81da-c96d2ae1bfa0")


def test_a_key_is_the_ids_first_twelve_hex_digits() -> None:
    assert canvas_key(_CANVAS) == "c103f7c70ef7"
    assert re.fullmatch(CANVAS_KEY_PATTERN, canvas_key(_CANVAS))
    assert not re.fullmatch(CANVAS_KEY_PATTERN, "C103F7C70EF7")
    assert not re.fullmatch(CANVAS_KEY_PATTERN, "c103f7c70ef")
    assert not re.fullmatch(CANVAS_KEY_PATTERN, str(_CANVAS))


def test_the_bounds_hold_every_id_with_the_key_and_no_other() -> None:
    low, high = key_bounds("c103f7c70ef7")
    assert low == uuid.UUID("c103f7c7-0ef7-0000-0000-000000000000")
    assert high == uuid.UUID("c103f7c7-0ef7-ffff-ffff-ffffffffffff")
    assert low <= _CANVAS <= high
    assert uuid.UUID("c103f7c7-0ef8-0000-0000-000000000000") > high
    assert uuid.UUID("c103f7c7-0ef6-ffff-ffff-ffffffffffff") < low


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
