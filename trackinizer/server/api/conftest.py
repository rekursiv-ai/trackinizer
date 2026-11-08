"""Shared fixtures for FastAPI route tests.

Phase 2 of ``docs/design.md (Auth)`` gated every route behind
:func:`require_role`. To keep the existing per-route behavioural tests
focused on what they actually test (route plumbing, store wiring, body
validation), :func:`route_client` installs a static :class:`AuthIdentity`
override on the dependency surface so every request resolves as a
writer-role principal. Tests that want to exercise the 401 / 403 paths
explicitly pop the override (see ``api/auth_routes_test.py`` for the
pattern).
"""

from __future__ import annotations

from collections.abc import MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Final, NoReturn, cast, override
from unittest import mock

import asyncio
import time
import uuid

from fastapi import HTTPException, Response
from fastapi.testclient import TestClient
from treekle import from_plain

import httpx2
import pytest
import pytest_asyncio

from trackinizer.conftest import FakeEngine, make_conn, make_store, restore_pglite
from trackinizer.sagent.session.types import UserMessage
from trackinizer.server.api.app import app
from trackinizer.server.api.session_access import (
    SessionAccess,
    require_session_access,
)
from trackinizer.server.api.session_identity import Asker
from trackinizer.server.asks import open_ask_ids
from trackinizer.server.auth import (
    AuthIdentity,
    Role,
    create_api_key,
    current_user,
)
from trackinizer.server.config import Assistant, Config
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.inbound import Inbound, InboundQueue
from trackinizer.server.session import SESSION_COOKIE_NAME, set_session_cookie
from trackinizer.server.session_reaper import STALE_AFTER, reap_silent_sessions
from trackinizer.server.store.core import Store
from trackinizer.types.inquiries import AgentSession
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire import wire_terminals
from trackinizer.wire.wire_machine_host import ENROLL_PATH, JOIN_PATH
from trackinizer.wire.wire_session_ir import ManifestBody, RecordBody


if TYPE_CHECKING:
    from collections.abc import (
        AsyncIterator,
        Awaitable,
        Callable,
        Generator,
        Iterator,
        Mapping,
        Sequence,
    )

    from fastapi import FastAPI, Request
    from starlette.datastructures import State

    from trackinizer.lib.postgres import PGliteEngine


# Stable test principal injected by ``route_client``. Pinned UUIDs +
# email so assertions about ``change_log.api_key_id`` / ``actor``
# defaulting can refer to known values.
TEST_USER_ID: uuid.UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")
TEST_API_KEY_ID: uuid.UUID = uuid.UUID("22222222-2222-2222-2222-222222222222")
TEST_USER_EMAIL: Final[str] = "test-user@example.com"

# The served app ``TerminalWorld`` signs browsers into and hosts join.
TERMINAL_SESSION_KEY: Final = "session-signing-key"
TERMINAL_ORIGIN: Final = "https://trax.test"
TERMINAL_HOST: Final = "trax.test"
TERMINAL_INSTANCE: Final = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def make_test_identity(
    *,
    user_id: uuid.UUID = TEST_USER_ID,
    api_key_id: uuid.UUID | None = TEST_API_KEY_ID,
    email: str = TEST_USER_EMAIL,
    role: Role = "writer",
) -> AuthIdentity:
    """Mint a deterministic :class:`AuthIdentity` for route tests."""
    return AuthIdentity(user_id=user_id, api_key_id=api_key_id, email=email, role=role)


def install_identity(identity: AuthIdentity) -> None:
    """Wire ``identity`` as the result of :func:`current_user` for tests.

    Every role guard (``require_role(...)``) ultimately depends on
    :func:`current_user`, so this single override flows through the whole
    role-enforcement chain. Callers that want to test a 401 path pop the
    override; tests that want a 403 path install a viewer identity here
    before hitting a writer route.
    """

    async def _override() -> AuthIdentity:
        return identity

    app.dependency_overrides[current_user] = _override


