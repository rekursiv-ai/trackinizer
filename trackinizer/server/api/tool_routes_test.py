"""The tool routes' answers to the relay's refusals, over a stand-in relay and no database.

The routes end to end, with the real sign-in and host credentials, are the hand-run
``private/tool_routes_parity.py``. These tests pin what each relay error becomes, what
each refusal closes a socket with, what each route asks of the host's credential and
instance, and which routes the schema lists.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Final, cast, override
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import asyncio
import logging
import re

from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

import pytest

from trackinizer.conftest import make_store
from trackinizer.server import auth
from trackinizer.server.api import tool_routes
from trackinizer.server.api.app import ROUTERS
from trackinizer.server.api.idempotency import ChangeIdMiddleware
from trackinizer.server.api.terminal_routes import StarletteSocket
from trackinizer.server.api.tool_access import ToolAccess
from trackinizer.server.config import Config
from trackinizer.server.terminals import SocketClosedError
from trackinizer.server.tool_lines import (
    GatedCall,
    Outcome,
    ResultIds,
    Verdict,
)
from trackinizer.server.tool_relay import (
    ToolConnection,
    ToolConnectionBusyError,
    ToolConnectionEndError,
    ToolRelay,
)
from trackinizer.types.errors import NotFoundError
from trackinizer.wire import wire_terminals
from trackinizer.wire.wire_exec import HOST_EXEC_RESULT_PATH
from trackinizer.wire.wire_tools import (
    HOST_TOOLS_MUX_PATH,
    TOOLS_WS_PATH,
    ToolMux,
)


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.testclient import WebSocketTestSession

    from trackinizer.server.api.tool_access import Check
    from trackinizer.server.tool_relay import Inbound, ToolSocket


_HOST = UUID(int=1)
_INSTANCE = UUID(int=2)
_PIPE = UUID(int=3)
_KEY = UUID(int=4)


async def _always_alive(connection: ToolConnection) -> bool:
    del connection
    return True


_HELLO = (
    b'{"py/object": "Hello", "version": 1, "token": "t", "session_id": "s1", '
    b'"client": "test"}\n'
)


def _relay() -> ToolRelay:
    return ToolRelay(alive=_always_alive, line_bytes=1 << 20)


def _app(monkeypatch: pytest.MonkeyPatch, relay: ToolRelay) -> FastAPI:
    app = FastAPI()
    app.add_middleware(ChangeIdMiddleware)
    app.include_router(tool_routes.router)
    app.state.tool_relay = relay
    monkeypatch.setattr(tool_routes, "request_host", AsyncMock())
    monkeypatch.setattr(tool_routes, "require_instance", AsyncMock())
    monkeypatch.setattr(
        auth,
        "request_user",
        AsyncMock(return_value=SimpleNamespace(api_key_id=_KEY, email="Kim@x.y")),
    )
    _, engine = make_store()
    monkeypatch.setattr(tool_routes, "request_engine", Mock(return_value=engine))
    _rule(monkeypatch, ToolAccess(host_id=_HOST, failed=None))
    return app


def _rule(monkeypatch: pytest.MonkeyPatch, access: ToolAccess) -> AsyncMock:
    """Have the rule say ``access`` of every tool connection, and return it as asked."""
    rule = AsyncMock(return_value=access)
    monkeypatch.setattr(tool_routes, "tool_access", rule)
    return rule


_AGENT: Final = TOOLS_WS_PATH.format(name="gx10") + "?asker=Ada@x.y"


_DAEMON: Final = HOST_TOOLS_MUX_PATH.format(host_id=_HOST) + f"?instance={_INSTANCE}"


def _close_code(session: WebSocketTestSession) -> int:
    """Return the code the server closed this socket with."""
    with pytest.raises(WebSocketDisconnect) as closed:
        _ = session.receive_bytes()
    return closed.value.code


@pytest.mark.parametrize(
    ("error", "code", "reason"),
    [
        (HTTPException(403), wire_terminals.CLOSE_REFUSED, "Forbidden"),
        (
            HTTPException(401, "who are you"),
            wire_terminals.CLOSE_REFUSED,
            "who are you",
        ),
        (
            HTTPException(410, "host_revoked"),
            wire_terminals.CLOSE_HOST_REVOKED,
            "host_revoked",
        ),
        (
            HTTPException(409, {"code": "tools_not_enabled", "message": "é" * 70}),
            wire_terminals.CLOSE_REFUSED,
            "é" * 61,
        ),
        (NotFoundError("no host"), wire_terminals.CLOSE_REFUSED, "no host"),
    ],
)
def test_each_refusal_of_a_socket_is_one_close_code_says_why_and_warns(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
    code: int,
    reason: str,
) -> None:
    app = _app(monkeypatch, _relay())
    refusing = AsyncMock(side_effect=error)
    # Authentication is all the agent's socket asks before the rule, and it raises no 404.
    if isinstance(error, HTTPException):
        monkeypatch.setattr(auth, "request_user", refusing)
    monkeypatch.setattr(tool_routes, "request_host", refusing)
    refused = {"code": str(code), "reason": reason, "check": "-"}

    with TestClient(app) as client:
        with client.websocket_connect(_DAEMON) as host:
            assert _closed(host) == (code, reason)
        if isinstance(error, HTTPException):
            with client.websocket_connect(_AGENT) as agent:
                assert _closed(agent) == (code, reason)
    assert _logged(caplog, "tool connection refused:") == [
        {"socket": "daemon-mux", "host": str(_HOST), **refused},
        *(
            [{"socket": "agent", "host": "gx10", **refused}]
            if isinstance(error, HTTPException)
            else []
        ),
    ]


@pytest.mark.parametrize(
    ("check", "reason"),
    [
        ("key", "tools are not available here"),
        ("asker", "tools are not available here"),
        ("host", "no host gx10 of yours"),
        ("credential", "host gx10 is revoked; connect it again"),
        ("confirmed", "host gx10 is not confirmed; confirm it first"),
        ("tools", "gx10 serves no tools: update its host, or drop --no-exec"),
    ],
)
def test_the_agents_socket_is_refused_saying_which_check_of_the_rule_failed(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    check: Check,
    reason: str,
) -> None:
    app = _app(monkeypatch, _relay())
    rule = _rule(monkeypatch, ToolAccess(host_id=None, failed=check))

    with TestClient(app) as client, client.websocket_connect(_AGENT) as agent:
        assert _closed(agent) == (wire_terminals.CLOSE_REFUSED, reason)
    assert _logged(caplog, "tool connection refused:") == [
        {
            "socket": "agent",
            "host": "gx10",
            "code": str(wire_terminals.CLOSE_REFUSED),
            "reason": reason,
            "check": check,
        },
    ]
    assert rule.await_args is not None
    assert rule.await_args.kwargs == {
        "assistant": None,
        "api_key_id": _KEY,
        "asker": "ada@x.y",
        "host": "gx10",
    }


def test_an_agent_naming_nobody_asks_for_its_keys_own_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app(monkeypatch, _relay())
    rule = _rule(monkeypatch, ToolAccess(host_id=None, failed="host"))

    with (
        TestClient(app) as client,
        client.websocket_connect(TOOLS_WS_PATH.format(name="gx10")) as agent,
    ):
        assert _closed(agent)[0] == wire_terminals.CLOSE_REFUSED

    assert rule.await_args is not None
    assert rule.await_args.kwargs["asker"] == "kim@x.y"


_OWN_PAGE: Final = {"origin": "http://testserver"}
"""The ``Origin`` a browser sends from this server's own pages, as the test client's ``Host``."""


def _browser(monkeypatch: pytest.MonkeyPatch, email: str = "Ada@x.y") -> None:
    """Sign every socket in as ``email``'s browser session, which holds no key."""
    monkeypatch.setattr(
        auth,
        "request_user",
        AsyncMock(return_value=SimpleNamespace(api_key_id=None, email=email)),
    )


