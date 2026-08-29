"""Tests for relaying a human's terminal to a child on a pseudo-terminal."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import cast

import asyncio
import contextlib
import fcntl
import os
import pty
import signal
import struct
import sys
import termios
import threading
import time

import pytest

from trackinizer.lib.posix.relay import Relay, ThreadedRelay, real_fd, terminal_size
from trackinizer.lib.posix.terminal import Terminal


class TestRealFd:
    """Descriptor resolution, which decides whether a direction runs at all."""

    def test_returns_a_real_descriptor(self) -> None:
        read_fd, write_fd = os.pipe()
        try:
            assert real_fd(os.fdopen(read_fd)) == read_fd
        finally:
            with contextlib.suppress(OSError):
                os.close(write_fd)

    def test_returns_minus_one_without_one(self) -> None:
        assert real_fd(_NoFileno()) == -1


class _NoFileno:
    """A stream whose ``fileno`` raises, as pytest's capture does."""

    def fileno(self) -> int:
        raise ValueError("no real descriptor")


class TestTerminalSize:
    """The geometry a child's pty inherits."""

    def test_reads_a_real_terminal(self) -> None:
        master, slave = pty.openpty()
        try:
            _ = fcntl.ioctl(
                slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0)
            )
            assert terminal_size(slave) == (40, 120)
        finally:
            os.close(master)
            os.close(slave)

    def test_falls_back_to_a_usable_geometry(self) -> None:
        """A 0x0 pty breaks child TUIs, so the fallback cannot be zeros."""
        read_fd, write_fd = os.pipe()
        try:
            rows, cols = terminal_size(read_fd)
        finally:
            os.close(read_fd)
            os.close(write_fd)
        assert rows > 0
        assert cols > 0


class TestRelayRoundTrip:
    """A real child on a real pty: keystrokes down, painting up."""

    def test_input_reaches_the_child_and_output_mirrors_back(self) -> None:
        child = (
            "import sys; line = sys.stdin.readline(); "
            "sys.stdout.write('GOT:' + line); sys.stdout.flush()"
        )
        seen, status = _drive(
            [sys.executable, "-c", child], typed=b"hello\n", needle=b"GOT:hello"
        )
        assert b"GOT:hello" in seen
        assert status == 0

    def test_child_exit_status_is_returned(self) -> None:
        seen, status = _drive(["sh", "-c", "exit 5"], typed=b"", needle=b"")
        del seen
        assert status == 5

    def test_on_input_sees_keystrokes_but_not_submissions(self) -> None:
        """The tee observes only what the human typed, never a spliced message.

        Slash-command capture hangs off this: the detector must see a typed
        ``/exit`` and must not mistake an injected message for a command.
        """
        child = (
            "import sys\n"
            "while True:\n"
            "    line = sys.stdin.readline()\n"
            "    if not line: break\n"
            "    sys.stdout.write('ECHO:' + line); sys.stdout.flush()\n"
        )
        observed: list[bytes] = []

        async def run() -> None:
            stdin_r, stdin_w = os.pipe()
            out_r, out_w = os.pipe()
            terminal = Terminal([sys.executable, "-u", "-c", child])
            relay = Relay(
                terminal,
                stdin=os.fdopen(stdin_r),
                stdout=os.fdopen(out_w, "wb", buffering=0),
                on_input=observed.append,
            )
            serving = asyncio.create_task(relay.serve())
            try:
                _ = os.write(stdin_w, b"/exit\n")
                await _wait_for(lambda: b"/exit" in b"".join(observed), 5.0)
                _ = await terminal.submit("spliced text")
                await asyncio.sleep(0.2)
            finally:
                await terminal.terminate()
                with contextlib.suppress(asyncio.TimeoutError):
                    _ = await asyncio.wait_for(serving, 5.0)
                for fd in (stdin_w, out_r):
                    with contextlib.suppress(OSError):
                        os.close(fd)

        asyncio.run(run())
        joined = b"".join(observed)
        assert b"/exit" in joined
        assert b"spliced text" not in joined

    def test_submission_reaches_the_child_alongside_keystrokes(self) -> None:
        """A spliced message and typed input are peers on the same master."""
        child = (
            "import sys\n"
            "while True:\n"
            "    line = sys.stdin.readline()\n"
            "    if not line: break\n"
            "    sys.stdout.write('ECHO:' + line); sys.stdout.flush()\n"
        )
        captured = bytearray()

        async def run() -> None:
            stdin_r, stdin_w = os.pipe()
            out_r, out_w = os.pipe()
            terminal = Terminal([sys.executable, "-u", "-c", child])
            relay = Relay(
                terminal,
                stdin=os.fdopen(stdin_r),
                stdout=os.fdopen(out_w, "wb", buffering=0),
            )
            serving = asyncio.create_task(relay.serve())
            reading = asyncio.create_task(_drain(out_r, captured))
            try:
                await asyncio.sleep(0.5)
                _ = await terminal.submit("run it")
                await _wait_for(lambda: b"run it" in bytes(captured), 10.0)
            finally:
                await terminal.terminate()
                with contextlib.suppress(asyncio.TimeoutError):
                    _ = await asyncio.wait_for(serving, 5.0)
                _ = reading.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reading
                for fd in (stdin_w, out_r):
                    with contextlib.suppress(OSError):
                        os.close(fd)

        asyncio.run(run())
        assert b"run it" in bytes(captured)


