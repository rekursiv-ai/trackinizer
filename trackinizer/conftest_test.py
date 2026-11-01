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
from trackinizer.lib.postgres import DatabaseEngine, PGliteEngine, PostgresEngine
from trackinizer.server.auth import BOOTSTRAP_ADMIN_ENV, BOOTSTRAP_TOKEN_FILE_ENV
from trackinizer.server.notify import tx
from trackinizer.server.sql import schema_migrations
from trackinizer.server.store.core import Store
from trackinizer.server.values import vetted_sql
from trackinizer.types.inquiries import Issue
from trackinizer.wire.bodies import SubmitIssue


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
    # Port policy discovery is independent of exclusive reservation. Keep the
    # real RNG selection, socket availability checks, and sentinel lifecycle.
    monkeypatch.setattr(api, "available_good_ports", lambda: set(range(10_000, 10_032)))
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


async def _refuse() -> None:
    raise _BuildError


class _BuildError(Exception):
    """Raised by a build the test forbids, to show the restore chose to build."""


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
