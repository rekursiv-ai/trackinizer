"""The tool routes end to end over PGlite: who may open a pipe, and a host's round trip.

The HTTP routes are driven through the whole app with the real sign-in and machine
credentials. A WebSocket route is called as the server calls it, with a Starlette
``WebSocket`` over a scripted ASGI channel, since the test client cannot share the
session event loop that owns the database. The host is played by the test: it holds the
wait route and dials back, as the machine host does.
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
from trackinizer.server.api._deps import pipe_hub
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
from trackinizer.wire.wire_exec import (
    MACHINE_EXEC_RESULT_PATH,
    MACHINE_EXEC_WAIT_PATH,
)
from trackinizer.wire.wire_machine_host import ENROLL_PATH, JOIN_PATH, REVOKE_PATH
from trackinizer.wire.wire_tools import (
    MACHINE_TOOLS_WAIT_PATH,
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
class _Machine:
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
    """A served app with users, the assistant, and a fresh pipe hub."""

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
    ) -> _Machine:
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
                "host_version": "t",
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
        return _Machine(
            name=name,
            id=uuid.UUID(body["machine_id"]),
            credential=body["credential"],
        )

    def agent(
        self,
        headers: Mapping[str, str],
        *,
        asker: str | None = "ada@x.y",
        machine: str = "gx10",
    ) -> _Peer:
        """Dial the agent's leg in a task of its own, as the server would run it."""
        peer = _Peer(headers)
        self.tasks.append(
            asyncio.ensure_future(
                tool_routes.tools_agent_socket(peer.websocket, machine, asker),
            ),
        )
        return peer

    def host(
        self,
        machine: _Machine,
        pipe_id: uuid.UUID,
        *,
        instance: uuid.UUID = _INSTANCE,
        headers: Mapping[str, str] | None = None,
    ) -> _Peer:
        """Dial the host's leg back in a task of its own."""
        peer = _Peer(machine.headers() if headers is None else headers)
        self.tasks.append(
            asyncio.ensure_future(
                tool_routes.tools_host_socket(
                    peer.websocket,
                    machine.id,
                    pipe_id,
                    instance,
                ),
            ),
        )
        return peer

    async def wait(
        self,
        machine: _Machine,
        *,
        instance: uuid.UUID = _INSTANCE,
        wait_sec: float = 0,
        key: uuid.UUID | None = None,
    ) -> httpx2.Response:
        """Hold the wait route as the host does."""
        path = MACHINE_TOOLS_WAIT_PATH.format(machine_id=machine.id)
        return await self.call(
            "GET",
            f"{path}?instance={instance}&wait_sec={wait_sec}",
            headers=machine.headers()
            | ({} if key is None else {"Idempotency-Key": str(key)}),
        )

    async def offer(self, machine: _Machine) -> uuid.UUID:
        """Take the machine's next offer, and return its pipe's id."""
        waited = await self.wait(machine, wait_sec=5)
        assert waited.status_code == 200, waited.text
        return ToolOffer.model_validate(waited.json()).pipe_id

    async def paired(
        self,
        machine: _Machine,
        headers: Mapping[str, str],
    ) -> tuple[_Peer, _Peer]:
        """Bring up a whole pipe, proven by a line crossing it."""
        agent = self.agent(headers)
        host = self.host(machine, await self.offer(machine))
        agent.push(_HELLO)
        assert await host.frame() == _HELLO
        return agent, host

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
    monkeypatch.delattr(app.state, "tool_pipes", raising=False)
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


async def _take_over(world: _World, machine: _Machine) -> uuid.UUID:
    """Make another instance the machine's current host, and return its id."""
    successor = uuid.uuid4()
    async with world.store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE machines SET host_instance = $2 WHERE id = $1",
            machine.id,
            successor,
        )
    return successor


# -- A pipe, whole ----------------------------------------------------------------------


