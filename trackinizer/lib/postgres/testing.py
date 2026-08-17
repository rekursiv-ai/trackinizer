"""Shared PGlite engine fixtures for tests.

Booting PGlite costs ~2.4s; resetting its schema costs ~0.004s (600x). A test
that builds its own :class:`~trackinizer.lib.postgres.PGliteEngine` therefore spends
essentially all of its wall time starting a server, and a package with dozens of
such tests pays that repeatedly for no added isolation -- an empty schema is an
empty schema however it was emptied.

These fixtures boot ONE engine per test session (per xdist worker) and hand each
test a clean schema instead. Import them from a package ``conftest.py``::

    from trackinizer.lib.postgres.testing import pglite_engine, pglite_workdir

    __all__ = ["pglite_engine", "pglite_workdir"]

What sharing does NOT cover: a test that must observe data surviving a real
PROCESS restart. Those construct their own engine against a ``tmp_path`` and are
correct to -- restart durability is the behavior under test. Sharing is for the
common case where the engine is incidental scaffolding.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio

from trackinizer.lib.postgres import PGliteEngine


@pytest.fixture(scope="session")
def pglite_workdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One session-scoped directory backing the shared PGlite server.

    Session-scoped because the engine is: PGlite does not lock its ``dataDir``
    (``substrate.py:118-121``), so two engines sharing a workdir corrupt the
    database. Under xdist each worker is its own process and gets its own
    ``tmp_path_factory`` root, so workers never collide.

    Returns:
      workdir: Directory the shared engine may own for the session.

    """
    workdir = tmp_path_factory.mktemp("pglite-shared")
    assert isinstance(workdir, Path)
    return workdir


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def pglite_engine(pglite_workdir: Path) -> AsyncGenerator[PGliteEngine]:
    """One PGlite server for the whole session, started once.

    ``loop_scope="session"`` is load-bearing, not decoration: the engine holds a
    persistent asyncpg connection, and asyncpg binds a connection to the loop
    that created it. Under the default per-test loop the second test to use this
    fixture fails with ``got Future ... attached to a different loop``. A test
    consuming it must therefore also declare
    ``@pytest.mark.asyncio(loop_scope="session")``.

    Returns:
      engine: A started engine. Call :func:`reset_schema` for an empty schema.

    """
    engine = PGliteEngine(workdir=pglite_workdir / "pg", extensions=())
    async with engine:
        yield engine


async def reset_schema(engine: PGliteEngine) -> None:
    """Drop and recreate ``public``, leaving the server running.

    The isolation the per-test engine provided, at 1/600th the cost. ``CASCADE``
    removes every table, index, sequence, and type the previous test created, so
    the next ``bootstrap()`` sees a database indistinguishable from a fresh one.

    Args:
      engine: A started engine to reset.

    """
    async with engine.acquire() as conn:
        await conn.execute("DROP SCHEMA public CASCADE")
        await conn.execute("CREATE SCHEMA public")
