"""Serve terminals on this machine: wait for an open, then bridge a shell to the server.

A machine host that opted in (``TRAX_MACHINE_TERMINAL=1``) holds a request on the
server for the next terminal to open. Each open runs one bridge: a login shell on a
pty (or, when the open names a CLI, a detached ``trax run`` of it, which outlives the
pty), and a WebSocket the host dials out to the server, which relays it to the owner's
browser. The host listens on nothing, except that a browser may offer it a direct link
(``trax/terminal_direct.py``) when a WebRTC library is installed.

Every open and close is written to a log only this user can read, with who the server
says opened it, so a dishonest server can lie about the opener but cannot hide an open.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

import asyncio
import contextlib
import logging
import os
import pwd
import shutil
import sys
import threading
import uuid

from pydantic import BaseModel, ConfigDict
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from trackinizer.client.errors import ClientError
from trackinizer.lib.posix.host import Frame, decode_frame, encode_frame
from trackinizer.lib.posix.terminal import Terminal
from trackinizer.lib.userdirs import state_dir
from trackinizer.trax.render import echo
from trackinizer.trax.terminal_direct import DirectRouter
from trackinizer.wire import wire_terminals


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from trackinizer.trax.terminal_direct import PeerFactory
    from trackinizer.wire.wire_terminals import TerminalWait


__all__ = ["TerminalClient", "TerminalHost"]

_logger = logging.getLogger(__name__)


class TerminalClient(Protocol):
    """The call the listener makes."""

    def wait_terminals(
        self,
        machine_id: uuid.UUID,
        *,
        instance: uuid.UUID,
        wait_sec: float,
    ) -> TerminalWait | None:
        """Hold for the next terminal to open."""
        ...

    def close(self) -> None:
        """Release the connection."""
        ...


_ROUTING_ENV: Final = frozenset(
    {"TRACKINIZER_URL", "TRACKINIZER_TOKEN", "TRACKINIZER_PROFILE"},
)
"""The variables ``trax`` reads to choose its server, credential and profile."""


class TerminalHost:
    """Opens the terminals the server asks for, each on its own bridge.

    Args:
      name: The machine's name, which names the log.
      server_url: The server's URL; the sockets use its host over ``ws``/``wss``.
      credential: The machine credential, sent as a bearer on each socket.
      client_for: Returns a client that sends the given credential.
      trax_env: The trax context every shell starts with, which names no secret.
      shell: The command a terminal runs; the user's login shell when empty.
      drain_sec: How long output may keep flowing after the shell exits, which a job
        it left running on the pty can prolong, before the exit is reported anyway.
      direct: Makes the peer that answers a browser's offer of a direct link; ``None``
        when no WebRTC library is installed, which keeps every terminal on the relay.

    """

    def __init__(
        self,
        *,
        name: str,
        server_url: str,
        credential: str,
        client_for: Callable[[str], TerminalClient],
        trax_env: dict[str, str],
        shell: Sequence[str] = (),
        drain_sec: float = 0.5,
        direct: PeerFactory | None = None,
    ) -> None:
        self.direct = direct
        """Makes the peer for a browser's offer; ``None`` keeps every terminal on the relay."""
        self._name = name
        self._url = server_url.replace("http", "ws", 1).rstrip("/")
        self._credential = credential
        self._client_for = client_for
        self.trax_env = trax_env
        """The trax context every shell starts with."""
        self._shell = list(shell)
        self._drain_sec = drain_sec
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._bridges: dict[uuid.UUID, threading.Thread] = {}
        self._loops: dict[
            uuid.UUID,
            tuple[asyncio.AbstractEventLoop, asyncio.Event],
        ] = {}
        self._listener: threading.Thread | None = None

    def start(self, machine_id: uuid.UUID, instance: uuid.UUID) -> None:
        """Start listening for opens on a thread.

        Args:
          machine_id: This machine's id.
          instance: This host's instance.

        """
        self._listener = threading.Thread(
            name="terminal-listener",
            target=self._listen,
            args=(machine_id, instance),
            daemon=True,
        )
        self._listener.start()

    def stop(self) -> None:
        """Stop listening and kill every shell; returns once the bridges have ended."""
        self._stop.set()
        with self._lock:
            loops = list(self._loops.values())
            bridges = list(self._bridges.values())
        for loop, ended in loops:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(ended.set)
        for bridge in bridges:
            bridge.join(timeout=10)

    def _listen(self, machine_id: uuid.UUID, instance: uuid.UUID) -> None:
        """Wait for opens until stopped, starting a bridge thread for each."""
        client = self._client_for(self._credential)
        try:
            while not self._stop.is_set():
                try:
                    opened = client.wait_terminals(
                        machine_id,
                        instance=instance,
                        wait_sec=wire_terminals.WAIT_MAX_SEC,
                    )
                except ClientError as err:
                    if err.status_code == 410:
                        return
                    echo(f"trax: terminal wait failed: {err}", err=True)
                    self._stop.wait(5.0)
                    continue
                if opened is not None:
                    self._start_bridge(opened, machine_id=machine_id, instance=instance)
        finally:
            client.close()

    def _start_bridge(
        self,
        opened: TerminalWait,
        *,
        machine_id: uuid.UUID,
        instance: uuid.UUID,
    ) -> None:
        """Start a bridge thread for an open."""
        if self._stop.is_set():
            return
        with self._lock:
            thread = threading.Thread(
                name=f"terminal-{opened.terminal_id}",
                target=self._run,
                args=(opened, machine_id, instance),
                daemon=True,
            )
            self._bridges[opened.terminal_id] = thread
            thread.start()

    def _run(
        self,
        opened: TerminalWait,
        machine_id: uuid.UUID,
        instance: uuid.UUID,
    ) -> None:
        """Bridge one terminal, then forget its thread."""
        try:
            self.serve(opened, machine_id=machine_id, instance=instance)
        finally:
            with self._lock:
                self._bridges.pop(opened.terminal_id, None)

    def serve(
        self,
        opened: TerminalWait,
        *,
        machine_id: uuid.UUID,
        instance: uuid.UUID,
    ) -> None:
        """Run one terminal to its end on the calling thread.

        Args:
          opened: The terminal the server asked for.
          machine_id: This machine's id.
          instance: This host's instance.

        """
        asyncio.run(
            self._bridge(opened, machine_id=machine_id, instance=instance),
        )

    async def _bridge(
        self,
        opened: TerminalWait,
        *,
        machine_id: uuid.UUID,
        instance: uuid.UUID,
    ) -> None:
        """Register cancellation before dialing, then serve one shell at most."""
        ended = asyncio.Event()
        with self._lock:
            self._loops[opened.terminal_id] = (asyncio.get_running_loop(), ended)
        if self._stop.is_set():
            ended.set()
        work = asyncio.create_task(
            self._serve(opened, machine_id=machine_id, instance=instance, ended=ended),
        )
        stopped = asyncio.create_task(ended.wait())
        try:
            await asyncio.wait({work, stopped}, return_when=asyncio.FIRST_COMPLETED)
            if work.done():
                await work
        finally:
            work.cancel()
            stopped.cancel()
            await asyncio.gather(work, stopped, return_exceptions=True)
            with self._lock:
                self._loops.pop(opened.terminal_id, None)

    async def _connect(
        self,
        opened: TerminalWait,
        *,
        machine_id: uuid.UUID,
        instance: uuid.UUID,
        open_timeout_sec: float = 10.0,
        startup_timeout_sec: float = 45.0,
        retry_sec: float = 1.0,
    ) -> ClientConnection | None:
        """Retry startup before any process starts; return only an acknowledged leg."""
        path = wire_terminals.MACHINE_TERMINAL_WS_PATH.format(
            machine_id=machine_id,
            terminal_id=opened.terminal_id,
        )
        deadline = asyncio.get_running_loop().time() + startup_timeout_sec
        reason = "connection deadline exceeded"
        while (remaining := deadline - asyncio.get_running_loop().time()) > 0:
            socket: ClientConnection | None = None
            try:
                async with asyncio.timeout(min(open_timeout_sec, remaining)):
                    socket = await connect(
                        f"{self._url}{path}?instance={instance}&ready=true",
                        additional_headers={
                            "Authorization": f"Bearer {self._credential}",
                        },
                        ping_interval=20,
                        open_timeout=min(open_timeout_sec, remaining),
                        close_timeout=1,
                    )
                    await socket.send(encode_frame(Frame.ACK))
                    if await socket.recv() != encode_frame(Frame.ACK):
                        await socket.close()
                        reason = "invalid startup acknowledgement"
                        break
                return socket
            except asyncio.CancelledError:
                if socket is not None:
                    await socket.close()
                raise
            except (OSError, InvalidHandshake, ConnectionClosed) as error:
                if socket is not None:
                    await socket.close()
                reason = str(error) or type(error).__name__
                if (
                    isinstance(error, ConnectionClosed)
                    and error.rcvd is not None
                    and error.rcvd.code
                    in {
                        wire_terminals.CLOSE_REFUSED,
                        wire_terminals.CLOSE_MACHINE_REVOKED,
                        wire_terminals.CLOSE_ENDED,
                    }
                ):
                    break
                if isinstance(error, InvalidStatus) and (
                    error.response.status_code < 500
                    and error.response.status_code != 429
                ):
                    break
                _logger.debug(
                    "terminal %s: startup retry: %s",
                    opened.terminal_id,
                    reason,
                )
            await asyncio.sleep(
                min(retry_sec, max(0, deadline - asyncio.get_running_loop().time())),
            )
        echo(
            f"trax: terminal startup failed ({opened.terminal_id}): {reason}",
            err=True,
        )
        self._log("startup_failed", opened, status=1)
        return None

    async def _serve(
        self,
        opened: TerminalWait,
        *,
        machine_id: uuid.UUID,
        instance: uuid.UUID,
        ended: asyncio.Event,
    ) -> None:
        """Start the shell once its relay is ready; never retry a launched shell."""
        socket = await self._connect(opened, machine_id=machine_id, instance=instance)
        if socket is None:
            return
        self._log("open", opened)
        status: int | None = None
        try:
            await socket.send(encode_frame(Frame.ACK))
            folder = await asyncio.to_thread(_start_folder, opened.cwd)
            if folder is None:
                await _end_with_line(
                    socket,
                    f"trax: {opened.cwd} is not a folder on this machine",
                )
                status = 1
                return
            async with Terminal(
                self._argv(opened),
                cwd=folder,
                env=self._env(opened),
                clean_env=opened.command is None,
                drop_env=() if opened.command is None else _inherited_trax_context(),
                terminate_grace_sec=2.0,
                winsize=(opened.rows, opened.cols),
            ) as terminal:
                router = DirectRouter(
                    relay=socket.send,
                    shell=terminal,
                    factory=self.direct,
                    ice_servers=tuple(opened.ice_servers),
                )
                try:
                    if opened.command is not None:
                        await router.output(f"trax: started in {folder}\r\n".encode())
                    status = await self._relay(terminal, socket, ended, router)
                finally:
                    await router.close()
        except ConnectionClosed:
            status = 1
        finally:
            self._log("close", opened, status=status)
            await socket.close()

    async def _relay(
        self,
        terminal: Terminal,
        socket: ClientConnection,
        ended: asyncio.Event,
        router: DirectRouter,
    ) -> int:
        """Relay until the shell exits, the server stops it, or the socket closes."""
        pump = asyncio.create_task(_pump(terminal, router))
        reader = asyncio.create_task(_read(router, socket))
        stopped = asyncio.create_task(ended.wait())
        # The shell's own exit counts, not only the pty closing: a job it left running
        # keeps the pty open, and would otherwise hide that the shell is gone.
        exited = asyncio.create_task(terminal.wait())
        try:
            await asyncio.wait(
                {pump, reader, stopped, exited},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            await terminal.terminate()
            # Reap through the existing waiter, even during cancellation; two
            # concurrent wait calls can steal the child's status from each other.
            status = await exited
            await asyncio.wait({pump}, timeout=self._drain_sec)
            for task in (pump, reader, stopped):
                task.cancel()
            await asyncio.gather(pump, reader, stopped, return_exceptions=True)
        with contextlib.suppress(ConnectionClosed):
            await router.exit(status)
        return status

    def _argv(self, opened: TerminalWait) -> list[str]:
        """Return a detached ``trax run`` of the CLI asked for, else a shell."""
        login = self._shell or [pwd.getpwuid(os.getuid()).pw_shell, "-l"]
        if opened.command is None:
            return login
        # The trax this host runs as is preferred: a PATH lookup could find another
        # install, and the run must come from the same code as the host.
        argv0 = Path(sys.argv[0])
        trax = (
            str(argv0.absolute())
            if argv0.is_file() and os.access(argv0, os.X_OK)
            else shutil.which("trax")
        )
        if trax is None:
            echo(
                "trax: no trax executable found; the terminal opens a login shell",
                err=True,
            )
            return login
        assert isinstance(opened.run_name, str)
        attach = [] if opened.attach is None else ["--attach", str(opened.attach)]
        return [
            trax,
            "run",
            "--detach",
            "--name",
            opened.run_name,
            *attach,
            opened.command,
        ]

    # A shell starts clean, so this and the essentials are all it has. A run inherits
    # the host's environment like a ``trax run`` typed where ``trax machine connect``
    # was started, and this replaces the trax context it inherited (see
    # :func:`_inherited_trax_context`).
    def _env(self, opened: TerminalWait) -> dict[str, str]:
        """Return the environment laid over the child's; no secret in it."""
        env = {
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            **self.trax_env,
        }
        if (lang := os.environ.get("LANG")) is not None:
            env["LANG"] = lang
        if opened.workspace is not None:
            env["TRAX_WORKSPACE"] = str(opened.workspace)
            env["LC_TRAX_WORKSPACE"] = str(opened.workspace)
        if opened.workstream is not None:
            env["TRAX_WORKSTREAM"] = str(opened.workstream)
            env["LC_TRAX_WORKSTREAM"] = str(opened.workstream)
        return env

    def _log(
        self,
        event: str,
        opened: TerminalWait,
        *,
        status: int | None = None,
    ) -> None:
        """Append one line to the host's log, readable by this user only."""
        path = (
            state_dir()
            / "rekursiv-ai"
            / "trax"
            / "machines"
            / f"{self._name}.terminals.log"
        )
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        line = _LogLine(
            at=datetime.now(UTC),
            event=event,
            terminal_id=opened.terminal_id,
            opener=opened.opener,
            requested_by=opened.requested_by,
            status=status,
        )
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as file:
            _ = file.write(line.model_dump_json() + "\n")
        if event == "open":
            echo(f"terminal opened by {opened.opener}")


async def _pump(terminal: Terminal, router: DirectRouter) -> None:
    """Send the shell's output until the pty closes."""
    try:
        async for chunk in terminal.output():
            await router.output(chunk)
    except ConnectionClosed:
        return


async def _read(router: DirectRouter, socket: ClientConnection) -> None:
    """Apply the server's frames to the shell; return on STOP or a closed socket."""
    try:
        async for message in socket:
            frame = _decode(message)
            if frame is None:
                return
            kind, payload = frame
            if kind is Frame.STOP:
                return
            await router.from_relay(kind, payload)
    except ConnectionClosed:
        return


def _decode(message: str | bytes) -> tuple[Frame, bytes] | None:
    """Return the frame a message carries, or ``None`` when it is not one."""
    if isinstance(message, str):
        return None
    try:
        return decode_frame(message)
    except ValueError:
        return None


def _start_folder(cwd: str | None) -> Path | None:
    """Return the folder a terminal starts in, or ``None`` when ``cwd`` is not one."""
    if cwd is None:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    try:
        folder = Path(cwd).expanduser()
    except RuntimeError:
        # ``~name`` names a user this machine does not have: no home to resolve against.
        return None
    return folder if folder.is_dir() else None


class _Leg(Protocol):
    """The one send a refusal makes on the host's socket."""

    async def send(self, message: bytes) -> None:
        """Send one binary message."""
        ...


# The shell never starts, so the browser is told the same way as after a shell that
# exits: output, then ``EXIT``.
async def _end_with_line(leg: _Leg, line: str) -> None:
    """Show ``line`` in the terminal, then end it with a failed status."""
    with contextlib.suppress(ConnectionClosed):
        await leg.send(encode_frame(Frame.OUTPUT, f"{line}\r\n".encode()))
        await leg.send(encode_frame(Frame.EXIT, (1).to_bytes(4)))


class _LogLine(BaseModel):
    """One line of the host-local terminal log."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    at: datetime
    event: str
    terminal_id: uuid.UUID
    opener: str
    requested_by: str | None = None
    status: int | None = None


# The station's trax context replaces any the host inherited: a server, a credential, a
# profile, or a ``TRAX_*`` / ``LC_TRAX_*`` name left by a trax run the host itself sits
# in outranks what the station sets for the run.
def _inherited_trax_context() -> frozenset[str]:
    """Return the host's variables that would route a run off this station."""
    return frozenset(
        name
        for name in os.environ
        if name in _ROUTING_ENV or name.startswith(("TRAX_", "LC_TRAX_"))
    )