class _StateEntries(MutableMapping[str, object]):
    """``app.state`` read as a mapping, for ``monkeypatch.setitem``.

    ``State`` keeps its keys outside the instance ``__dict__``, so pytest 9.2's
    ``monkeypatch.setattr`` records every key as absent and deletes it on undo.
    ``setitem`` reads the prior value through the mapping instead.
    """

    def __init__(self, state: State) -> None:
        self._state = state

    @override
    def __getitem__(self, key: str) -> object:
        return cast(object, self._state[key])

    @override
    def __setitem__(self, key: str, value: object) -> None:
        self._state[key] = value

    @override
    def __delitem__(self, key: str) -> None:
        del self._state[key]

    @override
    def __iter__(self) -> Iterator[str]:
        return iter(self._state)

    @override
    def __len__(self) -> int:
        return len(self._state)


def patch_state(monkeypatch: pytest.MonkeyPatch, name: str, value: object) -> None:
    """Set ``app.state.<name>`` for one test; teardown restores or removes it."""
    monkeypatch.setitem(_StateEntries(app.state), name, value)


def clear_identity_override() -> None:
    """Remove the :func:`current_user` override installed by tests."""
    app.dependency_overrides.pop(current_user, None)


def answer_account_active(engine: FakeEngine) -> None:
    """Make ``auth.assert_account_active`` pass on the fixture's mock conn.

    Route tests inject a static writer identity instead of seeding the
    ``users`` table, so the submit / ``set_account`` account-active gate
    would otherwise 422. Answer just that one ``users`` probe truthy while
    leaving every other ``fetchval`` on whatever the test configures via the
    mock's ``return_value`` / ``side_effect`` -- so a test that sets
    ``conn.fetchval.return_value = "Issue"`` still drives ``lookup_kind``.
    """
    conn = engine.conn

    def fetchval(sql: str, *args: object) -> object:
        del args
        # Answer only the account-active probe; ``DEFAULT`` tells the mock to
        # fall back to its own ``return_value``, so a test setting
        # ``conn.fetchval.return_value = "Issue"`` still drives ``lookup_kind``.
        if "FROM users" in sql and "status = 'active'" in sql:
            return 1
        return cast(object, mock.DEFAULT)

    conn.fetchval.side_effect = fetchval


@pytest.fixture(autouse=True)
def refuse_app_lifespan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail a test that runs the global ``app``'s lifespan (``with TestClient(app)``).

    The lifespan boots a real PGlite engine from the environment, about 2.5 s a
    test, and leaves ``config`` on the shared ``app.state``, which ``_serving``
    never restores. A lifespan test drives ``lifespan`` on an app of its own.
    """
    monkeypatch.setattr(app.router, "lifespan_context", _refused_lifespan)


@pytest.fixture
def route_client() -> Iterator[tuple[TestClient, Store, FakeEngine]]:
    """``TestClient`` + ``Store`` + ``FakeEngine`` with a writer principal injected.

    Yields rather than returns so the dependency override is torn down
    after each test -- otherwise a viewer-identity test in one file
    would leak into an unrelated writer-route test elsewhere.
    """
    store, engine = make_store()
    answer_account_active(engine)
    with _serving(engine, store, make_test_identity()):
        yield TestClient(app), store, engine


@pytest_asyncio.fixture(loop_scope="session")
async def pglite_route_client(
    pglite_engine: PGliteEngine,
) -> AsyncIterator[tuple[httpx2.AsyncClient, Store]]:
    """Async client for the whole ``app`` over a fresh PGlite ``Store``, as a writer.

    The whole app, not one router, so each request crosses the
    ``Idempotency-Key`` middleware and the domain-error handlers as it does in
    production. Async because the engine lives on the session loop, which
    ``TestClient``'s own loop cannot drive. Tests using it declare
    ``@pytest.mark.asyncio(loop_scope="session")``. Asks only for the root plugin's
    ``pglite_engine``, so another package's conftest can import it alone.
    """
    await restore_pglite(pglite_engine)
    store = Store(pglite_engine, embed=StubEmbedder())
    # Not raising app exceptions, so a crash reads as the 500 a client gets.
    transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
    # Keyless: ``change_log.api_key_id`` references ``api_keys``, empty here, so the
    # route tests' pinned key id would fail every audit write.
    with _serving(pglite_engine, store, make_test_identity(api_key_id=None)):
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as http:
            yield http, store


@pytest.fixture
def cheap_scrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hash secrets at the smallest accepted cost so a test pays no scrypt round."""
    monkeypatch.setattr("trackinizer.server.auth._SCRYPT_N", 2)


