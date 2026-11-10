"""The pipe hub: offers, waits, the relay between two legs and its ends, on a fake clock."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import asyncio
import logging
import re
import uuid

import pytest
import pytest_asyncio

from trackinizer.server import tool_pipes
from trackinizer.server.terminals import SocketClosedError, sweep_loop
from trackinizer.server.tool_pipes import (
    Pipe,
    PipeBusyError,
    PipeEndError,
    PipeHub,
    PipeRevokedError,
)
from trackinizer.wire import wire_terminals, wire_tools
from trackinizer.wire.wire_tools import ToolOffer


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Coroutine

    from trackinizer.server.tool_pipes import Inbound


pytestmark = pytest.mark.asyncio

_MACHINE = uuid.UUID(int=1)
_OTHER = uuid.UUID(int=2)
_ASKER = "ada@x.y"
_KEY = uuid.UUID(int=3)
_INSTANCE = uuid.UUID(int=4)
_LINE_BYTES = 256
"""The longest host line a test hub reads for the call it answers."""


async def _pass_through(pipe: Pipe, frame: bytes) -> tuple[bytes, bytes]:
    """Let every frame through to the host, and answer the agent nothing."""
    del pipe
    return frame, b""


class _Time:
    """A clock the test moves, and a sleep that ends only once it has moved far enough."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self._timers: list[tuple[float, asyncio.Event]] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        event = asyncio.Event()
        self._timers.append((self.now + seconds, event))
        await event.wait()

    async def advance(self, seconds: float) -> None:
        self.now += seconds
        for due, event in self._timers:
            if due <= self.now:
                event.set()
        self._timers = [each for each in self._timers if not each[1].is_set()]
        await _settle()


async def _settle() -> None:
    """Let every task that can run, run."""
    for _ in range(30):
        await asyncio.sleep(0)


