"""The live session's routes end to end over PGlite: who may do what, and a process's life.

The HTTP routes are driven through the whole app with real keys and host credentials.
The process's socket is called as the server calls it, with a Starlette ``WebSocket``
over a scripted ASGI channel, since the test client cannot share the session event loop
that owns the database. The test plays the daemon: it holds the wait and reports ends.
Every delay the routes take runs on a stand-in sleep the test lets pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, cast, override
from unittest.mock import AsyncMock, Mock

import asyncio
import uuid

from fastapi import Request, Response
from starlette.websockets import WebSocket
from treekle import from_plain

import pytest
import pytest_asyncio

from trackinizer.server import auth
from trackinizer.server.api import session_relay_routes
from trackinizer.server.api._deps import Later
from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import (
    clear_identity_override,
    patch_state,
    refusal_detail_code,
)
from trackinizer.server.api.session_relay_routes import ProcessKeySweep
from trackinizer.server.api.session_test_support import _always
from trackinizer.server.auth import create_api_key, process_key_name
from trackinizer.server.config import ChatOrgs, Config, RemoteAgentService
from trackinizer.server.host_auth import mint_secret
from trackinizer.server.route_iter import iter_route_leaves
from trackinizer.server.session import SESSION_COOKIE_NAME, set_session_cookie
from trackinizer.server.session_relay import BAD_FRAME, Dropped, SessionRelay
from trackinizer.server.store.session import RunPlace
from trackinizer.wire.wire_daemon import (
    CREDENTIAL_PREFIX,
    ENROLL_PATH,
    REVOKE_PATH,
)
from trackinizer.wire.wire_hosts import (
    HOST_DISCONNECT_PATH,
    HOST_LOGOUT_PATH,
    HOST_RUN_STOP_PATH,
)
from trackinizer.wire.wire_session_ir import (
    AppendRecordsRequest,
    ManifestBody,
    RecordBody,
    session_parts_path,
    session_records_path,
)
from trackinizer.wire.wire_session_relay import (
    HOST_SESSIONS_ENDED_PATH,
    HOST_SESSIONS_WAIT_PATH,
    MAX_FRAME_BYTES,
    SESSION_INPUTS_PATH,
    SESSION_LINES_PATH,
    SESSION_PROCESS_PATH,
    SESSION_PROCESS_WS_PATH,
    AgentProcess,
    EventLine,
    Hello,
    Input,
    NewInput,
    Opened,
    ProcessEnded,
    ProcessLine,
    ProcessSpec,
    RecordLine,
    ServeSession,
)
from trackinizer.wire.wire_terminals import (
    CLOSE_ENDED,
    CLOSE_HOST_REVOKED,
    CLOSE_REFUSED,
    CLOSE_TAKEN_OVER,
)


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping

    from starlette.types import Message

    import httpx2

    from trackinizer.server.api._deps import Sleep
    from trackinizer.server.auth import Role
    from trackinizer.server.store.core import Store
    from trackinizer.wire.wire_session_relay import ViewerFrame


pytestmark = [
    pytest.mark.db_pglite,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.usefixtures("cheap_scrypt"),
]

_SESSION_KEY: Final = "session-signing-key"
_HOST: Final = "trax.test"
_ORIGIN: Final = "https://trax.test"
_INSTANCE: Final = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_AGENTS: Final = "agents@service.invalid"
_RUNS: Final[dict[str, object]] = {"sessions": True, "logins": True}
_SERVICE: Final[dict[str, object]] = {
    "sessions": True,
    "logins": True,
    "remote_agents": True,
}


class _Sleeps:
    """A stand-in sleep: each wait is held until the test lets its length pass."""

    def __init__(self) -> None:
        self.held: list[tuple[float, asyncio.Future[None]]] = []
        self._asked = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        waited = asyncio.get_running_loop().create_future()
        self.held.append((seconds, waited))
        self._asked.set()
        await waited

    async def asked(self, seconds: float) -> None:
        """Return once a wait of ``seconds`` is held."""
        async with asyncio.timeout(5):
            while not any(
                length == seconds and not waited.done() for length, waited in self.held
            ):
                self._asked.clear()
                _ = await self._asked.wait()

    def pass_(self, seconds: float) -> int:
        """Let every wait of ``seconds`` end; return how many there were."""
        passing = [held for held in self.held if held[0] == seconds]
        for _, waited in passing:
            if not waited.done():
                waited.set_result(None)
        self.held = [held for held in self.held if held[0] != seconds]
        return len(passing)


class _Relay(SessionRelay):
    """The relay, saying when a daemon's wait has reached it, and what its caller said."""

    def __init__(self) -> None:
        super().__init__()
        self.waiting = asyncio.Event()
        self.gone: list[bool] = []
        """What each wait's caller answered when asked whether it had gone."""

    @override
    async def wait(
        self,
        host_id: uuid.UUID,
        *,
        wait_sec: float,
        dropped: Dropped,
    ) -> ServeSession | None:
        # Set in the step that goes on to hold the wait, so a test woken by it finds
        # the wait held.
        self.waiting.set()
        self.gone.append(await dropped())
        return await super().wait(host_id, wait_sec=wait_sec, dropped=dropped)


class _Later(Later):
    """The app's delayed work, each task kept for the test to await."""

    def __init__(self, *, sleep: Sleep) -> None:
        super().__init__(sleep=sleep)
        self.ran: list[asyncio.Task[None]] = []
        self.delays: list[float] = []

    @override
    def run(
        self,
        delay_sec: float,
        work: Callable[[], Awaitable[None]],
    ) -> asyncio.Task[None]:
        task = super().run(delay_sec, work)
        self.ran.append(task)
        self.delays.append(delay_sec)
        return task


class _Peer:
    """The process's end of its socket, scripted: frames pushed in, messages read out.

    Its scope is the one the app's router hands the socket's route: the route itself
    and the session it names, which a process key is let through by.
    """

    def __init__(self, headers: Mapping[str, str], session_id: uuid.UUID) -> None:
        self._inbox: asyncio.Queue[Message] = asyncio.Queue()
        self._outbox: asyncio.Queue[Message] = asyncio.Queue()
        self._inbox.put_nowait({"type": "websocket.connect"})
        self.websocket = WebSocket(
            {
                "type": "websocket",
                "app": app,
                "path": SESSION_PROCESS_WS_PATH.format(session_id=session_id),
                "route": dict(iter_route_leaves(app))[SESSION_PROCESS_WS_PATH],
                "path_params": {"session_id": str(session_id)},
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
        await self._outbox.put(message)

    def push(self, text: str) -> None:
        self._inbox.put_nowait({"type": "websocket.receive", "text": text})

    def leave(self, code: int) -> None:
        self._inbox.put_nowait({"type": "websocket.disconnect", "code": code})

    async def _next(self) -> Message:
        while True:
            message = await asyncio.wait_for(self._outbox.get(), 5)
            if message["type"] != "websocket.accept":
                return message

    async def input(self) -> Input:
        message = await self._next()
        assert message["type"] == "websocket.send", message
        text = cast(object, message["text"])
        assert isinstance(text, str)
        return Input.model_validate_json(text)

    async def close_code(self) -> int:
        message = await self._next()
        assert message["type"] == "websocket.close", message
        code = cast(object, message["code"])
        assert isinstance(code, int)
        return code


@dataclass(frozen=True, slots=True, kw_only=True)
class _Host:
    name: str
    id: uuid.UUID
    credential: str

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.credential}"}


class _World:
    """A served app with people, hosts, a fresh relay and a stand-in sleep."""

    def __init__(self, client: httpx2.AsyncClient, store: Store) -> None:
        self.client = client
        self.store = store
        self.relay = _Relay()
        self.sleeps = _Sleeps()
        self.later = _Later(sleep=self.sleeps.sleep)
        self.tasks: list[asyncio.Future[None]] = []

    async def person(self, email: str, *, role: str = "writer") -> uuid.UUID:
        user_id = uuid.uuid4()
        async with self.store.engine.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (id, email, name, role, status) "
                "VALUES ($1, $2, 'n', $3, 'active')",
                user_id,
                email,
                role,
            )
        return user_id

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

    def browser(self, user_id: uuid.UUID) -> dict[str, str]:
        signed = Response()
        set_session_cookie(
            signed,
            user_id=str(user_id),
            secret=_SESSION_KEY,
            max_age_seconds=600,
        )
        value = signed.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
        return {
            "Cookie": f"{SESSION_COOKIE_NAME}={value}",
            "Host": _HOST,
            "Origin": _ORIGIN,
        }

    async def host(
        self,
        name: str,
        *,
        owner: str,
        facts: Mapping[str, object],
        confirmed: bool = True,
    ) -> _Host:
        """Enroll and join a host of ``owner``'s, as a daemon that reports ``facts``."""
        minted = mint_secret(CREDENTIAL_PREFIX)
        async with self.store.engine.acquire() as conn:
            host_id = from_plain(
                await conn.fetchval(
                    "INSERT INTO hosts (name, updated_by, owner, facts, confirmed_at, "
                    "daemon_instance, last_heartbeat) "
                    "VALUES ($1, $2, $2, $3, CASE WHEN $4 THEN now() END, $5, now()) "
                    "RETURNING id",
                    name,
                    owner,
                    dict(facts),
                    confirmed,
                    _INSTANCE,
                ),
                uuid.UUID,
            )
            await conn.execute(
                "INSERT INTO host_credentials (id, host_id, secret_sha256) "
                "VALUES ($1, $2, $3)",
                minted.id,
                host_id,
                minted.digest,
            )
        return _Host(name=name, id=host_id, credential=minted.token)

    async def session(self, account: str, *, cli_session_id: str = "") -> uuid.UUID:
        session_id = uuid.uuid4()
        async with self.store.engine.acquire() as conn:
            await conn.execute(
                "INSERT INTO inquiries (id, kind, seq, status, account, title, "
                "agentsession_audience, agentsession_cli, agentsession_cli_session_id) "
                "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
                "$2, 's', 'open', 'sagent', NULLIF($3, ''))",
                session_id,
                account,
                cli_session_id,
            )
        return session_id

    async def call(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        body: Mapping[str, object] | None = None,
    ) -> httpx2.Response:
        return await self.client.request(method, path, json=body, headers=headers)

    async def start(
        self,
        headers: Mapping[str, str],
        session_id: uuid.UUID,
        **spec: object,
    ) -> httpx2.Response:
        return await self.call(
            "PUT",
            SESSION_PROCESS_PATH.format(session_id=session_id),
            headers=headers,
            body=spec,
        )

    async def wait(self, host: _Host, *, wait_sec: float = 0) -> httpx2.Response:
        path = HOST_SESSIONS_WAIT_PATH.format(host_id=host.id)
        return await self.call(
            "GET",
            f"{path}?instance={_INSTANCE}&wait_sec={wait_sec}",
            headers=host.headers(),
        )

    async def served(self, host: _Host) -> ServeSession:
        waited = await self.wait(host)
        assert waited.status_code == 200, waited.text
        return ServeSession.model_validate(waited.json())

    def process(
        self,
        headers: Mapping[str, str],
        session_id: uuid.UUID,
        *,
        hello: Hello | None = None,
    ) -> _Peer:
        """Dial a process's socket in a task of its own, sending ``hello`` first."""
        peer = _Peer(headers, session_id)
        if hello is not None:
            peer.push(hello.model_dump_json())
        self.tasks.append(
            asyncio.ensure_future(
                session_relay_routes.process_socket(peer.websocket, session_id),
            ),
        )
        return peer

    async def key_live(self, process_id: uuid.UUID) -> bool:
        async with self.store.engine.acquire() as conn:
            return (
                await conn.fetchval(
                    "SELECT revoked_at IS NULL FROM api_keys "
                    "WHERE starts_with(name, $1)",
                    f"agent process {process_id}",
                )
                is True
            )


