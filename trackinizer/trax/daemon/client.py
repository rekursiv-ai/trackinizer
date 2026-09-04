"""Thin connect-or-spawn client for traxd.

Must not reach the CLI's import graph -- see this package's ``__init__`` for
why, and ``protocol_test`` for the check that enforces it.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import os
import socket
import sys
import time

from trackinizer.trax.daemon.protocol import (
    FORWARDED_ENV,
    PROTOCOL_VERSION,
    Request,
    Response,
    read_frame,
    socket_address,
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

_SPAWN_TIMEOUT_SEC: Final = 5.0
"""Cap on waiting for a spawned daemon to accept connections. Past this the
CLI runs the command in-process rather than leaving the user at a hung
prompt; nothing has been delivered yet, so running locally is safe."""

_STDIN_SENTINEL: Final = "-"
"""``field to -`` reads the value from stdin, which the daemon does not share."""

# Operators after which a bare ``-`` is the stdin sentinel rather than an
# ordinary value. ``parser._parse_scalar_action`` only honors it after ``to``.
_VALUE_OPERATORS: Final[frozenset[str]] = frozenset({"to"})

# Verbs that must run in the calling process. ``run`` spawns a CLI on a PTY
# whose master fd it holds and mirrors the terminal both ways, so it cannot
# execute in a daemon that owns neither.
_LOCAL_ONLY_VERBS: Final[frozenset[str]] = frozenset({"run"})

# Global flags that take a separate value token, so the scan for the verb
# knows to skip past it. Mirrors ``cli._VALUE_FLAGS``.
_VALUE_FLAGS: Final[frozenset[str]] = frozenset({"--profile", "--host", "--port"})


class DaemonRequestLostError(Exception):
    """A request reached the daemon but its response did not come back.

    The daemon runs the verb BEFORE it replies, so the command may well have
    applied a write. Re-running it in-process would mint a fresh idempotency
    key and duplicate the effect -- a second row, a second edge, a second cost
    delta. Losing the answer is a failure to report, never a licence to retry.
    """


def should_delegate(argv: Sequence[str]) -> bool:
    """Whether ``argv`` may run in the daemon rather than this process.

    Refuses the daemon's own serve flag, anything that SPAWNS a CLI on a PTY,
    and any command whose value is the ``-`` stdin sentinel. Each is judged by
    POSITION rather than by presence, so a row whose title happens to be "run"
    still gets the daemon.
    """
    if SERVE_FLAG in argv:
        return False
    if _spawns_a_terminal(argv):
        return False
    return not _reads_stdin(argv)


def delegate(
    argv: Sequence[str],
    *,
    socket_override: Path | None = None,
    source_version: str,
    spawn: bool = True,
) -> Response | None:
    """Run ``argv`` on the daemon, or return ``None`` to run it in-process.

    ``None`` means nothing was delivered, so the caller may safely run the
    command itself: no daemon listening, a stale socket file, a spawn that
    never came up, or a daemon serving older source.

    Args:
      argv: The CLI arguments, already stripped of the program name.
      socket_override: Use this socket path instead of the default (tests).
      source_version: Fingerprint of the caller's source tree; a daemon
        reporting a different one is stale and is not used.
      spawn: Whether to start a daemon when none is listening.

    Returns:
      response: The daemon's answer, or ``None`` to run in-process.

    Raises:
      DaemonRequestLostError: The request was delivered but the response was not
        returned. The command may have taken effect, so it must not be
        retried here.

    """
    try:
        path = (
            socket_address(socket_override)
            if socket_override is not None
            else socket_path()
        )
    except OSError:
        # Delegation is optional and nothing has been delivered. An unusable
        # local socket address must leave the original in-process CLI working.
        return None
    response = _try_once(argv, path, source_version)
    if response is not None and response.exit_code != STALE_EXIT_CODE:
        return response
    if response is not None:
        # Stale daemon: it is shutting itself down, but this invocation must
        # not wait for the replacement to warm up. Run in-process; the next
        # one gets the fresh daemon.
        return None
    if not spawn or not _spawn(path):
        return None
    return _try_once(argv, path, source_version)


def _spawns_a_terminal(argv: Sequence[str]) -> bool:
    """Whether ``argv`` will put a CLI on a PTY this process must own.

    TWO spellings, and the second is why this is not a verb lookup:

    * ``trax run claude`` -- ``run`` in verb position.
    * ``trax agentsession 42 run codex`` -- the RESUME tail, whose verb is
      ``agentsession``. Judging by the verb alone delegated it, and the daemon
      then spawned the CLI on ITS terminal: measured, the child ran on the
      daemon's tty while the user's shell blocked on the socket forever, with
      a live CLI waiting for input on a terminal nobody was attached to.

    The tail is recognized by ``run`` FOLLOWED BY a target, which is what
    distinguishes it from the word appearing as a value (``title to run``) --
    refusing on presence alone would make an ordinary edit forfeit the daemon.
    """
    if _verb(argv) in _LOCAL_ONLY_VERBS:
        return True
    return any(
        token.lower() in _LOCAL_ONLY_VERBS
        and index + 1 < len(argv)
        and not argv[index + 1].startswith("-")
        for index, token in enumerate(argv)
        # Past the verb: a leading ``run`` is the case above, and the tail
        # always sits after a kind and its ref.
        if index > 0
    )


def _verb(argv: Sequence[str]) -> str:
    """The verb token: the first argument past the global-flag prefix."""
    index = 0
    while index < len(argv):
        token = argv[index]
        if not token.startswith("--"):
            return token.lower()
        if "=" not in token and token in _VALUE_FLAGS:
            index += 1
        index += 1
    return ""


def _reads_stdin(argv: Sequence[str]) -> bool:
    """Whether ``argv`` carries the ``-`` sentinel in a value position."""
    return any(
        token == _STDIN_SENTINEL and argv[index - 1].lower() in _VALUE_OPERATORS
        for index, token in enumerate(argv)
        if index > 0
    )


def _try_once(argv: Sequence[str], path: Path, source_version: str) -> Response | None:
    """One connect-send-receive round trip.

    Returns ``None`` only while nothing has been delivered. Once the request
    is on the wire the outcome is the daemon's answer or
    :class:`DaemonRequestLostError` -- never a silent ``None`` that would invite
    the caller to run a possibly-applied command a second time.
    """
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        try:
            # Connecting is the only phase with a deadline. The verb itself
            # has no bound -- a paging read or a retried write legitimately
            # runs for seconds -- and timing it out here would abandon a
            # request the daemon is still executing.
            conn.settimeout(_SPAWN_TIMEOUT_SEC)
            conn.connect(str(path))
        except OSError:
            # Nothing sent: no daemon, or a socket file left by a killed one.
            return None
        conn.settimeout(None)
        try:
            write_frame(conn, _request(argv, source_version).to_json())
        except OSError:
            return None
        try:
            return Response.from_json(read_frame(conn))
        except (OSError, ValueError, ConnectionError) as err:
            raise DaemonRequestLostError(
                f"the daemon accepted this command but did not report its result: {err}"
            ) from err
    finally:
        conn.close()


def _request(argv: Sequence[str], source_version: str) -> Request:
    """Snapshot the ambient state the daemon cannot observe for itself."""
    try:
        isatty = sys.stdout.isatty()
    except (AttributeError, ValueError):
        isatty = False
    if isatty:
        # 2.2ms, and only a terminal ever needs the width -- the piped and
        # daemon-spawned paths that dominate agent traffic would pay it for a
        # value they discard.
        import shutil  # noqa: PLC0415

        columns = shutil.get_terminal_size(fallback=(120, 24)).columns
    else:
        columns = 0
    return Request(
        argv=tuple(argv),
        cwd=str(Path.cwd()),
        env={
            name: value
            for name in FORWARDED_ENV
            if (value := os.environ.get(name)) is not None
        },
        isatty=isatty,
        columns=columns,
        protocol_version=PROTOCOL_VERSION,
        source_version=source_version,
    )


def _spawn(path: Path) -> bool:
    """Start a detached daemon and wait until it ACCEPTS, returning success.

    Spawns ``sys.executable -m <trax package>`` rather than re-running
    ``bin/trax``: that entry point is a polyglot shim that execs ``uv run``,
    which would re-resolve the project on every spawn. This process is
    already inside the resolved venv.

    ``start_new_session`` detaches the daemon from the caller's process group
    so it survives the shell that started it and does not hold the terminal
    open -- without it, the invoking shell hangs on exit.

    Readiness is a successful connect, not the socket file appearing: a stale
    file from a killed daemon exists immediately, and waiting on existence
    would report ready before anything is listening.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # 3.2ms, paid only on the once-per-daemon-lifetime spawn. Every other
    # invocation connects to a daemon that is already up.
    import subprocess  # noqa: PLC0415

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
    deadline = time.monotonic() + _SPAWN_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if _accepts(path):
            return True
        time.sleep(0.01)
    return False


def _accepts(path: Path) -> bool:
    """Whether a daemon is accepting connections on ``path``."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.2)
        probe.connect(str(path))
    except OSError:
        return False
    else:
        return True
    finally:
        probe.close()
