"""The sweep on real PostgreSQL, where the server's statement timeout fires.

PGlite ignores ``statement_timeout`` (``metrics_routes_pg_test``), so only a real
server can show a backlog page being cancelled by the timeout every server
connection carries (``build_engine``).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from trackinizer.addons.embedding.fakes import HashEmbedder, vector_store
from trackinizer.addons.embedding.sweep import SweepState, sweep_once
from trackinizer.addons.embedding.units import Settings, settings_key
from trackinizer.lib.postgres import Conn, DatabaseEngine


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from trackinizer.lib.postgres import PostgresEngine
    from trackinizer.server.store.core import Store


pytestmark = [
    pytest.mark.db_postgres,
    pytest.mark.asyncio(loop_scope="session"),
]


@pytest_asyncio.fixture(loop_scope="session")
async def store(integ_engine: PostgresEngine) -> Store:
    """Return a store whose database holds the vector table, every table empty."""
    return await vector_store(integ_engine)


async def test_a_backlog_page_walks_past_the_servers_statement_timeout(
    store: Store,
) -> None:
    """A page that walks many settled records to reach a pending one is not cancelled.

    Over ~10M records a walk took 37 s, past the server's 30 s timeout, so a page
    was cancelled on every round and nothing new was embedded.
    """
    model = HashEmbedder(dim=4)
    kinds = ("UserMessage",)
    settings = Settings(
        key=settings_key(
            model=model.name,
            dim=model.dim,
            chunk_chars=1_500,
            overlap_chars=7,
            session_kinds=kinds,
        ),
        dim=model.dim,
        chunk_chars=1_500,
        overlap_chars=7,
        session_kinds=kinds,
    )
    async with store.engine.acquire() as conn:
        await _records(conn, text="find me", count=1)
        settled = await _records(conn, text="done", count=300_000)
        # Every newer record already has its current vector, as nearly all do on a
        # long-running server, so the page tests each before it reaches the pending.
        await conn.execute(
            "INSERT INTO embedding.vectors "
            "(owner_id, part, idx, field, chunk, text_md5, settings, embedding) "
            "SELECT session_id, part, idx, 'content', 0, md5(text), $2, "
            "'[1,0,0,0]'::halfvec FROM session_records WHERE session_id = $1",
            settled,
            settings.key,
        )

    state = await sweep_once(
        cast("DatabaseEngine", _Timed(engine=store.engine, timeout_ms=50)),
        model=model,
        settings=settings,
        state=SweepState(),
        slice_units=4,
    )

    assert model.calls == [["find me"]]
    assert state.written == 1


# Each call is its own statement, so a later call's records are newer and the backlog,
# newest first, walks them before an earlier call's.
async def _records(conn: Conn, *, text: str, count: int) -> UUID:
    """Insert a session of ``count`` user messages of ``text``; return its id."""
    session_id = uuid4()
    await conn.execute(
        "INSERT INTO inquiries (id, kind, seq, status, account, title, "
        "agentsession_audience) "
        "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
        "'tester@example.com', 'embed test', 'open')",
        session_id,
    )
    await conn.execute(
        "INSERT INTO session_records (session_id, part, idx, kind, payload, text) "
        "SELECT $1, 0, idx, 'UserMessage', '{}'::json, $2 "
        "FROM generate_series(0, $3 - 1) idx",
        session_id,
        text,
        count,
    )
    await conn.execute(
        "INSERT INTO session_manifests "
        "(session_id, part, name, metadata, ir_id, format, records) "
        "VALUES ($1, 0, 's.jsonl', '{}'::json, gen_random_uuid(), 'claude', $2)",
        session_id,
        count,
    )
    return session_id


@dataclass(slots=True, kw_only=True)
class _Timed:
    """An engine whose connections carry a statement timeout, as the server's do."""

    engine: DatabaseEngine
    timeout_ms: int

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[Conn]:
        """Lend a connection of the wrapped engine under ``timeout_ms``."""
        async with self.engine.acquire() as conn:
            await conn.execute(f"SET statement_timeout = {self.timeout_ms}")
            try:
                yield conn
            finally:
                await conn.execute("RESET statement_timeout")


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
