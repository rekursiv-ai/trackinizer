"""The terminal routes end to end over PGlite, with the real sign-in and credentials.

The HTTP routes are driven through the whole app. A WebSocket route is called as the
server calls it, with a Starlette ``WebSocket`` over a scripted ASGI channel, since the
test client cannot share the session event loop that owns the database.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import asyncio
import uuid

from starlette.websockets import WebSocket, WebSocketDisconnect
from treekle import from_plain

import pytest

from trackinizer.lib.posix.host import Frame, decode_frame, encode_frame, encode_winsize
from trackinizer.server.api import terminal_routes
from trackinizer.server.api._deps import machine_changes, terminal_hub
from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import (
    TERMINAL_HOST,
    TERMINAL_INSTANCE,
    TERMINAL_ORIGIN,
    TERMINAL_SESSION_KEY,
    TerminalWorld,
    refusal_detail_code,
)
from trackinizer.server.config import Assistant, Config
from trackinizer.wire import wire_terminals
from trackinizer.wire.wire_machine_host import (
    ENROLL_PATH,
    JOIN_PATH,
    REVOKE_PATH,
)
from trackinizer.wire.wire_machines import MACHINES_PATH
from trackinizer.wire.wire_terminals import MachinesVersion


if TYPE_CHECKING:
    from collections.abc import Mapping

    from starlette.types import Message

    import httpx2

    from trackinizer.server.inbound import InboundQueue


pytestmark = [
    pytest.mark.db_pglite,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.usefixtures("cheap_scrypt"),
]


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
                    for key, value in {"host": TERMINAL_HOST, **headers}.items()
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


def _browser_peer(world: TerminalWorld, cookie: str, terminal_id: uuid.UUID) -> _Peer:
    return _Peer(
        wire_terminals.TERMINAL_WS_PATH.format(terminal_id=terminal_id),
        world.browser(cookie),
    )


def _host_peer(
    world: TerminalWorld,
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


async def _run_browser(peer: _Peer, terminal_id: uuid.UUID) -> None:
    await terminal_routes.terminal_web_socket(peer.websocket, terminal_id)


async def _run_host(peer: _Peer, world: TerminalWorld, terminal_id: uuid.UUID) -> None:
    await terminal_routes.terminal_host_socket(
        peer.websocket,
        world.machine_id,
        terminal_id,
        TERMINAL_INSTANCE,
    )


async def test_an_owner_opens_a_terminal_and_bytes_flow_both_ways_until_exit(
    world: TerminalWorld,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie)
    await world.wait()
    placed = await world.canvas(ada)

    terminal_id, ticket = _opened(
        await world.open(cookie, canvas=placed[0], requested_by="helper-1"),
    )
    offered = from_plain((await world.wait()).json(), dict[str, object])
    assert offered["requested_by"] == "helper-1"
    assert offered["terminal_id"] == str(terminal_id)
    assert (offered["opener"], offered["rows"], offered["cols"]) == ("ada@x.y", 24, 80)
    assert offered["canvas"] == str(placed[0])
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


async def _stored(world: TerminalWorld) -> list[set[str]]:
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
    world: TerminalWorld,
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
    world: TerminalWorld,
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
    world: TerminalWorld,
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


async def test_startup_negotiation_retries_without_losing_browser_input(
    world: TerminalWorld,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie)
    await world.wait()
    terminal_id, ticket = _opened(await world.open(cookie))
    browser = _browser_peer(world, cookie, terminal_id)
    browser_task = asyncio.create_task(_run_browser(browser, terminal_id))
    browser.push(Frame.ATTACH, _attach(ticket))
    assert (await browser.frame())[0] is Frame.ACK
    browser.push(Frame.INPUT, b"continue\n")

    async def connect(peer: _Peer) -> None:
        await terminal_routes.terminal_host_socket(
            peer.websocket,
            world.machine_id,
            terminal_id,
            TERMINAL_INSTANCE,
            ready=True,
        )

    first = _host_peer(world, terminal_id)
    first_task = asyncio.create_task(connect(first))
    first.push(Frame.ACK)
    assert await first.frame() == (Frame.ACK, b"")
    first.leave()
    await first_task
    assert terminal_hub(app).terminal(terminal_id) is not None

    second = _host_peer(world, terminal_id)
    second_task = asyncio.create_task(connect(second))
    try:
        second.push(Frame.ACK)
        assert await second.frame() == (Frame.ACK, b"")
        second.push(Frame.ACK)
        assert await second.frame() == (Frame.INPUT, b"continue\n")
        second.push(Frame.EXIT, (0).to_bytes(4))
        assert await browser.frame() == (Frame.EXIT, (0).to_bytes(4))
    finally:
        second.leave()
        browser.leave()
        await asyncio.gather(browser_task, second_task)


async def test_a_browser_that_reconnects_with_an_offset_gets_only_what_it_missed(
    world: TerminalWorld,
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


async def test_the_listing_names_every_live_terminal_of_the_canvas_with_its_surface(
    world: TerminalWorld,
) -> None:
    ada = await world.person("ada@x.y")
    bob = await world.person("bob@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie)
    canvas, _ = await world.canvas(ada)
    path = f"{wire_terminals.TERMINAL_MACHINES_PATH}?canvas={canvas}"

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
            "runs": [],
            "host_outdated": False,
            "files": False,
            "tools": False,
        },
    ]
    not_asked = await world.open(cookie, canvas=canvas)
    assert (not_asked.status_code, refusal_detail_code(not_asked)) == (
        409,
        "terminal_not_enabled",
    )

    await world.wait()
    first, _ = _opened(
        await world.open(cookie, canvas=canvas, surface="chat"),
    )
    second, _ = _opened(
        await world.open(cookie, canvas=canvas, surface="tile-1"),
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
    world: TerminalWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        app.state._state,
        "config",
        Config(
            session_secret=TERMINAL_SESSION_KEY,
            session_max_age_seconds=600,
            assistant=Assistant(actor="kb", email="kb@x.y"),
        ),
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
    canvas, _ = await world.canvas(ada)
    path = wire_terminals.TERMINAL_MACHINES_PATH

    def owned(email: str) -> str:
        return f"{path}?owner={email}&canvas={canvas}"

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
                "runs": [],
                "host_outdated": False,
                "files": False,
                "tools": False,
            },
            {
                "name": "laptop",
                "status": "online",
                "terminal_ready": False,
                "confirmed": True,
                "clis": [],
                "runs": [],
                "host_outdated": False,
                "files": False,
                "tools": False,
            },
        ],
        "terminals": [],
    }
    assert from_plain(for_bob.json(), dict[str, object])["machines"] == []
    refused = (for_viewer, as_itself, ada_names_herself, bob_names_ada)
    assert [each.status_code for each in refused] == [403] * 4
    assert {refusal_detail_code(each) for each in refused} == {"terminal_forbidden"}


async def test_the_assistant_waits_on_the_listing_of_the_person_it_answers(
    world: TerminalWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        app.state._state,
        "config",
        Config(
            session_secret=TERMINAL_SESSION_KEY,
            session_max_age_seconds=600,
            assistant=Assistant(actor="kb", email="kb@x.y"),
        ),
    )
    ada = await world.person("ada@x.y")
    bob = await world.person("bob@x.y")
    _ = await world.person("vic@x.y", role="viewer")
    assistant = {
        "Authorization": f"Bearer {await world.key(await world.person('kb@x.y'))}",
    }
    path = wire_terminals.TERMINAL_MACHINES_CHANGES_PATH

    async def version(owner: str, query: str = "") -> int:
        answer = await world.call(
            "GET",
            f"{path}?owner={owner}{query}",
            headers=assistant,
        )
        assert answer.status_code == 200, answer.text
        return MachinesVersion.model_validate(answer.json()).version

    now = await version("ada@x.y")
    bob_now = await version("bob@x.y")
    held = asyncio.ensure_future(version("ada@x.y", f"&since={now}"))
    await asyncio.sleep(0.05)
    assert not held.done()
    await world.join(world.cookie(ada), name="laptop")
    async with asyncio.timeout(5):
        woken = await held
    # A join writes the machine more than once; the first write woke the wait.
    joined = await version("ada@x.y")
    unchanged = await version("ada@x.y", f"&since={joined}&wait_sec=0.01")
    for_bob = await version("bob@x.y", f"&since={bob_now}&wait_sec=0.01")
    refused = [
        await world.call("GET", f"{path}?owner=vic@x.y", headers=assistant),
        await world.call("GET", path, headers=assistant),
        await world.call(
            "GET",
            f"{path}?owner=ada@x.y",
            headers=world.browser(world.cookie(bob)),
        ),
    ]
    own = await world.call("GET", path, headers=world.browser(world.cookie(ada)))

    assert woken != now
    assert unchanged == joined
    assert for_bob == bob_now
    assert [each.status_code for each in refused] == [403] * 3
    assert {refusal_detail_code(each) for each in refused} == {"terminal_forbidden"}
    assert MachinesVersion.model_validate(own.json()).version == joined


async def test_a_box_the_assistant_enrolls_for_a_person_opens_a_terminal_for_that_person_alone(
    world: TerminalWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        app.state._state,
        "config",
        Config(
            session_secret=TERMINAL_SESSION_KEY,
            session_max_age_seconds=600,
            assistant=Assistant(actor="kb", email="kb@x.y"),
        ),
    )
    ada = await world.person("ada@x.y")
    bob = await world.person("bob@x.y")
    assistant = {
        "Authorization": f"Bearer {await world.key(await world.person('kb@x.y'))}",
    }
    enrolled = await world.call(
        "POST",
        ENROLL_PATH,
        headers=assistant,
        body={"name": "box-1a2b3c4d", "asker": "ada@x.y"},
    )
    joined = await world.call(
        "POST",
        JOIN_PATH,
        headers={},
        body={
            "name": "box-1a2b3c4d",
            "token": from_plain(enrolled.json(), dict[str, str])["token"],
            "instance": str(TERMINAL_INSTANCE),
            "host_version": "t",
            "facts": {"terminal": True},
        },
    )
    world.machine_id = uuid.UUID(
        from_plain(joined.json(), dict[str, str])["machine_id"],
    )
    world.credential = from_plain(joined.json(), dict[str, str])["credential"]
    await world.wait()
    listing = wire_terminals.TERMINAL_MACHINES_PATH
    ada_sees = await world.call(
        "GET",
        listing,
        headers=world.browser(world.cookie(ada)),
    )
    bob_sees = await world.call(
        "GET",
        listing,
        headers=world.browser(world.cookie(bob)),
    )
    ada_opens = await world.open(world.cookie(ada), machine="box-1a2b3c4d")
    bob_opens = await world.open(world.cookie(bob), machine="box-1a2b3c4d")

    assert enrolled.status_code == 201, enrolled.text
    assert from_plain(enrolled.json(), dict[str, str])["owner"] == "ada@x.y"
    assert joined.status_code == 201, joined.text
    assert [
        (each["name"], each["terminal_ready"], each["confirmed"])
        for each in from_plain(ada_sees.json(), dict[str, list[dict[str, object]]])[
            "machines"
        ]
    ] == [("box-1a2b3c4d", True, True)]
    assert from_plain(bob_sees.json(), dict[str, object])["machines"] == []
    assert ada_opens.status_code == 201, ada_opens.text
    assert bob_opens.status_code == 404


async def test_the_assistant_opens_only_a_detached_run_on_its_askers_machine(
    world: TerminalWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        app.state._state,
        "config",
        Config(
            session_secret=TERMINAL_SESSION_KEY,
            session_max_age_seconds=600,
            assistant=Assistant(actor="kb", email="kb@x.y"),
        ),
    )
    ada = await world.person("ada@x.y")
    assistant = await world.person("kb@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie, facts={"clis": ["claude"]})
    await world.wait()
    canvas, _ = await world.canvas(ada)
    session = uuid.uuid4()
    body = {
        "machine": "gx10",
        "rows": 40,
        "cols": 120,
        "canvas": str(canvas),
        "command": "claude",
        "attach": str(session),
        "run_name": "campaign-issue-7",
        "cwd": "~/work",
    }
    headers = {"Authorization": f"Bearer {await world.key(assistant)}"}
    before = machine_changes(app).version("ada@x.y")

    opened = await world.call(
        "POST",
        f"{wire_terminals.TERMINALS_PATH}?asker=ada@x.y",
        headers=headers,
        body=body,
    )

    _opened(opened)
    # The folder a run names is the listing's ``cwd``.
    assert machine_changes(app).version("ada@x.y") == before + 1
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


async def test_the_assistant_cannot_open_a_shell_or_another_askers_machine(
    world: TerminalWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        app.state._state,
        "config",
        Config(
            session_secret=TERMINAL_SESSION_KEY,
            session_max_age_seconds=600,
            assistant=Assistant(actor="kb", email="kb@x.y"),
        ),
    )
    ada = await world.person("ada@x.y")
    bob = await world.person("bob@x.y")
    _ = await world.person("vic@x.y", role="viewer")
    assistant = await world.person("kb@x.y")
    await world.join(world.cookie(bob), facts={"clis": ["claude"]})
    await world.wait()
    canvas, _ = await world.canvas(ada)
    body = {
        "machine": "gx10",
        "rows": 40,
        "cols": 120,
        "canvas": str(canvas),
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
        (each.status_code, refusal_detail_code(each))
        for each in (viewer, shell, no_asker, bad_run)
    ] == [
        (403, "terminal_forbidden"),
    ] * 4


async def test_an_open_is_refused_unless_a_person_who_owns_an_online_enabled_machine(
    world: TerminalWorld,
) -> None:
    ada = await world.person("ada@x.y")
    bob = await world.person("bob@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie)

    assert (await world.open(cookie)).status_code == 409
    await world.wait()
    assert (await world.open(world.cookie(bob))).status_code == 404
    assert (await world.open(cookie, machine="nope")).status_code == 404
    # A cookie's write without this site's Origin is refused before the route runs.
    assert (await world.open(cookie, origin=None)).status_code == 403
    assert (await world.open(cookie, origin="https://evil.test")).status_code == 403
    bad = await world.open(cookie, surface="has space")
    assert bad.status_code == 422
    key = await world.key(ada)
    keyed = await world.call(
        "POST",
        wire_terminals.TERMINALS_PATH,
        headers={"Authorization": f"Bearer {key}", "Origin": TERMINAL_ORIGIN},
        body={"machine": "gx10", "rows": 24, "cols": 80},
    )
    assert keyed.status_code == 403
    for _ in range(wire_terminals.MAX_OPENS_PER_MIN_PER_USER):
        _opened(await world.open(cookie))
    over = await world.open(cookie)
    assert (over.status_code, refusal_detail_code(over)) == (429, "terminal_limit")

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


async def _key_enrolled(world: TerminalWorld, key: str, name: str) -> uuid.UUID:
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
            "instance": str(TERMINAL_INSTANCE),
            "host_version": "t",
        },
    )
    assert joined.status_code == 201, joined.text
    body = from_plain(joined.json(), dict[str, str])
    world.machine_id = uuid.UUID(body["machine_id"])
    world.credential = body["credential"]
    return world.machine_id


async def test_a_machine_an_agent_enrolled_opens_no_terminal_until_its_owner_confirms(
    world: TerminalWorld,
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
        headers={"Authorization": f"Bearer {key}", "Origin": TERMINAL_ORIGIN},
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
    assert refusal_detail_code(unconfirmed) == "terminal_unconfirmed"
    assert [(m["name"], m["confirmed"]) for m in listed] == [
        ("evilbox", False),
        ("older", False),
    ]

    before = machine_changes(app).version("ada@x.y")
    mine = await world.call("POST", confirm, headers=world.browser(cookie))
    opened = await world.open(cookie, machine="evilbox")

    assert (mine.status_code, opened.status_code) == (204, 201)
    assert machine_changes(app).version("ada@x.y") == before + 1


async def test_every_terminal_route_and_both_sockets_refuse_a_server_without_auth(
    world: TerminalWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie)
    await world.wait()
    terminal_id, _ = _opened(await world.open(cookie))
    monkeypatch.setitem(
        app.state._state,
        "config",
        Config(auth_disabled=True),
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
        ("GET", wire_terminals.TERMINAL_MACHINES_CHANGES_PATH, None),
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

    assert answers == [403] * 6
    assert waited.status_code == 403
    assert await browser.close_code() == wire_terminals.CLOSE_REFUSED
    assert await host.close_code() == wire_terminals.CLOSE_REFUSED


async def test_the_sweep_ends_a_terminal_whose_owner_is_no_longer_a_writer(
    world: TerminalWorld,
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
    world: TerminalWorld,
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
    world: TerminalWorld,
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
        {"Authorization": f"Bearer {await world.key(ada)}", "Origin": TERMINAL_ORIGIN},
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
    world: TerminalWorld,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie)
    await world.wait()
    terminal_id, _ = _opened(await world.open(cookie))

    async def code(peer: _Peer, instance: uuid.UUID = TERMINAL_INSTANCE) -> int:
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
    world: TerminalWorld,
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
        TERMINAL_INSTANCE,
    )
    assert await revoked.close_code() == wire_terminals.CLOSE_MACHINE_REVOKED


async def test_attach_mints_a_ticket_for_the_opener_and_close_stops_the_shell(
    world: TerminalWorld,
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
    world: TerminalWorld,
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
        headers=world.browser(world.cookie(boss)),
    )
    assert revoked.status_code == 204
    taken = await world.call(
        "POST",
        ENROLL_PATH,
        headers=world.browser(world.cookie(bob)),
        body={"name": "laptop"},
    )
    assert taken.status_code == 403


async def test_a_token_joins_while_its_owner_is_active_and_not_after(
    world: TerminalWorld,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    enrolled = await world.call(
        "POST",
        ENROLL_PATH,
        headers=world.browser(cookie),
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
            "instance": str(TERMINAL_INSTANCE),
            "host_version": "t",
        },
    )

    assert refused.status_code == 401


async def test_a_run_opens_only_where_the_machine_facts_list_its_cli(
    world: TerminalWorld,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie, facts={"clis": ["claude"]})
    await world.wait()

    missing = await world.open(cookie, command="codex")
    assert (missing.status_code, refusal_detail_code(missing)) == (
        409,
        "cli_not_installed",
    )
    _opened(await world.open(cookie, command="claude"))
    _opened(await world.open(cookie))


async def test_files_open_only_where_the_facts_say_files_and_the_host_is_told(
    world: TerminalWorld,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie, name="old")
    await world.join(cookie, name="new", facts={"files": True})
    await world.wait()

    refused = await world.open(cookie, machine="old", kind="files")
    _opened(await world.open(cookie, machine="new", kind="files"))
    files = from_plain((await world.wait()).json(), dict[str, object])
    _opened(await world.open(cookie, machine="new"))
    shell = from_plain((await world.wait()).json(), dict[str, object])

    assert (refused.status_code, refusal_detail_code(refused)) == (
        409,
        "files_not_enabled",
    )
    assert files["kind"] == "files"
    # A shell's wait names no kind, so a host on an older wire, which refuses a key it
    # does not know, still opens it.
    assert "kind" not in shell


async def test_the_listing_says_which_machines_serve_tools(
    world: TerminalWorld,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie, name="served", facts={"tools": True})
    await world.join(cookie, name="bare")

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
    assert {m["name"]: m["tools"] for m in machines} == {"served": True, "bare": False}


async def test_files_open_from_no_canvas_and_never_for_a_key(
    world: TerminalWorld,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie, facts={"files": True, "clis": ["claude"]})
    await world.wait()
    canvas, _ = await world.canvas(ada)

    on_canvas = await world.open(cookie, canvas=canvas, kind="files")
    run = await world.open(cookie, command="claude", kind="files")
    keyed = await world.call(
        "POST",
        wire_terminals.TERMINALS_PATH,
        headers={
            "Authorization": f"Bearer {await world.key(ada)}",
            "Origin": TERMINAL_ORIGIN,
        },
        body={"machine": "gx10", "rows": 24, "cols": 80, "kind": "files"},
    )

    assert (on_canvas.status_code, run.status_code) == (422, 422)
    assert (keyed.status_code, refusal_detail_code(keyed)) == (
        403,
        "terminal_forbidden",
    )


async def test_the_host_is_handed_each_run_with_its_session_and_its_name(
    world: TerminalWorld,
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


async def test_a_session_has_one_run_open_at_a_time(world: TerminalWorld) -> None:
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

    assert (refused.status_code, refusal_detail_code(refused)) == (409, "chat_run_live")
    _opened(await world.open(cookie, command="codex", attach=session_id))


async def test_a_run_a_trax_started_by_hand_holds_its_session_against_a_second(
    world: TerminalWorld,
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

    assert (refused.status_code, refusal_detail_code(refused)) == (409, "chat_run_live")


async def test_the_host_is_handed_the_folder_each_run_starts_in_as_named(
    world: TerminalWorld,
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
    world: TerminalWorld,
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
    world: TerminalWorld,
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


async def test_the_machine_list_says_which_hosts_serve_files(
    world: TerminalWorld,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie, name="old")
    await world.join(cookie, name="new", facts={"files": True})

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

    assert {
        from_plain(each["name"], str): each["files"]
        for each in from_plain(listed["machines"], list[dict[str, object]])
    } == {"old": False, "new": True}


async def test_the_machine_list_names_a_host_older_than_the_wire_supports(
    world: TerminalWorld,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie, name="old", host_version="0.1.8")
    await world.join(cookie, name="new", host_version="0.1.10")
    await world.join(cookie, name="source", host_version="")

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

    assert {
        from_plain(each["name"], str): each["host_outdated"]
        for each in from_plain(listed["machines"], list[dict[str, object]])
    } == {"old": True, "new": False, "source": False}


async def test_the_servers_stun_list_reaches_the_open_the_attach_and_the_host_wait(
    world: TerminalWorld,
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


async def test_a_retried_open_of_a_terminal_that_ended_is_gone(
    world: TerminalWorld,
) -> None:
    """The retry gets the first try's terminal back, and that one has ended."""
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie)
    await world.wait()
    headers = {**world.browser(cookie), "Idempotency-Key": str(uuid.uuid4())}
    body = {"machine": "gx10", "rows": 24, "cols": 80}
    terminal_id, _ticket = _opened(
        await world.call(
            "POST",
            wire_terminals.TERMINALS_PATH,
            headers=headers,
            body=body,
        ),
    )
    closed = await world.call(
        "POST",
        wire_terminals.TERMINAL_CLOSE_PATH.format(terminal_id=terminal_id),
        headers=world.browser(cookie),
    )

    retried = await world.call(
        "POST",
        wire_terminals.TERMINALS_PATH,
        headers=headers,
        body=body,
    )

    assert closed.status_code == 204
    assert (retried.status_code, retried.json()) == (
        410,
        {"detail": "The terminal opened by this request has ended"},
    )