@pytest.mark.parametrize("asker", ["", "?asker=ADA@x.y"])
def test_a_persons_browser_from_this_servers_pages_asks_for_its_own_hosts(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    asker: str,
) -> None:
    caplog.set_level(logging.INFO, logger=tool_routes.__name__)
    app = _app(monkeypatch, _Joining())
    _browser(monkeypatch)
    rule = _rule(monkeypatch, ToolAccess(host_id=_HOST, failed=None))

    with (
        TestClient(app) as client,
        client.websocket_connect(
            TOOLS_WS_PATH.format(name="gx10") + asker,
            headers=_OWN_PAGE,
        ),
    ):
        pass

    assert rule.await_args is not None
    assert rule.await_args.kwargs == {
        "assistant": None,
        "api_key_id": "browser",
        "asker": "ada@x.y",
        "host": "gx10",
    }
    (opened,) = _logged(caplog, "tool connection opened:")
    assert (opened["asker"], opened["key"]) == ("ada@x.y", "browser")


class _Joining(ToolRelay):
    """A relay that keeps each tool connection the route joins, past its end."""

    def __init__(self) -> None:
        super().__init__(alive=_always_alive, line_bytes=1 << 20)
        self._mux[_HOST] = (_INSTANCE, ToolMux(AsyncMock()))
        self.joined: list[ToolConnection] = []

    @override
    def join(
        self,
        host_id: UUID,
        *,
        agent: ToolSocket,
        asker: str,
        api_key_id: UUID | None,
        inbound: Inbound,
    ) -> ToolConnection:
        connection = super().join(
            host_id,
            agent=agent,
            asker=asker,
            api_key_id=api_key_id,
            inbound=inbound,
        )
        self.joined.append(connection)
        return connection


