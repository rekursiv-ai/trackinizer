r"""Keep ``trax run`` alive past its terminal: launch detached, attach by name.

``trax run --detach`` hosts the wrapped CLI in a background process no terminal
owns (:class:`~trackinizer.lib.posix.host.Host`), so closing the shell, dropping ssh,
or a launcher exiting leaves the agent running -- the job tmux did for
long-running agents. Capture, sync, and inbound delivery run in that process
exactly as in a foreground run. Each host has a name and a directory,
``state_dir()/rekursiv-ai/trax/run/hosts/<name>/``, holding:

- ``host.json``: what it runs, where it listens, and how it ended.
- ``scrollback.log``: every byte the CLI wrote, in order.
- ``host.log``: the runner's own messages.

These reach a host by name:

- ``trax run ls`` lists hosts and their state.
- ``trax run attach NAME`` drives it from this terminal; Ctrl-\ detaches.
- ``trax run log NAME [--follow]`` prints its scrollback.
- ``trax run send NAME TEXT`` types a message in and presses Enter.
- ``trax run stop NAME`` stops it and waits until it has exited.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, cast

import argparse
import asyncio
import json
import logging
import os
import re
import select
import shutil
import socket
import sys
import time

from trackinizer.lib.custom_json import convert, parse, to_builtins
from trackinizer.lib.posix.attach import attach, stop, submit
from trackinizer.lib.posix.host import HostSpec
from trackinizer.lib.posix.terminal import write_all
from trackinizer.lib.userdirs import state_dir
from trackinizer.trax.daemon.protocol import socket_address


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


__all__ = ["HOST_COMMANDS", "HostRecord", "build_parser", "launch", "main"]

_logger = logging.getLogger(__name__)


HOST_COMMANDS: Final = frozenset({"attach", "log", "ls", "send", "stop"})
"""``trax run`` first words that reach an existing host rather than wrap a CLI."""

# A path component: no separator, no leading dot, and short enough that the
# directory stays far inside every filesystem's 255-byte name limit. ``#`` and
# ``@`` are allowed because routing names (``scientist#2``) make natural host
# names.
_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@#+=:-]{0,127}")

# How long ``--detach`` waits for the host to accept. Opening a synced session
# is a server round trip with retries before the CLI is even spawned, so this is
# generous; a host still starting past it is reported, not killed.
_READY_TIMEOUT_SEC: Final = 120.0

# How long ``stop`` waits for the host process to finish once the CLI is gone:
# its teardown drains capture and closes the session under a 30s deadline of
# its own, and the name is not reusable until it has.
_STOP_TIMEOUT_SEC: Final = 60.0


@dataclass(frozen=True, slots=True, kw_only=True)
class HostRecord:
    """What ``host.json`` says about one host."""

    name: str
    command: str
    """The wrapped command line, for display."""

    cwd: str
    pid: int
    """The host process -- not the CLI, which is its child."""

    address: str
    """The socket it listens on. Recorded, not re-derived: a long state path
    maps to an alias under the temp dir, and a client with a different TMPDIR
    would derive a different one."""

    started: str
    state: str = "starting"
    """``starting``, then ``running`` once it accepts, then ``exited``."""

    exit_code: int | None = None
    ended: str | None = None


def launch(
    name: str,
    *,
    command: str,
    start: Callable[[HostSpec], int],
) -> int:
    """Start ``start`` in a detached host named ``name``; return once it accepts.

    Args:
      name: The host's name; must not belong to a live host.
      command: The wrapped command line, recorded for ``trax run ls``.
      start: Runs the CLI served at the given spec, returning its status. It is
        called in the host process, after this one has returned.

    Returns:
      exit_code: 0 once the host accepts; 1 when it failed or is not up yet.

    """
    paths = _paths(name)
    previous = _read_record(paths)
    state = "absent" if previous is None else _state(previous)
    if state in {"starting", "running", "exiting"}:
        raise SystemExit(
            f"trax run: host {name!r} is {state}; "
            f"stop it (trax run stop {name}) or choose another --name",
        )
    shutil.rmtree(paths, ignore_errors=True)
    paths.mkdir(parents=True, mode=0o700)
    address = socket_address(paths / "host.sock")
    # Unflushed output would be written twice: once by this process and once by
    # the host, which inherits the buffer.
    sys.stdout.flush()
    sys.stderr.flush()
    ready_r, ready_w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(ready_r)
        os._exit(
            _detach(
                paths,
                address=address,
                command=command,
                ready=ready_w,
                start=start,
            ),
        )
    os.close(ready_w)
    _ = os.waitpid(pid, 0)
    message = _await_ready(ready_r)
    if message == b"ok":
        sys.stderr.write(
            f"[trax run] {name} is running detached; "
            f"attach with: trax run attach {name}\n",
        )
        return 0
    reason = "did not accept in time" if message is None else "failed to start"
    sys.stderr.write(f"trax run: host {name!r} {reason}; its log follows\n")
    sys.stderr.write(_tail(paths / "host.log"))
    return 1


def main(argv: Sequence[str]) -> int:
    """Run ``trax run ls|attach|log|send|stop``; return the exit code.

    Args:
      argv: The arguments after ``trax run``, starting with the command.

    Returns:
      exit_code: The command's status; for ``attach``, the CLI's when it
        exited while attached.

    """
    flags = cast(_Flags, build_parser().parse_args(argv))
    if flags.command == "ls":
        return _ls()
    if flags.command == "attach":
        return _attach(flags.name)
    if flags.command == "log":
        return _log(flags.name, follow=flags.follow)
    if flags.command == "send":
        return _send(flags.name, " ".join(flags.text))
    return _stop(flags.name)


def build_parser() -> argparse.ArgumentParser:
    """Return the parser for ``trax run ls|attach|log|send|stop``.

    Public because the trax grammar is generated from it, so the documented
    host commands cannot drift from the ones this module accepts.

    Returns:
      parser: The parser, with one subcommand per host command.

    """
    parser = argparse.ArgumentParser(
        prog="trax run",
        description=(__doc__ or "").strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    return parser


class _Flags(Protocol):
    """Parsed host-command flags."""

    command: str
    name: str
    follow: bool
    text: list[str]


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register the host commands on ``parser``."""
    commands = parser.add_subparsers(dest="command", required=True)
    _ = commands.add_parser("ls", help="List hosts and their state.")
    attach_parser = commands.add_parser(
        "attach",
        help="Drive a host from this terminal; Ctrl-\\ detaches.",
    )
    attach_parser.add_argument("name")
    log_parser = commands.add_parser("log", help="Print a host's scrollback.")
    log_parser.add_argument("name")
    log_parser.add_argument(
        "-f",
        "--follow",
        action="store_true",
        help="Keep printing new output until the host exits.",
    )
    send_parser = commands.add_parser(
        "send",
        help="Type a message into a host and press Enter.",
    )
    send_parser.add_argument("name")
    send_parser.add_argument("text", nargs="+")
    stop_parser = commands.add_parser("stop", help="Stop a host and wait for it.")
    stop_parser.add_argument("name")


