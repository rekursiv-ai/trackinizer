"""Terminal wire contract: frame kinds equal the posix ``Frame``, codes are distinct."""

from __future__ import annotations

import uuid

import pydantic
import pytest

from trackinizer.lib.posix.host import Frame
from trackinizer.wire import wire_terminals
from trackinizer.wire.wire_terminals import (
    DirectSignal,
    OpenTerminal,
    TerminalOpened,
    TerminalTicket,
    TerminalWait,
)


def test_frame_kinds_equal_the_posix_frame() -> None:
    assert {kind: Frame(kind) for kind in (1, 2, 3, 5, 6, 7, 8, 9, 10, 11)} == {
        wire_terminals.FRAME_ATTACH: Frame.ATTACH,
        wire_terminals.FRAME_INPUT: Frame.INPUT,
        wire_terminals.FRAME_RESIZE: Frame.RESIZE,
        wire_terminals.FRAME_STOP: Frame.STOP,
        wire_terminals.FRAME_OUTPUT: Frame.OUTPUT,
        wire_terminals.FRAME_ACK: Frame.ACK,
        wire_terminals.FRAME_EXIT: Frame.EXIT,
        wire_terminals.FRAME_SIGNAL: Frame.SIGNAL,
        wire_terminals.FRAME_ROUTE: Frame.ROUTE,
        wire_terminals.FRAME_KEEPALIVE: Frame.KEEPALIVE,
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
        {"machine": "gx10", "rows": 24, "cols": 80, "surface": "has space"},
        {"machine": "gx10", "rows": 24, "cols": 80, "surface": "s" * 65},
        {"machine": "gx10", "rows": 24, "cols": 80, "surface": ""},
    ],
)
def test_open_body_refuses_out_of_range_and_unknown_fields(
    body: dict[str, object],
) -> None:
    with pytest.raises(pydantic.ValidationError):
        OpenTerminal.model_validate(body)


@pytest.mark.parametrize("surface", ["chat", str(uuid.UUID(int=7))])
def test_open_body_takes_a_surface_key(surface: str) -> None:
    body = {"machine": "gx10", "rows": 24, "cols": 80, "surface": surface}
    assert OpenTerminal.model_validate(body).surface == surface


@pytest.mark.parametrize(
    "body",
    [
        {"gen": 0, "type": "offer", "sdp": "v=0"},
        {"gen": 1, "type": "candidate", "sdp": "v=0"},
        {"gen": 1, "type": "offer", "sdp": "x" * 16_385},
        {"gen": 1, "type": "offer", "sdp": "v=0", "extra": 1},
    ],
)
def test_a_signal_refuses_a_bad_generation_type_size_or_field(
    body: dict[str, object],
) -> None:
    with pytest.raises(pydantic.ValidationError):
        DirectSignal.model_validate(body)


def test_a_signal_round_trips_and_an_abandon_needs_no_description() -> None:
    offer = DirectSignal(gen=2, type="offer", sdp="v=0")
    assert DirectSignal.model_validate_json(offer.model_dump_json()) == offer
    assert DirectSignal(gen=2, type="abandon").sdp == ""


def test_a_route_payload_round_trips_and_refuses_the_wrong_shape() -> None:
    payload = wire_terminals.encode_route(wire_terminals.ROUTE_DIRECT, 2**56 + 300)

    assert payload == b"\x01" + (2**56 + 300).to_bytes(8)
    assert wire_terminals.decode_route(payload) == (
        wire_terminals.ROUTE_DIRECT,
        2**56 + 300,
    )
    for bad in (b"", payload[:-1], b"\x02" + (0).to_bytes(8)):
        with pytest.raises(ValueError, match="ROUTE"):
            wire_terminals.decode_route(bad)


def test_a_keepalive_payload_is_the_idle_milliseconds() -> None:
    assert wire_terminals.encode_keepalive(1_500) == (1_500).to_bytes(4)
    assert wire_terminals.encode_keepalive(2**32 - 2) == b"\xff\xff\xff\xfe"
    assert wire_terminals.encode_keepalive(2**32 - 1) == b"\xff\xff\xff\xff"
    assert wire_terminals.encode_keepalive(2**40) == b"\xff\xff\xff\xff"
    assert wire_terminals.decode_keepalive((1_500).to_bytes(4)) == 1_500
    with pytest.raises(ValueError, match="KEEPALIVE"):
        wire_terminals.decode_keepalive(b"\x00")


def test_the_ice_servers_default_to_none_and_are_stun_only() -> None:
    opened = {"terminal_id": uuid.UUID(int=1), "ticket": "t" * 43}
    assert TerminalOpened.model_validate(opened).ice_servers == []
    assert TerminalTicket.model_validate({"ticket": "t"}).ice_servers == []
    stun = ["stun:stun.example.org:3478"]
    carried = TerminalOpened.model_validate({**opened, "ice_servers": stun})
    assert carried.ice_servers == stun
    wait = {"terminal_id": uuid.UUID(int=1), "opener": "a@x.y", "rows": 1, "cols": 1}
    assert (
        TerminalWait.model_validate({**wait, "ice_servers": stun}).ice_servers == stun
    )
    for bad in (["turn:relay.example.org"], ["stun:a b"], ["stun:x"] * 5):
        with pytest.raises(pydantic.ValidationError):
            TerminalOpened.model_validate({**opened, "ice_servers": bad})


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
