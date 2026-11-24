"""Real PostgreSQL tests for startup advisory-lock waits under pool timeouts.

PGlite does not enforce ``lock_timeout`` or concurrent backends, so proving that
startup advisory locks wait past the pool's ``lock_timeout`` and leave the
connection's session timeouts intact requires real PostgreSQL.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio

import pytest

from trackinizer.lib import postgres
from trackinizer.server.config import ServiceKeys
from trackinizer.server.notify import NOTIFY_CHANNEL
from trackinizer.server.service_keys import ensure_service_keys
from trackinizer.server.store.core import Store


if TYPE_CHECKING:
    from pathlib import Path

    from trackinizer.lib.postgres import Conn
    from trackinizer.server.auth import Role


pytestmark = [
    pytest.mark.db_postgres,
    pytest.mark.asyncio(loop_scope="session"),
]


class _NoopPolicy:
    """User observer stub for service-key startup tests."""

    async def on_user(self, conn: Conn, *, email: str, role: Role) -> None:
        del conn, email, role


async def test_bootstrap_waits_for_advisory_lock_past_lock_timeout_and_restores_gucs(
    pg_dsn: str,
    integ_store: Store,
) -> None:
    """``bootstrap`` waits past pool ``lock_timeout`` and never leaks ``lock_timeout = 0``."""
    del integ_store
    async with postgres.PostgresEngine(
        dsn=pg_dsn,
        listen_channel=NOTIFY_CHANNEL,
        max_size=4,
        server_settings={"statement_timeout": "500", "lock_timeout": "100"},
    ) as engine:
        held = asyncio.Event()
        release = asyncio.Event()

        async def hold_lock() -> None:
            async with engine.acquire() as conn:
                await conn.execute(
                    "SELECT pg_advisory_lock(hashtext('trackinizer.bootstrap'))",
                )
                held.set()
                try:
                    await release.wait()
                finally:
                    await conn.execute(
                        "SELECT pg_advisory_unlock(hashtext('trackinizer.bootstrap'))",
                    )

        holder = asyncio.create_task(hold_lock())
        await held.wait()
        try:
            boot = asyncio.create_task(Store(engine).bootstrap())
            await asyncio.sleep(
                0.25,
            )  # house-ignore[sleep] -- Hold lock past 100 ms Postgres lock_timeout.
            release.set()
            await boot
        finally:
            release.set()
            await holder

        async with engine.acquire() as conn:
            lock_timeout = await conn.fetchval("SHOW lock_timeout")
            statement_timeout = await conn.fetchval("SHOW statement_timeout")
        assert (lock_timeout, statement_timeout) == ("100ms", "500ms")


async def test_service_keys_waits_for_advisory_lock_past_lock_timeout_and_restores_gucs(
    pg_dsn: str,
    integ_store: Store,
    tmp_path: Path,
) -> None:
    """``ensure_service_keys`` waits past pool ``lock_timeout`` and restores pool GUCs."""
    del integ_store
    async with postgres.PostgresEngine(
        dsn=pg_dsn,
        listen_channel=NOTIFY_CHANNEL,
        max_size=4,
        server_settings={"statement_timeout": "500", "lock_timeout": "100"},
    ) as engine:
        held = asyncio.Event()
        release = asyncio.Event()

        async def hold_lock() -> None:
            async with engine.acquire() as conn:
                await conn.execute(
                    "SELECT pg_advisory_lock(hashtext('trackinizer.service_keys'))",
                )
                held.set()
                try:
                    await release.wait()
                finally:
                    await conn.execute(
                        "SELECT pg_advisory_unlock(hashtext('trackinizer.service_keys'))",
                    )

        async def run_ensure() -> None:
            async with engine.acquire() as conn:
                await ensure_service_keys(
                    conn,
                    keys=ServiceKeys(
                        accounts=(("locktest", "writer"),),
                        directory=tmp_path,
                    ),
                    policy=_NoopPolicy(),
                )
                lock_timeout = await conn.fetchval("SHOW lock_timeout")
                statement_timeout = await conn.fetchval("SHOW statement_timeout")
                assert (lock_timeout, statement_timeout) == ("100ms", "500ms")

        holder = asyncio.create_task(hold_lock())
        await held.wait()
        try:
            task = asyncio.create_task(run_ensure())
            await asyncio.sleep(
                0.25,
            )  # house-ignore[sleep] -- Hold lock past 100 ms Postgres lock_timeout.
            release.set()
            await task
        finally:
            release.set()
            await holder


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