def _paths(name: str) -> Path:
    """Return ``name``'s host directory, rejecting a name that is not a filename."""
    if _NAME.fullmatch(name) is None:
        raise SystemExit(
            f"trax run: invalid host name {name!r}; use letters, digits, and "
            "._@#+=:- (at most 128, not starting with punctuation)",
        )
    return state_dir() / "rekursiv-ai" / "trax" / "run" / "hosts" / name


# Runs in the forked child. Two forks, so the host is neither the launcher's child
# (nothing waits on it, and it survives the launcher) nor a session leader (it can never
# acquire a controlling terminal, so a closing terminal cannot SIGHUP it).
def _detach(
    paths: Path,
    *,
    address: Path,
    command: str,
    ready: int,
    start: Callable[[HostSpec], int],
) -> int:
    """Leave the launcher's session, then serve as the host; return its status."""
    _ = os.setsid()
    if os.fork() > 0:
        return 0
    _redirect_stdio(paths / "host.log")
    record = HostRecord(
        name=paths.name,
        command=command,
        cwd=str(Path.cwd()),
        pid=os.getpid(),
        address=str(address),
        started=_now(),
    )
    _write_record(paths, record)
    readiness = _Readiness(ready)
    status = 1
    try:
        status = start(
            HostSpec(
                address=address,
                scrollback=paths / "scrollback.log",
                on_listening=partial(_listening, paths, record, readiness),
            ),
        )
    except SystemExit as exit_:
        status = _exit_status(exit_.code)
    except Exception:
        # Nothing above this frame will report it: the host has no caller, and
        # the launcher shows only the log.
        _logger.exception("trax run: the host failed")
    finally:
        readiness.close()
        _write_record(
            paths,
            replace(record, state="exited", exit_code=status, ended=_now()),
        )
        sys.stdout.flush()
        sys.stderr.flush()
    return status