class _Leg:
    """One end of a pipe: what the test pushes is read, and what is sent is kept."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.closed: list[int] = []
        self.reasons: list[str] = []
        self.reads = 0
        self.gone = False
        self.open = asyncio.Event()
        self.open.set()
        self._frames: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def send_bytes(self, data: bytes) -> None:
        await self.open.wait()
        if self.gone:
            raise SocketClosedError
        self.sent.append(data)

    async def close(self, code: int, reason: str = "") -> None:
        self.closed.append(code)
        self.reasons.append(reason)

    async def read(self) -> bytes | None:
        self.reads += 1
        return await self._frames.get()

    def push(self, *frames: bytes) -> None:
        for frame in frames:
            self._frames.put_nowait(frame)

    def drop(self) -> None:
        """Go as a dropped peer does: its next read says so, and sends now fail."""
        self.gone = True
        self._frames.put_nowait(None)


class _World:
    """A hub on a fake clock whose machines hold 16 pipes, each dialed back in 20 s."""

    def __init__(
        self,
        *,
        pipes_per_machine: int = 16,
        dial_back_sec: float = 20,
    ) -> None:
        self.time = _Time()
        self.hub = PipeHub(
            alive=self._alive,
            pipes_per_machine=pipes_per_machine,
            dial_back_sec=dial_back_sec,
            line_bytes=_LINE_BYTES,
            clock=self.time,
            sleep=self.time.sleep,
        )
        self.tasks: list[asyncio.Task[object]] = []
        self.dead: set[uuid.UUID] = set()
        self.unknown: set[uuid.UUID] = set()
        self.asked: list[tuple[str, uuid.UUID, uuid.UUID | None]] = []

    async def _alive(self, pipe: Pipe) -> bool:
        self.asked.append((pipe.asker, pipe.api_key_id, pipe.instance))
        if pipe.id in self.unknown:
            raise OSError("the database is down")
        return pipe.id not in self.dead

    def join(self, machine_id: uuid.UUID = _MACHINE) -> Pipe:
        """Bring an agent that nothing will serve, to fill a slot or an offer."""
        return self.hub.join(
            machine_id,
            agent=_Leg(),
            asker=_ASKER,
            api_key_id=_KEY,
            inbound=_pass_through,
        )

    def run(self, work: Coroutine[object, object, object]) -> asyncio.Task[object]:
        task = asyncio.ensure_future(work)
        self.tasks.append(task)
        return task

    async def agent(
        self,
        *,
        machine_id: uuid.UUID = _MACHINE,
        inbound: Inbound = _pass_through,
    ) -> tuple[Pipe, _Leg]:
        """Bring an agent to the machine, and let it queue."""
        leg = _Leg()
        pipe = self.hub.join(
            machine_id,
            agent=leg,
            asker=_ASKER,
            api_key_id=_KEY,
            inbound=inbound,
        )
        _ = self.run(self.hub.serve_agent(pipe, leg.read))
        await _settle()
        return pipe, leg

    async def host(self, pipe: Pipe) -> _Leg:
        """Dial back for an offered pipe, and let it start."""
        leg = _Leg()
        attached = self.hub.attach(pipe.machine_id, pipe.id, leg, instance=_INSTANCE)
        assert attached is pipe
        _ = self.run(self.hub.serve_host(pipe, leg.read))
        await _settle()
        return leg

    async def paired(
        self,
        *,
        inbound: Inbound = _pass_through,
    ) -> tuple[_Leg, _Leg]:
        """Bring up a whole pipe, and return its agent's leg and its host's."""
        pipe, agent = await self.agent(inbound=inbound)
        assert await self.take() == ToolOffer(pipe_id=pipe.id)
        return agent, await self.host(pipe)

    async def take(self, machine_id: uuid.UUID = _MACHINE) -> ToolOffer | None:
        return await self.hub.wait(machine_id, wait_sec=0)


@pytest_asyncio.fixture
async def world() -> AsyncIterator[_World]:
    found = _World()
    yield found
    for task in found.tasks:
        _ = task.cancel()
    _ = await asyncio.gather(*found.tasks, return_exceptions=True)


# -- Who is offered what ----------------------------------------------------------------


async def test_a_held_wait_returns_the_moment_an_agent_arrives(world: _World) -> None:
    waiting = world.run(world.hub.wait(_MACHINE, wait_sec=25))
    await _settle()
    assert not waiting.done()

    pipe, _leg = await world.agent()

    assert await waiting == ToolOffer(pipe_id=pipe.id)


async def test_a_wait_with_no_agent_ends_empty_when_its_time_is_up(
    world: _World,
) -> None:
    waiting = world.run(world.hub.wait(_MACHINE, wait_sec=25))
    await _settle()

    await world.time.advance(24)
    assert not waiting.done()
    await world.time.advance(1)

    assert await waiting is None


async def test_a_wait_of_no_time_answers_at_once(world: _World) -> None:
    assert await world.take() is None


async def test_a_wait_that_lost_its_offer_to_another_wait_keeps_waiting(
    world: _World,
) -> None:
    first = world.run(world.hub.wait(_MACHINE, wait_sec=25))
    second = world.run(world.hub.wait(_MACHINE, wait_sec=25))
    await _settle()

    pipe, _leg = await world.agent()

    assert await first == ToolOffer(pipe_id=pipe.id)
    assert not second.done()
    await world.time.advance(25)
    assert await second is None


async def test_when_the_woken_wait_goes_before_it_runs_another_wait_gets_the_offer(
    world: _World,
) -> None:
    gone = world.run(world.hub.wait(_MACHINE, wait_sec=25))
    kept = world.run(world.hub.wait(_MACHINE, wait_sec=25))
    await _settle()

    leg = _Leg()
    pipe = world.hub.join(
        _MACHINE,
        agent=leg,
        asker=_ASKER,
        api_key_id=_KEY,
        inbound=_pass_through,
    )
    _ = world.run(world.hub.serve_agent(pipe, leg.read))
    _ = gone.cancel()
    await _settle()

    assert gone.cancelled()
    assert await kept == ToolOffer(pipe_id=pipe.id)


async def test_one_wait_takes_one_offer_oldest_first(world: _World) -> None:
    first, _ = await world.agent()
    second, _ = await world.agent()

    assert await world.take() == ToolOffer(pipe_id=first.id)
    assert await world.take() == ToolOffer(pipe_id=second.id)
    assert await world.take() is None


async def test_only_the_machine_asked_for_is_offered_its_pipes(world: _World) -> None:
    pipe, _ = await world.agent(machine_id=_OTHER)

    assert await world.take() is None
    assert await world.take(_OTHER) == ToolOffer(pipe_id=pipe.id)


async def test_a_wait_woken_twice_before_it_runs_takes_one_offer(world: _World) -> None:
    waiting = world.run(world.hub.wait(_MACHINE, wait_sec=25))
    await _settle()

    first = world.join()
    _ = world.join()

    assert await waiting == ToolOffer(pipe_id=first.id)


async def test_a_revoke_right_after_an_agent_arrives_is_what_the_wait_hears(
    world: _World,
) -> None:
    waiting = world.run(world.hub.wait(_MACHINE, wait_sec=25))
    await _settle()

    # The agent woke the wait first, and the revoke found it already woken: the pipe is
    # gone by the time the wait runs, so it holds on with nothing to take.
    _ = world.join()
    world.hub.close_machine(_MACHINE)
    await _settle()

    assert not waiting.done()


# -- Dialing back -----------------------------------------------------------------------


async def test_a_host_that_never_dials_back_ends_the_pipe_in_20_seconds(
    world: _World,
) -> None:
    pipe, agent = await world.agent()
    assert await world.take() == ToolOffer(pipe_id=pipe.id)

    await world.time.advance(19)
    assert agent.closed == []
    await world.time.advance(1)

    assert agent.closed == [wire_terminals.CLOSE_HOST_LOST]
    assert world.hub.attach(_MACHINE, pipe.id, _Leg(), instance=_INSTANCE) is None


async def test_a_host_that_dials_back_in_time_is_not_ended_by_the_timer(
    world: _World,
) -> None:
    agent, host = await world.paired()

    await world.time.advance(40)

    assert (agent.closed, host.closed) == ([], [])


async def test_a_hub_holds_the_pipes_and_waits_the_dial_back_it_is_given() -> None:
    world = _World(pipes_per_machine=1, dial_back_sec=5)
    pipe, agent = await world.agent()

    with pytest.raises(PipeBusyError) as busy:
        _ = world.join()
    await world.time.advance(4)
    assert agent.closed == []
    await world.time.advance(1)

    assert busy.value.limit == 1
    assert str(busy.value) == "the machine already holds its limit of 1 pipes"
    assert agent.closed == [wire_terminals.CLOSE_HOST_LOST]
    assert world.hub.attach(_MACHINE, pipe.id, _Leg(), instance=_INSTANCE) is None
    for task in world.tasks:
        _ = task.cancel()
    _ = await asyncio.gather(*world.tasks, return_exceptions=True)


async def test_an_agent_that_leaves_before_a_host_takes_the_pipe_is_never_offered(
    world: _World,
) -> None:
    _, agent = await world.agent()

    agent.drop()
    await _settle()

    assert agent.closed == [wire_terminals.CLOSE_ENDED]
    assert await world.take() is None


async def test_a_pipe_is_attached_only_by_its_own_machine(world: _World) -> None:
    pipe, _ = await world.agent()
    assert await world.take() == ToolOffer(pipe_id=pipe.id)

    assert world.hub.attach(_OTHER, pipe.id, _Leg(), instance=_INSTANCE) is None
    assert (
        world.hub.attach(_MACHINE, uuid.UUID(int=99), _Leg(), instance=_INSTANCE)
        is None
    )


async def test_a_pipe_its_host_was_never_offered_is_not_offered_again_once_attached(
    world: _World,
) -> None:
    """Only an offer names a pipe's id; one attached that way leaves the queue too."""
    pipe, _ = await world.agent()
    assert world.hub.attach(_MACHINE, pipe.id, _Leg(), instance=_INSTANCE) is pipe

    assert await world.take() == ToolOffer(pipe_id=pipe.id)