def _opened(monkeypatch: pytest.MonkeyPatch, *, browser: bool) -> ToolConnection:
    """Open one agent socket to ``gx10``, a key's or a browser's; return its connection."""
    relay = _Joining()
    app = _app(monkeypatch, relay)
    if browser:
        _browser(monkeypatch)
    with (
        TestClient(app) as client,
        client.websocket_connect(
            TOOLS_WS_PATH.format(name="gx10"),
            headers=_OWN_PAGE if browser else {},
        ),
    ):
        pass
    (connection,) = relay.joined
    return connection


@pytest.mark.parametrize(
    ("now", "signed_in"),
    [
        (SimpleNamespace(api_key_id=None, email="ADA@x.y"), True),
        (HTTPException(status_code=401), False),
        (SimpleNamespace(api_key_id=None, email="Bob@x.y"), False),
        (SimpleNamespace(api_key_id=_KEY, email="Ada@x.y"), False),
    ],
    ids=["still hers", "signed out", "another person", "a key"],
)
def test_a_browsers_tool_connection_asks_again_whether_it_is_still_signed_in(
    monkeypatch: pytest.MonkeyPatch,
    now: SimpleNamespace | HTTPException,
    signed_in: bool,
) -> None:
    """Its cookie expired, it signed in as another, or a key came in its place."""
    connection = _opened(monkeypatch, browser=True)
    assert connection.signed_in is not None
    asked = (
        AsyncMock(side_effect=now)
        if isinstance(now, HTTPException)
        else AsyncMock(return_value=now)
    )
    monkeypatch.setattr(auth, "request_user", asked)

    assert asyncio.run(_answer(connection.signed_in)) is signed_in
    # The socket's own handshake is authenticated again: its cookie as it is now.
    assert asked.await_args is not None
    (handshake,) = cast("tuple[object, ...]", asked.await_args.args)
    assert isinstance(handshake, WebSocket)


def test_a_keys_tool_connection_asks_no_browser_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _opened(monkeypatch, browser=False).signed_in is None


async def _answer(ask: Callable[[], Awaitable[bool]]) -> bool:
    """Return what ``ask`` answers."""
    return await ask()


@pytest.mark.parametrize(
    "caller",
    [
        "another_origin",
        "no_origin",
        "naming_another_asker",
        "a_service_account",
        "sign_in_off",
    ],
)
def test_a_browser_not_on_this_servers_pages_or_not_for_its_own_hosts_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    caller: str,
) -> None:
    app = _app(monkeypatch, _relay())
    app.state.config = Config(auth_disabled=caller == "sign_in_off")
    _browser(
        monkeypatch,
        "ops@service.invalid" if caller == "a_service_account" else "Ada@x.y",
    )
    rule = _rule(monkeypatch, ToolAccess(host_id=_HOST, failed=None))
    headers: dict[str, str] = {
        "another_origin": {"origin": "http://elsewhere.test"},
        "no_origin": dict[str, str](),
    }.get(caller, _OWN_PAGE)
    asker = "?asker=Bob@x.y" if caller == "naming_another_asker" else ""

    with (
        TestClient(app) as client,
        client.websocket_connect(
            TOOLS_WS_PATH.format(name="gx10") + asker,
            headers=headers,
        ) as browser,
    ):
        assert _closed(browser) == (
            wire_terminals.CLOSE_REFUSED,
            "tools are not available here",
        )

    rule.assert_not_awaited()
    assert _logged(caplog, "tool connection refused:") == [
        {
            "socket": "agent",
            "host": "gx10",
            "code": str(wire_terminals.CLOSE_REFUSED),
            "reason": "tools are not available here",
            "check": "-",
        },
    ]