async def test_a_browser_socket_with_no_valid_sign_in_is_refused(
    world: TerminalWorld,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie)
    await world.wait()
    terminal_id, ticket = _opened(await world.open(cookie))
    browser = _Peer(
        wire_terminals.TERMINAL_WS_PATH.format(terminal_id=terminal_id),
        world.browser("trax_session=forged"),
    )
    browser.push(Frame.ATTACH, _attach(ticket))

    await _run_browser(browser, terminal_id)

    assert await browser.close_code() == wire_terminals.CLOSE_REFUSED
    assert terminal_hub(app).terminal(terminal_id) is not None


async def test_a_browser_that_breaks_a_frame_rule_is_closed(
    world: TerminalWorld,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie)
    await world.wait()
    terminal_id, ticket = _opened(await world.open(cookie))
    browser, host, tasks = await _connected(world, cookie, terminal_id, ticket)

    browser.push(Frame.RESIZE, b"not a winsize")
    closed = await browser.close_code()
    browser.leave()
    await tasks[0]
    host.leave()
    await tasks[1]

    assert closed == wire_terminals.CLOSE_REFUSED


async def test_a_browser_taken_over_by_another_reads_no_more_of_its_own(
    world: TerminalWorld,
) -> None:
    """Its next frame finds another browser holds the terminal, and it closes."""
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie)
    await world.wait()
    terminal_id, ticket = _opened(await world.open(cookie))
    first, host, tasks = await _connected(world, cookie, terminal_id, ticket)
    minted = await world.call(
        "POST",
        wire_terminals.TERMINAL_ATTACH_PATH.format(terminal_id=terminal_id),
        headers=world.browser(cookie),
    )
    second = _browser_peer(world, cookie, terminal_id)
    second_task = asyncio.create_task(_run_browser(second, terminal_id))
    second.push(
        Frame.ATTACH,
        _attach(
            from_plain(from_plain(minted.json(), dict[str, object])["ticket"], str),
        ),
    )
    assert (await second.frame())[0] is Frame.ACK
    assert await first.close_code() == wire_terminals.CLOSE_TAKEN_OVER

    first.push(Frame.INPUT, b"stale\n")
    await tasks[0]
    second.push(Frame.INPUT, b"live\n")

    typed = await host.frame()
    while typed[0] is Frame.ROUTE:
        typed = await host.frame()
    assert typed == (Frame.INPUT, b"live\n")
    second.leave()
    host.leave()
    await asyncio.gather(second_task, tasks[1])


