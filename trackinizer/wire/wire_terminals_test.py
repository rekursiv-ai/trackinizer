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
    TerminalHost,
    TerminalHosts,
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
        wire_terminals.CLOSE_DAEMON_LOST,
        wire_terminals.CLOSE_REFUSED,
        wire_terminals.CLOSE_HOST_REVOKED,
    ]
    assert len(set(codes)) == len(codes)
    assert set(codes) > wire_terminals.FINAL_CLOSE_CODES
    assert wire_terminals.CLOSE_DAEMON_LOST not in wire_terminals.FINAL_CLOSE_CODES


@pytest.mark.parametrize(
    "body",
    [
        {"host": "gx10", "rows": 0, "cols": 80},
        {"host": "gx10", "rows": 24, "cols": 1_001},
        {"host": "Bad Name", "rows": 24, "cols": 80},
        {"host": "gx10", "rows": 24, "cols": 80, "hostname": "x"},
        {"host": "gx10", "rows": 24, "cols": 80, "requested_by": "x" * 64},
        {"host": "gx10", "rows": 24, "cols": 80, "requested_by": "a\nb"},
        {"host": "gx10", "rows": 24, "cols": 80, "requested_by": "\x1b[31mred"},
        {"host": "gx10", "rows": 24, "cols": 80, "requested_by": "calf\u00e9"},
        {"host": "gx10", "rows": 24, "cols": 80, "surface": "has space"},
        {"host": "gx10", "rows": 24, "cols": 80, "surface": "s" * 65},
        {"host": "gx10", "rows": 24, "cols": 80, "surface": ""},
    ],
)
def test_open_body_refuses_out_of_range_and_unknown_fields(
    body: dict[str, object],
) -> None:
    with pytest.raises(pydantic.ValidationError):
        OpenTerminal.model_validate(body)


@pytest.mark.parametrize("surface", ["chat", str(uuid.UUID(int=7))])
def test_open_body_takes_a_surface_key(surface: str) -> None:
    body = {"host": "gx10", "rows": 24, "cols": 80, "surface": surface}
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


@pytest.mark.parametrize(
    "route",
    [wire_terminals.ROUTE_RELAY, wire_terminals.ROUTE_DIRECT],
)
def test_a_route_payload_round_trips_and_refuses_the_wrong_shape(route: int) -> None:
    payload = wire_terminals.encode_route(route, 2**57 + 300)

    assert payload == bytes([route]) + (2**57 + 300).to_bytes(8)
    assert wire_terminals.decode_route(payload) == (route, 2**57 + 300)
    for bad in (b"", payload[:-1], b"\x02" + (0).to_bytes(8)):
        with pytest.raises(ValueError, match="ROUTE"):
            wire_terminals.decode_route(bad)


def test_a_keepalive_payload_is_the_input_then_the_output_idle_milliseconds() -> None:
    payload = wire_terminals.encode_keepalive(1_500, output_idle_ms=7)
    assert payload == (1_500).to_bytes(4) + (7).to_bytes(4)
    assert wire_terminals.decode_keepalive(payload) == (1_500, 7)
    assert wire_terminals.encode_keepalive(2**32 - 2, output_idle_ms=0)[:4] == (
        b"\xff\xff\xff\xfe"
    )
    assert wire_terminals.encode_keepalive(2**32 - 1, output_idle_ms=2**40) == (
        b"\xff\xff\xff\xff" * 2
    )
    assert wire_terminals.encode_keepalive(2**40, output_idle_ms=0)[:4] == (
        b"\xff\xff\xff\xff"
    )


def test_a_keepalive_from_a_host_before_output_idle_time_names_input_alone() -> None:
    assert wire_terminals.decode_keepalive((1_500).to_bytes(4)) == (1_500, None)
    for bad in (b"\x00", b"\x00" * 5, b"\x00" * 9):
        with pytest.raises(ValueError, match="KEEPALIVE"):
            wire_terminals.decode_keepalive(bad)


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


@pytest.mark.parametrize(
    "folder",
    ["/", "/srv/project", "~", "~/project", "/" + "x" * 1_023],
)
def test_a_shells_folder_is_absolute_or_home_relative_and_takes_1024_characters(
    folder: str,
) -> None:
    base = {"host": "gx10", "rows": 24, "cols": 80}
    assert OpenTerminal.model_validate({**base, "cwd": folder}).cwd == folder


@pytest.mark.parametrize(
    "folder",
    ["loop", "./loop", "", "/a\nb", "/a\x1b[31mred", "/" + "x" * 1_024],
)
def test_a_shells_folder_that_is_relative_empty_or_has_a_control_is_refused(
    folder: str,
) -> None:
    base = {"host": "gx10", "rows": 24, "cols": 80}
    with pytest.raises(pydantic.ValidationError):
        OpenTerminal.model_validate({**base, "cwd": folder})


def test_a_terminal_is_a_shell_and_an_open_or_a_wait_names_no_other_kind() -> None:
    """A host's files are read with its tools, as an agent's are, not over a terminal."""
    base = {"host": "gx10", "rows": 24, "cols": 80}
    wait = {"terminal_id": uuid.UUID(int=1), "opener": "a@x.y", "rows": 1, "cols": 1}
    assert OpenTerminal.model_validate(base)
    assert TerminalWait.model_validate(wait)
    with pytest.raises(pydantic.ValidationError, match="kind"):
        OpenTerminal.model_validate({**base, "kind": "files"})
    with pytest.raises(pydantic.ValidationError, match="kind"):
        TerminalWait.model_validate({**wait, "kind": "files"})


def test_the_host_is_handed_the_folder_its_shell_starts_in() -> None:
    wait = {"terminal_id": uuid.UUID(int=1), "opener": "a@x.y", "rows": 1, "cols": 1}
    handed = TerminalWait.model_validate({**wait, "cwd": "~/project"})
    assert handed.cwd == "~/project"
    plain = TerminalWait(terminal_id=uuid.UUID(int=1), opener="a@x.y", rows=1, cols=1)
    assert plain.cwd is None
    with pytest.raises(pydantic.ValidationError):
        TerminalWait.model_validate({**wait, "cwd": "loop"})


def test_a_host_lists_the_clis_its_facts_name_and_defaults_to_none() -> None:
    host = {"name": "gx10", "status": "online", "terminal_ready": True}
    assert TerminalHost.model_validate({**host, "confirmed": True}).clis == []
    listed = {**host, "confirmed": True, "clis": ["claude", "codex"]}
    assert TerminalHost.model_validate(listed).clis == ["claude", "codex"]


def test_a_listing_and_an_open_ignore_a_field_a_newer_server_added() -> None:
    run = {"session_id": str(uuid.UUID(int=3)), "cli": "claude", "lane": 1}
    host = {
        "name": "gx10",
        "status": "online",
        "terminal_ready": True,
        "confirmed": True,
        "runs": [run],
        "lane": 1,
    }
    live = {"terminal_id": str(uuid.UUID(int=2)), "host": "gx10", "lane": 1}
    listing = TerminalHosts.model_validate(
        {"hosts": [host], "terminals": [live], "lane": 1},
    )
    opened = TerminalOpened.model_validate(
        {"terminal_id": str(uuid.UUID(int=1)), "ticket": "t", "lane": 1},
    )

    assert [run.cli for run in listing.hosts[0].runs] == ["claude"]
    assert [term.host for term in listing.terminals] == ["gx10"]
    assert opened.ticket == "t"


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
