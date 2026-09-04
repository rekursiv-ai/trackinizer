"""A piped child keeps its two output streams apart; a pty cannot.

That separation is the whole reason this exists beside
:class:`~trackinizer.lib.posix.terminal.Terminal`, so it is what these tests pin --
along with the deadlock a naive one-stream-at-a-time reader would hit.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from trackinizer.lib.posix.pipes import Piped, Stream


def _collect(argv: list[str], *, stdin: bytes = b"") -> list[tuple[Stream, str]]:
    """Run a child to completion, returning its chunks as decoded text."""

    async def run() -> list[tuple[Stream, str]]:
        async with Piped(argv) as child:
            if stdin:
                _ = await child.write(stdin)
            child.close_stdin()
            return [(name, data.decode()) async for name, data in child.output()]

    return asyncio.run(run())


def test_stdout_and_stderr_arrive_on_their_own_streams() -> None:
    """The separation a pty structurally cannot provide.

    On a pty both streams are the same slave tty, so the kernel interleaves
    them before any reader sees a byte and no later step can undo it.
    """
    chunks = _collect(
        ["sh", "-c", "printf 'to-out\\n'; printf 'to-err\\n' >&2"],
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

    chunks = _collect(["sh", "-c", script])

    assert "".join(t for n, t in chunks if n == "stdout") == "done\n"
    assert len("".join(t for n, t in chunks if n == "stderr")) == 70_000


def test_input_reaches_the_child() -> None:
    """Bytes written to stdin come back out, which is what makes it a session."""
    chunks = _collect(["sh", "-c", "cat"], stdin=b"echoed\n")

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
    script = "import time; print('held'); time.sleep(0.2)"

    chunks = _collect([sys.executable, "-c", script])

    # It does arrive -- at exit, all at once, rather than when printed.
    assert "".join(t for n, t in chunks if n == "stdout") == "held\n"


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
