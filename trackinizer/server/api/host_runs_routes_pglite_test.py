"""The routes a host's owner sees and stops its agent processes with, over PGlite.

Each process is attached to a real session relay over a scripted socket, as the
process's own route attaches it, with a key minted for it as a start mints one. The
stop's delayed revoke waits on a stand-in sleep the test lets pass.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, override

import asyncio
import uuid

from treekle import json_loads

import pytest
import pytest_asyncio

from trackinizer.server.api._deps import Later
from trackinizer.server.api.conftest import (
    TEST_API_KEY_ID,
    TEST_USER_EMAIL,
    TEST_USER_ID,
    install_identity,
    make_test_identity,
    patch_state,
)
from trackinizer.server.api.session_test_support import (
    _always,
    act_as_user_agent,
    start_session,
)
from trackinizer.server.auth import create_api_key, process_key_name
from trackinizer.server.session_relay import SessionRelay
from trackinizer.server.store.session import RunPlace
from trackinizer.wire.wire_daemon import (
    ENROLL_PATH,
    HEARTBEAT_PATH,
    JOIN_PATH,
    EnrollResponse,
    JoinResponse,
)
from trackinizer.wire.wire_hosts import (
    HOST_DISCONNECT_PATH,
    HOST_LOGOUT_PATH,
    HOST_RUN_STOP_PATH,
    HOST_RUNS_PATH,
    HostLogout,
    HostRun,
    HostRuns,
)
from trackinizer.wire.wire_science_chat import chat_session_id
from trackinizer.wire.wire_session_relay import (
    AgentProcess,
    Hello,
    ProcessLine,
    ProcessSpec,
)
from trackinizer.wire.wire_terminals import (
    CLOSE_ENDED,
    CLOSE_HOST_REVOKED,
    TERMINAL_HOSTS_PATH,
    TerminalHosts,
)


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    import httpx2

    from trackinizer.server.store.core import Store


pytestmark = [
    pytest.mark.db_pglite,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.usefixtures("cheap_scrypt"),
]


_INSTANCE: Final = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_MINE: Final = uuid.UUID("11111111-1111-1111-1111-111111111111")
_THEIRS: Final = uuid.UUID("22222222-2222-2222-2222-222222222222")
_EVE: Final = "eve@example.com"
_EVE_ID: Final = uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
_MY_RUN: Final = HostRun(session_id=_MINE, cli="sagent")
"""What the owner's process on ``box`` is listed as."""


class _Minute:
    """A stand-in sleep: each wait ends once the test lets the time pass."""

    def __init__(self) -> None:
        self.asked: list[float] = []
        self.passed = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        self.asked.append(seconds)
        _ = await self.passed.wait()


class _Later(Later):
    """The app's delayed work, each task kept for the test to await."""

    def __init__(self, *, sleep: Callable[[float], Awaitable[None]]) -> None:
        super().__init__(sleep=sleep)
        self.ran: list[asyncio.Task[None]] = []

    @override
    def run(
        self,
        delay_sec: float,
        work: Callable[[], Awaitable[None]],
    ) -> asyncio.Task[None]:
        task = super().run(delay_sec, work)
        self.ran.append(task)
        return task


class _Socket:
    """An agent process's socket the test scripts: it sends nothing, and keeps its close."""

    def __init__(self) -> None:
        self._received: asyncio.Queue[str | bytes | int] = asyncio.Queue()
        self.closed: asyncio.Future[tuple[int, str]] = (
            asyncio.get_running_loop().create_future()
        )

    async def receive(self) -> str | bytes | int:
        return await self._received.get()

    async def send_text(self, text: str) -> None:
        del text

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if not self.closed.done():
            self.closed.set_result((code, reason))

    async def close_code(self) -> int:
        code, _ = await asyncio.wait_for(asyncio.shield(self.closed), 5)
        return code


class _Relay(SessionRelay):
    """The relay, keeping each socket the test has it serve."""

    def __init__(self) -> None:
        super().__init__()
        self.served: list[asyncio.Future[None]] = []


