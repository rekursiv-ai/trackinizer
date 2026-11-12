"""The tool routes end to end over PGlite: who may open a tool connection, and a host's round trip.

The HTTP routes are driven through the whole app with the real sign-in and host
credentials. A WebSocket route is called as the server calls it, with a Starlette
``WebSocket`` over a scripted ASGI channel, since the test client cannot share the
session event loop that owns the database. The host is played by the test: it holds the
wait route and dials back, as the daemon does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

import asyncio
import json
import logging
import re
import time
import uuid

from fastapi import Response
from starlette.websockets import WebSocket, WebSocketDisconnect
from treekle import from_plain

import pytest
import pytest_asyncio

from trackinizer.server.api import tool_routes
from trackinizer.server.api._deps import tool_relay
from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import clear_identity_override, patch_state
from trackinizer.server.api.tool_access import tool_access
from trackinizer.server.auth import create_api_key
from trackinizer.server.config import Assistant, Config
from trackinizer.server.session import SESSION_COOKIE_NAME, set_session_cookie
from trackinizer.server.tool_lines import (
    GatedCall,
    Verdict,
    refused_line,
)
from trackinizer.wire import wire_terminals
from trackinizer.wire.wire_daemon import ENROLL_PATH, JOIN_PATH, REVOKE_PATH
from trackinizer.wire.wire_exec import (
    HOST_EXEC_RESULT_PATH,
    HOST_EXEC_WAIT_PATH,
)
from trackinizer.wire.wire_tools import (
    HOST_TOOLS_WAIT_PATH,
    ToolOffer,
)


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from starlette.types import Message

    import httpx2

    from trackinizer.server.api.tool_access import Check
    from trackinizer.server.auth import Role
    from trackinizer.server.store.core import Store


type _Client = tuple[httpx2.AsyncClient, Store]

pytestmark = [
    pytest.mark.db_pglite,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.usefixtures("cheap_scrypt"),
]

_SESSION_KEY = "session-signing-key"
_ORIGIN = "https://trax.test"
_HOST = "trax.test"
_INSTANCE = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_ASSISTANT = "kb@x.y"
_HELLO = (
    b'{"py/object": "Hello", "version": 1, "token": "t", "session_id": "s1", '
    b'"client": "test"}\n'
)
_PING = b'{"py/object": "Ping"}\n'


@pytest.fixture
def cheap_scrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hash secrets at the smallest accepted cost so a test pays no scrypt round."""
    monkeypatch.setattr("trackinizer.server.auth._SCRYPT_N", 2)


@dataclass(frozen=True, slots=True, kw_only=True)
class _Host:
    name: str
    id: uuid.UUID
    credential: str

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.credential}"}


class _Peer:
    """One side of a WebSocket, scripted: frames pushed in, messages read out."""

    def __init__(self, headers: Mapping[str, str]) -> None:
        self._inbox: asyncio.Queue[Message] = asyncio.Queue()
        self._outbox: asyncio.Queue[Message] = asyncio.Queue()
        self._inbox.put_nowait({"type": "websocket.connect"})
        self._dropped = False
        self.reason = ""
        """The reason of the close last read by :meth:`close_code`."""

        self.websocket = WebSocket(
            {
                "type": "websocket",
                "app": app,
                "path": "/",
                "query_string": b"",
                "headers": [
                    (key.lower().encode(), value.encode())
                    for key, value in {"host": _HOST, **headers}.items()
                ],
            },
            self._inbox.get,
            self._send,
        )

    async def _send(self, message: Message) -> None:
        """Take what the handler sends; once the peer has left, raise as uvicorn does."""
        if self._dropped:
            raise WebSocketDisconnect(1006)
        await self._outbox.put(message)

    def push(self, payload: bytes) -> None:
        self._inbox.put_nowait({"type": "websocket.receive", "bytes": payload})

    def push_text(self, text: str) -> None:
        self._inbox.put_nowait({"type": "websocket.receive", "text": text})

    def leave(self, code: int = 1000) -> None:
        """Make the peer go: the handler reads a disconnect, and later sends raise."""
        self._inbox.put_nowait({"type": "websocket.disconnect", "code": code})
        self._dropped = True

    async def frame(self) -> bytes:
        """Return the next binary message sent to this peer, skipping the handshake."""
        while True:
            message = await asyncio.wait_for(self._outbox.get(), 5)
            if message["type"] == "websocket.send":
                assert isinstance(message["bytes"], bytes)
                return message["bytes"]
            assert message["type"] == "websocket.accept", message

    async def close_code(self) -> int:
        """Return the code this peer was closed with, having sent it nothing since."""
        while True:
            message = await asyncio.wait_for(self._outbox.get(), 5)
            if message["type"] == "websocket.close":
                assert isinstance(message["code"], int)
                self.reason = str(message.get("reason") or "")
                return message["code"]
            assert message["type"] == "websocket.accept", message