async def test_a_pipe_is_attached_once(world: _World) -> None:
    pipe, _ = await world.agent()
    assert await world.take() == ToolOffer(pipe_id=pipe.id)
    _ = await world.host(pipe)

    assert world.hub.attach(_MACHINE, pipe.id, _Leg(), instance=_INSTANCE) is None


# -- The relay --------------------------------------------------------------------------


async def test_bytes_cross_both_ways_unread(world: _World) -> None:
    agent, host = await world.paired()

    agent.push(b"not json \xff\n", b"half a li")
    host.push(b"\x00\xfe anything", b"ne\n")
    await _settle()

    assert host.sent == [b"not json \xff\n", b"half a li"]
    assert agent.sent == [b"\x00\xfe anything", b"ne\n"]


async def test_a_machine_is_idle_from_the_hub_start_until_a_call(
    world: _World,
) -> None:
    pipe, _, _ = await _live(world)
    await world.time.advance(30)
    assert world.hub.idle_sec(_MACHINE) == 30
    assert world.hub.idle_sec(_OTHER) == 30

    world.hub.called(pipe, "c1")

    assert world.hub.idle_sec(_MACHINE) == 0
    assert world.hub.idle_sec(_OTHER) == 30


async def test_a_call_held_open_past_any_idle_time_keeps_its_machine_in_use(
    world: _World,
) -> None:
    pipe, _, host = await _live(world)
    world.hub.called(pipe, "c1")
    world.hub.called(pipe, "c2")
    await world.time.advance(3_000)
    assert world.hub.idle_sec(_MACHINE) == 0

    # The result of c1 comes split across frames, behind a ping; c2 still runs.
    host.push(b'{"py/object": "Ping"}\n{"py/object": "Res', b'ult", "id": "c1"}\n')
    await _settle()
    assert (set(pipe.running), world.hub.idle_sec(_MACHINE)) == ({"c2"}, 0)

    host.push(b'{"py/object": "Result", "id": "c2", "record": {}}\n')
    await _settle()
    await world.time.advance(7)
    assert (set(pipe.running), world.hub.idle_sec(_MACHINE)) == (set(), 7)


