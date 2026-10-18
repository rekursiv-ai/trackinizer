"""The terminal host: real shells on real ptys, bridged to a local WebSocket server."""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio
import os
import stat
import subprocess
import threading
import uuid

from websockets.asyncio.server import Server, ServerConnection, serve

import pytest

from trackinizer.client.errors import ClientError
from trackinizer.lib.posix.host import Frame, decode_frame, encode_frame, encode_winsize
from trackinizer.lib.userdirs import state_dir
from trackinizer.trax.terminal_host import TerminalHost
from trackinizer.wire import wire_terminals
from trackinizer.wire.wire_terminals import TerminalWait


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

pytestmark = pytest.mark.asyncio

_MACHINE = uuid.UUID(int=1)
_INSTANCE = uuid.UUID(int=2)
_CREDENTIAL = "trax_machine_" + "0" * 32 + "_" + "c" * 43

type _Handler = Callable[[ServerConnection], Awaitable[None]]


class _Client:
    """Hands out scripted waits; once they run out it blocks like a held request."""

    def __init__(self, answers: Sequence[TerminalWait | ClientError]) -> None:
        self._answers = list(answers)
        self.idle = threading.Event()
        self.closed = threading.Event()

    def wait_terminals(
        self,
        machine_id: uuid.UUID,
        *,
        instance: uuid.UUID,
        wait_sec: float,
    ) -> TerminalWait | None:
        assert (machine_id, instance, wait_sec) == (_MACHINE, _INSTANCE, 25)
        if self._answers:
            answer = self._answers.pop(0)
            if isinstance(answer, ClientError):
                raise answer
            return answer
        self.idle.wait(0.005)
        return None

    def close(self) -> None:
        self.closed.set()


def _opened(**fields: object) -> TerminalWait:
    return TerminalWait.model_validate(
        {
            "terminal_id": uuid.uuid4(),
            "opener": "ada@x.y",
            "rows": 24,
            "cols": 80,
            **fields,
        },
    )


def _host(
    url: str,
    *shell: str,
    client: _Client | None = None,
    trax_env: dict[str, str] | None = None,
    drain_sec: float = 0.5,
) -> TerminalHost:
    return TerminalHost(
        name="box",
        server_url=url,
        credential=_CREDENTIAL,
        client_for=lambda _key: client or _Client([]),
        trax_env=trax_env or {},
        shell=shell,
        drain_sec=drain_sec,
    )


async def _listening(handler: _Handler) -> tuple[Server, str]:
    server = await serve(handler, "127.0.0.1", 0)
    return server, f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"


async def _until_exit(ws: ServerConnection) -> tuple[bytes, int]:
    """Collect output until the shell's exit frame; return both."""
    output = b""
    async for message in ws:
        assert isinstance(message, bytes)
        kind, payload = decode_frame(message)
        if kind is Frame.OUTPUT:
            output += payload
        elif kind is Frame.EXIT:
            return output, int.from_bytes(payload)
    raise AssertionError("closed before the shell exited")


async def _first_line(ws: ServerConnection) -> int:
    """Collect output to its first newline, which may span frames; parse it."""
    output = b""
    async for message in ws:
        assert isinstance(message, bytes)
        kind, payload = decode_frame(message)
        if kind is Frame.OUTPUT:
            output += payload
        if b"\n" in output:
            return int(output.split()[0])
    raise AssertionError("closed before the shell wrote a line")


async def _run(host: TerminalHost, opened: TerminalWait) -> None:
    await asyncio.to_thread(
        host.serve,
        opened,
        machine_id=_MACHINE,
        instance=_INSTANCE,
    )


async def test_a_shell_runs_on_a_real_pty_and_its_output_and_exit_status_arrive() -> (
    None
):
    seen: dict[str, object] = {}

    async def handler(ws: ServerConnection) -> None:
        assert ws.request is not None
        seen["path"] = ws.request.path
        seen["auth"] = ws.request.headers["Authorization"]
        seen["result"] = await _until_exit(ws)

    server, url = await _listening(handler)
    opened = _opened()
    await _run(_host(url, "/bin/sh", "-c", "printf ok; exit 3"), opened)
    server.close()

    path = wire_terminals.MACHINE_TERMINAL_WS_PATH.format(
        machine_id=_MACHINE,
        terminal_id=opened.terminal_id,
    )
    assert seen["path"] == f"{path}?instance={_INSTANCE}"
    assert seen["auth"] == f"Bearer {_CREDENTIAL}"
    assert seen["result"] == (b"ok", 3)


async def test_input_reaches_the_shell_and_a_resize_is_clamped() -> None:
    result: list[tuple[bytes, int]] = []

    async def handler(ws: ServerConnection) -> None:
        await ws.send(encode_frame(Frame.RESIZE, encode_winsize(0, 5_000)))
        await ws.send(encode_frame(Frame.INPUT, b"go\n"))
        result.append(await _until_exit(ws))

    server, url = await _listening(handler)
    await _run(_host(url, "/bin/sh", "-c", "read x; stty size; echo got-$x"), _opened())
    server.close()

    output, status = result[0]
    assert status == 0
    assert b"1 1000" in output
    assert b"got-go" in output