class _World:
    """A served app with users, the assistant, and a fresh tool connection relay."""

    def __init__(self, client: httpx2.AsyncClient, store: Store) -> None:
        self.client = client
        self.store = store
        self.tasks: list[asyncio.Task[None]] = []

    async def person(
        self,
        email: str,
        *,
        role: str = "writer",
        status: str = "active",
    ) -> uuid.UUID:
        user_id = uuid.uuid4()
        async with self.store.engine.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (id, email, name, role, status) "
                "VALUES ($1, $2, 'n', $3, $4)",
                user_id,
                email,
                role,
                status,
            )
        return user_id

    def cookie(self, user_id: uuid.UUID) -> str:
        signed = Response()
        set_session_cookie(
            signed,
            user_id=str(user_id),
            secret=_SESSION_KEY,
            max_age_seconds=600,
        )
        value = signed.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
        return f"{SESSION_COOKIE_NAME}={value}"

    async def key(
        self,
        user_id: uuid.UUID,
        *,
        ceiling: Role = "writer",
    ) -> dict[str, str]:
        async with self.store.engine.acquire() as conn:
            _, secret, _, _ = await create_api_key(
                conn,
                user_id=user_id,
                name="k",
                ceiling=ceiling,
            )
        return {"Authorization": f"Bearer {secret}"}

    async def assistant(self) -> dict[str, str]:
        return await self.key(await self.person(_ASSISTANT))

    def browser(self, cookie: str) -> dict[str, str]:
        return {"Cookie": cookie, "Host": _HOST, "Origin": _ORIGIN}

    async def call(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        body: Mapping[str, object] | None = None,
    ) -> httpx2.Response:
        return await self.client.request(method, path, json=body, headers=headers)

    async def join(
        self,
        cookie: str,
        name: str = "gx10",
        *,
        confirm: bool = True,
        facts: dict[str, object] | None = None,
    ) -> _Host:
        """Self-enroll ``name`` with ``cookie``'s user, join it, and confirm it."""
        enrolled = await self.call(
            "POST",
            ENROLL_PATH,
            headers=self.browser(cookie),
            body={"name": name},
        )
        assert enrolled.status_code == 201
        joined = await self.call(
            "POST",
            JOIN_PATH,
            headers={},
            body={
                "name": name,
                "token": from_plain(enrolled.json(), dict[str, str])["token"],
                "instance": str(_INSTANCE),
                "daemon_version": "t",
                "facts": {"tools": True} if facts is None else facts,
            },
        )
        assert joined.status_code == 201
        body = from_plain(joined.json(), dict[str, str])
        if confirm:
            confirmed = await self.call(
                "POST",
                wire_terminals.TERMINAL_CONFIRM_PATH.format(name=name),
                headers=self.browser(cookie),
            )
            assert confirmed.status_code == 204, confirmed.text
        return _Host(
            name=name,
            id=uuid.UUID(body["host_id"]),
            credential=body["credential"],
        )

    def agent(
        self,
        headers: Mapping[str, str],
        *,
        asker: str | None = "ada@x.y",
        host: str = "gx10",
    ) -> _Peer:
        """Dial the agent's socket in a task of its own, as the server would run it."""
        peer = _Peer(headers)
        self.tasks.append(
            asyncio.ensure_future(
                tool_routes.tools_agent_socket(peer.websocket, host, asker),
            ),
        )
        return peer

    def daemon(
        self,
        host: _Host,
        connection_id: uuid.UUID,
        *,
        instance: uuid.UUID = _INSTANCE,
        headers: Mapping[str, str] | None = None,
    ) -> _Peer:
        """Dial the host's socket back in a task of its own."""
        peer = _Peer(host.headers() if headers is None else headers)
        self.tasks.append(
            asyncio.ensure_future(
                tool_routes.tools_daemon_socket(
                    peer.websocket,
                    host.id,
                    connection_id,
                    instance,
                ),
            ),
        )
        return peer

    async def wait(
        self,
        host: _Host,
        *,
        instance: uuid.UUID = _INSTANCE,
        wait_sec: float = 0,
        key: uuid.UUID | None = None,
    ) -> httpx2.Response:
        """Hold the wait route as the host does."""
        path = HOST_TOOLS_WAIT_PATH.format(host_id=host.id)
        return await self.call(
            "GET",
            f"{path}?instance={instance}&wait_sec={wait_sec}",
            headers=host.headers()
            | ({} if key is None else {"Idempotency-Key": str(key)}),
        )

    async def offer(self, host: _Host) -> uuid.UUID:
        """Take the host's next offer, and return its tool connection's id."""
        waited = await self.wait(host, wait_sec=5)
        assert waited.status_code == 200, waited.text
        return ToolOffer.model_validate(waited.json()).connection_id

    async def paired(
        self,
        host: _Host,
        headers: Mapping[str, str],
    ) -> tuple[_Peer, _Peer]:
        """Bring up a whole tool connection, proven by a line crossing it."""
        agent = self.agent(headers)
        daemon = self.daemon(host, await self.offer(host))
        agent.push(_HELLO)
        assert await daemon.frame() == _HELLO
        return agent, daemon

    async def finish(self) -> None:
        """Say every route that was started has ended, with nothing left running."""
        done, running = await asyncio.wait(self.tasks, timeout=5)
        assert not running
        for task in done:
            task.result()


