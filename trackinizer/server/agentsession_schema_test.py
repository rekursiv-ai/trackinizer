"""Schema regression guard for AgentSession on a fresh ``schema.sql`` DB.

Adding the ``AgentSession`` Inquiry kind wove objects through generated DDL
in several places: per-kind columns + CHECKs on ``inquiries``, the
``seq_agentsession`` ref sequence, the ``session_*`` child tables, and the
kind enumerations on ``inquiries`` / ``change_log`` / ``edges``. A fresh
database built from ``schema.sql`` must contain all of them, or every
AgentSession write 500s/409s while reads pass.

This guards the *fresh* schema (the numbered migration that retrofitted
existing databases has been squashed back into ``schema.sql``). It is
``@pytest.mark.db_pglite`` (real PGlite engine) and self-skips when the
in-process Postgres substrate is unavailable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import uuid

import asyncpg
import pytest
import pytest_asyncio

from trackinizer.lib.agent.types.sessions import UserMessage as IRUserMessage
from trackinizer.lib.postgres import PGliteEngine
from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.store.core import Store, StubEmbedder
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.bodies import SubmitAgentSession, SubmitIssue


@pytest_asyncio.fixture(loop_scope="session")
async def store(pglite_engine: PGliteEngine) -> AsyncIterator[Store]:
    """A bootstrapped Store over the session's shared PGlite engine."""
    await reset_schema(pglite_engine)
    store = Store(pglite_engine, embed=StubEmbedder())
    await store.bootstrap()
    yield store


async def _table_exists(store: Store, table: str) -> bool:
    async with store.engine.acquire() as conn:
        return (
            await conn.fetchval(
                "SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = $1",
                table,
            )
            is not None
        )


async def _sequence_exists(store: Store, name: str) -> bool:
    async with store.engine.acquire() as conn:
        return (
            await conn.fetchval(
                "SELECT 1 FROM pg_class WHERE relkind = 'S' AND relname = $1", name
            )
            is not None
        )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_fresh_schema_has_agentsession_objects(store: Store) -> None:
    """A fresh DB carries the record store and the AgentSession ref sequence.

    ``agent_session_events`` is deliberately ABSENT: 021 retired it and the
    baseline no longer declares it, so a fresh install has only the IR tables.
    """
    assert await _table_exists(store, "session_records")
    assert await _table_exists(store, "session_manifests")
    assert not await _table_exists(store, "agent_session_events")
    assert await _sequence_exists(store, "seq_agentsession")


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_agentsession_lifecycle_writes_succeed(store: Store) -> None:
    """A session opens, takes records, and an unrelated Issue write still works.

    Exercises every kind enum AgentSession touches: the ``inquiries`` insert,
    the ``change_log`` audit row, the ``session_records`` append, and an Issue
    write that shares the widened ``change_log`` enum.
    """
    session_id = await store.submit_agentsession(
        SubmitAgentSession(
            title="claude session", cli="claude", account="tester@example.com"
        ),
        api_key_id=None,
        actor="ci",
    )
    assert await store.get_inquiry(session_id) is not None

    written, _skipped, _slash = await store.append_session_records(
        session_id,
        [
            SessionRecordRow.of(
                session_id=session_id,
                part=0,
                idx=0,
                record=IRUserMessage(content="hi"),
            )
        ],
    )
    assert written == 1

    issue_id = await store.submit_issue(
        SubmitIssue(title="probe issue", account="tester@example.com"),
        api_key_id=None,
        actor="ci",
    )
    assert await store.get_inquiry(issue_id) is not None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_submit_agentsession_stamps_opening_api_key(store: Store) -> None:
    """``submit_agentsession`` records the opening ``api_key_id`` on the row.

    The column is attribution + the resume-correlation key (``start_session``
    re-attaches a ``cli_session_id`` only to its original opener), so the open
    path must persist it. A fresh schema must carry the
    ``agentsession_opened_by_api_key_id`` column for the stamp to land.
    """
    # A real ``api_keys`` row: the ``change_log.api_key_id`` audit FK requires
    # the opening credential to exist.
    opener = uuid.uuid4()
    user_id = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, 'opener@test', 'opener', 'writer', 'active')",
            user_id,
        )
        await conn.execute(
            "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
            "VALUES ($1, $2, 'k', 'x', 'trax_opener0', 'writer')",
            opener,
            user_id,
        )
    session_id = await store.submit_agentsession(
        SubmitAgentSession(
            title="claude session", cli="claude", account="tester@example.com"
        ),
        api_key_id=opener,
        actor="ci",
    )
    async with store.engine.acquire() as conn:
        stored = await conn.fetchval(
            "SELECT agentsession_opened_by_api_key_id FROM inquiries WHERE id = $1",
            session_id,
        )
    assert stored == opener


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_create_time_status_is_persisted(store: Store) -> None:
    """An explicit create-time ``status`` is honored, not defaulted to active.

    The submit path threads ``status`` into the INSERT (COALESCE-defaulted to
    'active' only when unset), so a finished artifact -- a run search, a read
    paper -- can be born ``complete`` without a follow-up edit. Bare create
    still defaults to ``active``.
    """
    done = await store.submit_issue(
        SubmitIssue(
            title="already done", status="complete", account="tester@example.com"
        ),
        api_key_id=None,
        actor="ci",
    )
    row = await store.get_inquiry(done)
    assert row is not None
    assert row.status == "complete"

    bare = await store.submit_issue(
        SubmitIssue(title="fresh", account="tester@example.com"),
        api_key_id=None,
        actor="ci",
    )
    fresh = await store.get_inquiry(bare)
    assert fresh is not None
    assert fresh.status == "active", "an unset status is born active (the default)"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_create_time_status_cannot_violate_agentsession_lifecycle(
    store: Store,
) -> None:
    """Create-time ``status`` does NOT bypass the AgentSession lifecycle CHECK.

    The ``ended IS NOT NULL == status = 'complete'`` invariant is a DB table
    constraint (``inquiries_agentsession_lifecycle_check``), so threading
    ``status`` through create cannot mint a born-``complete`` session with no
    ``ended`` -- the INSERT is rejected at the database, not merely in Python.
    Guards that the new create-status path inherits every kind-specific
    lifecycle CHECK by construction.
    """
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await store.submit_agentsession(
            SubmitAgentSession(
                title="born complete",
                cli="claude",
                status="complete",
                account="tester@example.com",
            ),
            api_key_id=None,
            actor="ci",
        )


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
