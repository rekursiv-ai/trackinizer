"""The traxd request handler and accept loop.

Unlike ``protocol`` and ``client``, this module imports the full CLI on
purpose: holding those imports resident is the daemon's entire job.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final, override

import contextlib
import contextvars
import io
import os
import socket
import socketserver
import threading
import time
import traceback

from trackinizer.client.errors import ClientError
from trackinizer.trax.cli import parse_and_run
from trackinizer.trax.context import (
    CWD,
    ENV,
    ERR_STREAM,
    OUT_STREAM,
    OVERLAID_NAMES,
)
from trackinizer.trax.daemon.client import STALE_EXIT_CODE
from trackinizer.trax.daemon.protocol import (
    FORWARDED_ENV,
    ProtocolVersionError,
    Request,
    Response,
    package_root,
    read_frame,
    socket_path,
    source_version,
    write_frame,
)
from trackinizer.trax.render import TERMINAL_WIDTH


_IDLE_TIMEOUT_SEC: Final = 30 * 60
"""Exit after this long with no requests. A daemon per login session that
never exits is a leak; half an hour outlives a working session's gaps while
keeping an abandoned one from lingering for days."""

_CLIENT_ERROR_EXIT_CODE: Final = 2
"""``cli.main`` exits 2 on ``ClientError``; scripts branch on it, so the
daemon must reproduce it rather than inventing its own code."""

_INTERNAL_ERROR_EXIT_CODE: Final = 70
"""EX_SOFTWARE: an unexpected exception escaped a verb. Distinct from 2 so a
daemon bug is not mistaken for a user-facing CLI error."""

_SOCKET_DIR_MODE: Final = 0o700
"""Only the owner may reach the socket. The daemon executes writes under the
owner's profile token regardless of which ``$USER`` a request forwards, so a
connection from another local account would act with the owner's authority."""

_SOCKET_UMASK: Final = 0o177
"""Applied around ``bind`` so the socket is never briefly world-writable.
``chmod`` after ``bind`` leaves a window in which another local user can
connect; the umask closes it because it applies as the inode is created."""


def handle(
    request: Request,
    *,
    run: Callable[[Sequence[str]], None] = parse_and_run,
) -> Response:
    """Execute one request under the caller's ambient state.

    Streams, environment, and working directory are bound as ContextVars for
    the duration of the call and discarded with the copied context when it
    returns. Nothing process-global is written, so concurrent requests cannot
    observe each other's identity -- which would silently stamp one agent's
    audit rows with another's actor.

    Args:
      request: The invocation plus the caller's ambient state.
      run: The verb dispatcher; injected by tests.

    Returns:
      response: The captured stdout, stderr, and exit code.

    """
    out, err = io.StringIO(), io.StringIO()
    # A fresh Context per request: every per-invocation binding is a
    # ContextVar, so concurrent requests would otherwise see each other's.
    context = contextvars.copy_context()
    try:
        exit_code = context.run(_run_isolated, request, run, out, err)
    except BaseException:  # noqa: BLE001 -- one bad verb must not kill the daemon.
        return Response(
            stdout=out.getvalue(),
            stderr=err.getvalue() + traceback.format_exc(),
            exit_code=_INTERNAL_ERROR_EXIT_CODE,
        )
    return Response(stdout=out.getvalue(), stderr=err.getvalue(), exit_code=exit_code)


def serve(path: Path | None = None) -> None:
    """Run the daemon until it is idle for :data:`_IDLE_TIMEOUT_SEC`.

    Binding is the arbiter for the spawn race: several clients missing a
    daemon at once all try to start one, and every loser gets ``EADDRINUSE``
    and simply connects to the winner instead.
    """
    sock = path if path is not None else socket_path()
    sock.parent.mkdir(parents=True, exist_ok=True, mode=_SOCKET_DIR_MODE)
    version = source_version(package_root())
    server = _bind(sock, version)
    if server is None:
        return
    # The inode this daemon owns. Checked before unlinking on the way out: a
    # racing daemon may have replaced the file, and removing ITS socket would
    # take a live daemon off the air.
    owned_inode = sock.stat().st_ino
    try:
        server.serve_forever(poll_interval=1.0)
    finally:
        server.server_close()
        _unlink_if_owned(sock, owned_inode)


def _bind(sock: Path, version: str) -> _Server | None:
    """Bind the socket, clearing a dead predecessor's file first.

    Returns ``None`` when another daemon owns the socket -- it won the race,
    and this process has nothing to do.
    """
    for _ in range(2):
        previous_umask = os.umask(_SOCKET_UMASK)
        try:
            return _Server(str(sock), version)
        except OSError:
            pass
        finally:
            os.umask(previous_umask)
        # ``EADDRINUSE`` is either a live daemon (give up) or a file left by
        # one that was killed (clear it and retry exactly once).
        if _is_live(sock):
            return None
        with contextlib.suppress(OSError):
            sock.unlink()
    return None


