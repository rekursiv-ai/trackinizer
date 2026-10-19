"""The host side of a direct link, against a fake peer: no WebRTC library is needed."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import asyncio

import pytest

from trackinizer.lib.posix.host import Frame, decode_frame, encode_frame, encode_winsize
from trackinizer.trax.terminal_direct import DirectRouter
from trackinizer.wire import wire_terminals
from trackinizer.wire.wire_terminals import DirectSignal


pytestmark = pytest.mark.asyncio


@dataclass(slots=True, kw_only=True, eq=False)
class _Peer:
    """A data channel that records what it is sent and lets a test feed it."""

    sent: list[tuple[Frame, bytes]] = field(default_factory=list)
    is_open: bool = True
    lingers: bool = False
    """Still reports open after a close, as a channel does while it winds down."""

    closed: bool = False
    offer: str | None = None
    ice_servers: tuple[str, ...] = ()
    fail: bool = False
    feed: Callable[[bytes], None] = lambda _: None
    drop: Callable[[], None] = lambda: None

    async def accept(
        self,
        offer_sdp: str,
        *,
        ice_servers: tuple[str, ...],
        on_message: Callable[[bytes], None],
        on_close: Callable[[], None],
    ) -> str:
        await asyncio.sleep(0)
        self.feed, self.drop = on_message, on_close
        if self.fail:
            raise OSError("ice failed")
        self.offer, self.ice_servers = offer_sdp, ice_servers
        return "answer-sdp"

    def send(self, data: bytes) -> None:
        self.sent.append(decode_frame(data))

    async def close(self) -> None:
        self.closed = True
        self.is_open = self.lingers


@dataclass(slots=True, kw_only=True)
class _Shell:
    written: list[bytes] = field(default_factory=list)
    sizes: list[tuple[int, int]] = field(default_factory=list)

    async def write(self, data: bytes) -> bool:
        self.written.append(data)
        return True

    def set_winsize(self, rows: int, cols: int) -> None:
        self.sizes.append((rows, cols))


class _World:
    def __init__(self, *, peers: int = 1, ice_servers: tuple[str, ...] = ()) -> None:
        self.relay: list[tuple[Frame, bytes]] = []
        self.shell = _Shell()
        self.now = 100.0
        self.ticks: asyncio.Queue[None] = asyncio.Queue()
        self.peers = [_Peer() for _ in range(peers)]
        self.made = 0
        self.router = DirectRouter(
            relay=self._relay,
            shell=self.shell,
            factory=self._make if peers else None,
            ice_servers=ice_servers,
            clock=lambda: self.now,
            sleep=self._sleep,
        )

    async def _relay(self, data: bytes) -> None:
        self.relay.append(decode_frame(data))

    async def _sleep(self, seconds: float) -> None:
        assert seconds == wire_terminals.KEEPALIVE_SEC
        await self.ticks.get()

    def _make(self) -> _Peer:
        self.made += 1
        return self.peers[self.made - 1]

    @property
    def peer(self) -> _Peer:
        return self.peers[0]

    async def offer(self, *, gen: int = 1) -> None:
        signal = DirectSignal(gen=gen, type="offer", sdp="offer-sdp")
        await self.router.from_relay(Frame.SIGNAL, signal.model_dump_json().encode())
        await self.settle()

    async def settle(self) -> None:
        for _ in range(8):
            await asyncio.sleep(0)

    def dc(
        self,
        kind: Frame,
        payload: bytes = b"",
        *,
        peer: _Peer | None = None,
    ) -> None:
        (peer or self.peer).feed(encode_frame(kind, payload))

    def answers(self) -> list[DirectSignal]:
        return [
            DirectSignal.model_validate_json(payload)
            for kind, payload in self.relay
            if kind is Frame.SIGNAL
        ]

    async def upgraded(self, *, offset: int = 0) -> None:
        await self.offer()
        self.dc(
            Frame.ROUTE,
            wire_terminals.encode_route(wire_terminals.ROUTE_DIRECT, offset),
        )
        await self.settle()
        del self.relay[:]
        del self.peer.sent[:]


async def test_without_a_webrtc_library_an_offer_is_abandoned_and_output_stays_on_the_relay() -> (
    None
):
    world = _World(peers=0)

    await world.offer()
    await world.router.output(b"$ ")

    assert world.answers() == [DirectSignal(gen=1, type="abandon")]
    assert world.relay[-1] == (Frame.OUTPUT, b"$ ")


async def test_an_offer_is_answered_with_the_peers_description_and_the_stun_list() -> (
    None
):
    world = _World(ice_servers=("stun:s.test:3478",))

    await world.offer(gen=3)

    assert world.answers() == [DirectSignal(gen=3, type="answer", sdp="answer-sdp")]
    assert (world.peer.offer, world.peer.ice_servers) == (
        "offer-sdp",
        ("stun:s.test:3478",),
    )


async def test_a_peer_that_cannot_answer_is_closed_logged_and_never_heard_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    world = _World()
    world.peer.fail = True
    world.peer.lingers = True

    await world.offer()
    world.dc(Frame.ROUTE, wire_terminals.encode_route(wire_terminals.ROUTE_DIRECT, 0))
    await world.settle()
    await world.router.output(b"x")
    await world.router.exit(0)

    assert world.answers() == [DirectSignal(gen=1, type="abandon")]
    assert world.peer.closed
    assert world.peer.sent == []
    assert world.relay[-2:] == [(Frame.OUTPUT, b"x"), (Frame.EXIT, (0).to_bytes(4))]
    (record,) = caplog.records
    assert record.getMessage() == "direct link: could not answer an offer"
    assert record.exc_info


async def test_an_offer_that_is_not_newer_or_not_an_offer_is_ignored() -> None:
    world = _World(peers=2)
    await world.offer(gen=2)

    await world.offer(gen=2)
    await world.offer(gen=1)
    await world.router.from_relay(Frame.SIGNAL, b"not json")
    await world.router.from_relay(
        Frame.SIGNAL,
        DirectSignal(gen=9, type="answer", sdp="x").model_dump_json().encode(),
    )

    assert (world.made, len(world.answers())) == (1, 1)


async def test_a_newer_offer_replaces_the_peer() -> None:
    world = _World(peers=2)
    await world.offer(gen=1)

    await world.offer(gen=2)

    assert (world.peers[0].closed, world.peers[1].closed, world.made) == (
        True,
        False,
        2,
    )
    assert [each.gen for each in world.answers()] == [1, 2]


async def test_output_goes_to_the_relay_until_the_browser_switches_the_route() -> None:
    world = _World()
    await world.offer()

    await world.router.output(b"abc")

    assert world.relay[-1] == (Frame.OUTPUT, b"abc")
    assert world.peer.sent == []


async def test_the_browsers_route_gets_an_ack_the_ring_from_its_offset_then_live_output() -> (
    None
):
    world = _World()
    await world.router.output(b"abcdef")
    await world.offer()
    del world.relay[:]

    world.dc(Frame.ROUTE, wire_terminals.encode_route(wire_terminals.ROUTE_DIRECT, 2))
    await world.settle()
    await world.router.output(b"gh")

    assert world.peer.sent == [
        (Frame.ACK, (2).to_bytes(8)),
        (Frame.OUTPUT, b"cdef"),
        (Frame.OUTPUT, b"gh"),
    ]
    assert [kind for kind, _ in world.relay] == [Frame.KEEPALIVE]


async def test_direct_frames_are_cut_to_the_size_a_data_channel_carries() -> None:
    world = _World()
    await world.upgraded()
    chunk = bytes(range(256)) * 160

    await world.router.output(chunk)

    sizes = [len(payload) for _, payload in world.peer.sent]
    assert max(sizes) == wire_terminals.DIRECT_FRAME_BYTES
    assert b"".join(payload for _, payload in world.peer.sent) == chunk


async def test_a_route_to_a_peer_that_is_not_open_changes_nothing() -> None:
    world = _World()
    await world.offer()
    world.peer.is_open = False

    world.dc(Frame.ROUTE, wire_terminals.encode_route(wire_terminals.ROUTE_DIRECT, 0))
    await world.settle()
    await world.router.output(b"x")

    assert world.relay[-1] == (Frame.OUTPUT, b"x")


async def test_the_relay_fence_is_answered_on_the_channel_whichever_comes_first() -> (
    None
):
    world = _World()
    await world.router.output(b"ab")
    await world.upgraded()
    fence = wire_terminals.encode_route(wire_terminals.ROUTE_DIRECT, 2)

    await world.router.from_relay(Frame.ROUTE, fence)
    assert world.peer.sent == [(Frame.ROUTE, fence)]

    early = _World()
    await early.router.output(b"ab")
    await early.offer()
    await early.router.from_relay(Frame.ROUTE, fence)
    assert early.peer.sent == []
    early.dc(Frame.ROUTE, wire_terminals.encode_route(wire_terminals.ROUTE_DIRECT, 0))
    await early.settle()
    assert early.peer.sent[-1] == (Frame.ROUTE, fence)


async def test_input_and_resize_from_either_path_reach_the_shell_in_order() -> None:
    world = _World()
    await world.upgraded()

    await world.router.from_relay(Frame.INPUT, b"a")
    world.dc(Frame.INPUT, b"b")
    world.dc(Frame.RESIZE, encode_winsize(9_999, 0))
    world.dc(Frame.INPUT, b"c")
    await world.settle()

    assert world.shell.written == [b"a", b"b", b"c"]
    assert world.shell.sizes == [(wire_terminals.MAX_ROWS, 1)]


async def test_input_over_the_channel_is_capped_per_second_and_the_cap_is_inclusive() -> (
    None
):
    world = _World()
    await world.upgraded()
    big = b"x" * wire_terminals.MAX_INPUT_FRAME_BYTES
    frames = wire_terminals.MAX_INPUT_BYTES_PER_SEC // len(big)

    for _ in range(frames):
        world.dc(Frame.INPUT, big)
    await world.settle()
    assert world.shell.written == [big] * frames
    assert not world.peer.closed

    # A second later is a new window: the whole cap fits again.
    world.now += 1.0
    for _ in range(frames):
        world.dc(Frame.INPUT, big)
    await world.settle()
    assert world.shell.written == [big] * (2 * frames)
    assert not world.peer.closed

    world.dc(Frame.INPUT, b"over")
    await world.settle()
    assert world.peer.closed
    assert b"over" not in world.shell.written


async def test_a_malformed_frame_on_the_channel_closes_it() -> None:
    world = _World()
    await world.upgraded()

    world.peer.feed(b"\x02\x00")
    await world.settle()

    assert world.peer.closed


async def test_a_dropped_channel_keeps_output_in_the_ring_until_the_browser_asks_for_the_relay() -> (
    None
):
    world = _World()
    await world.upgraded()
    await world.router.output(b"abc")

    world.peer.drop()
    await world.router.output(b"def")
    assert world.relay == []
    assert world.peer.sent == [(Frame.OUTPUT, b"abc")]

    await world.router.from_relay(
        Frame.ROUTE,
        wire_terminals.encode_route(wire_terminals.ROUTE_RELAY, 3),
    )
    await world.router.output(b"g")

    assert world.relay == [
        (Frame.ACK, (3).to_bytes(8)),
        (Frame.OUTPUT, b"def"),
        (Frame.OUTPUT, b"g"),
    ]


async def test_a_return_to_the_relay_while_already_on_it_sends_nothing() -> None:
    world = _World()
    await world.router.output(b"abc")
    del world.relay[:]

    await world.router.from_relay(
        Frame.ROUTE,
        wire_terminals.encode_route(wire_terminals.ROUTE_RELAY, 0),
    )

    assert world.relay == []


async def test_a_return_resumes_at_the_start_of_the_ring_when_the_offset_fell_out() -> (
    None
):
    world = _World()
    await world.upgraded()
    await world.router.output(b"x" * (wire_terminals.RING_BYTES + 5))

    await world.router.from_relay(
        Frame.ROUTE,
        wire_terminals.encode_route(wire_terminals.ROUTE_RELAY, 0),
    )

    assert world.relay[0] == (Frame.ACK, (5).to_bytes(8))
    assert len(world.relay[1][1]) == wire_terminals.RING_BYTES


async def test_a_keepalive_reports_the_idle_time_every_period_while_output_goes_direct() -> (
    None
):
    world = _World()
    await world.router.from_relay(Frame.INPUT, b"a")
    world.now += 4
    await world.offer()

    world.dc(Frame.ROUTE, wire_terminals.encode_route(wire_terminals.ROUTE_DIRECT, 0))
    await world.settle()
    world.now += 10
    world.ticks.put_nowait(None)
    await world.settle()

    reports = [
        wire_terminals.decode_keepalive(payload)
        for kind, payload in world.relay
        if kind is Frame.KEEPALIVE
    ]
    assert reports == [4_000, 14_000]


async def test_the_keepalive_stops_once_output_is_back_on_the_relay() -> None:
    world = _World()
    await world.upgraded()
    await world.router.from_relay(
        Frame.ROUTE,
        wire_terminals.encode_route(wire_terminals.ROUTE_RELAY, 0),
    )
    del world.relay[:]

    world.ticks.put_nowait(None)
    await world.settle()

    assert all(kind is not Frame.KEEPALIVE for kind, _ in world.relay)


async def test_the_exit_goes_to_the_browser_on_both_paths() -> None:
    world = _World()
    await world.upgraded()

    await world.router.exit(7)

    assert world.peer.sent == [(Frame.EXIT, (7).to_bytes(4))]
    assert world.relay == [(Frame.EXIT, (7).to_bytes(4))]


async def test_closing_the_router_closes_the_peer_and_stops_its_tasks() -> None:
    world = _World()
    await world.upgraded()

    await world.router.close()
    world.ticks.put_nowait(None)
    await world.settle()

    assert world.peer.closed
    assert all(kind is not Frame.KEEPALIVE for kind, _ in world.relay)


async def test_consecutive_chunks_go_out_once_each_on_either_route() -> None:
    world = _World()
    await world.router.output(b"ab")
    await world.router.output(b"cd")
    assert world.relay == [(Frame.OUTPUT, b"ab"), (Frame.OUTPUT, b"cd")]

    await world.upgraded(offset=4)
    await world.router.output(b"ef")
    await world.router.output(b"gh")

    assert world.peer.sent == [(Frame.OUTPUT, b"ef"), (Frame.OUTPUT, b"gh")]


async def test_a_resize_from_the_relay_is_clamped_to_one_row_at_least() -> None:
    world = _World()

    await world.router.from_relay(Frame.RESIZE, encode_winsize(0, 80))
    await world.router.from_relay(Frame.RESIZE, b"bad")

    assert world.shell.sizes == [(1, 80)]


async def test_an_answer_still_pending_when_the_router_closes_is_never_sent() -> None:
    world = _World()
    signal = DirectSignal(gen=1, type="offer", sdp="offer-sdp")
    await world.router.from_relay(Frame.SIGNAL, signal.model_dump_json().encode())

    await world.router.close()
    await world.settle()

    assert world.answers() == []
    assert world.peer.closed


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