async def test_stop_and_a_closed_socket_each_kill_the_shells_whole_group() -> None:
    pids: list[int] = []

    # The pid and its newline are written apart, as a pty may deliver them anyway.
    pid_line = "printf %s $$; sleep 0.02; echo; sleep 30"

    async def stopper(ws: ServerConnection) -> None:
        pids.append(await _first_line(ws))
        await ws.send(encode_frame(Frame.STOP))
        _, status = await _until_exit(ws)
        pids.append(status)

    server, url = await _listening(stopper)
    await _run(_host(url, "/bin/sh", "-c", pid_line), _opened())
    server.close()
    pid, status = pids
    assert status > 128
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)

    gone: list[int] = []

    async def leaver(ws: ServerConnection) -> None:
        gone.append(await _first_line(ws))
        await ws.close()

    server, url = await _listening(leaver)
    await _run(_host(url, "/bin/sh", "-c", pid_line), _opened())
    server.close()
    with pytest.raises(ProcessLookupError):
        os.kill(gone[0], 0)


async def test_the_shell_starts_clean_with_the_trax_context_and_no_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "sentinel-secret")
    monkeypatch.setenv("TRACKINIZER_TOKEN", "sentinel-token")
    result: list[tuple[bytes, int]] = []

    async def handler(ws: ServerConnection) -> None:
        result.append(await _until_exit(ws))

    server, url = await _listening(handler)
    workspace, workstream = uuid.uuid4(), uuid.uuid4()
    host = _host(
        url,
        "/bin/sh",
        "-c",
        "env",
        trax_env={"LC_TRAX_SERVER": url, "TRACKINIZER_PROFILE": "hosted"},
    )
    await _run(host, _opened(workspace=workspace, workstream=workstream))
    server.close()

    lines = set(result[0][0].decode().split())
    assert {
        "TERM=xterm-256color",
        "COLORTERM=truecolor",
        f"LC_TRAX_SERVER={url}",
        "TRACKINIZER_PROFILE=hosted",
        f"TRAX_WORKSPACE={workspace}",
        f"LC_TRAX_WORKSPACE={workspace}",
        f"TRAX_WORKSTREAM={workstream}",
        f"LC_TRAX_WORKSTREAM={workstream}",
    } <= lines
    assert not [line for line in lines if "sentinel" in line]


async def test_each_open_and_close_is_logged_once_for_this_user_only() -> None:
    async def handler(ws: ServerConnection) -> None:
        await _until_exit(ws)

    server, url = await _listening(handler)
    opened = _opened(requested_by="helper-1")
    await _run(_host(url, "/bin/sh", "-c", "exit 5"), opened)
    server.close()

    path = state_dir() / "rekursiv-ai" / "trax" / "machines" / "box.terminals.log"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    first, second = path.read_text().splitlines()
    assert f'"event":"open","terminal_id":"{opened.terminal_id}"' in first
    assert '"opener":"ada@x.y"' in first
    assert '"requested_by":"helper-1"' in first
    assert '"event":"close"' in second
    assert '"status":5' in second


async def _five_opens(
    wait_for: Callable[[list[ServerConnection]], bool],
) -> tuple[TerminalHost, _Client, list[ServerConnection], Server]:
    """Start a host that is asked for five terminals; return once ``wait_for`` is true."""
    conns: list[ServerConnection] = []
    arrived = asyncio.Event()

    async def handler(ws: ServerConnection) -> None:
        conns.append(ws)
        arrived.set()
        await ws.wait_closed()

    server, url = await _listening(handler)
    client = _Client([_opened() for _ in range(5)])
    host = _host(url, "/bin/sh", "-c", "sleep 30", client=client)
    host.start(_MACHINE, _INSTANCE)
    for _ in range(500):
        await asyncio.sleep(0.002)
        if wait_for(conns):
            break
    return host, client, conns, server


async def test_a_host_serves_five_terminals_at_once_and_refuses_none() -> None:
    host, _, conns, server = await _five_opens(lambda conns: len(conns) == 5)

    open_now = [ws.close_code is None for ws in conns]
    await asyncio.to_thread(host.stop)
    server.close()

    assert open_now == [True] * 5


async def test_stop_ends_every_bridge_and_the_listener_lets_go_of_its_client() -> None:
    host, client, conns, server = await _five_opens(
        lambda conns: len(conns) == 5,
    )

    await asyncio.to_thread(host.stop)
    server.close()

    assert all(ws.close_code is not None for ws in conns)
    # The listener's held request ends by itself, then it lets go of its client.
    assert await asyncio.to_thread(client.closed.wait, 10)


async def test_the_exit_is_reported_though_a_job_the_shell_left_still_holds_the_pty() -> (
    None
):
    result: list[tuple[bytes, int]] = []

    async def handler(ws: ServerConnection) -> None:
        result.append(await _until_exit(ws))

    server, url = await _listening(handler)
    # ``setsid`` puts the job in a group of its own, as a job of an interactive shell
    # is, so it holds the pty open after the shell is gone.
    host = _host(
        url,
        "/bin/sh",
        "-c",
        "setsid sleep 7.31 & exit 4",
        drain_sec=0.02,
    )
    try:
        await asyncio.wait_for(_run(host, _opened()), 3)
    finally:
        server.close()
        await asyncio.to_thread(
            subprocess.run,
            ["pkill", "-f", "^sleep 7[.]31$"],
            check=False,
        )

    assert result[0][1] == 4


async def test_a_410_ends_the_listener_and_any_other_failure_is_retried() -> None:
    client = _Client([ClientError("-> 410", status_code=410)])
    host = _host("http://127.0.0.1:1", client=client)

    host.start(_MACHINE, _INSTANCE)
    await asyncio.to_thread(client.closed.wait, 10)

    assert client.closed.is_set()


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