class _Readiness:
    """The launcher's end of the readiness pipe, answered at most once."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def accept(self) -> None:
        """Tell the launcher the host is listening."""
        self._finish(b"ok")

    def close(self) -> None:
        """Release the pipe; a launcher still waiting reads a failure."""
        self._finish(b"")

    def _finish(self, message: bytes) -> None:
        if self._fd < 0:
            return
        _ = write_all(self._fd, message)
        os.close(self._fd)
        self._fd = -1


# The runner's clean refusals (a binary not on PATH) arrive as a ``SystemExit`` with a
# message, which only the interpreter's top level would print -- and the launcher shows
# the log, so the reason must land there.
def _exit_status(code: object) -> int:
    """Return the status a ``SystemExit`` code means, logging a message code."""
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    sys.stderr.write(f"{code}\n")
    return 1


def _listening(paths: Path, record: HostRecord, readiness: _Readiness) -> None:
    """Record the host as running, then release the launcher."""
    _write_record(paths, replace(record, state="running"))
    readiness.accept()


def _redirect_stdio(log: Path) -> None:
    """Point stdin at /dev/null and stdout and stderr at ``log``."""
    null = os.open(os.devnull, os.O_RDONLY)
    out = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    _ = os.dup2(null, 0)
    _ = os.dup2(out, 1)
    _ = os.dup2(out, 2)
    os.close(null)
    os.close(out)


def _await_ready(fd: int) -> bytes | None:
    """Return the host's readiness message, or None if it never answered."""
    try:
        readable, _, _ = select.select([fd], [], [], _READY_TIMEOUT_SEC)
        return os.read(fd, 2) if readable else None
    finally:
        os.close(fd)


def _ls() -> int:
    """Print every host's name, state, pid, start time, and command."""
    rows = [("NAME", "STATE", "PID", "STARTED", "COMMAND")]
    hosts = state_dir() / "rekursiv-ai" / "trax" / "run" / "hosts"
    for directory in sorted(hosts.iterdir() if hosts.is_dir() else ()):
        record = _read_record(directory)
        if record is not None:
            rows.append(
                (
                    record.name,
                    _state(record),
                    str(record.pid),
                    record.started,
                    record.command,
                ),
            )
    # The command, last, is left ragged: padding it would only trail spaces.
    widths = [
        max(len(row[column]) for row in rows) for column in range(len(rows[0]) - 1)
    ]
    for row in rows:
        cells = [row[column].ljust(width) for column, width in enumerate(widths)]
        sys.stdout.write("  ".join([*cells, row[-1]]) + "\n")
    return 0


def _attach(name: str) -> int:
    """Drive the host from this terminal until it exits or is detached."""
    record = _require_record(name)
    try:
        status = asyncio.run(attach(Path(record.address)))
    except OSError:
        raise SystemExit(f"trax run: host {name!r} is {_state(record)}") from None
    if status is None:
        sys.stderr.write(f"\n[trax run] detached from {name}; it is still running\n")
        return 0
    sys.stderr.write(f"\n[trax run] {name} exited with status {status}\n")
    return status


