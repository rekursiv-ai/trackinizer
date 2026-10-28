"""Tests for ``Child``: a started process, and its group, that no cancel strands."""

from __future__ import annotations

from pathlib import Path
from typing import IO, TYPE_CHECKING

import asyncio
import contextlib
import os
import signal
import time

import pytest

from trackinizer.lib.posix.child import Child, finish


if TYPE_CHECKING:
    from asyncio.subprocess import Process
    from collections.abc import Collection, Iterator, Mapping, Sequence


@pytest.fixture
def started() -> Iterator[list[int]]:
    """Collect the pids a test starts; whatever still runs at teardown is killed."""
    pids: list[int] = []
    yield pids
    for pid in pids:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


@pytest.mark.asyncio
async def test_a_cancel_while_a_child_starts_stops_it_and_its_jobs(
    tmp_path: Path,
    started: list[int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cancel lands before the caller holds the child, so no one else can stop it."""
    real = asyncio.create_subprocess_exec
    leaders: list[Process] = []
    handed = asyncio.Event()

    async def slow(
        *argv: str,
        stdin: int | IO[bytes] | None,
        stdout: int | IO[bytes] | None,
        stderr: int | IO[bytes] | None,
        cwd: str | Path | None,
        env: Mapping[str, str] | None,
        pass_fds: Collection[int],
        limit: int,
        start_new_session: bool,
    ) -> Process:
        leader = await real(
            *argv,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            cwd=cwd,
            env=env,
            pass_fds=pass_fds,
            limit=limit,
            start_new_session=start_new_session,
        )
        leaders.append(leader)
        await handed.wait()
        return leader

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow)
    starting = asyncio.create_task(
        Child.start(
            "/bin/sh",
            "-c",
            "sleep 60 & echo $! > job.pid; wait",
            group=True,
            cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE,
        ),
    )
    job = await _written_pid(tmp_path / "job.pid")
    started.extend([leaders[0].pid, job])
    starting.cancel()
    handed.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(starting, timeout=5)
    assert await _gone_within(started)


@pytest.mark.asyncio
async def test_a_child_that_cannot_start_raises_and_leaves_nothing(
    tmp_path: Path,
) -> None:
    with pytest.raises(FileNotFoundError):
        await Child.start(str(tmp_path / "missing"))


@pytest.mark.asyncio
async def test_a_child_runs_where_and_with_what_it_is_given(tmp_path: Path) -> None:
    """Each start option reaches the process."""
    read_end, write_end = os.pipe()
    try:
        # bash, not sh: dash redirects to single-digit descriptors only.
        async with await Child.start(
            "/bin/bash",
            "-c",
            f'cat; pwd; echo "$GREETING"; echo fd >&{write_end}',
            cwd=tmp_path,
            env={"GREETING": "hello", "PATH": os.environ["PATH"]},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            pass_fds=(write_end,),
            limit=8,
        ) as child:
            out, _ = await child.process.communicate(b"in\n")
            assert await child.exited() == 0
        os.close(write_end)
        write_end = -1
        assert out.decode().split("\n") == ["in", str(tmp_path), "hello", ""]
        assert os.read(read_end, 8) == b"fd\n"
    finally:
        os.close(read_end)
        if write_end >= 0:
            os.close(write_end)


@pytest.mark.asyncio
async def test_stopping_a_group_ends_the_jobs_it_started(
    tmp_path: Path,
    started: list[int],
) -> None:
    child = await Child.start(
        "/bin/sh",
        "-c",
        "sleep 60 & echo $! > job.pid; wait",
        group=True,
        cwd=tmp_path,
        stdout=asyncio.subprocess.PIPE,
    )
    started.extend([child.process.pid, await _written_pid(tmp_path / "job.pid")])
    assert await asyncio.wait_for(child.stop(), timeout=5) == -signal.SIGTERM
    assert await _gone_within(started)


@pytest.mark.asyncio
async def test_a_job_that_ignores_term_dies_with_its_group_after_the_grace(
    tmp_path: Path,
    started: list[int],
) -> None:
    """The leader ends at TERM; its job does not, and the group outlives it."""
    child = await Child.start(
        "/bin/sh",
        "-c",
        "(trap '' TERM; sleep 60) & echo $! > job.pid; wait",
        group=True,
        grace_sec=0.05,
        cwd=tmp_path,
    )
    started.extend([child.process.pid, await _written_pid(tmp_path / "job.pid")])
    await asyncio.wait_for(child.stop(), timeout=5)
    assert await _gone_within(started)


@pytest.mark.asyncio
async def test_stopping_only_the_child_leaves_the_jobs_it_started(
    tmp_path: Path,
    started: list[int],
) -> None:
    child = await Child.start(
        "/bin/sh",
        "-c",
        "sleep 60 & echo $! > job.pid; wait",
        cwd=tmp_path,
    )
    job = await _written_pid(tmp_path / "job.pid")
    started.extend([child.process.pid, job])
    await asyncio.wait_for(child.stop(), timeout=5)
    assert not await _gone_within([job], seconds=0.05)


@pytest.mark.asyncio
async def test_a_stop_cancelled_midway_still_ends_the_child(
    tmp_path: Path,
    started: list[int],
) -> None:
    """A child that ignores TERM is killed after the grace, whoever was waiting."""
    child = await Child.start(
        "/bin/sh",
        "-c",
        "trap '' TERM; echo $$ > shell.pid; sleep 60",
        group=True,
        grace_sec=0.05,
        cwd=tmp_path,
    )
    started.extend([child.process.pid, await _written_pid(tmp_path / "shell.pid")])
    stopping = asyncio.create_task(child.stop())
    await asyncio.sleep(0.01)
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(stopping, timeout=5)
    assert child.process.returncode == -signal.SIGKILL
    assert await _gone_within(started)


@pytest.mark.asyncio
async def test_stopping_a_child_that_has_exited_returns_its_status() -> None:
    child = await Child.start("/bin/sh", "-c", "exit 3")
    assert await child.exited() == 3
    assert await child.stop() == await child.stop() == 3


@pytest.mark.asyncio
async def test_a_child_has_exited_while_a_job_still_holds_its_pipe(
    tmp_path: Path,
    started: list[int],
) -> None:
    """Before 3.13, ``Process.wait`` also waits for the pipes, here held a minute."""
    child = await Child.start(
        "/bin/sh",
        "-c",
        "setsid sleep 60 & echo $! > job.pid",
        cwd=tmp_path,
        stdout=asyncio.subprocess.PIPE,
    )
    started.append(await _written_pid(tmp_path / "job.pid"))
    assert await asyncio.wait_for(child.exited(), timeout=5) == 0


@pytest.mark.asyncio
async def test_leaving_the_block_stops_the_child_whatever_ends_it(
    tmp_path: Path,
    started: list[int],
) -> None:
    child = await Child.start(
        "/bin/sh",
        "-c",
        "echo $$ > shell.pid; exec sleep 60",
        cwd=tmp_path,
    )
    started.append(await _written_pid(tmp_path / "shell.pid"))
    with pytest.raises(RuntimeError, match=r"^left$"):
        await _leave_raising(child)
    assert await _gone_within(started)


async def _leave_raising(child: Child) -> None:
    """Leave ``child``'s block by an error."""
    async with child:
        raise RuntimeError("left")


@pytest.mark.asyncio
async def test_finished_work_is_its_result_and_a_cancel_waits_for_it() -> None:
    gate = asyncio.Event()
    done: list[str] = []

    async def work() -> str:
        await gate.wait()
        done.append("done")
        return "result"

    assert await finish(asyncio.sleep(0, result="now")) == "now"
    waiting = asyncio.create_task(finish(work()))
    await asyncio.sleep(0)
    waiting.cancel()
    await asyncio.sleep(0)
    assert not waiting.done()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert done == ["done"]


async def _written_pid(path: Path) -> int:
    """Wait until ``path`` holds a pid, then return it."""
    async with asyncio.timeout(5):
        while True:
            text = _read(path)
            if text:
                return int(text)
            await asyncio.sleep(0.01)


def _read(path: Path) -> str:
    """Return ``path``'s text, stripped; empty until it is written."""
    return path.read_text().strip() if path.exists() else ""


def _running(pid: int) -> bool:
    """Whether ``pid`` runs; a zombie, which waits only to be reaped, does not."""
    stat = Path(f"/proc/{pid}/stat")
    if stat.parent.parent.is_dir():
        try:
            return stat.read_text().split(") ", 1)[1][0] != "Z"
        except FileNotFoundError:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _gone_within(pids: Sequence[int], *, seconds: float = 3.0) -> bool:
    """Whether every process in ``pids`` has ended within ``seconds``."""
    deadline = time.monotonic() + seconds
    while any(_running(pid) for pid in pids):
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.01)
    return True


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