@pytest_asyncio.fixture(loop_scope="session")
async def world(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[_World]:
    client, store = pglite_route_client
    clear_identity_override()
    patch_state(monkeypatch, "config", _config())
    monkeypatch.delattr(app.state, "tool_relay", raising=False)
    monkeypatch.delattr(app.state, "replays", raising=False)
    found = _World(client, store)
    yield found
    for task in found.tasks:
        _ = task.cancel()
    _ = await asyncio.gather(*found.tasks, return_exceptions=True)


def _config(*, tool_dial_back_sec: float = Config().tool_dial_back_sec) -> Config:
    """Return the world's configuration: the assistant, and a session key."""
    return Config(
        session_secret=_SESSION_KEY,
        session_max_age_seconds=600,
        assistant=Assistant(actor="kb", email=_ASSISTANT),
        tool_dial_back_sec=tool_dial_back_sec,
    )


def _call(call_id: str, arguments: dict[str, object] | None = None) -> bytes:
    """Return a ``Call`` line as the agent sends it."""
    line = {
        "py/object": "Call",
        "id": call_id,
        "agent": "main",
        "window": "w1",
        "tool": "bash",
        "arguments": {"command": "ls"} if arguments is None else arguments,
    }
    return (json.dumps(line) + "\n").encode()


async def _take_over(world: _World, host: _Host) -> uuid.UUID:
    """Make another instance the host's current host, and return its id."""
    successor = uuid.uuid4()
    async with world.store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE hosts SET daemon_instance = $2 WHERE id = $1",
            host.id,
            successor,
        )
    return successor


# -- A tool connection, whole ----------------------------------------------------------------------


