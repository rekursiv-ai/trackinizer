"""Tests for ``trax run --detach`` and the commands that reach a host by name."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import contextlib
import json
import os
import socket
import subprocess
import sys
import time

import pytest

from trackinizer.trax.daemon.protocol import socket_address
from trackinizer.trax.run import detach
from trackinizer.trax.run.detach import HostRecord, launch, main


if TYPE_CHECKING:
    from collections.abc import Generator


# A slug as long as the campaign names whose launch failed when tmux could not
# bind its socket ("File name too long"), and then some.
_LONG_NAME = "nightly-regression-sweep-shards-20260101-0900-" + "x" * 80


# A process of its own rather than the test's: a liveness probe that wrongly sent a
# real signal would otherwise hit the test runner itself.
@pytest.fixture
def live_child() -> Generator[subprocess.Popen[bytes]]:
    """Yield a running process that outlives the test, then end it.

    Yields:
      child: The running process.

    """
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield child
    finally:
        child.kill()
        _ = child.wait()


class TestNames:
    """A host name becomes a directory, so it must be one path component."""

    @pytest.mark.parametrize(
        "name",
        ["", "../up", "a/b", ".hidden", "_under", "x" * 129],
    )
    def test_rejects_a_name_that_is_not_a_filename(self, name: str) -> None:
        with pytest.raises(SystemExit, match="invalid host name"):
            _ = main(["log", name])

    @pytest.mark.parametrize("name", ["scientist#2", "lead@lab", _LONG_NAME])
    def test_accepts_routing_names_and_long_slugs(self, name: str) -> None:
        with pytest.raises(SystemExit, match="no host named"):
            _ = main(["log", name])


class TestSocketAddress:
    """A long name must not cost the host its socket, as it did under tmux."""

    def test_a_long_name_still_binds(self) -> None:
        root = detach._paths(_LONG_NAME)
        root.mkdir(parents=True)
        logical = root / "host.sock"
        assert len(os.fsencode(logical)) > 103, "the regression needs a long path"
        address = socket_address(logical)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(address))
        finally:
            listener.close()
        assert logical.exists()


class TestRecords:
    """``host.json``: what each command reads to find and judge a host."""

    def test_round_trips(self, tmp_path: Path) -> None:
        record = _record(tmp_path, state="exited", exit_code=3)
        detach._write_record(tmp_path, record)
        assert detach._read_record(tmp_path) == record

    def test_an_unreadable_record_is_absent(self, tmp_path: Path) -> None:
        (tmp_path / "host.json").write_text("{not json")
        assert detach._read_record(tmp_path) is None

    def test_the_record_is_json(self, tmp_path: Path) -> None:
        detach._write_record(tmp_path, _record(tmp_path))
        assert json.loads((tmp_path / "host.json").read_text())["name"] == "h"


class TestState:
    """The record's word, checked against whether anything still answers."""

    def test_exited_reports_the_status(self, tmp_path: Path) -> None:
        assert (
            detach._state(_record(tmp_path, state="exited", exit_code=7)) == "exited 7"
        )

    def test_a_listening_host_is_running(self, tmp_path: Path) -> None:
        with _listening(tmp_path / "s") as address:
            record = _record(tmp_path, address=address)
            assert detach._state(record) == "running"

    def test_a_live_host_that_stopped_listening_is_exiting(
        self,
        tmp_path: Path,
        live_child: subprocess.Popen[bytes],
    ) -> None:
        record = _record(tmp_path, state="running", pid=live_child.pid)
        assert detach._state(record) == "exiting"

    def test_a_live_host_not_yet_listening_is_starting(
        self,
        tmp_path: Path,
        live_child: subprocess.Popen[bytes],
    ) -> None:
        record = _record(tmp_path, state="starting", pid=live_child.pid)
        assert detach._state(record) == "starting"

    def test_a_vanished_host_is_lost(self, tmp_path: Path) -> None:
        """Killed outright, a host never records ``exited``."""
        record = _record(tmp_path, state="running", pid=_dead_pid())
        assert detach._state(record) == "lost"


