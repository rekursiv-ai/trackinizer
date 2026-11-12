"""Shared fixtures and helpers for trackinizer test modules.

Tests across ``trackinizer_test.py`` and friends share:

* mock-based unit testing helpers (``make_conn`` / ``FakeEngine`` /
  ``make_store`` / ``executed_sql`` / ``new_uuid``), imported explicitly;
* ``ExchangeEngine``, a ``FakeEngine`` that expects exactly the statements a test
  lists as :class:`Says` steps, and ``RecordingPolicy``, an access policy that
  records each call;
* ``pglite_db``, the session's shared PGlite engine, and ``store``, a
  :class:`Store` over it; ``integ_store``, one over the session's PostgreSQL
  database. :func:`restore` bootstraps each engine's schema once a session and
  puts it back before every test, so every test starts from a fresh, empty
  database, for any fixture holding the engine, whichever package imports it.
  Tests using them must declare
  ``@pytest.mark.asyncio(loop_scope="session")``: the engines live on the
  session loop, so per-test loops can't drive them;
* ``pglite_rollback_conn`` and ``integ_rollback_conn``, a connection to either
  build inside a transaction rolled back after the test, for a migration step's
  schema surgery, and ``savepoint`` for a statement such a test expects refused;
* :class:`Baselines`, what :func:`restore` rests on, for any package's builds.

A test database's builds, each made once and put back before every test that wants it.

A build is a database a test starts from: a root build fills empty schemas (a
service's bootstrap, say), and every other build runs its migrations over the root
build's database and adds its rows. Making one costs a bootstrap; putting one back
costs one transaction. The first :meth:`Baselines.ready` of a build makes it and
copies every filled table, and every sequence position, aside; each later call
clears every managed table and copies the rows back, so no test sees another's
writes, and drops the session's temporary tables.

The rows fit only the catalog they were copied from, so each put-back first
compares the catalog with the build's. A database holding another build's catalog
is given this one's: the build whose migrations this one's extend is put back, and
the rest run over it (the root build's, with all of them, at worst). A catalog
changed otherwise (a migration test, a dropped column, a reset, another package's
view or foreign key over a managed table) makes the build again. Session settings,
advisory locks and listeners are not covered.

Each build's record is kept in the database, beside its copies, so every holder of the
engine puts back the same build, whichever package's fixture made it: a build of the
same key over the same schemas is made once a database.

The usual root is :data:`CORE`, trackinizer's own bootstrap::

    made = Baselines(engine, root=CORE, schemas=("public", "billing"))
    keys = await made.ready(Build(name="paid", migrations=steps, fill=signed_up))

The builds keep their tables here, in a conftest, because they are test storage:
``new_tables_test`` gates the shared database's tables, and scans no test code.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Generator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal, Self, cast
from unittest.mock import AsyncMock, MagicMock

import hashlib
import uuid

from starlette.datastructures import State
from treekle import from_plain

import httpx2
import pytest
import pytest_asyncio

from trackinizer.lib import postgres
from trackinizer.lib.postgres import DatabaseEngine
from trackinizer.lib.postgres.testing import postgres_database, reset_schema
from trackinizer.lib.testing.resources import (
    isolate_user_dirs,
    pytest_collection_modifyitems,
    pytest_configure,
)
from trackinizer.mock_test_support import MockConnection
from trackinizer.server.access_policy import AccessDeniedError
from trackinizer.server.api.addons_routes import attach_deployment
from trackinizer.server.auth import BOOTSTRAP_ADMIN_ENV, BOOTSTRAP_TOKEN_FILE_ENV
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.notify import NOTIFY_CHANNEL
from trackinizer.server.store.core import Store
from trackinizer.server.values import vetted_sql
from trackinizer.types.errors import ConflictError


if TYPE_CHECKING:
    from collections.abc import (
        AsyncGenerator,
        AsyncIterator,
        Iterator,
    )
    from contextlib import AbstractAsyncContextManager

    from fastapi import FastAPI

    import asyncpg

    from trackinizer.addons.deployment import Deployment
    from trackinizer.lib.postgres import Conn, PGliteEngine
    from trackinizer.server.access_policy import (
        Action,
        Principal,
        Resource,
        ResourceKind,
    )
    from trackinizer.server.auth import Role


# Re-exported, not merely imported: an autouse fixture reaches only the
# directory of the conftest that names it, so binding it here is what points
# every trackinizer test's XDG lookups at a tmp dir instead of the developer's
# own -- notably trax profiles, which live under ``config_dir``.
__all__ = [
    "isolate_bootstrap_environment",
    "isolate_user_dirs",
    "pytest_collection_modifyitems",
    "pytest_configure",
]


@pytest.fixture(scope="session", autouse=True)
def isolate_bootstrap_environment() -> Iterator[None]:
    """Keep operator bootstrap settings out of test databases, including session setup."""
    with without_operator_bootstrap():
        yield


def make_conn() -> MockConnection:
    """Build an ``AsyncMock`` connection with permissive default returns.

    ``fetchrow`` dispatches: SQL matching ``emit_change``'s cost-update
    ``UPDATE inquiries ... RETURNING ...`` is auto-synthesized so individual
    tests never have to interleave cost rows into their field-read scripts;
    everything else flows through a per-mock queue / default that tests
    drive via :func:`set_field_row` and :func:`queue_field_rows`.
    """
    conn = MockConnection()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    field_default: list[object] = [None]
    field_queue: list[object] = []

    async def fetchrow(sql: str, *args: object) -> object:
        if "marginal_cost_agent_usd" in sql and "RETURNING" in sql:
            agent = from_plain(args[0], float) if args else 0.0
            resource = from_plain(args[1], float) if len(args) > 1 else 0.0
            old_agent = max(0.0, -agent)
            old_resource = max(0.0, -resource)
            subject_id = args[2] if len(args) > 2 else uuid.uuid4()
            return {
                "existing_id": subject_id,
                "old_agent": old_agent,
                "old_resource": old_resource,
                "new_agent": old_agent + agent,
                "new_resource": old_resource + resource,
                "current_subscribers": list[str](),
            }
        if field_queue:
            return field_queue.pop(0)
        return field_default[0]

    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    conn.field_default = field_default
    conn.field_queue = field_queue
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.set_type_codec = AsyncMock()
    conn.close = AsyncMock()
    conn.add_listener = AsyncMock()
    conn.is_closed = MagicMock(return_value=False)
    return conn


def set_field_row(conn: MockConnection, row: object | None) -> None:
    """Set the default field-read row returned by ``conn.fetchrow``."""
    conn.field_default[0] = row


def queue_field_rows(conn: MockConnection, *rows: object) -> None:
    """Queue field-read rows; ``conn.fetchrow`` pops one per call."""
    conn.field_queue.extend(rows)


class FakeEngine:
    """``DatabaseEngine`` Protocol stub backed by one ``AsyncMock`` connection."""

    def __init__(self, conn: MockConnection | None = None) -> None:
        self.conn: MockConnection = conn or make_conn()
        self.notify_calls: list[tuple[str, str]] = []
        self.notify_error: BaseException | None = None
        """When set, ``notify`` raises it -- the post-commit fanout is
        best-effort, and a test proving that needs a failing notify without
        rebinding the bound method."""
        self.listen_messages: list[str] = []
        self.entered = False
        self.exited = False
        self._held = False

    async def __aenter__(self) -> Self:
        self.entered = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        del exc
        self.exited = True

    def acquire(self) -> AbstractAsyncContextManager[MockConnection]:
        # Model the real single-connection substrate: a nested acquire while
        # one is already held would deadlock on PGlite, so it must raise here
        # too -- otherwise reentrancy bugs pass under the mock and only blow
        # up against a live server (see substrate.py's PGlite guard).
        @asynccontextmanager
        async def cm() -> AsyncGenerator[MockConnection]:
            if self._held:
                raise RuntimeError(
                    "Reentrant acquire on the single connection would "
                    "deadlock: this task already holds the connection.",
                )
            self._held = True
            try:
                yield self.conn
            finally:
                self._held = False

        return cm()

    async def notify(self, channel: str, payload: str) -> None:
        if self.notify_error is not None:
            raise self.notify_error
        self.notify_calls.append((channel, payload))

    def listen(self, channel: str) -> AsyncIterator[str]:
        del channel
        msgs = list(self.listen_messages)

        async def gen() -> AsyncIterator[str]:
            for m in msgs:
                yield m

        return gen()


@dataclass(frozen=True, slots=True, kw_only=True)
class Says:
    """One statement a code path must send, and the database's answer to it."""

    method: Literal["execute", "fetch", "fetchrow", "fetchval"]
    sql: str
    args: tuple[object, ...] = ()
    answer: object = None