async def test_bytes_cross_a_whole_connection_and_either_end_closes_the_other(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    host = await world.join(world.cookie(ada))
    agent, daemon = await world.paired(host, await world.assistant())

    daemon.push(b"Welcome \xff\n")
    assert await agent.frame() == b"Welcome \xff\n"

    daemon.leave()

    assert await agent.close_code() == wire_terminals.CLOSE_DAEMON_LOST
    await world.finish()


async def test_an_agent_that_leaves_closes_its_host_and_its_slot_is_free(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    host = await world.join(world.cookie(ada))
    agent, daemon = await world.paired(host, await world.assistant())

    agent.leave()

    assert await daemon.close_code() == wire_terminals.CLOSE_ENDED
    await world.finish()
    assert tool_relay(app)._connections == {}


async def test_a_frame_that_is_not_binary_ends_the_connection(world: _World) -> None:
    ada = await world.person("ada@x.y")
    host = await world.join(world.cookie(ada))
    agent, daemon = await world.paired(host, await world.assistant())

    agent.push_text("not binary")

    assert await daemon.close_code() == wire_terminals.CLOSE_ENDED
    assert await agent.close_code() == wire_terminals.CLOSE_ENDED
    await world.finish()


# -- Who may open the agent's socket -------------------------------------------------------


async def test_a_persons_own_key_is_refused(world: _World) -> None:
    ada = await world.person("ada@x.y")
    host = await world.join(world.cookie(ada))

    for asker in (None, "ada@x.y"):
        peer = world.agent(await world.key(ada), asker=asker)
        assert await peer.close_code() == wire_terminals.CLOSE_REFUSED

    assert (await world.wait(host)).json() is None


async def test_another_owners_host_is_refused(world: _World) -> None:
    ada = await world.person("ada@x.y")
    _ = await world.person("bob@x.y")
    host = await world.join(world.cookie(ada))
    headers = await world.assistant()

    foreign = world.agent(headers, asker="bob@x.y")
    unknown = world.agent(headers, host="nowhere")

    assert await foreign.close_code() == wire_terminals.CLOSE_REFUSED
    assert await unknown.close_code() == wire_terminals.CLOSE_REFUSED
    assert (await world.wait(host)).json() is None


@pytest.mark.parametrize(
    "caller",
    [
        "assistant_without_asker",
        "assistant_for_a_viewer",
        "assistant_for_nobody_known",
        "assistant_for_a_disabled_user",
        "assistant_with_a_viewer_key",
        "assistants_browser_session",
        "owners_browser",
        "owners_browser_naming_asker",
        "another_admins_key",
        "service_account",
        "no_credential",
        "unknown_key",
        "auth_disabled",
    ],
)
async def test_nobody_but_the_assistant_naming_a_writer_or_admin_opens_a_connection(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
    caller: str,
) -> None:
    ada = await world.person("ada@x.y")
    host = await world.join(world.cookie(ada))
    _ = await world.person("vic@x.y", role="viewer")
    _ = await world.person("dee@x.y", status="disabled")
    admin = await world.person("boss@x.y", role="admin")
    service = await world.person("svc@service.invalid")
    assistant_id = await world.person(_ASSISTANT)
    assistant = await world.key(assistant_id)
    if caller == "auth_disabled":
        patch_state(monkeypatch, "config", Config(auth_disabled=True))

    peer = world.agent(
        {
            "assistant_without_asker": assistant,
            "assistant_for_a_viewer": assistant,
            "assistant_for_nobody_known": assistant,
            "assistant_for_a_disabled_user": assistant,
            "assistant_with_a_viewer_key": await world.key(
                assistant_id,
                ceiling="viewer",
            ),
            "assistants_browser_session": world.browser(world.cookie(assistant_id)),
            "owners_browser": world.browser(world.cookie(ada)),
            "owners_browser_naming_asker": world.browser(world.cookie(ada)),
            "another_admins_key": await world.key(admin),
            "service_account": await world.key(service),
            "no_credential": {},
            "unknown_key": {"Authorization": "Bearer nonsense"},
            "auth_disabled": world.browser(world.cookie(ada)),
        }[caller],
        asker={
            "assistant_without_asker": None,
            "assistant_for_a_viewer": "vic@x.y",
            "assistant_for_nobody_known": "ghost@x.y",
            "assistant_for_a_disabled_user": "dee@x.y",
            "owners_browser": None,
        }.get(caller, "ada@x.y"),
    )

    assert await peer.close_code() == wire_terminals.CLOSE_REFUSED
    if caller != "auth_disabled":
        assert (await world.wait(host)).json() is None
    await world.finish()


async def test_an_admin_asker_owns_a_connection_too(world: _World) -> None:
    boss = await world.person("ada@x.y", role="admin")
    host = await world.join(world.cookie(boss))

    # Pairing proves the line crossed.
    _ = await world.paired(host, await world.assistant())


@pytest.mark.parametrize(
    "state",
    ["revoked", "unconfirmed", "tools_off", "tools_not_a_flag", "exec_only"],
)
async def test_a_host_that_is_revoked_unconfirmed_or_not_opted_in_opens_nothing(
    world: _World,
    state: str,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    facts: dict[str, dict[str, object]] = {
        "tools_off": {"tools": False},
        "tools_not_a_flag": {"tools": "true"},
        "exec_only": {"exec": True},
    }
    host = await world.join(
        cookie,
        confirm=state != "unconfirmed",
        facts=facts.get(state, {"tools": True}),
    )
    if state == "revoked":
        revoked = await world.call(
            "POST",
            REVOKE_PATH.format(name="gx10"),
            headers=world.browser(cookie),
        )
        assert revoked.status_code == 204

    peer = world.agent(await world.assistant())

    assert await peer.close_code() == wire_terminals.CLOSE_REFUSED
    waited = await world.wait(host)
    assert waited.status_code != 200 or waited.json() is None


async def test_a_host_heard_from_an_hour_ago_still_opens_a_connection(
    world: _World,
) -> None:
    """Whether its host is up is for the tool connection to find: it dials back, or does not."""
    ada = await world.person("ada@x.y")
    host = await world.join(world.cookie(ada))
    async with world.store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE hosts SET last_heartbeat = now() - interval '1 hour'",
        )

    agent, daemon = await world.paired(host, await world.assistant())

    agent.leave()
    assert await daemon.close_code() == wire_terminals.CLOSE_ENDED
    await world.finish()


# -- A host whose host reports no tools fact -------------------------------------------


@pytest.mark.parametrize("name", ["gx10", "a" * 63])
async def test_a_host_without_the_tools_fact_is_refused_at_once(
    world: _World,
    name: str,
) -> None:
    ada = await world.person("ada@x.y")
    host = await world.join(world.cookie(ada), name, facts={"exec": True})

    peer = world.agent(await world.assistant(), host=name)
    started = time.monotonic()

    assert await peer.close_code() == wire_terminals.CLOSE_REFUSED
    assert time.monotonic() - started < Config().tool_dial_back_sec / 4
    assert peer.reason == f"{name} serves no tools: update its host, or drop --no-exec"
    assert len(peer.reason.encode()) <= 123
    assert (await world.wait(host)).json() is None
    assert tool_relay(app)._connections == {}
    await world.finish()


# -- Logging each call ------------------------------------------------------------------


_PRIVATE = "sk-TOPSECRET-4242"
"""What a call's arguments hold, which no log line may."""


def _fields(message: str) -> dict[str, str]:
    """Return a log line's ``key=value`` fields, a quoted value unquoted."""
    found = cast(
        "list[tuple[str, str, str]]",
        re.findall(r"(\w+)=(?:'([^']*)'|(\S+))", message),
    )
    return {key: quoted or bare for key, quoted, bare in found}


def _calls(caplog: pytest.LogCaptureFixture) -> list[dict[str, str]]:
    """Return the fields of each call the relay logged, oldest first."""
    return [
        _fields(record.getMessage())
        for record in caplog.records
        if record.getMessage().startswith("tool call:")
    ]


async def test_each_call_logs_one_line_and_never_its_arguments(
    world: _World,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=tool_routes.__name__)
    ada = await world.person("ada@x.y")
    host = await world.join(world.cookie(ada))
    agent, daemon = await world.paired(host, await world.assistant())
    (connection_id,) = tool_relay(app)._connections
    arguments: dict[str, object] = {"command": f"echo {_PRIVATE}", "cwd": "/work"}
    shown = json.dumps(arguments, sort_keys=True).encode()
    agent.push(_call("c1", arguments))
    assert json.loads(await daemon.frame()) == json.loads(_call("c1", arguments))

    (logged,) = _calls(caplog)
    assert logged == {
        "connection": str(connection_id),
        "asker": "ada@x.y",
        "agent": "main",
        "session": "s1",
        "host": "gx10",
        "tool": "bash",
        "call_id": "c1",
        "argument_bytes": str(len(shown)),
        "verdict": "allow",
    }
    assert _PRIVATE not in caplog.text
    agent.push(_call("c2"))
    _ = await daemon.frame()
    assert [call["call_id"] for call in _calls(caplog)] == ["c1", "c2"]


async def test_a_denied_call_is_logged_and_never_reaches_the_host(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=tool_routes.__name__)

    def deny(call: GatedCall) -> Verdict:
        return Verdict(outcome="deny", reason=f"No {call.tool}.")

    monkeypatch.setattr(tool_routes, "authorize", deny)
    ada = await world.person("ada@x.y")
    host = await world.join(world.cookie(ada))
    agent, daemon = await world.paired(host, await world.assistant())

    agent.push(_call("c1"))
    agent.push(_PING)

    assert await daemon.frame() == _PING
    assert await agent.frame() == refused_line("c1", "No bash.")
    assert [(call["tool"], call["verdict"]) for call in _calls(caplog)] == [
        ("bash", "deny"),
    ]


def _named(call_id: str, tool: str) -> bytes:
    """Return a ``Call`` line for ``tool``."""
    line = cast(dict[str, object], json.loads(_call(call_id)))
    line["tool"] = tool
    return (json.dumps(line) + "\n").encode()


async def test_a_frame_of_calls_is_logged_in_order_and_routed_by_each_verdict(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=tool_routes.__name__)

    def gate(call: GatedCall) -> Verdict:
        if call.tool == "nope":
            return Verdict(outcome="deny", reason="No.")
        return Verdict(outcome="allow")

    monkeypatch.setattr(tool_routes, "authorize", gate)
    ada = await world.person("ada@x.y")
    host = await world.join(world.cookie(ada))
    agent, daemon = await world.paired(host, await world.assistant())
    last = _named("c4", "t4")

    agent.push(
        _named("c1", "t1") + _named("c2", "nope") + _named("c3", "t3") + last[:20],
    )

    forwarded = (await daemon.frame()).splitlines()
    assert [json.loads(line)["id"] for line in forwarded] == ["c1", "c3"]
    assert await agent.frame() == refused_line("c2", "No.")
    assert [(call["tool"], call["verdict"]) for call in _calls(caplog)] == [
        ("t1", "allow"),
        ("nope", "deny"),
        ("t3", "allow"),
    ]

    agent.push(last[20:])

    assert json.loads(await daemon.frame())["id"] == "c4"
    assert [call["tool"] for call in _calls(caplog)][-1] == "t4"


async def test_a_line_that_cannot_be_read_ends_the_connection_with_its_own_code(
    world: _World,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ada = await world.person("ada@x.y")
    host = await world.join(world.cookie(ada))
    agent, daemon = await world.paired(host, await world.assistant())
    (connection_id,) = tool_relay(app)._connections

    with caplog.at_level("INFO"):
        agent.push(f"{_PRIVATE} not json\n".encode())
        assert await daemon.close_code() == wire_terminals.CLOSE_REFUSED
        assert await agent.close_code() == wire_terminals.CLOSE_REFUSED
        assert agent.reason.startswith("the server will not relay a line: Expecting")
        await world.finish()

    about = [
        _fields(record.getMessage())
        for record in caplog.records
        if f"connection={connection_id}" in record.getMessage()
    ]
    assert [
        (each.get("failure"), each.get("by"), each.get("cause")) for each in about
    ] == [
        ("JSONDecodeError", None, None),
        (None, "agent", "JSONDecodeError"),
    ]
    assert _PRIVATE not in caplog.text


# -- Who may open the host's socket --------------------------------------------------------


async def test_a_stale_instance_is_refused(world: _World) -> None:
    ada = await world.person("ada@x.y")
    host = await world.join(world.cookie(ada))
    agent = world.agent(await world.assistant())
    connection_id = await world.offer(host)

    stale = world.daemon(host, connection_id, instance=uuid.uuid4())

    assert await stale.close_code() == wire_terminals.CLOSE_REFUSED

    # The refused dial took nothing: the current instance still gets the tool connection.
    daemon = world.daemon(host, connection_id)
    agent.push(_HELLO)
    assert await daemon.frame() == _HELLO


async def test_an_instance_taken_over_after_its_offer_is_refused(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    host = await world.join(world.cookie(ada))
    _ = world.agent(await world.assistant())
    connection_id = await world.offer(host)
    _ = await _take_over(world, host)

    old = world.daemon(host, connection_id)

    assert await old.close_code() == wire_terminals.CLOSE_REFUSED


async def test_another_owners_host_cannot_attach(world: _World) -> None:
    ada = await world.person("ada@x.y")
    bob = await world.person("bob@x.y")
    host = await world.join(world.cookie(ada))
    bobs = await world.join(world.cookie(bob), name="bobs")
    agent = world.agent(await world.assistant())
    connection_id = await world.offer(host)

    # With its own credential on its own path, and with it on the victim's path.
    own_path = world.daemon(bobs, connection_id)
    victim_path = world.daemon(host, connection_id, headers=bobs.headers())

    assert await own_path.close_code() == wire_terminals.CLOSE_REFUSED
    assert await victim_path.close_code() == wire_terminals.CLOSE_REFUSED

    daemon = world.daemon(host, connection_id)
    agent.push(_HELLO)
    assert await daemon.frame() == _HELLO


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer nonsense"},
    ],
)
async def test_a_host_socket_without_the_hosts_credential_is_refused(
    world: _World,
    headers: dict[str, str],
) -> None:
    ada = await world.person("ada@x.y")
    host = await world.join(world.cookie(ada))
    _ = world.agent(await world.assistant())
    connection_id = await world.offer(host)

    peer = world.daemon(host, connection_id, headers=headers)

    assert await peer.close_code() == wire_terminals.CLOSE_REFUSED


async def test_a_persons_session_cannot_open_a_host_socket(world: _World) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    host = await world.join(cookie)
    _ = world.agent(await world.assistant())
    connection_id = await world.offer(host)

    peer = world.daemon(host, connection_id, headers=world.browser(cookie))

    assert await peer.close_code() == wire_terminals.CLOSE_REFUSED


async def test_a_host_socket_for_a_connection_never_offered_is_refused(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    host = await world.join(world.cookie(ada))

    peer = world.daemon(host, uuid.uuid4())

    assert await peer.close_code() == wire_terminals.CLOSE_REFUSED


async def test_a_revoked_credential_dialing_back_is_told_it_is_revoked(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    host = await world.join(cookie)
    agent = world.agent(await world.assistant())
    connection_id = await world.offer(host)
    revoked = await world.call(
        "POST",
        REVOKE_PATH.format(name="gx10"),
        headers=world.browser(cookie),
    )
    assert revoked.status_code == 204

    peer = world.daemon(host, connection_id)

    assert await peer.close_code() == wire_terminals.CLOSE_HOST_REVOKED
    assert await agent.close_code() == wire_terminals.CLOSE_HOST_REVOKED


async def test_the_host_socket_refuses_a_server_without_auth(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ada = await world.person("ada@x.y")
    host = await world.join(world.cookie(ada))
    patch_state(monkeypatch, "config", Config(auth_disabled=True))

    peer = world.daemon(host, uuid.uuid4())

    assert await peer.close_code() == wire_terminals.CLOSE_REFUSED


# -- What ends a tool connection -------------------------------------------------------------------


async def test_a_revoked_host_ends_its_connections(world: _World) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    host = await world.join(cookie)
    headers = await world.assistant()
    agent, daemon = await world.paired(host, headers)
    queued = world.agent(headers)

    revoked = await world.call(
        "POST",
        REVOKE_PATH.format(name="gx10"),
        headers=world.browser(cookie),
    )

    assert revoked.status_code == 204
    assert await agent.close_code() == wire_terminals.CLOSE_HOST_REVOKED
    assert await daemon.close_code() == wire_terminals.CLOSE_HOST_REVOKED
    assert await queued.close_code() == wire_terminals.CLOSE_HOST_REVOKED
    await world.finish()


async def test_a_host_that_joins_again_ends_the_connections_of_its_old_self(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    host = await world.join(cookie)
    agent, daemon = await world.paired(host, await world.assistant())

    _ = await world.join(cookie)

    assert await agent.close_code() == wire_terminals.CLOSE_HOST_REVOKED
    assert await daemon.close_code() == wire_terminals.CLOSE_HOST_REVOKED


# -- Asking the rule again ---------------------------------------------------------------


# Each change that breaks the rule, applied while a tool connection is up, has the sweep end it;
# applied before a tool connection opens, has the open refused. Both name the same check.
_BROKEN: Final[dict[str, tuple[str, Check]]] = {
    "no_assistant": ("", "assistant"),
    "another_assistant": ("", "key"),
    "key_revoked": ("UPDATE api_keys SET revoked_at = now()", "key"),
    "key_lowered": ("UPDATE api_keys SET role = 'viewer'", "key"),
    "assistant_disabled": (
        "UPDATE users SET status = 'disabled' WHERE email = 'kb@x.y'",
        "key",
    ),
    "assistant_demoted": (
        "UPDATE users SET role = 'viewer' WHERE email = 'kb@x.y'",
        "key",
    ),
    "asker_disabled": (
        "UPDATE users SET status = 'disabled' WHERE email = 'ada@x.y'",
        "asker",
    ),
    "asker_demoted": (
        "UPDATE users SET role = 'viewer' WHERE email = 'ada@x.y'",
        "asker",
    ),
    "given_away": ("UPDATE hosts SET owner = 'bob@x.y'", "host"),
    "credential_revoked": (
        "UPDATE host_credentials SET revoked_at = now()",
        "credential",
    ),
    "unconfirmed": ("UPDATE hosts SET confirmed_at = NULL", "confirmed"),
    "tools_off": ("UPDATE hosts SET facts = '{\"tools\": false}'", "tools"),
    "no_tools_fact": ("UPDATE hosts SET facts = '{\"exec\": true}'", "tools"),
    "tools_not_a_flag": ('UPDATE hosts SET facts = \'{"tools": "true"}\'', "tools"),
}
"""Each change that breaks the rule, and the check that then fails."""


async def _break(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    sql, _ = _BROKEN[change]
    if sql:
        async with world.store.engine.acquire() as conn:
            await conn.execute(sql)
        return
    patch_state(
        monkeypatch,
        "config",
        Config(
            session_secret=_SESSION_KEY,
            session_max_age_seconds=600,
            assistant=(
                None
                if change == "no_assistant"
                else Assistant(actor="kb", email="someone-else@x.y")
            ),
        ),
    )


def _checks(caplog: pytest.LogCaptureFixture, prefix: str) -> list[str]:
    """Return the check each warning starting with ``prefix`` named."""
    return [
        found.group(1)
        for r in caplog.records
        if r.levelno == logging.WARNING
        and r.getMessage().startswith(prefix)
        and (found := re.search(r"check=(\w+)", r.getMessage())) is not None
    ]


@pytest.mark.parametrize("change", list(_BROKEN))
async def test_the_open_and_the_sweep_refuse_alike(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    change: str,
) -> None:
    caplog.set_level(logging.WARNING)
    ada = await world.person("ada@x.y")
    _ = await world.person("bob@x.y")
    host = await world.join(world.cookie(ada))
    headers = await world.assistant()
    agent, daemon = await world.paired(host, headers)

    await _break(world, monkeypatch, change)
    await tool_relay(app).sweep()
    refused = world.agent(headers)

    assert await agent.close_code() == wire_terminals.CLOSE_REFUSED
    assert await daemon.close_code() == wire_terminals.CLOSE_REFUSED
    assert await refused.close_code() == wire_terminals.CLOSE_REFUSED
    await world.finish()
    _, check = _BROKEN[change]
    assert _checks(caplog, "tool connection no longer allowed") == [check]
    assert _checks(caplog, "tool connection refused") == [check]
    assert tool_relay(app)._connections == {}


@pytest.mark.parametrize(
    ("change", "check"),
    [
        ("UPDATE hosts SET daemon_instance = gen_random_uuid()", "instance"),
        ("UPDATE hosts SET daemon_instance = NULL", "instance"),
        ("SELECT 1", None),
    ],
)
async def test_a_connection_a_host_attached_needs_that_host_to_stay_the_hosts(
    world: _World,
    change: str,
    check: Check | None,
) -> None:
    """Asked at the sweep alone: an open has no host yet, and an attach asks itself."""
    ada = await world.person("ada@x.y")
    host = await world.join(world.cookie(ada))
    _ = await world.paired(host, await world.assistant())
    (connection,) = tool_relay(app)._connections.values()
    async with world.store.engine.acquire() as conn:
        _ = await conn.execute(change)
        access = await tool_access(
            conn,
            assistant=Assistant(actor="kb", email=_ASSISTANT),
            api_key_id=connection.api_key_id,
            asker=connection.asker,
            host=connection.host_id,
            instance=connection.instance,
        )
    assert (access.host_id, access.failed) == (host.id, check)


async def test_a_host_is_named_by_its_name_or_its_id_and_the_asker_by_any_case(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    host = await world.join(world.cookie(ada))
    headers = await world.assistant()
    _ = await world.paired(host, headers)
    (connection,) = tool_relay(app)._connections.values()
    assistant = Assistant(actor="kb", email=_ASSISTANT)
    async with world.store.engine.acquire() as conn:
        by_name = await tool_access(
            conn,
            assistant=assistant,
            api_key_id=connection.api_key_id,
            asker="Ada@X.Y",
            host=host.name,
        )
        nowhere = await tool_access(
            conn,
            assistant=assistant,
            api_key_id=connection.api_key_id,
            asker="ada@x.y",
            host=uuid.uuid4(),
        )
        nobody = await tool_access(
            conn,
            assistant=assistant,
            api_key_id=connection.api_key_id,
            asker=None,
            host=host.name,
        )
    assert (by_name.host_id, by_name.failed) == (host.id, None)
    assert (nowhere.host_id, nowhere.failed) == (None, "host")
    assert (nobody.host_id, nobody.failed) == (None, "asker")


async def test_another_key_of_the_assistant_revoked_leaves_a_connection_up(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    host = await world.join(world.cookie(ada))
    assistant = await world.person(_ASSISTANT)
    agent, daemon = await world.paired(host, await world.key(assistant))
    _ = await world.key(assistant)

    async with world.store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE api_keys SET revoked_at = now() WHERE id = "
            "(SELECT id FROM api_keys ORDER BY created_at DESC LIMIT 1)",
        )
    await tool_relay(app).sweep()

    agent.push(_PING)
    assert await daemon.frame() == _PING


async def test_a_live_connection_survives_the_sweep_and_goes_on_relaying(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    host = await world.join(world.cookie(ada))
    agent, daemon = await world.paired(host, await world.assistant())

    await tool_relay(app).sweep()

    agent.push(_PING)
    assert await daemon.frame() == _PING
    daemon.push(b"Pong\n")
    assert await agent.frame() == b"Pong\n"


async def test_a_database_error_during_the_sweep_leaves_a_connection_up(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ada = await world.person("ada@x.y")
    host = await world.join(world.cookie(ada))
    agent, daemon = await world.paired(host, await world.assistant())

    def refused() -> None:
        raise OSError("the database is down")

    with monkeypatch.context() as down:
        down.setattr(world.store.engine, "acquire", refused)
        await tool_relay(app).sweep()

    agent.push(_PING)
    assert await daemon.frame() == _PING


async def test_a_queued_connection_is_not_ended_for_a_takeover_so_the_successor_takes_it(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    host = await world.join(world.cookie(ada))
    agent = world.agent(await world.assistant())
    for _ in range(2_000):
        if tool_relay(app)._connections:
            break
        # Relay state follows database I/O; nothing signals it.
        await asyncio.sleep(0.005)  # house-ignore[sleep] -- See above.
    successor = await _take_over(world, host)

    await tool_relay(app).sweep()

    handed = await world.wait(host, instance=successor, wait_sec=5)
    daemon = world.daemon(
        host,
        ToolOffer.model_validate(handed.json()).connection_id,
        instance=successor,
    )
    agent.push(_HELLO)
    assert await daemon.frame() == _HELLO


async def test_a_queued_connection_is_ended_when_its_key_is_revoked(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    _ = await world.join(world.cookie(ada))
    agent = world.agent(await world.assistant())
    for _ in range(2_000):
        if tool_relay(app)._connections:
            break
        # Relay state follows database I/O; nothing signals it.
        await asyncio.sleep(0.005)  # house-ignore[sleep] -- See above.

    async with world.store.engine.acquire() as conn:
        await conn.execute("UPDATE api_keys SET revoked_at = now()")
    await tool_relay(app).sweep()

    assert await agent.close_code() == wire_terminals.CLOSE_REFUSED


# -- The host's wait --------------------------------------------------------------------


async def test_the_wait_takes_only_the_hosts_own_credential_and_instance(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    host = await world.join(cookie)
    _ = world.agent(await world.assistant())

    stale = await world.wait(host, instance=uuid.uuid4())
    as_person = await world.call(
        "GET",
        f"{HOST_TOOLS_WAIT_PATH.format(host_id=host.id)}?instance={_INSTANCE}",
        headers={"Cookie": cookie},
    )

    assert stale.status_code == 403
    assert as_person.status_code == 401


async def test_an_offer_whose_response_is_lost_heals_by_the_dial_back_and_a_redial(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_state(monkeypatch, "config", _config(tool_dial_back_sec=0.2))
    ada = await world.person("ada@x.y")
    host = await world.join(world.cookie(ada))
    assistant = await world.assistant()
    lost = world.agent(assistant)
    key = uuid.uuid4()

    first = await world.wait(host, wait_sec=5, key=key)
    # The retry of the lost response, under its key, finds no offer kept for it.
    again = await world.wait(host, wait_sec=0, key=key)

    assert ToolOffer.model_validate(first.json()).connection_id
    assert again.json() is None
    # No host dials back, so the tool connection ends, and the agent dials a new one.
    assert await lost.close_code() == wire_terminals.CLOSE_DAEMON_LOST
    agent, daemon = await world.paired(host, assistant)
    agent.leave()
    assert await daemon.close_code() == wire_terminals.CLOSE_ENDED
    await world.finish()


async def test_the_wait_refuses_a_server_without_auth(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ada = await world.person("ada@x.y")
    host = await world.join(world.cookie(ada))
    patch_state(monkeypatch, "config", Config(auth_disabled=True))

    waited = await world.wait(host)

    assert waited.status_code == 403


async def test_an_offer_a_superseded_host_claims_costs_one_dial_back_and_a_redial(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_state(monkeypatch, "config", _config(tool_dial_back_sec=0.2))
    host = await world.join(world.cookie(await world.person("ada@x.y")))
    old = asyncio.ensure_future(world.wait(host, wait_sec=5))
    for _ in range(2_000):
        if host.id in tool_relay(app)._waiters:
            break
        # Relay state follows database I/O; nothing signals it.
        await asyncio.sleep(0.005)  # house-ignore[sleep] -- See above.
    successor = await _take_over(world, host)

    assistant = await world.assistant()
    first = world.agent(assistant)
    claimed = ToolOffer.model_validate((await old).json()).connection_id
    stale = world.daemon(host, claimed)

    # Its dial-back is refused, as only the current instance attaches.
    assert await stale.close_code() == wire_terminals.CLOSE_REFUSED
    assert await first.close_code() == wire_terminals.CLOSE_DAEMON_LOST
    second = world.agent(assistant)
    handed = await world.wait(host, instance=successor, wait_sec=5)
    daemon = world.daemon(
        host,
        ToolOffer.model_validate(handed.json()).connection_id,
        instance=successor,
    )
    second.push(_HELLO)
    assert await daemon.frame() == _HELLO


# -- An old host, on the exec wire, for one release --------------------------------------


async def test_an_old_hosts_exec_wait_gets_null(world: _World) -> None:
    ada = await world.person("ada@x.y")
    host = await world.join(world.cookie(ada))
    agent = world.agent(await world.assistant())
    path = HOST_EXEC_WAIT_PATH.format(host_id=host.id)

    waited = await world.call(
        "GET",
        f"{path}?instance={_INSTANCE}&wait_sec=0",
        headers=host.headers() | {"Idempotency-Key": str(uuid.uuid4())},
    )

    assert (waited.status_code, waited.json()) == (200, None)
    # The waiting agent stayed queued for a host on the tools wire.
    daemon = world.daemon(host, await world.offer(host))
    agent.push(_HELLO)
    assert await daemon.frame() == _HELLO


async def test_an_old_hosts_exec_wait_is_held_so_it_does_not_spin(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    host = await world.join(world.cookie(ada))
    path = HOST_EXEC_WAIT_PATH.format(host_id=host.id)

    waiting = asyncio.ensure_future(
        world.call(
            "GET",
            f"{path}?instance={_INSTANCE}&wait_sec=5",
            headers=host.headers(),
        ),
    )
    for _ in range(2_000):
        if host.id in tool_relay(app)._waiters:
            break
        # Relay state follows database I/O; nothing signals it.
        await asyncio.sleep(0.005)  # house-ignore[sleep] -- See above.

    assert host.id in tool_relay(app)._waiters
    assert not waiting.done()
    tool_relay(app).close_host(host.id)
    assert (await waiting).status_code == 410


async def test_an_old_hosts_exec_wait_keeps_its_credential_and_instance_checks(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    host = await world.join(cookie)
    path = HOST_EXEC_WAIT_PATH.format(host_id=host.id)
    query = f"?instance={_INSTANCE}&wait_sec=0"

    stale = await world.call(
        "GET",
        f"{path}?instance={uuid.uuid4()}&wait_sec=0",
        headers=host.headers(),
    )
    as_person = await world.call(
        "GET",
        path + query,
        headers={"Cookie": cookie},
    )
    assert (stale.status_code, as_person.status_code) == (403, 401)

    patch_state(monkeypatch, "config", Config(auth_disabled=True))
    without_auth = await world.call("GET", path + query, headers=host.headers())
    assert without_auth.status_code == 403


async def test_the_exec_command_and_result_routes_are_gone(world: _World) -> None:
    ada = await world.person("ada@x.y")
    host = await world.join(world.cookie(ada))

    run = await world.call(
        "POST",
        "/api/hosts/gx10/exec?asker=ada@x.y",
        headers=await world.assistant(),
        body={"command": "ls", "cwd": "~", "timeout_sec": 5},
    )
    result = await world.call(
        "POST",
        HOST_EXEC_RESULT_PATH.format(host_id=host.id, exec_id=uuid.uuid4()),
        headers=host.headers(),
        body={},
    )

    assert (run.status_code, result.status_code) == (404, 404)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