async def _ready_host(
    peer: _Peer,
    world: TerminalWorld,
    terminal_id: uuid.UUID,
) -> None:
    """Run a host leg that acknowledges its start before it claims the terminal."""
    await terminal_routes.terminal_host_socket(
        peer.websocket,
        world.machine_id,
        terminal_id,
        TERMINAL_INSTANCE,
        ready=True,
    )


async def test_a_ready_host_whose_first_frame_is_no_ack_is_refused(
    world: TerminalWorld,
) -> None:
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie)
    await world.wait()
    terminal_id, _ticket = _opened(await world.open(cookie))
    host = _host_peer(world, terminal_id)
    host.push(Frame.OUTPUT, b"$ ")

    await _ready_host(host, world, terminal_id)

    assert await host.close_code() == wire_terminals.CLOSE_REFUSED
    term = terminal_hub(app).terminal(terminal_id)
    assert term is not None
    assert term.host is None


async def test_a_host_leg_taken_over_while_starting_relays_no_more(
    world: TerminalWorld,
) -> None:
    """A second ready leg takes the start; the first one's next frame ends its relay."""
    ada = await world.person("ada@x.y")
    cookie = world.cookie(ada)
    await world.join(cookie)
    await world.wait()
    terminal_id, ticket = _opened(await world.open(cookie))
    browser = _browser_peer(world, cookie, terminal_id)
    browser_task = asyncio.create_task(_run_browser(browser, terminal_id))
    browser.push(Frame.ATTACH, _attach(ticket))
    assert (await browser.frame())[0] is Frame.ACK
    first = _host_peer(world, terminal_id)
    first_task = asyncio.create_task(_ready_host(first, world, terminal_id))
    first.push(Frame.ACK)
    assert await first.frame() == (Frame.ACK, b"")
    second = _host_peer(world, terminal_id)
    second_task = asyncio.create_task(_ready_host(second, world, terminal_id))
    second.push(Frame.ACK)
    assert await second.frame() == (Frame.ACK, b"")
    assert await first.close_code() == wire_terminals.CLOSE_TAKEN_OVER

    first.push(Frame.OUTPUT, b"stale")
    await first_task
    second.push(Frame.ACK)
    second.push(Frame.OUTPUT, b"live")

    assert await browser.frame() == (Frame.OUTPUT, b"live")
    second.leave()
    browser.leave()
    await asyncio.gather(browser_task, second_task)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
