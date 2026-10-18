"""Serve terminals on this machine: wait for an open, then bridge a shell to the server.

A machine host that opted in (``TRAX_MACHINE_TERMINAL=1``) holds a request on the
server for the next terminal to open. Each open runs one bridge: a login shell on a
pty, and a WebSocket the host dials out to the server, which relays it to the owner's
browser. The host listens on nothing.

Every open and close is written to a log only this user can read, with who the server
says opened it, so a dishonest server can lie about the opener but cannot hide an open.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import asyncio
import contextlib
import os
import pwd
import threading
import uuid

from pydantic import BaseModel, ConfigDict
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from trackinizer.client.errors import ClientError
from trackinizer.lib.posix.host import Frame, decode_frame, decode_winsize, encode_frame
from trackinizer.lib.posix.terminal import Terminal
from trackinizer.lib.userdirs import state_dir
from trackinizer.trax.render import echo
from trackinizer.wire import wire_terminals


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from trackinizer.wire.wire_terminals import TerminalWait


__all__ = ["TerminalClient", "TerminalHost"]


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


class TerminalHost:
    """Opens the terminals the server asks for, at most four at a time.

    Args:
      name: The machine's name, which names the log.
      server_url: The server's URL; the sockets use its host over ``ws``/``wss``.
      credential: The machine credential, sent as a bearer on each socket.
      client_for: Returns a client that sends the given credential.
      trax_env: The trax context every shell starts with, which names no secret.
      shell: The command a terminal runs; the user's login shell when empty.
      drain_sec: How long output may keep flowing after the shell exits, which a job
        it left running on the pty can prolong, before the exit is reported anyway.

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
    ) -> None:
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
        """Start a bridge thread, or turn the open away at the host's limit."""
        if self._stop.is_set():
            return
        with self._lock:
            full = len(self._bridges) >= wire_terminals.MAX_HOST_BRIDGES
            thread = threading.Thread(
                name=f"terminal-{opened.terminal_id}",
                target=self._run,
                args=(opened, machine_id, instance, full),
                daemon=True,
            )
            if not full:
                self._bridges[opened.terminal_id] = thread
        thread.start()

    def _run(
        self,
        opened: TerminalWait,
        machine_id: uuid.UUID,
        instance: uuid.UUID,
        full: bool,
    ) -> None:
        """Bridge one terminal, then forget its thread."""
        try:
            self.serve(opened, machine_id=machine_id, instance=instance, refuse=full)
        finally:
            with self._lock:
                self._bridges.pop(opened.terminal_id, None)

    def serve(
        self,
        opened: TerminalWait,
        *,
        machine_id: uuid.UUID,
        instance: uuid.UUID,
        refuse: bool = False,
    ) -> None:
        """Run one terminal to its end on the calling thread.

        Args:
          opened: The terminal the server asked for.
          machine_id: This machine's id.
          instance: This host's instance.
          refuse: Turn the open away with 4429 instead of starting a shell.

        """
        asyncio.run(
            self._bridge(
                opened,
                machine_id=machine_id,
                instance=instance,
                refuse=refuse,
            ),
        )

    async def _bridge(
        self,
        opened: TerminalWait,
        *,
        machine_id: uuid.UUID,
        instance: uuid.UUID,
        refuse: bool = False,
    ) -> None:
        """Dial the server, run the shell, and relay until either side ends."""
        path = wire_terminals.MACHINE_TERMINAL_WS_PATH.format(
            machine_id=machine_id,
            terminal_id=opened.terminal_id,
        )
        async with connect(
            f"{self._url}{path}?instance={instance}",
            additional_headers={"Authorization": f"Bearer {self._credential}"},
            ping_interval=20,
        ) as socket:
            if refuse:
                await socket.close(wire_terminals.CLOSE_LIMIT)
                return
            self._log("open", opened)
            status: int | None = None
            async with Terminal(
                self._argv(),
                cwd=Path(pwd.getpwuid(os.getuid()).pw_dir),
                env=self._env(opened),
                clean_env=True,
                terminate_grace_sec=2.0,
                winsize=(opened.rows, opened.cols),
            ) as terminal:
                ended = asyncio.Event()
                with self._lock:
                    self._loops[opened.terminal_id] = (
                        asyncio.get_running_loop(),
                        ended,
                    )
                # A stop that came before this bridge registered did not see it: the
                # flag it set first is what tells this one to end at once.
                if self._stop.is_set():
                    ended.set()
                try:
                    status = await self._relay(terminal, socket, ended)
                finally:
                    with self._lock:
                        self._loops.pop(opened.terminal_id, None)
            self._log("close", opened, status=status)

    async def _relay(
        self,
        terminal: Terminal,
        socket: ClientConnection,
        ended: asyncio.Event,
    ) -> int:
        """Relay until the shell exits, the server stops it, or the socket closes."""
        pump = asyncio.create_task(_pump(terminal, socket))
        reader = asyncio.create_task(_read(terminal, socket))
        stopped = asyncio.create_task(ended.wait())
        # The shell's own exit counts, not only the pty closing: a job it left running
        # keeps the pty open, and would otherwise hide that the shell is gone.
        exited = asyncio.create_task(terminal.wait())
        await asyncio.wait(
            {pump, reader, stopped, exited},
            return_when=asyncio.FIRST_COMPLETED,
        )
        await terminal.terminate()
        # The one reaper of the child: a second ``wait`` running beside it would find
        # the child gone and report status 0.
        status = await exited
        await asyncio.wait({pump}, timeout=self._drain_sec)
        for task in (pump, reader, stopped, exited):
            task.cancel()
        with contextlib.suppress(ConnectionClosed):
            await socket.send(encode_frame(Frame.EXIT, status.to_bytes(4)))
        return status

    def _argv(self) -> list[str]:
        """Return the command: the user's login shell unless one was given."""
        return self._shell or [pwd.getpwuid(os.getuid()).pw_shell, "-l"]

    def _env(self, opened: TerminalWait) -> dict[str, str]:
        """Return the shell's environment beyond the essentials; no secret in it."""
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


async def _pump(terminal: Terminal, socket: ClientConnection) -> None:
    """Send the shell's output until the pty closes."""
    try:
        async for chunk in terminal.output():
            await socket.send(encode_frame(Frame.OUTPUT, chunk))
    except ConnectionClosed:
        return


async def _read(terminal: Terminal, socket: ClientConnection) -> None:
    """Apply the server's frames to the shell; return on STOP or a closed socket."""
    try:
        async for message in socket:
            frame = _decode(message)
            if frame is None:
                return
            kind, payload = frame
            if kind is Frame.INPUT:
                _ = await terminal.write(payload)
            elif kind is Frame.RESIZE:
                _resize(terminal, payload)
            elif kind is Frame.STOP:
                return
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


def _resize(terminal: Terminal, payload: bytes) -> None:
    """Resize the pty to a geometry clamped to the limits; ignore a malformed one."""
    try:
        rows, cols = decode_winsize(payload)
    except ValueError:
        return
    terminal.set_winsize(
        min(max(rows, 1), wire_terminals.MAX_ROWS),
        min(max(cols, 1), wire_terminals.MAX_COLS),
    )


class _LogLine(BaseModel):
    """One line of the host-local terminal log."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    at: datetime
    event: str
    terminal_id: uuid.UUID
    opener: str
    requested_by: str | None = None
    status: int | None = None
