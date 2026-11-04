"""Regression tests for shared Trackinizer fixtures."""

from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from functools import partial
from pathlib import Path
from typing import Final, cast
from unittest.mock import MagicMock

import inspect
import os
import random

import asyncpg
import pytest


# pytest-postgresql imports psycopg, and psycopg without libpq raises a plain
# ImportError, which ``importorskip`` passes through unless told otherwise.
pytest.importorskip("psycopg", exc_type=ImportError)

from port_for import api
from pytest_postgresql import janitor
from pytest_postgresql.config import get_config
from pytest_postgresql.executors.proc import PostgreSQLExecutor
from pytest_postgresql.factories import process
from pytest_postgresql.plugin import postgresql_proc

from trackinizer import conftest
from trackinizer.conftest import (
    BEGIN,
    COMMIT,
    ROLLBACK,
    TRANSACTION_BEGIN,
    TRANSACTION_COMMIT,
    TRANSACTION_ROLLBACK,
    ExchangeEngine,
    RecordingPolicy,
    Says,
)
from trackinizer.lib.postgres import Conn, DatabaseEngine, PGliteEngine, PostgresEngine
from trackinizer.server.access_policy import (
    AccessDeniedError,
    AccessPolicy,
    Principal,
    Resource,
)
from trackinizer.server.auth import BOOTSTRAP_ADMIN_ENV, BOOTSTRAP_TOKEN_FILE_ENV
from trackinizer.server.notify import tx
from trackinizer.server.sql import schema_migrations
from trackinizer.server.store.core import Store
from trackinizer.server.values import vetted_sql
from trackinizer.types.errors import ConflictError
from trackinizer.types.inquiries import Issue
from trackinizer.wire.bodies import SubmitIssue


# A Protocol method the policy lacks fails both type checkers here, not a test that
# happens to call it.
_POLICY: Final[AccessPolicy] = RecordingPolicy()

_PROBE_COLUMN: Final = (
    "SELECT count(*) FROM information_schema.columns "
    "WHERE table_name = 'users' AND column_name = 'probe'"
)


@pytest.mark.parametrize("inherited", [None, "", "operator@example.com"])
def test_bootstrap_environment_is_isolated_and_restored(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    inherited: str | None,
) -> None:
    for name in (BOOTSTRAP_ADMIN_ENV, BOOTSTRAP_TOKEN_FILE_ENV):
        if inherited is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, inherited)
    before = dict(os.environ)
    isolate = contextmanager(
        cast(
            Callable[[], Generator[None]],
            inspect.unwrap(conftest.isolate_bootstrap_environment),
        ),
    )
    with isolate():
        assert os.environ[BOOTSTRAP_ADMIN_ENV] == ""
        assert os.environ[BOOTSTRAP_TOKEN_FILE_ENV] == ""
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv(BOOTSTRAP_ADMIN_ENV, "test@example.com")
            patch.setenv(BOOTSTRAP_TOKEN_FILE_ENV, str(tmp_path / "bootstrap-token"))
            assert os.environ[BOOTSTRAP_ADMIN_ENV] == "test@example.com"
            assert os.environ[BOOTSTRAP_TOKEN_FILE_ENV] == str(
                tmp_path / "bootstrap-token",
            )
    assert dict(os.environ) == before