@pytest_asyncio.fixture(loop_scope="session")
async def world(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[_World]:
    client, store = pglite_route_client
    clear_identity_override()
    found = _World(client, store)
    patch_state(monkeypatch, "config", _config())
    patch_state(monkeypatch, "session_relay", found.relay)
    patch_state(monkeypatch, "later", found.later)
    yield found
    for task in found.tasks:
        _ = task.cancel()
    _ = await asyncio.gather(*found.tasks, return_exceptions=True)


def _config(
    *,
    default_remote_agent_service: str = "remote-agents",
    remote_agent_services: tuple[RemoteAgentService, ...] = (
        RemoteAgentService(name="remote-agents", account=_AGENTS, admits=("ada@x.y",)),
    ),
    auth_disabled: bool = False,
    chat_orgs: ChatOrgs = "individual",
) -> Config:
    """Return the configuration: a session key, and a remote agent service for ada."""
    return Config(
        session_secret=_SESSION_KEY,
        session_max_age_seconds=600,
        remote_agent_services=remote_agent_services,
        default_remote_agent_service=default_remote_agent_service,
        auth_disabled=auth_disabled,
        chat_orgs=chat_orgs,
    )


def _request(headers: Mapping[str, str]) -> Request:
    """Return a ``GET`` the app serves, carrying ``headers``."""
    return Request(
        {
            "type": "http",
            "app": app,
            "method": "GET",
            "path": "/",
            "query_string": b"",
            "headers": [
                (key.lower().encode(), value.encode()) for key, value in headers.items()
            ],
        },
    )


async def _places(world: _World) -> dict[str, str]:
    """Give ada a key, a session, her hosts and the remote agent service, and bob a host of his."""
    ada = await world.person("ada@x.y")
    _ = await world.person("bob@x.y")
    _ = await world.person(_AGENTS)
    for name, owner, facts in (
        ("laptop", "ada@x.y", _RUNS),
        ("nologin", "ada@x.y", {"sessions": True}),
        ("remote-agents", _AGENTS, _SERVICE),
        ("bobs", "bob@x.y", _RUNS),
    ):
        _ = await world.host(name, owner=owner, facts=facts)
    return await world.key(ada)


@pytest.mark.parametrize("login_host", ["", "laptop", "remote-agents", "nologin"])
@pytest.mark.parametrize("host", ["", "laptop", "remote-agents", "bobs"])
async def test_a_start_finds_its_place_or_no_place_over_every_pairing(
    world: _World,
    host: str,
    login_host: str,
) -> None:
    """The agent host the server names, the caller's own, a service, another's."""
    headers = await _places(world)
    session_id = await world.session("ada@x.y")

    started = await world.start(headers, session_id, host=host, login_host=login_host)

    if host == "bobs" or login_host == "nologin":
        assert started.status_code == 409, started.text
        assert refusal_detail_code(started) == "no_place"
        assert world.relay.process(session_id) is None
        return
    assert started.status_code == 200, started.text
    process = AgentProcess.model_validate(started.json())
    assert (process.host, process.login_host, process.state) == (
        host or "remote-agents",
        login_host,
        "starting",
    )


@pytest.mark.parametrize(
    ("config", "spec"),
    [
        (_config(default_remote_agent_service=""), {}),
        (_config(), {"args": ["--model", "opus"]}),
        (_config(), {"host": "remote-agents", "args": ["--model", "opus"]}),
        (_config(remote_agent_services=()), {"host": "remote-agents"}),
        (
            _config(
                remote_agent_services=(
                    RemoteAgentService(name="remote-agents", account=_AGENTS),
                ),
            ),
            {},
        ),
        (_config(), {"host": "unconfirmed"}),
        (_config(), {"host": "laptop", "login_host": "bobs"}),
    ],
    ids=[
        "no default service named",
        "flags on the default service",
        "flags on a remote agent service named",
        "a service the server does not name",
        "a service that admits nobody",
        "an unconfirmed host",
        "another account's login",
    ],
)
async def test_a_start_no_place_can_serve_is_refused_before_a_key_is_minted(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
    spec: dict[str, object],
) -> None:
    headers = await _places(world)
    _ = await world.host("unconfirmed", owner="ada@x.y", facts=_RUNS, confirmed=False)
    patch_state(monkeypatch, "config", config)
    session_id = await world.session("ada@x.y")

    started = await world.start(headers, session_id, **spec)

    assert started.status_code == 409, started.text
    assert refusal_detail_code(started) == "no_place"
    async with world.store.engine.acquire() as conn:
        minted = await conn.fetchval(
            "SELECT count(*) FROM api_keys WHERE starts_with(name, 'agent process')",
        )
    assert minted == 0


async def test_a_remote_agent_service_must_say_it_confines_its_processes(
    world: _World,
) -> None:
    headers = await _places(world)
    async with world.store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE hosts SET facts = '{\"sessions\": true}' WHERE name = 'remote-agents'",
        )

    started = await world.start(headers, await world.session("ada@x.y"))

    assert refusal_detail_code(started) == "no_place"


async def test_flags_go_to_the_callers_own_host(world: _World) -> None:
    headers = await _places(world)
    laptop = await world.host("lab", owner="ada@x.y", facts=_RUNS)

    started = await world.start(
        headers,
        await world.session("ada@x.y"),
        host="lab",
        cwd="/work",
        args=["--model", "opus"],
    )

    assert started.status_code == 200, started.text
    served = await world.served(laptop)
    assert (served.spec.host, served.spec.cwd, served.spec.args) == (
        "lab",
        "/work",
        ("--model", "opus"),
    )


