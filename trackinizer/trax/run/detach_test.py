"""Tests for ``trax run --detach`` and the commands that reach a host by name."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Self

import contextlib
import json
import os
import socket
import subprocess
import sys
import threading
import time

from treekle import from_plain, json_loads, to_plain

import pytest

from trackinizer.trax.daemon.protocol import socket_address
from trackinizer.trax.run import detach
from trackinizer.trax.run.detach import HeadlessSpec, HostRecord, launch, main
from trackinizer.trax.run.headless import Reply, Request


if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping

    from trackinizer.lib.posix.host import HostSpec


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

    def test_ls_lists_the_live_hosts_one_line_each_and_all_lists_every_host(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # A host directory with no record yet, listed first, is passed over.
        detach._paths("a-new").mkdir(parents=True)
        alpha = detach._paths("alpha")
        alpha.mkdir(parents=True)
        detach._write_record(
            alpha,
            _record(alpha, name="alpha", state="exited", exit_code=0),
        )
        beta = detach._paths("beta")
        beta.mkdir(parents=True)
        # This test's own pid: a starting host whose process is still there, running
        # a script that spans lines.
        detach._write_record(
            beta,
            replace(
                _record(beta, name="beta", state="starting", pid=os.getpid()),
                command="bash -c 'echo a\necho b'",
            ),
        )

        assert main(["ls"]) == 0
        live = capsys.readouterr().out.splitlines()[1:]
        assert main(["ls", "--all"]) == 0
        every = capsys.readouterr().out.splitlines()[1:]

        assert [line.split()[:2] for line in live] == [["beta", "starting"]]
        assert live[0].endswith("bash -c 'echo a echo b'")
        assert [line.split()[:2] for line in every] == [
            ["alpha", "exited"],
            ["beta", "starting"],
        ]

    def test_ls_reads_the_directory_hosts_are_made_in(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Compared as text: a case-insensitive disk finds hosts under any spelling."""
        made = detach._paths("alpha").parent
        asked: list[Path] = []
        monkeypatch.setattr(Path, "is_dir", _noting_is_dir(asked, Path.is_dir))
        assert main(["ls"]) == 0
        assert made in asked

    def test_ls_all_lists_each_host(self, capsys: pytest.CaptureFixture[str]) -> None:
        root = detach._paths("alpha")
        root.mkdir(parents=True)
        detach._write_record(
            root,
            _record(root, name="alpha", state="exited", exit_code=0),
        )
        assert main(["ls", "--all"]) == 0
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
            "ls List the live hosts and their state.",
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
            with pytest.raises(SystemExit) as refused:
                _ = launch("delta", command="sh", start=_never)
            assert str(refused.value) == (
                "trax run: host 'delta' is running; "
                "stop it (trax run stop delta) or choose another --name"
            )

    def test_launch_repeats_what_the_host_said_before_it_accepted(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The host loads a chat after the fork; its line still reaches the person."""
        assert launch("eta", command="sh", start=_say_then_accept) == 0
        said, up = capsys.readouterr().err.splitlines()
        assert said == "[trax run] loaded 2 records"
        assert (
            up == "[trax run] eta is running detached; attach with: trax run attach eta"
        )


class TestAHeadlessHostIsRecordedAndFound:
    """A chat's run has no terminal until taken over; its record says how to reach it."""

    def test_the_host_is_told_where_the_control_socket_goes(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert launch("theta", command="sh", start=_say_the_spec) == 0
        said = capsys.readouterr().err.splitlines()[0]
        control = socket_address(detach._paths("theta") / "control.sock")
        assert said == f"[trax run] control {control} resume ''"

    def test_the_record_names_the_machine_and_the_socket(self) -> None:
        assert launch("iota", command="sh", start=_say_the_spec) == 0
        record = detach._read_record(detach._paths("iota"))
        assert record is not None
        assert record.machine == socket.gethostname()
        assert record.control == str(
            socket_address(detach._paths("iota") / "control.sock"),
        )

    def test_a_host_listening_on_its_control_socket_alone_is_running(
        self,
        tmp_path: Path,
    ) -> None:
        with _Control(tmp_path / "c", Reply(ok=True)) as control:
            record = _record(tmp_path, control=control.address)
            assert detach._state(record) == "running"

    def test_a_control_socket_nothing_listens_on_does_not_make_a_host_run(
        self,
        tmp_path: Path,
        live_child: subprocess.Popen[bytes],
    ) -> None:
        record = _record(
            tmp_path,
            state="running",
            pid=live_child.pid,
            control=tmp_path / "dead.sock",
        )
        assert detach._state(record) == "exiting"

    def test_a_record_from_before_headless_runs_still_reads(
        self,
        tmp_path: Path,
    ) -> None:
        written = from_plain(to_plain(_record(tmp_path)), dict[str, object])
        old = {
            key: value
            for key, value in written.items()
            if key not in {"machine", "control"}
        }
        (tmp_path / "host.json").write_text(json.dumps(old))
        record = detach._read_record(tmp_path)
        assert record is not None
        assert (record.machine, record.control) == ("", "")


class TestTakingAHeadlessRunOver:
    """``trax run attach`` stops the driver, then drives the CLI's own terminal."""

    def test_the_run_is_asked_to_hand_over_before_the_terminal_is_attached(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        steps: list[str] = []
        with _headless(tmp_path, monkeypatch, steps, Reply(ok=True)) as control:
            assert main(["attach", "kappa"]) == 0
            assert [asked.op for asked in control.asked] == ["takeover"]
        assert steps == ["takeover", "attach"]

    def test_it_says_how_the_chat_gets_the_cli_back_once_the_terminal_exits(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with _headless(tmp_path, monkeypatch, [], Reply(ok=True), status=0):
            assert main(["attach", "kappa"]) == 0
        assert capsys.readouterr().err == (
            "\n[trax run] kappa exited with status 0\n"
            "[trax run] Chat stays taken over; use Reattach to Chat in the chat or the Console to give it back.\n"
        )

    def test_the_status_the_terminal_ended_with_is_the_commands(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with _headless(tmp_path, monkeypatch, [], Reply(ok=True), status=3):
            assert main(["attach", "kappa"]) == 3

    def test_a_terminal_left_with_ctrl_backslash_leaves_the_cli_running(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with _headless(tmp_path, monkeypatch, [], Reply(ok=True)):
            assert main(["attach", "kappa"]) == 0
        assert capsys.readouterr().err == (
            "\n[trax run] detached from kappa; it is still running\n"
            "[trax run] Chat stays taken over; use Reattach to Chat in the chat or the Console to give it back.\n"
        )

    def test_a_run_that_will_not_hand_over_is_not_attached(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        steps: list[str] = []
        refusal = Reply(ok=False, reason="claude could not start")
        with (
            _headless(tmp_path, monkeypatch, steps, refusal),
            pytest.raises(SystemExit, match="cannot be taken over: claude could"),
        ):
            _ = main(["attach", "kappa"])
        assert steps == ["takeover"]

    def test_a_pty_host_is_attached_as_it_always_was(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        steps: list[str] = []
        _fake_attach(monkeypatch, steps, status=None)
        root = detach._paths("lambda")
        root.mkdir(parents=True)
        with _listening(root / "s") as address:
            detach._write_record(
                root,
                _record(root, name="lambda", address=address, machine=_HERE),
            )
            assert main(["attach", "lambda"]) == 0
        assert steps == ["attach"]
        said = capsys.readouterr().err
        assert "detached from lambda; it is still running" in said
        assert "Reattach" not in said

    def test_a_host_of_another_machine_is_refused_with_its_name(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        steps: list[str] = []
        with (
            _headless(
                tmp_path,
                monkeypatch,
                steps,
                Reply(ok=True),
                machine="elsewhere",
            ),
            pytest.raises(SystemExit) as refused,
        ):
            _ = main(["attach", "kappa"])
        said = str(refused.value)
        assert "runs on elsewhere" in said
        assert _HERE in said
        assert steps == []

    @pytest.mark.parametrize("command", ["send", "stop"])
    def test_stop_and_send_refuse_another_machines_host_too(
        self,
        command: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        argv = [command, "kappa", *(["hi"] if command == "send" else [])]
        with (
            _headless(
                tmp_path,
                monkeypatch,
                [],
                Reply(ok=True),
                machine="elsewhere",
            ),
            pytest.raises(SystemExit, match="runs on elsewhere"),
        ):
            _ = main(argv)

    def test_a_record_that_names_no_machine_is_attached_anywhere(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        steps: list[str] = []
        with _headless(tmp_path, monkeypatch, steps, Reply(ok=True), machine=""):
            assert main(["attach", "kappa"]) == 0
        assert steps == ["takeover", "attach"]


class TestReachingAHeadlessRun:
    """``trax run stop|send`` and a second launch reach the run on its control socket."""

    def test_stop_ends_the_run_through_its_socket_and_waits_for_the_process(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        stopped: list[Path] = []

        async def stop_terminal(address: Path) -> int | None:
            stopped.append(address)
            return 0

        monkeypatch.setattr(detach, "stop", stop_terminal)
        with _headless(tmp_path, monkeypatch, [], Reply(ok=True)) as control:
            assert main(["stop", "kappa"]) == 0
            assert [asked.op for asked in control.asked] == ["stop"]
        assert stopped == []
        assert capsys.readouterr().err == "[trax run] kappa stopped\n"

    def test_a_host_with_a_terminal_of_its_own_is_stopped_through_it(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        stopped: list[Path] = []

        async def stop_terminal(address: Path) -> int | None:
            stopped.append(address)
            return 143

        monkeypatch.setattr(detach, "stop", stop_terminal)
        root = detach._paths("omicron")
        root.mkdir(parents=True)
        with _listening(root / "s") as address:
            detach._write_record(
                root,
                _record(root, name="omicron", address=address, pid=_dead_pid()),
            )
            assert main(["stop", "omicron"]) == 0
        assert stopped == [address]
        assert capsys.readouterr().err == "[trax run] omicron stopped (status 143)\n"

    def test_send_types_through_the_socket(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with _headless(tmp_path, monkeypatch, [], Reply(ok=True)) as control:
            assert main(["send", "kappa", "two", "words"]) == 0
            assert [(asked.op, asked.text) for asked in control.asked] == [
                ("send", "two words"),
            ]

    @pytest.mark.parametrize(("taken", "code"), [(True, 0), (False, 1)])
    def test_a_host_with_a_terminal_of_its_own_takes_a_line_through_it(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        taken: bool,
        code: int,
    ) -> None:
        async def submit_line(address: Path, *, text: str) -> bool:
            del address
            return taken and text == "two words"

        monkeypatch.setattr(detach, "submit", submit_line)
        root = detach._paths("psi")
        root.mkdir(parents=True)
        detach._write_record(root, _record(root, name="psi"))
        with _listening(root / "s") as address:
            detach._write_record(root, _record(root, name="psi", address=address))
            assert main(["send", "psi", "two", "words"]) == code
        assert capsys.readouterr().err == (
            "" if taken else "trax run: psi did not take the message; it is exiting\n"
        )

    def test_a_host_that_will_not_exit_is_said_to_be_still_closing(
        self,
        live_child: subprocess.Popen[bytes],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        async def stop_terminal(address: Path) -> int | None:
            del address
            return 0

        monkeypatch.setattr(detach, "stop", stop_terminal)
        monkeypatch.setattr(detach, "_STOP_TIMEOUT_SEC", 0.05)
        root = detach._paths("omega")
        root.mkdir(parents=True)
        detach._write_record(
            root,
            _record(root, name="omega", pid=live_child.pid),
        )
        assert main(["stop", "omega"]) == 1
        assert capsys.readouterr().err == (
            f"trax run: omega stopped its CLI but the host (pid {live_child.pid}) "
            "is still closing after 0s\n"
        )

    def test_a_line_the_run_refuses_is_said_and_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        refusal = Reply(ok=False, reason="taken over in a terminal")
        with _headless(tmp_path, monkeypatch, [], refusal):
            assert main(["send", "kappa", "hi"]) == 1
        assert capsys.readouterr().err == (
            "trax run: kappa did not take the message: taken over in a terminal\n"
        )


class TestALaunchOfATakenOverName:
    """A taken-over run is replaced by the launch that gives the chat its CLI back."""

    def test_the_run_is_ended_and_the_new_one_resumes_its_session(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = detach._paths("mu")
        root.mkdir(parents=True)
        replies = {
            "state": Reply(ok=True, state="taken_over", cli="claude", session="sess-1"),
            "stop": Reply(ok=True),
        }
        with _Control(tmp_path / "c", replies) as control:
            detach._write_record(
                root,
                _record(
                    root,
                    name="mu",
                    control=control.address,
                    pid=_dead_pid(),
                    command="claude",
                ),
            )
            assert launch("mu", command="claude", start=_say_the_spec) == 0
            assert [asked.op for asked in control.asked] == ["state", "stop"]
        assert capsys.readouterr().err.splitlines()[0].endswith("resume 'sess-1'")

    def test_a_run_that_is_not_taken_over_keeps_its_name(self, tmp_path: Path) -> None:
        root = detach._paths("nu")
        root.mkdir(parents=True)
        replies = {"state": Reply(ok=True, state="running"), "stop": Reply(ok=True)}
        with _Control(tmp_path / "c", replies) as control:
            detach._write_record(
                root,
                _record(root, name="nu", control=control.address, command="claude"),
            )
            with pytest.raises(SystemExit) as refused:
                _ = launch("nu", command="claude", start=_never)
            assert [asked.op for asked in control.asked] == ["state"]
        said = str(refused.value)
        assert "'nu' is running (the chat is not taken over)" in said
        assert "trax run stop nu" in said

    def test_a_taken_over_run_on_its_way_out_is_waited_for(
        self,
        live_child: subprocess.Popen[bytes],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The server turns the old run away as it opens the new one, so both race."""
        root = detach._paths("xi")
        root.mkdir(parents=True)
        detach._write_record(
            root,
            _record(
                root,
                name="xi",
                pid=live_child.pid,
                session="sess-2",
                command="claude",
            ),
        )
        ending = threading.Timer(0.03, _end, args=(live_child,))
        ending.start()
        began = time.monotonic()
        try:
            assert launch("xi", command="claude", start=_say_the_spec) == 0
            assert time.monotonic() - began < 0.5
        finally:
            ending.join()
        assert capsys.readouterr().err.splitlines()[0].endswith("resume 'sess-2'")

    def test_a_run_that_will_not_go_keeps_its_name(
        self,
        live_child: subprocess.Popen[bytes],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(detach, "_STOP_TIMEOUT_SEC", 0.05)
        root = detach._paths("pi")
        root.mkdir(parents=True)
        detach._write_record(
            root,
            _record(
                root,
                name="pi",
                pid=live_child.pid,
                session="sess-3",
                command="claude",
            ),
        )
        with pytest.raises(SystemExit, match="'pi' is still closing"):
            _ = launch("pi", command="claude", start=_never)

    @pytest.mark.parametrize(
        ("command", "resumed"),
        [("claude", "sess-4"), ("claude --model opus", "sess-4"), ("codex", "")],
    )
    def test_a_run_that_ended_taken_over_is_resumed_by_the_same_cli_alone(
        self,
        capsys: pytest.CaptureFixture[str],
        command: str,
        resumed: str,
    ) -> None:
        root = detach._paths("rho")
        root.mkdir(parents=True)
        detach._write_record(
            root,
            _record(
                root,
                name="rho",
                state="exited",
                exit_code=0,
                session="sess-4",
                command="claude",
                ended=_ago(30),
            ),
        )
        assert launch("rho", command=command, start=_say_the_spec) == 0
        assert capsys.readouterr().err.splitlines()[0].endswith(f"resume {resumed!r}")

    def test_a_run_that_started_by_itself_and_has_not_answered_keeps_its_name(
        self,
        live_child: subprocess.Popen[bytes],
    ) -> None:
        root = detach._paths("phi")
        root.mkdir(parents=True)
        detach._write_record(
            root,
            _record(root, name="phi", state="starting", pid=live_child.pid),
        )
        with pytest.raises(SystemExit) as refused:
            _ = launch("phi", command="claude", start=_never)
        assert str(refused.value) == (
            "trax run: host 'phi' is starting; "
            "stop it (trax run stop phi) or choose another --name"
        )

    def test_a_run_started_with_arguments_is_still_the_same_cli(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = detach._paths("chi")
        root.mkdir(parents=True)
        detach._write_record(
            root,
            _record(
                root,
                name="chi",
                state="exited",
                exit_code=0,
                session="sess-7",
                command="claude --model opus",
                ended=_ago(30),
            ),
        )
        assert launch("chi", command="claude", start=_say_the_spec) == 0
        assert capsys.readouterr().err.splitlines()[0].endswith("resume 'sess-7'")

    def test_a_session_is_recent_until_the_window_has_passed(self) -> None:
        ended = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
        record = _record(Path("/h"), ended=ended.isoformat(timespec="seconds"))
        window = timedelta(seconds=120)

        assert detach._recent(record, now=ended + window - timedelta(seconds=1))
        assert not detach._recent(record, now=ended + window)
        assert detach._recent(_record(Path("/h")), now=ended + timedelta(days=9))

    def test_a_run_that_ended_long_ago_is_not_resumed(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The chat may have been answered by another provider since."""
        root = detach._paths("upsilon")
        root.mkdir(parents=True)
        detach._write_record(
            root,
            _record(
                root,
                name="upsilon",
                state="exited",
                exit_code=0,
                session="sess-6",
                command="claude",
                ended=_ago(3_600),
            ),
        )
        assert launch("upsilon", command="claude", start=_say_the_spec) == 0
        assert capsys.readouterr().err.splitlines()[0].endswith("resume ''")

    def test_a_run_that_was_never_taken_over_leaves_nothing_to_resume(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = detach._paths("sigma")
        root.mkdir(parents=True)
        detach._write_record(
            root,
            _record(
                root,
                name="sigma",
                state="exited",
                exit_code=0,
                command="claude",
            ),
        )
        assert launch("sigma", command="claude", start=_say_the_spec) == 0
        assert capsys.readouterr().err.splitlines()[0].endswith("resume ''")

    def test_a_takeover_is_written_into_the_hosts_record_beside_what_it_holds(
        self,
        tmp_path: Path,
    ) -> None:
        detach._write_record(tmp_path, _record(tmp_path, state="running", pid=7))

        detach._taken_over(tmp_path, "sess-8")

        record = detach._read_record(tmp_path)
        assert record is not None
        assert (record.session, record.state, record.pid) == ("sess-8", "running", 7)

    def test_a_takeover_in_a_host_with_no_record_writes_none(
        self,
        tmp_path: Path,
    ) -> None:
        detach._taken_over(tmp_path, "sess-9")

        assert not (tmp_path / "host.json").exists()

    def test_the_takeover_is_kept_in_the_record_through_the_hosts_end(self) -> None:
        assert launch("tau", command="claude", start=_take_over_then_end) == 0
        # The launcher returns once the host is up; the host ends a moment after.
        end = time.monotonic() + 10
        record = detach._read_record(detach._paths("tau"))
        while (record is None or record.state != "exited") and time.monotonic() < end:
            time.sleep(0.01)
            record = detach._read_record(detach._paths("tau"))
        assert record is not None
        assert (record.session, record.state) == ("sess-5", "exited")


def _ago(seconds: int) -> str:
    """Return the time ``seconds`` ago, as a host records when it ended."""
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat(
        timespec="seconds",
    )


def _end(child: subprocess.Popen[bytes]) -> None:
    """End a process and reap it, as a host ends."""
    child.kill()
    _ = child.wait()


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
        final = _trax("run", "ls", "--all")
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
        assert "exited 1" in _trax("run", "ls", "--all").stdout


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
    machine: str = "",
    control: Path | None = None,
    session: str = "",
    command: str = "sh -- true",
    ended: str | None = None,
) -> HostRecord:
    """Return a host record with test defaults."""
    return HostRecord(
        name=name,
        command=command,
        cwd=str(root),
        pid=pid,
        address=str(address or root / "absent.sock"),
        started="2026-10-03T00:00:00+00:00",
        state=state,
        exit_code=exit_code,
        machine=machine,
        control="" if control is None else str(control),
        session=session,
        ended=ended,
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


def _noting_is_dir(
    asked: list[Path],
    is_dir: Callable[[Path], bool],
) -> Callable[[Path], bool]:
    """Return ``is_dir`` that first notes each path it is asked about."""

    def noted(path: Path) -> bool:
        asked.append(path)
        return is_dir(path)

    return noted


# Written to the descriptor, not ``sys.stderr``: the host points descriptor 2 at its
# log, and pytest's capture replaces ``sys.stderr`` with a file of its own.
def _say_then_accept(spec: HostSpec) -> int:
    """Say one line, as a host loading a chat does, then accept and end."""
    _ = os.write(2, b"[trax run] loaded 2 records\n")
    assert spec.on_listening is not None
    spec.on_listening()
    return 0


def _take_over_then_end(spec: HostSpec) -> int:
    """Be taken over on a session, accept, and end."""
    assert isinstance(spec, HeadlessSpec)
    assert spec.on_takeover is not None
    assert spec.on_listening is not None
    spec.on_listening()
    spec.on_takeover("sess-5")
    return 0


def _never(spec: object) -> int:
    """Fail: the refusal must stop the launch before ``start`` runs."""
    raise AssertionError(f"launched despite a running host: {spec}")


def _say_the_spec(spec: HostSpec) -> int:
    """Say where the host was told to listen for control requests, then accept."""
    assert isinstance(spec, HeadlessSpec)
    _ = os.write(
        2,
        f"[trax run] control {spec.control} resume {spec.resume!r}\n".encode(),
    )
    assert spec.on_listening is not None
    spec.on_listening()
    return 0


_HERE = socket.gethostname()


class _Control:
    """A headless run's control socket: it answers each request with one canned reply.

    Every request is noted in :attr:`asked`, and its op in ``steps`` when given, so a
    test can say what was asked before what.
    """

    def __init__(
        self,
        path: Path,
        reply: Reply | Mapping[str, Reply],
        steps: list[str] | None = None,
    ) -> None:
        self.address = socket_address(path)
        self.asked: list[Request] = []
        self._reply = reply
        self._steps = steps
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.settimeout(0.01)
        self._over = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> Self:
        self._listener.bind(str(self.address))
        self._listener.listen(4)
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._over.set()
        self._thread.join(5.0)
        self._listener.close()
        self.address.unlink(missing_ok=True)

    def _serve(self) -> None:
        while not self._over.is_set():
            try:
                connection, _ = self._listener.accept()
            except TimeoutError:
                continue
            with connection, connection.makefile("rwb") as stream:
                line = stream.readline().decode()
                # A liveness probe connects and leaves without a word.
                if not line:
                    continue
                asked = json_loads(line, Request)
                self.asked.append(asked)
                if self._steps is not None:
                    self._steps.append(asked.op)
                reply = (
                    self._reply
                    if isinstance(self._reply, Reply)
                    else self._reply[asked.op]
                )
                stream.write(json.dumps(to_plain(reply)).encode() + b"\n")
                stream.flush()


def _fake_attach(
    monkeypatch: pytest.MonkeyPatch,
    steps: list[str],
    *,
    status: int | None,
) -> None:
    """Make the terminal attach note itself and return ``status``."""

    async def attached(address: Path) -> int | None:
        del address
        steps.append("attach")
        return status

    monkeypatch.setattr(detach, "attach", attached)


# Its process is already gone, so ``stop`` has nothing to wait for, and the terminal
# attach is faked.
@contextlib.contextmanager
def _headless(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    steps: list[str],
    reply: Reply,
    *,
    status: int | None = None,
    machine: str = _HERE,
) -> Generator[_Control]:
    """Record a live headless host named ``kappa`` whose control socket gives ``reply``."""
    _fake_attach(monkeypatch, steps, status=status)
    root = detach._paths("kappa")
    root.mkdir(parents=True)
    with _Control(tmp_path / "c", reply, steps) as control:
        detach._write_record(
            root,
            _record(
                root,
                name="kappa",
                pid=_dead_pid(),
                machine=machine,
                control=control.address,
            ),
        )
        yield control


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