class TestTerminalHandback:
    r"""The terminal is returned in the state it was borrowed in.

    A child that dies mid-TUI leaves DEC private modes on: mouse tracking,
    focus reporting, the alternate screen. ``tcsetattr`` restores the line
    discipline only, so without an explicit reset every focus change or mouse
    motion prints as escape text in the human's shell afterwards.
    """

    def test_modes_the_child_left_on_are_disabled(self) -> None:
        # The child enables any-motion mouse reporting, SGR encoding, focus
        # reporting and the alternate screen, then exits without disabling
        # them -- the crash shape.
        child = (
            "import sys; "
            "sys.stdout.write('\\x1b[?1049h\\x1b[?1003h\\x1b[?1006h\\x1b[?1004h'); "
            "sys.stdout.flush()"
        )
        captured = bytearray()

        async def run() -> int:
            # A real pty as the human's terminal, so ``os.isatty`` holds and
            # the epilogue takes the same path it does for a live user.
            term_master, term_slave = pty.openpty()
            reading = asyncio.create_task(_drain(term_master, captured))
            try:
                relay = Relay(
                    Terminal([sys.executable, "-c", child]),
                    stdin=os.fdopen(os.dup(term_slave), "r"),
                    stdout=os.fdopen(os.dup(term_slave), "wb", buffering=0),
                )
                status = await relay.serve()
                await _wait_for(lambda: b"\x1b[?1003l" in bytes(captured), 5.0)
                return status
            finally:
                _ = reading.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reading
                for fd in (term_master, term_slave):
                    with contextlib.suppress(OSError):
                        os.close(fd)

        assert asyncio.run(run()) == 0
        seen = bytes(captured)
        # The child's enables mirror through (byte-transparency)...
        assert b"\x1b[?1003h" in seen
        # ...and every mode it left on is disabled before the relay returns.
        for disable in (
            b"\x1b[?1004l",  # focus reporting: the reported ``\x1b[I`` symptom
            b"\x1b[?1003l",  # any-motion mouse reporting
            b"\x1b[?1006l",  # SGR mouse encoding
            b"\x1b[?1049l",  # alternate screen
            b"\x1b[?2004l",  # bracketed paste
            b"\x1b[?25h",  # cursor visible
        ):
            assert disable in seen

    def test_line_discipline_is_restored(self) -> None:
        """Raw mode is undone, so the shell afterwards still echoes and canons."""

        async def run() -> list[object]:
            term_master, term_slave = pty.openpty()
            captured = bytearray()
            reading = asyncio.create_task(_drain(term_master, captured))
            try:
                before = termios.tcgetattr(term_slave)
                relay = Relay(
                    Terminal(["true"]),
                    stdin=os.fdopen(os.dup(term_slave), "r"),
                    stdout=os.fdopen(os.dup(term_slave), "wb", buffering=0),
                )
                _ = await relay.serve()
                after = termios.tcgetattr(term_slave)
                return [before, after]
            finally:
                _ = reading.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reading
                for fd in (term_master, term_slave):
                    with contextlib.suppress(OSError):
                        os.close(fd)

        before, after = asyncio.run(run())
        assert before == after

    def test_a_child_that_cannot_spawn_restores_nothing_and_raises(self) -> None:
        """A missing binary fails before the terminal is ever borrowed."""

        async def run() -> None:
            _ = await Relay(Terminal(["definitely-not-a-real-binary-xyz"])).serve()

        with pytest.raises(FileNotFoundError):
            asyncio.run(run())