async def test_a_call_never_answered_keeps_its_machine_in_use_an_hour_no_longer(
    world: _World,
) -> None:
    pipe, _, _ = await _live(world)
    world.hub.called(pipe, "hung")
    await world.time.advance(3_599)
    in_use = world.hub.idle_sec(_MACHINE)

    await world.time.advance(1_801)

    assert (in_use, world.hub.idle_sec(_MACHINE)) == (0, 1_800)
    assert set(pipe.running) == {"hung"}


async def test_an_ack_ends_a_call_whose_result_the_hub_could_not_read(
    world: _World,
) -> None:
    pipe, agent, host = await _live(world)
    world.hub.called(pipe, "c1")

    long = b'{"py/object": "Result", "id": "c1", "record": "' + b"x" * _LINE_BYTES
    host.push(long, b'"}\n', b"not json\n")
    await _settle()
    running = set(pipe.running)
    world.hub.acked(pipe, "c1")
    world.hub.acked(pipe, "c9")

    assert (running, set(pipe.running)) == ({"c1"}, set())
    assert len(agent.sent) == 3


async def test_a_pipe_that_drops_mid_call_leaves_its_machine_idle_from_the_drop(
    world: _World,
) -> None:
    pipe, agent, _ = await _live(world)
    world.hub.called(pipe, "c1")
    await world.time.advance(600)

    agent.drop()
    await _settle()
    await world.time.advance(5)

    assert pipe.ended.done()
    assert (set(pipe.running), world.hub.idle_sec(_MACHINE)) == (set(), 5)


async def test_a_quiet_pipe_that_only_pings_keeps_no_machine_in_use(
    world: _World,
) -> None:
    agent, host = await world.paired()
    await world.time.advance(60)

    host.push(b'{"py/object": "Ping"}\n')
    agent.push(b'{"py/object": "Ack", "id": "c1"}\n')
    await _settle()

    assert world.hub.idle_sec(_MACHINE) == 60
    assert (len(agent.sent), len(host.sent)) == (1, 1)


async def _live(world: _World) -> tuple[Pipe, _Leg, _Leg]:
    """Bring up a whole pipe; return it, its agent's leg and its host's."""
    pipe, agent = await world.agent()
    assert await world.take() == ToolOffer(pipe_id=pipe.id)
    return pipe, agent, await world.host(pipe)


async def test_what_an_agent_sends_before_its_host_dials_back_waits_for_it(
    world: _World,
) -> None:
    pipe, agent = await world.agent()
    agent.push(b"Hello\n", b"Next\n")
    await _settle()
    assert agent.reads == 1
    assert await world.take() == ToolOffer(pipe_id=pipe.id)

    host = await world.host(pipe)

    assert host.sent == [b"Hello\n", b"Next\n"]


async def test_a_write_past_a_frame_is_split_into_frames(world: _World) -> None:
    agent, host = await world.paired()
    size = wire_tools.FRAME_BYTES

    agent.push(b"a" * (size + 1))
    host.push(b"b" * (2 * size))
    await _settle()

    assert [len(frame) for frame in host.sent] == [size, 1]
    assert [len(frame) for frame in agent.sent] == [size, size]