class TestAlive:
    """Liveness is a probe: it must never disturb the process it asks about."""

    def test_a_live_process_is_alive_and_untouched(
        self,
        live_child: subprocess.Popen[bytes],
    ) -> None:
        assert detach._alive(live_child.pid)
        time.sleep(0.05)
        assert live_child.poll() is None

    def test_a_reaped_process_is_gone(self) -> None:
        assert not detach._alive(_dead_pid())


class TestExitStatus:
    """A ``SystemExit`` raised inside the host, as the status it reports."""

    def test_none_is_success(self) -> None:
        assert detach._exit_status(None) == 0

    def test_an_int_is_the_status(self) -> None:
        assert detach._exit_status(3) == 3

    def test_a_message_is_logged_and_fails(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The launcher shows the host log, so the reason must reach it."""
        assert detach._exit_status("trax run: claude not found in PATH") == 1
        assert capsys.readouterr().err == "trax run: claude not found in PATH\n"


class TestReadiness:
    """The pipe a launcher waits on: one answer, then closed."""

    def test_accept_answers_ok_once(self) -> None:
        """The later ``close`` must neither write again nor close a reused fd."""
        read_fd, write_fd = os.pipe()
        readiness = detach._Readiness(write_fd)
        readiness.accept()
        readiness.close()
        assert detach._await_ready(read_fd) == b"ok"

    def test_close_without_accepting_reads_as_a_failure(self) -> None:
        read_fd, write_fd = os.pipe()
        detach._Readiness(write_fd).close()
        assert detach._await_ready(read_fd) == b""


class TestCommands:
    """``trax run ls|attach|log|send|stop`` against recorded hosts."""

    def test_ls_lists_each_host(self, capsys: pytest.CaptureFixture[str]) -> None:
        root = detach._paths("alpha")
        root.mkdir(parents=True)
        detach._write_record(
            root,
            _record(root, name="alpha", state="exited", exit_code=0),
        )
        assert main(["ls"]) == 0
        header, row = capsys.readouterr().out.splitlines()
        assert row.split() == [
            "alpha",
            "exited",
            "0",
            "1",
            "2026-10-03T00:00:00+00:00",
            "sh",
            "--",
            "true",
        ]
        # Columns line up under their headings, left-aligned.
        assert [header.index(title) for title in ("STATE", "PID", "COMMAND")] == [
            row.index("exited"),
            row.index(" 1 ") + 1,
            row.index("sh --"),
        ]

    def test_ls_with_no_hosts_prints_the_header(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert main(["ls"]) == 0
        assert capsys.readouterr().out.split() == [
            "NAME",
            "STATE",
            "PID",
            "STARTED",
            "COMMAND",
        ]

    def test_log_prints_the_scrollback(
        self,
        capfdbinary: pytest.CaptureFixture[bytes],
    ) -> None:
        root = detach._paths("beta")
        root.mkdir(parents=True)
        detach._write_record(
            root,
            _record(root, name="beta", state="exited", exit_code=0),
        )
        (root / "scrollback.log").write_bytes(b"\x1b[1mbold\x1b[0m\r\n")
        assert main(["log", "beta"]) == 0
        assert capfdbinary.readouterr().out == b"\x1b[1mbold\x1b[0m\r\n"

    @pytest.mark.parametrize("command", ["attach", "send", "stop"])
    def test_a_host_that_is_gone_is_named_as_such(self, command: str) -> None:
        root = detach._paths("gamma")
        root.mkdir(parents=True)
        detach._write_record(
            root,
            _record(root, name="gamma", state="running", pid=_dead_pid()),
        )
        argv = [command, "gamma", *(["two", "words"] if command == "send" else [])]
        with pytest.raises(SystemExit, match="'gamma' is lost"):
            _ = main(argv)

    def test_help_names_every_command(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exited:
            _ = main(["--help"])
        assert exited.value.code == 0
        out = " ".join(capsys.readouterr().out.split())
        for line in (
            "ls List hosts and their state.",
            "attach Drive a host from this terminal; Ctrl-\\ detaches.",
            "log Print a host's scrollback.",
            "send Type a message into a host and press Enter.",
            "stop Stop a host and wait for it.",
        ):
            assert line in out

    def test_a_command_is_required(self) -> None:
        with pytest.raises(SystemExit) as exited:
            _ = main([])
        assert exited.value.code == 2

    @pytest.mark.parametrize("flag", ["-f", "--follow"])
    def test_following_an_exited_host_prints_it_all_and_returns(
        self,
        capfdbinary: pytest.CaptureFixture[bytes],
        flag: str,
    ) -> None:
        root = detach._paths("epsilon")
        root.mkdir(parents=True)
        detach._write_record(
            root,
            _record(root, name="epsilon", state="exited", exit_code=0),
        )
        (root / "scrollback.log").write_bytes(b"done\r\n")
        assert main(["log", "epsilon", flag]) == 0
        assert capfdbinary.readouterr().out == b"done\r\n"

    def test_launch_refuses_a_running_name(self) -> None:
        """Checked before forking, so a refusal never leaves a stray process."""
        root = detach._paths("delta")
        root.mkdir(parents=True)
        with _listening(root / "s") as address:
            detach._write_record(root, _record(root, name="delta", address=address))
            with pytest.raises(SystemExit, match="'delta' is running"):
                _ = launch("delta", command="sh", start=_never)


@pytest.mark.cli_python_subprocess
class TestDetachedLifecycle:
    """A real ``trax run --detach``: launch, watch, type, leave, return, stop."""

    def test_full_lifecycle_through_the_cli(self) -> None:
        name = _LONG_NAME
        child = (
            "import sys\n"
            "print('host up', flush=True)\n"
            "for line in sys.stdin:\n"
            "    print('ECHO:' + line.strip(), flush=True)\n"
        )
        launched = _trax(
            "run",
            "--detach",
            "--no-sync",
            "--name",
            name,
            "sh",
            "--",
            sys.executable,
            "-u",
            "-c",
            child,
        )
        assert launched.returncode == 0, launched.stderr
        assert f"trax run attach {name}" in launched.stderr

        listed = _trax("run", "ls")
        assert any(
            line.split()[:2] == [name, "running"] for line in listed.stdout.splitlines()
        ), listed.stdout

        assert _trax("run", "send", name, "from", "send").returncode == 0
        _wait_for_log(name, b"ECHO:from send")

        attached = _trax(
            "run",
            "attach",
            name,
            stdin=b"typed\n",
            detach_after=b"ECHO:typed",
        )
        assert attached.returncode == 0, attached.stderr
        assert "still running" in attached.stderr
        assert b"host up" in attached.stdout_bytes
        assert b"ECHO:typed" in attached.stdout_bytes

        # Reattaching is resuming: the replay shows what happened before.
        again = _trax("run", "attach", name, stdin=b"", detach_after=b"ECHO:typed")
        assert again.returncode == 0, again.stderr

        stopped = _trax("run", "stop", name)
        assert stopped.returncode == 0, stopped.stderr
        assert "stopped (status 143)" in stopped.stderr
        final = _trax("run", "ls")
        assert any(
            line.split()[:3] == [name, "exited", "143"]
            for line in final.stdout.splitlines()
        ), final.stdout
        log = _trax("run", "log", name)
        assert log.stdout.index("host up") < log.stdout.index("ECHO:from send")
        assert log.stdout.index("ECHO:from send") < log.stdout.index("ECHO:typed")

    def test_a_host_that_cannot_start_reports_why(self) -> None:
        launched = _trax(
            "run",
            "--detach",
            "--no-sync",
            "--name",
            "broken",
            "sh",
            "--",
            "definitely-not-a-real-binary-xyz",
        )
        assert launched.returncode == 1
        assert "failed to start" in launched.stderr
        assert "definitely-not-a-real-binary-xyz not found in PATH" in launched.stderr
        assert "exited 1" in _trax("run", "ls").stdout


class _Completed:
    """A finished CLI invocation: its status and both streams."""

    def __init__(self, returncode: int, stdout: bytes, stderr: bytes) -> None:
        self.returncode = returncode
        self.stdout_bytes = stdout
        self.stdout = stdout.decode(errors="replace")
        self.stderr = stderr.decode(errors="replace")


# ``-m`` resolves the package from the working directory, so the CLI runs from the root
# THIS checkout's package was imported from -- not wherever pytest was started.
def _trax(
    *argv: str,
    stdin: bytes | None = None,
    detach_after: bytes | None = None,
) -> _Completed:
    """Run the ``trax`` CLI; with ``detach_after``, detach once it shows."""
    package = detach.__name__.rsplit(".", 2)[0]
    root = Path(detach.__file__).parents[detach.__name__.count(".")]
    process = subprocess.Popen(  # noqa: S603 -- fixed interpreter and module; test argv.
        [sys.executable, "-m", package, *argv],
        cwd=root,
        stdin=subprocess.PIPE if detach_after is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if detach_after is None:
        out, err = process.communicate(timeout=60)
        return _Completed(process.returncode, out, err)
    return _drive_attach(process, stdin or b"", detach_after)


def _drive_attach(
    process: subprocess.Popen[bytes],
    typed: bytes,
    detach_after: bytes,
) -> _Completed:
    """Type into an attach, wait to see ``detach_after``, then detach."""
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(typed)
    process.stdin.flush()
    seen = bytearray()
    os.set_blocking(process.stdout.fileno(), False)
    deadline = time.monotonic() + 30
    while detach_after not in seen and time.monotonic() < deadline:
        chunk = process.stdout.read()
        seen += chunk or b""
        time.sleep(0.05)
    process.stdin.write(b"\x1c")
    process.stdin.flush()
    process.stdin.close()
    _ = process.wait(timeout=30)
    os.set_blocking(process.stdout.fileno(), True)
    seen += process.stdout.read()
    assert process.stderr is not None
    return _Completed(process.returncode, bytes(seen), process.stderr.read())


def _wait_for_log(name: str, needle: bytes) -> None:
    """Wait until ``name``'s scrollback holds ``needle``."""
    scrollback = detach._paths(name) / "scrollback.log"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if scrollback.exists() and needle in scrollback.read_bytes():
            return
        time.sleep(0.05)
    raise AssertionError(f"{needle!r} never reached {scrollback}")


def _record(
    root: Path,
    *,
    name: str = "h",
    state: str = "running",
    pid: int = 1,
    address: Path | None = None,
    exit_code: int | None = None,
) -> HostRecord:
    """Return a host record with test defaults."""
    return HostRecord(
        name=name,
        command="sh -- true",
        cwd=str(root),
        pid=pid,
        address=str(address or root / "absent.sock"),
        started="2026-10-03T00:00:00+00:00",
        state=state,
        exit_code=exit_code,
    )


@contextlib.contextmanager
def _listening(path: Path) -> Generator[Path]:
    """Listen on a short alias of ``path`` for the duration of a block."""
    address = socket_address(path)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(address))
        listener.listen(1)
        yield address
    finally:
        listener.close()
        address.unlink(missing_ok=True)


def _dead_pid() -> int:
    """Return the pid of a process that has already exited and been reaped."""
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    _ = finished.wait()
    return finished.pid


def _never(spec: object) -> int:
    """Fail: the refusal must stop the launch before ``start`` runs."""
    raise AssertionError(f"launched despite a running host: {spec}")


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