class TestOnOutput:
    """The child's stream as a session source, for a binary with no log."""

    def test_sees_every_byte_the_child_emits(self) -> None:
        seen: list[bytes] = []

        async def run() -> int:
            relay = Relay(
                Terminal(["sh", "-c", "echo captured-line"]),
                stdin=_NoFileno(),
                stdout=_NoFileno(),
                on_output=seen.append,
            )
            return await relay.serve()

        assert asyncio.run(run()) == 0
        assert b"captured-line" in b"".join(seen)

    def test_sees_output_even_with_no_mirror_destination(self) -> None:
        """A captured stdout must not cost the observer its bytes.

        The tee runs before the mirror precisely so a harness whose stdout has
        no real descriptor still feeds capture.
        """
        seen: list[bytes] = []

        async def run() -> None:
            relay = Relay(
                Terminal(["sh", "-c", "echo only-to-observer"]),
                stdin=_NoFileno(),
                stdout=_NoFileno(),
                on_output=seen.append,
            )
            _ = await relay.serve()

        asyncio.run(run())
        assert b"only-to-observer" in b"".join(seen)


class TestSignalHandling:
    """Window resizes reach the child; a relay TERM tears it down."""

    def test_resize_forwards_the_new_geometry(self) -> None:
        """A SIGWINCH on the relay resizes the child's pty.

        Asserted through the handler the relay installs, not by raising the
        signal: pytest runs the loop on the main thread, and a raised SIGWINCH
        would race the test's own assertion.
        """
        master, slave = pty.openpty()

        async def run() -> tuple[int, int]:
            terminal = Terminal(["cat"])
            relay = Relay(terminal, stdin=os.fdopen(os.dup(slave), "r"))
            await terminal.start()
            try:
                _ = fcntl.ioctl(
                    slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0)
                )
                relay._resize(slave)
                packed = fcntl.ioctl(
                    terminal.master_fd,
                    termios.TIOCGWINSZ,
                    struct.pack("HHHH", 0, 0, 0, 0),
                )
                rows, cols, _, _ = struct.unpack("HHHH", packed)
                return (rows, cols)
            finally:
                await terminal.terminate()
                _ = await terminal.wait()
                await terminal.close()

        try:
            assert asyncio.run(run()) == (40, 120)
        finally:
            for fd in (master, slave):
                with contextlib.suppress(OSError):
                    os.close(fd)

    def test_resize_without_a_real_stdin_is_harmless(self) -> None:
        async def run() -> None:
            terminal = Terminal(["cat"])
            relay = Relay(terminal)
            await terminal.start()
            try:
                relay._resize(-1)
            finally:
                await terminal.terminate()
                _ = await terminal.wait()
                await terminal.close()

        asyncio.run(run())

    def test_a_relay_signal_becomes_the_reported_status(self) -> None:
        """A TERM to the relay ends the child and surfaces as 128+signal.

        A caller exiting with this reports what a directly-run child would
        have, rather than the status of a child it killed itself.
        """
        deaf = (
            "import signal,time; "
            "signal.signal(signal.SIGTERM, lambda *_: None); "
            "time.sleep(30)"
        )

        async def run() -> int:
            terminal = Terminal([sys.executable, "-c", deaf], terminate_grace_sec=0.2)
            relay = Relay(terminal, stdin=_NoFileno(), stdout=_NoFileno())
            serving = asyncio.create_task(relay.serve())
            await asyncio.sleep(0.3)
            relay._interrupt(signal.SIGTERM)
            return await asyncio.wait_for(serving, 10.0)

        assert asyncio.run(run()) == 128 + signal.SIGTERM

    def test_handlers_are_skipped_off_the_main_thread(self) -> None:
        """A relay driven from a worker goes without signals, not crashing.

        ``add_signal_handler`` is main-thread only, so a caller running the
        relay on a worker (a test harness, a supervisor) must still work.
        """
        installed: list[tuple[int, ...]] = []

        def drive() -> None:
            async def run() -> None:
                terminal = Terminal(["cat"])
                relay = Relay(terminal)
                await terminal.start()
                try:
                    installed.append(relay._install_handlers(-1))
                finally:
                    await terminal.terminate()
                    _ = await terminal.wait()
                    await terminal.close()

            asyncio.run(run())

        worker = threading.Thread(target=drive)
        worker.start()
        worker.join(timeout=10.0)
        assert installed == [()]


