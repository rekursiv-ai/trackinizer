"""The terminal routes end to end over PGlite, with the real sign-in and credentials.

The HTTP routes are driven through the whole app. A WebSocket route is called as the
server calls it, with a Starlette ``WebSocket`` over a scripted ASGI channel, since the
test client cannot share the session event loop that owns the database.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import asyncio
import uuid

from fastapi import Response
from starlette.websockets import WebSocket, WebSocketDisconnect
from treekle import from_plain

import pytest

from trackinizer.lib.posix.host import Frame, decode_frame, encode_frame, encode_winsize
from trackinizer.server.api import terminal_routes
from trackinizer.server.api._deps import terminal_hub
from trackinizer.server.api.app import app
from trackinizer.server.api.canvas_test_support import Agent, seed_session
from trackinizer.server.api.conftest import clear_identity_override
from trackinizer.server.auth import create_api_key
from trackinizer.server.config import Assistant, Config
from trackinizer.server.session import SESSION_COOKIE_NAME, set_session_cookie
from trackinizer.server.terminals import Opening
from trackinizer.wire import wire_sessions, wire_terminals
from trackinizer.wire.wire_machine_host import (
    ENROLL_PATH,
    JOIN_PATH,
    REVOKE_PATH,
)
from trackinizer.wire.wire_machines import MACHINES_PATH
from trackinizer.wire.wire_science_chat import (
    SCIENCE_CHAT_LABEL,
    chat_actor,
    chat_session_id,
)


if TYPE_CHECKING:
    from collections.abc import Mapping

    from starlette.types import Message

    import httpx2

    from trackinizer.server.inbound import InboundQueue
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


@pytest.fixture
def cheap_scrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hash secrets at the smallest accepted cost so a test pays no scrypt round."""
    monkeypatch.setattr("trackinizer.server.auth._SCRYPT_N", 2)