@pytest_asyncio.fixture(loop_scope="session", autouse=True)
async def _owner_account(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Seed the admin the fixture acts as, who owns the hosts it enrolls, and eve."""
    _, store = pglite_route_client
    async with store.engine.acquire() as conn:
        for user_id, email, role in (
            (TEST_USER_ID, TEST_USER_EMAIL, "admin"),
            (_EVE_ID, _EVE, "writer"),
        ):
            await conn.execute(
                "INSERT INTO users (id, email, name, role, status) "
                "VALUES ($1, $2, 'n', $3, 'active')",
                user_id,
                email,
                role,
            )
        await conn.execute(
            "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
            "VALUES ($1, $2, 'agent', 'test-hash', 'trax_agent', 'writer')",
            TEST_API_KEY_ID,
            TEST_USER_ID,
        )
    install_identity(make_test_identity(role="admin", api_key_id=None))


@pytest_asyncio.fixture(loop_scope="session")
async def relay(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Relay]:
    """Give the app a fresh relay, and end the sockets it still serves with the test."""
    started = _Relay()
    patch_state(monkeypatch, "session_relay", started)
    yield started
    for task in started.served:
        _ = task.cancel()
    _ = await asyncio.gather(*started.served, return_exceptions=True)


@pytest.fixture
def minute() -> _Minute:
    return _Minute()


@pytest.fixture
def later(monkeypatch: pytest.MonkeyPatch, minute: _Minute) -> _Later:
    kept = _Later(sleep=minute.sleep)
    patch_state(monkeypatch, "later", kept)
    return kept


async def test_a_process_on_a_host_is_listed_to_its_owner_and_no_other_account(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    relay: _Relay,
) -> None:
    client, store = pglite_route_client
    box, _ = await _joined(client, "box")
    _ = await _attached(store, relay, host_id=box, session_id=_MINE)
    await _evebox(store)

    listed = await client.get(HOST_RUNS_PATH.format(name="box"))
    hosts = await client.get(TERMINAL_HOSTS_PATH)
    install_identity(make_test_identity(user_id=_EVE_ID, email=_EVE, api_key_id=None))
    theirs = await client.get(HOST_RUNS_PATH.format(name="box"))
    eves = await client.get(HOST_RUNS_PATH.format(name="evebox"))
    eves_hosts = await client.get(TERMINAL_HOSTS_PATH)

    assert listed.status_code == 200, listed.text
    assert HostRuns.model_validate_json(listed.text) == HostRuns(runs=[_MY_RUN])
    (host,) = TerminalHosts.model_validate_json(hosts.text).hosts
    assert (host.name, host.runs) == ("box", [_MY_RUN])
    assert theirs.status_code == 404
    assert HostRuns.model_validate_json(eves.text) == HostRuns(runs=[])
    (eve_host,) = TerminalHosts.model_validate_json(eves_hosts.text).hosts
    assert (eve_host.name, eve_host.runs) == ("evebox", [])


async def test_each_run_names_its_agentsession_and_the_chat_it_is_a_session_of(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    relay: _Relay,
) -> None:
    client, store = pglite_route_client
    box, _ = await _joined(client, "box")
    act_as_user_agent("writer")
    chat = await start_session(client, cli="sagent")
    alone = await start_session(client, cli="sagent")
    theirs = await start_session(client, cli="sagent")
    conversation = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET agentsession_cli_session_id = $1 WHERE id = $2",
            chat_session_id(conversation),
            chat,
        )
        await conn.execute(
            "UPDATE inquiries SET account = $1 WHERE id = $2",
            _EVE,
            theirs,
        )
        seqs = {
            row["id"]: row["seq"]
            for row in await conn.fetch(
                "SELECT id, seq FROM inquiries WHERE id = ANY($1)",
                [chat, alone],
            )
        }
    install_identity(make_test_identity(role="admin", api_key_id=None))
    for session_id in (chat, alone, theirs):
        _ = _started(relay, host_id=box, session_id=session_id)

    listed = await client.get(HOST_RUNS_PATH.format(name="box"))
    hosts = await client.get(TERMINAL_HOSTS_PATH)

    assert listed.status_code == 200, listed.text
    named = {
        run.session_id: (run.seq, run.conversation_id)
        for run in HostRuns.model_validate_json(listed.text).runs
    }
    assert named == {
        chat: (seqs[chat], conversation),
        alone: (seqs[alone], None),
        # Another account's session, as a remote agent service runs: listed, not named.
        theirs: (None, None),
    }
    (host,) = TerminalHosts.model_validate_json(hosts.text).hosts
    assert {
        run.session_id: (run.seq, run.conversation_id) for run in host.runs
    } == named


async def test_stopping_a_process_detaches_it_and_revokes_its_key_a_minute_later(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    relay: _Relay,
    later: _Later,
    minute: _Minute,
) -> None:
    client, store = pglite_route_client
    box, _ = await _joined(client, "box")
    process_id, socket = await _attached(store, relay, host_id=box, session_id=_MINE)

    stopped = await client.post(_stop_path("box", _MINE))

    assert stopped.status_code == 204, stopped.text
    assert await socket.close_code() == CLOSE_ENDED
    process = relay.process(_MINE)
    assert process is not None
    assert (process.state, process.reason) == ("detached", "stopped")
    again = await client.post(_stop_path("box", _MINE))
    listed = await client.get(HOST_RUNS_PATH.format(name="box"))
    assert again.status_code == 404
    assert HostRuns.model_validate_json(listed.text).runs == []
    # A minute to upload its last records, then its key goes.
    (revoking,) = later.ran
    assert await _key_live(store, process_id)
    minute.passed.set()
    await asyncio.wait_for(revoking, 5)
    assert minute.asked == [60.0]
    assert not await _key_live(store, process_id)


async def test_another_persons_host_reads_as_none(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    relay: _Relay,
    later: _Later,
) -> None:
    client, store = pglite_route_client
    box, _ = await _joined(client, "box")
    async with store.engine.acquire() as conn:
        await conn.execute("UPDATE hosts SET owner = $1", _EVE)
    started = _started(relay, host_id=box, session_id=_THEIRS)

    listed = await client.get(HOST_RUNS_PATH.format(name="box"))
    stopped = await client.post(_stop_path("box", _THEIRS))
    unknown = await client.get(HOST_RUNS_PATH.format(name="nowhere"))

    assert (listed.status_code, stopped.status_code, unknown.status_code) == (
        404,
        404,
        404,
    )
    assert relay.process(_THEIRS) == started
    assert later.ran == []


async def test_disconnect_revokes_the_host_and_ends_the_processes_it_lists(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    relay: _Relay,
) -> None:
    client, store = pglite_route_client
    box, credential = await _joined(client, "box")
    process_id, socket = await _attached(store, relay, host_id=box, session_id=_MINE)

    disconnected = await client.post(HOST_DISCONNECT_PATH.format(name="box"))
    beat = await client.post(
        HEARTBEAT_PATH.format(host_id=box),
        json={"instance": str(_INSTANCE), "daemon_version": "0.1"},
        headers={"Authorization": f"Bearer {credential}"},
    )

    assert disconnected.status_code == 200, disconnected.text
    assert HostRuns.model_validate_json(disconnected.text) == HostRuns(runs=[_MY_RUN])
    assert beat.status_code == 410
    assert await socket.close_code() == CLOSE_HOST_REVOKED
    process = relay.process(_MINE)
    assert process is not None
    assert (process.state, process.reason) == ("detached", "host revoked")
    # Its key goes with its host, at once.
    assert not await _key_live(store, process_id)


async def test_a_hosts_past_runs_are_the_sessions_it_served_that_are_not_live(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    relay: _Relay,
) -> None:
    client, store = pglite_route_client
    await _enroll(client, "box")
    act_as_user_agent("writer")
    session = await start_session(client, cli="sagent")
    # What a process's socket writes as it attaches.
    attached = datetime.now(UTC)
    await store.record_session_seen(
        session,
        at=attached,
        polled=True,
        place=RunPlace(host="box", cli="sagent"),
    )
    # A later sign of life that names no place, as an inbound drain is, keeps both.
    await store.record_session_seen(
        session,
        at=attached + timedelta(minutes=2),
        polled=True,
    )
    install_identity(make_test_identity(role="admin", api_key_id=None))

    listed = await client.get(HOST_RUNS_PATH.format(name="box"))

    assert listed.status_code == 200, listed.text
    runs = HostRuns.model_validate_json(listed.text)
    assert runs.runs == []
    assert [(run.session_id, run.cli, run.run, run.cwd) for run in runs.past] == [
        (session, "sagent", None, None),
    ]
    assert runs.past[0].last_seen is not None
    assert relay.process(session) is None


async def test_logout_disconnects_and_revokes_the_keys_its_logins_made_alone(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    relay: _Relay,
) -> None:
    client, store = pglite_route_client
    host_id, credential = await _joined(client, "box")
    linked, other = uuid.uuid4(), uuid.uuid4()
    async with store.engine.acquire() as conn:
        for key, host in ((linked, host_id), (other, None)):
            await conn.execute(
                "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role, "
                "host_id) VALUES ($1, $2, 'trax@box', 'hash', $3, 'writer', $4)",
                key,
                TEST_USER_ID,
                f"trax_{key.hex[:6]}",
                host,
            )
    process_id, socket = await _attached(
        store,
        relay,
        host_id=host_id,
        session_id=_MINE,
    )

    out = await client.post(HOST_LOGOUT_PATH.format(name="box"))
    beat = await client.post(
        HEARTBEAT_PATH.format(host_id=host_id),
        json={"instance": str(_INSTANCE), "daemon_version": "0.1"},
        headers={"Authorization": f"Bearer {credential}"},
    )

    assert out.status_code == 200, out.text
    # The process's key, bound to the host too, went with the disconnect, and is not
    # counted again among the logins' keys.
    assert HostLogout.model_validate_json(out.text) == HostLogout(
        runs=[_MY_RUN],
        keys=1,
    )
    assert beat.status_code == 410
    assert await socket.close_code() == CLOSE_HOST_REVOKED
    async with store.engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, revoked_at IS NOT NULL AS revoked FROM api_keys WHERE id = ANY($1)",
            [linked, other],
        )
    assert {row["id"]: row["revoked"] for row in rows} == {linked: True, other: False}
    assert not await _key_live(store, process_id)


async def test_a_key_names_the_host_whose_logout_revokes_it(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    host_id, _ = await _joined(client, "box")
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE api_keys SET host_id = $1 WHERE id = $2",
            host_id,
            TEST_API_KEY_ID,
        )
    named: list[object] = []
    for key in (TEST_API_KEY_ID, None):
        install_identity(make_test_identity(role="admin", api_key_id=key))
        profile = await client.get("/api/me/profile")
        assert profile.status_code == 200, profile.text
        named.append(json_loads(profile.text, dict[str, object]).get("host"))

    assert named == ["box", None]


def _started(
    relay: _Relay,
    *,
    host_id: uuid.UUID,
    session_id: uuid.UUID,
    process_id: uuid.UUID | None = None,
    token: str = "",
) -> AgentProcess:
    """Offer a process of ``session_id`` to ``box``, as a start does."""
    return relay.start(
        session_id,
        ProcessSpec(),
        process_id=uuid.uuid4() if process_id is None else process_id,
        host_id=host_id,
        host="box",
        token=token,
        remote=False,
    )


async def _attached(
    store: Store,
    relay: _Relay,
    *,
    host_id: uuid.UUID,
    session_id: uuid.UUID,
) -> tuple[uuid.UUID, _Socket]:
    """Start a process on the host with a key minted for it, and attach it."""
    process_id = uuid.uuid4()
    async with store.engine.acquire() as conn:
        _, token, _, _ = await create_api_key(
            conn,
            user_id=TEST_USER_ID,
            name=process_key_name(process_id, session_id=session_id),
            ceiling="writer",
            host_id=host_id,
        )
    _ = _started(
        relay,
        host_id=host_id,
        session_id=session_id,
        process_id=process_id,
        token=token,
    )
    viewer = relay.lines(session_id, allowed=_always)
    _ = await anext(viewer)
    socket = _Socket()
    relay.served.append(
        asyncio.ensure_future(
            relay.serve_process(
                session_id,
                socket,
                Hello(process_id=process_id, version="1", host="box"),
                host_id=host_id,
                allowed=_always,
            ),
        ),
    )
    attached = await asyncio.wait_for(anext(viewer), 5)
    assert isinstance(attached, ProcessLine)
    assert attached.process.state == "attached"
    await viewer.aclose()
    return process_id, socket


async def _key_live(store: Store, process_id: uuid.UUID) -> bool:
    async with store.engine.acquire() as conn:
        return (
            await conn.fetchval(
                "SELECT revoked_at IS NULL FROM api_keys WHERE starts_with(name, $1)",
                f"agent process {process_id}",
            )
            is True
        )


async def _evebox(store: Store) -> None:
    """Register eve's own host, which runs nothing."""
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO hosts (name, updated_by, owner) VALUES ('evebox', $1, $1)",
            _EVE,
        )


def _stop_path(name: str, session_id: uuid.UUID) -> str:
    return HOST_RUN_STOP_PATH.format(name=name, session_id=session_id)


async def _enroll(client: httpx2.AsyncClient, name: str) -> str:
    response = await client.post(ENROLL_PATH, json={"name": name})
    assert response.status_code == 201, response.text
    return EnrollResponse.model_validate_json(response.text).token


async def _joined(client: httpx2.AsyncClient, name: str) -> tuple[uuid.UUID, str]:
    response = await client.post(
        JOIN_PATH,
        json={
            "name": name,
            "token": await _enroll(client, name),
            "instance": str(_INSTANCE),
            "daemon_version": "0.1",
            "facts": {},
        },
    )
    assert response.status_code == 201, response.text
    joined = JoinResponse.model_validate_json(response.text)
    return joined.host_id, joined.credential


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
