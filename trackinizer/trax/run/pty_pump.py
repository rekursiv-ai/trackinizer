"""Run a child CLI on a PTY we own, pumping bytes to/from the real terminal.

``trax run`` normally lets the CLI inherit the terminal directly. To inject
messages into a live session (the human keeps typing; the server splices in),
the wrapper must instead own the CLI's controlling terminal -- a pseudo-tty
whose master fd it holds. The human's keystrokes and server-injected text are
then peers on the same master: the CLI cannot tell them apart, so the native
TUI keeps working while injection lands in the same input stream.

The injection protocol is the one verified against both claude and codex
(``docs/design_session_messaging.md``): wrap the text in a **bracketed
paste** so the TUI treats it as one atomic block rather than racing the
human's keystrokes, then send Enter as a **separate write delayed past
codex's 120ms paste-Enter suppression window** so it submits instead of being
absorbed into the paste.

This module is the pump mechanism only. The source of injected messages (the
server back-channel) wires into :meth:`PtyPump.inject` separately.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from types import FrameType
from typing import IO, Any, Final

import contextlib
import errno
import fcntl
import os
import pty
import select
import signal
import struct
import sys
import termios
import threading
import time
import tty


# A ``signal.signal`` handler value: a callable, or one of the sentinel
# ``Handlers`` (SIG_DFL / SIG_IGN), or ``None`` when none was installed.
type _SignalHandler = (
    Callable[[int, FrameType | None], object] | int | signal.Handlers | None
)
# ``termios.tcgetattr`` returns a list mixing ints and the cc list; the stub
# exposes it as ``list[Any]``, so mirror that for the round-trip to tcsetattr.
type _TermAttr = list[Any]


# Bracketed-paste bookends: the TUI buffers everything between them as one
# atomic paste (``EnableBracketedPaste`` is on in both target CLIs).
_PASTE_START: Final = b"\x1b[200~"
_PASTE_END: Final = b"\x1b[201~"

_SUBMIT: Final = b"\r"

# Fallback PTY size (rows, cols) when the wrapper's stdin carries none (piped
# or redirected). A 0x0 PTY makes child TUIs render one char per line and
# breaks their input handling, so a non-tty run still gets a usable geometry.
_DEFAULT_WINSIZE: Final = (
    24,
    80,
)


def encode_injection(text: str) -> bytes:
    r"""Encode ``text`` as a bracketed-paste block (no trailing Enter).

    The Enter is deliberately separate (sent later by the pump) so the TUI
    does not absorb it into the paste burst; this returns only the atomic
    paste body so a caller can test the encoding without timing.

    The payload's own paste sentinels are stripped first: a ``\\x1b[201~`` in
    the text would close the bracket early and turn the remainder into live
    keystrokes outside the atomic paste, bypassing the submission protocol.
    Bracketed paste is the only thing keeping injection atomic against the
    human's typing, so the sentinel bytes must never survive in the payload.
    """
    body = text.encode().replace(_PASTE_END, b"").replace(_PASTE_START, b"")
    return _PASTE_START + body + _PASTE_END


class PtyPump:
    """Spawn ``argv`` on a PTY, mirror it to the real terminal, allow injection.

    Byte-transparent in both directions: the child renders to the real
    terminal exactly as if run directly, and the human's input reaches it
    unchanged. :meth:`inject` is thread-safe -- a background caller (the
    server channel) queues a message and the pump writes it to the master fd
    between human keystrokes.

    Args:
      argv: The child command and its arguments.

    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        # Enter, sent as its own write this long after the paste. Must exceed
        # codex's ``PASTE_ENTER_SUPPRESS_WINDOW`` (120ms) so it submits rather
        # than being folded into the paste burst. 150ms clears it with margin.
        enter_delay_sec: float = 0.15,
        env: Mapping[str, str] | None = None,
        on_input: Callable[[bytes], None] | None = None,
    ) -> None:
        self._argv = list(argv)
        self._enter_delay_sec = enter_delay_sec
        # Extra environment for the child (e.g. ``TRAX_ACTOR`` / ``TRAX_ROOMS``
        # so an agent knows its own routing identity to address peers).
        self._env = dict(env) if env else {}
        # Observer of the human's raw keystrokes (stdin -> master only, never
        # injected bytes), so the runner can detect typed slash-commands the
        # CLI handles internally and never logs. Must not raise or block.
        self._on_input = on_input
        self._master_fd = -1
        self._pid = -1
        # The child's exit status once ``_child_alive`` reaps it via WNOHANG,
        # else None. Without this, that early reap would leave ``_reap``'s
        # blocking ``waitpid`` with no child and it would report 0, losing the
        # real exit code when stdin closes before the child (K1).
        self._exit_status: int | None = None
        self._injected = 0
        # Serializes whole injections (paste + delay + Enter). Without it,
        # back-to-back injects from the poll loop would write paste1 paste2
        # before either Enter, merging two messages into one submit (REV-24).
        # Held across the Enter-delay sleep, but only injects contend on it.
        self._inject_lock = threading.Lock()
        # Serializes individual writes to the master so a human keystroke
        # cannot land inside an injection's bracketed paste (REV-30). Held
        # only around each ``os.write`` -- never across the sleep -- so a
        # keystroke waits at most one write, not the full Enter-delay (R2R-025).
        self._write_lock = threading.Lock()

    def inject(self, text: str) -> None:
        r"""Inject ``text`` into the child's input and submit it. Thread-safe.

        Writes the bracketed-paste block, waits past the Enter-suppression
        window, then writes ``\\r`` -- all under one lock, so each message is
        submitted on its own before the next paste begins (the human's
        keystrokes still interleave between injections, never within one).
        A no-op once the child has exited (master closed).

        Holds ``_inject_lock`` across the whole injection (paste, delay, Enter)
        so two injects never interleave (REV-24), but each ``os.write`` to the
        master takes the separate ``_write_lock`` only for its own duration.
        The Enter-delay sleep runs without ``_write_lock``, so a human keystroke
        forwarded stdin->master stalls at most one write, not the full delay
        (R2R-025). A keystroke landing between the (closed) paste and the Enter
        is harmless: the paste bracket is already terminated.
        """
        if self._master_fd < 0:
            return
        with self._inject_lock:
            if not self._write_locked(encode_injection(text)):
                return
            time.sleep(self._enter_delay_sec)
            self._write_locked(_SUBMIT)
            self._injected += 1

    def _write_locked(self, data: bytes) -> bool:
        """Write all of ``data`` to the master under ``_write_lock`` (REV-30)."""
        with self._write_lock:
            return self._write_all(data)

    def _write_all(self, data: bytes) -> bool:
        r"""Write every byte of ``data`` to the master, looping partial writes.

        A bare ``os.write`` may write fewer bytes than given; dropping the tail
        of a bracketed paste would strip its ``\\x1b[201~`` and wedge the TUI
        in paste mode (REV-25). Returns False if the fd died mid-write.
        """
        if self._master_fd < 0:
            return False
        return _write_all_fd(self._master_fd, data)

    def injected_count(self) -> int:
        """How many messages have been injected and submitted (tests/observability)."""
        with self._inject_lock:
            return self._injected

    def terminate(self) -> None:
        """Signal the child to exit (SIGTERM); safe to call repeatedly."""
        if self._pid > 0:
            with contextlib.suppress(ProcessLookupError):
                os.kill(self._pid, signal.SIGTERM)

    def run(self) -> int:
        """Spawn the child and pump until it exits; return its exit status.

        Sets the real terminal raw for the child's lifetime, forwards window
        size and ``SIGWINCH``, and restores the terminal on exit.
        """
        self._pid, self._master_fd = pty.fork()
        if self._pid == 0:
            # Child: exec the CLI. If exec fails (permissions, ENOEXEC, ...),
            # ``_exit`` immediately -- never fall through into the parent's
            # pump code, which would leave two processes sharing the master fd.
            # A TUI on a PTY needs ``TERM``; a non-tty parent env may lack it,
            # which degrades rendering and input handling, so default it.
            os.environ.setdefault("TERM", "xterm-256color")
            os.environ.update(self._env)  # routing identity for the agent
            try:
                os.execvp(self._argv[0], self._argv)  # noqa: S606 -- the point of `trax run`.
            except OSError:
                os._exit(127)
        # From here the child is forked and exec'd onto the PTY slave; any raise
        # in terminal setup (winsize, raw mode, SIGWINCH) before or during the
        # pump must still reap it, or it leaks as a zombie (R-24). The outer
        # ``finally`` terminates and reaps the child on every exit path.
        try:
            self._sync_winsize()
            stdin_fd = _real_fd(sys.stdin)
            out_fd = _real_fd(sys.stdout)
            old_attr = _enter_raw(stdin_fd)
            had_winch, old_winch = _install_winch(self._sync_winsize)
            try:
                return self._pump(stdin_fd, out_fd)
            finally:
                if had_winch:
                    signal.signal(signal.SIGWINCH, old_winch)
                _restore(stdin_fd, old_attr)
        finally:
            if self._master_fd >= 0:
                os.close(self._master_fd)
                self._master_fd = -1
            if self._pid > 0:
                # Setup failed before ``_pump`` reaped the child (the normal
                # path reaps via ``_reap`` and clears ``_pid``); terminate and
                # reap it now so the child is never leaked.
                self.terminate()
                self._reap()

    def _pump(self, stdin_fd: int, out_fd: int, *, poll_sec: float = 0.05) -> int:
        """Copy stdin<->master until the child exits.

        ``stdin_fd``/``out_fd`` may be -1 when the wrapper's own stdio is not
        a real fd (e.g. captured under a test): the pump then skips that
        direction and watches only the master, still mirroring the child.
        Injection (paste + Enter) is performed synchronously by :meth:`inject`
        on the caller's thread, so this loop only mirrors I/O.
        """
        while True:
            watch = [fd for fd in (stdin_fd, self._master_fd) if fd >= 0]
            try:
                readable, _, _ = select.select(watch, [], [], poll_sec)
            except OSError as err:
                if err.errno == errno.EINTR:  # SIGWINCH interrupted the wait.
                    continue
                raise
            if stdin_fd in readable and not self._forward(stdin_fd, self._master_fd):
                # Human stdin closed (Ctrl-D at our layer): stop forwarding it
                # but keep mirroring the child until it exits on its own.
                stdin_fd = -1
            if self._master_fd in readable and not self._forward(
                self._master_fd, out_fd
            ):
                break  # Child closed its side: it has exited.
            if stdin_fd < 0 and not self._child_alive():
                break
        return self._reap()

    def _forward(self, src_fd: int, dst_fd: int, *, read_size: int = 65_536) -> bool:
        """Copy one chunk ``src_fd`` -> ``dst_fd``; False on EOF/error.

        A ``dst_fd`` of -1 (no real destination, e.g. captured stdout under a
        test) still drains ``src_fd`` and reports liveness -- the bytes are
        discarded rather than crashing on a write to a bad fd. A write *to the
        master* takes ``_write_lock`` so a human keystroke cannot land inside
        an ``inject``'s bracketed paste (REV-30); the read-side is the only
        place that frames stdin->master.
        """
        try:
            data = os.read(src_fd, read_size)
        except OSError:
            return False
        if not data:
            return False
        if dst_fd < 0:
            return True
        if dst_fd == self._master_fd:
            wrote = self._write_locked(data)
            # Tee the human's keystrokes to the observer *after* they reach the
            # master, so the runner only records input the CLI actually
            # received (not bytes that failed to write, e.g. a dead child).
            # The only caller forwards stdin -> master here; injected bytes go
            # straight to the master via ``inject`` and never pass through, so
            # the observer sees exactly what the human typed. The ``src_fd``
            # guard is defence-in-depth against a future master -> master call
            # shape that does not exist today.
            if wrote and self._on_input is not None and src_fd != self._master_fd:
                self._on_input(data)
            return wrote
        # Loop the mirror write too: a bare ``os.write`` may short-write (a full
        # pipe, a slow terminal), and dropping the unwritten tail would corrupt
        # the child's mirrored output (K6-003), exactly as it would an injection.
        return _write_all_fd(dst_fd, data)

    def _sync_winsize(self, signum: int = 0, frame: FrameType | None = None) -> None:
        """Copy the real terminal's window size onto the PTY (SIGWINCH-safe).

        Doubles as the SIGWINCH handler, hence the (unused) signal-handler
        parameters; the ``signal.signal`` API fixes their names.
        """
        del signum, frame
        if self._master_fd < 0:
            return
        with contextlib.suppress(OSError):
            fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, _current_winsize())

    def _child_alive(self) -> bool:
        """Whether the child process has not yet been reaped.

        Reaps the child with ``WNOHANG`` and stashes its status in
        ``_exit_status`` so :meth:`_reap` can still return the real exit code:
        this poll, not the later blocking ``waitpid``, is what collects the
        status when stdin closes before the child.
        """
        if self._pid <= 0:
            return False
        try:
            pid, status = os.waitpid(self._pid, os.WNOHANG)
        except ChildProcessError:
            return False
        if pid == 0:
            return True
        self._exit_status = status
        return False

    def _reap(self) -> int:
        """Wait for the child and return its exit status (or 128+signal).

        Returns the status already collected by :meth:`_child_alive` when that
        poll reaped the child first. Clears ``_pid`` once reaped so a late
        :meth:`terminate` cannot ``os.kill`` a recycled, unrelated PID.
        """
        status = self._exit_status
        if status is None:
            try:
                _, status = os.waitpid(self._pid, 0)
            except ChildProcessError:
                self._pid = -1
                return 0
        self._pid = -1
        if os.WIFSIGNALED(status):
            return 128 + os.WTERMSIG(status)
        return os.WEXITSTATUS(status)