async def test_an_empty_frame_sends_nothing(world: _World) -> None:
    agent, host = await world.paired()

    agent.push(b"")
    host.push(b"")
    await _settle()

    assert (agent.sent, host.sent) == ([], [])
    assert (agent.closed, host.closed) == ([], [])


async def test_a_slow_agent_slows_the_host(world: _World) -> None:
    agent, host = await world.paired()
    agent.open.clear()
    reads = host.reads

    host.push(b"one", b"two", b"three")
    await _settle()

    # The read already waiting took the first frame, which is stuck in the send, so no
    # other was read, and nothing past it sits in the server.
    assert host.reads == reads
    assert agent.sent == []

    agent.open.set()
    await _settle()

    assert agent.sent == [b"one", b"two", b"three"]


async def test_a_slow_host_slows_the_agent(world: _World) -> None:
    agent, host = await world.paired()
    host.open.clear()
    reads = agent.reads

    agent.push(b"one", b"two")
    await _settle()

    assert agent.reads == reads

    host.open.set()
    await _settle()

    assert host.sent == [b"one", b"two"]


# -- How a pipe ends --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dropped", "code"),
    [
        ("agent", wire_terminals.CLOSE_ENDED),
        ("host", wire_terminals.CLOSE_HOST_LOST),
    ],
)
async def test_a_dropped_leg_ends_the_other(
    world: _World,
    dropped: str,
    code: int,
) -> None:
    agent, host = await world.paired()

    (agent if dropped == "agent" else host).drop()
    await _settle()

    assert (agent.closed, host.closed) == ([code], [code])


async def test_a_leg_that_drops_while_the_other_is_stuck_sending_to_it_ends_both(
    world: _World,
) -> None:
    agent, host = await world.paired()
    agent.open.clear()
    host.push(b"stuck")
    await _settle()

    agent.drop()
    await _settle()

    assert (agent.closed, host.closed) == (
        [wire_terminals.CLOSE_ENDED],
        [wire_terminals.CLOSE_ENDED],
    )
    assert agent.sent == []


@pytest.mark.parametrize(
    ("gone", "code"),
    [
        ("agent", wire_terminals.CLOSE_ENDED),
        ("host", wire_terminals.CLOSE_HOST_LOST),
    ],
)
async def test_a_send_that_finds_its_peer_gone_ends_the_pipe(
    world: _World,
    gone: str,
    code: int,
) -> None:
    agent, host = await world.paired()

    if gone == "agent":
        agent.gone = True
        host.push(b"to nobody")
    else:
        host.gone = True
        agent.push(b"to nobody")
    await _settle()

    assert (agent.closed, host.closed) == ([code], [code])


async def test_a_serve_cancelled_with_its_server_ends_the_pipe(world: _World) -> None:
    pipe, agent = await world.agent()
    leg = _Leg()
    assert await world.take() == ToolOffer(pipe_id=pipe.id)
    assert world.hub.attach(_MACHINE, pipe.id, leg, instance=_INSTANCE) is pipe
    serving = world.run(world.hub.serve_host(pipe, leg.read))
    await _settle()

    _ = serving.cancel()
    await _settle()

    assert serving.cancelled()
    assert (agent.closed, leg.closed) == (
        [wire_terminals.CLOSE_ENDED],
        [wire_terminals.CLOSE_ENDED],
    )


async def test_the_seventeenth_pipe_on_a_machine_is_refused(world: _World) -> None:
    joined = [await world.agent() for _ in range(16)]

    with pytest.raises(PipeBusyError) as busy:
        _ = world.join()
    assert busy.value.limit == 16

    # Another machine is not counted against this one.
    _ = await world.agent(machine_id=_OTHER)

    # A pipe that ended gives its slot back, so no drop leaks one.
    joined[0][1].drop()
    await _settle()
    _ = await world.agent()
    with pytest.raises(PipeBusyError):
        _ = world.join()


@pytest.mark.parametrize("ending", ["agent_drops", "host_drops", "revoked", "timer"])
async def test_every_ending_gives_all_sixteen_slots_back(
    world: _World,
    ending: str,
) -> None:
    agents = [await world.paired() for _ in range(8)]
    waiting = [await world.agent() for _ in range(8)]

    if ending == "revoked":
        world.hub.close_machine(_MACHINE)
    elif ending == "timer":
        for agent, _ in agents:
            agent.drop()
        await world.time.advance(20)
    else:
        for agent, host in agents:
            (agent if ending == "agent_drops" else host).drop()
        for _, agent in waiting:
            agent.drop()
    await _settle()

    for _ in range(16):
        _ = world.join()


