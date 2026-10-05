"""Tests for the entry point that serves, delegates, or runs in-process."""

from __future__ import annotations

from typing import TYPE_CHECKING

import socketserver
import subprocess
import threading
import time

from trackinizer.trax import cli
from trackinizer.trax.daemon import entry
from trackinizer.trax.daemon.client import _accepts
from trackinizer.trax.daemon.protocol import socket_path
from trackinizer.trax.daemon.server import _Server


if TYPE_CHECKING:
    from collections.abc import Sequence

    import pytest


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


def _serve_forever_quickly(server: _Server, poll_interval: float = 0.5) -> None:
    """Notice the stale daemon's shutdown in 10 ms rather than its 1 s poll."""
    del poll_interval
    socketserver.BaseServer.serve_forever(server, poll_interval=0.01)


def _refuse_spawn(*args: object, **kwargs: object) -> None:
    """Fail a spawn as the OS would: the test's daemon is the only one."""
    del args, kwargs
    raise OSError("spawn refused by test")


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
