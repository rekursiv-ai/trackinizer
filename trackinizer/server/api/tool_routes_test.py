"""The tool routes' answers to the relay's refusals, over a stand-in relay and no database.

The routes end to end, with the real sign-in and host credentials, are in
``tool_routes_pglite_test.py``. These tests pin what each relay error becomes, what each
refusal closes a socket with, and which routes the schema lists.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Final, cast, override
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import logging
import re

from fastapi import FastAPI, HTTPException
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import pytest

from trackinizer.conftest import make_store
from trackinizer.server import auth
from trackinizer.server.api import tool_routes
from trackinizer.server.api.idempotency import ChangeIdMiddleware
from trackinizer.server.api.tool_access import ToolAccess
from trackinizer.server.tool_lines import GatedCall, Outcome, ResultIds, Verdict
from trackinizer.server.tool_relay import (
    ToolConnection,
    ToolConnectionEndError,
    ToolConnectionRevokedError,
    ToolRelay,
)
from trackinizer.types.errors import NotFoundError
from trackinizer.wire import wire_terminals
from trackinizer.wire.wire_exec import HOST_EXEC_WAIT_PATH
from trackinizer.wire.wire_tools import (
    HOST_TOOLS_WAIT_PATH,
    HOST_TOOLS_WS_PATH,
    TOOLS_WS_PATH,
    ToolOffer,
)


if TYPE_CHECKING:
    from starlette.testclient import WebSocketTestSession

    from trackinizer.server.api.tool_access import Check
    from trackinizer.server.tool_relay import Inbound


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


def _relay(*, connections_per_host: int = 16) -> ToolRelay:
    return ToolRelay(
        alive=_always_alive,
        connections_per_host=connections_per_host,
        dial_back_sec=20,
        line_bytes=1 << 20,
    )


class _Hub(ToolRelay):
    """A relay whose every wait ends in the error the test chose."""

    def __init__(self, error: Exception) -> None:
        super().__init__(
            alive=_always_alive,
            connections_per_host=16,
            dial_back_sec=20,
            line_bytes=1 << 20,
        )
        self._error = error

    @override
    async def wait(self, host_id: UUID, *, wait_sec: float) -> ToolOffer | None:
        del host_id, wait_sec
        raise self._error

    @override
    async def hold(self, host_id: UUID, *, wait_sec: float) -> None:
        del host_id, wait_sec
        raise self._error


class _Holding(ToolRelay):
    """A relay that notes how long each wait was asked to hold, and has no offer."""

    def __init__(self) -> None:
        super().__init__(
            alive=_always_alive,
            connections_per_host=16,
            dial_back_sec=20,
            line_bytes=1 << 20,
        )
        self.held: list[float] = []

    @override
    async def wait(self, host_id: UUID, *, wait_sec: float) -> ToolOffer | None:
        del host_id
        self.held.append(wait_sec)

    @override
    async def hold(self, host_id: UUID, *, wait_sec: float) -> None:
        del host_id
        self.held.append(wait_sec)


def _app(monkeypatch: pytest.MonkeyPatch, relay: ToolRelay) -> FastAPI:
    app = FastAPI()
    app.add_middleware(ChangeIdMiddleware)
    app.include_router(tool_routes.router)
    app.state.tool_relay = relay
    monkeypatch.setattr(tool_routes, "request_host", AsyncMock())
    monkeypatch.setattr(tool_routes, "current_host", AsyncMock())
    monkeypatch.setattr(tool_routes, "require_instance", AsyncMock())
    monkeypatch.setattr(
        auth,
        "request_user",
        AsyncMock(return_value=SimpleNamespace(api_key_id=_KEY)),
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


def _params(wait_sec: float) -> dict[str, str | float]:
    return {"instance": str(_INSTANCE), "wait_sec": wait_sec}


def _close_code(session: WebSocketTestSession) -> int:
    """Return the code the server closed this socket with."""
    with pytest.raises(WebSocketDisconnect) as closed:
        _ = session.receive_bytes()
    return closed.value.code


@pytest.mark.parametrize(
    "path",
    [
        HOST_TOOLS_WAIT_PATH.format(host_id=_HOST),
        HOST_EXEC_WAIT_PATH.format(host_id=_HOST),
    ],
)
def test_a_host_whose_host_is_revoked_while_it_waits_gets_the_revoked_answer(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    with TestClient(_app(monkeypatch, _Hub(ToolConnectionRevokedError()))) as client:
        response = client.get(path, params=_params(1))

    assert (response.status_code, response.json()) == (
        410,
        {"detail": "host_revoked"},
    )


@pytest.mark.parametrize(
    "path",
    [
        HOST_TOOLS_WAIT_PATH.format(host_id=_HOST),
        HOST_EXEC_WAIT_PATH.format(host_id=_HOST),
    ],
)
@pytest.mark.parametrize(("asked", "held"), [(1_000, 25), (3, 3)])
def test_a_host_cannot_hold_its_wait_past_25_seconds(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    asked: float,
    held: float,
) -> None:
    relay = _Holding()

    with TestClient(_app(monkeypatch, relay)) as client:
        response = client.get(path, params=_params(asked))

    assert (response.status_code, response.json()) == (200, None)
    assert relay.held == [held]


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
        with client.websocket_connect(
            HOST_TOOLS_WS_PATH.format(host_id=_HOST, connection_id=_PIPE)
            + f"?instance={_INSTANCE}",
        ) as host:
            assert _closed(host) == (code, reason)
        if isinstance(error, HTTPException):
            with client.websocket_connect(_AGENT) as agent:
                assert _closed(agent) == (code, reason)
    assert _logged(caplog, "tool connection refused:") == [
        {"socket": "daemon", "host": str(_HOST), **refused},
        *(
            [{"socket": "agent", "host": "gx10", **refused}]
            if isinstance(error, HTTPException)
            else []
        ),
    ]


@pytest.mark.parametrize(
    ("check", "reason"),
    [
        ("assistant", "tools are not available here"),
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
        "asker": "Ada@x.y",
        "host": "gx10",
    }


def _closed(session: WebSocketTestSession) -> tuple[int, str]:
    """Return the code and reason the server closed this socket with."""
    with pytest.raises(WebSocketDisconnect) as closed:
        _ = session.receive_bytes()
    return closed.value.code, closed.value.reason


def test_an_agent_past_the_hosts_connections_is_closed_as_full(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=tool_routes.__name__)
    app = _app(monkeypatch, _relay(connections_per_host=1))
    path = _AGENT

    with (
        TestClient(app) as client,
        client.websocket_connect(path) as first,
        client.websocket_connect(path) as refused,
    ):
        assert _closed(refused) == (
            wire_terminals.CLOSE_REFUSED,
            "gx10 holds its 1 tool connections",
        )
        first.send_bytes(b"Hello\n")
    (opened,) = _logged(caplog, "tool connection opened:")
    assert opened == {
        "connection": opened["connection"],
        "host": "gx10",
        "host_id": str(_HOST),
        "asker": "ada@x.y",
        "key": str(_KEY),
        "assistant": "-",
    }
    assert _logged(caplog, "tool connection refused:") == [
        {
            "socket": "agent",
            "host": "gx10",
            "code": str(wire_terminals.CLOSE_REFUSED),
            "reason": "gx10 holds its 1 tool connections",
            "check": "-",
        },
    ]


def test_a_host_dialing_for_no_such_connection_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app(monkeypatch, _relay())

    with (
        TestClient(app) as client,
        client.websocket_connect(
            HOST_TOOLS_WS_PATH.format(host_id=_HOST, connection_id=_PIPE)
            + f"?instance={_INSTANCE}",
        ) as host,
    ):
        assert _close_code(host) == wire_terminals.CLOSE_REFUSED


def test_binary_frames_cross_and_a_host_that_leaves_ends_the_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app(monkeypatch, _relay())

    with (
        TestClient(app) as client,
        client.websocket_connect(_AGENT) as agent,
    ):
        agent.send_bytes(_HELLO)
        offer = client.get(
            HOST_TOOLS_WAIT_PATH.format(host_id=_HOST),
            params=_params(1),
        ).json()
        assert isinstance(offer, dict)
        connection_id = cast(dict[str, str], offer)["connection_id"]
        with client.websocket_connect(
            HOST_TOOLS_WS_PATH.format(host_id=_HOST, connection_id=connection_id)
            + f"?instance={_INSTANCE}",
        ) as host:
            assert host.receive_bytes() == _HELLO
            host.send_bytes(b"Welcome\n")
            assert agent.receive_bytes() == b"Welcome\n"

        assert _close_code(agent) == wire_terminals.CLOSE_DAEMON_LOST


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