@pytest.mark.parametrize(
    ("host", "remote"),
    [("", True), ("laptop", False)],
    ids=["the remote agent service", "ada's own"],
)
async def test_a_start_tells_its_daemon_whether_it_was_placed_for_remote_agents(
    world: _World,
    *,
    host: str,
    remote: bool,
) -> None:
    """A daemon holding the service's host may have been run unconfined."""
    ada = await world.key(await world.person("ada@x.y"))
    _ = await world.person(_AGENTS)
    service = await world.host("remote-agents", owner=_AGENTS, facts=_SERVICE)
    laptop = await world.host("laptop", owner="ada@x.y", facts=_RUNS)

    started = await world.start(ada, await world.session("ada@x.y"), host=host)

    assert started.status_code == 200, started.text
    served = await world.served(service if remote else laptop)
    assert served.remote is remote


async def test_a_start_hands_its_host_a_key_that_serves_the_session_and_only_there(
    world: _World,
) -> None:
    headers = await _places(world)
    laptop = await world.host("lab", owner="ada@x.y", facts=_RUNS)
    session_id = await world.session("ada@x.y")
    started = await world.start(headers, session_id, host="lab")
    process = AgentProcess.model_validate(started.json())

    serve = await world.served(laptop)
    again = await world.wait(laptop)
    viewer = world.relay.lines(session_id, allowed=_always)
    opened = await anext(viewer)
    _ = world.process(
        {"Authorization": f"Bearer {serve.token}"},
        session_id,
        hello=Hello(process_id=serve.process_id, version="1.0", host="elsewhere"),
    )
    attached = await asyncio.wait_for(anext(viewer), 5)

    assert serve.process_id == process.process_id
    assert again.json() is None
    assert isinstance(opened, Opened)
    assert isinstance(attached, ProcessLine)
    assert (attached.process.state, attached.process.host) == ("attached", "lab")
    async with world.store.engine.acquire() as conn:
        key = await conn.fetchrow(
            "SELECT k.role, k.host_id, u.email FROM api_keys k "
            "JOIN users u ON u.id = k.user_id WHERE k.name = $1",
            process_key_name(process.process_id, session_id=session_id),
        )
    assert key is not None
    assert (key["role"], key["host_id"], key["email"]) == (
        "writer",
        laptop.id,
        "ada@x.y",
    )


async def test_a_start_while_one_starts_is_the_one_already_there(world: _World) -> None:
    headers = await _places(world)
    session_id = await world.session("ada@x.y")
    first = await world.start(headers, session_id)

    second = await world.start(headers, session_id, host="laptop")

    assert second.json() == first.json()
    async with world.store.engine.acquire() as conn:
        minted = await conn.fetchval(
            "SELECT count(*) FROM api_keys WHERE starts_with(name, 'agent process')",
        )
    assert minted == 1


