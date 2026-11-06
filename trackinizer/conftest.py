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
  database; :func:`restore_pglite` does it for any fixture holding the shared
  PGlite engine, whichever package imports it. Tests using them must declare
  ``@pytest.mark.asyncio(loop_scope="session")``: the engines live on the
  session loop, so per-test loops can't drive them;
* ``pglite_rollback_conn`` and ``integ_rollback_conn``, a connection to either
  build inside a transaction rolled back after the test, for a migration step's
  schema surgery, and ``savepoint`` for a statement such a test expects refused.
"""

from __future__ import annotations

from collections.abc import (
    Callable,
)
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Final, Literal, Self, cast
from unittest.mock import AsyncMock, MagicMock

import uuid

from treekle import from_plain

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
from trackinizer.server.auth import BOOTSTRAP_ADMIN_ENV, BOOTSTRAP_TOKEN_FILE_ENV
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.notify import NOTIFY_CHANNEL, tx
from trackinizer.server.store.core import Store
from trackinizer.server.values import vetted_sql
from trackinizer.types.errors import ConflictError


if TYPE_CHECKING:
    from collections.abc import (
        AsyncGenerator,
        AsyncIterator,
        Awaitable,
        Generator,
        Iterator,
    )
    from contextlib import AbstractAsyncContextManager

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
    with _without_operator_bootstrap():
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


_COPIES: Final = "trackinizer_baseline"
"""The schema holding the build's copy of each table it filled."""

_BUILD: Final = "trackinizer_build"
"""The schema holding the build's record, apart from copies named as tables are."""

# Every relation, column and its default, index, view, function body, trigger and
# constraint of schema ``public``. A migration, a dropped column or a reset changes it,
# and the copied rows then no longer fit the tables; a changed default would reach
# every later test's rows. Types go by name: a reset drops pgvector with ``public``,
# and the bootstrap after it makes ``vector`` again under a new oid. An index goes by
# its definition, not its own columns, which keep the names they had when it was
# made: a renamed table column leaves them behind. So is every view and foreign key
# of another schema over one of its tables: another package's tests leave their schema
# on the shared engine, and a view of it over ``inquiries`` refuses a migration test's
# change to the table. The reset drops them with ``public``; a copy of a table, as
# knowaccess keeps its builds', hangs on no table and counts for nothing.
_CATALOG: Final = (
    "SELECT md5(coalesce(string_agg(item, ',' ORDER BY item), '')) FROM ("
    " SELECT c.relname || ':' || c.relkind::text || ':' || coalesce(a.attname || ':'"
    " || format_type(a.atttypid, a.atttypmod) || ':' || a.attnotnull::text || ':'"
    " || coalesce(pg_get_expr(d.adbin, d.adrelid), ''), '')"
    " AS item FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
    " LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND c.relkind <> 'i'"
    " AND a.attnum > 0 AND NOT a.attisdropped"
    " LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum"
    " WHERE n.nspname = 'public'"
    " UNION ALL SELECT c.relname || ':' || md5(pg_get_indexdef(c.oid))"
    " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE n.nspname = 'public' AND c.relkind = 'i'"
    " UNION ALL SELECT c.relname || ':' || md5(pg_get_viewdef(c.oid))"
    " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE n.nspname = 'public' AND c.relkind IN ('v', 'm')"
    " UNION ALL SELECT p.proname || ':' || md5(p.prosrc)"
    " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
    " WHERE n.nspname = 'public'"
    " UNION ALL SELECT c.relname || '.' || t.tgname || ':' || t.tgfoid::regproc::text"
    " FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid"
    " JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE n.nspname = 'public' AND NOT t.tgisinternal"
    " UNION ALL SELECT co.conname || ':' || md5(pg_get_constraintdef(co.oid))"
    " FROM pg_constraint co JOIN pg_namespace n ON n.oid = co.connamespace"
    " WHERE n.nspname = 'public'"
    " UNION ALL SELECT DISTINCT 'over ' || vn.nspname || '.' || v.relname"
    " FROM pg_class v JOIN pg_namespace vn ON vn.oid = v.relnamespace"
    " JOIN pg_rewrite r ON r.ev_class = v.oid"
    " JOIN pg_depend dep ON dep.classid = 'pg_rewrite'::regclass AND dep.objid = r.oid"
    " JOIN pg_class c ON c.oid = dep.refobjid"
    " JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE v.relkind IN ('v', 'm')"
    " AND vn.nspname NOT IN ('public', 'pg_catalog', 'information_schema')"
    " AND dep.refclassid = 'pg_class'::regclass AND n.nspname = 'public'"
    " UNION ALL SELECT 'over ' || cn.nspname || '.' || co.conname"
    " FROM pg_constraint co JOIN pg_namespace cn ON cn.oid = co.connamespace"
    " JOIN pg_class c ON c.oid = co.confrelid"
    " JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE co.contype = 'f' AND n.nspname = 'public' AND cn.nspname <> 'public'"
    ") items"
)