async def test_a_revoked_machine_ends_its_pipes_and_leaves_another_machines(
    world: _World,
) -> None:
    agent, host = await world.paired()
    waiting_pipe, waiting = await world.agent()
    other_pipe, other = await world.agent(machine_id=_OTHER)

    world.hub.close_machine(_MACHINE)
    await _settle()

    revoked = wire_terminals.CLOSE_MACHINE_REVOKED
    assert (agent.closed, host.closed, waiting.closed) == (
        [revoked],
        [revoked],
        [revoked],
    )
    assert other.closed == []
    assert (
        world.hub.attach(_MACHINE, waiting_pipe.id, _Leg(), instance=_INSTANCE) is None
    )
    assert await world.take(_OTHER) == ToolOffer(pipe_id=other_pipe.id)


async def test_revoking_a_machine_ends_a_held_wait_and_leaves_another_machines(
    world: _World,
) -> None:
    held = world.run(world.hub.wait(_MACHINE, wait_sec=25))
    other = world.run(world.hub.wait(_OTHER, wait_sec=25))
    await _settle()

    world.hub.close_machine(_MACHINE)

    with pytest.raises(PipeRevokedError):
        _ = await held
    assert not other.done()


async def test_a_machine_that_rejoins_after_a_revoke_takes_new_pipes(
    world: _World,
) -> None:
    world.hub.close_machine(_MACHINE)

    pipe, _ = await world.agent()

    assert await world.take() == ToolOffer(pipe_id=pipe.id)


# -- Asking the rule again ---------------------------------------------------------------


async def test_a_live_pipe_survives_a_sweep(world: _World) -> None:
    agent, host = await world.paired()

    await world.hub.sweep()
    await _settle()

    assert (agent.closed, host.closed) == ([], [])
    agent.push(b"Ping\n")
    await _settle()
    assert host.sent == [b"Ping\n"]


async def test_the_sweep_asks_about_who_opened_a_pipe_and_who_attached_to_it(
    world: _World,
) -> None:
    _ = await world.paired()
    queued, _leg = await world.agent()

    await world.hub.sweep()

    assert queued.instance is None
    assert world.asked == [(_ASKER, _KEY, _INSTANCE), (_ASKER, _KEY, None)]


async def test_a_sweep_refuses_a_pipe_whose_rule_no_longer_holds_and_frees_its_slot(
    world: _World,
) -> None:
    pipe, agent = await world.agent()
    assert await world.take() == ToolOffer(pipe_id=pipe.id)
    host = await world.host(pipe)
    _, other = await world.agent()
    world.dead.add(pipe.id)

    await world.hub.sweep()
    await _settle()

    refused = wire_terminals.CLOSE_REFUSED
    assert (agent.closed, host.closed) == ([refused], [refused])
    assert other.closed == []
    for _ in range(15):
        _ = world.join()
    with pytest.raises(PipeBusyError):
        _ = world.join()


async def test_a_sweep_refuses_a_pipe_no_host_has_taken_and_it_is_not_offered(
    world: _World,
) -> None:
    pipe, agent = await world.agent()
    world.dead.add(pipe.id)

    await world.hub.sweep()
    await _settle()

    assert agent.closed == [wire_terminals.CLOSE_REFUSED]
    assert await world.take() is None


async def test_a_failing_question_leaves_the_pipe_up_and_asks_about_the_rest(
    world: _World,
    caplog: pytest.LogCaptureFixture,
) -> None:
    unknown, unknown_agent = await world.agent()
    dead, dead_agent = await world.agent()
    world.unknown.add(unknown.id)
    world.dead.add(dead.id)

    await world.hub.sweep()
    await _settle()

    assert unknown_agent.closed == []
    assert dead_agent.closed == [wire_terminals.CLOSE_REFUSED]
    assert "liveness unknown" in caplog.text


async def test_the_sweep_loop_sweeps_a_pipe_hub(world: _World) -> None:
    pipe, agent = await world.agent()
    world.dead.add(pipe.id)

    looping = world.run(sweep_loop(world.hub, interval_sec=0))
    await _settle()

    assert agent.closed == [wire_terminals.CLOSE_REFUSED]
    assert not looping.done()


