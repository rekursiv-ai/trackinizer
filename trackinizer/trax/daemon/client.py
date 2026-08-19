"""Thin connect-or-spawn client for traxd.

STDLIB ONLY -- see this package's ``__init__`` for why, and
``protocol_test`` for the check that enforces it.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import os
import shutil
import socket
import subprocess
import sys
import time

from trackinizer.trax.daemon.protocol import (
    FORWARDED_ENV,
    PROTOCOL_VERSION,
    Request,
    Response,
    read_frame,
    socket_path,
    write_frame,
)


_CLI_MODULE: Final = __name__.rsplit(".", maxsplit=2)[0]
"""The ``trax`` package -- this module's own name minus ``daemon.client``.

Derived rather than written literally so the respawn target follows the
package if it is ever moved or renamed."""

SERVE_FLAG: Final = "--__serve"
"""Hidden flag that turns this entry point into the daemon. Underscored and
undocumented: it is an implementation detail of ``trax``, not a verb."""

STALE_EXIT_CODE: Final = 75
"""Daemon's answer when its source predates the caller's. 75 is EX_TEMPFAIL:
a retry (against a fresh daemon) is expected to succeed."""

_CONNECT_TIMEOUT_SEC: Final = 2.0
"""Cap on waiting for a spawned daemon to bind. Past this the CLI runs the
command in-process rather than leaving the user staring at a hung prompt."""

# Verbs that must run in the calling process. ``run`` spawns a CLI on a PTY
# whose master fd it holds and mirrors the terminal both ways, so it cannot
# execute in a daemon that owns neither.
_LOCAL_ONLY_VERBS: Final[frozenset[str]] = frozenset({"run"})


def should_delegate(argv: Sequence[str]) -> bool:
    """Whether ``argv`` may run in the daemon rather than this process.

    Refuses the PTY-owning ``run`` verb, the daemon's own serve flag, and any
    command carrying the ``-`` stdin sentinel (whose value is read from THIS
    process's stdin, which the daemon does not share).
    """
    if not argv:
        return True
    if SERVE_FLAG in argv:
        return False
    if "-" in argv:
        return False
    return not any(word.lower() in _LOCAL_ONLY_VERBS for word in argv)


def delegate(
    argv: Sequence[str],
    *,
    socket: Path | None = None,
    source_version: str,
    spawn: bool = True,
) -> Response | None:
    """Run ``argv`` on the daemon, or return ``None`` to run it in-process.

    ``None`` is the "fall back" signal and covers every failure mode: no
    daemon listening, a stale socket file left by a killed one, a spawn that
    did not come up in time, a daemon serving older source, or any transport
    error. The CLI must always remain usable without a daemon, so no failure
    here is fatal.

    Args:
      argv: The CLI arguments, already stripped of the program name.
      socket: Override the socket path (tests).
      source_version: Fingerprint of the caller's source tree; a daemon
        reporting a different one is stale and is not used.
      spawn: Whether to start a daemon when none is listening.

    Returns:
      response: The daemon's answer, or ``None`` to run in-process.

    """
    path = socket if socket is not None else socket_path()
    response = _try_once(argv, path, source_version)
    if response is not None and response.exit_code != STALE_EXIT_CODE:
        return response
    if response is not None:
        # Stale daemon: it is shutting itself down, but this invocation must
        # not wait for the replacement to warm up. Run in-process; the next
        # one gets the fresh daemon.
        return None
    if not spawn:
        return None
    if not _spawn(path):
        return None
    return _try_once(argv, path, source_version)


def _try_once(argv: Sequence[str], path: Path, source_version: str) -> Response | None:
    """One connect-send-receive round trip, or ``None`` on any failure."""
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError:
        return None
    try:
        conn.settimeout(_CONNECT_TIMEOUT_SEC)
        conn.connect(str(path))
        write_frame(conn, _request(argv, source_version).to_json())
        return Response.from_json(read_frame(conn))
    except (OSError, ValueError, ConnectionError):
        # A stale socket file (daemon killed without unlinking) raises
        # ECONNREFUSED here, which is indistinguishable from "not running"
        # for our purposes: both mean run it in-process.
        return None
    finally:
        conn.close()


def _request(argv: Sequence[str], source_version: str) -> Request:
    """Snapshot the ambient state the daemon cannot observe for itself."""
    try:
        isatty = sys.stdout.isatty()
    except (AttributeError, ValueError):
        isatty = False
    return Request(
        argv=tuple(argv),
        cwd=str(Path.cwd()),
        env={
            name: value
            for name in FORWARDED_ENV
            if (value := os.environ.get(name)) is not None
        },
        isatty=isatty,
        columns=shutil.get_terminal_size(fallback=(120, 24)).columns if isatty else 0,
        stdin="",
        protocol_version=PROTOCOL_VERSION,
        source_version=source_version,
    )


def _spawn(path: Path) -> bool:
    """Start a detached daemon and wait for it to bind, returning success.

    Spawns ``sys.executable -m trackinizer.trax`` rather than re-running
    ``bin/trax``: that entry point is a polyglot shim that execs ``uv run``,
    which would re-resolve the project on every spawn. This process is
    already inside the resolved venv.

    ``start_new_session`` detaches the daemon from the caller's process group
    so it survives the shell that started it and does not hold the terminal
    open -- without it, the invoking shell hangs on exit.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.Popen(  # noqa: S603 -- fixed interpreter and module path.
            [sys.executable, "-m", _CLI_MODULE, SERVE_FLAG],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False
    deadline = time.monotonic() + _CONNECT_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.01)
    return False
