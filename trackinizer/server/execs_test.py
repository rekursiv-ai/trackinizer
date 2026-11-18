"""The exec hub: queueing, waits, results and the limits, on a fake clock."""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio
import itertools
import uuid

import pytest
import pytest_asyncio

from trackinizer.server import execs
from trackinizer.server.execs import (
    Exec,
    ExecBusyError,
    ExecHub,
    ExecLimitError,
    ExecLostError,
    ExecRevokedError,
    ExecStaleError,
    Submission,
)
from trackinizer.wire import wire_exec
from trackinizer.wire.wire_exec import ExecRequest, ExecResult


if TYPE_CHECKING:
    from collections.abc import AsyncIterator


pytestmark = pytest.mark.asyncio

_MACHINE = uuid.UUID(int=1)
_OTHER = uuid.UUID(int=2)


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
    for _ in range(10):
        await asyncio.sleep(0)


def _result(*, exit_code: int = 0, stdout: str = "") -> ExecResult:
    return ExecResult(
        exit_code=exit_code,
        stdout_b64=stdout,
        stderr_b64="",
        cwd="/home/ada",
        truncated=False,
        duration_ms=1,
    )


class _World:
    def __init__(self) -> None:
        self.time = _Time()
        self.hub = ExecHub(clock=self.time, sleep=self.time.sleep)
        self.tasks: list[asyncio.Task[ExecResult]] = []

    async def start(
        self,
        *,
        user: str = "ada@x.y",
        machine_id: uuid.UUID = _MACHINE,
        timeout_sec: int = 5,
    ) -> asyncio.Task[ExecResult]:
        """Run a command in a task, and let it queue."""
        task = asyncio.ensure_future(
            self.hub.run(
                Submission(
                    user=user,
                    machine_id=machine_id,
                    machine="gx10",
                    request=ExecRequest(
                        command="ls",
                        cwd="~",
                        timeout_sec=timeout_sec,
                    ),
                ),
            ),
        )
        self.tasks.append(task)
        await _settle()
        return task

    async def take(self, machine_id: uuid.UUID = _MACHINE) -> Exec:
        found = await self.hub.wait(machine_id, wait_sec=0)
        assert found is not None
        return found


@pytest_asyncio.fixture
async def world() -> AsyncIterator[_World]:
    found = _World()
    yield found
    for task in found.tasks:
        _ = task.cancel()
    _ = await asyncio.gather(*found.tasks, return_exceptions=True)