@pytest.mark.parametrize(("workers", "retries"), [(7, 5), (1, 9)])
def test_pg_dsn_reserves_ports_for_seeded_workers(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    *,
    workers: int,
    retries: int,
) -> None:
    """Cover concurrent reservations and preserve a larger configured budget."""
    (tmp_path / "worker").mkdir()
    monkeypatch.setenv("PYTEST_XDIST_WORKER_COUNT", str(workers))
    monkeypatch.setattr(request.config, "workerinput", {}, raising=False)
    monkeypatch.setattr(request.config.option, "postgresql_port_search_count", retries)
    monkeypatch.setattr(tmp_path_factory, "getbasetemp", lambda: tmp_path / "worker")
    # Port policy discovery and the socket probe are independent of exclusive
    # reservation: every port reads as free, so seeded workers collide on
    # their first pick. Keep the real RNG selection and sentinel lifecycle,
    # which alone must keep the reservations apart.
    monkeypatch.setattr(api, "available_good_ports", lambda: set(range(10_000, 10_032)))
    monkeypatch.setattr(api, "port_is_used", _never_used)
    monkeypatch.setattr(process, "_pg_exe", MagicMock(return_value=Path("/unused")))
    executor = MagicMock(
        host="localhost",
        user="postgres",
        password=None,
        dbname="test",
    )
    executor.logfile = str(tmp_path / "unused.log")
    monkeypatch.setattr(process, "PostgreSQLExecutor", MagicMock(return_value=executor))
    monkeypatch.setattr(process, "DatabaseJanitor", MagicMock())
    # ``pg_dsn`` imports ``DatabaseJanitor`` when it runs, so patch it at the source.
    monkeypatch.setattr(janitor, "DatabaseJanitor", MagicMock())
    fixture = contextmanager(
        cast(
            Callable[
                [pytest.FixtureRequest, pytest.TempPathFactory],
                Generator[PostgreSQLExecutor],
            ],
            inspect.unwrap(postgresql_proc),
        ),
    )
    dsn_fixture = contextmanager(
        cast(
            Callable[[pytest.FixtureRequest], Generator[str]],
            inspect.unwrap(conftest.pg_dsn),
        ),
    )
    with ExitStack() as stack:

        def get_fixture(name: str) -> PostgreSQLExecutor:
            assert name == "postgresql_proc"
            return stack.enter_context(fixture(request, tmp_path_factory))

        fixture_request = MagicMock(spec=pytest.FixtureRequest, config=request.config)
        fixture_request.getfixturevalue.side_effect = get_fixture
        for _ in range(workers):
            random.seed(1337)
            stack.enter_context(
                dsn_fixture(cast(pytest.FixtureRequest, fixture_request)),
            )
        assert len(list(tmp_path.glob("*.port"))) == workers
        assert get_config(request).port_search_count == max(retries, workers - 1)
    assert not list(tmp_path.glob("*.port"))


# Each of the pair writes what the other then must not find, so whichever runs second
# fails unless the restore between them cleared it: run them in either order.
@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("writer", ["first", "second"])
async def test_a_pglite_test_meets_none_of_anothers_writes(
    store: Store,
    writer: str,
) -> None:
    assert await _filled(store) == {
        "applied_migrations": len(list(schema_migrations())),
    }
    async with store.engine.acquire() as conn:
        assert await conn.fetchval("SELECT to_regclass('pg_temp.probe')") is None
        await conn.execute("CREATE TEMP TABLE probe (writer TEXT)")
    await _write(store, writer)


@pytest.mark.db_postgres
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("writer", ["first", "second"])
async def test_a_postgres_test_meets_none_of_anothers_writes(
    integ_store: Store,
    writer: str,
) -> None:
    assert await _filled(integ_store) == {
        "applied_migrations": len(list(schema_migrations())),
    }
    await _write(integ_store, writer)


# The HNSW index kept every earlier test's embeddings, and a nearest-neighbour search
# spent its scan on them: from about the 330th test it stopped reaching a live row.
# Enough embeddings to fill several index pages show whether the restore left them.
@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_pglite_test_meets_none_of_anothers_index_entries(
    pglite_db: PGliteEngine,
) -> None:
    built = await _index_sizes(pglite_db)
    await _fill_embeddings(pglite_db)

    await conftest.restore_pglite(pglite_db)

    assert await _index_sizes(pglite_db) == built