class TerminalWorld:
    """A served app with users, a joined machine and a fresh terminal hub."""

    def __init__(self, client: httpx2.AsyncClient, store: Store) -> None:
        self.client = client
        self.store = store
        self.machine_id = uuid.UUID(int=0)
        self.credential = ""

    async def person(self, email: str, *, role: str = "writer") -> uuid.UUID:
        """Add an active user; return their id."""
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
        """Return the ``Cookie`` header value that signs ``user_id`` in."""
        signed = Response()
        set_session_cookie(
            signed,
            user_id=str(user_id),
            secret=TERMINAL_SESSION_KEY,
            max_age_seconds=600,
        )
        value = signed.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
        return f"{SESSION_COOKIE_NAME}={value}"

    async def key(self, user_id: uuid.UUID) -> str:
        """Mint a writer API key for ``user_id``; return its secret."""
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
        """Send one request to the served app."""
        return await self.client.request(method, path, json=body, headers=headers)

    def browser(
        self,
        cookie: str,
        *,
        origin: str | None = TERMINAL_ORIGIN,
    ) -> dict[str, str]:
        """Return the headers a signed-in browser sends, from ``origin`` if any."""
        found = {"Cookie": cookie, "Host": TERMINAL_HOST}
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
        host_version: str = "t",
    ) -> None:
        """Self-enroll ``name`` with ``cookie``'s user, join it, and confirm it."""
        enrolled = await self.call(
            "POST",
            ENROLL_PATH,
            headers=self.browser(cookie),
            body={"name": name},
        )
        if enrolled.status_code != 201:
            raise ValueError("Expected enrolled.status_code == 201.")
        joined = await self.call(
            "POST",
            JOIN_PATH,
            headers={},
            body={
                "name": name,
                "token": from_plain(enrolled.json(), dict[str, str])["token"],
                "instance": str(TERMINAL_INSTANCE),
                "host_version": host_version,
                "facts": facts or {},
            },
        )
        if joined.status_code != 201:
            raise ValueError("Expected joined.status_code == 201.")
        body = from_plain(joined.json(), dict[str, str])
        self.machine_id = uuid.UUID(body["machine_id"])
        self.credential = body["credential"]
        if confirm:
            confirmed = await self.call(
                "POST",
                wire_terminals.TERMINAL_CONFIRM_PATH.format(name=name),
                headers=self.browser(cookie),
            )
            if confirmed.status_code != 204:
                raise ValueError(confirmed.text)

    def host_headers(self) -> dict[str, str]:
        """Return the headers the joined host authenticates with."""
        return {"Authorization": f"Bearer {self.credential}"}

    async def wait(
        self,
        *,
        credential: str | None = None,
        instance: uuid.UUID = TERMINAL_INSTANCE,
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
        canvas: uuid.UUID | None = None,
        origin: str | None = TERMINAL_ORIGIN,
        requested_by: str | None = None,
        surface: str | None = None,
        command: str | None = None,
        attach: uuid.UUID | None = None,
        run_name: str | None = None,
        cwd: str | None = None,
        kind: str | None = None,
    ) -> httpx2.Response:
        """Ask to open a terminal from the browser signed in by ``cookie``."""
        body: dict[str, object] = {"machine": machine, "rows": 24, "cols": 80}
        if kind is not None:
            body["kind"] = kind
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
        if canvas is not None:
            body["canvas"] = str(canvas)
        return await self.call(
            "POST",
            wire_terminals.TERMINALS_PATH,
            headers=self.browser(cookie, origin=origin),
            body=body,
        )

    async def canvas(self, user_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
        """Give ``user_id`` a canvas in a workspace; return both ids."""
        canvas, workspace = uuid.uuid4(), uuid.uuid4()
        async with self.store.engine.acquire() as conn:
            await conn.execute(
                "INSERT INTO visual_canvases (id, user_id, state, metadata) "
                "VALUES ($1, $2, '{}', jsonb_build_object('name', 's', "
                "'workspace_id', $3::text))",
                canvas,
                user_id,
                str(workspace),
            )
        return canvas, workspace


@pytest.fixture
def world(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> TerminalWorld:
    """Serve the app signed in by session cookie alone, with no terminal hub yet."""
    client, store = pglite_route_client
    clear_identity_override()
    patch_state(
        monkeypatch,
        "config",
        Config(session_secret=TERMINAL_SESSION_KEY, session_max_age_seconds=600),
    )
    monkeypatch.delattr(app.state, "terminals", raising=False)
    return TerminalWorld(client, store)


def refusal_detail_code(response: httpx2.Response) -> str:
    """Return the ``code`` of a refusal's ``detail: {code, message}``."""
    detail = from_plain(response.json(), dict[str, dict[str, str]])["detail"]
    if set(detail) != {"code", "message"}:
        raise ValueError('Expected set(detail) == {"code", "message"}.')
    return detail["code"]


def refusal_message(response: httpx2.Response) -> str:
    """Return the ``message`` of a refusal's ``detail: {code, message}``."""
    detail = from_plain(response.json(), dict[str, dict[str, str]])["detail"]
    if set(detail) != {"code", "message"}:
        raise ValueError('Expected set(detail) == {"code", "message"}.')
    return detail["message"]


# A real instance, not a renamed stand-in: ``_require_session`` gates with
# ``isinstance``, so the row must be the canonical class. Defaults its opening
# credential to ``TEST_API_KEY_ID`` -- the key the default test identity presents -- so
# a route's owner-scope check passes for a same-credential caller; foreign-credential
# tests pass an explicit other id.
def live_session(
    opened_by_api_key_id: uuid.UUID | None = TEST_API_KEY_ID,
) -> AgentSession:
    """Return a minimal live ``AgentSession`` (``ended`` is ``None``)."""
    return AgentSession(
        owner="scientist",
        cli="claude",
        opened_by_api_key_id=opened_by_api_key_id,
    )


async def session_row(store: Store, *, session_id: uuid.UUID) -> AgentSession:
    """Return the session's row."""
    row = await store.get_inquiry(session_id)
    assert isinstance(row, AgentSession)
    return row


REAP_WINDOW = STALE_AFTER + timedelta(minutes=1)
"""Long enough from now that anything seen so far counts as silent."""


async def reap(store: Store, *, inbound: InboundQueue, after: timedelta) -> int:
    """Run one reaper pass as of ``after`` from now."""
    now = datetime.now(UTC) + after
    return await reap_silent_sessions(store, inbound=inbound, now=now)


AWAIT_MESSAGES = InboundQueue.await_messages
"""The wait itself, as it is before a test wraps it."""


@dataclass(slots=True, kw_only=True)
class Holds:
    """The requests that parked on the inbound queue to wait for a message."""

    parked: asyncio.Event = field(default_factory=asyncio.Event)
    """Set once a request has begun waiting."""

    asked_sec: list[float] = field(default_factory=list)
    """How long each request asked to wait."""


async def parked_wait(
    queue: InboundQueue,
    holds: Holds,
    session_id: uuid.UUID,
    *,
    timeout_sec: float,
) -> list[Inbound]:
    """Wait on ``queue`` as it does, noting that the wait began."""
    holds.asked_sec.append(timeout_sec)
    holds.parked.set()
    return await AWAIT_MESSAGES(queue, session_id, timeout_sec=timeout_sec)


def opens_a_part(name: str) -> dict[str, object]:
    """Return the body that opens the part ``name`` with one line."""
    record = RecordBody.of(
        SessionRecordRow.of(
            session_id=uuid.UUID(int=0),
            part=0,
            idx=0,
            record=UserMessage(content="hi"),
        ),
    )
    manifest = ManifestBody(name=name, ir_id=uuid.uuid4(), format="claude", records=1)
    return {
        "name": name,
        "manifest": manifest.model_dump(mode="json"),
        "records": [record.model_dump(mode="json")],
    }


async def attach_run(
    client: httpx2.AsyncClient,
    *,
    session_id: uuid.UUID,
) -> httpx2.Response:
    """Start a claude run that re-opens ``session_id``."""
    return await client.post(
        "/api/sessions/start",
        json={"cli": "claude", "attach": str(session_id)},
    )


async def report_state(
    client: httpx2.AsyncClient,
    *,
    session_id: uuid.UUID,
    state: str,
    instance: str,
) -> httpx2.Response:
    """Report a run's state as the installed identity."""
    return await client.post(
        f"/api/sessions/{session_id}/run-state",
        json={"state": state, "instance": instance},
    )


async def open_asks(store: Store, session_id: uuid.UUID) -> list[str]:
    """Return the asks open in a session."""
    async with store.engine.acquire() as conn:
        return await open_ask_ids(conn, session_id=session_id)


async def waited_on_locks(store: Store, *, count: int, wait_sec: float = 5) -> bool:
    """Return whether ``count`` other backends of the test database came to wait.

    That is the moment a race is set: every racer has reached the row the test's
    transaction holds and is queued on it. A fixed sleep only hoped it had, and a
    racer still on its way would take another path and pass for the wrong reason.
    It answers rather than raises, so the test still releases what it holds.

    Args:
      store: The store whose database the racers use.
      count: How many backends must be waiting on a lock.
      wait_sec: How long to wait for them.

    Returns:
      waited: True once they are; False if ``wait_sec`` passes first.

    """
    deadline = time.monotonic() + wait_sec
    async with store.engine.acquire() as conn:
        while time.monotonic() < deadline:
            waiting = await conn.fetchval(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() "
                "AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()",
            )
            if from_plain(waiting, int) >= count:
                return True
            await asyncio.sleep(0.005)
    return False


@dataclass(slots=True, kw_only=True)
class RecordingServer:
    """The app, as the helpers see it: canned answers by method and path."""

    answers: dict[tuple[str, str], tuple[int, object]]
    asked: list[httpx2.Request] = field(default_factory=list)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Record ``request`` and answer it with its canned answer."""
        self.asked.append(request)
        status, body = self.answers[(request.method, request.url.path)]
        return httpx2.Response(status, json=body)


def run_on[T](
    server: RecordingServer,
    *,
    call: Callable[[httpx2.AsyncClient], Awaitable[T]],
) -> T:
    """Run one helper against ``server``."""

    async def run() -> T:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(server)) as client:
            return await call(client)

    return asyncio.run(run())


OPENER: Final = uuid.uuid4()
"""The API key a session-access test's session was opened with."""

SCOUT: Final = Assistant(actor="scout", email="scout@example.com")
"""The assistant a session-access test configures."""


def make_caller(
    *read: str,
    key: uuid.UUID | None = None,
    email: str = "caller@example.com",
    hosted_domain: str | None = None,
) -> AuthIdentity:
    """Return a writer reading ``read``, presenting ``key`` if any."""
    return AuthIdentity(
        user_id=uuid.uuid4(),
        api_key_id=key,
        email=email,
        role="writer",
        read_audiences=frozenset(read),
        hosted_domain=hosted_domain,
    )


def make_asker(
    *read: str,
    role: Role = "writer",
    email: str = "asker@example.com",
) -> Asker:
    """Return a user the assistant acts for, holding ``read`` as a person."""
    return Asker(email=email, role=role, read_audiences=frozenset(read))


# Each answer is one ``fetchval``'s, in order: whether a reader reads a session, and for
# a read for an asker the chat's first session between them. The check must ask each,
# and no more.
async def access_refusal(
    caller: AuthIdentity,
    session: AgentSession,
    mode: SessionAccess,
    *,
    assistant: Assistant | None = None,
    asker: Asker | None = None,
    chat: uuid.UUID | None = None,
    answers: Sequence[object] = (),
) -> HTTPException | None:
    """Ask the check with ``assistant`` configured; the database answers ``answers``."""
    conn = make_conn()
    conn.fetchval.side_effect = list(answers)
    store, _engine = make_store(conn)
    state = SimpleNamespace(store=store, config=Config(assistant=assistant))
    request = cast("Request", SimpleNamespace(app=SimpleNamespace(state=state)))
    refusal: HTTPException | None = None
    try:
        await require_session_access(
            request,
            caller,
            session=session,
            mode=mode,
            asker=None if asker is None else replace(asker, chat=chat),
        )
    except HTTPException as error:
        refusal = error
    if conn.fetchval.await_count != len(answers):
        raise ValueError(
            f"Expected {len(answers)} database answers to be asked; "
            f"{conn.fetchval.await_count} were.",
        )
    return refusal


async def access_refused(
    caller: AuthIdentity,
    session: AgentSession,
    mode: SessionAccess,
    *,
    assistant: Assistant | None = None,
    asker: Asker | None = None,
    answers: Sequence[object] = (),
) -> bool:
    """Say whether the check refuses; a refusal names the session's audience."""
    refusal = await access_refusal(
        caller,
        session,
        mode,
        assistant=assistant,
        asker=asker,
        answers=answers,
    )
    if refusal is None:
        return False
    expected: dict[str, object] = {
        "code": "session_audience",
        "audience": session.audience,
    }
    if (
        refusal.status_code != 403
        or from_plain(refusal.detail, dict[str, object]) != expected
    ):
        raise ValueError(f"Expected a session_audience 403; got {refusal!r}.")
    return True


# Saves the prior ``app.state`` so an integration test that ran before (and set a real
# engine via lifespan) is not wiped out, and restores it after.
@contextmanager
def _serving(
    engine: object,
    store: Store,
    identity: AuthIdentity,
) -> Generator[None]:
    """Serve ``store`` from the global ``app`` to ``identity``, for one test."""
    prev_engine = getattr(app.state, "engine", None)
    prev_store = getattr(app.state, "store", None)
    prev_inbound = getattr(app.state, "inbound", None)
    app.state.engine = engine
    app.state.store = store
    app.state.inbound = InboundQueue()
    install_identity(identity)
    try:
        yield
    finally:
        clear_identity_override()
        if prev_engine is None:
            del app.state.engine
        else:
            app.state.engine = prev_engine
        if prev_store is None:
            del app.state.store
        else:
            app.state.store = prev_store
        if prev_inbound is None:
            del app.state.inbound
        else:
            app.state.inbound = prev_inbound


def _refused_lifespan(app: FastAPI) -> NoReturn:
    """Stand in for the global ``app``'s lifespan; see ``refuse_app_lifespan``."""
    del app
    raise RuntimeError(
        "A route test ran the global app's lifespan, which boots a real PGlite "
        "engine. Use TestClient(app) without `with`.",
    )
