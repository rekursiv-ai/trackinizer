"""The terminal hub: relay, replay, takeover, the limits and the sweep, on a fake clock."""

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
    TerminalHub,
    TerminalLimitError,
    decode_attach,
    sweep_loop,
)
from trackinizer.wire import wire_terminals


_MACHINE = uuid.UUID(int=1)
_STATION = uuid.UUID(int=2)


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
        self.now = 1_000.0

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


class _World:
    def __init__(self) -> None:
        self.clock = _Clock()
        self.dead: set[uuid.UUID] = set()
        self.broken: set[uuid.UUID] = set()
        """Terminals whose liveness question raises."""
        self.slept: list[float] = []
        self.hub = TerminalHub(
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
        machine_id: uuid.UUID = _MACHINE,
        workspace: uuid.UUID | None = None,
        surface: str | None = None,
        attach: uuid.UUID | None = None,
    ) -> Terminal:
        return self.hub.open(
            Opening(
                user=user,
                machine_id=machine_id,
                machine="gx10",
                rows=24,
                cols=80,
                workspace=workspace,
                surface=surface,
                command=None if attach is None else "claude",
                attach=attach,
            ),
        )

    async def live(self) -> tuple[Terminal, _Socket, _Socket]:
        term = self.open()
        host, browser = _Socket(), _Socket()
        assert await self.hub.host_connected(term, host)
        await self.hub.attach_browser(term, browser, offset=0)
        return term, host, browser


@pytest.mark.asyncio
async def test_output_reaches_the_browser_and_ring_and_input_the_host() -> None:
    world = _World()
    term, host, browser = await world.live()

    await world.hub.from_host(term, Frame.OUTPUT, b"$ ")
    await world.hub.from_browser(term, Frame.INPUT, b"ls\n")

    assert browser.sent[-1] == (Frame.OUTPUT, b"$ ")
    assert host.sent == [(Frame.INPUT, b"ls\n")]
    assert term.ring.since(0)[0] == b"$ "


@pytest.mark.asyncio
async def test_a_browser_reattaching_resumes_from_its_offset() -> None:
    world = _World()
    term, _, first = await world.live()
    await world.hub.from_host(term, Frame.OUTPUT, b"abcdef")
    world.hub.detach_browser(term, first)

    second = _Socket()
    await world.hub.attach_browser(term, second, offset=2)

    assert second.sent == [
        (Frame.ACK, (2).to_bytes(8)),
        (Frame.OUTPUT, b"cdef"),
    ]


@pytest.mark.asyncio
async def test_an_offset_past_the_ring_gets_the_whole_ring_after_a_notice() -> None:
    world = _World()
    term, _, first = await world.live()
    term.ring = Ring(capacity=4)
    await world.hub.from_host(term, Frame.OUTPUT, b"abcdefgh")
    world.hub.detach_browser(term, first)

    second = _Socket()
    await world.hub.attach_browser(term, second, offset=1)

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
    assert await world.hub.host_connected(term, _Socket())
    await world.hub.from_host(term, Frame.OUTPUT, b"abcdef")
    browser = _Socket(limit=limit)

    await world.hub.attach_browser(term, browser, offset=0)

    assert (term.browser, term.detached_at is None) == (None, False)


@pytest.mark.asyncio
async def test_a_browser_that_leaves_while_output_queued_behind_the_replay_detaches() -> (
    None
):
    world = _World()
    term = world.open()
    assert await world.hub.host_connected(term, _Socket())
    await world.hub.from_host(term, Frame.OUTPUT, b"ab")
    browser = _Socket(limit=2)

    attaching = asyncio.create_task(world.hub.attach_browser(term, browser, offset=0))
    await asyncio.sleep(0)
    await world.hub.from_host(term, Frame.OUTPUT, b"cd")
    await attaching

    assert term.browser is None


@pytest.mark.asyncio
async def test_a_second_browser_takes_over_and_closes_the_first_with_4001() -> None:
    world = _World()
    term, _, first = await world.live()
    second = _Socket()

    await world.hub.attach_browser(term, second, offset=0)
    world.hub.detach_browser(term, first)

    assert first.closed == [wire_terminals.CLOSE_TAKEN_OVER]
    assert term.browser is second