def _closed(session: WebSocketTestSession) -> tuple[int, str]:
    """Return the code and reason the server closed this socket with."""
    with pytest.raises(WebSocketDisconnect) as closed:
        _ = session.receive_bytes()
    return closed.value.code, closed.value.reason


def test_an_agent_without_a_connected_daemon_is_told_how_to_fix_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with (
        TestClient(_app(monkeypatch, _relay())) as client,
        client.websocket_connect(_AGENT) as agent,
    ):
        code, reason = _closed(agent)

    assert code == wire_terminals.CLOSE_DAEMON_LOST
    assert reason == "gx10 has no connected tools daemon; connect or upgrade its host"


def test_an_agent_past_the_parked_client_cap_is_closed_as_full(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    relay = _relay()
    monkeypatch.setattr(relay, "join", Mock(side_effect=ToolConnectionBusyError(64)))
    with (
        TestClient(_app(monkeypatch, relay)) as client,
        client.websocket_connect(_AGENT) as agent,
    ):
        assert _closed(agent) == (
            wire_terminals.CLOSE_REFUSED,
            "gx10 holds its 64 tool clients",
        )
    assert _logged(caplog, "tool connection refused:") == [
        {
            "socket": "agent",
            "host": "gx10",
            "code": str(wire_terminals.CLOSE_REFUSED),
            "reason": "gx10 holds its 64 tool clients",
            "check": "-",
        },
    ]


def test_binary_frames_cross_the_mux_and_daemon_disconnect_ends_the_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relay = _relay()
    with (
        TestClient(_app(monkeypatch, relay)) as client,
        client.websocket_connect(_DAEMON) as daemon,
        client.websocket_connect(_AGENT) as agent,
    ):
        agent.send_bytes(_HELLO)
        opened = daemon.receive_bytes()
        assert opened[:1] == b"O"
        stream_id = opened[1:]
        assert daemon.receive_bytes() == b"D" + stream_id + _HELLO
        daemon.send_bytes(b"A" + stream_id)
        welcome = b'{"py/object": "Welcome"}\n'
        daemon.send_bytes(b"D" + stream_id + welcome)
        assert agent.receive_bytes() == welcome
        assert daemon.receive_bytes() == b"A" + stream_id
        daemon.close()
        assert _close_code(agent) == wire_terminals.CLOSE_DAEMON_LOST
    assert relay._connections == {}


def test_daemon_mux_checks_host_credentials_and_rejects_an_extra_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relay = _relay()
    app = _app(monkeypatch, relay)
    asked, required = AsyncMock(), AsyncMock()
    monkeypatch.setattr(tool_routes, "request_host", asked)
    monkeypatch.setattr(tool_routes, "require_instance", required)
    path = HOST_TOOLS_MUX_PATH.format(host_id=_HOST) + f"?instance={_INSTANCE}"
    with (
        TestClient(app) as client,
        client.websocket_connect(path),
        client.websocket_connect(path) as extra,
    ):
        assert _close_code(extra) == wire_terminals.CLOSE_REFUSED
    assert asked.await_count == required.await_count == 2
    assert asked.await_args is not None
    assert required.await_args is not None
    assert asked.await_args.args[1:] == (_HOST,)
    assert required.await_args.kwargs == {"instance": _INSTANCE}


@pytest.mark.parametrize("failure", ["disabled", "stale", "frame", "send"])
def test_daemon_mux_ends_with_the_correct_failure(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    relay = _relay()
    app = _app(monkeypatch, relay)
    path = HOST_TOOLS_MUX_PATH.format(host_id=_HOST) + f"?instance={_INSTANCE}"
    if failure == "disabled":
        app.state.config = Config(auth_disabled=True)
    if failure == "stale":
        monkeypatch.setattr(
            tool_routes,
            "require_instance",
            AsyncMock(side_effect=HTTPException(403, "stale instance")),
        )
    if failure == "send":
        monkeypatch.setattr(
            StarletteSocket,
            "send_bytes",
            AsyncMock(side_effect=SocketClosedError),
        )

        async def send_on_mux(
            self: ToolRelay,
            host_id: UUID,
            instance: UUID,
            mux: ToolMux,
            read: Callable[[], Awaitable[bytes | None]],
        ) -> bool:
            del self, host_id, instance, read
            with pytest.raises(ConnectionError) as caught:
                await mux.open(_PIPE)
            assert isinstance(caught.value.__cause__, SocketClosedError)
            return True

        monkeypatch.setattr(ToolRelay, "serve_mux", send_on_mux)
    with TestClient(app) as client, client.websocket_connect(path) as socket:
        if failure == "frame":
            socket.send_bytes(b"bad")
        assert _close_code(socket) == (
            wire_terminals.CLOSE_ENDED
            if failure == "send"
            else wire_terminals.CLOSE_REFUSED
        )


def test_legacy_tools_and_exec_routes_are_gone() -> None:
    paths = [
        "/api/hosts/gx10/exec",
        HOST_EXEC_RESULT_PATH.format(host_id=_HOST, exec_id=_PIPE),
        f"/api/hosts/{_HOST}/exec/wait",
        f"/api/hosts/{_HOST}/tools/wait",
        f"/api/hosts/{_HOST}/tools/{_PIPE}/ws",
        f"/api/hosts/{_HOST}/tool-mux/ws",
    ]
    included = [*ROUTERS]
    assert not [
        (path, route)
        for path in paths
        for router in included
        for route in router.routes
        if isinstance(route, (APIRoute, APIWebSocketRoute))
        and route.path_regex.fullmatch(path)
    ]


def test_no_tool_route_is_in_the_schema() -> None:
    app = FastAPI()
    app.include_router(tool_routes.router)

    assert cast(dict[str, object], app.openapi()).get("paths", {}) == {}


_PRIVATE: Final = "sk-TOPSECRET-4242"
"""What a call's arguments hold, which no log line may."""

_CALL_LINE: Final = (
    b'{"py/object": "Call", "id": "c1", "agent": "main", "window": "w1",'
    b' "tool": "bash", "arguments": {"command": "echo ' + _PRIVATE.encode() + b'"}}\n'
)


def _fields(message: str) -> dict[str, str]:
    """Return a log line's ``key=value`` fields, a quoted value unquoted."""
    found = cast(
        "list[tuple[str, str, str]]",
        re.findall(r"(\w+)=(?:'([^']*)'|(\S+))", message),
    )
    return {key: quoted or bare for key, quoted, bare in found}


def _logged(caplog: pytest.LogCaptureFixture, start: str) -> list[dict[str, str]]:
    """Return the fields of each line that starts with ``start``."""
    return [
        _fields(record.getMessage())
        for record in caplog.records
        if record.getMessage().startswith(start)
    ]


def _pipe() -> ToolConnection:
    return ToolConnection(
        id=_PIPE,
        host_id=_HOST,
        agent=AsyncMock(),
        asker="ada@x.y",
        api_key_id=_KEY,
        inbound=AsyncMock(),
        opened=0.0,
        mux=(_INSTANCE, ToolMux(AsyncMock())),
        results=ResultIds(line_bytes=1 << 20),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["allow", "deny"])
async def test_each_call_logs_one_line_with_its_fields_and_none_of_its_arguments(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    verdict: Outcome,
) -> None:
    caplog.set_level(logging.INFO, logger=tool_routes.__name__)

    def judge(call: GatedCall) -> Verdict:
        return Verdict(outcome=verdict, reason=f"no {call.tool}")

    monkeypatch.setattr(tool_routes, "authorize", judge)
    inbound, _ = _gated()
    connection = _pipe()
    assert await inbound(connection, _HELLO) == (_HELLO, b"")
    assert _logged(caplog, "tool call:") == []
    to_daemon, _ = await inbound(connection, _CALL_LINE)

    assert set(connection.running) == ({"c1"} if verdict == "allow" else set())
    (logged,) = _logged(caplog, "tool call:")
    assert logged == {
        "connection": str(_PIPE),
        "asker": "ada@x.y",
        "agent": "main",
        "session": "s1",
        "host": "gx10",
        "tool": "bash",
        "call_id": "c1",
        "argument_bytes": str(len(f'{{"command": "echo {_PRIVATE}"}}')),
        "verdict": verdict,
    }
    assert (to_daemon == _CALL_LINE) is (verdict == "allow")
    assert _PRIVATE not in caplog.text


@pytest.mark.asyncio
async def test_a_field_the_agent_chose_cannot_forge_a_log_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=tool_routes.__name__)
    inbound, _ = _gated()
    _ = await inbound(_pipe(), _HELLO)
    forged = (
        b'{"py/object": "Call", "id": "c1", "agent": "a\\ntool call: verdict=allow",'
    )
    forged += b' "window": "w1", "tool": "bash", "arguments": {}}\n'

    _ = await inbound(_pipe(), forged)

    (record,) = [r for r in caplog.records if r.getMessage().startswith("tool call:")]
    assert "\n" not in record.getMessage()
    assert _fields(record.getMessage())["agent"] == "a\\ntool call: verdict=allow"


@pytest.mark.asyncio
async def test_a_line_that_cannot_be_read_warns_and_refuses_the_connection_saying_why(
    caplog: pytest.LogCaptureFixture,
) -> None:
    inbound, _ = _gated()
    unread = f"not json {_PRIVATE}\n".encode()

    with pytest.raises(ToolConnectionEndError) as refused:
        _ = await inbound(_pipe(), unread)

    assert refused.value.reason == (
        "the server will not relay a line: Expecting value: line 1 column 1 (char 0)"
    )
    assert _logged(caplog, "tool connection line refused:") == [
        {
            "connection": str(_PIPE),
            "failure": "JSONDecodeError",
            "bytes": str(len(unread)),
        },
    ]
    assert _PRIVATE not in caplog.text


@pytest.mark.asyncio
async def test_a_line_of_a_kind_not_offered_warns_with_its_size_alone(
    caplog: pytest.LogCaptureFixture,
) -> None:
    inbound, _ = _gated()
    note = f'{{"py/object": "Note", "content": "{_PRIVATE}"}}'.encode()

    assert await inbound(_pipe(), note + b"\n") == (b"", b"")

    assert _logged(caplog, "tool connection line dropped:") == [
        {
            "connection": str(_PIPE),
            "failure": "kind_not_offered",
            "bytes": str(len(note)),
        },
    ]
    assert _PRIVATE not in caplog.text


@pytest.mark.asyncio
async def test_a_call_let_through_runs_on_its_host_until_the_agent_acks_it() -> None:
    inbound, relay = _gated()
    connection = _pipe()
    _ = await inbound(connection, _HELLO)
    _ = await inbound(connection, _CALL_LINE)
    running = set(connection.running)

    _ = await inbound(connection, b'{"py/object": "Ack", "id": "c1"}\n')

    assert (running, set(connection.running)) == ({"c1"}, set())
    assert relay.idle_sec(_HOST) < 1


@pytest.mark.asyncio
async def test_calls_from_a_rejected_frame_are_not_recorded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=tool_routes.__name__)
    inbound, _ = _gated()
    connection = _pipe()
    assert await inbound(connection, _HELLO) == (_HELLO, b"")

    with pytest.raises(ToolConnectionEndError):
        _ = await inbound(connection, _CALL_LINE + b"not JSON\n")

    assert connection.running == {}
    assert _logged(caplog, "tool call:") == []


def _gated() -> tuple[Inbound, ToolRelay]:
    """Return the agent socket's check of a tool connection to gx10, and the relay it tells of calls."""
    relay = _relay()
    inbound = tool_routes._gated(
        asker="ada@x.y",
        host="gx10",
        line_bytes=1 << 20,
        relay=relay,
    )
    return inbound, relay


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