@pytest.mark.db_postgres
@pytest.mark.asyncio(loop_scope="session")
async def test_a_postgres_test_meets_none_of_anothers_index_entries(
    integ_store: Store,
    integ_engine: PostgresEngine,
) -> None:
    del integ_store  # Restores the build before the test.
    built = await _index_sizes(integ_engine)
    await _fill_embeddings(integ_engine)

    await conftest.restore(integ_engine, empty=partial(conftest._emptied, integ_engine))

    assert await _index_sizes(integ_engine) == built


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_changed_catalog_is_built_again(pglite_db: PGliteEngine) -> None:
    """A column the build lacks makes the next test build; dropping it again does not."""
    async with pglite_db.acquire() as conn:
        await conn.execute("ALTER TABLE users ADD COLUMN probe TEXT")
    with pytest.raises(_BuildError):
        await conftest.restore(pglite_db, empty=_refuse)
    async with pglite_db.acquire() as conn:
        await conn.execute("ALTER TABLE users DROP COLUMN probe")
    await conftest.restore(pglite_db, empty=_refuse)


# Another package's tests leave their schema on the shared engine. Its view or foreign
# key over a core table refuses a migration test's change to that table: knowaccess's
# views over ``inquiries`` failed 13 tests of ``schema_migration_test``.
@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    ("over", "found"),
    [
        (
            "CREATE VIEW addon.probe AS SELECT id FROM public.inquiries",
            "SELECT count(*) FROM pg_views WHERE schemaname = 'addon'",
        ),
        (
            "CREATE TABLE addon.probe (inquiry UUID REFERENCES public.inquiries (id))",
            (
                "SELECT count(*) FROM pg_constraint "
                "WHERE contype = 'f' AND connamespace = 'addon'::regnamespace"
            ),
        ),
    ],
    ids=["view", "foreign key"],
)
async def test_another_schemas_object_over_the_build_is_built_away(
    pglite_db: PGliteEngine,
    over: str,
    found: str,
) -> None:
    async with pglite_db.acquire() as conn:
        await conn.execute("CREATE SCHEMA addon")
        await conn.execute(over)
    try:
        with pytest.raises(_BuildError):
            await conftest.restore(pglite_db, empty=_refuse)
        await conftest.restore_pglite(pglite_db)
        async with pglite_db.acquire() as conn:
            assert await conn.fetchval(found) == 0
        await conftest.restore(pglite_db, empty=_refuse)
    finally:
        async with pglite_db.acquire() as conn:
            await conn.execute("DROP SCHEMA addon CASCADE")


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_another_schemas_copy_of_a_core_table_is_no_change(
    pglite_db: PGliteEngine,
) -> None:
    """Knowaccess keeps its builds' copies apart: a copy hangs on no core table."""
    async with pglite_db.acquire() as conn:
        await conn.execute(
            "CREATE SCHEMA addon; "
            "CREATE TABLE addon.copy AS SELECT * FROM public.session_embeddings",
        )
    try:
        await conftest.restore(pglite_db, empty=_refuse)
    finally:
        async with pglite_db.acquire() as conn:
            await conn.execute("DROP SCHEMA addon CASCADE")


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_rolled_back_migration_leaves_the_build(
    pglite_db: PGliteEngine,
) -> None:
    """Schema surgery in ``rolled_back`` is gone after it, so no test builds again."""
    async with pglite_db.acquire() as conn, conftest.rolled_back(conn):
        await conn.execute("ALTER TABLE users ADD COLUMN probe TEXT")
        assert await conn.fetchval(_PROBE_COLUMN) == 1
    async with pglite_db.acquire() as conn:
        assert await conn.fetchval(_PROBE_COLUMN) == 0
    await conftest.restore(pglite_db, empty=_refuse)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_refused_statement_in_a_savepoint_leaves_the_transaction_live(
    pglite_db: PGliteEngine,
) -> None:
    async with pglite_db.acquire() as conn, conftest.rolled_back(conn):
        await conn.execute("ALTER TABLE users ADD COLUMN probe TEXT")
        with pytest.raises(asyncpg.CheckViolationError):
            async with conftest.savepoint(conn):
                await conn.execute(
                    "INSERT INTO users (id, email, name, role, status) "
                    "VALUES (gen_random_uuid(), 'u@x.y', 'u', 'nobody', 'active')",
                )
        assert await conn.fetchval(_PROBE_COLUMN) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_an_exchange_answers_each_step_in_order_with_its_answer() -> None:
    engine = ExchangeEngine(
        Says(method="fetchval", sql="SELECT $1", args=(7,), answer=7),
        Says(method="fetch", sql="SELECT 2", answer=[{"n": 2}]),
        Says(method="fetchrow", sql="SELECT 3", answer={"n": 3}),
        Says(method="execute", sql="DELETE 4", answer="DELETE 4"),
    )

    async with cast(DatabaseEngine, engine).acquire() as conn:
        answers = [
            await conn.fetchval("SELECT $1", 7),
            await conn.fetch("SELECT 2"),
            await conn.fetchrow("SELECT 3"),
            await conn.execute("DELETE 4"),
        ]

    assert answers == [7, [{"n": 2}], {"n": 3}, "DELETE 4"]
    assert engine.done
    assert [each.args for each in engine.said] == [(7,), (), (), ()]


