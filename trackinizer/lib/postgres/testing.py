"""Shared PGlite engine fixtures for tests.

Booting PGlite costs ~2.4s; resetting its schema costs ~0.004s (600x). A test
that builds its own :class:`~trackinizer.lib.postgres.PGliteEngine` therefore spends
essentially all of its wall time starting a server, and a package with dozens of
such tests pays that repeatedly for no added isolation -- an empty schema is an
empty schema however it was emptied.

These fixtures boot ONE engine per test session (per xdist worker) and hand each
test a clean schema instead. The repo-root ``conftest.py`` names this module in
``pytest_plugins``, so every test sees them with no per-package wiring.

A package needing an extension defines ``pglite_extensions`` in its own
conftest::

    @pytest.fixture(scope="session")
    def pglite_extensions() -> tuple[str, ...]:
        return ("pgvector",)

A test that needs a real PostgreSQL server -- requests at once on connections of
their own, which PGlite's one connection cannot run -- names a database with
:func:`postgres_database` from a session fixture of its package's.

What sharing does NOT cover: a test that must observe data surviving a real
PROCESS restart. Those construct their own engine against a ``tmp_path`` and are
correct to -- restart durability is the behavior under test. Sharing is for the
common case where the engine is incidental scaffolding.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

import contextlib
import os
import shutil
import subprocess
import sys
import tempfile

import pytest
import pytest_asyncio

from trackinizer.lib.postgres import PGliteEngine


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator

    from trackinizer.lib.postgres import DatabaseEngine


@pytest.fixture(scope="session")
def pglite_workdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One session-scoped directory backing the shared PGlite servers.

    Session-scoped because the engines are: PGlite does not lock its ``dataDir``
    (``substrate.py:118-121``), so two engines sharing a workdir corrupt the
    database. Under xdist each worker is its own process and gets its own
    ``tmp_path_factory`` root, so workers never collide.

    Args:
      tmp_path_factory: pytest fixture for temp directory creation.

    Returns:
      workdir: Directory the shared engines may own for the session.

    """
    return tmp_path_factory.mktemp("pglite-shared")


@pytest_asyncio.fixture(loop_scope="session")
async def pglite_engine(
    pglite_engine_cache: _EngineCache,
    request: pytest.FixtureRequest,
) -> PGliteEngine:
    """One started PGlite server, shared across the session.

    ``loop_scope="session"`` is load-bearing, not decoration: the engine holds a
    persistent asyncpg connection, and asyncpg binds a connection to the loop
    that created it. Under the default per-test loop the second test to use this
    fixture fails with ``got Future ... attached to a different loop``. A test
    consuming it must therefore also declare
    ``@pytest.mark.asyncio(loop_scope="session")``.

    Function-scoped despite handing out a session-lived engine: the scope here
    governs only the ``pglite_extensions`` lookup below, and a session scope
    would resolve that once, in whichever package asked first, then serve the
    same engine to packages wanting a different extension set. The engine itself
    is still booted at most once per set, inside the cache.

    Args:
      pglite_engine_cache: Session cache of engines keyed by extension set.
      request: pytest fixture request to resolve pglite_extensions.

    Returns:
      engine: A started engine. Call :func:`reset_schema` for an empty schema.

    """
    # Looked up rather than declared as a parameter, so that a package which
    # needs no extension is not forced to define ``pglite_extensions``.
    # ``request.fixturenames`` cannot answer this: it holds only the REQUESTED
    # closure, which never names an override nobody requested.
    extensions: tuple[str, ...] = ()
    with contextlib.suppress(pytest.FixtureLookupError):
        extensions = cast(tuple[str, ...], request.getfixturevalue("pglite_extensions"))
    return await pglite_engine_cache.get(extensions)


@contextlib.contextmanager
def postgres_database(request: pytest.FixtureRequest, *, dbname: str) -> Generator[str]:
    """Create ``dbname`` on the session's PostgreSQL server; yield its DSN.

    The one way a fixture reaches a real server, which tests need wherever PGlite's one
    connection cannot show what they test. Each package names a database of its own: a
    second janitor creating a name another already holds fails every test after it on
    that worker. Without libpq or a PostgreSQL install, the calling test skips.

    Args:
      request: The calling fixture's request, which resolves ``postgres_server``.
      dbname: The database, named for the package that owns it.

    Yields:
      dsn: ``postgresql://`` URL of the database, dropped when the block ends.

    """
    try:
        from pytest_postgresql.janitor import (  # noqa: PLC0415 -- Imports psycopg, which needs libpq.
            DatabaseJanitor,
        )
    except ImportError as exc:
        pytest.skip(f"PostgreSQL client library unavailable: {exc}")
    sockets = cast(Path, request.getfixturevalue("postgres_server"))
    with DatabaseJanitor(user="postgres", host=str(sockets), port=5432, dbname=dbname):
        yield f"postgresql://postgres@/{dbname}?host={sockets}&port=5432"