def _write_all_fd(fd: int, data: bytes) -> bool:
    r"""Write every byte of ``data`` to ``fd``, looping partial writes.

    A bare ``os.write`` may write fewer bytes than given (a full pipe, a slow
    consumer); dropping the unwritten tail corrupts whatever framing the bytes
    carry -- a bracketed paste loses its ``\\x1b[201~`` and wedges the TUI
    (REV-25), and a mirrored child chunk arrives truncated (K6-003). Returns
    False if the fd died mid-write.
    """
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except OSError:
            return False
        if written == 0:
            # A blocking fd never returns 0 for a non-empty write under normal
            # conditions; a 0 means it has stopped accepting bytes. Bail instead
            # of looping forever on an unchanged view (C-PTY-WRITE-ZERO-SPIN).
            return False
        view = view[written:]
    return True


def _current_winsize() -> bytes:
    """Packed ``TIOCSWINSZ`` payload from the real stdin, or a sane default.

    When stdin is not a tty (piped/redirected, under a test), reading its size
    fails; a 0x0 PTY then makes child TUIs render one char per line and breaks
    input handling, so fall back to :data:`_DEFAULT_WINSIZE`.
    """
    try:
        return fcntl.ioctl(
            sys.stdin.fileno(), termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0)
        )
    except (OSError, ValueError, AttributeError):
        # Captured/replaced stdin under test can raise ValueError or
        # AttributeError on ``fileno()``, not just OSError; mirror ``_real_fd``.
        return struct.pack("HHHH", *_DEFAULT_WINSIZE, 0, 0)