async def restore(
    engine: DatabaseEngine,
    *,
    empty: Callable[[], Awaitable[None]],
) -> None:
    """Put ``engine``'s database back as trackinizer's bootstrap leaves it.

    The first restore builds it: the schema emptied, then bootstrapped without the
    operator's bootstrap settings, ~0.25 s on PGlite, and its rows copied aside. The
    build's record lives in the database beside the copies, so every fixture holding the
    engine restores the same build, whichever package's conftest imports it. Every
    later restore puts it back in one transaction: every table of schema ``public`` is
    cleared, the build's rows and sequence positions are copied back, and the session's
    temporary tables are discarded, so no test sees another's writes. A test that
    changes the catalog (a migration, a dropped column, a reset, another schema's view
    or foreign key over a table) leaves a database the copied rows no longer fit, so
    the next restore builds again. Session settings,
    advisory locks and listeners are not covered.

    Args:
      engine: The engine whose database to put back.
      empty: Drops every table of ``engine``, so the bootstrap after it meets a fresh
        database.

    """
    async with engine.acquire() as conn:
        if await _restored(conn):
            return
    await empty()
    with _without_operator_bootstrap():
        await Store(engine, embed=StubEmbedder()).bootstrap()
    async with engine.acquire() as conn, tx(conn):
        await _copy(conn)


async def restore_pglite(engine: PGliteEngine) -> None:
    """Put the shared PGlite ``engine``'s database back as the build left it."""
    await restore(engine, empty=partial(reset_schema, engine))


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
    await restore(integ_engine, empty=partial(_emptied, integ_engine))
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
    await restore_pglite(pglite_engine)
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


@contextmanager
def _without_operator_bootstrap() -> Generator[None]:
    """Blank the operator's bootstrap settings for the block, so no admin is seeded."""
    # Empty values also prevent subprocess conftests from reloading operator secrets.
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(BOOTSTRAP_ADMIN_ENV, "")
        patch.setenv(BOOTSTRAP_TOKEN_FILE_ENV, "")
        yield


async def _emptied(engine: postgres.PostgresEngine) -> None:
    """Drop every table of the Postgres database, as ``reset_schema`` does PGlite's."""
    async with engine.acquire() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")


async def _restored(conn: Conn) -> bool:
    """Put the build's rows back if the catalog is still the build's; say whether."""
    record = await conn.fetchval(
        vetted_sql("SELECT to_regclass('", _BUILD, ".record')"),
    )
    if record is None:
        return False
    found = await conn.fetchrow(
        vetted_sql(
            "SELECT b.reset, b.catalog = (",
            _CATALOG,
            ") AND b.copied = (",
            _copied("b.copies"),
            ") AS fits FROM ",
            _BUILD,
            ".record b",
        ),
    )
    if found is None:
        raise ValueError("Expected found is not None.")
    if not from_plain(found["fits"], bool):
        return False
    async with tx(conn):
        await conn.execute(from_plain(found["reset"], str))
    return True


# Dropping pgvector drops the columns of its type in the copies too, and their rows can
# no longer come back.
def _copied(copies: str) -> str:
    """Return a query digesting each column of the copies the array ``copies`` names."""
    return vetted_sql(
        "SELECT md5(coalesce(string_agg(c.relname || ':' || a.attname || ':' ||",
        " format_type(a.atttypid, a.atttypmod), ',' ORDER BY c.relname, a.attnum), ''))",
        " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace",
        " JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0",
        " AND NOT a.attisdropped WHERE n.nspname = '",
        _COPIES,
        "' AND c.relname = ANY(",
        copies,
        ")",
    )