# A follower stops once the host no longer accepts -- and reads once more after, since
# the host writes its last bytes before it closes the socket and the read before the
# check can have missed them.
def _log(name: str, *, follow: bool) -> int:
    """Print the host's scrollback; with ``follow``, until the host exits."""
    record = _require_record(name)
    scrollback = _paths(name) / "scrollback.log"
    if not scrollback.exists():
        return 0
    out = sys.stdout.buffer
    with scrollback.open("rb") as source:
        while True:
            chunk = source.read(65_536)
            if chunk:
                _ = out.write(chunk)
                out.flush()
                continue
            if not follow or not _accepts(Path(record.address)):
                _ = out.write(source.read())
                out.flush()
                return 0
            time.sleep(0.2)


def _send(name: str, text: str) -> int:
    """Type ``text`` into the host and press Enter."""
    record = _require_record(name)
    try:
        submitted = asyncio.run(submit(Path(record.address), text=text))
    except OSError:
        raise SystemExit(f"trax run: host {name!r} is {_state(record)}") from None
    if not submitted:
        sys.stderr.write(f"trax run: {name} did not take the message; it is exiting\n")
        return 1
    return 0


def _stop(name: str) -> int:
    """Stop the host's CLI, then wait until the host itself has exited."""
    record = _require_record(name)
    try:
        status = asyncio.run(stop(Path(record.address)))
    except OSError:
        raise SystemExit(f"trax run: host {name!r} is {_state(record)}") from None
    deadline = time.monotonic() + _STOP_TIMEOUT_SEC
    while _alive(record.pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _alive(record.pid):
        sys.stderr.write(
            f"trax run: {name} stopped its CLI but the host (pid {record.pid}) "
            f"is still closing after {_STOP_TIMEOUT_SEC:.0f}s\n",
        )
        return 1
    sys.stderr.write(f"[trax run] {name} stopped (status {status})\n")
    return 0


def _require_record(name: str) -> HostRecord:
    """Return ``name``'s record, or exit naming the missing host."""
    record = _read_record(_paths(name))
    if record is None:
        raise SystemExit(f"trax run: no host named {name!r}; see trax run ls")
    return record


def _read_record(paths: Path) -> HostRecord | None:
    """Return the host's record, or None when it has none (or none readable)."""
    try:
        text = (paths / "host.json").read_text()
        return convert(parse(text, dict[str, object]), HostRecord)
    except (OSError, ValueError, TypeError):
        return None


# Written whole and renamed into place, so a reader racing a state change sees the old
# record or the new one, never half of either.
def _write_record(paths: Path, record: HostRecord) -> None:
    """Persist ``record`` as the host's ``host.json``."""
    staged = paths / "host.json.tmp"
    _ = staged.write_text(json.dumps(to_builtins(record), indent=2) + "\n")
    _ = staged.replace(paths / "host.json")


# The record says what the host last wrote; the socket and the pid say whether it is
# still there to say otherwise. A host killed outright never writes ``exited``, and
# reports ``lost`` rather than a ``running`` nothing answers for.
def _state(record: HostRecord) -> str:
    """Return ``record``'s live state: its own, ``exiting``, or ``lost``."""
    if record.state == "exited":
        return f"exited {record.exit_code}"
    if _accepts(Path(record.address)):
        return "running"
    if not _alive(record.pid):
        return "lost"
    return "exiting" if record.state == "running" else record.state


def _accepts(address: Path) -> bool:
    """Whether a host is accepting connections at ``address``."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.5)
        probe.connect(str(address))
    except OSError:
        return False
    else:
        return True
    finally:
        probe.close()


def _alive(pid: int) -> bool:
    """Whether a process with ``pid`` exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _tail(path: Path, *, lines: int = 20) -> str:
    """Return the last ``lines`` lines of ``path``, or nothing if unreadable."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""
    return "".join(text.splitlines(keepends=True)[-lines:])


def _now() -> str:
    """Return the current UTC time to the second, ISO formatted."""
    return datetime.now(UTC).isoformat(timespec="seconds")