def _real_fd(stream: IO[Any]) -> int:
    """Return ``stream.fileno()`` if it is a real OS fd, else -1.

    Under test capture (and some redirections) ``fileno()`` raises or returns
    a negative value; the pump treats -1 as "no real fd" and skips that
    direction rather than feeding it to ``select`` (which rejects -1).
    """
    try:
        fd = stream.fileno()
    except (OSError, ValueError, AttributeError):
        return -1
    return fd if fd >= 0 else -1


def _enter_raw(fd: int) -> _TermAttr | None:
    """Put ``fd`` in raw mode; return prior attributes to restore, or None.

    Returns None when ``fd`` is not a tty (piped stdin under a test, or -1),
    so the pump still runs -- it just has no terminal modes to toggle.
    """
    if fd < 0:
        return None
    try:
        old = termios.tcgetattr(fd)
    except termios.error:
        return None
    tty.setraw(fd)
    return old


def _restore(fd: int, old_attr: _TermAttr | None) -> None:
    """Restore terminal attributes saved by :func:`_enter_raw`."""
    if old_attr is None:
        return
    with contextlib.suppress(termios.error):
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)


def _install_winch(
    handler: Callable[[int, FrameType | None], object],
) -> tuple[bool, _SignalHandler]:
    """Install a SIGWINCH handler; return ``(installed, prior_handler)``.

    Signal handlers can only be set on the main thread; ``trax run`` calls the
    pump from the main thread, but a test driving it from a worker would
    otherwise crash, so a non-main thread silently skips resize forwarding
    (``installed`` is False and nothing is restored).
    """
    if threading.current_thread() is not threading.main_thread():
        return (False, None)
    return (True, signal.signal(signal.SIGWINCH, handler))
