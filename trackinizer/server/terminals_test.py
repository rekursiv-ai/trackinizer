"""The terminal relay: relay, replay, takeover, the limits and the sweep, on a fake clock."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import override

import asyncio
import uuid

import pytest

from trackinizer.lib.posix.host import Frame, decode_frame, encode_frame, encode_winsize
from trackinizer.lib.posix.ring import Ring
from trackinizer.server.terminals import (
    Opening,
    SocketClosedError,
    Terminal,
    TerminalLimitError,
    TerminalRelay,
    decode_attach,
    sweep_loop,
)
from trackinizer.wire import wire_terminals


_HOST = uuid.UUID(int=1)
_CANVAS = uuid.UUID(int=2)
_START = 1_000.0
"""Where every test's clock starts, and so when its relay started."""

_KEEPALIVE = wire_terminals.encode_keepalive(0, output_idle_ms=0)
"""A keepalive saying input and output came just now."""


@dataclass(slots=True, kw_only=True)
class _Socket:
    sent: list[tuple[Frame, bytes]] = field(default_factory=list)
    closed: list[int] = field(default_factory=list)
    gone: bool = False
    limit: int | None = None
    """Sends it takes before the peer is gone."""

    broken_close: bool = False
    """Raises from ``close`` as uvicorn does once the peer has dropped."""

    # Both yield, as an ASGI send does, so another task can run between two sends.
    async def send_bytes(self, data: bytes) -> None:
        await asyncio.sleep(0)
        if self.gone or (self.limit is not None and len(self.sent) >= self.limit):
            raise SocketClosedError
        self.sent.append(decode_frame(data))

    async def close(self, code: int) -> None:
        await asyncio.sleep(0)
        self.closed.append(code)
        if self.broken_close:
            raise OSError("peer gone")

    def kinds(self) -> list[Frame]:
        return [kind for kind, _ in self.sent]


class _Clock:
    def __init__(self) -> None:
        self.now = _START

    def __call__(self) -> float:
        return self.now


class _BlockedSocket(_Socket):
    def __init__(self) -> None:
        super().__init__()
        self.sending = asyncio.Event()
        self.release = asyncio.Event()

    @override
    async def send_bytes(self, data: bytes) -> None:
        self.sending.set()
        await self.release.wait()
        await super().send_bytes(data)

    @override
    async def close(self, code: int) -> None:
        self.gone = True
        self.release.set()
        await super().close(code)


class _FailingSocket(_Socket):
    """A host whose sends fail the way a closed ASGI socket's do: not as a close."""

    @override
    async def send_bytes(self, data: bytes) -> None:
        if not self.sent and decode_frame(data)[0] is Frame.ROUTE:
            await super().send_bytes(data)
            return
        raise RuntimeError("Cannot call send once a close message has been sent")


class _ClosingSocket(_Socket):
    def __init__(self) -> None:
        super().__init__()
        self.closing = asyncio.Event()
        self.release = asyncio.Event()

    @override
    async def close(self, code: int) -> None:
        self.closing.set()
        await self.release.wait()
        await super().close(code)


class _World:
    def __init__(self) -> None:
        self.clock = _Clock()
        self.dead: set[uuid.UUID] = set()
        self.broken: set[uuid.UUID] = set()
        """Terminals whose liveness question raises."""
        self.slept: list[float] = []
        self.relay = TerminalRelay(
            alive=self._alive,
            clock=self.clock,
            sleep=self._sleep,
        )

    async def _alive(self, term: Terminal) -> bool:
        if term.id in self.broken:
            raise OSError("database blip")
        return term.id not in self.dead

    async def _sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    def open(
        self,
        *,
        user: str = "a@x.y",
        host_id: uuid.UUID = _HOST,
        canvas: uuid.UUID | None = None,
        surface: str | None = None,
    ) -> Terminal:
        return self.relay.open(
            Opening(
                user=user,
                host_id=host_id,
                host="gx10",
                rows=24,
                cols=80,
                canvas=canvas,
                surface=surface,
            ),
        )

    async def live(self) -> tuple[Terminal, _Socket, _Socket]:
        term = self.open()
        host, browser = _Socket(), _Socket()
        assert await self.relay.daemon_connected(term, host)
        await self.relay.attach_browser(term, browser, offset=0)
        assert host.sent == [
            (Frame.ROUTE, wire_terminals.encode_route(wire_terminals.ROUTE_RELAY, 0)),
        ]
        host.sent.clear()
        return term, host, browser


def test_the_relay_keeps_the_stun_servers_for_both_connection_sockets() -> None:
    world = _World()
    servers = ("stun:stun.example.org:3478",)
    relay = TerminalRelay(alive=world._alive, ice_servers=servers)

    assert relay.ice_servers == servers


@pytest.mark.asyncio
async def test_output_reaches_the_browser_and_ring_and_input_the_host() -> None:
    world = _World()
    term, host, browser = await world.live()

    await world.relay.from_daemon(term, Frame.OUTPUT, b"$ ")
    await world.relay.from_browser(term, Frame.INPUT, b"ls\n")

    assert browser.sent[-1] == (Frame.OUTPUT, b"$ ")
    assert host.sent == [(Frame.INPUT, b"ls\n")]
    assert term.ring.since(0)[0] == b"$ "


@pytest.mark.asyncio
async def test_a_browser_reattaching_resumes_from_its_offset() -> None:
    world = _World()
    term, _, first = await world.live()
    await world.relay.from_daemon(term, Frame.OUTPUT, b"abcdef")
    world.relay.detach_browser(term, first)

    second = _Socket()
    await world.relay.attach_browser(term, second, offset=2)

    assert second.sent == [
        (Frame.ACK, (2).to_bytes(8)),
        (Frame.OUTPUT, b"cdef"),
    ]


@pytest.mark.asyncio
async def test_an_offset_past_the_ring_gets_the_whole_ring_after_a_notice() -> None:
    world = _World()
    term, _, first = await world.live()
    term.ring = Ring(capacity=4)
    await world.relay.from_daemon(term, Frame.OUTPUT, b"abcdefgh")
    world.relay.detach_browser(term, first)

    second = _Socket()
    await world.relay.attach_browser(term, second, offset=1)

    assert second.sent == [
        (Frame.OUTPUT, b"\r\n--- output lost ---\r\n"),
        (Frame.ACK, (4).to_bytes(8)),
        (Frame.OUTPUT, b"efgh"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 1, 2])
