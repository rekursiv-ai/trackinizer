"""Run a child on pipes, keeping its two output streams apart.

The sibling of :class:`~trackinizer.lib.posix.terminal.Terminal`, and the trade it
makes is the opposite one. A pty gives the child ONE terminal, so its stdout
and stderr are interleaved by the kernel before any reader sees a byte -- what
that buys is a tty, which is what a TUI needs and what makes injected keystrokes
indistinguishable from typed ones. Pipes give three real descriptors, so a
reader can say which stream a line crossed, and lose the tty.

Measured on a child printing three lines then one to stderr:

* pty: lines arrive at 0.01s, 0.31s, 0.61s -- libc line-buffers on a tty --
  and the stderr line is indistinguishable from the others.
* pipe: nothing until 0.91s, when the child exits and libc flushes its block
  buffer, but the stderr line comes back on its own stream.

That buffering is the CHILD's, not this class's: a program deciding to hold its
output until exit is invisible from the parent side, and the only fixes are the
child's own (``python -u``, ``stdbuf -oL``). A child killed before it flushes
loses whatever it was holding.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Self

import asyncio
import contextlib
import os
import shutil
import signal


__all__ = ["Piped", "Stream"]


type Stream = str
"""Which descriptor a chunk crossed: ``"stdout"`` or ``"stderr"``."""


class Piped:
    """Spawn a child on pipes and read its streams separately.

    Args:
      argv: The child command and its arguments.
      cwd: Directory to run the child in; the caller's when None.
      env: Extra environment for the child.
      terminate_grace_sec: How long the child gets to honor TERM before its
        whole process group is killed.

    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        terminate_grace_sec: float = 1.0,
    ) -> None:
        self._argv = list(argv)
        self._cwd = cwd
        self._env = dict(env or {})
        self._terminate_grace_sec = terminate_grace_sec
        self._process: asyncio.subprocess.Process | None = None

    async def __aenter__(self) -> Self:
        """Spawn the child and return the handle driving it.

        Returns:
          piped: This object, with its child running.

        Raises:
          FileNotFoundError: ``argv[0]`` is not on PATH.

        """
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Stop the child and reap it."""
        del exc
        await self.terminate()
        _ = await self.wait()

    async def start(self) -> None:
        """Spawn the child with all three streams piped.

        Raises:
          FileNotFoundError: ``argv[0]`` is not on PATH.

        """
        binary = shutil.which(self._argv[0])
        if binary is None:
            raise FileNotFoundError(self._argv[0])
        self._process = await asyncio.create_subprocess_exec(
            binary,
            *self._argv[1:],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env={**os.environ, **self._env} if self._env else None,
            # Its own process group, so terminate reaches a child that spawned
            # children of its own -- a shell wrapper leaves orphans otherwise.
            start_new_session=True,
        )

    async def write(self, data: bytes) -> bool:
        """Send raw bytes to the child's stdin; False once it is gone.

        Args:
          data: Bytes to send.

        Returns:
          written: Whether the bytes reached the child.

        """
        process = self._process
        if process is None or process.stdin is None:
            return False
        try:
            process.stdin.write(data)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            return False
        return True

    async def output(self) -> AsyncIterator[tuple[Stream, bytes]]:
        """Yield the child's output as it arrives, tagged by stream.

        Both streams are read CONCURRENTLY, not one after the other: a child
        that fills the stderr pipe buffer while the reader is draining stdout
        blocks forever on a write nobody is consuming, which is the classic
        deadlock this shape exists to avoid.

        Yields:
          chunk: The stream a chunk crossed, and its bytes.

        """
        process = self._process
        if process is None:
            return
        chunks: asyncio.Queue[tuple[Stream, bytes] | None] = asyncio.Queue()
        readers = [
            asyncio.create_task(_pump(name, reader, chunks))
            for name, reader in (
                ("stdout", process.stdout),
                ("stderr", process.stderr),
            )
            if reader is not None
        ]
        if not readers:
            return
        try:
            live = len(readers)
            while live:
                item = await chunks.get()
                if item is None:
                    live -= 1
                    continue
                yield item
        finally:
            for reader in readers:
                _ = reader.cancel()
            for reader in readers:
                with contextlib.suppress(asyncio.CancelledError):
                    await reader

    async def terminate(self) -> None:
        """Stop the child's process group; safe to call repeatedly."""
        process = self._process
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        with contextlib.suppress(TimeoutError):
            _ = await asyncio.wait_for(
                process.wait(), timeout=self._terminate_grace_sec
            )
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)

    async def wait(self) -> int:
        """Reap the child and return its exit status."""
        process = self._process
        if process is None:
            return 0
        return await process.wait()

    def close_stdin(self) -> None:
        """Close the child's stdin, so a reader-until-EOF child can finish."""
        process = self._process
        if process is None or process.stdin is None:
            return
        with contextlib.suppress(BrokenPipeError, RuntimeError):
            process.stdin.close()


async def _pump(
    name: Stream,
    reader: asyncio.StreamReader,
    chunks: asyncio.Queue[tuple[Stream, bytes] | None],
) -> None:
    """Forward one stream's chunks onto the shared queue, then mark it done."""
    try:
        while True:
            data = await reader.read(65_536)
            if not data:
                return
            await chunks.put((name, data))
    finally:
        await chunks.put(None)
