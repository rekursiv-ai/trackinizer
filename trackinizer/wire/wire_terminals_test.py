"""Terminal wire contract: frame kinds equal the posix ``Frame``, codes are distinct."""

from __future__ import annotations

import pydantic
import pytest

from trackinizer.lib.posix.host import Frame
from trackinizer.wire import wire_terminals
from trackinizer.wire.wire_terminals import OpenTerminal


def test_frame_kinds_equal_the_posix_frame() -> None:
    assert {kind: Frame(kind) for kind in (1, 2, 3, 5, 6, 7, 8)} == {
        wire_terminals.FRAME_ATTACH: Frame.ATTACH,
        wire_terminals.FRAME_INPUT: Frame.INPUT,
        wire_terminals.FRAME_RESIZE: Frame.RESIZE,
        wire_terminals.FRAME_STOP: Frame.STOP,
        wire_terminals.FRAME_OUTPUT: Frame.OUTPUT,
        wire_terminals.FRAME_ACK: Frame.ACK,
        wire_terminals.FRAME_EXIT: Frame.EXIT,
    }


def test_close_codes_are_distinct_and_final_ones_are_a_subset() -> None:
    codes = [
        wire_terminals.CLOSE_ENDED,
        wire_terminals.CLOSE_TAKEN_OVER,
        wire_terminals.CLOSE_IDLE,
        wire_terminals.CLOSE_LIFETIME,
        wire_terminals.CLOSE_HOST_LOST,
        wire_terminals.CLOSE_REFUSED,
        wire_terminals.CLOSE_MACHINE_REVOKED,
        wire_terminals.CLOSE_LIMIT,
    ]
    assert len(set(codes)) == len(codes)
    assert set(codes) > wire_terminals.FINAL_CLOSE_CODES
    assert wire_terminals.CLOSE_HOST_LOST not in wire_terminals.FINAL_CLOSE_CODES


@pytest.mark.parametrize(
    "body",
    [
        {"machine": "gx10", "rows": 0, "cols": 80},
        {"machine": "gx10", "rows": 24, "cols": 1_001},
        {"machine": "Bad Name", "rows": 24, "cols": 80},
        {"machine": "gx10", "rows": 24, "cols": 80, "host": "x"},
        {"machine": "gx10", "rows": 24, "cols": 80, "requested_by": "x" * 64},
        {"machine": "gx10", "rows": 24, "cols": 80, "requested_by": "a\nb"},
        {"machine": "gx10", "rows": 24, "cols": 80, "requested_by": "\x1b[31mred"},
        {"machine": "gx10", "rows": 24, "cols": 80, "requested_by": "calf\u00e9"},
    ],
)
def test_open_body_refuses_out_of_range_and_unknown_fields(
    body: dict[str, object],
) -> None:
    with pytest.raises(pydantic.ValidationError):
        OpenTerminal.model_validate(body)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