BEGIN: Final = Says(method="execute", sql="BEGIN", answer="BEGIN")
COMMIT: Final = Says(method="execute", sql="COMMIT", answer="COMMIT")
ROLLBACK: Final = Says(method="fetch", sql="ROLLBACK", answer=[])

TRANSACTION_BEGIN: Final = Says(method="execute", sql="BEGIN;", answer="BEGIN")
"""What asyncpg's ``conn.transaction()`` sends on entry."""

TRANSACTION_COMMIT: Final = Says(method="execute", sql="COMMIT;", answer="COMMIT")
"""What asyncpg's ``conn.transaction()`` sends when its block ends cleanly."""

TRANSACTION_ROLLBACK: Final = Says(
    method="execute",
    sql="ROLLBACK;",
    answer="ROLLBACK",
)
"""What asyncpg's ``conn.transaction()`` sends when its block raises."""


class ExchangeEngine(FakeEngine):
    """A ``FakeEngine`` whose connection expects exactly ``steps``, in order.

    Each statement must be the next step's method, SQL and arguments; it gets that
    step's answer. Any other statement fails the test, so a test states a code
    path's whole exchange with the database. Once a statement has failed, the
    ``ROLLBACK`` that ``tx`` or ``conn.transaction()`` sends on the way out is let
    through, so the first mismatch is the one reported. ``said`` keeps each step with
    the arguments actually sent. An answer that is an exception is raised, as by a
    database that refuses the statement. ``conn.transaction()`` sends what asyncpg's
    does (:data:`TRANSACTION_BEGIN`, then :data:`TRANSACTION_COMMIT` or
    :data:`TRANSACTION_ROLLBACK`) through the same exchange, whether used as a block
    or started and ended by hand.
    """

    def __init__(self, *steps: Says) -> None:
        super().__init__()
        self.pending = list(steps)
        self.said: list[Says] = []
        self.failed = False
        self.conn.execute = AsyncMock(side_effect=self._execute)
        self.conn.fetch = AsyncMock(side_effect=self._fetch)
        self.conn.fetchrow = AsyncMock(side_effect=self._fetchrow)
        self.conn.fetchval = AsyncMock(side_effect=self._fetchval)
        self.conn.transaction = MagicMock(side_effect=self._transaction)

    @property
    def done(self) -> bool:
        """Whether every step was said."""
        return not self.pending

    async def _execute(self, sql: str, *args: object) -> object:
        return self._said("execute", sql, args)

    async def _fetch(self, sql: str, *args: object) -> object:
        return self._said("fetch", sql, args)

    async def _fetchrow(self, sql: str, *args: object) -> object:
        return self._said("fetchrow", sql, args)

    async def _fetchval(self, sql: str, *args: object) -> object:
        return self._said("fetchval", sql, args)

    def _transaction(self) -> _Transaction:
        return _Transaction(say=self._said)

    def _said(self, method: str, sql: str, args: tuple[object, ...]) -> object:
        if self.failed and sql in {"ROLLBACK", TRANSACTION_ROLLBACK.sql}:
            return None
        step = self.pending.pop(0) if self.pending else None
        if step is None or (method, sql, args) != (step.method, step.sql, step.args):
            self.failed = True
            raise AssertionError(f"sent {method} {sql!r} {args!r}; expected {step!r}")
        self.said.append(
            Says(method=step.method, sql=sql, args=args, answer=step.answer),
        )
        if isinstance(step.answer, BaseException):
            raise step.answer
        return step.answer