def _is_live(sock: Path) -> bool:
    """Whether something is accepting connections on ``sock``."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.2)
        probe.connect(str(sock))
    except OSError:
        return False
    else:
        return True
    finally:
        probe.close()


def _unlink_if_owned(sock: Path, inode: int) -> None:
    """Remove the socket only if it is still the one this daemon bound."""
    try:
        if sock.stat().st_ino == inode:
            sock.unlink()
    except OSError:
        return


def _run_isolated(
    request: Request,
    run: Callable[[Sequence[str]], None],
    out: io.StringIO,
    err: io.StringIO,
) -> int:
    """Run one verb bound to the caller's streams, environment, and directory.

    Every binding is a ContextVar, never a process global. ``os.environ``,
    ``os.chdir``, and ``sys.stdout`` are shared by the whole process, so
    swapping them around a request leaks into every other request in flight
    -- measured, not theorized: four concurrent requests saw each other's
    ``$USER``, and one response came back carrying another's output.

    ``sys.stdout``/``sys.stderr`` ARE redirected on top of that, for the one
    writer that cannot be taught the ContextVars: ``argparse`` prints usage
    and errors to the real streams before raising ``SystemExit``. Those are
    the daemon's ``/dev/null``, so without this an invalid flag returns exit
    2 with no message at all. The redirect is process-wide, so a concurrent
    request could capture a stray write from another -- acceptable only
    because every trax writer goes through ``echo``; argparse is the
    exception this exists for.
    """
    OUT_STREAM.set(out)
    ERR_STREAM.set(err)
    ENV.set(dict(request.env))
    OVERLAID_NAMES.set(frozenset(FORWARDED_ENV))
    CWD.set(request.cwd)
    # The daemon's stdout is a socket, so ``isatty()`` there is always False
    # and autodetection would size every table as if piped.
    TERMINAL_WIDTH.set(request.columns if request.isatty else 0)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            run(request.argv)
    except ClientError as error:
        err.write(f"trax: {error}\n")
        return _CLIENT_ERROR_EXIT_CODE
    except SystemExit as exit_request:
        return _exit_status(exit_request, err)
    return 0


def _exit_status(exit_request: SystemExit, err: io.StringIO) -> int:
    """Map a ``SystemExit`` payload to a process exit status.

    ``sys.exit`` accepts a string, which the interpreter prints to stderr and
    reports as status 1. Coercing it with ``int()`` would raise instead, and
    the real message would be replaced by an internal-error traceback.
    """
    code = exit_request.code
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    err.write(f"{code}\n")
    return 1


class _Handler(socketserver.BaseRequestHandler):
    """Serve one connection: read a frame, run it, write the answer."""

    @override
    def handle(self) -> None:
        server = self.server
        assert isinstance(server, _Server)
        server.touch()
        try:
            request = Request.from_json(read_frame(self.request))
        except ProtocolVersionError as mismatch:
            # Answer rather than dropping the connection: a silent close is
            # indistinguishable from "no daemon", so the client would fall
            # back in-process and the user would never learn the versions
            # disagree.
            self._reply(
                Response(
                    stdout="",
                    stderr=f"trax: {mismatch}\n",
                    exit_code=_INTERNAL_ERROR_EXIT_CODE,
                )
            )
            return
        except (ConnectionError, ValueError):
            return
        if request.source_version != server.version:
            # The worktree moved under us. Report it and shut down so the
            # next invocation spawns a daemon running the current source;
            # continuing to serve would emit stale behavior that looks right.
            self._reply(Response(stdout="", stderr="", exit_code=STALE_EXIT_CODE))
            server.begin_shutdown()
            return
        self._reply(handle(request))

    def _reply(self, response: Response) -> None:
        """Write one response frame, tolerating a client that hung up."""
        with contextlib.suppress(OSError):
            write_frame(self.request, response.to_json())


class _Server(socketserver.ThreadingUnixStreamServer):
    """Threaded so one slow request cannot stall the rest.

    ``Client._request`` sleeps up to ~1s retrying a 5xx, and a single-threaded
    daemon would serialize a swarm behind that.
    """

    daemon_threads = True
    request_queue_size = 128

    def __init__(self, path: str, version: str) -> None:
        super().__init__(path, _Handler)
        self.version = version
        self._last_seen = time.monotonic()
        self._shutting_down = False

    def touch(self) -> None:
        """Record activity, deferring the idle shutdown."""
        self._last_seen = time.monotonic()

    def begin_shutdown(self) -> None:
        """Stop the accept loop, once, from a request thread.

        ``shutdown`` blocks until the loop exits, so it cannot be called from
        the loop's own thread; the flag keeps a repeated trigger (the idle
        poll fires every second) from spawning a thread per tick.
        """
        if self._shutting_down:
            return
        self._shutting_down = True
        threading.Thread(target=self.shutdown, daemon=True).start()

    @override
    def service_actions(self) -> None:
        """Exit once idle long enough; called between accepts."""
        super().service_actions()
        if time.monotonic() - self._last_seen > _IDLE_TIMEOUT_SEC:
            self.begin_shutdown()
