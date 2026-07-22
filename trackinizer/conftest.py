"""Shared fixtures and helpers for trackinizer test modules.

Tests across ``trackinizer_test.py`` and friends share:

* mock-based unit testing helpers (``make_conn`` / ``FakeEngine`` /
  ``make_store`` / ``executed_sql`` / ``new_uuid``), imported explicitly;
* session-scoped Postgres DSN and engine plus a per-test ``integ_store``
  fixture (``@pytest.mark.integration``). Tests using ``integ_store``
  must declare ``@pytest.mark.asyncio(loop_scope="session")``: the
  engine and its asyncpg pool live on the session loop, so per-test
  loops can't drive it.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Self, cast
from unittest.mock import AsyncMock, MagicMock

import uuid

from pytest_postgresql.exceptions import ExecutableMissingException
from pytest_postgresql.janitor import DatabaseJanitor

import pytest
import pytest_asyncio


if TYPE_CHECKING:
    from pytest_postgresql.executor import PostgreSQLExecutor

from trackinizer.lib import postgres
from trackinizer.lib.postgres import DatabaseEngine
from trackinizer.server.notify import NOTIFY_CHANNEL
from trackinizer.server.store.core import Store, StubEmbedder


def make_conn() -> AsyncMock:
    """Build an ``AsyncMock`` connection with permissive default returns.

    ``fetchrow`` dispatches: SQL matching ``emit_change``'s cost-update
    ``UPDATE inquiries ... RETURNING ...`` is auto-synthesized so individual
    tests never have to interleave cost rows into their field-read scripts;
    everything else flows through a per-mock queue / default that tests
    drive via :func:`set_field_row` and :func:`queue_field_rows`.
    """
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    field_default: list[Any] = [None]
    field_queue: list[Any] = []

    async def fetchrow(sql: str, *args: Any) -> Any:
        if "marginal_cost_agent_usd" in sql and "RETURNING" in sql:
            agent = float(args[0]) if args else 0.0
            resource = float(args[1]) if len(args) > 1 else 0.0
            old_agent = max(0.0, -agent)
            old_resource = max(0.0, -resource)
            subject_id = args[2] if len(args) > 2 else uuid.uuid4()
            return {
                "existing_id": subject_id,
                "old_agent": old_agent,
                "old_resource": old_resource,
                "new_agent": old_agent + agent,
                "new_resource": old_resource + resource,
                "current_subscribers": [],
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


def set_field_row(conn: AsyncMock, row: Any | None) -> None:
    """Set the default field-read row returned by ``conn.fetchrow``."""
    cast(list[Any], conn.field_default)[0] = row


def queue_field_rows(conn: AsyncMock, *rows: Any) -> None:
    """Queue field-read rows; ``conn.fetchrow`` pops one per call."""
    cast(list[Any], conn.field_queue).extend(rows)


class FakeEngine:
    """``DatabaseEngine`` Protocol stub backed by one ``AsyncMock`` connection."""

    def __init__(self, conn: AsyncMock | None = None) -> None:
        self.conn: AsyncMock = conn or make_conn()
        self.notify_calls: list[tuple[str, str]] = []
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

    def acquire(self) -> Any:
        # Model the real single-connection substrate: a nested acquire while
        # one is already held would deadlock on PGlite, so it must raise here
        # too -- otherwise reentrancy bugs pass under the mock and only blow
        # up against a live server (see substrate.py's PGlite guard).
        @asynccontextmanager
        async def cm() -> AsyncGenerator[AsyncMock]:
            if self._held:
                raise RuntimeError(
                    "Reentrant acquire on the single connection would "
                    "deadlock: this task already holds the connection."
                )
            self._held = True
            try:
                yield self.conn
            finally:
                self._held = False

        return cm()

    async def notify(self, channel: str, payload: str) -> None:
        self.notify_calls.append((channel, payload))

    def listen(self, channel: str) -> AsyncIterator[str]:
        del channel
        msgs = list(self.listen_messages)

        async def gen() -> AsyncIterator[str]:
            for m in msgs:
                yield m

        return gen()


def make_store(conn: AsyncMock | None = None) -> tuple[Store, FakeEngine]:
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
    for name, args, _kwargs in conn.mock_calls:
        if name in {"execute", "fetch"} and args:
            sql.append(cast(str, args[0]))
    return sql


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


_INTEG_TABLES = (
    "change_log",
    "edges",
    "inquiry_embeddings",
    "agent_session_events",
    "inquiries",
    # Auth v2 (Phase 1) state. Order before ``users`` so the CASCADE
    # from ``api_keys.user_id`` / ``allowlist.added_by`` doesn't surprise.
    "api_keys",
    "allowlist",
    "users",
)
"""Truncated by :func:`truncate_all` between integration tests.