# -- The seam for the line check ----------------------------------------------------------


class _Rewrite:
    """A line check: says what goes each way, and notes what it was shown."""

    def __init__(self, *answers: tuple[bytes, bytes]) -> None:
        self.shown: list[bytes] = []
        self._answers = list(answers)

    async def __call__(self, pipe: Pipe, frame: bytes) -> tuple[bytes, bytes]:
        del pipe
        self.shown.append(frame)
        return self._answers.pop(0)


async def test_inbound_decides_what_reaches_the_host_and_answers_the_agent(
    world: _World,
) -> None:
    check = _Rewrite((b"", b"refused\n"), (b"rewritten\n", b""))
    agent, host = await world.paired(inbound=check)

    agent.push(b"denied\n", b"allowed\n")
    await _settle()

    assert check.shown == [b"denied\n", b"allowed\n"]
    assert host.sent == [b"rewritten\n"]
    assert agent.sent == [b"refused\n"]


async def test_inbound_never_reads_the_hosts_direction(world: _World) -> None:
    check = _Rewrite()
    agent, host = await world.paired(inbound=check)

    host.push(b"from the host\n")
    await _settle()

    assert check.shown == []
    assert agent.sent == [b"from the host\n"]


async def test_an_answer_to_the_agent_waits_for_the_end_of_the_hosts_line(
    world: _World,
) -> None:
    check = _Rewrite((b"", b"refused\n"))
    agent, host = await world.paired(inbound=check)

    host.push(b"half a li")
    await _settle()
    agent.push(b"denied\n")
    await _settle()
    assert agent.sent == [b"half a li"]

    host.push(b"ne\n")
    await _settle()

    assert agent.sent == [b"half a li", b"ne\n", b"refused\n"]


