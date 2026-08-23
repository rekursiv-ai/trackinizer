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

What sharing does NOT cover: a test that must observe data surviving a real
PROCESS restart. Those construct their own engine against a ``tmp_path`` and are
correct to -- restart durability is the behavior under test. Sharing is for the
common case where the engine is incidental scaffolding.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import cast

import contextlib

import pytest
import pytest_asyncio

from trackinizer.lib.postgres import PGliteEngine


@pytest.fixture(scope="session")
def pglite_workdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One session-scoped directory backing the shared PGlite servers.

    Session-scoped because the engines are: PGlite does not lock its ``dataDir``
    (``substrate.py:118-121``), so two engines sharing a workdir corrupt the
    database. Under xdist each worker is its own process and gets its own
    ``tmp_path_factory`` root, so workers never collide.

    Returns:
      workdir: Directory the shared engines may own for the session.

    """
    workdir = tmp_path_factory.mktemp("pglite-shared")
    assert isinstance(workdir, Path)
    return workdir


@pytest_asyncio.fixture(loop_scope="session")
async def pglite_engine(
    pglite_engine_cache: _EngineCache, request: pytest.FixtureRequest
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


async def reset_schema(engine: PGliteEngine) -> None:
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
            "SELECT nspname FROM pg_namespace WHERE oid = pg_my_temp_schema()"
        )
        if temp_schema:
            await conn.execute(f'DROP SCHEMA "{temp_schema}" CASCADE')


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def pglite_engine_cache(pglite_workdir: Path) -> AsyncGenerator[_EngineCache]:
    """Session-lived cache of one started engine per extension set.

    Consumed by :func:`pglite_engine`, which is what a test should ask for.

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
        """Return the started engine for ``extensions``, booting on first ask."""
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