@pytest.fixture
def ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the commands the ids 10, 11, ... so a test can name one it was not handed."""
    count = itertools.count(10)
    monkeypatch.setattr(execs, "uuid4", lambda: uuid.UUID(int=next(count)))


async def test_a_command_the_host_reports_on_returns_that_result(world: _World) -> None:
    run = await world.start()

    offered = await world.take()
    delivered = world.hub.deliver(_MACHINE, offered.id, _result(stdout="aGk="))

    assert delivered
    assert (await run).stdout_b64 == "aGk="
    assert (offered.request.command, offered.user) == ("ls", "ada@x.y")


async def test_a_held_wait_returns_the_moment_a_command_is_queued(
    world: _World,
) -> None:
    waiting = asyncio.ensure_future(world.hub.wait(_MACHINE, wait_sec=25))
    await _settle()
    assert not waiting.done()

    run = await world.start()

    offered = await waiting
    assert offered is not None
    assert world.hub.deliver(_MACHINE, offered.id, _result())
    _ = await run


async def test_a_wait_with_nothing_queued_ends_empty_when_its_time_is_up(
    world: _World,
) -> None:
    waiting = asyncio.ensure_future(world.hub.wait(_MACHINE, wait_sec=25))
    await _settle()

    await world.time.advance(24)
    assert not waiting.done()
    await world.time.advance(1)

    assert await waiting is None


async def test_a_wait_of_no_time_answers_at_once(world: _World) -> None:
    assert await world.hub.wait(_MACHINE, wait_sec=0) is None
    run = await world.start()

    offered = await world.hub.wait(_MACHINE, wait_sec=0)

    assert offered is not None
    assert world.hub.deliver(_MACHINE, offered.id, _result())
    _ = await run


async def test_a_wait_that_lost_its_command_to_another_wait_keeps_waiting(
    world: _World,
) -> None:
    first = asyncio.ensure_future(world.hub.wait(_MACHINE, wait_sec=25))
    second = asyncio.ensure_future(world.hub.wait(_MACHINE, wait_sec=25))
    await _settle()

    run = await world.start()

    handed = [each for each in (first, second) if each.done()]
    assert len(handed) == 1
    offered = handed[0].result()
    assert offered is not None
    assert world.hub.deliver(_MACHINE, offered.id, _result())
    _ = await run
    await world.time.advance(25)
    assert [first.result(), second.result()].count(None) == 1


@pytest.mark.usefixtures("ids")
async def test_one_wait_takes_one_command_oldest_first(world: _World) -> None:
    first = await world.start()
    second = await world.start()

    one, two = await world.take(), await world.take()

    assert (one.id, two.id) == (uuid.UUID(int=10), uuid.UUID(int=11))
    assert await world.hub.wait(_MACHINE, wait_sec=0) is None
    for each in (one, two):
        assert world.hub.deliver(_MACHINE, each.id, _result())
    _ = await first, await second


async def test_only_the_machine_asked_for_is_handed_its_commands(
    world: _World,
) -> None:
    run = await world.start(machine_id=_OTHER)

    assert await world.hub.wait(_MACHINE, wait_sec=0) is None

    offered = await world.take(_OTHER)
    assert world.hub.deliver(_OTHER, offered.id, _result())
    _ = await run


async def test_a_fifth_command_is_refused_until_one_of_the_four_ends(
    world: _World,
) -> None:
    runs = [await world.start() for _ in range(wire_exec.MAX_RUNNING_PER_MACHINE)]

    refused = await world.start()
    assert isinstance(refused.exception(), ExecBusyError)
    other = await world.start(machine_id=_OTHER)
    assert not other.done()
    offered = await world.take()
    assert world.hub.deliver(_MACHINE, offered.id, _result())
    await _settle()
    again = await world.start()

    assert not again.done()
    assert sum(each.done() for each in runs) == 1


async def test_a_busy_refusal_does_not_count_against_the_rate(world: _World) -> None:
    runs = [await world.start() for _ in range(wire_exec.MAX_RUNNING_PER_MACHINE)]
    for _ in range(wire_exec.MAX_EXECS_PER_MIN_PER_USER):
        refused = await world.start()
        assert isinstance(refused.exception(), ExecBusyError)

    for each in runs:
        _ = each.cancel()
    await _settle()
    fresh = await world.start()

    assert not fresh.done()


async def test_a_user_past_the_rate_is_refused_until_the_minute_is_up(
    world: _World,
) -> None:
    per_minute = wire_exec.MAX_EXECS_PER_MIN_PER_USER
    capacity = wire_exec.MAX_RUNNING_PER_MACHINE
    for n in range(per_minute):
        _ = await world.start(machine_id=uuid.UUID(int=100 + n // capacity))

    refused = await world.start(machine_id=uuid.UUID(int=999))
    stranger = await world.start(user="bob@x.y", machine_id=uuid.UUID(int=998))
    assert isinstance(refused.exception(), ExecLimitError)
    assert not stranger.done()

    await world.time.advance(60)
    later = await world.start(machine_id=uuid.UUID(int=997))
    assert not later.done()


async def test_a_command_no_host_takes_is_lost_after_the_offer_window(
    world: _World,
) -> None:
    run = await world.start()

    await world.time.advance(wire_exec.OFFER_TTL_SEC - 1)
    assert not run.done()
    await world.time.advance(1)

    with pytest.raises(ExecLostError) as lost:
        _ = await run
    assert (lost.value.taken, str(lost.value)) == (
        False,
        "the host did not take the command",
    )
    assert await world.hub.wait(_MACHINE, wait_sec=0) is None


async def test_a_command_the_host_never_reports_on_is_lost_after_its_timeout_and_grace(
    world: _World,
) -> None:
    run = await world.start(timeout_sec=10)
    offered = await world.take()
    await _settle()

    await world.time.advance(10 + wire_exec.RESULT_GRACE_SEC - 1)
    assert not run.done()
    await world.time.advance(1)

    with pytest.raises(ExecLostError) as lost:
        _ = await run
    assert (lost.value.taken, str(lost.value)) == (
        True,
        "the host took the command but did not report",
    )
    assert not world.hub.deliver(_MACHINE, offered.id, _result())


@pytest.mark.usefixtures("ids")
@pytest.mark.parametrize("case", ["unknown", "other_machine", "not_handed"])
async def test_a_result_is_taken_only_from_the_machine_that_was_handed_the_command(
    world: _World,
    case: str,
) -> None:
    run = await world.start()
    if case != "not_handed":
        _ = await world.take()

    delivered = world.hub.deliver(
        _OTHER if case == "other_machine" else _MACHINE,
        uuid.UUID(int=77 if case == "unknown" else 10),
        _result(exit_code=9),
    )

    await _settle()
    assert not delivered
    assert not run.done()


@pytest.mark.usefixtures("ids")
@pytest.mark.parametrize("caller_waits", [True, False])
async def test_a_result_sent_again_is_accepted_and_changes_nothing(
    world: _World,
    caller_waits: bool,
) -> None:
    run = await world.start()
    _ = await world.take()
    if not caller_waits:
        _ = run.cancel()
        await _settle()
    assert world.hub.deliver(_MACHINE, uuid.UUID(int=10), _result())

    again = world.hub.deliver(_MACHINE, uuid.UUID(int=10), _result(exit_code=9))
    elsewhere = world.hub.deliver(_OTHER, uuid.UUID(int=10), _result())

    assert (again, elsewhere) == (True, False)
    if caller_waits:
        assert (await run).exit_code == 0
    await world.time.advance(61)
    assert not world.hub.deliver(_MACHINE, uuid.UUID(int=10), _result())


async def test_revoking_a_machine_ends_its_commands_handed_or_not(
    world: _World,
) -> None:
    handed = await world.start()
    queued = await world.start()
    other = await world.start(machine_id=_OTHER)
    offered = await world.take()

    world.hub.close_machine(_MACHINE)
    # Before the callers have run again, the revoked command is still in the hub.
    assert await world.hub.wait(_MACHINE, wait_sec=0) is None
    await _settle()

    for ended in (handed, queued):
        assert isinstance(ended.exception(), ExecRevokedError)
    assert not world.hub.deliver(_MACHINE, offered.id, _result())
    other_offered = await world.take(_OTHER)
    assert world.hub.deliver(_OTHER, other_offered.id, _result())
    _ = await other


async def test_revoking_a_machine_ends_a_held_wait_and_leaves_another_machines(
    world: _World,
) -> None:
    held = asyncio.ensure_future(world.hub.wait(_MACHINE, wait_sec=25))
    elsewhere = asyncio.ensure_future(world.hub.wait(_OTHER, wait_sec=25))
    await _settle()

    world.hub.close_machine(_MACHINE)
    await _settle()

    with pytest.raises(ExecRevokedError):
        _ = await held
    assert not elsewhere.done()
    await world.time.advance(25)
    assert await elsewhere is None


async def test_a_machine_that_rejoins_after_a_revoke_takes_new_commands(
    world: _World,
) -> None:
    _ = await world.start()
    world.hub.close_machine(_MACHINE)
    await _settle()

    fresh = await world.start()
    offered = await world.take()

    assert world.hub.deliver(_MACHINE, offered.id, _result())
    _ = await fresh


async def test_a_caller_that_gives_up_frees_its_slot_and_the_host_is_never_offered_it(
    world: _World,
) -> None:
    runs = [await world.start() for _ in range(wire_exec.MAX_RUNNING_PER_MACHINE)]

    for each in runs:
        _ = each.cancel()
    await _settle()

    assert await world.hub.wait(_MACHINE, wait_sec=0) is None
    fresh = await world.start()
    assert not fresh.done()


async def test_a_taken_command_whose_caller_left_keeps_its_slot_until_it_is_reported(
    world: _World,
) -> None:
    runs = [await world.start() for _ in range(wire_exec.MAX_RUNNING_PER_MACHINE)]
    offers = [await world.take() for _ in runs]
    for each in runs:
        _ = each.cancel()
    await _settle()

    refused = await world.start()
    assert isinstance(refused.exception(), ExecBusyError)
    assert world.hub.deliver(_MACHINE, offers[0].id, _result())
    fresh = await world.start()

    assert not fresh.done()
    assert isinstance(
        (await world.start()).exception(),
        ExecBusyError,
    )


async def test_a_taken_command_whose_caller_left_frees_its_slot_at_its_timeout_and_grace(
    world: _World,
) -> None:
    runs = [
        await world.start(timeout_sec=10)
        for _ in range(wire_exec.MAX_RUNNING_PER_MACHINE)
    ]
    for _ in runs:
        _ = await world.take()
    for each in runs:
        _ = each.cancel()
    await _settle()

    await world.time.advance(10 + wire_exec.RESULT_GRACE_SEC - 1)
    held = await world.start()
    assert isinstance(held.exception(), ExecBusyError)
    await world.time.advance(1)
    freed = await world.start()

    assert not freed.done()


async def test_a_command_lost_after_it_was_taken_holds_no_slot_once_its_time_is_up(
    world: _World,
) -> None:
    runs = [await world.start() for _ in range(wire_exec.MAX_RUNNING_PER_MACHINE)]
    for _ in runs:
        _ = await world.take()
    await _settle()

    await world.time.advance(5 + wire_exec.RESULT_GRACE_SEC)

    for each in runs:
        assert isinstance(each.exception(), ExecLostError)
    assert not (await world.start()).done()


async def test_a_command_a_caller_left_before_a_host_took_it_is_never_offered(
    world: _World,
) -> None:
    gone = await world.start()
    _ = gone.cancel()
    lost = await world.start()
    await world.time.advance(wire_exec.OFFER_TTL_SEC)
    await _settle()

    assert isinstance(lost.exception(), ExecLostError)
    assert not world.hub._pending
    assert await world.hub.wait(_MACHINE, wait_sec=0) is None


async def test_a_wait_that_ended_leaves_nothing_behind_for_its_machine(
    world: _World,
) -> None:
    waiting = asyncio.ensure_future(world.hub.wait(_MACHINE, wait_sec=25))
    await _settle()
    await world.time.advance(25)

    assert await waiting is None
    assert not world.hub._waiters


async def test_a_refused_or_finished_user_leaves_no_rate_record_behind(
    world: _World,
) -> None:
    runs = [await world.start() for _ in range(wire_exec.MAX_RUNNING_PER_MACHINE)]
    refused = await world.start(user="bob@x.y")
    assert isinstance(refused.exception(), ExecBusyError)
    for each in runs:
        _ = each.cancel()
    await _settle()

    await world.time.advance(60)
    _ = await world.start(machine_id=_OTHER)

    assert list(world.hub._starts) == ["ada@x.y"]


async def test_when_the_woken_wait_goes_before_it_runs_another_wait_gets_the_command(
    world: _World,
) -> None:
    first = asyncio.ensure_future(world.hub.wait(_MACHINE, wait_sec=25))
    second = asyncio.ensure_future(world.hub.wait(_MACHINE, wait_sec=25))
    await _settle()

    run = asyncio.ensure_future(
        world.hub.run(
            Submission(
                user="ada@x.y",
                machine_id=_MACHINE,
                machine="gx10",
                request=ExecRequest(command="ls", cwd="~", timeout_sec=5),
            ),
        ),
    )
    world.tasks.append(run)
    # The command is queued before the first wait runs again to take it.
    _ = first.cancel()
    await _settle()

    offered = await second
    assert offered is not None
    assert world.hub.deliver(_MACHINE, offered.id, _result())
    _ = await run


async def test_a_host_that_is_no_longer_current_is_handed_nothing(
    world: _World,
) -> None:
    async def superseded() -> bool:
        return False

    idle = asyncio.ensure_future(
        world.hub.wait(_MACHINE, wait_sec=25, is_current=superseded),
    )
    await _settle()
    run = await world.start()

    with pytest.raises(ExecStaleError):
        _ = await idle
    offered = await world.take()
    assert world.hub.deliver(_MACHINE, offered.id, _result())
    _ = await run


async def test_a_host_is_not_asked_whether_it_is_current_when_nothing_is_queued(
    world: _World,
) -> None:
    async def refuse() -> bool:
        raise AssertionError

    assert await world.hub.wait(_MACHINE, wait_sec=0, is_current=refuse) is None


async def test_revoking_a_machine_after_its_host_took_the_command_says_it_may_have_run(
    world: _World,
) -> None:
    handed = await world.start()
    queued = await world.start()
    _ = await world.take()

    world.hub.close_machine(_MACHINE)
    await _settle()

    for ended, taken in ((handed, True), (queued, False)):
        error = ended.exception()
        assert isinstance(error, ExecRevokedError)
        assert error.taken is taken


async def test_revoking_a_machine_forgets_the_commands_its_callers_left_running(
    world: _World,
) -> None:
    run = await world.start()
    offered = await world.take()
    _ = run.cancel()
    await _settle()

    world.hub.close_machine(_MACHINE)

    assert not world.hub.deliver(_MACHINE, offered.id, _result())
    assert not (await world.start()).done()


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
