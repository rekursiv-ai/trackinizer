"""Tests for attaching a terminal to a hosted child and leaving it running."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import asyncio
import contextlib
import os
import shutil
import sys
import tempfile

import pytest

from trackinizer.lib.posix.attach import (
    DETACH_KEY,
    RemoteTerminal,
    attach,
    stop,
    submit,
)
from trackinizer.lib.posix.host import Host, HostSpec
from trackinizer.lib.posix.terminal import Terminal


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator


_ECHO = (
    "import sys\n"
    "print('early', flush=True)\n"
    "for line in sys.stdin:\n"
    "    print('ECHO:' + line.strip(), flush=True)\n"
    "    if line.strip() == 'quit':\n"
    "        sys.exit(5)\n"
)


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """Yield a directory short enough to hold an AF_UNIX socket.

    Yields:
      directory: A fresh directory, removed afterwards.

    """
    directory = Path(tempfile.mkdtemp(prefix="a"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


class TestAttach:
    """A human's terminal lent to a child some other process hosts."""

    def test_detach_key_leaves_the_child_running(self, short_dir: Path) -> None:
        """Keystrokes before the key arrive, after it do not; the child stays."""

        async def run() -> tuple[int | None, bytes, bytes, bool]:
            async with _hosted(short_dir) as (spec, serving):
                status, mirrored = await _attach_typing(
                    spec,
                    typed=b"hello\n",
                    shown=b"ECHO:hello",
                    then=DETACH_KEY + b"never\n",
                )
                return status, mirrored, spec.scrollback.read_bytes(), serving.done()

        status, mirrored, scrollback, ended = asyncio.run(run())
        assert status is None
        assert b"early" in mirrored
        assert b"never" not in scrollback
        assert not ended

    def test_returns_the_status_when_the_child_exits(self, short_dir: Path) -> None:
        async def run() -> tuple[int | None, bytes]:
            async with _hosted(short_dir) as (spec, _):
                return await _attach_typing(
                    spec,
                    typed=b"quit\n",
                    shown=b"ECHO:quit",
                    then=b"",
                )

        status, mirrored = asyncio.run(run())
        assert status == 5
        assert b"ECHO:quit" in mirrored

    def test_a_second_attach_sees_what_the_first_typed(self, short_dir: Path) -> None:
        """Detaching and reattaching is a resume: the replay carries the past."""

        async def run() -> bytes:
            async with _hosted(short_dir) as (spec, _):
                _ = await _attach_typing(
                    spec,
                    typed=b"first\n",
                    shown=b"ECHO:first",
                    then=DETACH_KEY,
                )
                _, mirrored = await _attach_typing(
                    spec,
                    typed=b"",
                    shown=b"ECHO:first",
                    then=DETACH_KEY,
                )
                return mirrored

        mirrored = asyncio.run(run())
        assert mirrored.count(b"ECHO:first") == 1

    def test_nothing_listening_raises(self, short_dir: Path) -> None:
        with pytest.raises(OSError, match=r"No such file|refused"):
            _ = asyncio.run(attach(short_dir / "absent"))

    def test_remote_terminal_owns_no_pid(self, short_dir: Path) -> None:
        assert RemoteTerminal(short_dir / "s").pid is None


class TestOneShotClients:
    """Messages and stops sent without a terminal."""

    def test_submit_then_stop(self, short_dir: Path) -> None:
        async def run() -> tuple[bool, int | None]:
            async with _hosted(short_dir) as (spec, _):
                submitted = await submit(spec.address, text="quit-not")
                await _wait_for_bytes(spec.scrollback, b"ECHO:quit-not")
                return submitted, await stop(spec.address)

        submitted, status = asyncio.run(run())
        assert submitted
        assert status == 128 + 15

    def test_submit_to_an_exiting_child_reports_failure(
        self,
        short_dir: Path,
    ) -> None:
        async def run() -> bool:
            async with _hosted(short_dir) as (spec, serving):
                assert await submit(spec.address, text="quit")
                _ = await asyncio.wait_for(serving, 10.0)
                with contextlib.suppress(OSError):
                    return await submit(spec.address, text="late")
                return False

        assert not asyncio.run(run())


@contextlib.asynccontextmanager
async def _hosted(
    directory: Path,
) -> AsyncGenerator[tuple[HostSpec, asyncio.Task[int]]]:
    """Host the echo child until the block exits."""
    listening = asyncio.Event()
    spec = HostSpec(
        address=directory / "s",
        scrollback=directory / "scrollback",
        on_listening=listening.set,
    )
    terminal = Terminal(
        [sys.executable, "-u", "-c", _ECHO],
        enter_delay_sec=0,
        bracketed_paste=False,
    )
    serving = asyncio.create_task(Host(terminal, spec=spec).serve())
    try:
        await asyncio.wait_for(listening.wait(), 5.0)
        yield spec, serving
    finally:
        if not serving.done():
            _ = serving.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            _ = await serving


# A person detaches after seeing the screen, not before the replay arrives: the key
# goes in only once ``shown`` has been mirrored, or the attach could end with nothing
# drawn and the test would be timing the race rather than the behavior.
async def _attach_typing(
    spec: HostSpec,
    *,
    typed: bytes,
    shown: bytes,
    then: bytes,
) -> tuple[int | None, bytes]:
    """Attach, type ``typed``, wait to see ``shown``, type ``then``; return result."""
    stdin_r, stdin_w = os.pipe()
    out_r, out_w = os.pipe()
    os.set_blocking(out_r, False)
    mirrored = bytearray()
    attaching = asyncio.create_task(
        attach(
            spec.address,
            stdin=os.fdopen(stdin_r),
            stdout=os.fdopen(out_w, "wb", buffering=0),
        ),
    )
    try:
        if typed:
            _ = os.write(stdin_w, typed)
        async with asyncio.timeout(5.0):
            while shown not in mirrored:
                mirrored += _read_available(out_r)
                await asyncio.sleep(0.01)
        _ = os.write(stdin_w, then)
        status = await asyncio.wait_for(attaching, 5.0)
        mirrored += _read_available(out_r)
    finally:
        os.close(stdin_w)
        os.close(out_r)
    return status, bytes(mirrored)


def _read_available(fd: int) -> bytes:
    """Return whatever a non-blocking pipe holds right now."""
    try:
        return os.read(fd, 65_536)
    except BlockingIOError:
        return b""


async def _wait_for_bytes(path: Path, needle: bytes) -> None:
    """Wait until ``path`` holds ``needle``, failing the test after 5s."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    while loop.time() < deadline:
        if needle in _read(path):
            return
        await asyncio.sleep(0.01)
    raise TimeoutError(f"{needle!r} not in {path} within 5s")


def _read(path: Path) -> bytes:
    """Return ``path``'s bytes, or nothing before it exists."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