@dataclass(frozen=True, slots=True, kw_only=True)
class _Transaction:
    """What ``conn.transaction()`` returns, sending asyncpg's statements through ``say``.

    As asyncpg's ``Transaction``, entering it starts it and leaving it commits, or
    rolls back when its block raises; ``start``, ``commit`` and ``rollback`` also
    work by hand.
    """

    say: Callable[[str, str, tuple[object, ...]], object]

    async def start(self) -> None:
        _ = self.say("execute", TRANSACTION_BEGIN.sql, ())

    async def commit(self) -> None:
        _ = self.say("execute", TRANSACTION_COMMIT.sql, ())

    async def rollback(self) -> None:
        _ = self.say("execute", TRANSACTION_ROLLBACK.sql, ())

    async def __aenter__(self) -> None:
        await self.start()

    async def __aexit__(
        self,
        raised: type[BaseException] | None,
        *rest: object,
    ) -> None:
        del rest
        await (self.commit() if raised is None else self.rollback())


@dataclass(slots=True, kw_only=True)
class RecordingPolicy:
    """An access policy that records each call and allows what the default allows.

    ``calls`` keeps each call's name and arguments in order; ``sent`` keeps, per
    call, every statement a mock connection had been sent before it, so a test sees
    whether the call ran inside the write's transaction.
    """

    groups_of: frozenset[str] = frozenset({"g1"})
    """What :meth:`groups` answers."""

    deny_writes: bool = False
    """Whether :meth:`check` refuses, as a policy whose grants exclude the caller."""

    refuse_states: bool = False
    """Whether :meth:`validate` refuses the resource's new state."""

    calls: list[tuple[str, object]] = field(default_factory=list)
    sent: list[list[str]] = field(default_factory=list)

    async def groups(self, conn: Conn, principal: Principal) -> frozenset[str]:
        del conn
        self.calls.append(("groups", principal.email))
        return self.groups_of

    def read_filter(
        self,
        kind: ResourceKind,
        alias: str,
        *,
        params: list[object],
        principal: Principal,
    ) -> str:
        del kind, alias, params, principal
        self.calls.append(("read_filter", None))
        return "TRUE"

    async def check(
        self,
        conn: Conn,
        *,
        principal: Principal,
        resource: Resource,
        action: Action,
    ) -> None:
        self._called(conn, "check", (principal, resource, action))
        if self.deny_writes:
            raise AccessDeniedError("The row's write grant excludes the caller.")

    async def on_create(
        self,
        conn: Conn,
        *,
        principal: Principal | None,
        resource: Resource,
    ) -> None:
        self._called(conn, "on_create", (principal, resource))

    async def prepare_account_change(self, conn: Conn, *, resource: Resource) -> None:
        self._called(conn, "prepare_account_change", resource)

    async def validate(self, conn: Conn, *, resource: Resource) -> None:
        self._called(conn, "validate", resource)
        if self.refuse_states:
            raise ConflictError("The new account may not hold the row.")

    async def on_user(self, conn: Conn, *, email: str, role: Role) -> None:
        self._called(conn, "on_user", (email, role))

    async def prepare_audience_change(self, conn: Conn) -> None:
        self._called(conn, "prepare_audience_change", None)

    async def on_audience(
        self,
        conn: Conn,
        *,
        principal: Principal | None,
        resource: Resource,
    ) -> None:
        self._called(conn, "on_audience", (principal, resource))

    def of(self, name: str) -> list[object]:
        """Return the recorded arguments of every call to ``name``, in order."""
        return [args for called, args in self.calls if called == name]

    def _called(self, conn: Conn, name: str, args: object) -> None:
        self.calls.append((name, args))
        # A live connection keeps no record of what it was sent.
        self.sent.append(executed_sql(conn) if isinstance(conn, AsyncMock) else [])


def make_store(conn: MockConnection | None = None) -> tuple[Store, FakeEngine]:
    """Construct a ``Store`` bound to a fresh ``FakeEngine``."""
    engine = FakeEngine(conn)
    store = Store(cast(DatabaseEngine, engine), embed=StubEmbedder())
    return store, engine


def executed_sql(conn: AsyncMock) -> list[str]:
    """Return every recorded SQL statement, in call order.

    Reads ``conn.mock_calls`` so it captures statements from both ``execute``
    (data statements, simple protocol) and ``fetch`` (``tx()``'s error-path
    ``ROLLBACK``, issued over the extended protocol because pglite 0.5 mis-frames
    a simple-query control reply after an aborted statement). ``mock_calls``
    records child-mock calls in order even when a test overrides ``execute`` /
    ``fetch`` with its own ``side_effect``, so order-sensitive assertions hold.
    """
    sql: list[str] = []
    for call in conn.mock_calls:
        name, args, _kwargs = cast(
            tuple[str, tuple[object, ...], dict[str, object]],
            call,
        )
        if name in {"execute", "fetch"} and args:
            sql.append(str(args[0]))
    return sql


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


async def warm(served: FastAPI) -> None:
    """Make every route's handler now, as the process's first request would.

    FastAPI makes an included router's handlers on the app's first request to it,
    ~40 ms a router. A path no route matches is tried against every route, which
    makes them all, and it touches no database.
    """
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=served),
        base_url="http://testserver",
    ) as http:
        missed = await http.get("/api/no-route-is-here")
    if missed.status_code != 404:
        raise ValueError(missed.text)