@pytest.fixture(scope="session")
def postgres_server(tmp_path_factory: pytest.TempPathFactory) -> Generator[Path]:
    """Start the session's PostgreSQL server; yield the directory holding its socket.

    The server listens on no TCP port, only on a Unix socket in a directory of its
    own, so nothing else on the host can take its address. A TCP port is one
    namespace for the whole host: pytest runs starting together drew the same ports
    and failed each other's servers. Without a PostgreSQL install, the calling test
    skips. Consumed by :func:`postgres_database`, which is what a fixture should call.

    Args:
      tmp_path_factory: pytest fixture for the server's data directory.

    Yields:
      sockets: Directory of the server's socket, a DSN's ``host``.

    """
    pg_ctl = _pg_ctl()
    if pg_ctl is None:
        pytest.skip(
            "PostgreSQL toolchain unavailable: no pg_ctl beside pg_config or on PATH",
        )
    data = tmp_path_factory.mktemp("postgres")
    log = data / "server.log"
    # A socket path must fit in about 104 bytes, which a tmp_path under macOS's
    # $TMPDIR does not leave room for.
    with tempfile.TemporaryDirectory(prefix="pg-") as sockets:
        _run_pg_ctl(
            pg_ctl,
            "initdb",
            "-D",
            data / "pgdata",
            "-o",
            "--username=postgres --auth=trust --no-sync",
            log=log,
        )
        _run_pg_ctl(
            pg_ctl,
            "start",
            "-w",
            "-D",
            data / "pgdata",
            "-l",
            log,
            "-o",
            f"-F -p 5432 -k {sockets} -c listen_addresses=''",
            log=log,
        )
        try:
            yield Path(sockets)
        finally:
            _run_pg_ctl(pg_ctl, "stop", "-D", data / "pgdata", "-m", "fast", log=log)


async def reset_schema(engine: DatabaseEngine) -> None:
    """Drop and recreate ``public`` and the session temp schema.

    The isolation a per-test engine provided, at 1/600th the cost. ``CASCADE``
    removes every table, index, sequence, and type the previous test created, so
    the next ``bootstrap()`` sees a database indistinguishable from a fresh one.

    Args:
      engine: A started engine to reset.

    """
    async with engine.acquire() as conn:
        await conn.execute("DROP SCHEMA public CASCADE")
        await conn.execute("CREATE SCHEMA public")
        # A ``CREATE TEMP TABLE`` lands in ``pg_temp_N``, which the ``public``
        # drop leaves alone; the next test creating that name then fails with
        # ``relation already exists``. Resolved by lookup because ``pg_temp`` is
        # a per-session alias that ``DROP SCHEMA`` silently matches nothing for.
        temp_schema = await conn.fetchval(
            "SELECT nspname FROM pg_namespace WHERE oid = pg_my_temp_schema()",
        )
        if temp_schema:
            await conn.execute(f'DROP SCHEMA "{temp_schema}" CASCADE')


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def pglite_engine_cache(pglite_workdir: Path) -> AsyncGenerator[_EngineCache]:
    """Session-lived cache of one started engine per extension set.

    Consumed by :func:`pglite_engine`, which is what a test should ask for.

    Args:
      pglite_workdir: Session temp directory for database files.

    Yields:
      cache: Call ``get(extensions)`` for a started engine.

    """
    cache = _EngineCache(pglite_workdir)
    try:
        yield cache
    finally:
        await cache.aclose()


class _EngineCache:
    """Started PGlite engines, at most one per extension set.

    Keyed on extensions because one session spans packages that disagree about
    them. A single engine would let whichever package ran first fix the
    extension set for the rest, failing the others with ``extension "vector" is
    not available``.
    """

    def __init__(self, workdir: Path) -> None:
        self._workdir = workdir
        self._engines: dict[tuple[str, ...], PGliteEngine] = {}

    async def get(self, extensions: tuple[str, ...]) -> PGliteEngine:
        """Return the started engine for ``extensions``, booting on first ask.

        Args:
          extensions: PostgreSQL extension names to load.

        Returns:
          engine: Started and connected PGliteEngine instance.

        """
        key = tuple(sorted(extensions))
        engine = self._engines.get(key)
        if engine is None:
            # Each engine owns a DISTINCT directory: PGlite does not lock its
            # dataDir, so two live engines over one would corrupt it.
            slot = "-".join(key) or "bare"
            engine = PGliteEngine(workdir=self._workdir / f"pg-{slot}", extensions=key)
            _ = await engine.__aenter__()
            self._engines[key] = engine
        return engine

    async def aclose(self) -> None:
        """Stop every engine this cache started."""
        for engine in self._engines.values():
            await engine.__aexit__()
        self._engines.clear()


# pytest-postgresql's order: a PGDG install keeps its binaries off ``PATH``
# (``/usr/lib/postgresql/<major>/bin``) and exposes them only through ``pg_config``.
def _pg_ctl() -> str | None:
    """Find ``pg_ctl`` beside the binaries ``pg_config`` names, else on ``PATH``."""
    pg_config = shutil.which("pg_config")
    if pg_config is not None:
        bindir = subprocess.run(  # noqa: S603 -- The install's pg_config, fixed arguments.
            [pg_config, "--bindir"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if (found := shutil.which("pg_ctl", path=bindir)) is not None:
            return found
    return shutil.which("pg_ctl")


def _run_pg_ctl(pg_ctl: str, *argv: str | Path, log: Path) -> None:
    """Run ``pg_ctl``; on failure raise with its output and the server's log."""
    # Pinned, as pytest-postgresql pinned it: initdb takes its locale from the
    # environment, where a bare C one makes a SQL_ASCII database, and on macOS a
    # server without a valid LC_ALL exits "multithreaded during startup". macOS has
    # lacked C.UTF-8.
    locale = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"
    done = subprocess.run(  # noqa: S603 -- The install's pg_ctl, fixed arguments.
        [pg_ctl, *map(str, argv)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "LC_ALL": locale},
    )
    if done.returncode != 0:
        server = log.read_text() if log.exists() else ""
        raise RuntimeError(done.stdout + done.stderr + server)
