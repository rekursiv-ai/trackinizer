"""Tests for shared PGlite pytest fixtures."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol, Self, override, runtime_checkable

import pytest

from trackinizer.lib.postgres import PGliteEngine, testing


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Coroutine


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


def _workdir_function(
    fixture: WorkdirFixture,
) -> Callable[[pytest.TempPathFactory], Path]:
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
        del query
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


@pytest.mark.asyncio
async def test_pglite_engine_cache_fixture_closes_cache(tmp_path: Path) -> None:
    fixture = testing.pglite_engine_cache
    assert isinstance(fixture, _CacheFixture)
    generator = fixture._fixture_function(tmp_path)
    await anext(generator)

    await generator.aclose()


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
