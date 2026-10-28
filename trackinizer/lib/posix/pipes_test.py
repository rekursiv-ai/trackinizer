"""A piped child keeps its two output streams apart; a pty cannot.

That separation is the whole reason this exists beside
:class:`~trackinizer.lib.posix.terminal.Terminal`, so it is what these tests pin --
along with the deadlock a naive one-stream-at-a-time reader would hit.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import IO, TYPE_CHECKING
from unittest.mock import patch

import asyncio
import contextlib
import os
import signal
import sys
import time

import pytest

from trackinizer.lib.posix.pipes import Piped, Stream


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


def test_stdout_and_stderr_arrive_on_their_own_streams() -> None:
    """The separation a pty structurally cannot provide.

    On a pty both streams are the same slave tty, so the kernel interleaves
    them before any reader sees a byte and no later step can undo it.
    """
    chunks = asyncio.run(
        _collect(["sh", "-c", "printf 'to-out\\n'; printf 'to-err\\n' >&2"]),
    )

    joined = {name: "".join(t for n, t in chunks if n == name) for name, _ in chunks}
    assert joined["stdout"] == "to-out\n"
    assert joined["stderr"] == "to-err\n"


def test_a_child_flooding_one_stream_does_not_deadlock() -> None:
    """Both pipes are drained concurrently, so neither blocks the other.

    A reader that drained stdout to EOF first would hang here forever: the
    child cannot finish writing stdout until someone consumes stderr, and
    64 KiB is past the pipe buffer on every platform this runs on.
    """
    script = "printf 'x%.0s' $(seq 1 70000) >&2; printf 'done\\n'"

    chunks = asyncio.run(_collect(["sh", "-c", script]))

    assert "".join(t for n, t in chunks if n == "stdout") == "done\n"
    assert len("".join(t for n, t in chunks if n == "stderr")) == 70_000


def test_input_reaches_the_child() -> None:
    """Bytes written to stdin come back out, which is what makes it a session."""
    chunks = asyncio.run(_collect(["sh", "-c", "cat"], stdin=b"echoed\n"))

    assert "".join(t for n, t in chunks if n == "stdout") == "echoed\n"


def test_the_exit_status_is_the_childs() -> None:
    async def run() -> int:
        async with Piped(["sh", "-c", "exit 5"]) as child:
            child.close_stdin()
            return await child.wait()

    assert asyncio.run(run()) == 5


def test_a_missing_binary_names_itself() -> None:
    """Resolved before spawning, so the failure names the command."""

    async def run() -> None:
        async with Piped(["definitely-not-a-real-binary-xyz"]):
            pass

    with pytest.raises(FileNotFoundError, match="definitely-not-a-real-binary-xyz"):
        asyncio.run(run())


def test_a_child_holding_its_buffer_yields_nothing_until_it_flushes() -> None:
    """The documented cost of pipes, pinned so it is not mistaken for a bug.

    libc block-buffers on a pipe and line-buffers on a tty, so a child that
    never flushes produces NOTHING until it exits -- and one killed first loses
    it. The fix is the child's (``python -u``); there is none on this side.
    """
    script = (
        "import sys; print('held'); "
        "print('ready', file=sys.stderr, flush=True); sys.stdin.read(1)"
    )

    assert asyncio.run(_read_buffered_child(script)) == b"held\n"


def test_unstarted_process_operations_are_safe() -> None:
    """Operations before start report absence without touching a child."""

    async def run() -> tuple[bool, list[tuple[Stream, bytes]], int]:
        child = Piped(["unused"])
        return (
            await child.write(b"data"),
            [item async for item in child.output()],
            await child.wait(),
        )

    assert asyncio.run(run()) == (False, [], 0)


def test_write_reports_a_closed_stdin() -> None:
    """A process whose stdin is gone rejects the write."""

    async def run() -> bool:
        child = Piped(["unused"])
        with patch.object(child, "_child", _FakeChild(stdin=None)):
            return await child.write(b"data")

    assert asyncio.run(run()) is False


def test_write_reports_pipe_failures() -> None:
    """Broken and reset streams are reported as failed writes."""

    async def run(error: BaseException) -> bool:
        child = Piped(["unused"])
        with patch.object(
            child,
            "_child",
            _FakeChild(stdin=_FailingStdin(error)),
        ):
            return await child.write(b"data")

    for error in (BrokenPipeError(), ConnectionResetError(), RuntimeError()):
        assert asyncio.run(run(error)) is False


def test_output_with_no_readers_and_close_stdin_without_stdin() -> None:
    """Missing output and input streams are handled without raising."""

    async def run() -> list[tuple[Stream, bytes]]:
        child = Piped(["unused"])
        with patch.object(
            child,
            "_child",
            _FakeChild(stdin=None),
        ):
            child.close_stdin()
            return [item async for item in child.output()]

    assert asyncio.run(run()) == []


@pytest.mark.asyncio
async def test_terminate_kills_a_child_that_ignores_term_after_the_grace(
    tmp_path: Path,
    started: list[int],
) -> None:
    child = Piped(
        ["sh", "-c", "trap '' TERM; echo $$ > shell.pid; sleep 60"],
        cwd=tmp_path,
        terminate_grace_sec=0.05,
    )
    await child.start()
    started.append(await _written_pid(tmp_path / "shell.pid"))
    await asyncio.wait_for(child.terminate(), timeout=5)
    assert await child.wait() == -signal.SIGKILL
    assert await _gone_within(started)


@pytest.mark.asyncio
async def test_terminate_ends_a_job_that_ignores_term_after_its_shell_exits(
    tmp_path: Path,
    started: list[int],
) -> None:
    """The shell ends at TERM; its job does not, and the group outlives it."""
    async with Piped(
        ["sh", "-c", "(trap '' TERM; sleep 60) & echo $! > job.pid; wait"],
        cwd=tmp_path,
        terminate_grace_sec=0.05,
    ) as child:
        started.append(await _written_pid(tmp_path / "job.pid"))
        await asyncio.wait_for(child.terminate(), timeout=5)
    assert await _gone_within(started)


@pytest.mark.asyncio
async def test_a_cancel_while_the_child_starts_stops_it_and_its_jobs(
    tmp_path: Path,
    started: list[int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cancel lands before ``Piped`` holds the child, so no one else can stop it."""
    real = asyncio.create_subprocess_exec
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
        process = await real(
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
        await handed.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow)
    starting = asyncio.create_task(
        Piped(["sh", "-c", "sleep 60 & echo $! > job.pid; wait"], cwd=tmp_path).start(),
    )
    started.append(await _written_pid(tmp_path / "job.pid"))
    starting.cancel()
    handed.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(starting, timeout=5)
    assert await _gone_within(started)