class Hosted:
    """An app built and warmed once a session, served to each test from a fresh state.

    Each test's state holds only what the build set, plus its own engine, store and
    queue: what a request leaves there (replays, hubs, rate limits) ends with it. A
    test that brings its own deployment gets the app's routes as built, with that
    deployment's mounted after them: the built ones keep the handlers ``warm`` made,
    and nothing an earlier test mounted stays.
    """

    def __init__(self, app: FastAPI) -> None:
        self._app = app
        self._built = {key: app.state[key] for key in app.state}
        self._routes = list(app.router.routes)

    @property
    def app(self) -> FastAPI:
        """The app."""
        return self._app

    @asynccontextmanager
    async def serve(
        self,
        *,
        engine: DatabaseEngine,
        store: Store,
        deployment: Deployment | None = None,
        state: Mapping[str, object] = MappingProxyType({}),
    ) -> AsyncGenerator[httpx2.AsyncClient]:
        """Yield a client of the app, serving ``store`` from a fresh state.

        Args:
          engine: The database the app serves.
          store: The store over ``engine``.
          deployment: Mounted for this test alone; none keeps the built one.
          state: More of the app's state, such as its config, for this test.

        Yields:
          http: A client of the app.

        """
        self._app.router.routes[:] = self._routes
        self._app.state = State(
            {
                **self._built,
                "engine": engine,
                "store": store,
                "inbound": InboundQueue(),
                **state,
            },
        )
        if deployment is not None:
            attach_deployment(self._app, deployment=deployment)
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self._app, raise_app_exceptions=False),
            base_url="http://testserver",
        ) as http:
            yield http


async def restore(engine: DatabaseEngine) -> None:
    """Put ``engine``'s database back as trackinizer's bootstrap leaves it.

    The first restore of a database makes it, ~0.25 s on PGlite; every later one puts
    it back in one transaction (:class:`Baselines`), so no test sees another's writes.
    The build's record lives in the database, so every fixture holding the engine
    restores the same build, whichever package's conftest imports it.

    Args:
      engine: The engine whose database to put back.

    """
    await Baselines(engine, root=CORE, schemas=("public",)).ready(CORE)


@pytest.fixture(scope="session")
def pg_dsn(request: pytest.FixtureRequest) -> Iterator[str]:
    """Provision trackinizer's database on the session's PostgreSQL server.

    Without a PostgreSQL install or libpq, the integration tests that need it skip.
    """
    with postgres_database(request, dbname="tests") as dsn:
        yield dsn


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def integ_engine(
    pg_dsn: str,
) -> AsyncGenerator[postgres.PostgresEngine]:
    """Session-scoped Postgres engine: one asyncpg pool for every integration test.

    Per-test pool/codec setup runs ~0.3s; amortizing across the suite
    drops integration tests from seconds to milliseconds. Its schema is
    ``integ_store``'s to build.
    """
    async with postgres.PostgresEngine(
        dsn=pg_dsn,
        listen_channel=NOTIFY_CHANNEL,
    ) as engine:
        yield engine


@pytest_asyncio.fixture(loop_scope="session")
async def integ_store(integ_engine: postgres.PostgresEngine) -> Store:
    """Return a ``Store`` on the shared Postgres database, freshly bootstrapped."""
    await restore(integ_engine)
    return Store(integ_engine, embed=StubEmbedder())


@pytest.fixture(scope="session")
def pglite_extensions() -> tuple[str, ...]:
    """Trackinizer's schema declares a ``vector`` column, so pgvector loads.

    Overrides the default in ``trackinizer.lib.postgres.testing``: without it
    ``Store.bootstrap`` fails with ``extension "vector" is not available``.
    """
    return ("pgvector",)


@pytest_asyncio.fixture(loop_scope="session")
async def pglite_db(pglite_engine: PGliteEngine) -> PGliteEngine:
    """Return the shared PGlite engine, its schema freshly bootstrapped and empty."""
    await restore(pglite_engine)
    return pglite_engine


@pytest.fixture
def store(pglite_db: PGliteEngine) -> Store:
    """Return a ``Store`` on ``pglite_db``."""
    return Store(pglite_db, embed=StubEmbedder())


@pytest_asyncio.fixture(loop_scope="session")
async def pglite_rollback_conn(pglite_db: PGliteEngine) -> AsyncIterator[Conn]:
    """Yield ``pglite_db``'s connection inside a transaction rolled back after the test.

    A migration step's schema surgery ends with the test, so the next test meets the
    build instead of building it again. The test holds the engine's one connection, so
    it reaches the database through this connection alone.
    """
    async with pglite_db.acquire() as conn, rolled_back(conn):
        yield conn


@pytest_asyncio.fixture(loop_scope="session")
async def integ_rollback_conn(
    integ_engine: postgres.PostgresEngine,
    integ_store: Store,
) -> AsyncIterator[Conn]:
    """Yield a connection to ``integ_store``'s database in a transaction rolled back after.

    A migration step's schema surgery ends with the test, so the next test meets the
    build instead of building it again. Asks for ``integ_store`` only for the freshly
    bootstrapped database it leaves.
    """
    del integ_store
    async with integ_engine.acquire() as conn, rolled_back(conn):
        yield conn


@asynccontextmanager
async def rolled_back(conn: Conn) -> AsyncGenerator[None]:
    """Hold ``conn`` in a transaction for the block, then roll it back."""
    await conn.execute("BEGIN")
    try:
        yield
    finally:
        # Over the extended protocol, as ``tx``'s is: the block may end on a failed
        # statement, after which PGlite misframes a simple-query reply.
        await conn.fetch("ROLLBACK")


@asynccontextmanager
async def savepoint(conn: Conn) -> AsyncGenerator[None]:
    """Run the block in a savepoint, so a statement it fails leaves the transaction live.

    For a test in a rolled-back transaction that goes on after a refused statement.
    """
    await conn.execute("SAVEPOINT refusable")
    try:
        yield
    except BaseException:
        await conn.fetch("ROLLBACK TO SAVEPOINT refusable")
        raise
    else:
        await conn.execute("RELEASE SAVEPOINT refusable")


type Fill = Callable[
    ["DatabaseEngine", Sequence[tuple[str, str]]],
    Awaitable[Mapping[str, str]],
]
"""Runs a build's migrations and adds its rows; returns what the tests read back."""

type Shape = dict[str, set[tuple[object, ...]]]
"""A catalog's definitions, by category: relations, columns, constraints, ..."""


