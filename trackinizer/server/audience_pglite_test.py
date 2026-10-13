"""The audience SQL fragments keep and drop the right rows on a real database.

The unit tests pin the text of each fragment. This runs them against ``inquiries`` to
show that the parameter numbering, the array cast and the anti-join do what the text
says.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from treekle import from_plain

import pytest
import pytest_asyncio

from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.audience import hidden_exists_sql, visible_sql
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.store.core import Store
from trackinizer.server.values import vetted_sql


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trackinizer.lib.postgres import PGliteEngine


pytestmark = [pytest.mark.db_pglite, pytest.mark.asyncio(loop_scope="session")]


@pytest_asyncio.fixture(loop_scope="session")
async def store(pglite_engine: PGliteEngine) -> AsyncIterator[Store]:
    """Return a bootstrapped Store over the session's shared PGlite engine."""
    await reset_schema(pglite_engine)
    built = Store(pglite_engine, embed=StubEmbedder())
    await built.bootstrap()
    yield built


async def _session(store: Store, audience: str) -> UUID:
    session_id = uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title, "
            "agentsession_audience) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "'tester@example.com', $2, $2)",
            session_id,
            audience,
        )
    return session_id


async def test_visible_keeps_the_sessions_of_an_audience_read(store: Store) -> None:
    open_id = await _session(store, "open")
    staff_id = await _session(store, "staff")
    await _session(store, "partner")

    async def kept(read: frozenset[str]) -> set[UUID]:
        params: list[object] = ["title"]
        clause = visible_sql("i.agentsession_audience", params=params, audiences=read)
        async with store.engine.acquire() as conn:
            rows = await conn.fetch(
                vetted_sql(
                    "SELECT i.id FROM inquiries AS i WHERE i.title <> $1 AND ",
                    clause,
                ),
                *params,
            )
        return {from_plain(row["id"], UUID) for row in rows}

    assert await kept(frozenset({"open"})) == {open_id}
    assert await kept(frozenset({"open", "staff"})) == {open_id, staff_id}
    assert await kept(frozenset()) == set()


async def test_hidden_drops_the_rows_of_a_session_not_read_and_keeps_the_rest(
    store: Store,
) -> None:
    open_id = await _session(store, "open")
    staff_id = await _session(store, "staff")
    no_session = uuid4()

    async def kept(read: frozenset[str]) -> set[UUID]:
        params: list[object] = [[open_id, staff_id, no_session]]
        clause = hidden_exists_sql("t.session_id", params=params, audiences=read)
        async with store.engine.acquire() as conn:
            rows = await conn.fetch(
                vetted_sql(
                    "SELECT t.session_id FROM unnest($1::uuid[]) AS t(session_id) ",
                    "WHERE ",
                    clause,
                ),
                *params,
            )
        return {from_plain(row["session_id"], UUID) for row in rows}

    assert await kept(frozenset({"open"})) == {open_id, no_session}
    assert await kept(frozenset({"open", "staff"})) == {open_id, staff_id, no_session}
    assert await kept(frozenset()) == {no_session}


async def test_hidden_drops_a_session_that_lost_its_audience(store: Store) -> None:
    """A NULL audience is hidden, as ``require_session_access`` hides it.

    The column's CHECK makes a NULL unreachable, so the test stands up a bare
    ``inquiries`` in a scratch schema ahead of ``public`` on the search path, which
    the helper's unqualified name finds first. It is not a temp table: dropping the
    session's temp schema leaves later temp tables beyond ``reset_schema``'s reach.
    """
    lost = uuid4()
    params: list[object] = [lost]
    clause = hidden_exists_sql("$1::uuid", params=params, audiences=frozenset({"open"}))
    async with store.engine.acquire() as conn:
        await conn.execute("CREATE SCHEMA audience_probe")
        await conn.execute(
            "CREATE TABLE audience_probe.inquiries "
            "(id uuid, agentsession_audience text)",
        )
        await conn.execute("SET search_path TO audience_probe, public")
        try:
            await conn.execute("INSERT INTO inquiries VALUES ($1, NULL)", lost)
            kept = await conn.fetchval(
                vetted_sql("SELECT ", clause),
                lost,
                ["open"],
            )
        finally:
            await conn.execute("RESET search_path")
            await conn.execute("DROP SCHEMA audience_probe CASCADE")

    assert kept is False


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