class TestThreadedRelay:
    """The relay as a synchronous caller sees it: run blocks, submit crosses."""

    def test_run_returns_the_childs_status(self) -> None:
        assert ThreadedRelay(["sh", "-c", "exit 5"]).run() == 5

    def test_missing_binary_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            _ = ThreadedRelay(["definitely-not-a-real-binary-xyz"]).run()

    def test_submit_reaches_the_child_from_another_thread(self) -> None:
        """A background caller types into a live child and it echoes back."""
        child = (
            "import sys\n"
            "while True:\n"
            "    line = sys.stdin.readline()\n"
            "    if not line: break\n"
            "    sys.stdout.write('ECHO:' + line); sys.stdout.flush()\n"
        )
        captured = bytearray()
        relay = ThreadedRelay(
            [sys.executable, "-u", "-c", child],
            bracketed_paste=False,
            on_output=captured.extend,
        )
        worker = threading.Thread(target=relay.run, daemon=True)
        worker.start()
        try:
            _wait_running(relay, 5.0)
            relay.submit("from another thread")
            _wait(lambda: b"from another thread" in bytes(captured), 10.0)
            assert relay.submitted == 1
        finally:
            relay.terminate()
            worker.join(timeout=5.0)
        assert b"from another thread" in bytes(captured)

    def test_submit_before_the_child_is_up_is_dropped(self) -> None:
        """A submission with no live child is a no-op, not a crash."""
        relay = ThreadedRelay(["cat"])
        relay.submit("nobody is listening")
        assert relay.submitted == 0

    def test_terminate_before_run_is_a_noop(self) -> None:
        ThreadedRelay(["cat"]).terminate()

    def test_terminate_after_exit_is_a_noop(self) -> None:
        """Once the child is reaped, a late terminate signals nothing."""
        relay = ThreadedRelay(["true"])
        assert relay.run() == 0
        relay.terminate()

    def test_terminate_ends_a_child_deaf_to_term(self) -> None:
        deaf = (
            "import signal,time; "
            "signal.signal(signal.SIGTERM, lambda *_: None); "
            "time.sleep(30)"
        )
        relay = ThreadedRelay([sys.executable, "-c", deaf])
        status: list[int] = []

        def drive() -> None:
            status.append(relay.run())

        worker = threading.Thread(target=drive, daemon=True)
        worker.start()
        _wait_running(relay, 5.0)
        time.sleep(0.3)  # let the child install its handler
        relay.terminate()
        worker.join(timeout=10.0)
        assert not worker.is_alive()
        assert status == [128 + signal.SIGKILL]


class TestRelayWithoutRealStreams:
    """A relay whose own stdio is captured still drives the child."""

    def test_runs_with_no_real_descriptors(self, tmp_path: Path) -> None:
        """Neither direction has a real fd; the child still runs and is reaped."""
        marker = tmp_path / "ran.txt"
        child = f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ran')"

        async def run() -> int:
            relay = Relay(
                Terminal([sys.executable, "-c", child]),
                stdin=_NoFileno(),
                stdout=_NoFileno(),
            )
            return await relay.serve()

        assert asyncio.run(run()) == 0
        assert marker.read_text() == "ran"


def _drive(argv: list[str], *, typed: bytes, needle: bytes) -> tuple[bytes, int]:
    """Run one relay against ``argv``, typing ``typed``; return output + status."""
    captured = bytearray()

    async def run() -> int:
        stdin_r, stdin_w = os.pipe()
        out_r, out_w = os.pipe()
        relay = Relay(
            Terminal(argv),
            stdin=os.fdopen(stdin_r),
            stdout=os.fdopen(out_w, "wb", buffering=0),
        )
        serving = asyncio.create_task(relay.serve())
        reading = asyncio.create_task(_drain(out_r, captured))
        try:
            if typed:
                _ = os.write(stdin_w, typed)
            if needle:
                await _wait_for(lambda: needle in bytes(captured), 10.0)
            return await asyncio.wait_for(serving, 15.0)
        finally:
            _ = reading.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reading
            for fd in (stdin_w, out_r):
                with contextlib.suppress(OSError):
                    os.close(fd)

    status = asyncio.run(run())
    return bytes(captured), status


async def _drain(fd: int, into: bytearray) -> None:
    """Accumulate everything ``fd`` produces into ``into``."""
    loop = asyncio.get_running_loop()
    while True:
        ready = cast(asyncio.Future[None], loop.create_future())
        loop.add_reader(fd, lambda f=ready: f.done() or f.set_result(None))
        try:
            await ready
        finally:
            with contextlib.suppress(ValueError, OSError):
                loop.remove_reader(fd)
        try:
            chunk = os.read(fd, 65_536)
        except OSError:
            return
        if not chunk:
            return
        into.extend(chunk)


def _wait(predicate: Callable[[], bool], timeout_sec: float) -> None:
    """Block until ``predicate`` holds or the deadline passes."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)


def _wait_running(relay: ThreadedRelay, timeout_sec: float) -> None:
    """Block until ``relay`` has a live child to submit to."""
    _wait(relay._running.is_set, timeout_sec)


async def _wait_for(predicate: Callable[[], bool], timeout_sec: float) -> None:
    """Poll ``predicate`` until it holds or the deadline passes."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_sec
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