@pytest.mark.asyncio
async def test_a_browser_that_left_is_stopped_after_the_grace_and_not_before() -> None:
    world = _World()
    term, host, browser = await world.live()
    world.hub.detach_browser(term, browser)

    world.clock.now += wire_terminals.DETACH_GRACE_SEC - 1
    await world.hub.sweep()
    assert not term.ended

    world.clock.now += 2
    await world.hub.sweep()
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

    await world.hub.sweep()

    assert browser.closed == [code]


@pytest.mark.asyncio
async def test_input_resets_the_idle_clock() -> None:
    world = _World()
    term, _, _ = await world.live()
    world.clock.now += wire_terminals.IDLE_SEC - 1
    await world.hub.from_browser(term, Frame.INPUT, b"x")
    world.clock.now += wire_terminals.IDLE_SEC - 1

    await world.hub.sweep()

    assert not term.ended


@pytest.mark.asyncio
async def test_no_count_of_live_terminals_refuses_an_open() -> None:
    world = _World()
    for _ in range(70):
        world.open(workspace=_STATION)
        world.clock.now += 7

    assert len(world.hub.live_in("a@x.y", workspace=_STATION)) == 70


@pytest.mark.asyncio
async def test_opens_per_minute_are_limited_and_the_window_slides() -> None:
    world = _World()
    for _ in range(wire_terminals.MAX_OPENS_PER_MIN_PER_USER):
        term = world.open(machine_id=uuid.uuid4())
        await world.hub.end(term, code=wire_terminals.CLOSE_ENDED)
    with pytest.raises(TerminalLimitError):
        world.open(machine_id=uuid.uuid4())

    world.clock.now += 61
    world.open(machine_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_a_station_lists_each_of_the_users_live_terminals_oldest_first() -> None:
    world = _World()
    first = world.open(workspace=_STATION, surface="chat")
    second = world.open(workspace=_STATION, surface="tile")
    world.open(workspace=uuid.UUID(int=3))
    world.open(user="b@x.y", workspace=_STATION)

    assert world.hub.live_in("a@x.y", workspace=_STATION) == [first, second]
    assert [t.surface for t in world.hub.live_in("a@x.y", workspace=_STATION)] == [
        "chat",
        "tile",
    ]
    await world.hub.end(first, code=wire_terminals.CLOSE_ENDED)
    assert world.hub.live_in("a@x.y", workspace=_STATION) == [second]


@pytest.mark.asyncio
async def test_a_terminal_told_to_stop_is_no_longer_listed_for_its_station() -> None:
    world = _World()
    closing, host, _ = await world.live()
    closing.workspace = _STATION
    other = world.open(workspace=_STATION)

    await world.hub.stop(closing)

    assert host.sent == [(Frame.STOP, b"")]
    assert not closing.ended
    assert world.hub.live_in("a@x.y", workspace=_STATION) == [other]


@pytest.mark.asyncio
async def test_the_runs_attached_to_a_session_are_those_still_live_for_it() -> None:
    world = _World()
    chat, other = uuid.UUID(int=7), uuid.UUID(int=8)
    run = world.open(attach=chat)
    stopping = world.open(attach=chat)
    ended = world.open(attach=chat)
    world.open(attach=other)
    world.open()

    await world.hub.stop(stopping)
    await world.hub.end(ended, code=wire_terminals.CLOSE_ENDED)

    assert world.hub.attached_to(chat) == [run]
    assert [t.attach for t in world.hub.attached_to(other)] == [other]
    assert world.hub.attached_to(uuid.UUID(int=9)) == []


@pytest.mark.asyncio
async def test_an_untaken_open_fails_after_twenty_seconds() -> None:
    world = _World()
    term = world.open()
    browser = _Socket()
    await world.hub.attach_browser(term, browser, offset=0)

    world.clock.now += wire_terminals.OPEN_TTL_SEC + 1
    await world.hub.sweep()

    assert term.ended
    assert browser.closed == [wire_terminals.CLOSE_HOST_LOST]
    assert world.hub.terminal(term.id) is None


@pytest.mark.asyncio
async def test_a_ticket_is_one_use_short_lived_and_bound() -> None:
    world = _World()
    term = world.open()

    # Each check has a ticket of its own: spending one must not make a later one pass
    # for the wrong reason.
    wrong_user = world.hub.mint_ticket(term)
    wrong_terminal = world.hub.mint_ticket(term)
    expiring = world.hub.mint_ticket(term)
    world.clock.now += wire_terminals.TICKET_TTL_SEC + 1
    fresh = world.hub.mint_ticket(term)

    assert not world.hub.redeem(wrong_user, user="b@x.y", terminal_id=term.id)
    assert not world.hub.redeem(wrong_terminal, user="a@x.y", terminal_id=uuid.uuid4())
    assert not world.hub.redeem(expiring, user="a@x.y", terminal_id=term.id)
    assert world.hub.redeem(fresh, user="a@x.y", terminal_id=term.id)
    assert not world.hub.redeem(fresh, user="a@x.y", terminal_id=term.id)
    assert len(fresh) == wire_terminals.TICKET_CHARS


@pytest.mark.asyncio
async def test_a_ticket_is_spent_by_the_second_it_expires() -> None:
    world = _World()
    term = world.open()
    ticket = world.hub.mint_ticket(term)
    world.clock.now += wire_terminals.TICKET_TTL_SEC

    assert not world.hub.redeem(ticket, user="a@x.y", terminal_id=term.id)


@pytest.mark.asyncio
async def test_close_machine_sends_4410_to_both_legs() -> None:
    world = _World()
    term, host, browser = await world.live()

    await world.hub.close_machine(_MACHINE)

    assert host.closed == browser.closed == [wire_terminals.CLOSE_MACHINE_REVOKED]
    assert term.ended


@pytest.mark.asyncio
async def test_the_sweep_ends_a_terminal_whose_owner_or_credential_died() -> None:
    world = _World()
    term, _, browser = await world.live()
    world.dead.add(term.id)

    await world.hub.sweep()

    assert browser.closed == [wire_terminals.CLOSE_MACHINE_REVOKED]


@pytest.mark.asyncio
async def test_a_lost_host_ends_the_terminal_with_4004() -> None:
    world = _World()
    term, host, browser = await world.live()

    await world.hub.host_lost(term, host)

    assert browser.closed == [wire_terminals.CLOSE_HOST_LOST]


@pytest.mark.asyncio
async def test_the_shells_exit_reaches_the_browser_and_ends_the_terminal() -> None:
    world = _World()
    term, _, browser = await world.live()

    await world.hub.from_host(term, Frame.EXIT, (3).to_bytes(4))

    assert browser.sent[-1] == (Frame.EXIT, (3).to_bytes(4))
    assert browser.closed == [wire_terminals.CLOSE_ENDED]


@pytest.mark.asyncio
async def test_a_second_host_claim_is_refused() -> None:
    world = _World()
    term = world.open()

    assert await world.hub.host_connected(term, _Socket())
    assert not await world.hub.host_connected(term, _Socket())


@pytest.mark.asyncio
async def test_resize_is_clamped_and_a_bad_frame_is_refused() -> None:
    world = _World()
    term, host, _ = await world.live()

    assert await world.hub.from_browser(term, Frame.RESIZE, encode_winsize(0, 5_000))
    assert not await world.hub.from_browser(term, Frame.RESIZE, b"xx")
    assert not await world.hub.from_browser(term, Frame.OUTPUT, b"x")
    assert not await world.hub.from_browser(
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
    await world.hub.from_browser(term, Frame.STOP, b"")
    assert host.sent == [(Frame.STOP, b"")]
    assert not term.ended

    pending = world.open(machine_id=uuid.uuid4())
    await world.hub.stop(pending)
    assert pending.ended

    waiting = world.open(machine_id=uuid.uuid4())
    browser = _Socket()
    await world.hub.attach_browser(waiting, browser, offset=0)
    await world.hub.stop(waiting)
    assert browser.closed == [wire_terminals.CLOSE_ENDED]


@pytest.mark.asyncio
async def test_a_browser_that_vanished_detaches_and_a_vanished_host_ends() -> None:
    world = _World()
    term, host, browser = await world.live()
    browser.gone = True
    await world.hub.from_host(term, Frame.OUTPUT, b"x")
    assert term.browser is None
    assert term.detached_at == world.clock.now

    host.gone = True
    await world.hub.from_browser(term, Frame.INPUT, b"y")
    assert term.ended


@pytest.mark.asyncio
async def test_input_and_output_over_their_rate_wait_for_the_window() -> None:
    world = _World()
    term, _, _ = await world.live()

    await world.hub.from_browser(
        term,
        Frame.INPUT,
        bytes(wire_terminals.MAX_INPUT_FRAME_BYTES),
    )
    assert world.slept == []
    for _ in range(wire_terminals.MAX_INPUT_BYTES_PER_SEC // 65_536 + 1):
        await world.hub.from_browser(
            term,
            Frame.INPUT,
            bytes(wire_terminals.MAX_INPUT_FRAME_BYTES),
        )
    assert world.slept
    assert all(0 < wait <= 1 for wait in world.slept)

    world.slept.clear()
    for _ in range(wire_terminals.MAX_OUTPUT_BYTES_PER_SEC // 65_536 + 1):
        await world.hub.from_host(term, Frame.OUTPUT, bytes(65_536))
    assert world.slept


@pytest.mark.asyncio
async def test_a_wait_returns_an_open_at_once_and_marks_the_machine_ready() -> None:
    world = _World()
    assert not world.hub.ready(_MACHINE)

    waiting = asyncio.create_task(world.hub.wait(_MACHINE, wait_sec=5))
    await asyncio.sleep(0)
    term = world.open()

    assert await waiting is term
    assert world.hub.ready(_MACHINE)
    world.clock.now += wire_terminals.WAIT_LEASE_SEC + 1
    assert not world.hub.ready(_MACHINE)


@pytest.mark.asyncio
async def test_a_wait_with_nothing_queued_returns_none_and_an_open_is_offered_once() -> (
    None
):
    world = _World()
    assert await world.hub.wait(_MACHINE, wait_sec=0) is None

    term = world.open()
    assert await world.hub.wait(_MACHINE, wait_sec=0) is term
    assert await world.hub.wait(_MACHINE, wait_sec=0) is None


@pytest.mark.asyncio
async def test_a_wait_with_no_open_times_out_at_its_deadline() -> None:
    world = _World()

    assert await world.hub.wait(_MACHINE, wait_sec=0.01) is None


@pytest.mark.asyncio
async def test_the_sweep_loop_sweeps_each_interval() -> None:
    world = _World()
    term = world.open()
    world.dead.add(term.id)

    task = asyncio.create_task(sweep_loop(world.hub, interval_sec=0))
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
    first = world.open(machine_id=uuid.UUID(int=10))
    second = world.open(machine_id=uuid.UUID(int=11))
    world.broken.add(first.id)
    world.dead.add(second.id)

    await world.hub.sweep()

    assert (first.ended, second.ended) == (False, True)


@pytest.mark.asyncio
async def test_a_leg_that_raises_on_close_does_not_stop_the_terminal_ending() -> None:
    world = _World()
    term, host, browser = await world.live()
    host.broken_close = True

    await world.hub.host_lost(term, host)

    assert browser.closed == [wire_terminals.CLOSE_HOST_LOST]
    assert world.hub.terminal(term.id) is None


@pytest.mark.asyncio
async def test_a_takeover_works_when_the_old_socket_raises_on_close() -> None:
    world = _World()
    term, _, first = await world.live()
    first.broken_close = True
    second = _Socket()

    await world.hub.attach_browser(term, second, offset=0)

    assert term.browser is second
    assert second.sent[0][0] is Frame.ACK


@pytest.mark.asyncio
async def test_close_machine_and_sweep_finish_every_terminal_when_one_close_raises() -> (
    None
):
    world = _World()
    a, host_a, _ = await world.live()
    b, _, browser_b = await world.live()
    host_a.broken_close = True

    await world.hub.close_machine(_MACHINE)

    assert (a.ended, b.ended) == (True, True)
    assert browser_b.closed == [wire_terminals.CLOSE_MACHINE_REVOKED]


@pytest.mark.asyncio
async def test_a_reattach_while_output_flows_neither_repeats_nor_reorders() -> None:
    world = _World()
    term, _, old = await world.live()
    await world.hub.from_host(term, Frame.OUTPUT, b"abc")
    fresh = _Socket()

    attaching = asyncio.create_task(world.hub.attach_browser(term, fresh, offset=0))
    await asyncio.sleep(0)
    await world.hub.from_host(term, Frame.OUTPUT, b"def")
    await attaching
    await world.hub.from_host(term, Frame.OUTPUT, b"ghi")

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
    assert await world.hub.host_connected(term, _Socket())
    browser = _BlockedSocket()
    attaching = asyncio.create_task(world.hub.attach_browser(term, browser, offset=0))
    await browser.sending.wait()
    payload = b"x" * (wire_terminals.RING_BYTES + 1)
    output = asyncio.create_task(world.hub.from_host(term, Frame.OUTPUT, payload))
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
    assert await world.hub.host_connected(term, _Socket())
    blocked = _BlockedSocket()
    attaching = asyncio.create_task(world.hub.attach_browser(term, blocked, offset=0))
    await blocked.sending.wait()
    output = asyncio.create_task(world.hub.from_host(term, Frame.OUTPUT, b"pending"))
    try:
        await asyncio.sleep(0)
        assert not output.done()
        fresh = _Socket()
        await world.hub.attach_browser(term, fresh, offset=0)
        await attaching
        await output
    finally:
        blocked.release.set()
        await attaching
        await output
    assert fresh.sent == [(Frame.ACK, (0).to_bytes(8)), (Frame.OUTPUT, b"pending")]


@pytest.mark.asyncio
async def test_what_the_browser_sent_before_the_host_dialled_reaches_it_then() -> None:
    world = _World()
    term = world.open()
    await world.hub.attach_browser(term, _Socket(), offset=0)
    await world.hub.from_browser(term, Frame.RESIZE, encode_winsize(10, 20))
    await world.hub.from_browser(term, Frame.RESIZE, encode_winsize(30, 90))
    await world.hub.from_browser(term, Frame.INPUT, b"ls")
    await world.hub.from_browser(term, Frame.INPUT, b"\n")
    await world.hub.from_browser(
        term,
        Frame.INPUT,
        bytes(wire_terminals.MAX_INPUT_FRAME_BYTES),
    )
    host = _Socket()

    assert await world.hub.host_connected(term, host)

    assert host.sent == [
        (Frame.RESIZE, encode_winsize(30, 90)),
        (Frame.INPUT, b"ls\n"),
    ]


@pytest.mark.asyncio
async def test_output_that_arrives_after_the_end_is_dropped() -> None:
    world = _World()
    term, _, browser = await world.live()
    await world.hub.end(term, code=wire_terminals.CLOSE_ENDED)
    sent = list(browser.sent)

    await world.hub.from_host(term, Frame.OUTPUT, b"late")

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

    assert await world.hub.from_browser(term, Frame.SIGNAL, b"offer-bytes")
    await world.hub.from_host(term, Frame.SIGNAL, b"answer-bytes")

    assert host.sent == [(Frame.SIGNAL, b"offer-bytes")]
    assert browser.sent == [(Frame.SIGNAL, b"answer-bytes")]


@pytest.mark.asyncio
async def test_a_signal_with_no_host_or_no_browser_goes_nowhere() -> None:
    world = _World()
    term = world.open()
    assert await world.hub.from_browser(term, Frame.SIGNAL, b"x")
    host = _Socket()
    assert await world.hub.host_connected(term, host)

    await world.hub.from_host(term, Frame.SIGNAL, b"y")

    assert host.sent == []


@pytest.mark.asyncio
async def test_an_oversize_signal_or_the_seventh_in_a_minute_closes_the_browser() -> (
    None
):
    world = _World()
    term, host, _ = await world.live()

    assert not await world.hub.from_browser(
        term,
        Frame.SIGNAL,
        b"x" * (wire_terminals.MAX_SIGNAL_BYTES + 1),
    )
    for _ in range(wire_terminals.MAX_SIGNALS_PER_MIN):
        assert await world.hub.from_browser(term, Frame.SIGNAL, b"x")
    assert not await world.hub.from_browser(term, Frame.SIGNAL, b"x")
    world.clock.now += 61
    assert await world.hub.from_browser(term, Frame.SIGNAL, b"x")
    assert len(host.sent) == wire_terminals.MAX_SIGNALS_PER_MIN + 1


@pytest.mark.asyncio
async def test_a_route_passes_to_the_host_and_a_malformed_one_is_refused() -> None:
    world = _World()
    term, host, _ = await world.live()
    route = wire_terminals.encode_route(wire_terminals.ROUTE_DIRECT, 5)

    assert await world.hub.from_browser(term, Frame.ROUTE, route)
    assert not await world.hub.from_browser(term, Frame.ROUTE, route[:-1])

    assert host.sent == [(Frame.ROUTE, route)]


@pytest.mark.asyncio
async def test_a_keepalive_keeps_a_direct_terminal_from_idling_out() -> None:
    world = _World()
    term, host, _ = await world.live()
    world.clock.now += wire_terminals.IDLE_SEC + 10

    await world.hub.from_host(
        term,
        Frame.KEEPALIVE,
        wire_terminals.encode_keepalive(1_000),
    )
    await world.hub.sweep()

    assert (term.ended, host.closed) == (False, [])
    world.clock.now += wire_terminals.IDLE_SEC
    await world.hub.sweep()
    assert term.ended


@pytest.mark.asyncio
async def test_a_malformed_keepalive_changes_nothing() -> None:
    world = _World()
    term, _, _ = await world.live()
    before = term.last_input_at

    await world.hub.from_host(term, Frame.KEEPALIVE, b"\x00")

    assert (term.last_input_at, term.direct_until) == (before, 0.0)


@pytest.mark.asyncio
async def test_a_host_that_returns_to_the_relay_restarts_the_ring_where_it_resumes() -> (
    None
):
    world = _World()
    term, _, browser = await world.live()
    await world.hub.from_host(term, Frame.OUTPUT, b"abc")
    del browser.sent[:]

    await world.hub.from_host(term, Frame.ACK, (10).to_bytes(8))
    await world.hub.from_host(term, Frame.OUTPUT, b"xy")

    assert browser.sent == [(Frame.ACK, (10).to_bytes(8)), (Frame.OUTPUT, b"xy")]
    assert term.ring.since(10) == (b"xy", 10, False)


@pytest.mark.asyncio
async def test_a_browser_attaching_to_a_direct_terminal_asks_the_host_back_to_the_relay() -> (
    None
):
    world = _World()
    term, host, first = await world.live()
    await world.hub.from_host(term, Frame.OUTPUT, b"abc")
    await world.hub.from_host(term, Frame.KEEPALIVE, wire_terminals.encode_keepalive(0))
    world.hub.detach_browser(term, first)
    del host.sent[:]

    await world.hub.attach_browser(term, _Socket(), offset=0)

    assert host.sent == [
        (Frame.ROUTE, wire_terminals.encode_route(wire_terminals.ROUTE_RELAY, 3)),
    ]
    assert term.direct_until == 0.0


@pytest.mark.asyncio
async def test_a_browser_attaching_to_a_relayed_terminal_asks_the_host_nothing() -> (
    None
):
    world = _World()
    term, host, first = await world.live()
    await world.hub.from_host(term, Frame.KEEPALIVE, wire_terminals.encode_keepalive(0))
    world.clock.now += 3 * wire_terminals.KEEPALIVE_SEC + 1
    world.hub.detach_browser(term, first)
    del host.sent[:]

    await world.hub.attach_browser(term, _Socket(), offset=0)

    assert host.sent == []


@pytest.mark.asyncio
async def test_a_signal_of_exactly_the_cap_passes_and_the_window_is_exactly_a_minute() -> (
    None
):
    world = _World()
    term, host, _ = await world.live()

    assert await world.hub.from_browser(
        term,
        Frame.SIGNAL,
        b"x" * wire_terminals.MAX_SIGNAL_BYTES,
    )
    for _ in range(wire_terminals.MAX_SIGNALS_PER_MIN - 1):
        assert await world.hub.from_browser(term, Frame.SIGNAL, b"x")
    world.clock.now += 59.9
    assert not await world.hub.from_browser(term, Frame.SIGNAL, b"x")
    world.clock.now += 0.1
    assert await world.hub.from_browser(term, Frame.SIGNAL, b"x")
    assert len(host.sent) == wire_terminals.MAX_SIGNALS_PER_MIN + 1


@pytest.mark.asyncio
async def test_a_keepalive_dates_the_last_input_and_holds_the_direct_mark_for_three_periods() -> (
    None
):
    world = _World()
    term, host, first = await world.live()
    await world.hub.from_host(
        term,
        Frame.KEEPALIVE,
        wire_terminals.encode_keepalive(2_500),
    )
    world.hub.detach_browser(term, first)

    assert term.last_input_at == world.clock.now - 2.5
    world.clock.now += 3 * wire_terminals.KEEPALIVE_SEC - 1
    await world.hub.attach_browser(term, _Socket(), offset=0)
    assert [kind for kind, _ in host.sent] == [Frame.ROUTE]

    await world.hub.from_host(term, Frame.KEEPALIVE, wire_terminals.encode_keepalive(0))
    del host.sent[:]
    world.clock.now += 3 * wire_terminals.KEEPALIVE_SEC
    await world.hub.attach_browser(term, _Socket(), offset=0)
    assert host.sent == []


@pytest.mark.asyncio
async def test_only_an_ack_of_eight_bytes_restarts_the_ring() -> None:
    world = _World()
    term, _, _ = await world.live()
    await world.hub.from_host(term, Frame.OUTPUT, b"abc")

    await world.hub.from_host(term, Frame.INPUT, (99).to_bytes(8))
    await world.hub.from_host(term, Frame.ACK, b"\x01")

    assert term.ring.since(0) == (b"abc", 0, False)


@pytest.mark.asyncio
async def test_a_run_open_hands_the_wait_its_cli_its_session_and_its_name() -> None:
    world = _World()
    session_id = uuid.UUID("9f1c2e3a-0000-4000-8000-000000000001")
    opened = world.hub.open(
        Opening(
            user="a@x.y",
            machine_id=_MACHINE,
            machine="gx10",
            rows=24,
            cols=80,
            command="claude",
            attach=session_id,
            run_name="chat-9f1c2e3a",
        ),
    )

    offered = await world.hub.wait(_MACHINE, wait_sec=0)

    assert offered is opened
    assert offered is not None
    assert (offered.command, offered.attach, offered.run_name) == (
        "claude",
        session_id,
        "chat-9f1c2e3a",
    )


@pytest.mark.asyncio
async def test_a_run_open_without_a_name_is_named_by_its_session_or_its_terminal() -> (
    None
):
    world = _World()
    session_id = uuid.UUID("9f1c2e3a-0000-4000-8000-000000000001")
    chat = world.hub.open(
        Opening(
            user="a@x.y",
            machine_id=_MACHINE,
            machine="gx10",
            rows=24,
            cols=80,
            command="codex",
            attach=session_id,
        ),
    )
    run = world.hub.open(
        Opening(
            user="a@x.y",
            machine_id=_MACHINE,
            machine="gx10",
            rows=24,
            cols=80,
            command="claude",
        ),
    )

    assert chat.run_name == "chat-9f1c2e3a"
    assert run.run_name == f"run-{run.id.hex[:8]}"


@pytest.mark.asyncio
async def test_a_login_shell_open_names_no_run() -> None:
    term = _World().open()

    assert (term.command, term.attach, term.run_name) == (None, None, None)


@pytest.mark.asyncio
async def test_a_run_open_hands_the_wait_the_folder_it_starts_in_as_named() -> None:
    world = _World()
    opened = world.hub.open(
        Opening(
            user="a@x.y",
            machine_id=_MACHINE,
            machine="gx10",
            rows=24,
            cols=80,
            command="claude",
            cwd="~/project",
        ),
    )

    offered = await world.hub.wait(_MACHINE, wait_sec=0)

    assert offered is opened
    assert offered is not None
    assert offered.cwd == "~/project"
    assert world.open().cwd is None


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
