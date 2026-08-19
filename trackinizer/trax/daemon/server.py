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
import socket
import socketserver
import sys
import threading
import time
import traceback

from trackinizer.client.errors import ClientError
from trackinizer.trax.cli import parse_and_run
from trackinizer.trax.context import CWD, ENV, ERR_STREAM, OUT_STREAM
from trackinizer.trax.daemon.client import STALE_EXIT_CODE
from trackinizer.trax.daemon.protocol import (
    PROTOCOL_VERSION,
    Request,
    Response,
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


def handle(
    request: Request,
    *,
    run: Callable[[Sequence[str]], None] = parse_and_run,
) -> Response:
    """Execute one request under the caller's ambient state.

    The daemon's own stdout, environment, and working directory belong to
    whichever shell spawned it, so each is swapped for the caller's around
    the verb and restored after -- leaking one caller's identity or terminal
    geometry into the next request would silently attribute an audit row to
    the wrong user.

    Args:
      request: The invocation plus the caller's ambient state.
      run: The verb dispatcher; injected by tests.

    Returns:
      response: The captured stdout, stderr, and exit code.

    """
    if request.protocol_version != PROTOCOL_VERSION:
        return Response(
            stdout="",
            stderr=(
                f"trax: daemon speaks protocol {PROTOCOL_VERSION}, "
                f"client speaks {request.protocol_version}\n"
            ),
            exit_code=_INTERNAL_ERROR_EXIT_CODE,
        )
    out, err = io.StringIO(), io.StringIO()
    # A fresh Context per request: ``SHOW_IDS`` and ``TERMINAL_WIDTH`` are
    # ContextVars read deep in the render path, so concurrent requests would
    # otherwise see each other's flags.
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

    Binding is the arbiter for the spawn race: several clients missing at
    once all try to start a daemon, and every loser gets ``EADDRINUSE`` and
    simply connects to the winner instead.
    """
    sock = path if path is not None else socket_path()
    sock.parent.mkdir(parents=True, exist_ok=True)
    _unlink_if_dead(sock)
    version = source_version(Path(__file__).resolve().parents[1])
    try:
        server = _Server(str(sock), version)
    except OSError:
        # Another daemon won the race and owns the socket; nothing to do.
        return
    try:
        sock.chmod(0o600)
        server.serve_forever(poll_interval=1.0)
    finally:
        server.server_close()
        sock.unlink(missing_ok=True)


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
    ``$USER``, and one response came back carrying another's output. Under a
    polling swarm that misattributes audit actors.
    """
    OUT_STREAM.set(out)
    ERR_STREAM.set(err)
    ENV.set(dict(request.env))
    CWD.set(request.cwd)
    # The daemon's stdout is a socket, so ``isatty()`` there is always False
    # and autodetection would size every table as if piped.
    TERMINAL_WIDTH.set(request.columns if request.isatty else 0)
    try:
        run(request.argv)
    except ClientError as error:
        err.write(f"trax: {error}\n")
        return _CLIENT_ERROR_EXIT_CODE
    except SystemExit as exit_request:
        return int(exit_request.code or 0)
    return 0


def _unlink_if_dead(sock: Path) -> None:
    """Remove a socket file no daemon is listening on.

    A daemon killed with SIGKILL leaves its socket behind; without this every
    subsequent spawn fails to bind and the CLI silently runs in-process
    forever.
    """
    if not sock.exists():
        return
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.2)
        probe.connect(str(sock))
    except OSError:
        sock.unlink(missing_ok=True)
    finally:
        probe.close()


class _Handler(socketserver.BaseRequestHandler):
    """Serve one connection: read a frame, run it, write the answer."""

    @override
    def handle(self) -> None:
        server = self.server
        assert isinstance(server, _Server)
        server.touch()
        try:
            request = Request.from_json(read_frame(self.request))
        except (ConnectionError, ValueError):
            return
        if request.source_version != server.version:
            # The worktree moved under us. Report it and shut down so the
            # next invocation spawns a daemon running the current source;
            # continuing to serve would emit stale behavior that looks right.
            with contextlib.suppress(OSError):
                write_frame(
                    self.request,
                    Response(stdout="", stderr="", exit_code=STALE_EXIT_CODE).to_json(),
                )
            threading.Thread(target=server.shutdown, daemon=True).start()
            return
        with contextlib.suppress(OSError):
            write_frame(self.request, handle(request).to_json())


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

    def touch(self) -> None:
        """Record activity, deferring the idle shutdown."""
        self._last_seen = time.monotonic()

    @override
    def service_actions(self) -> None:
        """Exit once idle long enough; called between accepts."""
        super().service_actions()
        if time.monotonic() - self._last_seen > _IDLE_TIMEOUT_SEC:
            threading.Thread(target=self.shutdown, daemon=True).start()


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``trax --__serve``."""
    del argv
    serve()
    return 0


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    sys.exit(main())
