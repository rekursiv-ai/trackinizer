"""The terminal routes' machine lookup, over a fake connection, one branch at a time.

The routes end to end are in ``terminal_routes_pglite_test.py`` over PGlite. These tests
need no database, so they pin each branch of the lookup that decides whether a terminal
may open, which CLIs its machine offers, and whether it serves files.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from functools import partial
from typing import TYPE_CHECKING, Literal, cast, override
from unittest.mock import AsyncMock

import asyncio
import uuid

from fastapi import HTTPException
from starlette.requests import Request
from starlette.websockets import WebSocketDisconnect
from treekle import from_plain

import pytest

from trackinizer.conftest import FakeEngine
from trackinizer.lib.posix.host import Frame
from trackinizer.mock_test_support import MockConnection
from trackinizer.server.api import terminal_routes
from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import make_test_identity, patch_state
from trackinizer.server.config import Config
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.terminals import (
    Opening,
    SocketClosedError,
    Terminal,
    TerminalHub,
)
from trackinizer.types.errors import NotFoundError
from trackinizer.wire.wire_machine_host import OFFLINE_AFTER_SEC
from trackinizer.wire.wire_terminals import OpenTerminal, TerminalWait


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import WebSocket
    from starlette.types import Message

    from trackinizer.wire.wire_terminals import TerminalKind


pytestmark = pytest.mark.asyncio

_MACHINE = uuid.UUID("9f1c2e3a-0000-4000-8000-000000000001")
_IDENTITY = make_test_identity()


def _request(conn: AsyncMock, monkeypatch: pytest.MonkeyPatch) -> Request:
    patch_state(monkeypatch, "engine", FakeEngine(conn))
    return Request({"type": "http", "app": app})


async def test_a_confirmed_machine_is_returned_with_its_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = AsyncMock(
        fetchrow=AsyncMock(
            return_value={
                "id": str(_MACHINE),
                "facts": {"clis": ["claude", "git"]},
                "confirmed": True,
            },
        ),
    )

    found = await terminal_routes._owned_machine(
        _request(conn, monkeypatch),
        _IDENTITY,
        name="gx10",
    )

    assert found == (_MACHINE, {"clis": ["claude", "git"]})


async def test_the_lookup_asks_for_an_online_machine_of_the_caller_with_a_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = AsyncMock(
        fetchrow=AsyncMock(
            return_value={
                "id": str(_MACHINE),
                "facts": dict[str, object](),
                "confirmed": True,
            },
        ),
    )
    conn = cast(MockConnection, raw)

    _ = await terminal_routes._owned_machine(
        _request(raw, monkeypatch),
        _IDENTITY,
        name="gx10",
    )

    # The whole query, since any changed word in it lets an unscoped machine, or one
    # whose host is gone, be offered a terminal.
    assert conn.fetchrow.call_args is not None
    assert from_plain(conn.fetchrow.call_args.args[0], str) == (
        "SELECT m.id, m.facts, m.confirmed_at IS NOT NULL AS confirmed "
        "FROM machines m WHERE m.name = $1 AND m.owner = $2 "
        "AND m.host_instance IS NOT NULL "
        "AND m.last_heartbeat > now() - make_interval(secs => $3) "
        "AND EXISTS (SELECT 1 FROM machine_credentials c "
        "WHERE c.machine_id = m.id AND c.revoked_at IS NULL)"
    )
    assert conn.fetchrow.call_args.args[1:] == (
        "gx10",
        _IDENTITY.email,
        OFFLINE_AFTER_SEC,
    )


async def test_an_unconfirmed_machine_is_refused_until_its_owner_confirms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = AsyncMock(
        fetchrow=AsyncMock(
            return_value={
                "id": str(_MACHINE),
                "facts": dict[str, object](),
                "confirmed": False,
            },
        ),
    )

    with pytest.raises(HTTPException) as refused:
        _ = await terminal_routes._owned_machine(
            _request(conn, monkeypatch),
            _IDENTITY,
            name="gx10",
        )

    detail = from_plain(refused.value.detail, dict[str, str])
    assert (refused.value.status_code, detail) == (
        409,
        {"code": "terminal_unconfirmed", "message": "confirm this machine first"},
    )


async def test_a_machine_the_caller_does_not_own_or_that_is_offline_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = AsyncMock(fetchrow=AsyncMock(return_value=None))

    with pytest.raises(NotFoundError) as missing:
        _ = await terminal_routes._owned_machine(
            _request(conn, monkeypatch),
            _IDENTITY,
            name="gx10",
        )

    assert str(missing.value) == "no terminal-capable machine gx10"


async def test_facts_list_the_clis_and_none_without_a_list() -> None:
    assert terminal_routes._machine_clis({"clis": ["claude", "git"]}) == [
        "claude",
        "git",
    ]
    assert terminal_routes._machine_clis({"os": "linux", "cpus": 20}) == []


@pytest.mark.parametrize("listed", ["claude", True, 3])
async def test_facts_whose_cli_list_is_not_a_list_offer_no_cli(listed: object) -> None:
    assert terminal_routes._machine_clis({"clis": listed}) == []


@pytest.mark.parametrize(
    ("facts", "serves"),
    [({"files": True}, True), ({"files": "yes"}, False), ({}, False), (None, False)],
)
async def test_only_facts_that_say_files_serve_a_files_terminal(
    facts: object,
    serves: bool,
) -> None:
    assert terminal_routes._serves_files(facts) is serves


async def _alive(term: Terminal) -> bool:
    del term
    return True


async def _barrier_update(barrier: asyncio.Barrier, *args: object) -> str:
    del args
    await barrier.wait()
    return "UPDATE 1"


class _ConcurrentEngine(FakeEngine):
    @override
    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[AsyncMock]:
        """Model independent connections in the production pool."""
        yield self.conn


async def test_concurrent_opens_reserve_one_run_for_a_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = AsyncMock(
        execute=AsyncMock(side_effect=partial(_barrier_update, asyncio.Barrier(2))),
    )
    request = _request(conn, monkeypatch)
    patch_state(monkeypatch, "engine", _ConcurrentEngine(conn))
    patch_state(monkeypatch, "terminals", TerminalHub(alive=_alive))
    patch_state(monkeypatch, "inbound", InboundQueue())
    opening = Opening(
        user=_IDENTITY.email,
        machine_id=_MACHINE,
        machine="gx10",
        rows=24,
        cols=80,
        command="claude",
        attach=uuid.uuid4(),
    )

    answers = await asyncio.gather(
        terminal_routes._open_terminal(request, opening=opening),
        terminal_routes._open_terminal(request, opening=opening),
        return_exceptions=True,
    )

    assert sum(isinstance(answer, uuid.UUID) for answer in answers) == 1
    [refused] = [answer for answer in answers if isinstance(answer, HTTPException)]
    assert refused.status_code == 409
    assert from_plain(refused.detail, dict[str, str])["code"] == "chat_run_live"


@pytest.mark.parametrize(("kind", "offered"), [("files", "files"), ("shell", None)])
async def test_the_next_open_is_offered_to_the_host_with_all_it_was_opened_with(
    kind: TerminalKind,
    offered: Literal["files"] | None,
) -> None:
    hub = TerminalHub(alive=_alive, ice_servers=("stun:stun.example:3478",))
    canvas, workspace, session = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    term = hub.open(
        Opening(
            user="ada@x.y",
            machine_id=_MACHINE,
            machine="gx10",
            rows=40,
            cols=120,
            canvas=canvas,
            workspace=workspace,
            requested_by="helper-1",
            command="codex",
            attach=session,
            run_name="campaign-1",
            cwd="~/work",
            kind=kind,
        ),
    )

    wait = await terminal_routes._next_terminal(hub, machine_id=_MACHINE, wait_sec=0)

    assert wait == TerminalWait(
        terminal_id=term.id,
        opener="ada@x.y",
        rows=40,
        cols=120,
        canvas=canvas,
        workspace=workspace,
        requested_by="helper-1",
        command="codex",
        attach=session,
        run_name="campaign-1",
        cwd="~/work",
        kind=offered,
        ice_servers=["stun:stun.example:3478"],
    )


async def test_a_host_that_waits_out_its_hold_is_offered_nothing() -> None:
    hub = TerminalHub(alive=_alive)

    assert (
        await terminal_routes._next_terminal(hub, machine_id=_MACHINE, wait_sec=0)
        is None
    )


class _Socket:
    """A WebSocket as a leg uses it: each call answers or raises as scripted."""

    def __init__(self, *received: Message | BaseException) -> None:
        self._received = list(received)
        self.send_error: BaseException | None = None

    async def send_bytes(self, data: bytes) -> None:
        del data
        if self.send_error is not None:
            raise self.send_error

    async def receive(self) -> Message:
        if not self._received:
            await asyncio.sleep(3600)
        answer = self._received.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _leg(socket: _Socket) -> terminal_routes._Leg:
    return terminal_routes._Leg(cast("WebSocket", socket))


@pytest.mark.parametrize(
    "error",
    [WebSocketDisconnect(1006), RuntimeError("closed"), OSError("reset")],
)
async def test_a_send_to_a_peer_that_left_is_a_closed_socket(
    error: BaseException,
) -> None:
    socket = _Socket()
    socket.send_error = error

    with pytest.raises(SocketClosedError):
        await _leg(socket).send_bytes(b"x")


@pytest.mark.parametrize(
    "received",
    [
        WebSocketDisconnect(1000),
        RuntimeError("not connected"),
        {"type": "websocket.receive", "text": "hi"},
        {"type": "websocket.receive", "bytes": b""},
    ],
    ids=["disconnect", "not-connected", "text", "no-frame"],
)
async def test_anything_but_a_frame_from_a_peer_reads_as_its_leaving(
    received: Message | BaseException,
) -> None:
    assert await terminal_routes._read_frame(_leg(_Socket(received))) is None


async def test_a_first_frame_that_never_comes_is_none() -> None:
    assert await terminal_routes._first_frame(_leg(_Socket()), timeout_sec=0.01) is None


@pytest.mark.parametrize(
    "frame",
    [None, (Frame.INPUT, b"x"), (Frame.ATTACH, b"short")],
    ids=["none", "not-attach", "bad-attach"],
)
async def test_only_a_well_formed_attach_frame_names_a_ticket(
    frame: tuple[Frame, bytes] | None,
) -> None:
    assert terminal_routes._parse_attach(frame) is None


async def test_with_authentication_off_no_one_opens_a_run_for_an_asker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every caller is a synthetic admin then, so none is trusted to name an asker."""
    patch_state(monkeypatch, "config", Config(auth_disabled=True))

    with pytest.raises(HTTPException) as refused:
        await terminal_routes._terminal_owner(
            Request({"type": "http", "app": app, "headers": []}),
            _IDENTITY,
            asker="ada@x.y",
            body=OpenTerminal(machine="gx10", rows=24, cols=80),
        )

    assert refused.value.status_code == 403


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
