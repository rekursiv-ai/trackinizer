"""Tests for shared PGlite pytest fixtures."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from functools import partial
from pathlib import Path
from subprocess import CompletedProcess
from types import ModuleType
from typing import TYPE_CHECKING, Protocol, Self, cast, override, runtime_checkable
from unittest.mock import MagicMock, call

import os
import subprocess
import sys

import asyncpg
import pytest

from trackinizer.lib.postgres import PGliteEngine, testing


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Coroutine, Generator


@runtime_checkable
class WorkdirFixture(Protocol):
    _fixture_function: Callable[[pytest.TempPathFactory], Path]


@runtime_checkable
class _EngineFixture(Protocol):
    _fixture_function: Callable[
        [testing._EngineCache, pytest.FixtureRequest],
        Coroutine[object, object, PGliteEngine],
    ]


@runtime_checkable
class _CacheFixture(Protocol):
    _fixture_function: Callable[[Path], AsyncGenerator[testing._EngineCache, None]]


class _ServerFixture(Protocol):
    _fixture_function: Callable[[pytest.TempPathFactory], Generator[Path]]


def _workdir_function(
    fixture: WorkdirFixture,
) -> Callable[[pytest.TempPathFactory], Path]:
    return fixture._fixture_function


def _server_function(
    fixture: _ServerFixture,
) -> Callable[[pytest.TempPathFactory], Generator[Path]]:
    return fixture._fixture_function


class _Connection:
    def __init__(self, temp_schema: str | None) -> None:
        self.temp_schema = temp_schema
        self.commands: list[str] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        del exc_type, exc_value, traceback

    async def execute(self, command: str) -> str:
        self.commands.append(command)
        return ""

    async def fetchval(self, query: str) -> str | None:
        self.commands.append(query)
        return self.temp_schema


class _CachedEngine(PGliteEngine):
    def __init__(self, *, workdir: Path, extensions: tuple[str, ...]) -> None:
        self.workdir = workdir
        self.extensions = extensions
        self.entered = False
        self.exited = False

    @override
    async def __aenter__(self) -> Self:
        self.entered = True
        return self

    @override
    async def __aexit__(self, *exc: object) -> None:
        del exc
        self.exited = True


@pytest.mark.asyncio
async def test_pglite_workdir_creates_session_directory(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    workdir = _workdir_function(testing.pglite_workdir)(tmp_path_factory)

    assert workdir.is_dir()
    assert workdir.name.startswith("pglite-shared")


@pytest.mark.asyncio
@pytest.mark.parametrize("has_extension_fixture", [False, True])
async def test_pglite_engine_resolves_optional_extensions(
    has_extension_fixture: bool,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    cache = testing._EngineCache(Path("unused"))
    requested: list[tuple[str, ...]] = []

    async def fake_get(extensions: tuple[str, ...]) -> PGliteEngine:
        requested.append(extensions)
        return PGliteEngine(workdir=Path("unused"), extensions=())

    monkeypatch.setattr(cache, "get", fake_get)

    def getfixturevalue(argname: str) -> object:
        if has_extension_fixture:
            return ("vector",)
        raise pytest.FixtureLookupError(argname, request)

    monkeypatch.setattr(request, "getfixturevalue", getfixturevalue)
    fixture = testing.pglite_engine
    assert isinstance(fixture, _EngineFixture)

    await fixture._fixture_function(cache, request)

    assert requested == [("vector",) if has_extension_fixture else ()]


@pytest.mark.asyncio
async def test_reset_schema_recreates_public_and_temp_schemas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection("pg_temp_7")
    engine = PGliteEngine(workdir=Path("unused"), extensions=())
    monkeypatch.setattr(engine, "acquire", lambda: connection)

    await testing.reset_schema(engine)

    assert connection.commands == [
        "DROP SCHEMA public CASCADE",
        "CREATE SCHEMA public",
        "SELECT nspname FROM pg_namespace WHERE oid = pg_my_temp_schema()",
        'DROP SCHEMA "pg_temp_7" CASCADE',
    ]


@pytest.mark.asyncio
async def test_reset_schema_skips_missing_temp_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection(None)
    engine = PGliteEngine(workdir=Path("unused"), extensions=())
    monkeypatch.setattr(engine, "acquire", lambda: connection)

    await testing.reset_schema(engine)

    assert connection.commands == [
        "DROP SCHEMA public CASCADE",
        "CREATE SCHEMA public",
        "SELECT nspname FROM pg_namespace WHERE oid = pg_my_temp_schema()",
    ]


@pytest.mark.asyncio
async def test_pglite_engine_cache_sorts_extensions_and_reuses_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(testing, "PGliteEngine", _CachedEngine)
    cache = testing._EngineCache(tmp_path)

    first = await cache.get(("zeta", "alpha"))
    again = await cache.get(("alpha", "zeta"))
    await cache.aclose()
    assert isinstance(first, _CachedEngine)

    assert first is again
    assert first.extensions == ("alpha", "zeta")
    assert first.workdir == tmp_path / "pg-alpha-zeta"
    assert first.entered
    assert first.exited
    assert cache._engines == {}


@pytest.mark.asyncio
async def test_pglite_engine_cache_uses_bare_slot_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(testing, "PGliteEngine", _CachedEngine)

    engine = await testing._EngineCache(tmp_path).get(())
    assert isinstance(engine, _CachedEngine)
    assert engine.workdir == tmp_path / "pg-bare"


class _Janitor:
    """Records each database a janitor creates, where, and its drop."""

    def __init__(
        self,
        made: list[tuple[object, ...]],
        *,
        user: str,
        host: str,
        port: int,
        dbname: str,
    ) -> None:
        self.made = made
        self._create = ("create", user, host, port, dbname)
        self._dbname = dbname

    def __enter__(self) -> Self:
        self.made.append(self._create)
        return self

    def __exit__(self, *exc: object) -> None:
        del exc
        self.made.append(("drop", self._dbname))


def test_postgres_database_names_its_own_database_and_drops_it(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    janitor = cast(
        ModuleType,
        pytest.importorskip("pytest_postgresql.janitor", exc_type=ImportError),
    )
    made = list[tuple[object, ...]]()
    monkeypatch.setattr(janitor, "DatabaseJanitor", partial(_Janitor, made))
    monkeypatch.setattr(request, "getfixturevalue", _started)

    with testing.postgres_database(request, dbname="gpu_jobs_tests") as dsn:
        held = list(made)

    created = ("create", "postgres", "/run/pg", 5432, "gpu_jobs_tests")
    assert dsn == "postgresql://postgres@/gpu_jobs_tests?host=/run/pg&port=5432"
    assert held == [created]
    assert made == [created, ("drop", "gpu_jobs_tests")]


def test_postgres_server_listens_on_a_socket_of_its_own_and_no_tcp_port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    calls = list[tuple[str, ...]]()
    monkeypatch.setattr(testing, "_pg_ctl", MagicMock(return_value="pg_ctl"))
    monkeypatch.setattr(testing, "_run_pg_ctl", partial(_record, calls))
    server = _server_function(testing.postgres_server)(tmp_path_factory)

    sockets = next(server)
    assert sockets.is_dir()
    assert sockets.name.startswith("pg-")
    with pytest.raises(StopIteration):
        next(server)

    root = Path(calls[0][3]).parent
    assert root.name.startswith("postgres")
    data, log = str(root / "pgdata"), str(root / "server.log")
    options = f"-F -p 5432 -k {sockets} -c listen_addresses=''"
    assert calls == [
        (
            "pg_ctl",
            "initdb",
            "-D",
            data,
            "-o",
            "--username=postgres --auth=trust --no-sync",
            log,
        ),
        ("pg_ctl", "start", "-w", "-D", data, "-l", log, "-o", options, log),
        ("pg_ctl", "stop", "-D", data, "-m", "fast", log),
    ]
    assert not sockets.exists()


def test_postgres_server_skips_without_a_postgresql_install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    monkeypatch.setattr(testing, "_pg_ctl", MagicMock(return_value=None))
    server = _server_function(testing.postgres_server)(tmp_path_factory)

    with pytest.raises(pytest.skip.Exception) as skipped:
        next(server)

    assert str(skipped.value) == (
        "PostgreSQL toolchain unavailable: no pg_ctl beside pg_config or on PATH"
    )


@pytest.mark.db_postgres
@pytest.mark.asyncio
async def test_two_servers_on_one_host_run_at_once(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    start = contextmanager(_server_function(testing.postgres_server))

    with ExitStack() as servers:
        for _ in range(2):
            sockets = servers.enter_context(start(tmp_path_factory))
            conn = await asyncpg.connect(
                f"postgresql://postgres@/postgres?host={sockets}&port=5432",
            )
            try:
                assert await conn.fetchval("SHOW listen_addresses") == ""
            finally:
                await conn.close()


@pytest.mark.parametrize(
    ("on_path", "in_bindir", "found"),
    [
        (("pg_config", "pg_ctl"), ("pg_ctl",), "pgdg/pg_ctl"),
        (("pg_config", "pg_ctl"), (), "bin/pg_ctl"),
        (("pg_ctl",), ("pg_ctl",), "bin/pg_ctl"),
        ((), ("pg_ctl",), None),
    ],
)
def test_pg_ctl_is_found_beside_pg_configs_binaries_then_on_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    on_path: tuple[str, ...],
    in_bindir: tuple[str, ...],
    found: str | None,
) -> None:
    bindir = _executables(tmp_path / "pgdg", in_bindir)
    path = _executables(tmp_path / "bin", on_path)
    monkeypatch.setenv("PATH", str(path))
    # Answers as ``pg_config --bindir`` does; the fake on PATH is never run.
    answer = CompletedProcess(args=(), returncode=0, stdout=f"{bindir}\n")
    run = MagicMock(return_value=answer)
    monkeypatch.setattr(subprocess, "run", run)

    assert testing._pg_ctl() == (None if found is None else str(tmp_path / found))
    asked = call(
        [str(path / "pg_config"), "--bindir"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert run.call_args_list == ([asked] if "pg_config" in on_path else [])


@pytest.mark.parametrize(
    ("logged", "reported"),
    [(True, "out err FATAL"), (False, "out err ")],
)
def test_a_failed_pg_ctl_raises_with_its_output_and_the_servers_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    logged: bool,
    reported: str,
) -> None:
    log = tmp_path / "server.log"
    if logged:
        log.write_text("FATAL")
    answer = CompletedProcess(args=(), returncode=1, stdout="out ", stderr="err ")
    monkeypatch.setattr(subprocess, "run", MagicMock(return_value=answer))

    with pytest.raises(RuntimeError) as raised:
        testing._run_pg_ctl("pg_ctl", "start", log=log)

    assert str(raised.value) == reported


@pytest.mark.parametrize(
    ("platform", "locale"),
    [("darwin", "en_US.UTF-8"), ("linux", "C.UTF-8")],
)
def test_pg_ctl_runs_under_a_pinned_utf8_locale(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    platform: str,
    locale: str,
) -> None:
    answer = CompletedProcess(args=(), returncode=0, stdout="", stderr="")
    run = MagicMock(return_value=answer)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(sys, "platform", platform)

    testing._run_pg_ctl("pg_ctl", "stop", "-D", tmp_path, log=tmp_path / "log")

    run.assert_called_once_with(
        ["pg_ctl", "stop", "-D", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "LC_ALL": locale},
    )


def test_postgres_database_skips_without_libpq(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    # A module set to None in sys.modules raises ImportError, as psycopg does
    # without libpq.
    monkeypatch.setitem(sys.modules, "pytest_postgresql.janitor", None)

    with (
        pytest.raises(pytest.skip.Exception) as skipped,
        testing.postgres_database(request, dbname="gpu_jobs_tests"),
    ):
        pass

    assert str(skipped.value).startswith("PostgreSQL client library unavailable: ")


@pytest.mark.asyncio
async def test_pglite_engine_cache_fixture_closes_cache(tmp_path: Path) -> None:
    fixture = testing.pglite_engine_cache
    assert isinstance(fixture, _CacheFixture)
    generator = fixture._fixture_function(tmp_path)
    await anext(generator)

    await generator.aclose()


def _started(name: str) -> Path:
    """Answer ``getfixturevalue`` with a server's socket directory."""
    assert name == "postgres_server"
    return Path("/run/pg")


def _record(
    calls: list[tuple[str, ...]],
    pg_ctl: str,
    *argv: str | Path,
    log: Path,
) -> None:
    """Record a ``pg_ctl`` run, its log last, in place of running it."""
    calls.append((pg_ctl, *map(str, argv), str(log)))


def _executables(where: Path, names: tuple[str, ...]) -> Path:
    """Make ``where`` holding an executable file per name, never run; return it."""
    where.mkdir()
    for name in names:
        (where / name).touch(mode=0o755)
    return where


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