@pytest.mark.parametrize(
    ("method", "sql", "args"),
    [
        pytest.param("fetch", "SELECT $1", (7,), id="method"),
        pytest.param("fetchval", "SELECT $2", (7,), id="sql"),
        pytest.param("fetchval", "SELECT $1", (8,), id="args"),
    ],
)
@pytest.mark.asyncio(loop_scope="session")
async def test_an_exchange_refuses_a_statement_other_than_the_next_step(
    method: str,
    sql: str,
    args: tuple[object, ...],
) -> None:
    engine = ExchangeEngine(Says(method="fetchval", sql="SELECT $1", args=(7,)))

    async with cast(DatabaseEngine, engine).acquire() as conn:
        with pytest.raises(AssertionError, match=r"^sent .* expected Says\("):
            _ = await getattr(conn, method)(sql, *args)

    assert (engine.failed, engine.said) == (True, [])


@pytest.mark.asyncio(loop_scope="session")
async def test_an_exchange_refuses_a_statement_past_its_last_step() -> None:
    engine = ExchangeEngine()

    async with cast(DatabaseEngine, engine).acquire() as conn:
        with pytest.raises(AssertionError, match="expected None"):
            _ = await conn.execute("SELECT 1")


@pytest.mark.asyncio(loop_scope="session")
async def test_an_exchange_raises_an_answer_that_is_an_exception() -> None:
    refused = ConflictError("taken")
    engine = ExchangeEngine(
        BEGIN,
        Says(method="execute", sql="INSERT 1", answer=refused),
        ROLLBACK,
    )

    with pytest.raises(ConflictError, match="taken"):
        async with cast(DatabaseEngine, engine).acquire() as conn, tx(conn):
            _ = await conn.execute("INSERT 1")

    assert (engine.failed, engine.done) == (False, True)
    assert [each.sql for each in engine.said] == ["BEGIN", "INSERT 1", "ROLLBACK"]


@pytest.mark.asyncio(loop_scope="session")
async def test_after_a_mismatch_only_the_way_outs_rollback_gets_through() -> None:
    engine = ExchangeEngine(BEGIN, COMMIT)

    with pytest.raises(AssertionError, match="sent execute 'INSERT 2'"):
        async with cast(DatabaseEngine, engine).acquire() as conn, tx(conn):
            _ = await conn.execute("INSERT 2")

    # The mismatch took the step it was checked against; the ROLLBACK after it
    # raised nothing, so the mismatch is what the test reports.
    assert (engine.failed, engine.done) == (True, True)
    assert [each.sql for each in engine.said] == ["BEGIN"]


@pytest.mark.asyncio(loop_scope="session")
async def test_a_rollback_before_any_mismatch_is_a_step_like_any_other() -> None:
    engine = ExchangeEngine(ROLLBACK)

    async with cast(DatabaseEngine, engine).acquire() as conn:
        assert await conn.fetch("ROLLBACK") == []

    assert engine.done