async def test_a_browser_that_leaves_mid_replay_detaches_whichever_send_it_fails(
    limit: int,
) -> None:
    world = _World()
    term = world.open()
    term.ring = Ring(capacity=3)
    assert await world.relay.daemon_connected(term, _Socket())
    await world.relay.from_daemon(term, Frame.OUTPUT, b"abcdef")
    browser = _Socket(limit=limit)

    await world.relay.attach_browser(term, browser, offset=0)

    assert (term.browser, term.detached_at is None) == (None, False)


@pytest.mark.asyncio
async def test_a_browser_that_leaves_while_output_queued_behind_the_replay_detaches() -> (
    None
):
    world = _World()
    term = world.open()
    assert await world.relay.daemon_connected(term, _Socket())
    await world.relay.from_daemon(term, Frame.OUTPUT, b"ab")
    browser = _Socket(limit=2)

    attaching = asyncio.create_task(world.relay.attach_browser(term, browser, offset=0))
    await asyncio.sleep(0)
    await world.relay.from_daemon(term, Frame.OUTPUT, b"cd")
    await attaching

    assert term.browser is None


@pytest.mark.asyncio
async def test_a_second_browser_takes_over_and_closes_the_first_with_4001() -> None:
    world = _World()
    term, _, first = await world.live()
    second = _Socket()

    await world.relay.attach_browser(term, second, offset=0)
    world.relay.detach_browser(term, first)

    assert first.closed == [wire_terminals.CLOSE_TAKEN_OVER]
    assert term.browser is second


@pytest.mark.asyncio
async def test_a_browser_that_left_is_stopped_after_the_grace_and_not_before() -> None:
    world = _World()
    term, host, browser = await world.live()
    world.relay.detach_browser(term, browser)

    world.clock.now += wire_terminals.DETACH_GRACE_SEC - 1
    await world.relay.sweep()
    assert not term.ended

    world.clock.now += 1
    await world.relay.sweep()
    assert not term.ended
    assert host.sent == []

    world.clock.now += 1
    await world.relay.sweep()
    assert term.ended
    assert (Frame.STOP, b"") in host.sent
    assert host.closed == [wire_terminals.CLOSE_ENDED]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seconds", "code"),
    [
        (wire_terminals.IDLE_SEC + 1, wire_terminals.CLOSE_IDLE),
        (wire_terminals.LIFETIME_SEC + 1, wire_terminals.CLOSE_LIFETIME),
    ],
)
async def test_idle_and_lifetime_close_the_terminal(seconds: int, code: int) -> None:
    world = _World()
    term, _, browser = await world.live()
    world.clock.now += seconds
    if code == wire_terminals.CLOSE_LIFETIME:
        term.last_input_at = world.clock.now

    await world.relay.sweep()

    assert browser.closed == [code]


@pytest.mark.asyncio
async def test_input_resets_the_idle_clock() -> None:
    world = _World()
    term, _, browser = await world.live()
    world.clock.now += wire_terminals.IDLE_SEC - 1
    await world.relay.from_browser(term, Frame.INPUT, b"x")
    world.clock.now += wire_terminals.IDLE_SEC - 1

    await world.relay.sweep()

    assert not term.ended
    world.clock.now += 2
    await world.relay.sweep()
    assert browser.closed == [wire_terminals.CLOSE_IDLE]
    assert world.relay.terminal(term.id) is None


@pytest.mark.asyncio
async def test_no_count_of_live_terminals_refuses_an_open() -> None:
    world = _World()
    for _ in range(70):
        world.open(canvas=_CANVAS)
        world.clock.now += 7

    assert len(world.relay.live_in("a@x.y", canvas=_CANVAS)) == 70


