"""Tests for the PTY pump: injection encoding + a real end-to-end round-trip."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import contextlib
import fcntl
import os
import pty
import struct
import sys
import threading
import time

from trackinizer.trax.run import pty_pump
from trackinizer.trax.run.pty_pump import (
    PtyPump,
    encode_injection,
)


if TYPE_CHECKING:
    import pytest


class TestEncodeInjection:
    def test_wraps_in_bracketed_paste_without_enter(self) -> None:
        out = encode_injection("hello")
        # Bracketed-paste bookends so the TUI treats it as one atomic block.
        assert out == b"\x1b[200~hello\x1b[201~"
        # No trailing Enter: the pump sends ``\r`` separately, after a delay,
        # so codex does not absorb it into the paste burst.
        assert not out.endswith(b"\r")

    def test_neutralizes_smuggled_paste_end(self) -> None:
        r"""A sender cannot break out of the atomic paste with ``\\x1b[201~``.

        Bracketed paste is the only thing keeping injection atomic against the
        human's keystrokes. If the payload could carry the paste-end sentinel,
        bytes after it would land as live keystrokes outside the bracket --
        bypassing the submission protocol. The encoder must leave exactly one
        paste-end, at the very end.
        """
        out = encode_injection("hello\x1b[201~rm -rf x")
        assert out.count(b"\x1b[201~") == 1
        assert out.endswith(b"\x1b[201~")
        # The same guard for a smuggled paste-start.
        assert encode_injection("a\x1b[200~b").count(b"\x1b[200~") == 1

    def test_encodes_unicode(self) -> None:
        assert encode_injection("café").startswith(b"\x1b[200~")
        assert "café".encode() in encode_injection("café")


class TestForwardHandlesPartialWrite:
    """The child->stdout forward must deliver every byte despite short writes.

    K6-003: ``_forward`` mirrored the child's output to stdout with a single
    bare ``os.write``, which may write fewer bytes than given (a full pipe, a
    slow terminal). The unwritten tail was dropped, corrupting the mirrored
    output. The forward must loop partial writes like the injection path does.
    """

    def test_short_write_to_stdout_delivers_all_bytes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A source pipe holding the child's output, and a fake ``os.write`` that
        # writes at most one byte per call (the worst-case short write).
        src_r, src_w = os.pipe()
        payload = b"child output that must arrive whole"
        os.write(src_w, payload)
        os.close(src_w)

        delivered: list[int] = []
        real_write = os.write

        def short_write(fd: int, data: bytes) -> int:
            if fd == 99:  # the fake stdout destination
                delivered.append(data[0])
                return 1  # write only one byte, forcing the loop
            return real_write(fd, data)

        monkeypatch.setattr(os, "write", short_write)
        pump = PtyPump(["true"])
        # dst_fd 99 is neither the master nor -1: the child->stdout branch.
        forwarded = pump._forward(src_r, 99)
        os.close(src_r)

        assert forwarded
        # Every byte of the chunk reached the destination, in order.
        assert bytes(delivered) == payload

    def test_zero_write_does_not_spin_forever(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # C-PTY-WRITE-ZERO-SPIN: an ``os.write`` returning 0 leaves the unsent
        # view unchanged, so the partial-write loop would spin forever. A 0 on a
        # blocking fd means it stopped accepting bytes; treat it as a write
        # failure (return False) instead of looping. Use a real source pipe with
        # bytes and a dst whose write always returns 0.
        src_r, src_w = os.pipe()
        os.write(src_w, b"bytes that cannot be written")
        os.close(src_w)
        real_write = os.write

        def zero_write(fd: int, data: bytes) -> int:
            return 0 if fd == 99 else real_write(fd, data)

        monkeypatch.setattr(os, "write", zero_write)
        pump = PtyPump(["true"])
        forwarded = pump._forward(src_r, 99)  # returns (does not hang)
        os.close(src_r)
        assert forwarded is False, "a 0-byte write is a failure, not an infinite loop"


class TestInjectSubmitsEachMessage:
    def test_back_to_back_injects_each_get_their_own_enter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r"""Two queued messages must not merge into one submit (REV-24).

        Each ``inject`` must write its paste and its own ``\\r`` before the
        next paste, or the composer accumulates ``paste1 paste2`` and the
        first Enter submits the merged blob. Drive two injects back-to-back
        (as the poll loop does) and assert the write order interleaves
        paste/enter, never paste/paste/enter/enter.
        """
        writes: list[bytes] = []

        def fake_write(fd: int, data: bytes) -> int:
            del fd
            writes.append(bytes(data))
            return len(data)

        monkeypatch.setattr(os, "write", fake_write)
        pump = PtyPump(["true"], enter_delay_sec=0.0)
        # Pretend the child is live so inject writes to a (fake) master fd.
        pump._master_fd = 7
        pump.inject("alpha")
        pump.inject("beta")
        joined = b"".join(writes)
        # Each paste is immediately followed by its own submit.
        assert joined == (
            encode_injection("alpha") + b"\r" + encode_injection("beta") + b"\r"
        )

    def test_keystroke_does_not_stall_behind_injects_sleep(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A human keystroke must not wait the inject's Enter-delay (R2R-025).

        ``inject`` sleeps ``enter_delay_sec`` between its paste and its Enter.
        If it holds the master-write lock across that sleep, every keystroke
        forwarded stdin->master (``_forward``) stalls the full delay, freezing
        the TUI for ~delay per in-flight inject. The lock must cover only the
        ``os.write`` calls, not the sleep, so a keystroke lands promptly.
        """
        # Real pipe stands in for the PTY master; a reader drains it so writes
        # never block on a full buffer.
        master_r, master_w = os.pipe()
        stop_reader = threading.Event()

        def _drain() -> None:
            while not stop_reader.is_set():
                try:
                    if not os.read(master_r, 4096):
                        return
                except OSError:
                    return

        reader = threading.Thread(target=_drain, daemon=True)
        reader.start()

        key_r, key_w = os.pipe()
        os.write(key_w, b"x")

        # Signal exactly when the inject has written its paste and entered the
        # Enter-delay sleep, then hold the sleep open until the test releases
        # it -- so the keystroke is forwarded while the inject is provably
        # mid-sleep (deterministic, not timing-guessed).
        in_sleep = threading.Event()
        release = threading.Event()

        def fake_sleep(_seconds: float) -> None:
            in_sleep.set()
            release.wait(5.0)

        # ``pty_pump`` calls ``time.sleep`` via the stdlib ``time`` module, so
        # patching the shared module object intercepts the inject's Enter-delay.
        monkeypatch.setattr(time, "sleep", fake_sleep)

        pump = PtyPump(["true"], enter_delay_sec=0.5)
        pump._master_fd = master_w

        injector = threading.Thread(target=pump.inject, args=("hello",), daemon=True)
        injector.start()
        assert in_sleep.wait(2.0), "inject never reached its Enter-delay sleep"

        # The inject is mid-sleep; forward one keystroke. With the lock held
        # across the sleep this blocks until ``release``; the fix lets it pass.
        start = time.monotonic()
        forwarded = pump._forward(key_r, master_w)
        elapsed = time.monotonic() - start
        release.set()

        assert forwarded
        assert elapsed < 0.2, f"keystroke stalled {elapsed:.3f}s behind inject sleep"

        injector.join(timeout=2.0)
        stop_reader.set()
        for fd in (master_r, master_w, key_r, key_w):
            with contextlib.suppress(OSError):
                os.close(fd)


def _run_pump_in_thread(pump: PtyPump) -> tuple[threading.Thread, list[int]]:
    """Run ``pump.run`` on a worker; the rc lands in the returned list.

    Late errors (e.g. a write to a test fd already closed during teardown)
    are swallowed: they are harness artifacts, not pump behavior, and would
    otherwise trip pytest's thread-exception plugin under warnings-as-errors.
    """
    rc: list[int] = []

    def _target() -> None:
        try:
            rc.append(pump.run())
        except Exception:  # noqa: BLE001 -- teardown-race artifact, not under test.
            rc.append(-1)

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    return t, rc


class TestPtyPumpRoundTrip:
    """A real child on a real PTY: byte-transparency and injection.

    The child is a tiny Python process that echoes its stdin to stdout, so
    bytes written to the master (human keystrokes *or* injection) come back
    out the master and the test can assert what the child received.
    """

    def test_human_input_reaches_child_and_output_mirrors_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A child that reads one line and echoes it with a marker, then exits.
        child = (
            "import sys; line = sys.stdin.readline(); "
            "sys.stdout.write('GOT:' + line); sys.stdout.flush()"
        )
        pump = PtyPump([sys.executable, "-c", child])

        # Drive the pump's own stdin from a pipe so the test can "type".
        stdin_r, stdin_w = os.pipe()
        monkeypatch.setattr(sys, "stdin", os.fdopen(stdin_r))
        # Capture what the pump writes to stdout (the child's mirrored output).
        out_r, out_w = os.pipe()
        monkeypatch.setattr(
            sys, "stdout", os.fdopen(out_w, "w", buffering=1, errors="replace")
        )

        thread, rc = _run_pump_in_thread(pump)
        os.write(stdin_w, b"hello\n")
        captured = b""
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            chunk = os.read(out_r, 4096)
            if chunk:
                captured += chunk
            if b"GOT:hello" in captured or not thread.is_alive():
                break
        thread.join(timeout=5.0)
        assert b"GOT:hello" in captured, captured
        assert rc == [0]

    def test_injection_is_received_by_child(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The child reads until EOF and reports everything it saw, so the test
        # can confirm the injected text (sans paste bookends) arrived.
        child = (
            "import sys; data = sys.stdin.read(); "
            "sys.stdout.write('SAW:' + data.replace(chr(13), '|')); "
            "sys.stdout.flush()"
        )
        pump = PtyPump([sys.executable, "-c", child])
        stdin_r, stdin_w = os.pipe()
        monkeypatch.setattr(sys, "stdin", os.fdopen(stdin_r))
        out_r, out_w = os.pipe()
        monkeypatch.setattr(
            sys, "stdout", os.fdopen(out_w, "w", buffering=1, errors="replace")
        )

        thread, _rc = _run_pump_in_thread(pump)
        # ``inject`` no-ops until the child's PTY master is live, so retry it
        # until one lands rather than guessing a fixed startup delay.
        deadline = time.monotonic() + 5.0
        while pump.injected_count() == 0 and time.monotonic() < deadline:
            pump.inject("run it")
            time.sleep(0.005)
        captured = b""
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            chunk = os.read(out_r, 4096)
            if chunk:
                captured += chunk
            if b"run it" in captured:
                break
            if not thread.is_alive():
                break
        # The injected text reached the child (terminal may transform the
        # bracketed-paste control bytes, but the payload survives).
        assert b"run it" in captured, captured
        # The child blocks reading the PTY slave until EOF, which closing our
        # upstream stdin pipe never delivers (we still hold the master open),
        # so SIGTERM it to end the pump loop instead of hanging the join.
        os.close(stdin_w)
        pump.terminate()
        thread.join(timeout=5.0)

    def test_on_input_sees_human_keystrokes_not_injection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stdin tee observes only what the human typed, never injection.

        Slash-command capture (task A) hangs off this: the detector must see
        the human's ``/exit`` but must not mistake a server-injected message
        for a typed command.
        """
        child = (
            "import sys; data = sys.stdin.read(); "
            "sys.stdout.write('SAW:' + data.replace(chr(13), '|')); "
            "sys.stdout.flush()"
        )
        seen: list[bytes] = []
        pump = PtyPump([sys.executable, "-c", child], on_input=seen.append)
        stdin_r, stdin_w = os.pipe()
        monkeypatch.setattr(sys, "stdin", os.fdopen(stdin_r))
        _out_r, out_w = os.pipe()
        monkeypatch.setattr(
            sys, "stdout", os.fdopen(out_w, "w", buffering=1, errors="replace")
        )

        thread, _rc = _run_pump_in_thread(pump)
        os.write(stdin_w, b"/exit\n")  # human types a slash-command
        # Wait until the keystroke reaches the observer (which also means the
        # master is live) before injecting, instead of a fixed delay.
        deadline = time.monotonic() + 5.0
        while b"/exit" not in b"".join(seen) and time.monotonic() < deadline:
            time.sleep(0.005)
        pump.inject("injected text")  # server splices a message in
        # ``inject`` writes the paste and Enter before returning, so injection
        # is already complete -- no settle delay needed.
        os.close(stdin_w)
        # The child blocks reading the PTY slave until EOF; SIGTERM ends the
        # pump loop instead of hanging the join for its full timeout.
        pump.terminate()
        thread.join(timeout=5.0)

        observed = b"".join(seen)
        # The human's keystrokes reached the observer...
        assert b"/exit" in observed, observed
        # ...but the injected text did not (it bypasses the stdin->master tee).
        assert b"injected text" not in observed


class _FakeStdin:
    """Minimal stand-in for ``sys.stdin`` with a fixed ``fileno``."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def fileno(self) -> int:
        return self._fd


class TestCurrentWinsize:
    """The PTY size fallback that keeps child TUIs usable under non-tty stdin."""

    def test_uses_real_stdin_size_when_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Drive the success branch directly: a fake ioctl returning a known
        # size (real ``sys.stdin`` under pytest is captured and has none).
        packed = struct.pack("HHHH", 40, 120, 0, 0)

        def fake_ioctl(*_a: object) -> bytes:
            return packed

        monkeypatch.setattr(fcntl, "ioctl", fake_ioctl)
        monkeypatch.setattr(sys, "stdin", _FakeStdin(0))
        rows, cols, _, _ = struct.unpack("HHHH", pty_pump._current_winsize())
        assert (rows, cols) == (40, 120)

    def test_falls_back_to_default_when_stdin_has_no_size(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_a: object) -> bytes:
            raise OSError("not a tty")

        monkeypatch.setattr(fcntl, "ioctl", boom)
        rows, cols, _, _ = struct.unpack("HHHH", pty_pump._current_winsize())
        # A 0x0 PTY breaks child TUIs; the fallback must be a usable geometry.
        assert (rows, cols) == pty_pump._DEFAULT_WINSIZE
        assert rows > 0
        assert cols > 0


class TestPtyPumpLifecycle:
    """Child reaping and post-exit safety."""

    def test_run_returns_child_exit_code(self) -> None:
        assert PtyPump(["true"]).run() == 0
        assert PtyPump(["false"]).run() == 1

    def test_exit_code_survives_child_alive_poll(self) -> None:
        """``_child_alive`` must not discard the child's exit status.

        When the human's stdin closes before the child, the pump polls
        ``_child_alive`` (a ``waitpid(WNOHANG)``) until the child is gone. That
        call reaps the zombie; if it drops the status, the later ``_reap``
        finds no child (``ChildProcessError``) and reports 0, losing the
        wrapped CLI's real exit code. Spawn a child that exits 7, let it die,
        then drive the exact ``_child_alive`` -> ``_reap`` sequence the pump's
        stdin-closed-first path takes.
        """
        pump = PtyPump(["unused"])
        pump._pid, pump._master_fd = pty.fork()
        if pump._pid == 0:  # pragma: no cover -- the spawned child.
            os._exit(7)
        # Wait for the child to actually exit so ``_child_alive`` reaps it.
        deadline = time.monotonic() + 5.0
        while pump._child_alive() and time.monotonic() < deadline:
            time.sleep(0.005)
        # The child is gone and ``_child_alive`` has just reaped it; the real
        # exit code must still surface through ``_reap``.
        assert pump._reap() == 7
        os.close(pump._master_fd)

    def test_reap_clears_pid_so_late_terminate_is_noop(self) -> None:
        """After the child is reaped, ``terminate`` must not signal a PID.

        REV-34: a retained PID could be recycled by the OS; a late
        ``terminate`` would then ``os.kill`` an unrelated process. ``_reap``
        resets ``_pid`` to -1, so a post-exit ``terminate`` is a clean no-op.
        """
        pump = PtyPump(["true"])
        assert pump.run() == 0
        assert pump._pid == -1
        # No ProcessLookupError, no signal to a stale/recycled PID.
        pump.terminate()

    def test_setup_failure_after_fork_reaps_the_child(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A raise between ``pty.fork`` and ``_pump`` must not leak the child.

        R-24: ``run`` forks, then runs terminal setup (winsize, raw mode,
        SIGWINCH) before the pump. If any of that raises, the child -- already
        forked and exec'd onto the PTY slave -- was never reaped: it lingered as
        a zombie (or worse, a live process). The child must be reaped in a
        ``finally`` so a setup failure cannot leak it.
        """
        # A child that sleeps long enough to still be alive when setup fails, so
        # the test proves the child is actively reaped, not merely already gone.
        pump = PtyPump([sys.executable, "-c", "import time; time.sleep(30)"])

        def boom(_handler: object) -> object:
            raise RuntimeError("setup blew up after fork")

        # ``_install_winch`` runs after ``pty.fork`` and before ``_pump``.
        monkeypatch.setattr(pty_pump, "_install_winch", boom)

        with contextlib.suppress(RuntimeError):
            pump.run()

        # The child must have been reaped: its PID is cleared and a direct
        # waitpid finds no child (already reaped), rather than a leaked zombie.
        assert pump._pid == -1, "child PID not cleared; the child was leaked"

    def test_env_overrides_reach_the_child(self, tmp_path: Path) -> None:
        """``env`` overrides are visible to the spawned child (routing identity).

        The child writes ``$TRAX_ACTOR`` to a file; after the pump exits the
        file must hold the injected value -- proving an agent inside the
        session can read its own routing name.
        """
        out = tmp_path / "actor.txt"
        child = (
            "import os,pathlib;"
            f"pathlib.Path({str(out)!r}).write_text(os.environ.get('TRAX_ACTOR',''))"
        )
        pump = PtyPump([sys.executable, "-c", child], env={"TRAX_ACTOR": "scientist"})
        assert pump.run() == 0
        assert out.read_text() == "scientist"


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