@pytest.mark.asyncio(loop_scope="session")
async def test_an_exchanges_transaction_begins_and_commits_as_asyncpgs_does() -> None:
    select = Says(method="fetchval", sql="SELECT 1", answer=1)
    engine = ExchangeEngine(TRANSACTION_BEGIN, select, TRANSACTION_COMMIT)

    async with cast(DatabaseEngine, engine).acquire() as conn, conn.transaction():
        found = await conn.fetchval("SELECT 1")

    assert found == 1
    assert [(each.method, each.sql) for each in engine.said] == [
        ("execute", "BEGIN;"),
        ("fetchval", "SELECT 1"),
        ("execute", "COMMIT;"),
    ]


@pytest.mark.asyncio(loop_scope="session")
async def test_an_exchanges_transaction_rolls_back_a_refused_statement() -> None:
    refused = Says(method="execute", sql="INSERT 1", answer=ValueError("refused"))
    engine = ExchangeEngine(TRANSACTION_BEGIN, refused, TRANSACTION_ROLLBACK)

    with pytest.raises(ValueError, match="refused"):
        async with cast(DatabaseEngine, engine).acquire() as conn, conn.transaction():
            _ = await conn.execute("INSERT 1")

    assert [each.sql for each in engine.said] == ["BEGIN;", "INSERT 1", "ROLLBACK;"]


@pytest.mark.asyncio(loop_scope="session")
async def test_an_exchange_reports_the_first_mismatch_not_the_rollback() -> None:
    engine = ExchangeEngine(TRANSACTION_BEGIN)

    with pytest.raises(AssertionError, match="sent execute 'INSERT 2'"):
        async with cast(DatabaseEngine, engine).acquire() as conn, conn.transaction():
            _ = await conn.execute("INSERT 2")

    assert (engine.failed, engine.done) == (True, True)


@pytest.mark.asyncio(loop_scope="session")
async def test_an_exchanges_transaction_starts_and_ends_by_hand_as_asyncpgs_does() -> (
    None
):
    select = Says(method="fetchval", sql="SELECT 1", answer=1)
    engine = ExchangeEngine(
        TRANSACTION_BEGIN,
        select,
        TRANSACTION_ROLLBACK,
        TRANSACTION_BEGIN,
        TRANSACTION_COMMIT,
    )

    async with cast(DatabaseEngine, engine).acquire() as conn:
        undone = conn.transaction()
        await undone.start()
        found = await conn.fetchval("SELECT 1")
        await undone.rollback()
        kept = conn.transaction()
        await kept.start()
        await kept.commit()

    assert found == 1
    assert [each.sql for each in engine.said] == [
        "BEGIN;",
        "SELECT 1",
        "ROLLBACK;",
        "BEGIN;",
        "COMMIT;",
    ]


@pytest.mark.asyncio(loop_scope="session")
async def test_a_recording_policy_records_each_call_and_what_was_sent_before() -> None:
    policy = RecordingPolicy(groups_of=frozenset({"g2"}))
    engine = conftest.FakeEngine()
    ada = Principal(email="ada@example.com", role="writer")
    row = Resource(kind="inquiry", id="1", account="ada@example.com")
    params: list[object] = []

    async with cast(DatabaseEngine, engine).acquire() as conn:
        assert await policy.groups(conn, ada) == frozenset({"g2"})
        _ = await conn.execute("BEGIN")
        await policy.check(conn, principal=ada, resource=row, action="write")
        await policy.on_create(conn, principal=None, resource=row)
        await policy.prepare_account_change(conn, resource=row)
        await policy.validate(conn, resource=row)
        await policy.on_user(conn, email="bob@example.com", role="viewer")
        await policy.prepare_audience_change(conn)
        await policy.on_audience(conn, principal=ada, resource=row)
    filtered = policy.read_filter("inquiry", "r", params=params, principal=ada)

    assert (filtered, params) == ("TRUE", [])
    assert [name for name, _ in policy.calls] == [
        "groups",
        "check",
        "on_create",
        "prepare_account_change",
        "validate",
        "on_user",
        "prepare_audience_change",
        "on_audience",
        "read_filter",
    ]
    assert policy.of("groups") == ["ada@example.com"]
    assert policy.of("check") == [(ada, row, "write")]
    assert policy.of("on_create") == [(None, row)]
    assert policy.of("prepare_account_change") == [row]
    assert policy.of("validate") == [row]
    assert policy.of("on_user") == [("bob@example.com", "viewer")]
    assert policy.of("prepare_audience_change") == [None]
    assert policy.of("on_audience") == [(ada, row)]
    assert policy.of("read_filter") == [None]
    assert policy.sent == [["BEGIN"]] * 7