@pytest.mark.asyncio
async def test_a_terminate_cancelled_midway_still_ends_the_child(
    tmp_path: Path,
    started: list[int],
) -> None:
    child = Piped(
        ["sh", "-c", "trap '' TERM; echo $$ > shell.pid; sleep 60"],
        cwd=tmp_path,
        terminate_grace_sec=0.05,
    )
    await child.start()
    started.append(await _written_pid(tmp_path / "shell.pid"))
    terminating = asyncio.create_task(child.terminate())
    await asyncio.sleep(0.01)
    terminating.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(terminating, timeout=5)
    assert await _gone_within(started)


@pytest.mark.asyncio
async def test_wait_returns_while_a_job_that_left_the_group_holds_the_pipes(
    tmp_path: Path,
    started: list[int],
) -> None:
    """Before 3.13, ``Process.wait`` also waits for the pipes, here held a minute."""
    async with Piped(
        ["sh", "-c", "setsid sleep 60 & echo $! > job.pid"],
        cwd=tmp_path,
    ) as child:
        started.append(await _written_pid(tmp_path / "job.pid"))
        assert await asyncio.wait_for(child.wait(), timeout=5) == 0


async def _collect(argv: list[str], *, stdin: bytes = b"") -> list[tuple[Stream, str]]:
    """Run a child to completion, returning its chunks as decoded text."""
    async with Piped(argv) as child:
        if stdin:
            _ = await child.write(stdin)
        child.close_stdin()
        return [(name, data.decode()) async for name, data in child.output()]


async def _read_buffered_child(script: str) -> bytes:
    """Release the child after stderr proves it has buffered stdout."""
    # The gate's PYTHONUNBUFFERED must not disable this fixture's buffering.
    async with Piped([sys.executable, "-E", "-c", script]) as child:
        output = child.output()
        # The child is blocked on stdin with stdout still buffered.
        assert await asyncio.wait_for(anext(output), 5.0) == ("stderr", b"ready\n")
        assert await child.write(b"x")
        child.close_stdin()
        return b"".join([data async for name, data in output if name == "stdout"])


class _FailingStdin:
    def __init__(self, error: BaseException) -> None:
        self._error = error

    def write(self, data: bytes) -> None:
        del data

    async def drain(self) -> None:
        raise self._error


class _FakeChild:
    """A started child whose process has the given stdin and no output streams."""

    def __init__(self, *, stdin: object) -> None:
        self.process = SimpleNamespace(stdin=stdin, stdout=None, stderr=None)


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