async def test_a_failing_inbound_ends_the_pipe_and_forwards_nothing(
    world: _World,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def failing(pipe: Pipe, frame: bytes) -> tuple[bytes, bytes]:
        del pipe, frame
        raise ValueError("the line check broke")

    agent, host = await world.paired(inbound=failing)

    agent.push(b"Call\n")
    await _settle()

    assert host.sent == []
    assert (agent.closed, host.closed) == (
        [wire_terminals.CLOSE_REFUSED],
        [wire_terminals.CLOSE_REFUSED],
    )
    assert (
        agent.reasons
        == host.reasons
        == [
            "the server could not go on relaying this pipe",
        ]
    )
    assert "tool pipe failed" in caplog.text


async def test_an_inbound_refuses_the_pipe_saying_why(
    world: _World,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def refusing(pipe: Pipe, frame: bytes) -> tuple[bytes, bytes]:
        del pipe, frame
        raise PipeEndError("é" * 100)

    agent, host = await world.paired(inbound=refusing)

    agent.push(b"a line nobody can read\n")
    await _settle()

    assert host.sent == []
    assert (agent.closed, host.closed) == (
        [wire_terminals.CLOSE_REFUSED],
        [wire_terminals.CLOSE_REFUSED],
    )
    # A close frame holds 123 bytes of reason, and a character is cut whole.
    assert agent.reasons == host.reasons == ["é" * 61]
    assert "tool pipe failed" not in caplog.text
    assert str(PipeEndError("why")) == "why"


# -- What is logged -----------------------------------------------------------------------


def _fields(message: str) -> dict[str, str]:
    """Return a log line's ``key=value`` fields, a quoted value unquoted."""
    found = cast(
        "list[tuple[str, str, str]]",
        re.findall(r"(\w+)=(?:'([^']*)'|(\S+))", message),
    )
    return {key: quoted or bare for key, quoted, bare in found}


def _ended(caplog: pytest.LogCaptureFixture, pipe: Pipe) -> list[dict[str, str]]:
    """Return the fields of each line saying ``pipe`` ended."""
    return [
        _fields(record.getMessage())
        for record in caplog.records
        if record.getMessage().startswith("tool pipe ended:")
        and f"pipe={pipe.id}" in record.getMessage()
    ]


@pytest.mark.parametrize(
    ("ending", "said"),
    [
        ("agent_leaves", ("agent", wire_terminals.CLOSE_ENDED, "the agent left")),
        ("host_leaves", ("host", wire_terminals.CLOSE_HOST_LOST, "the host left")),
        (
            "revoked",
            (
                "revoke",
                wire_terminals.CLOSE_MACHINE_REVOKED,
                "the machine was revoked, or joined again",
            ),
        ),
        (
            "swept",
            (
                "sweep",
                wire_terminals.CLOSE_REFUSED,
                "no longer allowed: what opened this pipe no longer holds",
            ),
        ),
    ],
)
async def test_a_pipe_that_ends_logs_one_line_with_its_leg_code_and_time(
    world: _World,
    caplog: pytest.LogCaptureFixture,
    ending: str,
    said: tuple[str, int, str],
) -> None:
    by, code, reason = said
    caplog.set_level(logging.INFO, logger=tool_pipes.__name__)
    pipe, agent = await world.agent()
    assert await world.take() == ToolOffer(pipe_id=pipe.id)
    # The offer handed to a host says so, and which pipe it names.
    assert [
        _fields(r.getMessage())
        for r in caplog.records
        if r.getMessage().startswith("tool pipe offered:")
    ] == [{"pipe": str(pipe.id), "machine_id": str(_MACHINE)}]
    host = await world.host(pipe)
    await world.time.advance(3)

    if ending == "agent_leaves":
        agent.drop()
    elif ending == "host_leaves":
        host.drop()
    elif ending == "revoked":
        world.hub.close_machine(_MACHINE)
    else:
        world.dead.add(pipe.id)
        await world.hub.sweep()
    await _settle()
    world.hub.close_machine(_MACHINE)
    await _settle()

    assert _ended(caplog, pipe) == [
        {
            "pipe": str(pipe.id),
            "machine_id": str(_MACHINE),
            "by": by,
            "code": str(code),
            "reason": reason,
            "cause": "-",
            "instance": str(_INSTANCE),
            "duration_sec": "3.000",
        },
    ]


async def test_a_dial_back_that_times_out_warns_and_ends_the_pipe_once(
    world: _World,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=tool_pipes.__name__)
    pipe, _ = await world.agent()

    await world.time.advance(20)

    warned = [
        _fields(record.getMessage())
        for record in caplog.records
        if record.levelno == logging.WARNING
    ]
    assert warned == [
        {"pipe": str(pipe.id), "machine_id": str(_MACHINE), "wait_sec": "20"},
    ]
    (ended,) = _ended(caplog, pipe)
    # No host attached, so none is named.
    assert (ended["by"], ended["code"], ended["instance"]) == (
        "dial-back",
        str(wire_terminals.CLOSE_HOST_LOST),
        "-",
    )


@pytest.mark.parametrize(
    ("error", "by", "cause"),
    [
        (PipeEndError("refused"), "agent", "-"),
        (ValueError("the line check broke"), "server", "ValueError"),
    ],
)
async def test_a_pipe_its_line_check_ends_names_the_leg_and_only_the_errors_type(
    world: _World,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
    by: str,
    cause: str,
) -> None:
    caplog.set_level(logging.INFO, logger=tool_pipes.__name__)

    async def failing(pipe: Pipe, frame: bytes) -> tuple[bytes, bytes]:
        del pipe, frame
        raise error

    pipe, agent = await world.agent(inbound=failing)
    assert await world.take() == ToolOffer(pipe_id=pipe.id)
    _ = await world.host(pipe)

    agent.push(b"secret-line\n")
    await _settle()

    (ended,) = _ended(caplog, pipe)
    assert (ended["by"], ended["cause"]) == (by, cause)
    assert "secret-line" not in caplog.text


# -- An old host's wait ------------------------------------------------------------------


async def test_an_old_hosts_hold_lasts_its_time_and_is_never_offered_a_pipe(
    world: _World,
) -> None:
    holding = world.run(world.hub.hold(_MACHINE, wait_sec=25))
    await _settle()

    pipe, _ = await world.agent()
    await _settle()

    assert not holding.done()
    assert await world.take() == ToolOffer(pipe_id=pipe.id)
    await world.time.advance(25)
    assert await holding is None


async def test_an_old_hosts_hold_of_no_time_ends_at_once(world: _World) -> None:
    await world.hub.hold(_MACHINE, wait_sec=0)


async def test_revoking_a_machine_ends_an_old_hosts_hold(world: _World) -> None:
    holding = world.run(world.hub.hold(_MACHINE, wait_sec=25))
    await _settle()

    world.hub.close_machine(_MACHINE)

    with pytest.raises(PipeRevokedError):
        _ = await holding


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
