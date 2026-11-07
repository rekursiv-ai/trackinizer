"""Tests for the entry point that serves, delegates, or runs in-process."""

from __future__ import annotations

from typing import TYPE_CHECKING

import socketserver
import subprocess
import threading
import time

import pytest

from trackinizer.trax import cli
from trackinizer.trax.daemon import entry
from trackinizer.trax.daemon.client import DaemonRequestLostError, _accepts
from trackinizer.trax.daemon.protocol import Response, socket_path
from trackinizer.trax.daemon.server import _Server


if TYPE_CHECKING:
    from collections.abc import Sequence


@pytest.fixture(autouse=True)
def no_ambient_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrub an exported ``TRACKINIZER_TOKEN``.

    With ``TRACKINIZER_URL`` it forces every call in-process; alone it does not.
    """
    monkeypatch.delenv("TRACKINIZER_TOKEN", raising=False)


def test_an_edit_while_the_daemon_starts_restarts_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A daemon must not vouch for an edit that landed while it imported.

    It imports the CLI for a few hundred milliseconds after its caller decided
    to spawn it. Fingerprinting the tree after those imports would vouch for an
    edit made during them -- the daemon would serve the code from before the
    edit under the version from after it, until the next edit. Spawned with the
    fingerprint its caller took first, it answers stale instead, and the
    caller runs in-process.
    """
    monkeypatch.delenv("TRAX_NO_DAEMON", raising=False)
    monkeypatch.setattr(subprocess, "Popen", _refuse_spawn)
    ran_in_process: list[Sequence[str]] = []
    monkeypatch.setattr(cli, "main", ran_in_process.append)
    monkeypatch.setattr(_Server, "serve_forever", _serve_forever_quickly)
    daemon = threading.Thread(
        target=entry.main,
        args=(["--__serve", "fingerprint-taken-before-the-edit"],),
        daemon=True,
    )
    daemon.start()
    deadline = time.monotonic() + 30
    while not _accepts(socket_path()):
        assert time.monotonic() < deadline, "the daemon never accepted"
        time.sleep(0.01)

    entry.main(["help"])

    daemon.join(timeout=5)
    assert ran_in_process == [["help"]], "the daemon served code older than its tree"
    assert not daemon.is_alive(), "a daemon serving stale code kept running"


def test_a_daemons_answer_is_printed_and_its_exit_code_returned(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("TRAX_NO_DAEMON", raising=False)
    sent: list[Sequence[str]] = []

    def answered(argv: Sequence[str], *, source_version: str) -> Response:
        assert source_version
        sent.append(argv)
        return Response(stdout="out\n", stderr="err\n", exit_code=3)

    monkeypatch.setattr(entry, "delegate", answered)
    monkeypatch.setattr(cli, "main", _never_in_process)

    assert entry.main(["issue", "list"]) == 3

    assert sent == [["issue", "list"]]
    assert capsys.readouterr() == ("out\n", "err\n")


def test_a_lost_answer_is_reported_and_never_rerun_in_process(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The daemon may have applied the command; a rerun would duplicate it."""
    monkeypatch.delenv("TRAX_NO_DAEMON", raising=False)

    def lost(argv: Sequence[str], *, source_version: str) -> Response:
        del argv, source_version
        raise DaemonRequestLostError("the daemon hung up mid-reply")

    monkeypatch.setattr(entry, "delegate", lost)
    monkeypatch.setattr(cli, "main", _never_in_process)

    assert entry.main(["issue", "list"]) == 75

    assert capsys.readouterr().err == "trax: the daemon hung up mid-reply\n"


@pytest.mark.parametrize("value", ["1", "true", "yes"])
def test_the_escape_hatch_runs_every_command_in_process(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("TRAX_NO_DAEMON", value)
    monkeypatch.setattr(entry, "delegate", _never_delegated)
    ran: list[Sequence[str]] = []
    monkeypatch.setattr(cli, "main", ran.append)

    _ = entry.main(["issue", "list"])

    assert ran == [["issue", "list"]]


def _never_in_process(argv: Sequence[str]) -> int:
    raise AssertionError(f"ran in-process: {argv}")


def _never_delegated(argv: Sequence[str], *, source_version: str) -> Response:
    del source_version
    raise AssertionError(f"delegated: {argv}")


def _serve_forever_quickly(server: _Server, poll_interval: float = 0.5) -> None:
    """Notice the stale daemon's shutdown in 10 ms rather than its 1 s poll."""
    del poll_interval
    socketserver.BaseServer.serve_forever(server, poll_interval=0.01)


def _refuse_spawn(*args: object, **kwargs: object) -> None:
    """Fail a spawn as the OS would: the test's daemon is the only one."""
    del args, kwargs
    raise OSError("spawn refused by test")


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