async def _copy(conn: Conn) -> None:
    """Copy every filled table and every sequence position, and record how to restore them."""
    await conn.execute(
        vetted_sql(
            "DROP SCHEMA IF EXISTS ",
            _COPIES,
            " CASCADE; CREATE SCHEMA ",
            _COPIES,
            "; DROP SCHEMA IF EXISTS ",
            _BUILD,
            " CASCADE; CREATE SCHEMA ",
            _BUILD,
            "; CREATE TABLE ",
            _BUILD,
            ".record (catalog TEXT, copies TEXT[], copied TEXT, reset TEXT)",
        ),
    )
    tables = [
        str(row["tablename"])
        for row in await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "ORDER BY tablename",
        )
    ]
    # Replica role skips foreign keys and triggers: the tables empty in any order,
    # and the copied rows come back exactly as the build left them. A test's
    # temporary table is no part of the build either.
    script = ["SET LOCAL session_replication_role = replica", "DISCARD TEMP"]
    approximate = await _approximately_indexed(conn)
    script.extend(
        vetted_sql("DELETE FROM public.", _quoted(table))
        for table in tables
        if table not in approximate
    )
    script.extend(
        vetted_sql("TRUNCATE public.", _quoted(table)) for table in sorted(approximate)
    )
    copies: list[str] = []
    for table in tables:
        source = vetted_sql("public.", _quoted(table))
        if not await conn.fetchval(
            vetted_sql("SELECT EXISTS (SELECT 1 FROM ", source, ")"),
        ):
            continue
        target = vetted_sql(_COPIES, ".", _quoted(table))
        await conn.execute(
            vetted_sql("CREATE TABLE ", target, " AS SELECT * FROM ", source),
        )
        # A generated column takes no value: it is computed again from the rest.
        columns = ", ".join(
            _quoted(str(row["attname"]))
            for row in await conn.fetch(
                "SELECT a.attname FROM pg_attribute a "
                "JOIN pg_class c ON c.oid = a.attrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = $1 AND a.attnum > 0 "
                "AND NOT a.attisdropped AND a.attgenerated = '' ORDER BY a.attnum",
                table,
            )
        )
        script.append(
            vetted_sql(
                "INSERT INTO ",
                source,
                " (",
                columns,
                ") OVERRIDING SYSTEM VALUE SELECT ",
                columns,
                " FROM ",
                target,
            ),
        )
        copies.append(table)
    for row in await conn.fetch(
        "SELECT sequencename FROM pg_sequences WHERE schemaname = 'public'",
    ):
        sequence = vetted_sql("public.", _quoted(str(row["sequencename"])))
        position = await conn.fetchrow(
            vetted_sql("SELECT last_value, is_called FROM ", sequence),
        )
        if position is None:
            raise ValueError("Expected position is not None.")
        script.append(
            vetted_sql(
                "SELECT setval('",
                sequence.replace("'", "''"),
                "', ",
                str(from_plain(position["last_value"], int)),
                ", ",
                "true" if position["is_called"] else "false",
                ")",
            ),
        )
    await conn.execute(
        vetted_sql(
            "INSERT INTO ",
            _BUILD,
            ".record SELECT (",
            _CATALOG,
            "), $1::text[], (",
            _copied("$1::text[]"),
            "), $2",
        ),
        copies,
        ";\n".join(script),
    )


# A deleted row stays in its table's indexes until a vacuum, and nothing vacuums these
# databases. An exact index (btree, GIN, GiST) rechecks each entry and answers the same;
# an approximate one spends its scan on the dead entries. The HNSW index offered every
# earlier test's embeddings to a nearest-neighbour search, which from about the 330th
# test stopped reaching a live row. TRUNCATE drops a table's index entries with its
# rows, at ~2 ms a table on PGlite: every table would take its suite from 55 s to 210 s.
async def _approximately_indexed(conn: Conn) -> set[str]:
    """Return the tables of schema ``public`` under a pgvector approximate index."""
    return {
        str(row["relname"])
        for row in await conn.fetch(
            "SELECT t.relname FROM pg_index x "
            "JOIN pg_class i ON i.oid = x.indexrelid "
            "JOIN pg_am am ON am.oid = i.relam "
            "JOIN pg_class t ON t.oid = x.indrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE n.nspname = 'public' AND am.amname IN ('hnsw', 'ivfflat')",
        )
    }


def _quoted(name: str) -> str:
    """Return ``name`` as a quoted SQL identifier."""
    return '"' + name.replace('"', '""') + '"'