async def shape(conn: Conn, *, schemas: tuple[str, ...]) -> Shape:
    """Read every definition of ``schemas``, sequence positions aside.

    Args:
      conn: The database's connection.
      schemas: The schemas to read.

    Returns:
      shape: Each category's definitions, as rows.

    """
    return {
        category: {tuple(row) for row in await conn.fetch(query, list(schemas))}
        for category, query in _SHAPE.items()
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class Build:
    """A database the tests start from.

    Attributes:
      name: Names what ``fill`` adds; two fills adding different rows need two.
      fill: Runs ``migrations`` and adds the rows the tests start from; returns
        what they read back, such as each user's API key. A root build's runs
        over empty schemas, every other build's over the root build's database.
      migrations: The migrations ``fill`` runs, in order. A build whose
        migrations begin with another's is made from that one's database.

    """

    name: str
    fill: Fill
    migrations: tuple[tuple[str, str], ...] = ()

    @property
    def key(self) -> str:
        """Names the database: two builds with one key leave the same one."""
        named = "".join(f"{name}\n{body}\n" for name, body in self.migrations)
        return f"{self.name}:" + hashlib.sha256(named.encode()).hexdigest()


async def bootstrapped(
    engine: DatabaseEngine,
    migrations: Sequence[tuple[str, str]],
) -> Mapping[str, str]:
    """Run ``migrations`` as a server's first start does, and add no rows.

    The operator's bootstrap settings are blanked for it, so no admin is seeded.
    """
    with without_operator_bootstrap():
        await Store(engine, embed=StubEmbedder()).bootstrap(addon_migrations=migrations)
    return dict[str, str]()


@contextmanager
def without_operator_bootstrap() -> Generator[None]:
    """Blank the operator's bootstrap settings for the block, so no admin is seeded."""
    # Empty values also keep subprocess conftests from reloading operator secrets.
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(BOOTSTRAP_ADMIN_ENV, "")
        patch.setenv(BOOTSTRAP_TOKEN_FILE_ENV, "")
        yield


CORE: Final = Build(name="core", fill=bootstrapped)
"""Trackinizer's schema, bootstrapped with no addon, and no rows: the usual root."""


class Baselines[E: DatabaseEngine]:
    """Each build's database as it was made, put back before every test that asks.

    Args:
      engine: The database every build is made and put back in.
      root: The build every other is made over.
      schemas: The schemas the builds fill. ``public`` is emptied, and every other
        dropped, before the root build is made; a build's rows are those of these
        schemas' tables.

    """

    def __init__(self, engine: E, *, root: Build, schemas: tuple[str, ...]) -> None:
        self._engine = engine
        self._root = root
        self._schemas = schemas
        self._shapes: dict[str, Shape] = {}

    @property
    def engine(self) -> E:
        """The database every build is put back in."""
        return self._engine

    async def shape(self, build: Build) -> Shape:
        """Return ``build``'s catalog, every definition of it, read once a session.

        Args:
          build: The database whose catalog to read; it is put back first.

        Returns:
          shape: Each category's definitions, as :func:`shape` reads them.

        """
        found = self._shapes.get(build.key)
        if found is None:
            await self.ready(build)
            async with self._engine.acquire() as conn:
                found = await shape(conn, schemas=self._schemas)
            self._shapes[build.key] = found
        return found

    async def ready(self, build: Build) -> Mapping[str, str]:
        """Put the database back as ``build`` made it, making it if need be.

        Args:
          build: The database the test starts from.

        Returns:
          keys: What the build's fill returned.

        """
        name = _digest(build.key, *self._schemas)
        async with self._engine.acquire() as conn:
            baseline = await _recorded(conn, name=name)
        if baseline is not None:
            found = await self._restored(baseline)
            if found is None:
                return baseline.keys
            if build.key != self._root.key:
                await self._extend(build, catalog=found)
                if await self._restored(baseline) is None:
                    return baseline.keys
        if build.key == self._root.key:
            await reset_schema(self._engine)
            async with self._engine.acquire() as conn:
                for schema in self._schemas:
                    if schema != "public":
                        await _drop(conn, schema)
        else:
            await self.ready(self._root)
        keys = await build.fill(self._engine, build.migrations)
        async with self._engine.acquire() as conn:
            await _record(
                conn,
                name=name,
                managed=self._schemas,
                migrations=build.migrations,
                baseline=await _copy(
                    conn,
                    prefix=name,
                    keys=keys,
                    schemas=self._schemas,
                ),
            )
        return keys

    # The build with the most of ``build``'s first migrations whose catalog is the
    # database's is put back, and the rest of the migrations run over it; with no such
    # build, the root build's is.
    async def _extend(self, build: Build, *, catalog: str) -> None:
        """Give the database ``build``'s catalog from the database's own ``catalog``."""
        start: _Baseline | None = None
        steps, longest = build.migrations, 0
        async with self._engine.acquire() as conn:
            made = await _made(conn, managed=self._schemas, catalog=catalog)
        for prefix, baseline in made:
            if (
                prefix
                # Two equally long prefixes with one catalog leave the same database.
                and len(prefix) > longest  # pragma: no mutate -- ties tie.
                and build.migrations[: len(prefix)] == prefix
            ):
                start, steps = baseline, build.migrations[len(prefix) :]
                longest = len(prefix)
        if start is None or await self._restored(start) is not None:
            await self.ready(self._root)
            steps = build.migrations
        async with self._engine.acquire() as conn:
            for _name, body in steps:
                await conn.execute(body)

    async def _restored(self, baseline: _Baseline) -> str | None:
        """Put ``baseline``'s rows back if the catalog is its; else return the catalog."""
        async with self._engine.acquire() as conn:
            # A build's migrations may add a schema its root lacks; dropping it puts
            # the root's catalog back without making the root again.
            for schema in self._schemas:
                if schema not in baseline.schemas:
                    await _drop(conn, schema)
            catalog = str(await conn.fetchval(_CATALOG, list(self._schemas)))
            copied = await conn.fetchval(_COPIED, _COPIES, list(baseline.copies))
            if (catalog, copied) != (baseline.catalog, baseline.copied):
                return catalog
            async with conn.transaction():
                await conn.execute(baseline.reset)
        return None


_COPIES: Final = "test_baselines"
"""The schema holding each build's copy of every table it filled."""

_SHAPE: Final = {
    "relations": "SELECT n.nspname, c.relname, c.relkind::text "
    "FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace "
    "WHERE n.nspname = ANY($1::text[]) AND c.relkind IN ('r', 'p', 'v', 'm', 'S')",
    "columns": "SELECT n.nspname, c.relname, a.attname, "
    "format_type(a.atttypid, a.atttypmod), a.attnotnull, a.attidentity::text, "
    "a.attgenerated::text, pg_get_expr(d.adbin, d.adrelid) "
    "FROM pg_attribute AS a JOIN pg_class AS c ON c.oid = a.attrelid "
    "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
    "LEFT JOIN pg_attrdef AS d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
    "WHERE n.nspname = ANY($1::text[]) AND a.attnum > 0 AND NOT a.attisdropped "
    "AND c.relkind IN ('r', 'p', 'v', 'm')",
    "constraints": "SELECT n.nspname, c.relname, x.conname, "
    "pg_get_constraintdef(x.oid), x.convalidated "
    "FROM pg_constraint AS x JOIN pg_class AS c ON c.oid = x.conrelid "
    "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
    "WHERE n.nspname = ANY($1::text[])",
    "indexes": "SELECT schemaname, tablename, indexname, indexdef FROM pg_indexes "
    "WHERE schemaname = ANY($1::text[])",
    "sequences": "SELECT schemaname, sequencename, data_type::text, start_value, "
    "min_value, max_value, increment_by, cycle, cache_size FROM pg_sequences "
    "WHERE schemaname = ANY($1::text[])",
    "views": "SELECT schemaname, viewname, definition FROM pg_views "
    "WHERE schemaname = ANY($1::text[])",
    "triggers": "SELECT n.nspname, c.relname, t.tgname, pg_get_triggerdef(t.oid) "
    "FROM pg_trigger AS t JOIN pg_class AS c ON c.oid = t.tgrelid "
    "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
    "WHERE n.nspname = ANY($1::text[]) AND NOT t.tgisinternal",
    "functions": "SELECT n.nspname, p.proname, pg_get_functiondef(p.oid) "
    "FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid = p.pronamespace "
    "WHERE n.nspname = ANY($1::text[]) AND p.prokind <> 'a'",
}
"""Each category of :data:`Shape`, and the query reading it."""

# Every relation, column and its default, index, view, function body, trigger and
# constraint of the managed schemas. A migration, a dropped column or a reset changes
# it, and the copied rows then no longer fit the tables; a changed default would reach
# every later test's rows. Types go by name: a reset drops pgvector with ``public``,
# and the bootstrap after it makes ``vector`` again under a new oid. An index goes by
# its definition, not its own columns, which keep the names they had when it was made:
# a renamed table column leaves them behind. So is every view and foreign key of
# another schema over one of their tables: another package's tests leave their schema
# on a shared engine, and a view of it over a table refuses a migration test's change
# to it. A copy of a table hangs on no table and counts for nothing.
_CATALOG: Final = (
    "SELECT md5(coalesce(string_agg(item, ',' ORDER BY item), '')) FROM ("
    " SELECT n.nspname || '.' || c.relname || ':' || c.relkind::text || ':' ||"
    " coalesce(a.attname || ':' || format_type(a.atttypid, a.atttypmod) || ':' ||"
    " a.attnotnull::text || ':' || coalesce(pg_get_expr(d.adbin, d.adrelid), ''), '')"
    " AS item FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
    " LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND c.relkind <> 'i'"
    " AND a.attnum > 0 AND NOT a.attisdropped"
    " LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum"
    " WHERE n.nspname = ANY($1::text[])"
    " UNION ALL SELECT n.nspname || '.' || c.relname || ':' || md5(pg_get_indexdef(c.oid))"
    " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE n.nspname = ANY($1::text[]) AND c.relkind = 'i'"
    " UNION ALL SELECT n.nspname || '.' || c.relname || ':' || md5(pg_get_viewdef(c.oid))"
    " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE n.nspname = ANY($1::text[]) AND c.relkind IN ('v', 'm')"
    " UNION ALL SELECT n.nspname || '.' || p.proname || ':' || md5(p.prosrc)"
    " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
    " WHERE n.nspname = ANY($1::text[])"
    " UNION ALL SELECT n.nspname || '.' || c.relname || '.' || t.tgname || ':' ||"
    " t.tgfoid::regproc::text"
    " FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid"
    " JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE n.nspname = ANY($1::text[]) AND NOT t.tgisinternal"
    " UNION ALL SELECT n.nspname || '.' || co.conname || ':' ||"
    " md5(pg_get_constraintdef(co.oid))"
    " FROM pg_constraint co JOIN pg_namespace n ON n.oid = co.connamespace"
    " WHERE n.nspname = ANY($1::text[])"
    " UNION ALL SELECT DISTINCT 'over ' || vn.nspname || '.' || v.relname"
    " FROM pg_class v JOIN pg_namespace vn ON vn.oid = v.relnamespace"
    " JOIN pg_rewrite r ON r.ev_class = v.oid"
    " JOIN pg_depend dep ON dep.classid = 'pg_rewrite'::regclass AND dep.objid = r.oid"
    " JOIN pg_class c ON c.oid = dep.refobjid"
    " JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE v.relkind IN ('v', 'm')"
    " AND NOT vn.nspname = ANY($1::text[])"
    " AND vn.nspname NOT IN ('pg_catalog', 'information_schema')"
    " AND dep.refclassid = 'pg_class'::regclass AND n.nspname = ANY($1::text[])"
    " UNION ALL SELECT 'over ' || cn.nspname || '.' || co.conname"
    " FROM pg_constraint co JOIN pg_namespace cn ON cn.oid = co.connamespace"
    " JOIN pg_class c ON c.oid = co.confrelid"
    " JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE co.contype = 'f' AND n.nspname = ANY($1::text[])"
    " AND NOT cn.nspname = ANY($1::text[])"
    ") items"
)

# Each column of the copies ``$2`` in schema ``$1``. Dropping an extension's type drops
# the columns of it in the copies too, and their rows can no longer come back.
_COPIED: Final = (
    "SELECT md5(coalesce(string_agg(c.relname || ':' || a.attname || ':' ||"
    " format_type(a.atttypid, a.atttypmod), ',' ORDER BY c.relname, a.attnum), ''))"
    " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
    " JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped"
    " WHERE n.nspname = $1 AND c.relname = ANY($2::text[])"
)


_MAKE_COPIES: Final = f"CREATE SCHEMA IF NOT EXISTS {_COPIES}"

_RECORDS: Final = f"{_COPIES}.record"
"""Each build's record, by the digest naming its copies."""

_MAKE_RECORDS: Final = (
    f"CREATE TABLE IF NOT EXISTS {_RECORDS} (name TEXT PRIMARY KEY, managed TEXT[], "
    "steps TEXT[], bodies TEXT[], catalog TEXT, copies TEXT[], copied TEXT, "
    "reset TEXT, key_names TEXT[], key_values TEXT[], schemas TEXT[])"
)

_SAVE_RECORD: Final = vetted_sql(
    "INSERT INTO ",
    _RECORDS,
    " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) "
    "ON CONFLICT (name) DO UPDATE SET managed = $2, steps = $3, bodies = $4, "
    "catalog = $5, copies = $6, copied = $7, reset = $8, key_names = $9, "
    "key_values = $10, schemas = $11",
)

_RECORDS_EXIST: Final = vetted_sql("SELECT to_regclass('", _RECORDS, "')")

_READ_RECORD: Final = vetted_sql("SELECT * FROM ", _RECORDS, " WHERE name = $1")

_MADE: Final = vetted_sql(
    "SELECT * FROM ",
    _RECORDS,
    " WHERE managed = $1::text[] AND catalog = $2",
)

_PREFIX: Final = 8
"""Hex digits of a build's digest naming its copies: unique, and under 63 bytes."""

_PUT_BACK: Final = (
    "INSERT INTO {table} ({columns}) OVERRIDING SYSTEM VALUE "
    "SELECT {columns} FROM {copy}"
)

_TABLES: Final = (
    "SELECT schemaname, tablename FROM pg_tables "
    "WHERE schemaname = ANY($1::text[]) ORDER BY schemaname, tablename"
)

_COLUMNS: Final = (
    "SELECT a.attname FROM pg_attribute a "
    "JOIN pg_class c ON c.oid = a.attrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = $1 AND c.relname = $2 AND a.attnum > 0 "
    "AND NOT a.attisdropped AND a.attgenerated = '' ORDER BY a.attnum"
)
"""Each copyable column of table ``$2`` in schema ``$1``: a generated one is computed."""

_SEQUENCES: Final = (
    "SELECT schemaname, sequencename FROM pg_sequences "
    "WHERE schemaname = ANY($1::text[])"
)


_APPROXIMATE: Final = (
    "SELECT n.nspname, t.relname FROM pg_index x "
    "JOIN pg_class i ON i.oid = x.indexrelid "
    "JOIN pg_am am ON am.oid = i.relam "
    "JOIN pg_class t ON t.oid = x.indrelid "
    "JOIN pg_namespace n ON n.oid = t.relnamespace "
    "WHERE n.nspname = ANY($1::text[]) AND am.amname IN ('hnsw', 'ivfflat')"
)

_RESET_START: Final = ("SET LOCAL session_replication_role = replica", "DISCARD TEMP")
"""Replica role skips foreign keys and triggers: the tables empty in any order, and the
copied rows come back exactly as the build left them. A test's temporary table is no
part of the build either."""

_EMPTY: Final = {True: "TRUNCATE {table}", False: "DELETE FROM {table}"}
"""How a table is emptied, by whether an approximate index covers it."""


@dataclass(frozen=True, slots=True, kw_only=True)
class _Baseline:
    """One build's database as it was made.

    Attributes:
      catalog: The catalog the build left, which the copied rows fit.
      copies: The tables in ``_COPIES`` holding the build's rows.
      copied: The columns of ``copies`` as they were made.
      reset: One script clearing every managed table, then copying the rows and
        the sequence positions back.
      keys: What the build's fill returned.
      schemas: The managed schemas the build left.

    """

    catalog: str
    copies: tuple[str, ...]
    copied: str
    reset: str
    keys: Mapping[str, str]
    schemas: tuple[str, ...]


async def _copy(
    conn: Conn,
    *,
    prefix: str,
    keys: Mapping[str, str],
    schemas: tuple[str, ...],
) -> _Baseline:
    """Copy every filled table and every sequence position; return how to restore them."""
    await conn.execute(_MAKE_COPIES)
    tables = [
        (str(row["schemaname"]), str(row["tablename"]))
        for row in await conn.fetch(
            _TABLES,
            list(schemas),
        )
    ]
    approximate = await _approximately_indexed(conn, schemas=schemas)
    script: list[str] = [*_RESET_START]
    script.extend(
        _EMPTY[table in approximate].format(table=_name(table)) for table in tables
    )
    copies: list[str] = []
    for table in tables:
        filled = await conn.fetchval(
            vetted_sql(
                "SELECT EXISTS (SELECT 1 FROM ",  # pragma: no mutate -- SQL ignores keyword case.
                _name(table),
                ")",  # pragma: no mutate -- a word after the table only aliases it.
            ),
        )
        if not filled:
            continue
        copy = f"{prefix}__{table[0]}__{table[1]}"
        target = _name((_COPIES, copy))
        await conn.execute(
            vetted_sql(
                "DROP TABLE IF EXISTS ",  # pragma: no mutate -- SQL ignores keyword case.
                target,
            ),
        )
        await conn.execute(
            vetted_sql(
                "CREATE TABLE ",  # pragma: no mutate -- SQL ignores keyword case.
                target,
                " AS SELECT * FROM ",  # pragma: no mutate -- SQL ignores keyword case.
                _name(table),
            ),
        )
        # A generated column takes no value: it is computed again from the rest.
        columns = ", ".join(
            _quoted(str(row["attname"])) for row in await conn.fetch(_COLUMNS, *table)
        )
        script.append(
            _PUT_BACK.format(table=_name(table), columns=columns, copy=target),
        )
        copies.append(copy)
    for row in await conn.fetch(_SEQUENCES, list(schemas)):
        sequence = _name((str(row["schemaname"]), str(row["sequencename"])))
        # Read from the sequence itself: ``pg_sequences.last_value`` is null for one
        # never read, whose position is its start, which ``setval`` must be told.
        last = await conn.fetchval(
            vetted_sql(
                "SELECT last_value FROM ",  # pragma: no mutate -- SQL ignores keyword case.
                sequence,
            ),
        )
        called = await conn.fetchval(
            vetted_sql(
                "SELECT is_called FROM ",  # pragma: no mutate -- SQL ignores keyword case.
                sequence,
            ),
        )
        script.append(
            vetted_sql(
                "SELECT setval('",  # pragma: no mutate -- SQL ignores keyword case.
                sequence.replace("'", "''"),
                "', ",
                str(cast(int, last)),
                ", ",
                "true" if called else "false",  # pragma: no mutate -- SQL ignores case.
                ")",
            ),
        )
    present = await conn.fetch(
        "SELECT nspname FROM pg_namespace WHERE nspname = ANY($1::text[])",  # pragma: no mutate -- SQL ignores keyword case.
        list(schemas),
    )
    return _Baseline(
        catalog=str(await conn.fetchval(_CATALOG, list(schemas))),
        copies=tuple(copies),
        copied=str(await conn.fetchval(_COPIED, _COPIES, copies)),
        reset=";\n".join(script),
        keys=dict(keys),
        schemas=tuple(str(row["nspname"]) for row in present),
    )


# A deleted row stays in its table's indexes until a vacuum, and nothing vacuums these
# databases. An exact index (btree, GIN, GiST) rechecks each entry and answers the same;
# an approximate one spends its scan on the dead entries. The HNSW index offered every
# earlier test's embeddings to a nearest-neighbour search, which from about the 330th
# test stopped reaching a live row. TRUNCATE drops a table's index entries with its
# rows, at ~2 ms a table on PGlite: every table would take a suite from 55 s to 210 s.
async def _approximately_indexed(
    conn: Conn,
    *,
    schemas: tuple[str, ...],
) -> set[tuple[str, str]]:
    """Return the managed tables under a pgvector approximate index."""
    return {
        (str(row["nspname"]), str(row["relname"]))
        for row in await conn.fetch(_APPROXIMATE, list(schemas))
    }


async def _drop(conn: Conn, schema: str) -> None:
    """Drop ``schema`` and everything in it, if it is there."""
    await conn.execute(
        vetted_sql(
            "DROP SCHEMA IF EXISTS ",  # pragma: no mutate -- SQL ignores keyword case.
            _quoted(schema),
            " CASCADE",  # pragma: no mutate -- SQL ignores keyword case.
        ),
    )


async def _record(
    conn: Conn,
    *,
    name: str,
    managed: tuple[str, ...],
    migrations: tuple[tuple[str, str], ...],
    baseline: _Baseline,
) -> None:
    """Keep ``baseline`` as build ``name``'s, made over ``managed`` by ``migrations``."""
    await conn.execute(_MAKE_RECORDS)
    await conn.execute(
        _SAVE_RECORD,
        name,
        list(managed),
        [step for step, _ in migrations],
        [body for _, body in migrations],
        baseline.catalog,
        list(baseline.copies),
        baseline.copied,
        baseline.reset,
        list(baseline.keys),
        list(baseline.keys.values()),
        list(baseline.schemas),
    )


async def _recorded(conn: Conn, *, name: str) -> _Baseline | None:
    """Return build ``name``'s record, or ``None`` when it was never made here."""
    if await conn.fetchval(_RECORDS_EXIST) is None:
        return None
    row = await conn.fetchrow(_READ_RECORD, name)
    return None if row is None else _baseline(row)


async def _made(
    conn: Conn,
    *,
    managed: tuple[str, ...],
    catalog: str,
) -> list[tuple[tuple[tuple[str, str], ...], _Baseline]]:
    """Return each build over ``managed`` whose catalog is ``catalog``, by its migrations."""
    return [
        (
            tuple(
                zip(
                    cast(list[str], row["steps"]),
                    cast(list[str], row["bodies"]),
                    strict=True,
                ),
            ),
            _baseline(row),
        )
        for row in await conn.fetch(_MADE, list(managed), catalog)
    ]


def _baseline(row: asyncpg.Record) -> _Baseline:
    """Read a build's record from its row."""
    return _Baseline(
        catalog=str(row["catalog"]),
        copies=tuple(cast(list[str], row["copies"])),
        copied=str(row["copied"]),
        reset=str(row["reset"]),
        keys=dict(
            zip(
                cast(list[str], row["key_names"]),
                cast(list[str], row["key_values"]),
                strict=True,
            ),
        ),
        schemas=tuple(cast(list[str], row["schemas"])),
    )


def _digest(*parts: str) -> str:
    """Return a short digest of ``parts`` that names one build's copies."""
    joined = "\n".join(parts)  # pragma: no mutate -- any separator tells builds apart.
    return hashlib.sha256(joined.encode()).hexdigest()[:_PREFIX]


def _name(table: tuple[str, str]) -> str:
    """Return a schema-qualified name, each part quoted."""
    return f"{_quoted(table[0])}.{_quoted(table[1])}"


def _quoted(name: str) -> str:
    """Return ``name`` as a quoted SQL identifier."""
    return '"' + name.replace('"', '""') + '"'