async def test_bytes_cross_a_whole_pipe_and_either_end_closes_the_other(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    machine = await world.join(world.cookie(ada))
    agent, host = await world.paired(machine, await world.assistant())

    host.push(b"Welcome \xff\n")
    assert await agent.frame() == b"Welcome \xff\n"

    host.leave()

    assert await agent.close_code() == wire_terminals.CLOSE_HOST_LOST
    await world.finish()


async def test_an_agent_that_leaves_closes_its_host_and_its_slot_is_free(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    machine = await world.join(world.cookie(ada))
    agent, host = await world.paired(machine, await world.assistant())

    agent.leave()

    assert await host.close_code() == wire_terminals.CLOSE_ENDED
    await world.finish()
    assert pipe_hub(app)._pipes == {}


async def test_a_frame_that_is_not_binary_ends_the_pipe(world: _World) -> None:
    ada = await world.person("ada@x.y")
    machine = await world.join(world.cookie(ada))
    agent, host = await world.paired(machine, await world.assistant())

    agent.push_text("not binary")

    assert await host.close_code() == wire_terminals.CLOSE_ENDED
    assert await agent.close_code() == wire_terminals.CLOSE_ENDED
    await world.finish()


# -- Who may open the agent's leg -------------------------------------------------------


async def test_a_persons_own_key_is_refused(world: _World) -> None:
    ada = await world.person("ada@x.y")
    machine = await world.join(world.cookie(ada))

    for asker in (None, "ada@x.y"):
        peer = world.agent(await world.key(ada), asker=asker)
        assert await peer.close_code() == wire_terminals.CLOSE_REFUSED

    assert (await world.wait(machine)).json() is None


async def test_another_owners_machine_is_refused(world: _World) -> None:
    ada = await world.person("ada@x.y")
    _ = await world.person("bob@x.y")
    machine = await world.join(world.cookie(ada))
    headers = await world.assistant()

    foreign = world.agent(headers, asker="bob@x.y")
    unknown = world.agent(headers, machine="nowhere")

    assert await foreign.close_code() == wire_terminals.CLOSE_REFUSED
    assert await unknown.close_code() == wire_terminals.CLOSE_REFUSED
    assert (await world.wait(machine)).json() is None


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
async def test_nobody_but_the_assistant_naming_a_writer_or_admin_opens_a_pipe(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
    caller: str,
) -> None:
    ada = await world.person("ada@x.y")
    machine = await world.join(world.cookie(ada))
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
        assert (await world.wait(machine)).json() is None
    await world.finish()


async def test_an_admin_asker_owns_a_pipe_too(world: _World) -> None:
    boss = await world.person("ada@x.y", role="admin")
    machine = await world.join(world.cookie(boss))

    # Pairing proves the line crossed.
    _ = await world.paired(machine, await world.assistant())


@pytest.mark.parametrize(
    "state",
    ["revoked", "unconfirmed", "tools_off", "tools_not_a_flag", "exec_only"],
)
async def test_a_machine_that_is_revoked_unconfirmed_or_not_opted_in_opens_nothing(
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
    machine = await world.join(
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
    waited = await world.wait(machine)
    assert waited.status_code != 200 or waited.json() is None


async def test_a_machine_heard_from_an_hour_ago_still_opens_a_pipe(
    world: _World,
) -> None:
    """Whether its host is up is for the pipe to find: it dials back, or does not."""
    ada = await world.person("ada@x.y")
    machine = await world.join(world.cookie(ada))
    async with world.store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE machines SET last_heartbeat = now() - interval '1 hour'",
        )

    agent, host = await world.paired(machine, await world.assistant())

    agent.leave()
    assert await host.close_code() == wire_terminals.CLOSE_ENDED
    await world.finish()


# -- A machine whose host reports no tools fact -------------------------------------------


@pytest.mark.parametrize("name", ["gx10", "a" * 63])
async def test_a_machine_without_the_tools_fact_is_refused_at_once(
    world: _World,
    name: str,
) -> None:
    ada = await world.person("ada@x.y")
    machine = await world.join(world.cookie(ada), name, facts={"exec": True})

    peer = world.agent(await world.assistant(), machine=name)
    started = time.monotonic()

    assert await peer.close_code() == wire_terminals.CLOSE_REFUSED
    assert time.monotonic() - started < Config().tool_dial_back_sec / 4
    assert peer.reason == f"{name} serves no tools: update its host, or drop --no-exec"
    assert len(peer.reason.encode()) <= 123
    assert (await world.wait(machine)).json() is None
    assert pipe_hub(app)._pipes == {}
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
    machine = await world.join(world.cookie(ada))
    agent, host = await world.paired(machine, await world.assistant())
    (pipe_id,) = pipe_hub(app)._pipes
    arguments: dict[str, object] = {"command": f"echo {_PRIVATE}", "cwd": "/work"}
    shown = json.dumps(arguments, sort_keys=True).encode()
    agent.push(_call("c1", arguments))
    assert json.loads(await host.frame()) == json.loads(_call("c1", arguments))

    (logged,) = _calls(caplog)
    assert logged == {
        "pipe": str(pipe_id),
        "asker": "ada@x.y",
        "agent": "main",
        "session": "s1",
        "machine": "gx10",
        "tool": "bash",
        "call_id": "c1",
        "argument_bytes": str(len(shown)),
        "verdict": "allow",
    }
    assert _PRIVATE not in caplog.text
    agent.push(_call("c2"))
    _ = await host.frame()
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
    machine = await world.join(world.cookie(ada))
    agent, host = await world.paired(machine, await world.assistant())

    agent.push(_call("c1"))
    agent.push(_PING)

    assert await host.frame() == _PING
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
    machine = await world.join(world.cookie(ada))
    agent, host = await world.paired(machine, await world.assistant())
    last = _named("c4", "t4")

    agent.push(
        _named("c1", "t1") + _named("c2", "nope") + _named("c3", "t3") + last[:20],
    )

    forwarded = (await host.frame()).splitlines()
    assert [json.loads(line)["id"] for line in forwarded] == ["c1", "c3"]
    assert await agent.frame() == refused_line("c2", "No.")
    assert [(call["tool"], call["verdict"]) for call in _calls(caplog)] == [
        ("t1", "allow"),
        ("nope", "deny"),
        ("t3", "allow"),
    ]

    agent.push(last[20:])

    assert json.loads(await host.frame())["id"] == "c4"
    assert [call["tool"] for call in _calls(caplog)][-1] == "t4"


async def test_a_line_that_cannot_be_read_ends_the_pipe_with_its_own_code(
    world: _World,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ada = await world.person("ada@x.y")
    machine = await world.join(world.cookie(ada))
    agent, host = await world.paired(machine, await world.assistant())
    (pipe_id,) = pipe_hub(app)._pipes

    with caplog.at_level("INFO"):
        agent.push(f"{_PRIVATE} not json\n".encode())
        assert await host.close_code() == wire_terminals.CLOSE_REFUSED
        assert await agent.close_code() == wire_terminals.CLOSE_REFUSED
        assert agent.reason.startswith("the server will not relay a line: Expecting")
        await world.finish()

    about = [
        _fields(record.getMessage())
        for record in caplog.records
        if f"pipe={pipe_id}" in record.getMessage()
    ]
    assert [
        (each.get("failure"), each.get("by"), each.get("cause")) for each in about
    ] == [
        ("JSONDecodeError", None, None),
        (None, "agent", "JSONDecodeError"),
    ]
    assert _PRIVATE not in caplog.text


# -- Who may open the host's leg --------------------------------------------------------


async def test_a_stale_instance_is_refused(world: _World) -> None:
    ada = await world.person("ada@x.y")
    machine = await world.join(world.cookie(ada))
    agent = world.agent(await world.assistant())
    pipe_id = await world.offer(machine)

    stale = world.host(machine, pipe_id, instance=uuid.uuid4())

    assert await stale.close_code() == wire_terminals.CLOSE_REFUSED

    # The refused dial took nothing: the current instance still gets the pipe.
    host = world.host(machine, pipe_id)
    agent.push(_HELLO)
    assert await host.frame() == _HELLO


async def test_an_instance_taken_over_after_its_offer_is_refused(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    machine = await world.join(world.cookie(ada))
    _ = world.agent(await world.assistant())
    pipe_id = await world.offer(machine)
    _ = await _take_over(world, machine)

    old = world.host(machine, pipe_id)

    assert await old.close_code() == wire_terminals.CLOSE_REFUSED


async def test_another_owners_host_cannot_attach(world: _World) -> None:
    ada = await world.person("ada@x.y")
    bob = await world.person("bob@x.y")
    machine = await world.join(world.cookie(ada))
    bobs = await world.join(world.cookie(bob), name="bobs")
    agent = world.agent(await world.assistant())
    pipe_id = await world.offer(machine)

    # With its own credential on its own path, and with it on the victim's path.
    own_path = world.host(bobs, pipe_id)
    victim_path = world.host(machine, pipe_id, headers=bobs.headers())

    assert await own_path.close_code() == wire_terminals.CLOSE_REFUSED
    assert await victim_path.close_code() == wire_terminals.CLOSE_REFUSED

    host = world.host(machine, pipe_id)
    agent.push(_HELLO)
    assert await host.frame() == _HELLO


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer nonsense"},
    ],
)
async def test_a_host_leg_without_the_machines_credential_is_refused(
    world: _World,
    headers: dict[str, str],
) -> None:
    ada = await world.person("ada@x.y")
    machine = await world.join(world.cookie(ada))
    _ = world.agent(await world.assistant())
    pipe_id = await world.offer(machine)

    peer = world.host(machine, pipe_id, headers=headers)

    assert await peer.close_code() == wire_terminals.CLOSE_REFUSED


async def test_a_persons_session_cannot_open_a_host_leg(world: _World) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    machine = await world.join(cookie)
    _ = world.agent(await world.assistant())
    pipe_id = await world.offer(machine)

    peer = world.host(machine, pipe_id, headers=world.browser(cookie))

    assert await peer.close_code() == wire_terminals.CLOSE_REFUSED


async def test_a_host_leg_for_a_pipe_never_offered_is_refused(world: _World) -> None:
    ada = await world.person("ada@x.y")
    machine = await world.join(world.cookie(ada))

    peer = world.host(machine, uuid.uuid4())

    assert await peer.close_code() == wire_terminals.CLOSE_REFUSED


async def test_a_revoked_credential_dialing_back_is_told_it_is_revoked(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    machine = await world.join(cookie)
    agent = world.agent(await world.assistant())
    pipe_id = await world.offer(machine)
    revoked = await world.call(
        "POST",
        REVOKE_PATH.format(name="gx10"),
        headers=world.browser(cookie),
    )
    assert revoked.status_code == 204

    peer = world.host(machine, pipe_id)

    assert await peer.close_code() == wire_terminals.CLOSE_MACHINE_REVOKED
    assert await agent.close_code() == wire_terminals.CLOSE_MACHINE_REVOKED


async def test_the_host_leg_refuses_a_server_without_auth(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ada = await world.person("ada@x.y")
    machine = await world.join(world.cookie(ada))
    patch_state(monkeypatch, "config", Config(auth_disabled=True))

    peer = world.host(machine, uuid.uuid4())

    assert await peer.close_code() == wire_terminals.CLOSE_REFUSED


# -- What ends a pipe -------------------------------------------------------------------


async def test_a_revoked_machine_ends_its_pipes(world: _World) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    machine = await world.join(cookie)
    headers = await world.assistant()
    agent, host = await world.paired(machine, headers)
    queued = world.agent(headers)

    revoked = await world.call(
        "POST",
        REVOKE_PATH.format(name="gx10"),
        headers=world.browser(cookie),
    )

    assert revoked.status_code == 204
    assert await agent.close_code() == wire_terminals.CLOSE_MACHINE_REVOKED
    assert await host.close_code() == wire_terminals.CLOSE_MACHINE_REVOKED
    assert await queued.close_code() == wire_terminals.CLOSE_MACHINE_REVOKED
    await world.finish()


async def test_a_host_that_joins_again_ends_the_pipes_of_its_old_self(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    machine = await world.join(cookie)
    agent, host = await world.paired(machine, await world.assistant())

    _ = await world.join(cookie)

    assert await agent.close_code() == wire_terminals.CLOSE_MACHINE_REVOKED
    assert await host.close_code() == wire_terminals.CLOSE_MACHINE_REVOKED


# -- Asking the rule again ---------------------------------------------------------------


# Each change that breaks the rule, applied while a pipe is up, has the sweep end it;
# applied before a pipe opens, has the open refused. Both name the same check.
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
    "given_away": ("UPDATE machines SET owner = 'bob@x.y'", "machine"),
    "credential_revoked": (
        "UPDATE machine_credentials SET revoked_at = now()",
        "credential",
    ),
    "unconfirmed": ("UPDATE machines SET confirmed_at = NULL", "confirmed"),
    "tools_off": ("UPDATE machines SET facts = '{\"tools\": false}'", "tools"),
    "no_tools_fact": ("UPDATE machines SET facts = '{\"exec\": true}'", "tools"),
    "tools_not_a_flag": ('UPDATE machines SET facts = \'{"tools": "true"}\'', "tools"),
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
    machine = await world.join(world.cookie(ada))
    headers = await world.assistant()
    agent, host = await world.paired(machine, headers)

    await _break(world, monkeypatch, change)
    await pipe_hub(app).sweep()
    refused = world.agent(headers)

    assert await agent.close_code() == wire_terminals.CLOSE_REFUSED
    assert await host.close_code() == wire_terminals.CLOSE_REFUSED
    assert await refused.close_code() == wire_terminals.CLOSE_REFUSED
    await world.finish()
    _, check = _BROKEN[change]
    assert _checks(caplog, "tool pipe no longer allowed") == [check]
    assert _checks(caplog, "tool pipe refused") == [check]
    assert pipe_hub(app)._pipes == {}


@pytest.mark.parametrize(
    ("change", "check"),
    [
        ("UPDATE machines SET host_instance = gen_random_uuid()", "instance"),
        ("UPDATE machines SET host_instance = NULL", "instance"),
        ("SELECT 1", None),
    ],
)
async def test_a_pipe_a_host_attached_needs_that_host_to_stay_the_machines(
    world: _World,
    change: str,
    check: Check | None,
) -> None:
    """Asked at the sweep alone: an open has no host yet, and an attach asks itself."""
    ada = await world.person("ada@x.y")
    machine = await world.join(world.cookie(ada))
    _ = await world.paired(machine, await world.assistant())
    (pipe,) = pipe_hub(app)._pipes.values()
    async with world.store.engine.acquire() as conn:
        _ = await conn.execute(change)
        access = await tool_access(
            conn,
            assistant=Assistant(actor="kb", email=_ASSISTANT),
            api_key_id=pipe.api_key_id,
            asker=pipe.asker,
            machine=pipe.machine_id,
            instance=pipe.instance,
        )
    assert (access.machine_id, access.failed) == (machine.id, check)


async def test_a_machine_is_named_by_its_name_or_its_id_and_the_asker_by_any_case(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    machine = await world.join(world.cookie(ada))
    headers = await world.assistant()
    _ = await world.paired(machine, headers)
    (pipe,) = pipe_hub(app)._pipes.values()
    assistant = Assistant(actor="kb", email=_ASSISTANT)
    async with world.store.engine.acquire() as conn:
        by_name = await tool_access(
            conn,
            assistant=assistant,
            api_key_id=pipe.api_key_id,
            asker="Ada@X.Y",
            machine=machine.name,
        )
        nowhere = await tool_access(
            conn,
            assistant=assistant,
            api_key_id=pipe.api_key_id,
            asker="ada@x.y",
            machine=uuid.uuid4(),
        )
        nobody = await tool_access(
            conn,
            assistant=assistant,
            api_key_id=pipe.api_key_id,
            asker=None,
            machine=machine.name,
        )
    assert (by_name.machine_id, by_name.failed) == (machine.id, None)
    assert (nowhere.machine_id, nowhere.failed) == (None, "machine")
    assert (nobody.machine_id, nobody.failed) == (None, "asker")


async def test_another_key_of_the_assistant_revoked_leaves_a_pipe_up(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    machine = await world.join(world.cookie(ada))
    assistant = await world.person(_ASSISTANT)
    agent, host = await world.paired(machine, await world.key(assistant))
    _ = await world.key(assistant)

    async with world.store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE api_keys SET revoked_at = now() WHERE id = "
            "(SELECT id FROM api_keys ORDER BY created_at DESC LIMIT 1)",
        )
    await pipe_hub(app).sweep()

    agent.push(_PING)
    assert await host.frame() == _PING


async def test_a_live_pipe_survives_the_sweep_and_goes_on_relaying(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    machine = await world.join(world.cookie(ada))
    agent, host = await world.paired(machine, await world.assistant())

    await pipe_hub(app).sweep()

    agent.push(_PING)
    assert await host.frame() == _PING
    host.push(b"Pong\n")
    assert await agent.frame() == b"Pong\n"


async def test_a_database_error_during_the_sweep_leaves_a_pipe_up(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ada = await world.person("ada@x.y")
    machine = await world.join(world.cookie(ada))
    agent, host = await world.paired(machine, await world.assistant())

    def refused() -> None:
        raise OSError("the database is down")

    with monkeypatch.context() as down:
        down.setattr(world.store.engine, "acquire", refused)
        await pipe_hub(app).sweep()

    agent.push(_PING)
    assert await host.frame() == _PING


async def test_a_queued_pipe_is_not_ended_for_a_takeover_so_the_successor_takes_it(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    machine = await world.join(world.cookie(ada))
    agent = world.agent(await world.assistant())
    for _ in range(2_000):
        if pipe_hub(app)._pipes:
            break
        await asyncio.sleep(0.005)
    successor = await _take_over(world, machine)

    await pipe_hub(app).sweep()

    handed = await world.wait(machine, instance=successor, wait_sec=5)
    host = world.host(
        machine,
        ToolOffer.model_validate(handed.json()).pipe_id,
        instance=successor,
    )
    agent.push(_HELLO)
    assert await host.frame() == _HELLO


async def test_a_queued_pipe_is_ended_when_its_key_is_revoked(world: _World) -> None:
    ada = await world.person("ada@x.y")
    _ = await world.join(world.cookie(ada))
    agent = world.agent(await world.assistant())
    for _ in range(2_000):
        if pipe_hub(app)._pipes:
            break
        await asyncio.sleep(0.005)

    async with world.store.engine.acquire() as conn:
        await conn.execute("UPDATE api_keys SET revoked_at = now()")
    await pipe_hub(app).sweep()

    assert await agent.close_code() == wire_terminals.CLOSE_REFUSED


# -- The host's wait --------------------------------------------------------------------


async def test_the_wait_takes_only_the_machines_own_credential_and_instance(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    machine = await world.join(cookie)
    _ = world.agent(await world.assistant())

    stale = await world.wait(machine, instance=uuid.uuid4())
    as_person = await world.call(
        "GET",
        f"{MACHINE_TOOLS_WAIT_PATH.format(machine_id=machine.id)}?instance={_INSTANCE}",
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
    machine = await world.join(world.cookie(ada))
    assistant = await world.assistant()
    lost = world.agent(assistant)
    key = uuid.uuid4()

    first = await world.wait(machine, wait_sec=5, key=key)
    # The retry of the lost response, under its key, finds no offer kept for it.
    again = await world.wait(machine, wait_sec=0, key=key)

    assert ToolOffer.model_validate(first.json()).pipe_id
    assert again.json() is None
    # No host dials back, so the pipe ends, and the agent dials a new one.
    assert await lost.close_code() == wire_terminals.CLOSE_HOST_LOST
    agent, host = await world.paired(machine, assistant)
    agent.leave()
    assert await host.close_code() == wire_terminals.CLOSE_ENDED
    await world.finish()


async def test_the_wait_refuses_a_server_without_auth(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ada = await world.person("ada@x.y")
    machine = await world.join(world.cookie(ada))
    patch_state(monkeypatch, "config", Config(auth_disabled=True))

    waited = await world.wait(machine)

    assert waited.status_code == 403


async def test_an_offer_a_superseded_host_claims_costs_one_dial_back_and_a_redial(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_state(monkeypatch, "config", _config(tool_dial_back_sec=0.2))
    machine = await world.join(world.cookie(await world.person("ada@x.y")))
    old = asyncio.ensure_future(world.wait(machine, wait_sec=5))
    for _ in range(2_000):
        if machine.id in pipe_hub(app)._waiters:
            break
        await asyncio.sleep(0.005)
    successor = await _take_over(world, machine)

    assistant = await world.assistant()
    first = world.agent(assistant)
    claimed = ToolOffer.model_validate((await old).json()).pipe_id
    stale = world.host(machine, claimed)

    # Its dial-back is refused, as only the current instance attaches.
    assert await stale.close_code() == wire_terminals.CLOSE_REFUSED
    assert await first.close_code() == wire_terminals.CLOSE_HOST_LOST
    second = world.agent(assistant)
    handed = await world.wait(machine, instance=successor, wait_sec=5)
    host = world.host(
        machine,
        ToolOffer.model_validate(handed.json()).pipe_id,
        instance=successor,
    )
    second.push(_HELLO)
    assert await host.frame() == _HELLO


# -- An old host, on the exec wire, for one release --------------------------------------


async def test_an_old_hosts_exec_wait_gets_null(world: _World) -> None:
    ada = await world.person("ada@x.y")
    machine = await world.join(world.cookie(ada))
    agent = world.agent(await world.assistant())
    path = MACHINE_EXEC_WAIT_PATH.format(machine_id=machine.id)

    waited = await world.call(
        "GET",
        f"{path}?instance={_INSTANCE}&wait_sec=0",
        headers=machine.headers() | {"Idempotency-Key": str(uuid.uuid4())},
    )

    assert (waited.status_code, waited.json()) == (200, None)
    # The waiting agent stayed queued for a host on the tools wire.
    host = world.host(machine, await world.offer(machine))
    agent.push(_HELLO)
    assert await host.frame() == _HELLO


async def test_an_old_hosts_exec_wait_is_held_so_it_does_not_spin(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    machine = await world.join(world.cookie(ada))
    path = MACHINE_EXEC_WAIT_PATH.format(machine_id=machine.id)

    waiting = asyncio.ensure_future(
        world.call(
            "GET",
            f"{path}?instance={_INSTANCE}&wait_sec=5",
            headers=machine.headers(),
        ),
    )
    for _ in range(2_000):
        if machine.id in pipe_hub(app)._waiters:
            break
        await asyncio.sleep(0.005)

    assert machine.id in pipe_hub(app)._waiters
    assert not waiting.done()
    pipe_hub(app).close_machine(machine.id)
    assert (await waiting).status_code == 410


async def test_an_old_hosts_exec_wait_keeps_its_credential_and_instance_checks(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    machine = await world.join(cookie)
    path = MACHINE_EXEC_WAIT_PATH.format(machine_id=machine.id)
    query = f"?instance={_INSTANCE}&wait_sec=0"

    stale = await world.call(
        "GET",
        f"{path}?instance={uuid.uuid4()}&wait_sec=0",
        headers=machine.headers(),
    )
    as_person = await world.call(
        "GET",
        path + query,
        headers={"Cookie": cookie},
    )
    assert (stale.status_code, as_person.status_code) == (403, 401)

    patch_state(monkeypatch, "config", Config(auth_disabled=True))
    without_auth = await world.call("GET", path + query, headers=machine.headers())
    assert without_auth.status_code == 403


async def test_the_exec_command_and_result_routes_are_gone(world: _World) -> None:
    ada = await world.person("ada@x.y")
    machine = await world.join(world.cookie(ada))

    run = await world.call(
        "POST",
        "/api/machines/gx10/exec?asker=ada@x.y",
        headers=await world.assistant(),
        body={"command": "ls", "cwd": "~", "timeout_sec": 5},
    )
    result = await world.call(
        "POST",
        MACHINE_EXEC_RESULT_PATH.format(machine_id=machine.id, exec_id=uuid.uuid4()),
        headers=machine.headers(),
        body={},
    )

    assert (run.status_code, result.status_code) == (404, 404)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