async def test_a_start_that_loses_a_race_revokes_the_key_it_minted(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = await _places(world)
    session_id = await world.session("ada@x.y")
    winner = await world.start(headers, session_id)
    winning = AgentProcess.model_validate(winner.json())
    # As if the other start had not landed when this one looked.
    monkeypatch.setattr(world.relay, "process", Mock(return_value=None))

    lost = await world.start(headers, session_id)

    assert lost.json() == winner.json()
    async with world.store.engine.acquire() as conn:
        keys = await conn.fetch(
            "SELECT name, revoked_at IS NULL AS live FROM api_keys "
            "WHERE starts_with(name, 'agent process') ORDER BY created_at",
        )
    assert (keys[0]["name"], keys[0]["live"]) == (
        process_key_name(winning.process_id, session_id=session_id),
        True,
    )
    assert [row["live"] for row in keys] == [True, False]


async def test_an_input_reaches_the_process_under_its_senders_name(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    headers = await world.key(ada)
    session_id = await world.session("ada@x.y")
    process_id = uuid.uuid4()
    peer = world.process(
        headers,
        session_id,
        hello=Hello(process_id=process_id, version="1", host="laptop"),
    )
    viewer = world.relay.lines(session_id, allowed=_always)
    _ = await anext(viewer)
    _ = await asyncio.wait_for(anext(viewer), 5)
    new = NewInput(kind="UserMessage", payload={"content": "hi"})

    sent = await world.call(
        "POST",
        SESSION_INPUTS_PATH.format(session_id=session_id),
        headers=headers,
        body=new.model_dump(mode="json"),
    )

    assert (sent.status_code, sent.json()) == (200, {"id": str(new.id)})
    got = await peer.input()
    assert (got.id, got.author, got.payload) == (new.id, "ada@x.y", {"content": "hi"})


async def test_an_input_nobody_serves_is_not_live(world: _World) -> None:
    headers = await world.key(await world.person("ada@x.y"))
    session_id = await world.session("ada@x.y")

    sent = await world.call(
        "POST",
        SESSION_INPUTS_PATH.format(session_id=session_id),
        headers=headers,
        body={"kind": "Halt", "payload": {}},
    )

    assert sent.status_code == 409
    assert refusal_detail_code(sent) == "not_live"


async def test_a_line_of_files_and_no_words_reaches_the_process(world: _World) -> None:
    headers = await world.key(await world.person("ada@x.y"))
    session_id = await world.session("ada@x.y")
    peer = world.process(
        headers,
        session_id,
        hello=Hello(process_id=uuid.uuid4(), version="1", host="laptop"),
    )
    viewer = world.relay.lines(session_id, allowed=_always)
    _ = await anext(viewer)
    _ = await asyncio.wait_for(anext(viewer), 5)
    picture = {"mime_descriptor": "image/png", "data": {"py/b64": "iVBORw0KGgo="}}
    line = NewInput(
        kind="UserMessage",
        payload={"py/object": "UserMessage", "content": "", "attachments": [picture]},
    )

    sent = await world.call(
        "POST",
        SESSION_INPUTS_PATH.format(session_id=session_id),
        headers=headers,
        body=line.model_dump(mode="json"),
    )

    assert sent.status_code == 200
    got = await peer.input()
    assert (got.id, got.payload) == (line.id, line.payload)


async def test_an_input_the_relay_will_not_hold_is_refused_saying_why(
    world: _World,
) -> None:
    headers = await _places(world)
    session_id = await world.session("ada@x.y")
    _ = await world.start(headers, session_id)

    sent = await world.call(
        "POST",
        SESSION_INPUTS_PATH.format(session_id=session_id),
        headers=headers,
        body={"kind": "UserMessage", "payload": {"content": "x" * MAX_FRAME_BYTES}},
    )

    assert sent.status_code == 409
    assert refusal_detail_code(sent) == "input_refused"
    assert "an input is at most" in sent.text


async def test_a_stop_ends_the_process_and_revokes_its_key_a_minute_later(
    world: _World,
) -> None:
    headers = await _places(world)
    laptop = await world.host("lab", owner="ada@x.y", facts=_RUNS)
    session_id = await world.session("ada@x.y")
    _ = await world.start(headers, session_id, host="lab")
    serve = await world.served(laptop)
    peer = world.process(
        {"Authorization": f"Bearer {serve.token}"},
        session_id,
        hello=Hello(process_id=serve.process_id, version="1", host="lab"),
    )
    viewer = world.relay.lines(session_id, allowed=_always)
    _ = await anext(viewer)
    _ = await asyncio.wait_for(anext(viewer), 5)

    stopped = await world.call(
        "DELETE",
        SESSION_PROCESS_PATH.format(session_id=session_id),
        headers=headers,
    )

    assert stopped.status_code == 200
    process = AgentProcess.model_validate(stopped.json())
    assert (process.state, process.reason) == ("detached", "stopped")
    assert await peer.close_code() == CLOSE_ENDED
    # The start's two give-ups, queued then taken, then the stop's revoke.
    assert world.later.delays == [180.0, 30.0, 60.0]
    revoking = world.later.ran[2]
    await world.sleeps.asked(60.0)
    assert await world.key_live(serve.process_id)
    minted = {"Authorization": f"Bearer {serve.token}"}
    path = session_parts_path(session_id)
    assert (await world.call("GET", path, headers=minted)).status_code == 200
    assert world.sleeps.pass_(60.0) >= 1
    await asyncio.wait_for(revoking, 5)
    assert not await world.key_live(serve.process_id)
    # Verified a moment ago, and refused at once all the same.
    assert (await world.call("GET", path, headers=minted)).status_code == 401


async def test_ending_a_session_stops_its_agent_and_revokes_its_key_a_minute_later(
    world: _World,
) -> None:
    """Chat's Stop session ends the session: its agent keeps no tools and no key."""
    headers = await _places(world)
    lab = await world.host("lab", owner="ada@x.y", facts=_RUNS)
    session_id = await world.session("ada@x.y")
    _ = await world.start(headers, session_id, host="lab")
    serve = await world.served(lab)
    viewer = world.relay.lines(session_id, allowed=_always)
    _ = await anext(viewer)
    peer = world.process(
        {"Authorization": f"Bearer {serve.token}"},
        session_id,
        hello=Hello(process_id=serve.process_id, version="1", host="lab"),
    )
    _ = await asyncio.wait_for(anext(viewer), 5)

    ended = await world.call(
        "POST",
        f"/api/sessions/{session_id}/end",
        headers=headers,
        body={},
    )

    assert ended.status_code == 200, ended.text
    process = world.relay.process(session_id)
    assert process is not None
    assert (process.state, process.reason) == ("detached", "stopped")
    assert await peer.close_code() == CLOSE_ENDED
    # The start's two give-ups, queued then taken, then the stop's revoke.
    assert world.later.delays == [180.0, 30.0, 60.0]
    revoking = world.later.ran[2]
    assert await world.key_live(serve.process_id)
    await world.sleeps.asked(60.0)
    assert world.sleeps.pass_(60.0) >= 1
    await asyncio.wait_for(revoking, 5)
    assert not await world.key_live(serve.process_id)


async def test_a_stop_revokes_the_key_of_the_process_it_stopped_not_its_successor(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A start that lands while the stopped socket closes keeps its own key."""
    headers = await _places(world)
    laptop = await world.host("lab", owner="ada@x.y", facts=_RUNS)
    session_id = await world.session("ada@x.y")
    _ = await world.start(headers, session_id, host="lab")
    first = await world.served(laptop)
    _ = world.process(
        {"Authorization": f"Bearer {first.token}"},
        session_id,
        hello=Hello(process_id=first.process_id, version="1", host="lab"),
    )
    viewer = world.relay.lines(session_id, allowed=_always)
    _ = await anext(viewer)
    _ = await asyncio.wait_for(anext(viewer), 5)
    released = asyncio.Event()
    close = session_relay_routes._ProcessWebSocket.close

    async def held(
        socket: session_relay_routes._ProcessWebSocket,
        code: int = 1000,
        reason: str = "",
    ) -> None:
        _ = await released.wait()
        await close(socket, code, reason)

    monkeypatch.setattr(session_relay_routes._ProcessWebSocket, "close", held)
    path = SESSION_PROCESS_PATH.format(session_id=session_id)
    stopping = asyncio.ensure_future(world.call("DELETE", path, headers=headers))
    detached = await asyncio.wait_for(anext(viewer), 5)
    assert isinstance(detached, ProcessLine)
    assert detached.process.state == "detached"
    started = await world.start(headers, session_id, host="lab")
    second = AgentProcess.model_validate(started.json())
    released.set()
    stopped = AgentProcess.model_validate((await stopping).json())

    assert stopped.process_id == first.process_id != second.process_id
    revoking = world.later.ran[-1]
    await world.sleeps.asked(60.0)
    assert world.sleeps.pass_(60.0) >= 1
    await asyncio.wait_for(revoking, 5)
    assert not await world.key_live(first.process_id)
    assert await world.key_live(second.process_id)


async def test_an_admins_start_mints_a_writer_key_that_reaches_only_its_process_calls(
    world: _World,
) -> None:
    """The process's own calls pass; every other route refuses its key and acts not."""
    ada = await world.person("ada@x.y", role="admin")
    own = await world.key(ada, ceiling="admin")
    lab = await world.host("lab", owner="ada@x.y", facts=_RUNS)
    session_id = await world.session("ada@x.y")
    _ = await world.start(own, session_id, host="lab")
    serve = await world.served(lab)
    minted = {"Authorization": f"Bearer {serve.token}"}
    # A key a start minted before starts were capped at writer, and named their session.
    async with world.store.engine.acquire() as conn:
        role = await conn.fetchval(
            "SELECT role FROM api_keys WHERE starts_with(name, $1)",
            f"agent process {serve.process_id}",
        )
        _, old_secret, _, _ = await create_api_key(
            conn,
            user_id=ada,
            name=f"agent process {uuid.uuid4()}",
            ceiling="admin",
            host_id=lab.id,
        )
    old = {"Authorization": f"Bearer {old_secret}"}
    # Another agent of ada's, attached on lab.
    other = await world.session("ada@x.y")
    _ = await world.start(own, other, host="lab")
    beside = await world.served(lab)
    viewer = world.relay.lines(other, allowed=_always)
    _ = await anext(viewer)
    _ = world.process(
        {"Authorization": f"Bearer {beside.token}"},
        other,
        hello=Hello(process_id=beside.process_id, version="1", host="lab"),
    )
    _ = await asyncio.wait_for(anext(viewer), 5)
    managing = [
        ("GET", "/api/me/tokens", None),
        ("POST", "/api/me/tokens", {"name": "more"}),
        ("GET", "/api/admin/users", None),
    ]
    # No call an agent process makes: each would reach every session or host of ada's.
    # The host routes last: before they were refused, they revoked the minted key.
    acting = [
        (
            "POST",
            SESSION_INPUTS_PATH.format(session_id=other),
            {"kind": "Command", "payload": {"line": "/mcp.list"}},
        ),
        ("PUT", SESSION_PROCESS_PATH.format(session_id=other), {"host": "lab"}),
        ("DELETE", SESSION_PROCESS_PATH.format(session_id=session_id), None),
        ("GET", SESSION_PROCESS_PATH.format(session_id=session_id), None),
        ("POST", ENROLL_PATH, {"name": "laptop"}),
        # A line with no agent's session of its own reads as ada's.
        (
            "POST",
            "/api/messages",
            {"to": {"session": str(other), "label": "Agent"}, "text": "proceed"},
        ),
        ("POST", HOST_RUN_STOP_PATH.format(name="lab", session_id=other), None),
        ("POST", REVOKE_PATH.format(name="lab"), None),
        ("POST", HOST_DISCONNECT_PATH.format(name="lab"), None),
        ("POST", HOST_LOGOUT_PATH.format(name="lab"), None),
    ]

    by_process = [
        (await world.call(method, path, headers=key, body=body)).status_code
        for key in (minted, old)
        for method, path, body in managing
    ]
    acted = [
        (await world.call(method, path, headers=minted, body=body)).status_code
        for method, path, body in acting
    ]
    by_person = [
        (await world.call(method, path, headers=own, body=None)).status_code
        for method, path, _ in managing
        if method == "GET"
    ]
    own_calls = [
        (await world.call("GET", path, headers=minted)).status_code
        for path in (session_parts_path(session_id), session_records_path(session_id))
    ]

    assert role == "writer"
    assert by_process == [403] * 6
    assert acted == [403] * len(acting)
    processes = [world.relay.process(each) for each in (session_id, other)]
    assert [
        (process.process_id, process.state) for process in processes if process
    ] == [(serve.process_id, "starting"), (beside.process_id, "attached")]
    assert world.relay.processes(lab.id) != []
    assert by_person == [200, 200]
    assert own_calls == [200, 200]


async def test_a_process_key_serves_and_writes_its_own_process_s_session_alone(
    world: _World,
) -> None:
    """A process no key was minted for is a person's own, and a process key no person's."""
    headers = await _places(world)
    lab = await world.host("lab", owner="ada@x.y", facts=_RUNS)
    session_id = await world.session("ada@x.y")
    _ = await world.start(headers, session_id, host="lab")
    serve = await world.served(lab)
    minted = {"Authorization": f"Bearer {serve.token}"}
    other = await world.session("ada@x.y")

    strangers = [
        world.process(
            minted,
            other,
            hello=Hello(process_id=process_id, version="1", host="lab"),
        )
        for process_id in (uuid.uuid4(), serve.process_id)
    ]
    refused = [await peer.close_code() for peer in strangers]
    elsewhere = [
        (
            await world.call("GET", session_parts_path(other), headers=minted)
        ).status_code,
        (
            await world.call(
                "POST",
                session_records_path(other),
                headers=minted,
                body={"name": "", "records": []},
            )
        ).status_code,
    ]
    # Ada's own key serves a process she started herself, on any session of hers; the
    # minted key serves its own process, on its own session.
    attached: list[ViewerFrame] = []
    for key, served, process_id in (
        (headers, other, uuid.uuid4()),
        (minted, session_id, serve.process_id),
    ):
        viewer = world.relay.lines(served, allowed=_always)
        _ = await anext(viewer)
        _ = world.process(
            key,
            served,
            hello=Hello(process_id=process_id, version="1", host="laptop"),
        )
        attached.append(await asyncio.wait_for(anext(viewer), 5))

    assert refused == [CLOSE_REFUSED, CLOSE_REFUSED]
    assert elsewhere == [403, 403]
    assert [
        frame.process.state for frame in attached if isinstance(frame, ProcessLine)
    ] == ["attached", "attached"]


async def test_after_a_restart_a_key_no_process_holds_is_revoked_past_its_grace(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop's revoke waited in the memory a restart dropped; a held key stays."""
    headers = await _places(world)
    lab = await world.host("lab", owner="ada@x.y", facts=_RUNS)
    stopped_session, held_session, starting_session = [
        await world.session("ada@x.y") for _ in range(3)
    ]
    _ = await world.start(headers, stopped_session, host="lab")
    stopped = await world.served(lab)
    path = SESSION_PROCESS_PATH.format(session_id=stopped_session)
    assert (await world.call("DELETE", path, headers=headers)).status_code == 200
    # The server restarts: its relay, and the revoke it held, are gone.
    restarted = SessionRelay()
    patch_state(monkeypatch, "session_relay", restarted)
    _ = await world.start(headers, held_session, host="lab")
    held = await world.served(lab)
    viewer = restarted.lines(held_session, allowed=_always)
    _ = await anext(viewer)
    _ = world.process(
        {"Authorization": f"Bearer {held.token}"},
        held_session,
        hello=Hello(process_id=held.process_id, version="1", host="lab"),
    )
    _ = await asyncio.wait_for(anext(viewer), 5)
    starting = AgentProcess.model_validate(
        (await world.start(headers, starting_session, host="lab")).json(),
    )
    now = [0.0]
    sweep = ProcessKeySweep(
        relay=restarted,
        store=world.store,
        clock=lambda: now[0],
        unheld_sec=900.0,
    )
    processes = (stopped.process_id, held.process_id, starting.process_id)

    await sweep.sweep()
    now[0] = 899.0
    await sweep.sweep()
    before = [await world.key_live(each) for each in processes]
    now[0] = 900.0
    await sweep.sweep()
    after = [await world.key_live(each) for each in processes]

    assert before == [True, True, True]
    assert after == [False, True, True]
    minted = {"Authorization": f"Bearer {stopped.token}"}
    assert (await world.call("GET", path, headers=minted)).status_code == 401


async def test_a_stop_of_a_session_with_no_process_is_null(world: _World) -> None:
    headers = await world.key(await world.person("ada@x.y"))

    stopped = await world.call(
        "DELETE",
        SESSION_PROCESS_PATH.format(session_id=await world.session("ada@x.y")),
        headers=headers,
    )

    assert (stopped.status_code, stopped.json()) == (200, None)
    assert world.sleeps.held == []


async def test_a_daemons_report_detaches_the_process_and_revokes_its_key(
    world: _World,
) -> None:
    headers = await _places(world)
    laptop = await world.host("lab", owner="ada@x.y", facts=_RUNS)
    session_id = await world.session("ada@x.y")
    _ = await world.start(headers, session_id, host="lab")
    serve = await world.served(laptop)
    report = ProcessEnded(process_id=serve.process_id, exit_code=1, reason="no login")

    elsewhere = await world.call(
        "POST",
        HOST_SESSIONS_ENDED_PATH.format(host_id=laptop.id),
        headers={"Authorization": "Bearer nonsense"},
        body=report.model_dump(mode="json"),
    )
    ended = await world.call(
        "POST",
        HOST_SESSIONS_ENDED_PATH.format(host_id=laptop.id),
        headers=laptop.headers(),
        body=report.model_dump(mode="json"),
    )

    assert elsewhere.status_code == 401
    assert ended.status_code == 204
    assert not await world.key_live(serve.process_id)
    process = world.relay.process(session_id)
    assert process is not None
    assert (process.state, process.reason) == ("detached", "no login")


async def test_a_processs_output_is_read_by_a_writer_of_its_session_alone(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bob reads ada's session, both of one organisation, and is told only how it ended."""
    patch_state(monkeypatch, "config", _config(chat_orgs="single"))
    ada = await _places(world)
    async with world.store.engine.acquire() as conn:
        adas, bobs = [
            from_plain(
                await conn.fetchval("SELECT id FROM users WHERE email = $1", email),
                uuid.UUID,
            )
            for email in ("ada@x.y", "bob@x.y")
        ]
    # Her own key, at the viewer role, may read her session and write none of it.
    hers_to_read, bob = await world.key(adas, ceiling="viewer"), await world.key(bobs)
    laptop = await world.host("lab", owner="ada@x.y", facts=_RUNS)
    session_id = await world.session("ada@x.y")
    _ = await world.start(ada, session_id, host="lab")
    serve = await world.served(laptop)
    viewer = world.relay.lines(session_id, allowed=_always)
    _ = await anext(viewer)
    report = ProcessEnded(
        process_id=serve.process_id,
        exit_code=1,
        reason="exited with code 1",
        output="Traceback: secret=hunter2",
    )

    ended = await world.call(
        "POST",
        HOST_SESSIONS_ENDED_PATH.format(host_id=laptop.id),
        headers=laptop.headers(),
        body=report.model_dump(mode="json"),
    )

    assert ended.status_code == 204
    told = await asyncio.wait_for(anext(viewer), 5)
    assert isinstance(told, ProcessLine)
    assert (told.process.reason, told.process.output) == ("exited with code 1", "")
    path = SESSION_PROCESS_PATH.format(session_id=session_id)
    hers, read, his = [
        AgentProcess.model_validate((await world.call("GET", path, headers=h)).json())
        for h in (ada, hers_to_read, bob)
    ]
    assert (hers.reason, hers.output) == (
        "exited with code 1",
        "Traceback: secret=hunter2",
    )
    assert (read.output, his.reason, his.output) == ("", "exited with code 1", "")


async def test_a_process_key_its_daemon_reported_ended_is_refused_at_once(
    world: _World,
) -> None:
    """A key verified a moment ago is not honoured from its cache once revoked."""
    ada = await _places(world)
    lab = await world.host("lab", owner="ada@x.y", facts=_RUNS)
    session_id = await world.session("ada@x.y")
    _ = await world.start(ada, session_id, host="lab")
    serve = await world.served(lab)
    minted = {"Authorization": f"Bearer {serve.token}"}
    path = session_parts_path(session_id)
    before = await world.call("GET", path, headers=minted)

    ended = await world.call(
        "POST",
        HOST_SESSIONS_ENDED_PATH.format(host_id=lab.id),
        headers=lab.headers(),
        body=ProcessEnded(
            process_id=serve.process_id,
            exit_code=0,
            reason="bye",
        ).model_dump(mode="json"),
    )
    after = await world.call("GET", path, headers=minted)

    assert (before.status_code, ended.status_code) == (200, 204), before.text
    assert not await world.key_live(serve.process_id)
    assert after.status_code == 401, f"revoked key answered {after.status_code}"


async def test_another_hosts_report_revokes_no_key(world: _World) -> None:
    headers = await _places(world)
    laptop = await world.host("lab", owner="ada@x.y", facts=_RUNS)
    other = await world.host("other", owner="ada@x.y", facts=_RUNS)
    session_id = await world.session("ada@x.y")
    _ = await world.start(headers, session_id, host="lab")
    serve = await world.served(laptop)

    ended = await world.call(
        "POST",
        HOST_SESSIONS_ENDED_PATH.format(host_id=other.id),
        headers=other.headers(),
        body=ProcessEnded(
            process_id=serve.process_id,
            exit_code=0,
            reason="x",
        ).model_dump(
            mode="json",
        ),
    )

    assert ended.status_code == 204
    assert await world.key_live(serve.process_id)
    process = world.relay.process(session_id)
    assert process is not None
    assert process.state == "starting"


async def test_a_revoked_host_stops_its_processes_and_their_keys(world: _World) -> None:
    ada = await world.person("ada@x.y")
    headers = await world.key(ada)
    laptop = await world.host("lab", owner="ada@x.y", facts=_RUNS)
    session_id = await world.session("ada@x.y")
    _ = await world.start(headers, session_id, host="lab")
    serve = await world.served(laptop)
    viewer = world.relay.lines(session_id, allowed=_always)
    _ = await anext(viewer)
    peer = world.process(
        {"Authorization": f"Bearer {serve.token}"},
        session_id,
        hello=Hello(process_id=serve.process_id, version="1", host="lab"),
    )
    _ = await asyncio.wait_for(anext(viewer), 5)
    # Held in the relay when the revoke lands, which wakes it.
    world.relay.waiting.clear()
    waiting = asyncio.ensure_future(world.wait(laptop, wait_sec=5))
    _ = await asyncio.wait_for(world.relay.waiting.wait(), 5)

    revoked = await world.call(
        "POST",
        REVOKE_PATH.format(name="lab"),
        headers=world.browser(ada),
    )

    assert revoked.status_code == 204, revoked.text
    assert await peer.close_code() == CLOSE_HOST_REVOKED
    assert not await world.key_live(serve.process_id)
    assert (await asyncio.wait_for(waiting, 5)).status_code == 410
    # Its socket was verified as it opened, and it is refused at once all the same.
    after = await world.call(
        "GET",
        SESSION_PROCESS_PATH.format(session_id=session_id),
        headers={"Authorization": f"Bearer {serve.token}"},
    )
    assert after.status_code == 401


async def test_a_start_taken_and_never_answered_is_not_started_and_its_key_revoked(
    world: _World,
) -> None:
    """Its daemon died, or the answer was lost, between the take and the start."""
    headers = await _places(world)
    laptop = await world.host("lab", owner="ada@x.y", facts=_RUNS)
    session_id = await world.session("ada@x.y")
    _ = await world.start(headers, session_id, host="lab")
    serve = await world.served(laptop)
    assert world.later.delays == [180.0, 30.0]
    _, giving_up = world.later.ran

    await world.sleeps.asked(30.0)
    assert await world.key_live(serve.process_id)
    assert world.sleeps.pass_(30.0) >= 1
    await asyncio.wait_for(giving_up, 5)

    assert not await world.key_live(serve.process_id)
    process = world.relay.process(session_id)
    assert process is not None
    assert (process.state, process.reason) == ("detached", "not started")
    # The wait asked its request whether its caller had gone; it had not.
    assert world.relay.gone == [False]
    # The next line has no process to reach; a new start makes one, with a new key.
    again = AgentProcess.model_validate((await world.start(headers, session_id)).json())
    assert (again.state, again.process_id != serve.process_id) == ("starting", True)


async def test_a_start_no_daemon_takes_in_time_is_given_up_and_its_key_revoked(
    world: _World,
) -> None:
    """Its host's daemon is away for as long as the host reads offline."""
    headers = await _places(world)
    lab = await world.host("lab", owner="ada@x.y", facts=_RUNS)
    session_id = await world.session("ada@x.y")
    viewer = world.relay.lines(session_id, allowed=_always)
    _ = await anext(viewer)
    started = AgentProcess.model_validate(
        (await world.start(headers, session_id, host="lab")).json(),
    )
    _ = await asyncio.wait_for(anext(viewer), 5)
    assert world.later.delays == [180.0]
    (giving_up,) = world.later.ran

    await world.sleeps.asked(180.0)
    assert await world.key_live(started.process_id)
    assert world.sleeps.pass_(180.0) == 1
    await asyncio.wait_for(giving_up, 5)

    told = await asyncio.wait_for(anext(viewer), 5)
    assert isinstance(told, ProcessLine)
    assert (told.process.state, told.process.reason) == (
        "detached",
        "its host did not take it",
    )
    assert not await world.key_live(started.process_id)
    # Its daemon, back, finds nothing to start.
    assert (await world.wait(lab)).json() is None


async def test_a_start_that_attached_in_time_keeps_its_key(world: _World) -> None:
    headers = await _places(world)
    laptop = await world.host("lab", owner="ada@x.y", facts=_RUNS)
    session_id = await world.session("ada@x.y")
    _ = await world.start(headers, session_id, host="lab")
    serve = await world.served(laptop)
    viewer = world.relay.lines(session_id, allowed=_always)
    _ = await anext(viewer)
    _ = world.process(
        {"Authorization": f"Bearer {serve.token}"},
        session_id,
        hello=Hello(process_id=serve.process_id, version="1", host="lab"),
    )
    _ = await asyncio.wait_for(anext(viewer), 5)
    _, giving_up = world.later.ran

    await world.sleeps.asked(30.0)
    assert world.sleeps.pass_(30.0) >= 1
    await asyncio.wait_for(giving_up, 5)

    assert await world.key_live(serve.process_id)
    process = world.relay.process(session_id)
    assert process is not None
    assert process.state == "attached"


async def test_a_start_a_person_stopped_first_is_revoked_once_by_the_stop(
    world: _World,
) -> None:
    headers = await _places(world)
    laptop = await world.host("lab", owner="ada@x.y", facts=_RUNS)
    session_id = await world.session("ada@x.y")
    _ = await world.start(headers, session_id, host="lab")
    serve = await world.served(laptop)
    stopped = await world.call(
        "DELETE",
        SESSION_PROCESS_PATH.format(session_id=session_id),
        headers=headers,
    )
    assert world.later.delays == [180.0, 30.0, 60.0]
    _, giving_up, revoking = world.later.ran

    await world.sleeps.asked(30.0)
    assert world.sleeps.pass_(30.0) >= 1
    await asyncio.wait_for(giving_up, 5)
    # The give-up finds it stopped already, and leaves the key to the stop's minute.
    assert await world.key_live(serve.process_id)
    assert world.sleeps.pass_(60.0) == 1
    await asyncio.wait_for(revoking, 5)

    assert stopped.status_code == 200
    assert not await world.key_live(serve.process_id)
    process = world.relay.process(session_id)
    assert process is not None
    assert process.reason == "stopped"


async def test_a_wait_that_hands_out_no_start_arms_nothing(world: _World) -> None:
    laptop = await world.host("lab", owner="ada@x.y", facts=_RUNS)
    _ = await world.person("ada@x.y")

    assert (await world.wait(laptop)).json() is None
    assert world.later.delays == []


async def test_the_wait_takes_the_hosts_own_credential_and_current_instance(
    world: _World,
) -> None:
    laptop = await world.host("lab", owner="ada@x.y", facts=_RUNS)
    _ = await world.person("ada@x.y")
    path = HOST_SESSIONS_WAIT_PATH.format(host_id=laptop.id)

    stale = await world.call(
        "GET",
        f"{path}?instance={uuid.uuid4()}&wait_sec=0",
        headers=laptop.headers(),
    )
    stranger = await world.call(
        "GET",
        f"{path}?instance={_INSTANCE}&wait_sec=0",
        headers={"Authorization": "Bearer nonsense"},
    )

    assert (stale.status_code, stranger.status_code) == (403, 401)
    assert (await world.wait(laptop)).json() is None


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", SESSION_PROCESS_PATH, None),
        ("PUT", SESSION_PROCESS_PATH, {}),
        ("DELETE", SESSION_PROCESS_PATH, None),
        ("POST", SESSION_INPUTS_PATH, {"kind": "Halt", "payload": {}}),
    ],
)
async def test_a_reader_who_is_not_a_writer_writes_nothing(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    """Bob reads ada's open session, both of one organisation, and controls none of it."""
    patch_state(monkeypatch, "config", _config(chat_orgs="single"))
    _ = await world.person("ada@x.y")
    bob = await world.key(await world.person("bob@x.y", role="admin"))
    session_id = await world.session("ada@x.y")

    response = await world.call(
        method,
        path.format(session_id=session_id),
        headers=bob,
        body=body,
    )

    assert response.status_code == (200 if method == "GET" else 403), response.text


async def test_a_session_that_is_none_is_404_on_every_route(world: _World) -> None:
    headers = await world.key(await world.person("ada@x.y"))
    nowhere = uuid.uuid4()
    routes: list[tuple[str, str, dict[str, object] | None]] = [
        ("GET", SESSION_PROCESS_PATH, None),
        ("PUT", SESSION_PROCESS_PATH, {}),
        ("DELETE", SESSION_PROCESS_PATH, None),
        ("POST", SESSION_INPUTS_PATH, {"kind": "Halt", "payload": {}}),
        ("GET", SESSION_LINES_PATH, None),
    ]

    statuses = [
        (
            await world.call(
                method,
                path.format(session_id=nowhere),
                headers=headers,
                body=body,
            )
        ).status_code
        for method, path, body in routes
    ]

    assert statuses == [404] * len(routes)


async def test_a_server_with_authentication_off_serves_none_of_it(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    laptop = await world.host("lab", owner="ada@x.y", facts=_RUNS)
    session_id = await world.session("ada@x.y")
    patch_state(monkeypatch, "config", _config(auth_disabled=True))
    routes: list[tuple[str, str, dict[str, object] | None]] = [
        ("GET", SESSION_LINES_PATH.format(session_id=session_id), None),
        ("GET", SESSION_PROCESS_PATH.format(session_id=session_id), None),
        ("PUT", SESSION_PROCESS_PATH.format(session_id=session_id), {}),
        ("DELETE", SESSION_PROCESS_PATH.format(session_id=session_id), None),
        (
            "POST",
            SESSION_INPUTS_PATH.format(session_id=session_id),
            {"kind": "Halt", "payload": {}},
        ),
        (
            "GET",
            f"{HOST_SESSIONS_WAIT_PATH.format(host_id=laptop.id)}?instance={_INSTANCE}",
            None,
        ),
        (
            "POST",
            HOST_SESSIONS_ENDED_PATH.format(host_id=laptop.id),
            {"process_id": str(uuid.uuid4()), "exit_code": 0, "reason": ""},
        ),
    ]

    answers = [
        await world.call(method, path, headers=laptop.headers(), body=body)
        for method, path, body in routes
    ]
    socket = world.process(
        {},
        session_id,
        hello=Hello(process_id=uuid.uuid4(), version="1", host="x"),
    )

    assert [answer.status_code for answer in answers] == [403] * len(routes)
    assert {refusal_detail_code(answer) for answer in answers} == {"sessions_forbidden"}
    assert await socket.close_code() == CLOSE_REFUSED


async def test_a_process_a_person_started_holds_the_session_on_their_host(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    laptop = await world.host("laptop", owner="ada@x.y", facts=_RUNS)
    session_id = await world.session("ada@x.y")
    process_id = uuid.uuid4()

    peer = world.process(
        await world.key(ada),
        session_id,
        hello=Hello(process_id=process_id, version="2.0", host="laptop"),
    )
    viewer = world.relay.lines(session_id, allowed=_always)
    _ = await anext(viewer)
    attached = await asyncio.wait_for(anext(viewer), 5)
    peer.push(EventLine(kind="ModelCallStarted", payload={}).model_dump_json())
    line = await asyncio.wait_for(anext(viewer), 5)

    assert isinstance(attached, ProcessLine)
    assert (attached.process.process_id, attached.process.host) == (
        process_id,
        "laptop",
    )
    assert line == EventLine(kind="ModelCallStarted", payload={})
    # Known by its host, so the host's revoke closes it.
    await world.relay.stop_host(laptop.id)
    assert await peer.close_code() == CLOSE_HOST_REVOKED


@pytest.mark.parametrize(
    "caller",
    ["no_key", "unknown_key", "another_account", "no_session", "viewer_role"],
)
async def test_a_key_that_may_not_serve_the_session_is_refused(
    world: _World,
    caller: str,
) -> None:
    """A viewer-role key would take every input and write every viewer's lines."""
    ada = await world.person("ada@x.y")
    bob = await world.person("bob@x.y")
    session_id = await world.session("ada@x.y")
    headers = {
        "no_key": {},
        "unknown_key": {"Authorization": "Bearer nonsense"},
        "another_account": await world.key(bob),
        "no_session": await world.key(ada),
        "viewer_role": await world.key(ada, ceiling="viewer"),
    }[caller]

    peer = world.process(
        headers,
        uuid.uuid4() if caller == "no_session" else session_id,
        hello=Hello(process_id=uuid.uuid4(), version="1", host="x"),
    )

    assert await peer.close_code() == CLOSE_REFUSED
    assert world.relay.process(session_id) is None


async def test_a_process_whose_key_is_revoked_is_closed_by_the_next_sweep(
    world: _World,
) -> None:
    """Its key was checked as its socket opened; a sweep asks again."""
    ada = await world.person("ada@x.y")
    headers = await world.key(ada)
    session_id = await world.session("ada@x.y")
    viewer = world.relay.lines(session_id, allowed=_always)
    _ = await anext(viewer)
    peer = world.process(
        headers,
        session_id,
        hello=Hello(process_id=uuid.uuid4(), version="1", host="laptop"),
    )
    attached = await asyncio.wait_for(anext(viewer), 5)
    await world.relay.sweep()
    async with world.store.engine.acquire() as conn:
        key_id = from_plain(
            await conn.fetchval("SELECT id FROM api_keys WHERE user_id = $1", ada),
            uuid.UUID,
        )
    revoked = await world.call(
        "POST",
        f"/api/me/tokens/{key_id}/revoke",
        headers=headers,
    )

    await world.relay.sweep()

    assert isinstance(attached, ProcessLine)
    assert revoked.status_code == 200, revoked.text
    assert await peer.close_code() == CLOSE_REFUSED
    detached = await asyncio.wait_for(anext(viewer), 5)
    assert isinstance(detached, ProcessLine)
    assert (detached.process.state, detached.process.reason) == ("detached", "refused")


async def test_a_process_another_key_was_minted_for_is_served_under_none_but_it(
    world: _World,
) -> None:
    """Naming ada's process on his own session, bob could stop it and revoke its key."""
    ada = await world.key(await world.person("ada@x.y"))
    bob = await world.key(await world.person("bob@x.y"))
    lab = await world.host("lab", owner="ada@x.y", facts=_RUNS)
    hers = await world.session("ada@x.y")
    started = await world.start(ada, hers, host="lab")
    assert started.status_code == 200, started.text
    serve = await world.served(lab)
    his = await world.session("bob@x.y")

    peer = world.process(
        bob,
        his,
        hello=Hello(process_id=serve.process_id, version="1", host="x"),
    )
    refused = await peer.close_code()
    stopped = await world.call(
        "DELETE",
        SESSION_PROCESS_PATH.format(session_id=his),
        headers=bob,
    )

    assert refused == CLOSE_REFUSED
    assert world.relay.process(his) is None
    # With no process of his to stop, nothing is revoked, now or a minute later: the
    # delayed work is the two give-ups of ada's own start.
    assert (stopped.status_code, stopped.json()) == (200, None)
    assert world.later.delays == [180.0, 30.0]
    assert await world.key_live(serve.process_id), "bob's stop revoked ada's key"


async def test_a_process_is_known_again_by_the_host_its_key_is_bound_to(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a restart a remote agent's process is its service's to stop, as before."""
    ada = await world.key(await world.person("ada@x.y"))
    _ = await world.person(_AGENTS)
    service = await world.host("remote-agents", owner=_AGENTS, facts=_SERVICE)
    session_id = await world.session("ada@x.y")
    started = await world.start(ada, session_id)
    assert started.status_code == 200, started.text
    serve = await world.served(service)
    # The server restarted: a relay that knows no process.
    restarted = _Relay()
    patch_state(monkeypatch, "session_relay", restarted)
    viewer = restarted.lines(session_id, allowed=_always)
    _ = await anext(viewer)

    peer = world.process(
        {"Authorization": f"Bearer {serve.token}"},
        session_id,
        hello=Hello(process_id=serve.process_id, version="1", host="remote-agents"),
    )
    attached = await asyncio.wait_for(anext(viewer), 5)
    await restarted.stop_host(service.id)

    assert isinstance(attached, ProcessLine)
    assert (attached.process.process_id, attached.process.host) == (
        serve.process_id,
        "remote-agents",
    )
    assert await peer.close_code() == CLOSE_HOST_REVOKED


@pytest.mark.parametrize(
    "origin",
    [_ORIGIN, "https://evil.trax.test"],
    ids=["own page", "sibling page"],
)
async def test_a_signed_in_browser_attaches_no_process_whatever_page_it_is_on(
    world: _World,
    origin: str,
) -> None:
    """An agent process always holds a key: a cookie serves no session."""
    ada = await world.person("ada@x.y")
    session_id = await world.session("ada@x.y")

    peer = world.process(
        {**world.browser(ada), "Origin": origin},
        session_id,
        hello=Hello(process_id=uuid.uuid4(), version="1", host="laptop"),
    )

    assert await peer.close_code() == CLOSE_REFUSED
    assert world.relay.process(session_id) is None
    viewer = world.relay.lines(session_id, allowed=_always)
    _ = await anext(viewer)
    keyed = world.process(
        await world.key(ada),
        session_id,
        hello=Hello(process_id=uuid.uuid4(), version="1", host="laptop"),
    )
    attached = await asyncio.wait_for(anext(viewer), 5)
    assert isinstance(attached, ProcessLine)
    assert attached.process.state == "attached"
    keyed.leave(1000)


@pytest.mark.parametrize("first", ["no hello", "a line", "nothing"])
async def test_a_socket_whose_first_frame_is_no_hello_is_closed_as_bad(
    world: _World,
    first: str,
) -> None:
    ada = await world.person("ada@x.y")
    session_id = await world.session("ada@x.y")

    peer = world.process(await world.key(ada), session_id)
    if first == "a line":
        peer.push(EventLine(kind="X", payload={}).model_dump_json())
    elif first == "no hello":
        peer.push("not json")
    else:
        await world.sleeps.asked(10.0)
        assert world.sleeps.pass_(10.0) == 1

    assert await peer.close_code() == BAD_FRAME


async def test_a_socket_the_relay_refuses_changes_nothing_of_its_session(
    world: _World,
) -> None:
    """Seen, placed and reopened once the relay takes the socket, never before."""
    headers = await world.key(await world.person("ada@x.y"))
    session_id = await world.session("ada@x.y")
    viewer = world.relay.lines(session_id, allowed=_always)
    _ = await anext(viewer)
    _ = world.process(
        headers,
        session_id,
        hello=Hello(process_id=uuid.uuid4(), version="1", host="lab"),
    )
    _ = await asyncio.wait_for(anext(viewer), 5)
    await world.sleeps.asked(60.0)
    # As if the session were ended while its process held it, as the reaper could
    # after a restart of the server.
    async with world.store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET status = 'complete', agentsession_ended = now() "
            "WHERE id = $1",
            session_id,
        )
        await conn.execute(
            "UPDATE session_liveness SET reaped = true WHERE session_id = $1",
            session_id,
        )

    refused = world.process(
        headers,
        session_id,
        hello=Hello(process_id=uuid.uuid4(), version="1", host="other"),
    )

    assert await refused.close_code() == CLOSE_TAKEN_OVER
    async with world.store.engine.acquire() as conn:
        watched = await conn.fetchrow(
            "SELECT host, reaped FROM session_liveness WHERE session_id = $1",
            session_id,
        )
    assert watched is not None
    assert (watched["host"], watched["reaped"]) == ("lab", True)
    assert await _ended(world, session_id) is not None


async def test_an_attached_session_is_watched_and_seen_once_a_minute(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ada = await world.person("ada@x.y")
    session_id = await world.session("ada@x.y")
    seen = AsyncMock(wraps=world.store.record_session_seen)
    monkeypatch.setattr(world.store, "record_session_seen", seen)

    peer = world.process(
        await world.key(ada),
        session_id,
        hello=Hello(process_id=uuid.uuid4(), version="1", host="laptop"),
    )
    # The loop sleeps once the attach has been seen, and again after each minute.
    await world.sleeps.asked(60.0)
    assert seen.await_count == 1
    assert world.sleeps.pass_(60.0) == 1
    await world.sleeps.asked(60.0)
    peer.leave(1000)
    await asyncio.wait_for(asyncio.gather(*world.tasks), 5)

    assert seen.await_count == 2
    assert all(call.kwargs["polled"] for call in seen.await_args_list)
    place = cast(object, seen.await_args_list[0].kwargs["place"])
    assert place == RunPlace(host="laptop", cli="sagent")
    async with world.store.engine.acquire() as conn:
        watched = await conn.fetchrow(
            "SELECT host, cli FROM session_liveness WHERE session_id = $1",
            session_id,
        )
    assert watched is not None
    assert (watched["host"], watched["cli"]) == ("laptop", "sagent")


async def test_a_reaped_session_is_reopened_when_its_process_attaches(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    session_id = await world.session("ada@x.y")
    async with world.store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO session_liveness (session_id, last_seen, reaped) "
            "VALUES ($1, $2, true)",
            session_id,
            datetime(2026, 1, 1, tzinfo=UTC),
        )
        await conn.execute(
            "UPDATE inquiries SET status = 'complete', agentsession_ended = now() "
            "WHERE id = $1",
            session_id,
        )

    _ = world.process(
        await world.key(ada),
        session_id,
        hello=Hello(process_id=uuid.uuid4(), version="1", host="laptop"),
    )
    # The minute's loop starts once the attach is seen and the session reopened.
    await world.sleeps.asked(60.0)

    async with world.store.engine.acquire() as conn:
        ended = await conn.fetchval(
            "SELECT agentsession_ended FROM inquiries WHERE id = $1",
            session_id,
        )
    assert ended is None


async def test_an_ended_session_a_process_attaches_to_is_live_and_stores_its_records(
    world: _World,
) -> None:
    """A process continues a session its own run, an upload or a stop ended."""
    headers = await world.key(await world.person("ada@x.y"))
    session_id = await world.session("ada@x.y")
    ended = await world.call(
        "POST",
        f"/api/sessions/{session_id}/end",
        headers=headers,
        body={},
    )

    _ = world.process(
        headers,
        session_id,
        hello=Hello(process_id=uuid.uuid4(), version="1", host="laptop"),
    )
    # The minute's loop starts once the attach is seen and the session reopened.
    await world.sleeps.asked(60.0)
    stored = await _record(world, headers, session_id)

    assert ended.status_code == 200, ended.text
    assert stored.status_code == 200, stored.text
    assert await _ended(world, session_id) is None


async def test_a_session_stopped_in_chat_is_live_again_once_a_new_agent_attaches(
    world: _World,
) -> None:
    """A stop ends the session and its agent; the next line's agent reopens it."""
    headers = await _places(world)
    lab = await world.host("lab", owner="ada@x.y", facts=_RUNS)
    session_id = await world.session("ada@x.y")
    keys: list[dict[str, str]] = []
    for _ in range(2):
        _ = await world.start(headers, session_id, host="lab")
        serve = await world.served(lab)
        keys.append({"Authorization": f"Bearer {serve.token}"})
        viewer = world.relay.lines(session_id, allowed=_always)
        _ = await anext(viewer)
        _ = world.process(
            keys[-1],
            session_id,
            hello=Hello(process_id=serve.process_id, version="1", host="lab"),
        )
        _ = await asyncio.wait_for(anext(viewer), 5)
        await viewer.aclose()
        if len(keys) == 1:
            ended = await world.call(
                "POST",
                f"/api/sessions/{session_id}/end",
                headers=headers,
                body={},
            )
            assert ended.status_code == 200, ended.text
            assert await _ended(world, session_id) is not None
    await world.sleeps.asked(60.0)

    stored = await _record(world, keys[-1], session_id)

    assert stored.status_code == 200, stored.text
    assert await _ended(world, session_id) is None
    process = world.relay.process(session_id)
    assert process is not None
    assert process.state == "attached"


async def _record(
    world: _World,
    headers: Mapping[str, str],
    session_id: uuid.UUID,
) -> httpx2.Response:
    """Append one record to a part of the session, as a process saves its journal."""
    return await world.call(
        "POST",
        session_records_path(session_id),
        headers=headers,
        body=AppendRecordsRequest(
            name="journal.jsonl",
            manifest=ManifestBody(
                name="journal.jsonl",
                ir_id=uuid.uuid4(),
                format="sagent",
                records=1,
            ),
            records=[RecordBody(idx=0, kind="UserMessage", text="hi")],
        ).model_dump(mode="json"),
    )


async def _ended(world: _World, session_id: uuid.UUID) -> datetime | None:
    """Return when the session ended; ``None`` while it is live."""
    async with world.store.engine.acquire() as conn:
        return from_plain(
            await conn.fetchval(
                "SELECT agentsession_ended FROM inquiries WHERE id = $1",
                session_id,
            ),
            datetime,
            default=None,
        )


async def test_a_viewer_reads_each_frame_as_a_server_sent_event(world: _World) -> None:
    ada = await world.person("ada@x.y")
    session_id = await world.session("ada@x.y")
    request = _request(await world.key(ada))

    streamed = await session_relay_routes.session_lines_route(
        session_id,
        request,
        await auth.request_user(request),
    )
    body = aiter(streamed.body_iterator)
    first = await anext(body)
    process = world.relay.start(
        session_id,
        ProcessSpec(),
        process_id=uuid.uuid4(),
        host_id=uuid.uuid4(),
        host="lab",
        token=f"key-{session_id.hex}",
        remote=False,
    )
    second = await anext(body)

    assert streamed.media_type == "text/event-stream"
    assert first == f"data: {Opened().model_dump_json()}\n\n".encode()
    assert (
        second == f"data: {ProcessLine(process=process).model_dump_json()}\n\n".encode()
    )


async def test_a_viewer_who_may_not_read_the_session_opens_no_stream(
    world: _World,
) -> None:
    _ = await world.person("ada@x.y")
    bob = await world.key(await world.person("bob@x.y"))
    session_id = await world.session("ada@x.y")
    async with world.store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET agentsession_audience = 'private' WHERE id = $1",
            session_id,
        )

    refused = await world.call(
        "GET",
        SESSION_LINES_PATH.format(session_id=session_id),
        headers=bob,
    )

    assert refused.status_code == 403


async def test_a_viewer_who_may_no_longer_read_the_session_reads_no_more_lines(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bob reads ada's open session, both of one organisation, until she hides it."""
    patch_state(monkeypatch, "config", _config(chat_orgs="single"))
    _ = await world.person("ada@x.y")
    bob = await world.key(await world.person("bob@x.y"))
    session_id = await world.session("ada@x.y")
    request = _request(bob)
    streamed = await session_relay_routes.session_lines_route(
        session_id,
        request,
        await auth.request_user(request),
    )
    body = aiter(streamed.body_iterator)
    assert await anext(body) == f"data: {Opened().model_dump_json()}\n\n".encode()
    shown = RecordLine(part=0, record=RecordBody(idx=0, kind="X", text="shown"))
    # A sweep while bob may read leaves his stream as it was.
    await world.relay.sweep()
    world.relay.publish_records(session_id, part=0, records=[shown.record])
    assert await anext(body) == f"data: {shown.model_dump_json()}\n\n".encode()
    async with world.store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET agentsession_audience = 'private' WHERE id = $1",
            session_id,
        )
    reopened = await world.call(
        "GET",
        SESSION_LINES_PATH.format(session_id=session_id),
        headers=bob,
    )

    await world.relay.sweep()
    world.relay.publish_records(
        session_id,
        part=0,
        records=[RecordBody(idx=1, kind="Secret", text="hidden now")],
    )

    assert reopened.status_code == 403, reopened.text
    # The stream ends with nothing more, and its next open hears the refusal.
    with pytest.raises(StopAsyncIteration):
        _ = await asyncio.wait_for(anext(body), 5)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