@pytest.mark.asyncio
async def test_opens_per_minute_are_limited_and_the_window_slides() -> None:
    world = _World()
    for _ in range(wire_terminals.MAX_OPENS_PER_MIN_PER_USER):
        term = world.open(host_id=uuid.uuid4())
        await world.relay.end(term, code=wire_terminals.CLOSE_ENDED)
    with pytest.raises(TerminalLimitError):
        world.open(host_id=uuid.uuid4())
    world.clock.now += 59.5
    with pytest.raises(TerminalLimitError):
        world.open(host_id=uuid.uuid4())

    # A minute after them, to the second, every one of those opens has left the window.
    world.clock.now += 0.5
    for _ in range(wire_terminals.MAX_OPENS_PER_MIN_PER_USER):
        term = world.open(host_id=uuid.uuid4())
        await world.relay.end(term, code=wire_terminals.CLOSE_ENDED)
    with pytest.raises(TerminalLimitError):
        world.open(host_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_a_canvas_lists_each_of_the_users_live_terminals_oldest_first() -> None:
    world = _World()
    first = world.open(canvas=_CANVAS, surface="chat")
    second = world.open(canvas=_CANVAS, surface="tile")
    world.open(canvas=uuid.UUID(int=3))
    world.open(user="b@x.y", canvas=_CANVAS)

    assert world.relay.live_in("a@x.y", canvas=_CANVAS) == [first, second]
    assert [t.surface for t in world.relay.live_in("a@x.y", canvas=_CANVAS)] == [
        "chat",
        "tile",
    ]
    await world.relay.end(first, code=wire_terminals.CLOSE_ENDED)
    assert world.relay.live_in("a@x.y", canvas=_CANVAS) == [second]


@pytest.mark.asyncio
async def test_a_terminal_told_to_stop_is_no_longer_listed_for_its_canvas() -> None:
    world = _World()
    closing, host, _ = await world.live()
    closing.canvas = _CANVAS
    other = world.open(canvas=_CANVAS)

    await world.relay.stop(closing)

    assert host.sent == [(Frame.STOP, b"")]
    assert not closing.ended
    assert world.relay.live_in("a@x.y", canvas=_CANVAS) == [other]


@pytest.mark.asyncio
async def test_an_untaken_open_fails_after_twenty_seconds() -> None:
    world = _World()
    term = world.open()
    browser = _Socket()
    await world.relay.attach_browser(term, browser, offset=0)

    world.clock.now += wire_terminals.OPEN_TTL_SEC + 1
    await world.relay.sweep()

    assert term.ended
    assert browser.closed == [wire_terminals.CLOSE_DAEMON_LOST]
    assert world.relay.terminal(term.id) is None


@pytest.mark.asyncio
async def test_a_ticket_is_one_use_short_lived_and_bound() -> None:
    world = _World()
    term = world.open()

    # Each check has a ticket of its own: spending one must not make a later one pass
    # for the wrong reason.
    wrong_user = world.relay.mint_ticket(term)
    wrong_terminal = world.relay.mint_ticket(term)
    expiring = world.relay.mint_ticket(term)
    world.clock.now += wire_terminals.TICKET_TTL_SEC + 1
    fresh = world.relay.mint_ticket(term)

    assert not world.relay.redeem(wrong_user, user="b@x.y", terminal_id=term.id)
    assert not world.relay.redeem(
        wrong_terminal,
        user="a@x.y",
        terminal_id=uuid.uuid4(),
    )
    assert not world.relay.redeem(expiring, user="a@x.y", terminal_id=term.id)
    assert world.relay.redeem(fresh, user="a@x.y", terminal_id=term.id)
    assert not world.relay.redeem(fresh, user="a@x.y", terminal_id=term.id)
    assert len(fresh) == wire_terminals.TICKET_CHARS


@pytest.mark.asyncio
async def test_a_ticket_is_spent_by_the_second_it_expires() -> None:
    world = _World()
    term = world.open()
    ticket = world.relay.mint_ticket(term)
    world.clock.now += wire_terminals.TICKET_TTL_SEC

    assert not world.relay.redeem(ticket, user="a@x.y", terminal_id=term.id)


@pytest.mark.asyncio
async def test_close_host_sends_4410_to_both_sockets() -> None:
    world = _World()
    term, host, browser = await world.live()

    await world.relay.close_host(_HOST)

    assert host.closed == browser.closed == [wire_terminals.CLOSE_HOST_REVOKED]
    assert term.ended


@pytest.mark.asyncio
async def test_the_sweep_ends_a_terminal_whose_owner_or_credential_died() -> None:
    world = _World()
    term, _, browser = await world.live()
    world.dead.add(term.id)

    await world.relay.sweep()

    assert browser.closed == [wire_terminals.CLOSE_HOST_REVOKED]


@pytest.mark.asyncio
async def test_a_lost_host_ends_the_terminal_with_4004() -> None:
    world = _World()
    term, host, browser = await world.live()

    await world.relay.daemon_lost(term, host)

    assert browser.closed == [wire_terminals.CLOSE_DAEMON_LOST]


@pytest.mark.asyncio
async def test_the_shells_exit_reaches_the_browser_and_ends_the_terminal() -> None:
    world = _World()
    term, _, browser = await world.live()

    await world.relay.from_daemon(term, Frame.EXIT, (3).to_bytes(4))

    assert browser.sent[-1] == (Frame.EXIT, (3).to_bytes(4))
    assert browser.closed == [wire_terminals.CLOSE_ENDED]


@pytest.mark.asyncio
async def test_a_second_host_claim_is_refused() -> None:
    world = _World()
    term = world.open()

    assert await world.relay.daemon_connected(term, _Socket())
    assert not await world.relay.daemon_connected(term, _Socket())


@pytest.mark.asyncio
async def test_an_unconfirmed_startup_releases_its_claim_without_losing_input() -> None:
    world = _World()
    term = world.open()
    await world.relay.from_browser(term, Frame.INPUT, b"continue\n")
    first, second = _Socket(), _Socket()
    assert await world.relay.daemon_connected(term, first, ready=True)
    assert first.sent == [(Frame.ACK, b"")]

    await world.relay.daemon_lost(term, first)
    assert world.relay.terminal(term.id) is term
    assert await world.relay.daemon_connected(term, second, ready=True)
    await world.relay.from_daemon(term, Frame.ACK, b"")

    assert second.sent == [(Frame.ACK, b""), (Frame.INPUT, b"continue\n")]
    await world.relay.daemon_lost(term, second)
    assert term.ended


@pytest.mark.asyncio
async def test_a_failed_startup_acknowledgement_leaves_the_terminal_retryable() -> None:
    world = _World()
    term = world.open()

    assert not await world.relay.daemon_connected(term, _Socket(gone=True), ready=True)
    assert world.relay.terminal(term.id) is term
    assert await world.relay.daemon_connected(term, _Socket(), ready=True)


@pytest.mark.asyncio
async def test_a_retry_can_replace_a_claim_whose_acknowledgement_is_still_blocked() -> (
    None
):
    world = _World()
    term = world.open()
    first, second = _BlockedSocket(), _Socket()
    claiming = asyncio.create_task(
        world.relay.daemon_connected(term, first, ready=True),
    )
    try:
        await asyncio.wait_for(first.sending.wait(), 1)
        accepted = await world.relay.daemon_connected(term, second, ready=True)
        first.release.set()
        await claiming

        assert accepted
        assert term.daemon is second
        assert not term.ended
        assert first.closed == [wire_terminals.CLOSE_TAKEN_OVER]
        assert second.sent == [(Frame.ACK, b"")]
        await world.relay.daemon_lost(term, first)
        assert term.daemon is second
    finally:
        first.release.set()
        await claiming


@pytest.mark.asyncio
async def test_typing_during_startup_cannot_overtake_the_buffered_input() -> None:
    world = _World()
    term = world.open()
    await world.relay.from_browser(term, Frame.RESIZE, encode_winsize(24, 80))
    await world.relay.from_browser(term, Frame.INPUT, b"first\n")
    host = _BlockedSocket()
    connecting = asyncio.create_task(world.relay.daemon_connected(term, host))
    try:
        await asyncio.wait_for(host.sending.wait(), 1)
        typing = asyncio.create_task(
            world.relay.from_browser(term, Frame.INPUT, b"second\n"),
        )
        await asyncio.sleep(0)
        host.release.set()
        await asyncio.gather(connecting, typing)

        assert [payload for kind, payload in host.sent if kind is Frame.INPUT] == [
            b"first\n",
            b"second\n",
        ]
    finally:
        host.release.set()
        await connecting


@pytest.mark.asyncio
async def test_resize_is_clamped_and_a_bad_frame_is_refused() -> None:
    world = _World()
    term, host, _ = await world.live()

    assert await world.relay.from_browser(term, Frame.RESIZE, encode_winsize(0, 5_000))
    assert not await world.relay.from_browser(term, Frame.RESIZE, b"xx")
    assert not await world.relay.from_browser(term, Frame.OUTPUT, b"x")
    assert not await world.relay.from_browser(
        term,
        Frame.INPUT,
        bytes(wire_terminals.MAX_INPUT_FRAME_BYTES + 1),
    )

    assert host.sent == [
        (
            Frame.RESIZE,
            encode_winsize(1, wire_terminals.MAX_COLS),
        ),
    ]


@pytest.mark.asyncio
async def test_stop_asks_the_host_and_ends_at_once_without_one() -> None:
    world = _World()
    term, host, _ = await world.live()
    await world.relay.from_browser(term, Frame.STOP, b"")
    assert host.sent == [(Frame.STOP, b"")]
    assert not term.ended

    pending = world.open(host_id=uuid.uuid4())
    await world.relay.stop(pending)
    assert pending.ended

    waiting = world.open(host_id=uuid.uuid4())
    browser = _Socket()
    await world.relay.attach_browser(waiting, browser, offset=0)
    await world.relay.stop(waiting)
    assert browser.closed == [wire_terminals.CLOSE_ENDED]


@pytest.mark.asyncio
async def test_stop_ends_a_host_that_has_not_confirmed_startup() -> None:
    world = _World()
    term = world.open()
    host = _Socket()
    assert await world.relay.daemon_connected(term, host, ready=True)

    await world.relay.stop(term)

    assert term.ended
    assert host.closed == [wire_terminals.CLOSE_ENDED]
    assert not await world.relay.daemon_connected(term, _Socket(), ready=True)


@pytest.mark.asyncio
async def test_a_browser_that_vanished_detaches_and_a_vanished_host_ends() -> None:
    world = _World()
    term, host, browser = await world.live()
    browser.gone = True
    await world.relay.from_daemon(term, Frame.OUTPUT, b"x")
    assert term.browser is None
    assert term.detached_at == world.clock.now

    host.gone = True
    await world.relay.from_browser(term, Frame.INPUT, b"y")
    assert term.ended


@pytest.mark.asyncio
async def test_input_and_output_over_their_rate_wait_for_the_window() -> None:
    world = _World()
    term, _, _ = await world.live()

    await world.relay.from_browser(
        term,
        Frame.INPUT,
        bytes(wire_terminals.MAX_INPUT_FRAME_BYTES),
    )
    assert world.slept == []
    for _ in range(wire_terminals.MAX_INPUT_BYTES_PER_SEC // 65_536 + 1):
        await world.relay.from_browser(
            term,
            Frame.INPUT,
            bytes(wire_terminals.MAX_INPUT_FRAME_BYTES),
        )
    assert world.slept
    assert all(0 < wait <= 1 for wait in world.slept)

    world.slept.clear()
    for _ in range(wire_terminals.MAX_OUTPUT_BYTES_PER_SEC // 65_536 + 1):
        await world.relay.from_daemon(term, Frame.OUTPUT, bytes(65_536))
    assert world.slept


@pytest.mark.asyncio
async def test_a_wait_returns_an_open_at_once_and_marks_the_host_ready() -> None:
    world = _World()
    assert not world.relay.ready(_HOST)

    waiting = asyncio.create_task(world.relay.wait(_HOST, wait_sec=0.5))
    try:
        await asyncio.sleep(0)
        assert world.relay.ready(_HOST)
        term = world.open()
        assert await waiting is term
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)

    world.clock.now += wire_terminals.WAIT_LEASE_SEC
    assert world.relay.ready(_HOST)
    world.clock.now += 1
    assert not world.relay.ready(_HOST)


@pytest.mark.asyncio
async def test_a_wait_with_nothing_queued_returns_none_and_an_open_is_offered_once() -> (
    None
):
    world = _World()
    assert await world.relay.wait(_HOST, wait_sec=0) is None

    term = world.open()
    assert await world.relay.wait(_HOST, wait_sec=0) is term
    assert await world.relay.wait(_HOST, wait_sec=0) is None


@pytest.mark.asyncio
async def test_a_wait_with_no_open_times_out_at_its_deadline() -> None:
    world = _World()

    assert await world.relay.wait(_HOST, wait_sec=0.01) is None


@pytest.mark.asyncio
async def test_the_sweep_loop_sweeps_each_interval() -> None:
    world = _World()
    term = world.open()
    world.dead.add(term.id)

    task = asyncio.create_task(sweep_loop(world.relay, interval_sec=0))
    for _ in range(50):
        await asyncio.sleep(0)
        if term.ended:
            break
    task.cancel()

    assert term.ended


@pytest.mark.asyncio
async def test_one_failing_liveness_question_does_not_starve_the_rest_of_a_pass() -> (
    None
):
    world = _World()
    first = world.open(host_id=uuid.UUID(int=10))
    second = world.open(host_id=uuid.UUID(int=11))
    world.broken.add(first.id)
    world.dead.add(second.id)

    await world.relay.sweep()

    assert (first.ended, second.ended) == (False, True)


@pytest.mark.asyncio
async def test_a_socket_that_raises_on_close_does_not_stop_the_terminal_ending() -> (
    None
):
    world = _World()
    term, host, browser = await world.live()
    host.broken_close = True

    await world.relay.daemon_lost(term, host)

    assert browser.closed == [wire_terminals.CLOSE_DAEMON_LOST]
    assert world.relay.terminal(term.id) is None


@pytest.mark.asyncio
async def test_a_takeover_works_when_the_old_socket_raises_on_close() -> None:
    world = _World()
    term, _, first = await world.live()
    first.broken_close = True
    second = _Socket()

    await world.relay.attach_browser(term, second, offset=0)

    assert term.browser is second
    assert second.sent[0][0] is Frame.ACK


@pytest.mark.asyncio
async def test_close_host_and_sweep_finish_every_terminal_when_one_close_raises() -> (
    None
):
    world = _World()
    a, host_a, _ = await world.live()
    b, _, browser_b = await world.live()
    host_a.broken_close = True

    await world.relay.close_host(_HOST)

    assert (a.ended, b.ended) == (True, True)
    assert browser_b.closed == [wire_terminals.CLOSE_HOST_REVOKED]


@pytest.mark.asyncio
async def test_a_reattach_while_output_flows_neither_repeats_nor_reorders() -> None:
    world = _World()
    term, _, old = await world.live()
    await world.relay.from_daemon(term, Frame.OUTPUT, b"abc")
    fresh = _Socket()

    attaching = asyncio.create_task(world.relay.attach_browser(term, fresh, offset=0))
    await asyncio.sleep(0)
    await world.relay.from_daemon(term, Frame.OUTPUT, b"def")
    await attaching
    await world.relay.from_daemon(term, Frame.OUTPUT, b"ghi")

    kinds = [kind for kind, _ in fresh.sent]
    assert kinds[0] is Frame.ACK
    assert (fresh.sent[0][1]) == (0).to_bytes(8)
    streamed = b"".join(payload for kind, payload in fresh.sent if kind is Frame.OUTPUT)
    assert streamed == b"abcdefghi"
    assert old.closed == [wire_terminals.CLOSE_TAKEN_OVER]


@pytest.mark.asyncio
async def test_a_blocked_replay_applies_backpressure_to_host_output() -> None:
    world = _World()
    term = world.open()
    assert await world.relay.daemon_connected(term, _Socket())
    browser = _BlockedSocket()
    attaching = asyncio.create_task(world.relay.attach_browser(term, browser, offset=0))
    await browser.sending.wait()
    payload = b"x" * (wire_terminals.RING_BYTES + 1)
    output = asyncio.create_task(world.relay.from_daemon(term, Frame.OUTPUT, payload))
    try:
        await asyncio.sleep(0)
        assert not output.done()
        assert term.ring.end == 0
    finally:
        browser.release.set()
        await attaching
        await output
    assert browser.sent == [(Frame.ACK, (0).to_bytes(8)), (Frame.OUTPUT, payload)]


@pytest.mark.asyncio
async def test_takeover_unblocks_the_old_replay_without_repeating_pending_output() -> (
    None
):
    world = _World()
    term = world.open()
    assert await world.relay.daemon_connected(term, _Socket())
    blocked = _BlockedSocket()
    attaching = asyncio.create_task(world.relay.attach_browser(term, blocked, offset=0))
    await blocked.sending.wait()
    output = asyncio.create_task(
        world.relay.from_daemon(term, Frame.OUTPUT, b"pending"),
    )
    try:
        await asyncio.sleep(0)
        assert not output.done()
        fresh = _Socket()
        await world.relay.attach_browser(term, fresh, offset=0)
        await attaching
        await output
    finally:
        blocked.release.set()
        await attaching
        await output
    assert fresh.sent == [(Frame.ACK, (0).to_bytes(8)), (Frame.OUTPUT, b"pending")]


@pytest.mark.asyncio
async def test_output_held_behind_a_replay_is_dropped_when_the_terminal_ends() -> None:
    world = _World()
    term = world.open()
    host = _Socket()
    assert await world.relay.daemon_connected(term, host)
    blocked = _BlockedSocket()
    attaching = asyncio.create_task(world.relay.attach_browser(term, blocked, offset=0))
    await blocked.sending.wait()
    output = asyncio.create_task(world.relay.from_daemon(term, Frame.OUTPUT, b"late"))
    await asyncio.sleep(0)
    assert not output.done()

    await world.relay.end(term, code=wire_terminals.CLOSE_ENDED)
    await output
    await attaching
    # Ending twice closes nothing twice.
    await world.relay.end(term, code=wire_terminals.CLOSE_DAEMON_LOST)

    assert term.ring.end == 0
    assert host.closed == [wire_terminals.CLOSE_ENDED]
    assert blocked.closed == [wire_terminals.CLOSE_ENDED]


@pytest.mark.asyncio
async def test_a_host_send_that_fails_unexpectedly_spares_the_rest_of_a_sweep(
    caplog: pytest.LogCaptureFixture,
) -> None:
    world = _World()
    first = world.open(host_id=uuid.UUID(int=10))
    assert await world.relay.daemon_connected(first, _FailingSocket())
    second = world.open(host_id=uuid.UUID(int=11))
    world.dead.update({first.id, second.id})

    await world.relay.sweep()

    assert (first.ended, second.ended) == (False, True)
    assert f"terminal {first.id}: sweep failed" in caplog.text


@pytest.mark.asyncio
async def test_a_failed_sweep_pass_is_logged_and_the_loop_goes_on(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    world = _World()
    passes: list[int] = []

    async def sweep() -> None:
        passes.append(len(passes))
        if len(passes) == 1:
            raise RuntimeError("clock blip")
        if len(passes) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(world.relay, "sweep", sweep)

    with pytest.raises(asyncio.CancelledError):
        await sweep_loop(world.relay, interval_sec=0)

    assert passes == [0, 1, 2]
    assert "relay sweep failed; retrying next cycle" in caplog.text


@pytest.mark.asyncio
async def test_what_the_browser_sent_before_the_host_dialled_reaches_it_then() -> None:
    world = _World()
    term = world.open()
    await world.relay.attach_browser(term, _Socket(), offset=0)
    await world.relay.from_browser(term, Frame.RESIZE, encode_winsize(10, 20))
    await world.relay.from_browser(term, Frame.RESIZE, encode_winsize(30, 90))
    await world.relay.from_browser(term, Frame.INPUT, b"ls")
    await world.relay.from_browser(term, Frame.INPUT, b"\n")
    await world.relay.from_browser(
        term,
        Frame.INPUT,
        bytes(wire_terminals.MAX_INPUT_FRAME_BYTES),
    )
    host = _Socket()

    assert await world.relay.daemon_connected(term, host)

    assert host.sent == [
        (Frame.RESIZE, encode_winsize(30, 90)),
        (Frame.INPUT, b"ls\n"),
    ]


@pytest.mark.asyncio
async def test_output_that_arrives_after_the_end_is_dropped() -> None:
    world = _World()
    term, _, browser = await world.live()
    await world.relay.end(term, code=wire_terminals.CLOSE_ENDED)
    sent = list(browser.sent)

    await world.relay.from_daemon(term, Frame.OUTPUT, b"late")

    assert (browser.sent, term.ring.end) == (sent, 0)


def test_the_ring_keeps_the_tail_and_the_attach_payload_decodes() -> None:
    ring = Ring(capacity=3)
    ring.append(b"ab")
    ring.append(b"cde")

    assert (ring.start, ring.end) == (2, 5)
    assert ring.since(99) == (b"", 5, False)
    ticket = "t" * wire_terminals.TICKET_CHARS
    assert decode_attach(ticket.encode() + (7).to_bytes(8)) == (ticket, 7)
    with pytest.raises(ValueError, match="ATTACH"):
        decode_attach(b"short")
    assert encode_frame(Frame.STOP) == b"\x05\x00\x00\x00\x00"


@pytest.mark.asyncio
async def test_a_signal_passes_between_the_browser_and_the_host_unread() -> None:
    world = _World()
    term, host, browser = await world.live()
    del browser.sent[:]

    assert await world.relay.from_browser(term, Frame.SIGNAL, b"offer-bytes")
    await world.relay.from_daemon(term, Frame.SIGNAL, b"answer-bytes")

    assert host.sent == [(Frame.SIGNAL, b"offer-bytes")]
    assert browser.sent == [(Frame.SIGNAL, b"answer-bytes")]


@pytest.mark.asyncio
async def test_a_signal_with_no_host_or_no_browser_goes_nowhere() -> None:
    world = _World()
    term = world.open()
    assert await world.relay.from_browser(term, Frame.SIGNAL, b"x")
    host = _Socket()
    assert await world.relay.daemon_connected(term, host)

    await world.relay.from_daemon(term, Frame.SIGNAL, b"y")

    assert host.sent == []


@pytest.mark.asyncio
async def test_an_oversize_signal_or_the_seventh_in_a_minute_closes_the_browser() -> (
    None
):
    world = _World()
    term, host, _ = await world.live()

    assert not await world.relay.from_browser(
        term,
        Frame.SIGNAL,
        b"x" * (wire_terminals.MAX_SIGNAL_BYTES + 1),
    )
    for _ in range(wire_terminals.MAX_SIGNALS_PER_MIN):
        assert await world.relay.from_browser(term, Frame.SIGNAL, b"x")
    assert not await world.relay.from_browser(term, Frame.SIGNAL, b"x")
    world.clock.now += 61
    assert await world.relay.from_browser(term, Frame.SIGNAL, b"x")
    assert len(host.sent) == wire_terminals.MAX_SIGNALS_PER_MIN + 1


@pytest.mark.asyncio
async def test_a_route_passes_to_the_host_and_a_malformed_one_is_refused() -> None:
    world = _World()
    term, host, _ = await world.live()
    route = wire_terminals.encode_route(wire_terminals.ROUTE_DIRECT, 5)

    assert await world.relay.from_browser(term, Frame.ROUTE, route)
    assert not await world.relay.from_browser(term, Frame.ROUTE, route[:-1])

    assert host.sent == [(Frame.ROUTE, route)]


@pytest.mark.asyncio
async def test_a_keepalive_keeps_a_direct_terminal_from_idling_out() -> None:
    world = _World()
    term, host, _ = await world.live()
    world.clock.now += wire_terminals.IDLE_SEC + 10

    await world.relay.from_daemon(
        term,
        Frame.KEEPALIVE,
        wire_terminals.encode_keepalive(1_000, output_idle_ms=0),
    )
    await world.relay.sweep()

    assert (term.ended, host.closed) == (False, [])
    world.clock.now += wire_terminals.IDLE_SEC
    await world.relay.sweep()
    assert term.ended


@pytest.mark.asyncio
async def test_a_host_is_idle_from_the_relay_start_until_input_or_output() -> None:
    world = _World()
    term, _, _ = await world.live()
    other = uuid.uuid4()
    world.clock.now += 50
    assert world.relay.idle_sec(other) == world.clock.now - _START

    await world.relay.from_browser(term, Frame.INPUT, b"ls\n")
    world.clock.now += 7
    assert world.relay.idle_sec(term.host_id) == 7

    await world.relay.from_daemon(term, Frame.OUTPUT, b"a")
    assert world.relay.idle_sec(term.host_id) == 0
    # Neither a resize nor a signal is use.
    world.clock.now += 3
    assert await world.relay.from_browser(term, Frame.RESIZE, b"\x00\x10\x00\x20")
    assert world.relay.idle_sec(term.host_id) == 3


@pytest.mark.asyncio
async def test_a_direct_keepalive_dates_use_by_the_later_of_input_and_output() -> None:
    world = _World()
    term, _, _ = await world.live()
    world.clock.now += 100

    await world.relay.from_daemon(
        term,
        Frame.KEEPALIVE,
        wire_terminals.encode_keepalive(90_000, output_idle_ms=4_000),
    )
    assert world.relay.idle_sec(term.host_id) == 4

    # An older report than one already taken moves nothing back.
    await world.relay.from_daemon(
        term,
        Frame.KEEPALIVE,
        wire_terminals.encode_keepalive(90_000, output_idle_ms=60_000),
    )
    assert world.relay.idle_sec(term.host_id) == 4


@pytest.mark.asyncio
async def test_a_keepalive_from_an_older_host_dates_use_by_input_alone() -> None:
    world = _World()
    term, _, _ = await world.live()
    world.clock.now += 100

    await world.relay.from_daemon(term, Frame.KEEPALIVE, (30_000).to_bytes(4))

    assert world.relay.idle_sec(term.host_id) == 30
    assert term.last_input_at == world.clock.now - 30


@pytest.mark.asyncio
async def test_a_malformed_keepalive_changes_nothing() -> None:
    world = _World()
    term, _, _ = await world.live()
    before = term.last_input_at

    await world.relay.from_daemon(term, Frame.KEEPALIVE, b"\x00")

    assert (term.last_input_at, term.direct_until) == (before, 0.0)


@pytest.mark.asyncio
async def test_a_host_that_returns_to_the_relay_restarts_the_ring_where_it_resumes() -> (
    None
):
    world = _World()
    term, _, browser = await world.live()
    await world.relay.from_daemon(term, Frame.OUTPUT, b"abc")
    del browser.sent[:]

    await world.relay.from_daemon(term, Frame.ACK, (10).to_bytes(8))
    await world.relay.from_daemon(term, Frame.OUTPUT, b"xy")

    assert browser.sent == [(Frame.ACK, (10).to_bytes(8)), (Frame.OUTPUT, b"xy")]
    assert term.ring.since(10) == (b"xy", 10, False)


@pytest.mark.asyncio
async def test_a_browser_attaching_to_a_direct_terminal_asks_the_host_back_to_the_relay() -> (
    None
):
    world = _World()
    term, host, first = await world.live()
    await world.relay.from_daemon(term, Frame.OUTPUT, b"abc")
    await world.relay.from_daemon(term, Frame.KEEPALIVE, _KEEPALIVE)
    world.relay.detach_browser(term, first)
    del host.sent[:]

    await world.relay.attach_browser(term, _Socket(), offset=0)

    assert host.sent == [
        (Frame.ROUTE, wire_terminals.encode_route(wire_terminals.ROUTE_RELAY, 3)),
    ]
    assert term.direct_until == 0.0


@pytest.mark.asyncio
async def test_a_browser_attaching_to_a_relayed_terminal_resets_negotiation() -> None:
    world = _World()
    term, host, first = await world.live()
    await world.relay.from_daemon(term, Frame.KEEPALIVE, _KEEPALIVE)
    world.clock.now += 3 * wire_terminals.KEEPALIVE_SEC + 1
    world.relay.detach_browser(term, first)
    del host.sent[:]

    await world.relay.attach_browser(term, _Socket(), offset=0)

    assert host.sent == [
        (Frame.ROUTE, wire_terminals.encode_route(wire_terminals.ROUTE_RELAY, 0)),
    ]


@pytest.mark.asyncio
async def test_a_signal_of_exactly_the_cap_passes_and_the_window_is_exactly_a_minute() -> (
    None
):
    world = _World()
    term, host, _ = await world.live()

    assert await world.relay.from_browser(
        term,
        Frame.SIGNAL,
        b"x" * wire_terminals.MAX_SIGNAL_BYTES,
    )
    for _ in range(wire_terminals.MAX_SIGNALS_PER_MIN - 1):
        assert await world.relay.from_browser(term, Frame.SIGNAL, b"x")
    world.clock.now += 59.9
    assert not await world.relay.from_browser(term, Frame.SIGNAL, b"x")
    world.clock.now += 0.1
    assert await world.relay.from_browser(term, Frame.SIGNAL, b"x")
    assert len(host.sent) == wire_terminals.MAX_SIGNALS_PER_MIN + 1


@pytest.mark.asyncio
async def test_a_keepalive_dates_the_last_input_and_holds_the_direct_mark_for_three_periods() -> (
    None
):
    world = _World()
    term, host, first = await world.live()
    await world.relay.from_daemon(
        term,
        Frame.KEEPALIVE,
        wire_terminals.encode_keepalive(2_500, output_idle_ms=0),
    )
    world.relay.detach_browser(term, first)

    assert term.last_input_at == world.clock.now - 2.5
    world.clock.now += 3 * wire_terminals.KEEPALIVE_SEC - 1
    await world.relay.attach_browser(term, _Socket(), offset=0)
    assert [kind for kind, _ in host.sent] == [Frame.ROUTE]

    await world.relay.from_daemon(term, Frame.KEEPALIVE, _KEEPALIVE)
    del host.sent[:]
    world.clock.now += 3 * wire_terminals.KEEPALIVE_SEC
    await world.relay.attach_browser(term, _Socket(), offset=0)
    assert host.sent == [
        (Frame.ROUTE, wire_terminals.encode_route(wire_terminals.ROUTE_RELAY, 0)),
    ]


@pytest.mark.asyncio
async def test_only_an_ack_of_eight_bytes_restarts_the_ring() -> None:
    world = _World()
    term, _, _ = await world.live()
    await world.relay.from_daemon(term, Frame.OUTPUT, b"abc")

    await world.relay.from_daemon(term, Frame.INPUT, (99).to_bytes(8))
    await world.relay.from_daemon(term, Frame.ACK, b"\x01")

    assert term.ring.since(0) == (b"abc", 0, False)


@pytest.mark.asyncio
async def test_an_open_hands_the_wait_the_folder_its_shell_starts_in() -> None:
    world = _World()
    opened = world.relay.open(
        Opening(
            user="a@x.y",
            host_id=_HOST,
            host="gx10",
            rows=24,
            cols=80,
            cwd="~/project",
        ),
    )

    offered = await world.relay.wait(_HOST, wait_sec=0)

    assert offered is opened
    assert offered is not None
    assert offered.cwd == "~/project"
    assert world.open().cwd is None


@pytest.mark.asyncio
async def test_a_pending_open_lives_through_its_exact_startup_deadline() -> None:
    world = _World()
    term = world.open()
    browser = _Socket()
    await world.relay.attach_browser(term, browser, offset=0)

    world.clock.now += wire_terminals.OPEN_TTL_SEC
    await world.relay.sweep()
    assert world.relay.terminal(term.id) is term
    assert browser.closed == []

    world.clock.now += 1
    await world.relay.sweep()
    assert browser.closed == [wire_terminals.CLOSE_DAEMON_LOST]
    assert world.relay.terminal(term.id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seconds", "code"),
    [
        (wire_terminals.IDLE_SEC, wire_terminals.CLOSE_IDLE),
        (wire_terminals.LIFETIME_SEC, wire_terminals.CLOSE_LIFETIME),
    ],
)
async def test_idle_and_lifetime_allow_the_exact_boundary(
    seconds: int,
    code: int,
) -> None:
    world = _World()
    term, host, browser = await world.live()
    world.clock.now += seconds
    if code == wire_terminals.CLOSE_LIFETIME:
        assert await world.relay.from_browser(term, Frame.INPUT, b"active")

    await world.relay.sweep()
    assert world.relay.terminal(term.id) is term
    assert host.closed == browser.closed == []

    world.clock.now += 1
    await world.relay.sweep()
    assert host.closed == browser.closed == [code]
    assert world.relay.terminal(term.id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("now", [0.0, float(wire_terminals.LIFETIME_SEC)])
async def test_a_fresh_terminal_does_not_expire_at_a_different_clock_origin(
    now: float,
) -> None:
    world = _World()
    world.clock.now = now
    term, host, browser = await world.live()

    await world.relay.sweep()

    assert world.relay.terminal(term.id) is term
    assert host.closed == browser.closed == []


@pytest.mark.asyncio
async def test_wait_skips_an_open_that_ended_before_it_was_offered() -> None:
    world = _World()
    ended = world.open()
    await world.relay.end(ended, code=wire_terminals.CLOSE_ENDED)
    live = world.open()

    assert await world.relay.wait(_HOST, wait_sec=0) is live
    assert await world.relay.wait(_HOST, wait_sec=0) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix_bytes", [0, 3])
async def test_input_buffered_before_the_host_accepts_exactly_one_full_frame(
    prefix_bytes: int,
) -> None:
    world = _World()
    term = world.open()
    prefix = b"a" * prefix_bytes
    suffix = b"b" * (wire_terminals.MAX_INPUT_FRAME_BYTES - prefix_bytes)
    assert await world.relay.from_browser(term, Frame.INPUT, prefix)
    assert await world.relay.from_browser(term, Frame.INPUT, suffix)
    host = _Socket()

    assert await world.relay.daemon_connected(term, host)

    assert host.sent == [(Frame.INPUT, prefix + suffix)]


@pytest.mark.asyncio
async def test_browser_input_during_host_close_is_not_forwarded() -> None:
    world = _World()
    term = world.open()
    host, browser = _ClosingSocket(), _Socket()
    assert await world.relay.daemon_connected(term, host)
    await world.relay.attach_browser(term, browser, offset=0)
    host.sent.clear()
    ending = asyncio.create_task(world.relay.end(term, code=wire_terminals.CLOSE_ENDED))
    try:
        await asyncio.wait_for(host.closing.wait(), 1)
        assert await world.relay.from_browser(term, Frame.INPUT, b"late")
        assert host.sent == []
    finally:
        host.release.set()
        await ending

    assert host.closed == browser.closed == [wire_terminals.CLOSE_ENDED]


@pytest.mark.asyncio
async def test_failed_host_input_closes_the_browser_with_host_lost() -> None:
    world = _World()
    term, host, browser = await world.live()
    host.gone = True

    assert await world.relay.from_browser(term, Frame.INPUT, b"lost")

    assert browser.closed == [wire_terminals.CLOSE_DAEMON_LOST]
    assert world.relay.terminal(term.id) is None


@pytest.mark.asyncio
async def test_a_host_without_a_first_browser_still_expires_after_the_grace() -> None:
    world = _World()
    term = world.open()
    host = _Socket()
    assert await world.relay.daemon_connected(term, host)

    world.clock.now += wire_terminals.DETACH_GRACE_SEC + 1
    await world.relay.sweep()

    assert host.sent == [(Frame.STOP, b"")]
    assert host.closed == [wire_terminals.CLOSE_ENDED]
    assert world.relay.terminal(term.id) is None


@pytest.mark.asyncio
async def test_output_during_startup_does_not_confirm_the_host() -> None:
    world = _World()
    term = world.open()
    host, browser = _Socket(), _Socket()
    assert await world.relay.daemon_connected(term, host, ready=True)
    await world.relay.attach_browser(term, browser, offset=0)

    await world.relay.from_daemon(term, Frame.OUTPUT, b"early")
    assert browser.sent[-1] == (Frame.OUTPUT, b"early")
    assert await world.relay.from_browser(term, Frame.INPUT, b"buffered")
    assert host.sent == [(Frame.ACK, b"")]

    await world.relay.from_daemon(term, Frame.ACK, b"")
    assert host.sent == [(Frame.ACK, b""), (Frame.INPUT, b"buffered")]


@pytest.mark.asyncio
@pytest.mark.parametrize("cols", [0, 1])
async def test_resize_allows_a_single_column(cols: int) -> None:
    world = _World()
    term, host, _ = await world.live()

    assert await world.relay.from_browser(term, Frame.RESIZE, encode_winsize(3, cols))

    assert host.sent == [(Frame.RESIZE, encode_winsize(3, 1))]


@pytest.mark.asyncio
@pytest.mark.parametrize("now", [0.0, 1_000.0])
@pytest.mark.parametrize(
    ("kind", "limit"),
    [
        (Frame.INPUT, wire_terminals.MAX_INPUT_BYTES_PER_SEC),
        (Frame.OUTPUT, wire_terminals.MAX_OUTPUT_BYTES_PER_SEC),
    ],
)
async def test_byte_rates_allow_the_cap_and_reset_at_exactly_one_second(
    now: float,
    kind: Frame,
    limit: int,
) -> None:
    world = _World()
    world.clock.now = now
    term, _, _ = await world.live()
    send = world.relay.from_browser if kind is Frame.INPUT else world.relay.from_daemon
    chunk = bytes(wire_terminals.MAX_INPUT_FRAME_BYTES)
    for _ in range(limit // len(chunk)):
        await send(term, kind, chunk)
    assert world.slept == []
    await send(term, kind, b"x")
    assert world.slept == [1.0]

    world.slept.clear()
    world.clock.now += 1
    for _ in range(limit // len(chunk)):
        await send(term, kind, chunk)
    assert world.slept == []
    world.clock.now += 0.25
    await send(term, kind, b"x")
    assert world.slept == [0.75]


@pytest.mark.asyncio
async def test_an_open_keeps_its_context_in_the_canvas_listing_and_host_offer() -> None:
    world = _World()
    workspace = uuid.UUID(int=13)
    opened = world.relay.open(
        Opening(
            user="context@x.y",
            host_id=_HOST,
            host="worker-2",
            rows=37,
            cols=91,
            canvas=_CANVAS,
            workspace=workspace,
            requested_by="campaign-helper",
            surface="run-tile",
            cwd="~/context",
        ),
    )

    [listed] = world.relay.live_in("context@x.y", canvas=_CANVAS)
    assert (listed.id, listed.host, listed.surface) == (
        opened.id,
        "worker-2",
        "run-tile",
    )
    offered = await world.relay.wait(_HOST, wait_sec=0)
    assert offered is opened
    assert offered is not None
    assert (offered.user, offered.rows, offered.cols) == ("context@x.y", 37, 91)
    assert (offered.canvas, offered.workspace, offered.requested_by) == (
        _CANVAS,
        workspace,
        "campaign-helper",
    )
    assert offered.cwd == "~/context"


@pytest.mark.asyncio
async def test_an_open_reaches_only_its_host() -> None:
    world = _World()
    host = uuid.UUID(int=19)
    opened = world.relay.open(
        Opening(
            user="worker@x.y",
            host_id=host,
            host="worker",
            rows=3,
            cols=5,
        ),
    )

    assert await world.relay.wait(_HOST, wait_sec=0) is None
    offered = await world.relay.wait(host, wait_sec=0)
    assert offered is opened
    assert offered is not None
    assert offered.host == "worker"
    assert offered.cwd is None
    assert world.relay.live_in("worker@x.y", canvas=_CANVAS) == []
    assert await world.relay.wait(host, wait_sec=0) is None


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
