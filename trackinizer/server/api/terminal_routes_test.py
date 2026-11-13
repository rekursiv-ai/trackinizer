"""The terminal routes' host lookup, over a fake connection, one branch at a time.

The routes end to end are in ``terminal_routes_pglite_test.py`` over PGlite. These tests
need no database, so they pin each branch of the lookup that decides whether a terminal
may open, which CLIs its host offers, and whether it serves files.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, Mock

import asyncio
import uuid

from fastapi import HTTPException, WebSocket
from starlette.requests import Request
from starlette.websockets import WebSocketDisconnect
from treekle import from_plain

import pytest

from trackinizer.conftest import FakeEngine, make_conn, make_store
from trackinizer.lib.posix.host import Frame, encode_frame
from trackinizer.mock_test_support import MockConnection
from trackinizer.server import auth
from trackinizer.server.api import terminal_routes
from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import make_test_identity, patch_state
from trackinizer.server.api.replays import Replays
from trackinizer.server.api.session_identity import Asker
from trackinizer.server.config import Config
from trackinizer.server.terminals import (
    Opening,
    SocketClosedError,
    Terminal,
    TerminalRelay,
)
from trackinizer.types.errors import NotFoundError
from trackinizer.wire import wire_terminals
from trackinizer.wire.wire_daemon import MIN_DAEMON_VERSION, OFFLINE_AFTER_SEC
from trackinizer.wire.wire_terminals import OpenTerminal, TerminalWait


if TYPE_CHECKING:
    from starlette.types import Message

    from trackinizer.mock_test_support import AsyncMockProtocol


pytestmark = pytest.mark.asyncio

_HOST = uuid.UUID("9f1c2e3a-0000-4000-8000-000000000001")
_IDENTITY = make_test_identity()


def _request(conn: AsyncMock, monkeypatch: pytest.MonkeyPatch) -> Request:
    patch_state(monkeypatch, "engine", FakeEngine(conn))
    return Request({"type": "http", "app": app})


async def test_a_confirmed_host_is_returned_by_its_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = AsyncMock(
        fetchrow=AsyncMock(return_value={"id": str(_HOST), "confirmed": True}),
    )

    found = await terminal_routes._owned_host(
        _request(conn, monkeypatch),
        _IDENTITY,
        name="gx10",
    )

    assert found == _HOST


async def test_the_lookup_asks_for_an_online_host_of_the_caller_with_a_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = AsyncMock(
        fetchrow=AsyncMock(return_value={"id": str(_HOST), "confirmed": True}),
    )
    conn = cast(MockConnection, raw)

    _ = await terminal_routes._owned_host(
        _request(raw, monkeypatch),
        _IDENTITY,
        name="gx10",
    )

    # The whole query, since any changed word in it lets an unscoped host, or one
    # whose host is gone, be offered a terminal.
    assert conn.fetchrow.call_args is not None
    assert from_plain(conn.fetchrow.call_args.args[0], str) == (
        "SELECT m.id, m.confirmed_at IS NOT NULL AS confirmed "
        "FROM hosts m WHERE m.name = $1 AND m.owner = $2 "
        "AND m.daemon_instance IS NOT NULL "
        "AND m.last_heartbeat > now() - make_interval(secs => $3) "
        "AND EXISTS (SELECT 1 FROM host_credentials c "
        "WHERE c.host_id = m.id AND c.revoked_at IS NULL)"
    )
    assert conn.fetchrow.call_args.args[1:] == (
        "gx10",
        _IDENTITY.email,
        OFFLINE_AFTER_SEC,
    )


async def test_an_unconfirmed_host_is_refused_until_its_owner_confirms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = AsyncMock(
        fetchrow=AsyncMock(
            return_value={
                "id": str(_HOST),
                "facts": dict[str, object](),
                "confirmed": False,
            },
        ),
    )

    with pytest.raises(HTTPException) as refused:
        _ = await terminal_routes._owned_host(
            _request(conn, monkeypatch),
            _IDENTITY,
            name="gx10",
        )

    detail = from_plain(refused.value.detail, dict[str, str])
    assert (refused.value.status_code, detail) == (
        409,
        {"code": "terminal_unconfirmed", "message": "confirm this host first"},
    )


async def test_a_host_the_caller_does_not_own_or_that_is_offline_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = AsyncMock(fetchrow=AsyncMock(return_value=None))

    with pytest.raises(NotFoundError) as missing:
        _ = await terminal_routes._owned_host(
            _request(conn, monkeypatch),
            _IDENTITY,
            name="gx10",
        )

    assert str(missing.value) == "no terminal-capable host gx10"


async def test_facts_list_the_clis_and_none_without_a_list() -> None:
    assert terminal_routes._host_clis({"clis": ["claude", "git"]}) == [
        "claude",
        "git",
    ]
    assert terminal_routes._host_clis({"os": "linux", "cpus": 20}) == []


@pytest.mark.parametrize("listed", ["claude", True, 3])
async def test_facts_whose_cli_list_is_not_a_list_offer_no_cli(listed: object) -> None:
    assert terminal_routes._host_clis({"clis": listed}) == []


async def _alive(term: Terminal) -> bool:
    del term
    return True


async def test_the_next_open_is_offered_to_the_host_with_all_it_was_opened_with() -> (
    None
):
    relay = TerminalRelay(alive=_alive, ice_servers=("stun:stun.example:3478",))
    canvas, workspace = uuid.uuid4(), uuid.uuid4()
    term = relay.open(
        Opening(
            user="ada@x.y",
            host_id=_HOST,
            host="gx10",
            rows=40,
            cols=120,
            canvas=canvas,
            workspace=workspace,
            requested_by="helper-1",
            cwd="~/work",
        ),
    )

    wait = await terminal_routes._next_terminal(relay, host_id=_HOST, wait_sec=0)

    assert wait == TerminalWait(
        terminal_id=term.id,
        opener="ada@x.y",
        rows=40,
        cols=120,
        canvas=canvas,
        workspace=workspace,
        requested_by="helper-1",
        cwd="~/work",
        ice_servers=["stun:stun.example:3478"],
    )


async def test_a_host_that_waits_out_its_hold_is_offered_nothing() -> None:
    relay = TerminalRelay(alive=_alive)

    assert (
        await terminal_routes._next_terminal(relay, host_id=_HOST, wait_sec=0) is None
    )


class _Socket:
    """A WebSocket as a socket uses it: each call answers or raises as scripted."""

    def __init__(self, *received: Message | BaseException) -> None:
        self._received = list(received)
        self.send_error: BaseException | None = None

    async def send_bytes(self, data: bytes) -> None:
        del data
        if self.send_error is not None:
            raise self.send_error

    async def receive(self) -> Message:
        if not self._received:
            _ = await asyncio.Event().wait()
        answer = self._received.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _socket(socket: _Socket) -> terminal_routes.StarletteSocket:
    return terminal_routes.StarletteSocket(cast("WebSocket", socket))


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
        await _socket(socket).send_bytes(b"x")


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
    assert await terminal_routes._read_frame(_socket(_Socket(received))) is None


async def test_a_first_frame_that_never_comes_is_none() -> None:
    assert (
        await terminal_routes._first_frame(_socket(_Socket()), timeout_sec=0.01) is None
    )


@pytest.mark.parametrize(
    "frame",
    [None, (Frame.INPUT, b"x"), (Frame.ATTACH, b"short")],
    ids=["none", "not-attach", "bad-attach"],
)
async def test_only_a_well_formed_attach_frame_names_a_ticket(
    frame: tuple[Frame, bytes] | None,
) -> None:
    assert terminal_routes._parse_attach(frame) is None


def _browser_request(monkeypatch: pytest.MonkeyPatch) -> Request:
    patch_state(monkeypatch, "config", Config(auth_disabled=False))
    patch_state(monkeypatch, "replays", Replays())
    return Request(
        {
            "type": "http",
            "app": app,
            "headers": [(b"host", b"trax.test"), (b"origin", b"https://trax.test")],
        },
    )


def _opening(*, canvas: uuid.UUID | None = None) -> Opening:
    return Opening(
        user=_IDENTITY.email,
        host_id=_HOST,
        host="gx10",
        rows=2,
        cols=3,
        canvas=canvas,
        surface="chat",
    )


async def test_open_route_queues_once_and_mints_fresh_replay_tickets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _browser_request(monkeypatch)
    request.state.idempotency_key = uuid.uuid4()
    relay = TerminalRelay(alive=_alive, ice_servers=("stun:trax.test:3478",))
    patch_state(monkeypatch, "terminals", relay)
    await relay.wait(_HOST, wait_sec=0)
    monkeypatch.setattr(
        terminal_routes,
        "_owned_host",
        AsyncMock(return_value=_HOST),
    )
    body = OpenTerminal(host="gx10", rows=2, cols=3, surface="chat")
    human = replace(_IDENTITY, api_key_id=None)
    first = await terminal_routes.open_terminal_route(body, request, human)
    second = await terminal_routes.open_terminal_route(body, request, human)
    assert first.terminal_id == second.terminal_id
    assert first.ticket != second.ticket
    assert first.ice_servers == ["stun:trax.test:3478"]
    term = relay.terminal(first.terminal_id)
    assert term is not None
    assert (term.rows, term.cols, term.surface, term.user) == (
        2,
        3,
        "chat",
        human.email,
    )
    assert relay.redeem(first.ticket, user=human.email, terminal_id=term.id)
    await relay.end(term, code=wire_terminals.CLOSE_ENDED)
    with pytest.raises(HTTPException) as ended:
        await terminal_routes.open_terminal_route(body, request, human)
    assert ended.value.status_code == 410


@pytest.mark.parametrize("failure", ["offline", "limit"])
async def test_open_route_reports_readiness_and_rate_refusals(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    request = _browser_request(monkeypatch)
    relay = TerminalRelay(alive=_alive)
    patch_state(monkeypatch, "terminals", relay)
    if failure != "offline":
        await relay.wait(_HOST, wait_sec=0)
    if failure == "limit":
        for _ in range(wire_terminals.MAX_OPENS_PER_MIN_PER_USER):
            relay.open(_opening())
    monkeypatch.setattr(
        terminal_routes,
        "_owned_host",
        AsyncMock(return_value=_HOST),
    )
    body = OpenTerminal(host="gx10", rows=2, cols=3)
    with pytest.raises(HTTPException) as refused:
        await terminal_routes.open_terminal_route(
            body,
            request,
            replace(_IDENTITY, api_key_id=None),
        )
    assert refused.value.status_code == (429 if failure == "limit" else 409)
    assert (
        from_plain(refused.value.detail, dict[str, str])["code"]
        == {
            "offline": "terminal_not_enabled",
            "limit": "terminal_limit",
        }[failure]
    )


async def test_host_listing_filters_owner_sorts_and_includes_canvas_terminals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _browser_request(monkeypatch)
    store, engine = make_store()
    patch_state(monkeypatch, "engine", engine)
    patch_state(monkeypatch, "store", store)
    relay = TerminalRelay(alive=_alive)
    patch_state(monkeypatch, "terminals", relay)
    await relay.wait(_HOST, wait_sec=0)
    canvas = uuid.uuid4()
    term = relay.open(_opening(canvas=canvas))
    created = datetime.now(UTC)
    row = {
        "id": str(_HOST),
        "name": "gx10",
        "owner": _IDENTITY.email,
        "status": "online",
        "confirmed_at": created,
        "facts": {"clis": ["claude"], "tools": True},
        "last_run_cwd": "~/work",
        "daemon_version": MIN_DAEMON_VERSION,
        "created": created,
    }
    fetch = AsyncMock(
        return_value=[
            row,
            {**row, "name": "foreign", "owner": "other@example.com"},
            {
                **row,
                "name": "older",
                "status": "offline",
                "confirmed_at": None,
                "created": created - timedelta(seconds=1),
            },
        ],
    )
    monkeypatch.setattr(terminal_routes, "fetch_hosts", fetch)
    monkeypatch.setattr(terminal_routes, "live_sessions", Mock(return_value=[]))
    human = replace(_IDENTITY, api_key_id=None)
    listing = await terminal_routes.list_terminal_hosts_route(
        request,
        human,
        canvas=canvas,
    )
    assert [host.name for host in listing.hosts] == ["gx10", "older"]
    assert [host.terminal_ready for host in listing.hosts] == [True, False]
    assert [host.confirmed for host in listing.hosts] == [True, False]
    assert listing.hosts[0].clis == ["claude"]
    assert listing.hosts[0].tools is True
    assert listing.hosts[0].cwd == "~/work"
    assert [(live.terminal_id, live.surface) for live in listing.terminals] == [
        (term.id, "chat"),
    ]
    without_canvas = await terminal_routes.list_terminal_hosts_route(request, human)
    assert without_canvas.terminals == []


@pytest.mark.parametrize("exists", [False, True])
async def test_confirm_updates_only_the_browser_owners_host(
    monkeypatch: pytest.MonkeyPatch,
    exists: bool,
) -> None:
    request = _browser_request(monkeypatch)
    conn = make_conn()
    conn.fetchval.return_value = _HOST if exists else None
    patch_state(monkeypatch, "engine", FakeEngine(conn))
    human = replace(_IDENTITY, api_key_id=None)
    if exists:
        assert (
            await terminal_routes.confirm_terminal_host_route(
                "gx10",
                request,
                human,
            )
            is None
        )
    else:
        with pytest.raises(NotFoundError, match="no host gx10 of yours"):
            await terminal_routes.confirm_terminal_host_route("gx10", request, human)
    assert conn.fetchval.call_args is not None
    assert conn.fetchval.call_args.args[1:] == ("gx10", human.email)
    assert "COALESCE(confirmed_at, now())" in str(conn.fetchval.call_args.args[0])


async def test_attach_and_close_require_a_live_terminal_of_the_human_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _browser_request(monkeypatch)
    relay = TerminalRelay(alive=_alive, ice_servers=("stun:trax.test:3478",))
    patch_state(monkeypatch, "terminals", relay)
    term = relay.open(_opening())
    human = replace(_IDENTITY, api_key_id=None)
    ticket = await terminal_routes.attach_terminal_route(term.id, request, human)
    assert ticket.ice_servers == ["stun:trax.test:3478"]
    assert relay.redeem(ticket.ticket, user=human.email, terminal_id=term.id)
    with pytest.raises(NotFoundError):
        await terminal_routes.attach_terminal_route(
            term.id,
            request,
            replace(human, email="other@example.com"),
        )
    await terminal_routes.close_terminal_route(term.id, request, human)
    assert term.stopping
    assert term.ended
    with pytest.raises(NotFoundError):
        await terminal_routes.attach_terminal_route(term.id, request, human)


@pytest.mark.parametrize("disabled", [False, True])
async def test_host_wait_checks_authentication_and_clamps_the_hold(
    monkeypatch: pytest.MonkeyPatch,
    disabled: bool,
) -> None:
    request = _browser_request(monkeypatch)
    patch_state(monkeypatch, "config", Config(auth_disabled=disabled))
    relay = TerminalRelay(alive=_alive)
    patch_state(monkeypatch, "terminals", relay)
    current = AsyncMock()
    instance = AsyncMock()
    wait = AsyncMock(return_value=None)
    monkeypatch.setattr(terminal_routes, "current_host", current)
    monkeypatch.setattr(terminal_routes, "require_instance", instance)
    monkeypatch.setattr(relay, "wait", wait)
    daemon_instance = uuid.uuid4()
    if disabled:
        with pytest.raises(HTTPException) as refused:
            await terminal_routes.terminal_wait_route(
                _HOST,
                request,
                daemon_instance,
                wait_sec=99,
            )
        assert refused.value.status_code == 403
        current.assert_not_awaited()
    else:
        assert (
            await terminal_routes.terminal_wait_route(
                _HOST,
                request,
                daemon_instance,
                wait_sec=99,
            )
            is None
        )
        current.assert_awaited_once_with(request, _HOST)
        instance.assert_awaited_once_with(request, _HOST, instance=daemon_instance)
        wait.assert_awaited_once_with(_HOST, wait_sec=wire_terminals.WAIT_MAX_SEC)


@pytest.mark.parametrize("bad", ["key", "service", "origin", "disabled"])
async def test_an_open_refuses_nonhuman_and_foreign_browser_requests(
    monkeypatch: pytest.MonkeyPatch,
    bad: str,
) -> None:
    request = _browser_request(monkeypatch)
    identity = replace(_IDENTITY, api_key_id=None)
    if bad == "key":
        identity = _IDENTITY
    elif bad == "service":
        identity = replace(identity, email="agent@service.invalid")
    elif bad == "origin":
        request.scope["headers"] = list[tuple[bytes, bytes]]()
    else:
        patch_state(monkeypatch, "config", Config(auth_disabled=True))
    with pytest.raises(HTTPException) as refused:
        await terminal_routes.open_terminal_route(
            OpenTerminal(host="gx10", rows=2, cols=3),
            request,
            identity,
        )
    assert refused.value.status_code == 403


@pytest.mark.parametrize(
    "mode",
    ["human", "key", "foreign", "viewer", "writer", "disabled"],
)
async def test_host_owner_refuses_unattested_names_and_viewers(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    request = _browser_request(monkeypatch)
    caller = _IDENTITY if mode == "key" else replace(_IDENTITY, api_key_id=None)
    found = Asker(
        email="ada@example.com",
        role="viewer" if mode == "viewer" else "writer",
        read_audiences=frozenset(),
    )
    monkeypatch.setattr(
        terminal_routes,
        "resolve_asker",
        AsyncMock(return_value=found if mode in {"viewer", "writer"} else None),
    )
    patch_state(monkeypatch, "config", Config(auth_disabled=mode == "disabled"))
    owner = None if mode in {"human", "key", "disabled"} else found.email
    if mode in {"human", "key", "writer"}:
        assert await terminal_routes._hosts_owner(
            request,
            caller,
            owner=owner,
        ) == (found.email if mode == "writer" else caller.email)
    else:
        with pytest.raises(HTTPException) as refused:
            await terminal_routes._hosts_owner(request, caller, owner=owner)
        assert refused.value.status_code == 403


class _WebPeer:
    """Script an ASGI peer without a server or a network socket."""

    def __init__(self, *frames: bytes) -> None:
        self.messages: list[Message] = [
            {"type": "websocket.connect"},
            *({"type": "websocket.receive", "bytes": frame} for frame in frames),
            {"type": "websocket.disconnect", "code": 1000},
        ]
        self.sent: list[Message] = []

    async def receive(self) -> Message:
        """Return the next scripted peer message."""
        return self.messages.pop(0)

    async def send(self, message: Message) -> None:
        """Record one message sent to the peer."""
        self.sent.append(message)

    def socket(self) -> WebSocket:
        """Build the WebSocket adapter the route receives."""
        return WebSocket(
            {
                "type": "websocket",
                "app": app,
                "headers": [
                    (b"host", b"trax.test"),
                    (b"origin", b"https://trax.test"),
                ],
            },
            self.receive,
            self.send,
        )


@pytest.mark.parametrize(
    "mode",
    [
        "ok",
        "bad_frame",
        "unknown",
        "key",
        "ticket",
        "auth",
        "origin",
        "disabled",
        "invalid",
        "taken",
    ],
)
async def test_browser_socket_authenticates_attaches_relays_and_detaches(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    _browser_request(monkeypatch)
    relay = TerminalRelay(alive=_alive)
    patch_state(monkeypatch, "terminals", relay)
    term = relay.open(_opening())
    ticket = relay.mint_ticket(term)
    attach = encode_frame(
        Frame.ATTACH,
        ("x" * 43 if mode == "ticket" else ticket).encode() + (0).to_bytes(8),
    )
    peer = _WebPeer(
        b"bad" if mode == "bad_frame" else attach,
        encode_frame(Frame.INPUT, b"x"),
    )
    socket = peer.socket()
    identity = _IDENTITY if mode == "key" else replace(_IDENTITY, api_key_id=None)
    monkeypatch.setattr(
        auth,
        "request_user",
        AsyncMock(
            return_value=identity,
            side_effect=HTTPException(401) if mode == "auth" else None,
        ),
    )
    if mode == "origin":
        socket.scope["headers"] = list[tuple[bytes, bytes]]()
    if mode == "disabled":
        patch_state(monkeypatch, "config", Config(auth_disabled=True))
    if mode == "invalid":
        monkeypatch.setattr(relay, "from_browser", AsyncMock(return_value=False))
    if mode == "taken":
        monkeypatch.setattr(relay, "attach_browser", AsyncMock())
    await terminal_routes.terminal_web_socket(
        socket,
        uuid.uuid4() if mode == "unknown" else term.id,
    )
    assert peer.sent[0]["type"] == "websocket.accept"
    assert peer.sent[-1] == {
        "type": "websocket.close",
        "code": wire_terminals.CLOSE_REFUSED,
        "reason": "",
    }
    assert term.browser is None
    assert bytes(term.pre_input) == (b"x" if mode == "ok" else b"")
    assert any(
        message.get("bytes") == encode_frame(Frame.ACK, (0).to_bytes(8))
        for message in peer.sent
    ) == (mode in {"ok", "invalid"})


@pytest.mark.parametrize(
    "mode",
    [
        "ok",
        "ready",
        "disabled",
        "revoked",
        "forbidden",
        "bad_ack",
        "unknown",
        "host",
        "occupied",
        "taken",
    ],
)
async def test_host_socket_authenticates_claims_relays_and_releases(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    _browser_request(monkeypatch)
    relay = TerminalRelay(alive=_alive)
    patch_state(monkeypatch, "terminals", relay)
    term = relay.open(_opening())
    frames = [encode_frame(Frame.OUTPUT, b"out")]
    if mode in {"ready", "bad_ack"}:
        frames.insert(0, encode_frame(Frame.ACK if mode == "ready" else Frame.INPUT))
    peer = _WebPeer(*frames)
    socket = peer.socket()
    status = 410 if mode == "revoked" else 403 if mode == "forbidden" else None
    monkeypatch.setattr(
        terminal_routes,
        "request_host",
        AsyncMock(side_effect=HTTPException(status) if status else None),
    )
    monkeypatch.setattr(terminal_routes, "require_instance", AsyncMock())
    if mode == "disabled":
        patch_state(monkeypatch, "config", Config(auth_disabled=True))
    if mode == "host":
        term.host_id = uuid.uuid4()
    if mode == "occupied":
        monkeypatch.setattr(relay, "daemon_connected", AsyncMock(return_value=False))
    if mode == "taken":
        monkeypatch.setattr(relay, "daemon_connected", AsyncMock(return_value=True))
    await terminal_routes.terminal_daemon_socket(
        socket,
        _HOST,
        uuid.uuid4() if mode == "unknown" else term.id,
        uuid.uuid4(),
        ready=mode in {"ready", "bad_ack"},
    )
    assert peer.sent[0]["type"] == "websocket.accept"
    if mode in {"ok", "ready"}:
        assert term.ring.since(0)[0] == b"out"
        assert term.ended == (mode == "ok")
        assert term.daemon is None or term.ended
    elif mode == "taken":
        assert term.ring.since(0)[0] == b""
        assert term.ended is False
    else:
        assert peer.sent[-1]["code"] == (
            wire_terminals.CLOSE_HOST_REVOKED
            if mode == "revoked"
            else wire_terminals.CLOSE_REFUSED
        )
        assert term.daemon is None


@pytest.mark.parametrize(
    "error",
    [None, WebSocketDisconnect(), RuntimeError("closed"), OSError("reset")],
)
async def test_socket_close_absorbs_a_peer_that_already_left(
    error: BaseException | None,
) -> None:
    close: AsyncMockProtocol[None] = AsyncMock(side_effect=error)
    socket = AsyncMock(spec=WebSocket, close=close)
    await terminal_routes.StarletteSocket(socket).close(wire_terminals.CLOSE_REFUSED)
    close.assert_awaited_once_with(wire_terminals.CLOSE_REFUSED, "")


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