``change_log.subject_id`` is deliberately FK-free (so ``purged`` rows
survive); only ``edges`` and ``inquiry_embeddings`` actually cascade
from ``inquiries``. The order puts dependent tables first so the
TRUNCATE works under any combination of CASCADE/RESTRICT modes."""

_INTEG_SEQUENCES = (
    "seq_issue",
    "seq_artifact",
    "seq_experiment",
    "seq_paper",
    "seq_belief",
    "seq_codechange",
    "seq_webresult",
    "seq_websearch",
    "seq_agentsession",
)
"""Per-kind short-ref sequences reset between integration tests so
``Issue#1`` numbering is independent of test ordering."""


@pytest.fixture(scope="session")
def pg_dsn(request: pytest.FixtureRequest) -> Iterator[str]:
    """Provision a session-scoped Postgres database; yield its DSN.

    The ``postgresql_proc`` fixture needs a real PostgreSQL install
    (``pg_config`` / ``initdb`` on ``PATH``). When that toolchain is
    absent -- as on machines without a system Postgres -- these
    integration tests skip cleanly instead of erroring at setup.
    """
    try:
        postgresql_proc = cast(
            "PostgreSQLExecutor", request.getfixturevalue("postgresql_proc")
        )
    except ExecutableMissingException as exc:
        pytest.skip(f"PostgreSQL toolchain unavailable: {exc}")
    else:
        with DatabaseJanitor(
            user=postgresql_proc.user,
            host=postgresql_proc.host,
            port=postgresql_proc.port,
            dbname=postgresql_proc.dbname,
            version=postgresql_proc.version,
            password=postgresql_proc.password,
        ):
            password = (
                f":{postgresql_proc.password}" if postgresql_proc.password else ""
            )
            yield (
                f"postgresql://{postgresql_proc.user}{password}"
                f"@{postgresql_proc.host}:{postgresql_proc.port}/"
                f"{postgresql_proc.dbname}"
            )


async def truncate_all(engine: postgres.PostgresEngine) -> None:
    """``TRUNCATE`` every domain table and reset per-kind sequences."""
    async with engine.acquire() as conn:
        await conn.execute(f"TRUNCATE {', '.join(_INTEG_TABLES)} CASCADE")
        for seq in _INTEG_SEQUENCES:
            await conn.execute(f"ALTER SEQUENCE {seq} RESTART")


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def integ_engine(
    pg_dsn: str,
) -> AsyncGenerator[postgres.PostgresEngine]:
    """Session-scoped Postgres engine: one asyncpg pool for every integration test.

    Per-test pool/codec setup runs ~0.3s; amortizing across the suite
    drops integration tests from seconds to milliseconds.
    """
    async with postgres.PostgresEngine(
        dsn=pg_dsn,
        listen_channel=NOTIFY_CHANNEL,
    ) as engine:
        store = Store(engine, embed=StubEmbedder())
        await store.bootstrap()
        yield engine


@pytest_asyncio.fixture(loop_scope="session")
async def integ_store(
    integ_engine: postgres.PostgresEngine,
) -> AsyncGenerator[Store]:
    """Per-test ``Store`` on the shared engine; tables truncated each call."""
    await truncate_all(integ_engine)
    yield Store(integ_engine, embed=StubEmbedder())