@pytest.mark.asyncio(loop_scope="session")
async def test_a_recording_policy_refuses_as_it_is_told() -> None:
    policy = RecordingPolicy(deny_writes=True, refuse_states=True)
    ada = Principal(email="ada@example.com", role="writer")
    row = Resource(kind="inquiry", id="1")
    conn = cast(Conn, object())

    with pytest.raises(AccessDeniedError, match="excludes the caller"):
        await policy.check(conn, principal=ada, resource=row, action="write")
    with pytest.raises(ConflictError, match="may not hold the row"):
        await policy.validate(conn, resource=row)

    # A live connection keeps no record of what it was sent.
    assert policy.sent == [[], []]


async def _filled(store: Store) -> dict[str, int]:
    """Return how many rows each table of schema ``public`` holds, if any."""
    async with store.engine.acquire() as conn:
        counts = await conn.fetch(
            " UNION ALL ".join(
                vetted_sql(
                    "SELECT '",
                    str(row["tablename"]),
                    "' AS name, count(*) AS n FROM public.\"",
                    str(row["tablename"]),
                    '"',
                )
                for row in await conn.fetch(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'",
                )
            ),
        )
    return {str(row["name"]): cast(int, row["n"]) for row in counts if row["n"]}


async def _write(store: Store, writer: str) -> None:
    """Write an issue, its change and a user: rows and sequence positions to undo."""
    issue = await store.submit_issue(SubmitIssue(account="t@example.com", title=writer))
    assert cast(Issue, await store.get_inquiry(issue)).seq == 1
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES (gen_random_uuid(), $1, 'u', 'writer', 'active')",
            f"{writer}@example.com",
        )


async def _index_sizes(engine: DatabaseEngine) -> dict[str, int]:
    """Return the size in bytes of each index of schema ``public``."""
    async with engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT c.relname, pg_relation_size(c.oid) AS size FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'i'",
        )
    return {str(row["relname"]): cast(int, row["size"]) for row in rows}


async def _fill_embeddings(engine: DatabaseEngine) -> None:
    """Write enough embeddings to spread their HNSW index over several pages."""
    # Replica role skips the foreign key to an inquiry these rows have no need of.
    async with engine.acquire() as conn, tx(conn):
        await conn.execute("SET LOCAL session_replication_role = replica")
        await conn.execute(
            "INSERT INTO session_embeddings "
            "(session_id, part, idx, field, mapper, model, embedding, text_md5) "
            "SELECT gen_random_uuid(), 0, n, 'content', 'footprint', 'stub-1024', "
            "array_fill(n::real, ARRAY[1024])::halfvec, md5(n::text) "
            "FROM generate_series(1, 200) n",
        )


def _never_used(port: int, host: str = "127.0.0.1") -> bool:
    """Answer ``port_is_used`` for every port: free, with no socket opened."""
    del port, host
    return False


async def _refuse() -> None:
    raise _BuildError


class _BuildError(Exception):
    """Raised by a build the test forbids, to show the restore chose to build."""


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