class _World:
    """A served app with users, a joined machine and a fresh terminal hub."""

    def __init__(self, client: httpx2.AsyncClient, store: Store) -> None:
        self.client = client
        self.store = store
        self.machine_id = uuid.UUID(int=0)
        self.credential = ""

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

    async def key(self, user_id: uuid.UUID) -> str:
        async with self.store.engine.acquire() as conn:
            _, secret, _, _ = await create_api_key(
                conn,
                user_id=user_id,
                name="k",
                ceiling="writer",
            )
        return secret

    async def call(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        body: Mapping[str, object] | None = None,
    ) -> httpx2.Response:
        return await self.client.request(method, path, json=body, headers=headers)

    def browser(self, cookie: str, *, origin: str | None = _ORIGIN) -> dict[str, str]:
        found = {"Cookie": cookie, "Host": _HOST}
        if origin is not None:
            found["Origin"] = origin
        return found

    async def join(
        self,
        cookie: str,
        name: str = "gx10",
        *,
        confirm: bool = True,
        facts: dict[str, object] | None = None,
    ) -> None:
        """Self-enroll ``name`` with ``cookie``'s user, join it, and confirm it."""
        enrolled = await self.call(
            "POST",
            ENROLL_PATH,
            headers={"Cookie": cookie},
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
                "facts": facts or {},
            },
        )
        assert joined.status_code == 201
        body = from_plain(joined.json(), dict[str, str])
        self.machine_id = uuid.UUID(body["machine_id"])
        self.credential = body["credential"]
        if confirm:
            confirmed = await self.call(
                "POST",
                wire_terminals.TERMINAL_CONFIRM_PATH.format(name=name),
                headers=self.browser(cookie),
            )
            assert confirmed.status_code == 204, confirmed.text

    def host_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.credential}"}

    async def wait(
        self,
        *,
        credential: str | None = None,
        instance: uuid.UUID = _INSTANCE,
        wait_sec: float = 0,
    ) -> httpx2.Response:
        """Ask for the next terminal as the host does; a wait also marks it ready."""
        path = wire_terminals.MACHINE_TERMINAL_WAIT_PATH.format(
            machine_id=self.machine_id,
        )
        return await self.call(
            "GET",
            f"{path}?instance={instance}&wait_sec={wait_sec}",
            headers={"Authorization": f"Bearer {credential or self.credential}"},
        )

    async def open(
        self,
        cookie: str,
        *,
        machine: str = "gx10",
        workspace: uuid.UUID | None = None,
        origin: str | None = _ORIGIN,
        requested_by: str | None = None,
        surface: str | None = None,
        command: str | None = None,
        attach: uuid.UUID | None = None,
        run_name: str | None = None,
        cwd: str | None = None,
    ) -> httpx2.Response:
        body: dict[str, object] = {"machine": machine, "rows": 24, "cols": 80}
        if surface is not None:
            body["surface"] = surface
        if requested_by is not None:
            body["requested_by"] = requested_by
        if command is not None:
            body["command"] = command
        if attach is not None:
            body["attach"] = str(attach)
        if run_name is not None:
            body["run_name"] = run_name
        if cwd is not None:
            body["cwd"] = cwd
        if workspace is not None:
            body["workspace"] = str(workspace)
        return await self.call(
            "POST",
            wire_terminals.TERMINALS_PATH,
            headers=self.browser(cookie, origin=origin),
            body=body,
        )

    async def station(self, user_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
        workspace, workstream = uuid.uuid4(), uuid.uuid4()
        async with self.store.engine.acquire() as conn:
            await conn.execute(
                "INSERT INTO visual_workspaces (id, user_id, state, station) "
                "VALUES ($1, $2, '{}', jsonb_build_object('name', 's', "
                "'workstream_id', $3::text))",
                workspace,
                user_id,
                str(workstream),
            )
        return workspace, workstream


@pytest.fixture
def world(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> _World:
    client, store = pglite_route_client
    clear_identity_override()
    monkeypatch.setattr(
        app.state,
        "config",
        Config(session_secret=_SESSION_KEY, session_max_age_seconds=600),
        raising=False,
    )
    monkeypatch.delattr(app.state, "terminals", raising=False)
    return _World(client, store)


class _Peer:
    """One side of a WebSocket, scripted: frames pushed in, messages read out."""

    def __init__(self, path: str, headers: Mapping[str, str], query: str = "") -> None:
        self._inbox: asyncio.Queue[Message] = asyncio.Queue()
        self._outbox: asyncio.Queue[Message] = asyncio.Queue()
        self._inbox.put_nowait({"type": "websocket.connect"})
        self._dropped = False
        self.websocket = WebSocket(
            {
                "type": "websocket",
                "app": app,
                "path": path,
                "query_string": query.encode(),
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

    def push(self, kind: Frame, payload: bytes = b"") -> None:
        self._inbox.put_nowait(
            {"type": "websocket.receive", "bytes": encode_frame(kind, payload)},
        )

    def leave(self, code: int = 1000) -> None:
        """Make the peer go: the handler reads a disconnect, and later sends raise."""
        self._inbox.put_nowait({"type": "websocket.disconnect", "code": code})
        self._dropped = True

    async def frame(self) -> tuple[Frame, bytes]:
        """Return the next frame sent to this peer, skipping the handshake."""
        while True:
            message = await asyncio.wait_for(self._outbox.get(), 5)
            if message["type"] == "websocket.send":
                assert isinstance(message["bytes"], bytes)
                return decode_frame(message["bytes"])
            assert message["type"] == "websocket.accept", message

    async def close_code(self) -> int:
        """Return the code this peer was closed with."""
        while True:
            message = await asyncio.wait_for(self._outbox.get(), 5)
            if message["type"] == "websocket.close":
                assert isinstance(message["code"], int)
                return message["code"]


def _browser_peer(world: _World, cookie: str, terminal_id: uuid.UUID) -> _Peer:
    return _Peer(
        wire_terminals.TERMINAL_WS_PATH.format(terminal_id=terminal_id),
        world.browser(cookie),
    )


def _host_peer(
    world: _World,
    terminal_id: uuid.UUID,
    *,
    credential: str | None = None,
) -> _Peer:
    return _Peer(
        wire_terminals.MACHINE_TERMINAL_WS_PATH.format(
            machine_id=world.machine_id,
            terminal_id=terminal_id,
        ),
        {"Authorization": f"Bearer {credential or world.credential}"},
    )


def _attach(ticket: str, offset: int = 0) -> bytes:
    return ticket.encode() + offset.to_bytes(8)


def _opened(response: httpx2.Response) -> tuple[uuid.UUID, str]:
    assert response.status_code == 201, response.text
    body = from_plain(response.json(), dict[str, object])
    return uuid.UUID(from_plain(body["terminal_id"], str)), from_plain(
        body["ticket"],
        str,
    )


def _code(response: httpx2.Response) -> str:
    """Return the ``code`` of a refusal's ``detail: {code, message}``."""
    detail = from_plain(response.json(), dict[str, dict[str, str]])["detail"]
    assert set(detail) == {"code", "message"}
    return detail["code"]


async def _run_browser(peer: _Peer, terminal_id: uuid.UUID) -> None:
    await terminal_routes.terminal_web_socket(peer.websocket, terminal_id)


async def _run_host(peer: _Peer, world: _World, terminal_id: uuid.UUID) -> None:
    await terminal_routes.terminal_host_socket(
        peer.websocket,
        world.machine_id,
        terminal_id,
        _INSTANCE,
    )


async def test_an_owner_opens_a_terminal_and_bytes_flow_both_ways_until_exit(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie)
    await world.wait()
    workspace, workstream = await world.station(ada)

    terminal_id, ticket = _opened(
        await world.open(cookie, workspace=workspace, requested_by="helper-1"),
    )
    offered = from_plain((await world.wait()).json(), dict[str, object])
    assert offered["requested_by"] == "helper-1"
    assert offered["terminal_id"] == str(terminal_id)
    assert (offered["opener"], offered["rows"], offered["cols"]) == ("ada@x.y", 24, 80)
    assert (offered["workspace"], offered["workstream"]) == (
        str(workspace),
        str(workstream),
    )

    browser = _browser_peer(world, cookie, terminal_id)
    host = _host_peer(world, terminal_id)
    tasks = [
        asyncio.create_task(_run_browser(browser, terminal_id)),
        asyncio.create_task(_run_host(host, world, terminal_id)),
    ]
    browser.push(Frame.ATTACH, _attach(ticket))
    assert await browser.frame() == (Frame.ACK, (0).to_bytes(8))

    host.push(Frame.OUTPUT, b"$ ")
    assert await browser.frame() == (Frame.OUTPUT, b"$ ")
    browser.push(Frame.INPUT, b"ls\n")
    assert await host.frame() == (Frame.INPUT, b"ls\n")
    browser.push(Frame.RESIZE, encode_winsize(40, 120))
    assert await host.frame() == (Frame.RESIZE, encode_winsize(40, 120))

    host.push(Frame.EXIT, (0).to_bytes(4))
    assert await browser.frame() == (Frame.EXIT, (0).to_bytes(4))
    assert await browser.close_code() == wire_terminals.CLOSE_ENDED
    host.leave()
    browser.leave()
    await asyncio.gather(*tasks)

    listed = await world.call(
        "GET",
        wire_terminals.TERMINAL_MACHINES_PATH,
        headers=world.browser(cookie),
    )
    assert from_plain(listed.json(), dict[str, object])["terminals"] == []


async def _stored(world: _World) -> list[set[str]]:
    """Name what a session would leave behind: rows, manifests and records."""
    queries = (
        "SELECT id::text AS key FROM inquiries",
        "SELECT session_id::text AS key FROM session_manifests",
        (
            "SELECT session_id::text || '/' || part || '/' || idx AS key "
            "FROM session_records"
        ),
    )
    async with world.store.engine.acquire() as conn:
        return [
            {from_plain(each["key"], str) for each in await conn.fetch(query)}
            for query in queries
        ]


async def test_opening_and_using_a_terminal_stores_no_session_and_no_record(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie)
    await world.wait()
    before = await _stored(world)

    opened = await world.open(cookie)
    terminal_id, ticket = _opened(opened)
    browser, host, tasks = await _connected(world, cookie, terminal_id, ticket)
    host.push(Frame.OUTPUT, b"echo hi\r\nhi\r\n")
    assert await browser.frame() == (Frame.OUTPUT, b"echo hi\r\nhi\r\n")
    browser.push(Frame.INPUT, b"ls\n")
    assert await host.frame() == (Frame.INPUT, b"ls\n")
    host.push(Frame.EXIT, (0).to_bytes(4))
    assert await browser.frame() == (Frame.EXIT, (0).to_bytes(4))
    host.leave()
    browser.leave()
    await asyncio.gather(*tasks)
    await asyncio.sleep(0)

    assert await _stored(world) == before
    assert set(from_plain(opened.json(), dict[str, object])) == {
        "terminal_id",
        "ticket",
        "ice_servers",
    }


async def _connected(
    world: _World,
    cookie: str,
    terminal_id: uuid.UUID,
    ticket: str,
) -> tuple[_Peer, _Peer, list[asyncio.Task[None]]]:
    """Run a browser and a host on one terminal; return them once the browser is attached."""
    browser = _browser_peer(world, cookie, terminal_id)
    host = _host_peer(world, terminal_id)
    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(_run_browser(browser, terminal_id)),
        asyncio.create_task(_run_host(host, world, terminal_id)),
    ]
    browser.push(Frame.ATTACH, _attach(ticket))
    assert (await browser.frame())[0] is Frame.ACK
    return browser, host, tasks


async def test_an_abrupt_host_drop_closes_the_browser_and_raises_nothing(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie)
    await world.wait()
    terminal_id, ticket = _opened(await world.open(cookie))
    browser, host, tasks = await _connected(world, cookie, terminal_id, ticket)
    host.push(Frame.OUTPUT, b"last words")
    assert (await browser.frame())[0] is Frame.OUTPUT

    host.leave(code=1006)
    closed = await browser.close_code()
    browser.leave()
    await asyncio.gather(*tasks)

    assert closed == wire_terminals.CLOSE_HOST_LOST
    assert terminal_hub(app).terminal(terminal_id) is None


async def test_a_browser_that_reconnects_with_an_offset_gets_only_what_it_missed(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie)
    await world.wait()
    terminal_id, ticket = _opened(await world.open(cookie))
    first, host, tasks = await _connected(world, cookie, terminal_id, ticket)
    host.push(Frame.OUTPUT, b"abcdef")
    assert await first.frame() == (Frame.OUTPUT, b"abcdef")
    first.leave()
    await tasks[0]
    minted = await world.call(
        "POST",
        wire_terminals.TERMINAL_ATTACH_PATH.format(terminal_id=terminal_id),
        headers=world.browser(cookie),
    )

    second = _browser_peer(world, cookie, terminal_id)
    task = asyncio.create_task(_run_browser(second, terminal_id))
    second.push(
        Frame.ATTACH,
        _attach(
            from_plain(from_plain(minted.json(), dict[str, object])["ticket"], str),
            2,
        ),
    )

    assert await second.frame() == (Frame.ACK, (2).to_bytes(8))
    assert await second.frame() == (Frame.OUTPUT, b"cdef")
    host.push(Frame.OUTPUT, b"g")
    assert await second.frame() == (Frame.OUTPUT, b"g")
    second.leave()
    host.leave()
    await asyncio.gather(task, tasks[1])


async def test_the_listing_names_every_live_terminal_of_the_station_with_its_surface(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    bob = await world.person("bob@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie)
    workspace, _ = await world.station(ada)
    path = f"{wire_terminals.TERMINAL_MACHINES_PATH}?workspace={workspace}"

    before = from_plain(
        (await world.call("GET", path, headers=world.browser(cookie))).json(),
        dict[str, object],
    )
    assert before["machines"] == [
        {
            "name": "gx10",
            "status": "online",
            "terminal_ready": False,
            "confirmed": True,
            "clis": [],
        },
    ]
    not_asked = await world.open(cookie, workspace=workspace)
    assert (not_asked.status_code, _code(not_asked)) == (409, "terminal_not_enabled")

    await world.wait()
    first, _ = _opened(
        await world.open(cookie, workspace=workspace, surface="chat"),
    )
    second, _ = _opened(
        await world.open(cookie, workspace=workspace, surface="tile-1"),
    )
    after = from_plain(
        (await world.call("GET", path, headers=world.browser(cookie))).json(),
        dict[str, object],
    )
    other = from_plain(
        (
            await world.call("GET", path, headers=world.browser(world.cookie(bob)))
        ).json(),
        dict[str, object],
    )

    assert from_plain(after["machines"], list[dict[str, object]])[0]["terminal_ready"]
    assert after["terminals"] == [
        {"terminal_id": str(first), "machine": "gx10", "surface": "chat"},
        {"terminal_id": str(second), "machine": "gx10", "surface": "tile-1"},
    ]
    assert (other["machines"], other["terminals"]) == ([], [])


async def test_the_assistant_lists_the_machines_of_the_person_it_answers(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app.state,
        "config",
        Config(
            session_secret=_SESSION_KEY,
            session_max_age_seconds=600,
            assistant=Assistant(actor="kb", email="kb@x.y"),
        ),
        raising=False,
    )
    ada = await world.person("ada@x.y")
    bob = await world.person("bob@x.y")
    _ = await world.person("vic@x.y", role="viewer")
    assistant = {
        "Authorization": f"Bearer {await world.key(await world.person('kb@x.y'))}",
    }
    cookie = world.cookie(ada)
    await world.join(cookie, name="laptop")
    await world.join(cookie, name="gx10")
    await world.wait()
    workspace, _ = await world.station(ada)
    path = wire_terminals.TERMINAL_MACHINES_PATH

    def owned(email: str) -> str:
        return f"{path}?owner={email}&workspace={workspace}"

    for_ada = await world.call("GET", owned("ada@x.y"), headers=assistant)
    for_bob = await world.call("GET", owned("bob@x.y"), headers=assistant)
    for_viewer = await world.call("GET", owned("vic@x.y"), headers=assistant)
    as_itself = await world.call("GET", path, headers=assistant)
    ada_names_herself = await world.call(
        "GET",
        owned("ada@x.y"),
        headers=world.browser(cookie),
    )
    bob_names_ada = await world.call(
        "GET",
        owned("ada@x.y"),
        headers=world.browser(world.cookie(bob)),
    )

    assert for_ada.status_code == 200, for_ada.text
    assert from_plain(for_ada.json(), dict[str, object]) == {
        "machines": [
            {
                "name": "gx10",
                "status": "online",
                "terminal_ready": True,
                "confirmed": True,
                "clis": [],
            },
            {
                "name": "laptop",
                "status": "online",
                "terminal_ready": False,
                "confirmed": True,
                "clis": [],
            },
        ],
        "terminals": [],
    }
    assert from_plain(for_bob.json(), dict[str, object])["machines"] == []
    refused = (for_viewer, as_itself, ada_names_herself, bob_names_ada)
    assert [each.status_code for each in refused] == [403] * 4
    assert {_code(each) for each in refused} == {"terminal_forbidden"}


async def test_the_assistant_opens_only_a_detached_run_on_its_askers_machine(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app.state,
        "config",
        Config(
            session_secret=_SESSION_KEY,
            session_max_age_seconds=600,
            assistant=Assistant(actor="kb", email="kb@x.y"),
        ),
        raising=False,
    )
    ada = await world.person("ada@x.y")
    assistant = await world.person("kb@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie, facts={"clis": ["claude"]})
    await world.wait()
    station, _ = await world.station(ada)
    session = uuid.uuid4()
    body = {
        "machine": "gx10",
        "rows": 40,
        "cols": 120,
        "workspace": str(station),
        "command": "claude",
        "attach": str(session),
        "run_name": "campaign-issue-7",
        "cwd": "~/work",
    }
    headers = {"Authorization": f"Bearer {await world.key(assistant)}"}

    opened = await world.call(
        "POST",
        f"{wire_terminals.TERMINALS_PATH}?asker=ada@x.y",
        headers=headers,
        body=body,
    )

    _opened(opened)
    waiting = from_plain((await world.wait()).json(), dict[str, object])
    assert (
        waiting["command"],
        waiting["attach"],
        waiting["run_name"],
        waiting["cwd"],
    ) == (
        "claude",
        str(session),
        "campaign-issue-7",
        "~/work",
    )


async def test_campaign_brief_waits_for_the_askers_run_and_places_in_its_station(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app.state,
        "config",
        Config(
            session_secret=_SESSION_KEY,
            session_max_age_seconds=600,
            assistant=Assistant(actor="kb", email="kb@x.y"),
        ),
        raising=False,
    )
    ada = await world.person("ada@x.y")
    assistant = await world.person("kb@x.y")
    station, _ = await world.station(ada)
    assistant_key = {"Authorization": f"Bearer {await world.key(assistant)}"}
    owner_key = {"Authorization": f"Bearer {await world.key(ada)}"}
    started = await world.call(
        "POST",
        "/api/sessions/start",
        headers=assistant_key,
        body={
            "cli": "claude",
            "cli_session_id": "campaign:issue-7",
            "account": "ada@x.y",
            "title": "Probe",
        },
    )
    assert started.status_code == 201, started.text
    session = uuid.UUID(
        from_plain(from_plain(started.json(), dict[str, object])["id"], str),
    )
    async with world.store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET labels = ARRAY['campaign'] WHERE id = $1",
            session,
        )

    queued = await world.call(
        "POST",
        f"/api/sessions/{session}/inbound",
        headers=assistant_key,
        body={"text": "Probe\n\nReport in this chat when done", "asker": "ada@x.y"},
    )
    attached = await world.call(
        "POST",
        "/api/sessions/start",
        headers=owner_key,
        body={"cli": "claude", "attach": str(session)},
    )
    placed = await world.call(
        "POST",
        f"/api/sessions/{session}/place",
        headers=owner_key,
        body={"workspace": str(station)},
    )
    drained = await world.call(
        "GET",
        f"/api/sessions/{session}/inbound",
        headers=owner_key,
    )

    assert (queued.status_code, attached.status_code, placed.status_code) == (
        200,
        201,
        200,
    )
    assert from_plain(queued.json(), dict[str, object])["queued"] == 1
    assert from_plain(placed.json(), dict[str, object])["workspace_id"] == str(station)
    assert "Probe" in drained.text


async def test_the_assistant_cannot_open_a_shell_or_another_askers_machine(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app.state,
        "config",
        Config(
            session_secret=_SESSION_KEY,
            session_max_age_seconds=600,
            assistant=Assistant(actor="kb", email="kb@x.y"),
        ),
        raising=False,
    )
    ada = await world.person("ada@x.y")
    bob = await world.person("bob@x.y")
    _ = await world.person("vic@x.y", role="viewer")
    assistant = await world.person("kb@x.y")
    await world.join(world.cookie(bob), facts={"clis": ["claude"]})
    await world.wait()
    station, _ = await world.station(ada)
    body = {
        "machine": "gx10",
        "rows": 40,
        "cols": 120,
        "workspace": str(station),
        "command": "claude",
        "attach": str(uuid.uuid4()),
        "run_name": "campaign-issue-7",
        "cwd": "~/work",
    }
    headers = {"Authorization": f"Bearer {await world.key(assistant)}"}

    foreign = await world.call(
        "POST",
        f"{wire_terminals.TERMINALS_PATH}?asker=ada@x.y",
        headers=headers,
        body=body,
    )
    viewer = await world.call(
        "POST",
        f"{wire_terminals.TERMINALS_PATH}?asker=vic@x.y",
        headers=headers,
        body=body,
    )
    shell = await world.call(
        "POST",
        f"{wire_terminals.TERMINALS_PATH}?asker=bob@x.y",
        headers=headers,
        body={"machine": "gx10", "rows": 24, "cols": 80},
    )
    no_asker = await world.call(
        "POST",
        wire_terminals.TERMINALS_PATH,
        headers=headers,
        body=body,
    )
    bad_run = await world.call(
        "POST",
        f"{wire_terminals.TERMINALS_PATH}?asker=bob@x.y",
        headers=headers,
        body=body | {"run_name": None},
    )

    assert foreign.status_code == 404
    assert [
        (each.status_code, _code(each)) for each in (viewer, shell, no_asker, bad_run)
    ] == [
        (403, "terminal_forbidden"),
    ] * 4


async def test_an_open_is_refused_unless_a_person_who_owns_an_online_enabled_machine(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    bob = await world.person("bob@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie)

    assert (await world.open(cookie)).status_code == 409
    await world.wait()
    assert (await world.open(world.cookie(bob))).status_code == 404
    assert (await world.open(cookie, machine="nope")).status_code == 404
    foreign = await world.open(cookie, origin=None)
    assert (foreign.status_code, _code(foreign)) == (403, "terminal_forbidden")
    assert (await world.open(cookie, origin="https://evil.test")).status_code == 403
    bad = await world.open(cookie, surface="has space")
    assert bad.status_code == 422
    key = await world.key(ada)
    keyed = await world.call(
        "POST",
        wire_terminals.TERMINALS_PATH,
        headers={"Authorization": f"Bearer {key}", "Origin": _ORIGIN},
        body={"machine": "gx10", "rows": 24, "cols": 80},
    )
    assert keyed.status_code == 403
    stranger_station, _ = await world.station(bob)
    assert (await world.open(cookie, workspace=stranger_station)).status_code == 404
    for _ in range(wire_terminals.MAX_OPENS_PER_MIN_PER_USER):
        _opened(await world.open(cookie))
    over = await world.open(cookie)
    assert (over.status_code, _code(over)) == (429, "terminal_limit")

    async with world.store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE machines SET last_heartbeat = now() - interval '1 hour'",
        )
    assert (await world.open(cookie)).status_code == 404
    async with world.store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE machines SET last_heartbeat = now(), owner = NULL",
        )
    assert (await world.open(cookie)).status_code == 404
    # A host that released the machine on Ctrl-C leaves it offline at once.
    async with world.store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE machines SET owner = 'ada@x.y', host_instance = NULL",
        )
    assert (await world.open(cookie)).status_code == 404


async def _key_enrolled(world: _World, key: str, name: str) -> uuid.UUID:
    """Enroll and join ``name`` as an agent holding ``key`` would; return its id."""
    enrolled = await world.call(
        "POST",
        ENROLL_PATH,
        headers={"Authorization": f"Bearer {key}"},
        body={"name": name},
    )
    assert enrolled.status_code == 201, enrolled.text
    joined = await world.call(
        "POST",
        JOIN_PATH,
        headers={},
        body={
            "name": name,
            "token": from_plain(enrolled.json(), dict[str, str])["token"],
            "instance": str(_INSTANCE),
            "host_version": "t",
        },
    )
    assert joined.status_code == 201, joined.text
    body = from_plain(joined.json(), dict[str, str])
    world.machine_id = uuid.UUID(body["machine_id"])
    world.credential = body["credential"]
    return world.machine_id


async def test_a_machine_an_agent_enrolled_opens_no_terminal_until_its_owner_confirms(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    bob = await world.person("bob@x.y")
    cookie = world.cookie(ada)
    key = await world.key(ada)
    await _key_enrolled(world, key, "older")
    await _key_enrolled(world, key, "evilbox")
    await world.wait()
    confirm = wire_terminals.TERMINAL_CONFIRM_PATH.format(name="evilbox")

    unconfirmed = await world.open(cookie, machine="evilbox")
    listed = from_plain(
        from_plain(
            (
                await world.call(
                    "GET",
                    wire_terminals.TERMINAL_MACHINES_PATH,
                    headers=world.browser(cookie),
                )
            ).json(),
            dict[str, object],
        )["machines"],
        list[dict[str, object]],
    )
    by_key = await world.call(
        "POST",
        confirm,
        headers={"Authorization": f"Bearer {key}", "Origin": _ORIGIN},
    )
    no_origin = await world.call(
        "POST",
        confirm,
        headers=world.browser(cookie, origin=None),
    )
    stranger = await world.call(
        "POST",
        confirm,
        headers=world.browser(world.cookie(bob)),
    )
    assert (unconfirmed.status_code, by_key.status_code) == (409, 403)
    assert (no_origin.status_code, stranger.status_code) == (403, 404)
    assert _code(unconfirmed) == "terminal_unconfirmed"
    assert [(m["name"], m["confirmed"]) for m in listed] == [
        ("evilbox", False),
        ("older", False),
    ]

    mine = await world.call("POST", confirm, headers=world.browser(cookie))
    opened = await world.open(cookie, machine="evilbox")

    assert (mine.status_code, opened.status_code) == (204, 201)


async def test_every_terminal_route_and_both_sockets_refuse_a_server_without_auth(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie)
    await world.wait()
    terminal_id, _ = _opened(await world.open(cookie))
    monkeypatch.setattr(
        app.state,
        "config",
        Config(auth_disabled=True),
        raising=False,
    )
    rebound = {"Host": "evil.example:8765", "Origin": "http://evil.example:8765"}
    machine = wire_terminals.TERMINAL_MACHINES_PATH
    paths = [
        (
            "POST",
            wire_terminals.TERMINALS_PATH,
            {"machine": "gx10", "rows": 24, "cols": 80},
        ),
        ("GET", machine, None),
        ("POST", wire_terminals.TERMINAL_CONFIRM_PATH.format(name="gx10"), None),
        (
            "POST",
            wire_terminals.TERMINAL_ATTACH_PATH.format(terminal_id=terminal_id),
            None,
        ),
        (
            "POST",
            wire_terminals.TERMINAL_CLOSE_PATH.format(terminal_id=terminal_id),
            None,
        ),
    ]

    answers = [
        (await world.call(method, path, headers=rebound, body=body)).status_code
        for method, path, body in paths
    ]
    waited = await world.wait()
    browser = _Peer(
        wire_terminals.TERMINAL_WS_PATH.format(terminal_id=terminal_id),
        rebound,
    )
    browser.push(Frame.ATTACH, _attach("x" * wire_terminals.TICKET_CHARS))
    await _run_browser(browser, terminal_id)
    host = _host_peer(world, terminal_id)
    await _run_host(host, world, terminal_id)

    assert answers == [403] * 5
    assert waited.status_code == 403
    assert await browser.close_code() == wire_terminals.CLOSE_REFUSED
    assert await host.close_code() == wire_terminals.CLOSE_REFUSED


async def test_the_sweep_ends_a_terminal_whose_owner_is_no_longer_a_writer(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie)
    await world.wait()
    terminal_id, _ = _opened(await world.open(cookie))
    hub = terminal_hub(app)
    await hub.sweep()
    assert hub.terminal(terminal_id) is not None

    async with world.store.engine.acquire() as conn:
        await conn.execute("UPDATE users SET role = 'viewer'")
    await hub.sweep()

    assert hub.terminal(terminal_id) is None


async def test_a_wait_is_clamped_to_its_ceiling_and_needs_the_current_instance(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ada = await world.person("ada@x.y")
    await world.join(world.cookie(ada))
    monkeypatch.setattr(wire_terminals, "WAIT_MAX_SEC", 0.01)

    clamped = await asyncio.wait_for(world.wait(wait_sec=1000), 10)
    stale = await world.wait(instance=uuid.uuid4())

    assert (clamped.status_code, clamped.json()) == (200, None)
    assert stale.status_code == 403


async def test_the_browser_socket_refuses_a_bad_ticket_a_reuse_a_stranger_and_a_key(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    bob = await world.person("bob@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie)
    await world.wait()
    terminal_id, ticket = _opened(await world.open(cookie))

    async def refused(peer: _Peer, first: bytes) -> int:
        peer.push(Frame.ATTACH, first)
        await _run_browser(peer, terminal_id)
        return await peer.close_code()

    bad = _browser_peer(world, cookie, terminal_id)
    foreign_origin = _Peer(
        wire_terminals.TERMINAL_WS_PATH.format(terminal_id=terminal_id),
        world.browser(cookie, origin="https://evil.test"),
    )
    stranger = _browser_peer(world, world.cookie(bob), terminal_id)
    keyed = _Peer(
        wire_terminals.TERMINAL_WS_PATH.format(terminal_id=terminal_id),
        {"Authorization": f"Bearer {await world.key(ada)}", "Origin": _ORIGIN},
    )
    codes = [
        await refused(bad, _attach("x" * wire_terminals.TICKET_CHARS)),
        await refused(foreign_origin, _attach(ticket)),
        await refused(stranger, _attach(ticket)),
        await refused(keyed, _attach(ticket)),
    ]

    assert codes == [wire_terminals.CLOSE_REFUSED] * 4
    # A ticket is spent by any attempt that names it, so the opener asks for another.
    minted = await world.call(
        "POST",
        wire_terminals.TERMINAL_ATTACH_PATH.format(terminal_id=terminal_id),
        headers=world.browser(cookie),
    )
    ticket = from_plain(from_plain(minted.json(), dict[str, object])["ticket"], str)
    good = _browser_peer(world, cookie, terminal_id)
    task = asyncio.create_task(_run_browser(good, terminal_id))
    good.push(Frame.ATTACH, _attach(ticket))
    assert (await good.frame())[0] is Frame.ACK
    again = _browser_peer(world, cookie, terminal_id)
    assert await refused(again, _attach(ticket)) == wire_terminals.CLOSE_REFUSED
    good.leave()
    await task


async def test_the_host_socket_refuses_an_unknown_credential_a_stale_instance_and_a_second_claim(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie)
    await world.wait()
    terminal_id, _ = _opened(await world.open(cookie))

    async def code(peer: _Peer, instance: uuid.UUID = _INSTANCE) -> int:
        await terminal_routes.terminal_host_socket(
            peer.websocket,
            world.machine_id,
            terminal_id,
            instance,
        )
        return await peer.close_code()

    unknown = _host_peer(world, terminal_id, credential="trax_machine_x")
    stale = _host_peer(world, terminal_id)
    claimed = _host_peer(world, terminal_id)
    holder = _host_peer(world, terminal_id)
    task = asyncio.create_task(_run_host(holder, world, terminal_id))
    await asyncio.sleep(0.05)
    codes = [
        await code(unknown),
        await code(stale, uuid.uuid4()),
        await code(claimed),
    ]
    holder.leave()
    await task

    assert codes == [wire_terminals.CLOSE_REFUSED] * 3
    assert terminal_hub(app).terminal(terminal_id) is None


async def test_a_revoke_closes_both_legs_with_4410(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    bob = await world.person("bob@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie)
    await world.wait()
    terminal_id, ticket = _opened(await world.open(cookie))
    browser = _browser_peer(world, cookie, terminal_id)
    host = _host_peer(world, terminal_id)
    tasks = [
        asyncio.create_task(_run_browser(browser, terminal_id)),
        asyncio.create_task(_run_host(host, world, terminal_id)),
    ]
    browser.push(Frame.ATTACH, _attach(ticket))
    assert (await browser.frame())[0] is Frame.ACK

    path = REVOKE_PATH.format(name="gx10")
    assert (
        await world.call("POST", path, headers=world.browser(world.cookie(bob)))
    ).status_code == 403
    assert (
        await world.call("POST", path, headers=world.browser(cookie))
    ).status_code == 204
    codes = [await browser.close_code(), await host.close_code()]
    host.leave()
    browser.leave()
    await asyncio.gather(*tasks)

    assert codes == [wire_terminals.CLOSE_MACHINE_REVOKED] * 2
    revoked = _host_peer(world, terminal_id)
    await terminal_routes.terminal_host_socket(
        revoked.websocket,
        world.machine_id,
        terminal_id,
        _INSTANCE,
    )
    assert await revoked.close_code() == wire_terminals.CLOSE_MACHINE_REVOKED


async def test_attach_mints_a_ticket_for_the_opener_and_close_stops_the_shell(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    bob = await world.person("bob@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie)
    await world.wait()
    terminal_id, _ = _opened(await world.open(cookie))
    host = _host_peer(world, terminal_id)
    task = asyncio.create_task(_run_host(host, world, terminal_id))
    await asyncio.sleep(0.05)
    attach = wire_terminals.TERMINAL_ATTACH_PATH.format(terminal_id=terminal_id)
    close = wire_terminals.TERMINAL_CLOSE_PATH.format(terminal_id=terminal_id)

    stranger = await world.call(
        "POST",
        attach,
        headers=world.browser(world.cookie(bob)),
    )
    no_origin = await world.call(
        "POST",
        close,
        headers=world.browser(cookie, origin=None),
    )
    minted = await world.call("POST", attach, headers=world.browser(cookie))
    closed = await world.call("POST", close, headers=world.browser(cookie))

    assert (stranger.status_code, no_origin.status_code) == (404, 403)
    assert (
        len(from_plain(from_plain(minted.json(), dict[str, object])["ticket"], str))
        == 43
    )
    assert closed.status_code == 204
    assert await host.frame() == (Frame.STOP, b"")
    host.leave()
    await task


async def test_a_writer_enrolls_a_personal_machine_only_its_owner_sees(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    bob = await world.person("bob@x.y")
    boss = await world.person("boss@x.y", role="admin")
    await world.join(world.cookie(ada), name="laptop")

    seen = {
        name: from_plain(
            (
                await world.call(
                    "GET",
                    MACHINES_PATH,
                    headers={"Cookie": world.cookie(user)},
                )
            ).json(),
            dict[str, list[dict[str, object]]],
        )["machines"]
        for name, user in (("ada", ada), ("bob", bob), ("boss", boss))
    }

    assert [(m["name"], m["role"], m["owner"]) for m in seen["ada"]] == [
        ("laptop", "personal", "ada@x.y"),
    ]
    assert seen["bob"] == []
    assert seen["boss"] == []
    # An admin never lists it, but still takes it out of service by name.
    revoked = await world.call(
        "POST",
        REVOKE_PATH.format(name="laptop"),
        headers={"Cookie": world.cookie(boss)},
    )
    assert revoked.status_code == 204
    taken = await world.call(
        "POST",
        ENROLL_PATH,
        headers={"Cookie": world.cookie(bob)},
        body={"name": "laptop"},
    )
    assert taken.status_code == 403


async def test_a_token_joins_while_its_owner_is_active_and_not_after(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    enrolled = await world.call(
        "POST",
        ENROLL_PATH,
        headers={"Cookie": cookie},
        body={"name": "laptop"},
    )
    token = from_plain(enrolled.json(), dict[str, str])["token"]
    async with world.store.engine.acquire() as conn:
        await conn.execute("UPDATE users SET status = 'disabled'")

    refused = await world.call(
        "POST",
        JOIN_PATH,
        headers={},
        body={
            "name": "laptop",
            "token": token,
            "instance": str(_INSTANCE),
            "host_version": "t",
        },
    )

    assert refused.status_code == 401


async def test_a_run_opens_only_where_the_machine_facts_list_its_cli(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie, facts={"clis": ["claude"]})
    await world.wait()

    missing = await world.open(cookie, command="codex")
    assert (missing.status_code, _code(missing)) == (409, "cli_not_installed")
    _opened(await world.open(cookie, command="claude"))
    _opened(await world.open(cookie))


async def test_the_host_is_handed_each_run_with_its_session_and_its_name(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie, facts={"clis": ["claude", "codex"]})
    await world.wait()
    session_id = uuid.UUID("9f1c2e3a-0000-4000-8000-000000000001")

    _opened(await world.open(cookie, command="claude", attach=session_id))
    chat = from_plain((await world.wait()).json(), dict[str, object])
    _opened(await world.open(cookie, command="codex"))
    codex = from_plain((await world.wait()).json(), dict[str, object])
    _opened(await world.open(cookie, command="claude", run_name="nightly"))
    named = from_plain((await world.wait()).json(), dict[str, object])
    _opened(await world.open(cookie))
    shell = from_plain((await world.wait()).json(), dict[str, object])

    assert (chat["command"], chat["attach"], chat["run_name"]) == (
        "claude",
        str(session_id),
        "chat-9f1c2e3a",
    )
    assert (codex["command"], codex.get("attach")) == ("codex", None)
    assert from_plain(codex["run_name"], str).startswith("run-")
    assert len(from_plain(codex["run_name"], str)) == len("run-") + 8
    assert (named["command"], named["run_name"]) == ("claude", "nightly")
    # A shell's wait names none of a run's fields, so a host on an older wire, which
    # refuses a key it does not know, opens it.
    assert not {"command", "attach", "run_name", "cwd"} & set(shell)


async def test_a_session_has_one_run_open_at_a_time(world: _World) -> None:
    """A run opened on a session another run is attached to is refused, until it ends."""
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie, facts={"clis": ["claude", "codex"]})
    await world.wait()
    session_id = uuid.uuid4()

    first, _ = _opened(await world.open(cookie, command="claude", attach=session_id))
    refused = await world.open(cookie, command="codex", attach=session_id)
    _opened(await world.open(cookie, command="codex", attach=uuid.uuid4()))
    hub = terminal_hub(app)
    term = hub.terminal(first)
    assert term is not None
    await hub.stop(term)

    assert (refused.status_code, _code(refused)) == (409, "chat_run_live")
    _opened(await world.open(cookie, command="codex", attach=session_id))


async def test_a_run_a_trax_started_by_hand_holds_its_session_against_a_second(
    world: _World,
) -> None:
    """The lease its drain takes counts: the browser may not start another beside it."""
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie, facts={"clis": ["claude"]})
    await world.wait()
    session_id = uuid.uuid4()
    inbound = cast("InboundQueue", app.state.inbound)

    # The assistant's lease names no CLI: it is not a run of the chat.
    inbound.mark_poller(session_id)
    _opened(await world.open(cookie, command="claude", attach=session_id))
    inbound.mark_poller(session_id, cli="claude", instance="a" * 32)
    refused = await world.open(cookie, command="claude", attach=session_id)
    inbound.forget_poller(session_id)

    assert (refused.status_code, _code(refused)) == (409, "chat_run_live")


async def test_the_host_is_handed_the_folder_each_run_starts_in_as_named(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie, facts={"clis": ["claude"]})
    await world.wait()

    refused = await world.open(cookie, cwd="~/project")
    assert refused.status_code == 422
    _opened(await world.open(cookie, command="claude", cwd="~/project"))
    run = from_plain((await world.wait()).json(), dict[str, object])
    _opened(await world.open(cookie, command="claude"))
    plain = from_plain((await world.wait()).json(), dict[str, object])

    assert (run["cwd"], plain.get("cwd")) == ("~/project", None)


async def test_a_machine_listing_keeps_the_folder_its_owner_named(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie, facts={"clis": ["claude"]})
    await world.wait()

    _opened(await world.open(cookie, command="claude", cwd="~/work"))
    listed = await world.call(
        "GET",
        wire_terminals.TERMINAL_MACHINES_PATH,
        headers=world.browser(cookie),
    )

    machines = from_plain(
        from_plain(listed.json(), dict[str, object])["machines"],
        list[dict[str, object]],
    )
    assert machines[0]["cwd"] == "~/work"


async def test_the_machine_list_names_the_clis_its_facts_list(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie, facts={"clis": ["claude", "git"]})

    listed = from_plain(
        (
            await world.call(
                "GET",
                wire_terminals.TERMINAL_MACHINES_PATH,
                headers=world.browser(cookie),
            )
        ).json(),
        dict[str, object],
    )

    machines = from_plain(listed["machines"], list[dict[str, object]])
    assert machines[0]["clis"] == ["claude", "git"]


async def test_the_servers_stun_list_reaches_the_open_the_attach_and_the_host_wait(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie)
    await world.wait()
    monkeypatch.setattr(terminal_hub(app), "ice_servers", ("stun:s.test:3478",))

    opened = await world.open(cookie)
    offered = await world.wait()
    attach = wire_terminals.TERMINAL_ATTACH_PATH.format(
        terminal_id=_opened(opened)[0],
    )
    minted = await world.call("POST", attach, headers=world.browser(cookie))

    for response in (opened, offered, minted):
        assert from_plain(response.json(), dict[str, object])["ice_servers"] == [
            "stun:s.test:3478",
        ]


_OLD_RUN = "a" * 32


class _Chat:
    """A person's chat on claude: its session, its station, and the run that drained it."""

    def __init__(
        self,
        session: uuid.UUID,
        station: uuid.UUID,
        conversation: uuid.UUID,
    ) -> None:
        self.session = session
        self.station = station
        self.conversation = conversation


async def _chat(
    world: _World,
    user_id: uuid.UUID,
    email: str,
    *,
    provider: str = "claude",
) -> _Chat:
    """Give a person a station on ``provider`` holding a chat their key opened."""
    async with world.store.engine.acquire() as conn:
        key_id, _, _, _ = await create_api_key(
            conn,
            user_id=user_id,
            name="run",
            ceiling="writer",
        )
    conversation = uuid.uuid4()
    session = await seed_session(
        world.store,
        Agent(key_id=key_id, email=email),
        actor=chat_actor(conversation),
        cli=provider,
        cli_session_id=chat_session_id(conversation),
        labels=[SCIENCE_CHAT_LABEL],
        polled=False,
    )
    station = uuid.uuid4()
    async with world.store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO visual_workspaces (id, user_id, station, state) "
            "VALUES ($1, $2, $3, $4)",
            station,
            user_id,
            {"name": f"s-{station.hex[:6]}", "conversation_id": str(conversation)},
            {"visuals": [], "provider": provider},
        )
    return _Chat(session, station, conversation)


def _taken_over(chat: _Chat) -> None:
    """Make the chat's run, working in ``~/work`` on gx10, report a takeover."""
    inbound = cast("InboundQueue", app.state.inbound)
    inbound.mark_poller(
        chat.session,
        run="chat-aaaaaaaa",
        machine="gx10",
        cwd="~/work",
        cli="claude",
        instance=_OLD_RUN,
    )
    assert inbound.report_state(chat.session, instance=_OLD_RUN, state="taken_over")


async def _reattach(
    world: _World,
    cookie: str,
    chat: _Chat,
    *,
    key: uuid.UUID | None = None,
    origin: str | None = _ORIGIN,
) -> httpx2.Response:
    """Ask the server to reopen the chat's run headless, as the browser does."""
    return await world.call(
        "POST",
        wire_sessions.session_reattach_path(chat.session),
        headers={
            **world.browser(cookie, origin=origin),
            "Idempotency-Key": str(key or uuid.uuid4()),
        },
    )


async def test_a_chat_taken_over_is_reopened_headless_where_it_ran(
    world: _World,
) -> None:
    """The host is offered the run in the same folder; the taken-over run is turned away."""
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie, facts={"clis": ["claude"]})
    await world.wait()
    chat = await _chat(world, ada, "ada@x.y")
    _taken_over(chat)
    inbound = cast("InboundQueue", app.state.inbound)
    hub = terminal_hub(app)
    held = hub.open(
        Opening(
            user="ada@x.y",
            machine_id=world.machine_id,
            machine="gx10",
            rows=24,
            cols=80,
            command="claude",
            attach=chat.session,
        ),
    )
    _ = await world.wait()
    key = uuid.uuid4()

    reattached = await _reattach(world, cookie, chat, key=key)
    again = await _reattach(world, cookie, chat, key=key)

    assert (reattached.status_code, again.status_code) == (204, 204)
    offered = from_plain((await world.wait()).json(), dict[str, object])
    assert (offered["command"], offered["attach"], offered["cwd"]) == (
        "claude",
        str(chat.session),
        "~/work",
    )
    assert (offered["opener"], offered["workspace"]) == ("ada@x.y", str(chat.station))
    # The taken-over run's own name: a launch of it ends that host and resumes the
    # CLI session its terminal continued.
    assert offered["run_name"] == "chat-aaaaaaaa"
    # The retry under the same key reopened nothing.
    assert (await world.wait()).json() is None
    assert held.stopping
    assert inbound.poller_run(chat.session) is None
    assert inbound.held_by_another(chat.session, cli="claude", instance=_OLD_RUN)


async def test_a_chat_whose_run_is_gone_is_reopened_where_it_last_ran(
    world: _World,
) -> None:
    """The lease is gone with the run, but the place it named is kept."""
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie, facts={"clis": ["claude"]})
    await world.wait()
    chat = await _chat(world, ada, "ada@x.y")
    inbound = cast("InboundQueue", app.state.inbound)
    inbound.mark_poller(chat.session, machine="gx10", cwd="/srv/app", cli="claude")
    inbound.forget_poller(chat.session)

    reattached = await _reattach(world, cookie, chat)

    assert reattached.status_code == 204, reattached.text
    offered = from_plain((await world.wait()).json(), dict[str, object])
    assert (offered["command"], offered["cwd"]) == ("claude", "/srv/app")
    # A run that named no host gets the name the server gives a chat's run.
    assert offered["run_name"] == f"chat-{chat.session.hex[:8]}"


async def test_a_live_run_is_not_reattached(world: _World) -> None:
    """Reattach is for a chat no headless run holds: a live one keeps its lease."""
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie, facts={"clis": ["claude"]})
    await world.wait()
    chat = await _chat(world, ada, "ada@x.y")
    _taken_over(chat)
    inbound = cast("InboundQueue", app.state.inbound)
    assert inbound.report_state(chat.session, instance=_OLD_RUN, state="running")

    refused = await _reattach(world, cookie, chat)

    assert (refused.status_code, _code(refused)) == (409, "chat_run_live")
    assert (await world.wait()).json() is None
    held = inbound.poller_run(chat.session)
    assert held is not None
    assert held.instance == _OLD_RUN
    assert not inbound.held_by_another(chat.session, cli="claude", instance=_OLD_RUN)


async def test_a_reattach_past_the_open_rate_leaves_the_taken_over_run_alone(
    world: _World,
) -> None:
    """No new run can start, so the terminal that took the chat over is not ended."""
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie, facts={"clis": ["claude"]})
    await world.wait()
    chat = await _chat(world, ada, "ada@x.y")
    _taken_over(chat)
    inbound = cast("InboundQueue", app.state.inbound)
    hub = terminal_hub(app)
    held = [
        hub.open(
            Opening(
                user="ada@x.y",
                machine_id=world.machine_id,
                machine="gx10",
                rows=24,
                cols=80,
                command="claude",
                attach=chat.session,
            ),
        )
        for _ in range(wire_terminals.MAX_OPENS_PER_MIN_PER_USER)
    ]

    refused = await _reattach(world, cookie, chat)

    assert (refused.status_code, _code(refused)) == (429, "terminal_limit")
    assert not any(each.stopping for each in held)
    still = inbound.poller_run(chat.session)
    assert still is not None
    assert (still.instance, still.state) == (_OLD_RUN, "taken_over")
    assert not inbound.held_by_another(chat.session, cli="claude", instance=_OLD_RUN)


async def test_only_the_starter_reattaches_a_chat_from_their_own_browser(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One organisation reads every chat, so bob reads it and is refused as a non-starter.
    monkeypatch.setattr(
        app.state,
        "config",
        Config(
            session_secret=_SESSION_KEY,
            session_max_age_seconds=600,
            chat_orgs="single",
        ),
        raising=False,
    )
    ada = await world.person("ada@x.y")
    bob = await world.person("bob@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie, facts={"clis": ["claude"]})
    await world.wait()
    chat = await _chat(world, ada, "ada@x.y")
    _taken_over(chat)

    by_bob = await _reattach(world, world.cookie(bob), chat)
    no_origin = await _reattach(world, cookie, chat, origin=None)
    other_site = await _reattach(world, cookie, chat, origin="https://evil.test")
    unknown = await world.call(
        "POST",
        wire_sessions.session_reattach_path(uuid.uuid4()),
        headers={**world.browser(cookie), "Idempotency-Key": str(uuid.uuid4())},
    )

    assert (by_bob.status_code, _code(by_bob)) == (403, "reattach_not_owner")
    assert (no_origin.status_code, other_site.status_code) == (403, 403)
    assert unknown.status_code == 404
    assert (await world.wait()).json() is None
    inbound = cast("InboundQueue", app.state.inbound)
    assert inbound.poller_run(chat.session) is not None


async def test_a_session_that_is_no_chat_and_a_chat_of_the_assistant_are_refused(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie, facts={"clis": ["claude"]})
    await world.wait()
    assistants = await _chat(world, ada, "ada@x.y", provider="assistant")
    async with world.store.engine.acquire() as conn:
        key_id, _, _, _ = await create_api_key(
            conn,
            user_id=ada,
            name="plain",
            ceiling="writer",
        )
    plain = await seed_session(
        world.store,
        Agent(key_id=key_id, email="ada@x.y"),
        actor="plain-run",
        cli="claude",
        polled=False,
    )

    on_assistant = await _reattach(world, cookie, assistants)
    on_plain = await world.call(
        "POST",
        wire_sessions.session_reattach_path(plain),
        headers={**world.browser(cookie), "Idempotency-Key": str(uuid.uuid4())},
    )

    assert (on_assistant.status_code, _code(on_assistant)) == (
        409,
        "provider_is_assistant",
    )
    assert (on_plain.status_code, _code(on_plain)) == (409, "not_a_chat")
    assert (await world.wait()).json() is None


async def test_a_chat_that_never_named_where_it_ran_is_not_guessed_at(
    world: _World,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie, facts={"clis": ["claude"]})
    await world.wait()
    chat = await _chat(world, ada, "ada@x.y")
    inbound = cast("InboundQueue", app.state.inbound)
    inbound.mark_poller(chat.session, cli="claude")
    inbound.forget_poller(chat.session)

    refused = await _reattach(world, cookie, chat)

    assert (refused.status_code, _code(refused)) == (409, "reattach_place_unknown")
    assert (await world.wait()).json() is None


async def test_the_machine_must_still_list_the_cli_and_be_online(world: _World) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie, facts={"clis": ["codex"]})
    await world.wait()
    chat = await _chat(world, ada, "ada@x.y")
    _taken_over(chat)
    gone = await _chat(world, ada, "ada@x.y")
    inbound = cast("InboundQueue", app.state.inbound)
    inbound.mark_poller(gone.session, machine="elsewhere", cwd="~/w", cli="claude")

    no_cli = await _reattach(world, cookie, chat)
    no_machine = await _reattach(world, cookie, gone)

    assert (no_cli.status_code, _code(no_cli)) == (409, "cli_not_installed")
    assert no_machine.status_code == 404
    assert (await world.wait()).json() is None
    # A refused reattach leaves the run as it was: nothing was retired.
    assert not inbound.held_by_another(chat.session, cli="claude", instance=_OLD_RUN)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
