"""Run a child process on a pseudo-terminal we own.

A TUI reaches things no command line does: a slash command is handled inside
the program and never becomes an argument, so ``/model`` and ``/compact`` are
expressible only to a terminal. Owning the master fd is what lets a caller
type into one.

Two kinds of child read from that terminal, and they need opposite protocols:

- A **TUI** gets a **bracketed paste** so it treats the text as one atomic
  block rather than racing whatever else is typing, then Enter as a
  **separate write** delayed past codex's 120ms paste-Enter suppression
  window, or the Enter is absorbed into the paste and never submits.
- A **line reader** gets one plain newline-terminated write. The paste
  sentinels would arrive as literal bytes in its ``read``, and it also needs
  the line discipline silenced -- see :meth:`Terminal.silence_line_discipline`.

This is the child half. Attaching a human's own terminal to one of these --
raw mode, window-size forwarding, mirroring bytes back -- is
:mod:`trackinizer.lib.posix.relay`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Final, Self

import asyncio
import contextlib
import fcntl
import os
import pty
import shutil
import signal
import struct
import termios


__all__ = [
    "ESSENTIAL_ENV",
    "PASTE_END",
    "PASTE_START",
    "SUBMIT",
    "TERMINAL_RESET",
    "Terminal",
    "encode_paste",
    "reset_terminal_modes",
    "write_all",
]


PASTE_START: Final = b"\x1b[200~"
PASTE_END: Final = b"\x1b[201~"
"""Bracketed-paste bookends; the TUI buffers everything between as one block."""

SUBMIT: Final = b"\r"
"""What a terminal sends for Enter."""

ESSENTIAL_ENV: Final = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "TZ",
    "LANG",
)
"""Kept even under ``clean_env``: without these a child cannot run at all.

No ``PATH`` and it cannot exec; no ``HOME`` and every library writing a
dotfile picks a different wrong answer.
"""

# Written to a real terminal once the child is gone. These are DECSET private
# modes the child turned on by emitting escape sequences; they live in the
# terminal emulator, not the line discipline, so ``tcsetattr`` cannot undo
# them. A child that exits without its own epilogue -- a crash, or the KILL in
# :meth:`Terminal.terminate` -- leaves them set in the human's shell: focus
# reporting (``?1004``) then prints ``\x1b[I`` on every window focus and mouse
# reporting prints a burst per motion event. Ordered outermost-first and
# idempotent, so sending them when the child already cleaned up is a no-op.
TERMINAL_RESET: Final = b"".join(
    (
        b"\x1b[?1049l",  # leave alternate screen
        b"\x1b[?1000l\x1b[?1002l\x1b[?1003l",  # mouse: click, drag, any-motion
        b"\x1b[?1005l\x1b[?1006l\x1b[?1015l",  # mouse encodings: utf8, SGR, urxvt
        b"\x1b[?1004l",  # focus in/out reporting
        b"\x1b[?2004l",  # bracketed paste
        b"\x1b[?1l\x1b>",  # normal cursor keys, numeric keypad
        b"\x1b[?7h",  # autowrap back on
        b"\x1b[?25h",  # cursor visible
        b"\x1b[0m",  # default colors and attributes
    )
)


def encode_paste(text: str) -> bytes:
    r"""Encode ``text`` as one atomic bracketed-paste block, without Enter.

    The Enter is deliberately separate -- :meth:`Terminal.submit` sends it
    after a delay -- so the TUI does not absorb it into the paste burst. The
    payload's own sentinels are stripped first: a ``\x1b[201~`` inside would
    close the bracket early and turn the remainder into live keystrokes
    outside the atomic paste, which is the only thing keeping a submission
    from interleaving with whatever else is typing.

    Args:
      text: What to type.

    Returns:
      block: The bookended bytes, with exactly one sentinel at each end.

    """
    body = text.encode().replace(PASTE_END, b"").replace(PASTE_START, b"")
    return PASTE_START + body + PASTE_END


def write_all(fd: int, data: bytes) -> bool:
    r"""Write every byte of ``data`` to a blocking ``fd``, looping short writes.

    A bare ``os.write`` may write fewer bytes than given (a full pipe, a slow
    consumer); dropping the unwritten tail corrupts whatever framing the bytes
    carry -- a bracketed paste loses its ``\x1b[201~`` and wedges the TUI, and
    a mirrored child chunk arrives truncated.

    Args:
      fd: Destination descriptor.
      data: Bytes to write.

    Returns:
      written: Whether every byte landed; False once the descriptor died.

    """
    view = memoryview(data)
    while view:
        try:
            count = os.write(fd, view)
        except OSError:
            return False
        if count == 0:
            # A blocking fd never returns 0 for a non-empty write under normal
            # conditions; a 0 means it has stopped accepting bytes. Bail rather
            # than spin forever on an unchanged view.
            return False
        view = view[count:]
    return True


def reset_terminal_modes(fd: int) -> bool:
    """Disable DECSET modes a child may have left on; True if written.

    Only for a real terminal: a redirected or piped ``fd`` would collect the
    escape bytes as literal junk in a file someone later reads. A closed or
    dying descriptor is not an error here -- the caller is on its way out.

    Args:
      fd: Descriptor of the terminal to restore.

    Returns:
      written: Whether the reset was sent.

    """
    if fd < 0 or not os.isatty(fd):
        return False
    return write_all(fd, TERMINAL_RESET)


class Terminal:
    """A child process on a pseudo-terminal, driven as a conversation.

    Args:
      argv: Command and arguments to run.
      cwd: Directory to run in; the caller's when None. A ``PWD`` in ``env``
        is not a substitute -- that is a shell convention, and a program
        asking the kernel where it is gets the inherited directory.
      env: Extra environment for the child, merged over the inherited one --
        or the WHOLE environment when ``clean_env`` is set.
      clean_env: Whether the child starts from an empty environment. Off, the
        child inherits the caller's, which makes a run depend on the operator's
        shell. On, it sees only ``env`` plus the few variables a process cannot
        work without (see :data:`ESSENTIAL_ENV`).
      enter_delay_sec: Gap between a paste and its Enter. Must exceed codex's
        120ms paste-Enter suppression window or the Enter is swallowed and the
        line never submits.
      terminate_grace_sec: How long the child gets to honor TERM before its
        whole process group is killed.
      winsize: Rows and columns given to the pty. A 0x0 terminal makes TUIs
        render one character per line and breaks their input handling.
      bracketed_paste: Which submission protocol the child reads. True for a
        TUI. False for a plain line reader, which also gets its line
        discipline silenced -- see :meth:`silence_line_discipline`.

    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        clean_env: bool = False,
        enter_delay_sec: float = 0.15,
        terminate_grace_sec: float = 1.0,
        winsize: tuple[int, int] = (24, 80),
        bracketed_paste: bool = True,
    ) -> None:
        self._argv = list(argv)
        self._cwd = cwd
        self._env = dict(env or {})
        self._clean_env = clean_env
        self._enter_delay_sec = enter_delay_sec
        self._terminate_grace_sec = terminate_grace_sec
        self._winsize = winsize
        self._bracketed_paste = bracketed_paste
        self._master_fd = -1
        self._pid = -1
        self._status: int | None = None
        self._submitted = 0
        # Two locks, not one. ``_submit_lock`` spans a whole submission (paste,
        # delay, Enter) so two of them never interleave into one merged submit.
        # ``_write_lock`` covers only each individual write, so a peer writing
        # to the same master -- a human's keystroke relayed in -- waits at most
        # one write rather than the full Enter delay.
        self._submit_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    async def __aenter__(self) -> Self:
        """Spawn the child and return the terminal driving it.

        Returns:
          terminal: This terminal, with its child running.

        Raises:
          FileNotFoundError: ``argv[0]`` is not on PATH.

        """
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Stop the child and release the terminal."""
        del exc
        await self.terminate()
        _ = await self.wait()
        await self.close()

    @property
    def master_fd(self) -> int:
        """Descriptor of the pty master, or -1 once released."""
        return self._master_fd

    @property
    def submitted(self) -> int:
        """How many messages have been typed in and submitted."""
        return self._submitted

    async def start(self) -> None:
        """Spawn the child on a fresh pty.

        Resolves the binary before forking: a failed ``execvp`` after the fork
        cannot become an exception in the parent, so it would surface as a
        mystery exit code instead of naming the missing command.

        Raises:
          FileNotFoundError: ``argv[0]`` is not on PATH.

        """
        binary = shutil.which(self._argv[0])
        if binary is None:
            raise FileNotFoundError(self._argv[0])
        self._pid, self._master_fd = pty.fork()
        if self._pid == 0:
            if self._clean_env:
                kept = {
                    name: os.environ[name]
                    for name in ESSENTIAL_ENV
                    if name in os.environ
                }
                os.environ.clear()
                os.environ.update(kept)
            # A TUI on a pty needs ``TERM``; a non-tty parent environment may
            # lack it, which degrades rendering and input handling.
            os.environ.setdefault("TERM", "xterm-256color")
            os.environ.update(self._env)
            try:
                if self._cwd is not None:
                    # An actual chdir, not just ``PWD``: a program that asks
                    # the kernel where it is -- as an agent CLI does to file
                    # its session -- gets this, never the environment variable.
                    os.chdir(self._cwd)
                os.execv(binary, self._argv)  # noqa: S606 -- the point of this class.
            except OSError:
                # Never fall through into the parent's code: two processes
                # would then share the master fd.
                os._exit(127)
        self.set_winsize(*self._winsize)
        if not self._bracketed_paste:
            self.silence_line_discipline()
        os.set_blocking(self._master_fd, False)

    def silence_line_discipline(self) -> None:
        """Clear ``ECHO`` and ``ICANON`` on the pty; tolerate a dead master.

        Only for a plain line reader, and it must be re-asserted rather than
        set once: the termios state is shared with the child, so anything it
        runs (``stty echo``, a curses init) restores echo, after which every
        submitted byte echoes back through the master and is captured as the
        child's own output.

        The canonical editor is the other half. It silently discards an input
        line past ``MAX_CANON`` (~1024 bytes) and corrupts the line after it,
        so a long submission would vanish with no error anywhere.
        """
        if self._master_fd < 0:
            return
        with contextlib.suppress(termios.error):
            attr = termios.tcgetattr(self._master_fd)
            attr[3] &= ~(termios.ECHO | termios.ICANON)
            termios.tcsetattr(self._master_fd, termios.TCSANOW, attr)

    def set_winsize(self, rows: int, cols: int) -> None:
        """Resize the pty, so the child's TUI redraws for the new geometry.

        Failure is not fatal -- the child keeps running on whatever geometry it
        already had -- because a resize race against a closing pty is routine.

        Args:
          rows: Terminal height.
          cols: Terminal width.

        """
        if self._master_fd < 0:
            return
        self._winsize = (rows, cols)
        with contextlib.suppress(OSError):
            _ = fcntl.ioctl(
                self._master_fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0),
            )

    async def write(self, data: bytes) -> bool:
        """Send raw bytes to the child; False once it is gone.

        For a keystroke a TUI reads directly -- Escape to stop a turn, or a
        human's relayed input -- where :meth:`submit` would wrap it in a paste
        and append Enter.

        Args:
          data: Bytes to send.

        Returns:
          written: Whether every byte reached the child.

        """
        async with self._write_lock:
            return await self._write(data)

    async def submit(self, text: str) -> bool:
        """Type ``text`` into the child and press Enter.

        Args:
          text: What to type.

        Returns:
          submitted: Whether the text reached the child; False once it is
            gone, in which case no Enter is sent.

        """
        async with self._submit_lock:
            if not self._bracketed_paste:
                return await self._submit_line(text)
            async with self._write_lock:
                if not await self._write(encode_paste(text)):
                    return False
            # Its own write, after the delay: folded into the paste burst, the
            # Enter is suppressed and the line never submits. The delay is
            # deliberately OUTSIDE ``_write_lock`` -- holding that across the
            # sleep stalls every peer keystroke for the full delay.
            await asyncio.sleep(self._enter_delay_sec)
            async with self._write_lock:
                _ = await self._write(SUBMIT)
            self._submitted += 1
            return True

    async def _submit_line(self, text: str) -> bool:
        """Send one plain newline-terminated line to a line-reading child.

        No paste bracket (its sentinels would be literal bytes to a ``read``)
        and no Enter delay to outwait. Interior newlines become spaces so that
        ONE submission is ONE read: a literal newline would split the message
        into several input records, turning one routed message into several
        commands.
        """
        self.silence_line_discipline()
        body = text.replace("\r", "\n").replace("\n", " ").encode()
        async with self._write_lock:
            if not await self._write(body + b"\n"):
                return False
        self._submitted += 1
        return True

    async def output(self) -> AsyncIterator[bytes]:
        """Yield the child's output as it arrives; ends when the child exits.

        Single-consumer: it registers a reader on the master fd, so two
        concurrent iterations would steal each other's bytes.

        Yields:
          chunk: Bytes the child wrote, unmodified.

        """
        loop = asyncio.get_running_loop()
        while self._master_fd >= 0:
            ready: asyncio.Future[None] = loop.create_future()
            loop.add_reader(self._master_fd, _resolve, ready)
            try:
                await ready
            finally:
                with contextlib.suppress(ValueError, OSError):
                    loop.remove_reader(self._master_fd)
            try:
                chunk = os.read(self._master_fd, 65_536)
            except BlockingIOError:
                continue
            except OSError:
                # The child closed its side: on a pty the master reads EIO
                # rather than EOF, so this is the normal end of a session.
                return
            if not chunk:
                return
            yield chunk

    async def terminate(self) -> None:
        """Stop the child's process group; safe to repeat.

        TERM first, then KILL after the grace period. A CLI wrapping a native
        child (codex is a node launcher over one) can ignore TERM, and a shell
        running ``sleep`` passes it to no one -- so without the escalation the
        caller waits for work it already abandoned.

        The grace poll deliberately does NOT reap: leaving the exited leader as
        a zombie keeps it anchoring the group id, so the final KILL cannot land
        on a recycled, unrelated group. :meth:`wait` reaps it afterwards.
        """
        if self._pid <= 0:
            return
        if not self._signal(signal.SIGTERM):
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._terminate_grace_sec
        while loop.time() < deadline:
            if self._exited():
                break
            await asyncio.sleep(0.01)
        # The whole group received TERM; once its leader is gone, the rest are
        # stragglers with no reason to outlive it.
        if self._signal(signal.SIGKILL):
            await asyncio.to_thread(self._wait_until_exited)

    async def wait(self) -> int:
        """Return the child's exit status, or ``128 + signal`` when killed.

        Returns:
          status: The child's exit code.

        """
        if self._status is None:
            self._status = await asyncio.to_thread(self._reap)
        return self._status

    async def close(self) -> None:
        """Release the master fd. Idempotent; does not stop the child."""
        if self._master_fd >= 0:
            os.close(self._master_fd)
            self._master_fd = -1

    def _signal(self, sig: int) -> bool:
        """Signal the child's process group; False once it is gone.

        The pid is re-read here rather than captured by the caller: a
        concurrent :meth:`wait` can reap the child and clear it mid-grace, and
        ``killpg(-1, ...)`` signals every process the user owns.

        ESRCH from ``killpg`` does NOT mean the child is gone. ``pty.fork``
        returns in the parent before the child finishes ``setsid``, so for a
        moment no group has ``pgid == child_pid`` and the call fails on a
        perfectly healthy child -- measured at ~25% of spawns. Treated as
        death, that abandons the child and the caller's ``wait`` blocks
        forever. In that window the child is still in OUR group and has not
        yet exec'd, so it has no descendants and ``kill`` reaches exactly it.
        """
        pid = self._pid
        if pid <= 0:
            return False
        try:
            os.killpg(pid, sig)
        except PermissionError:
            return False
        except ProcessLookupError:
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                return False
        return True

    def _exited(self) -> bool:
        """Whether the child has exited, WITHOUT reaping it."""
        if self._pid <= 0:
            return True
        try:
            exited = os.waitid(
                os.P_PID, self._pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
            )
        except ChildProcessError:
            return True
        return exited is not None

    def _wait_until_exited(self) -> None:
        """Wait for child exit without consuming the status used by wait()."""
        pid = self._pid
        if pid <= 0:
            return
        with contextlib.suppress(ChildProcessError):
            _ = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)

    def _reap(self) -> int:
        """Block until the child exits; return its status."""
        if self._pid <= 0:
            return 0
        try:
            _, status = os.waitpid(self._pid, 0)
        except ChildProcessError:
            self._pid = -1
            return 0
        self._pid = -1
        if os.WIFSIGNALED(status):
            return 128 + os.WTERMSIG(status)
        return os.WEXITSTATUS(status)

    async def _write(self, data: bytes) -> bool:
        """Write every byte to the master; False once the child is gone.

        Caller holds ``_write_lock``: the master is non-blocking, so a large
        payload yields mid-write and a peer could otherwise land bytes inside
        a bracketed paste.
        """
        if self._master_fd < 0:
            return False
        view = memoryview(data)
        while view:
            try:
                count = os.write(self._master_fd, view)
            except BlockingIOError:
                await asyncio.sleep(0)
                continue
            except OSError:
                return False
            if count == 0:
                return False
            view = view[count:]
        return True


def _resolve(ready: asyncio.Future[None]) -> None:
    """Complete ``ready`` once, ignoring a repeated reader callback."""
    if not ready.done():
        ready.set_result(None)
