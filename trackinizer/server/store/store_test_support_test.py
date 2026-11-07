"""Tests for the helpers the Postgres-backed store tests share."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from trackinizer.conftest import make_store
from trackinizer.sagent.session.types import UserMessage
from trackinizer.server.store.store_test_support import (
    EVERY_SESSION,
    append_user_message,
    fetch_value,
    last_seen,
    open_agent_session,
    polled_session,
    seed_active_user,
)
from trackinizer.wire.audience import AUDIENCES
from trackinizer.wire.bodies import SubmitAgentSession


if TYPE_CHECKING:
    from trackinizer.server.store.core import Store


_SEEN: Final = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class _Scalar:
    """A connection whose ``fetchval`` answers one value and records the query."""

    def __init__(self, value: object) -> None:
        self.value = value
        self.asked: list[tuple[str, tuple[object, ...]]] = []

    async def fetchval(self, query: str, *args: object) -> object:
        self.asked.append((query, args))
        return self.value


def test_every_session_reads_each_audience_and_names_no_session() -> None:
    assert EVERY_SESSION.reader.audiences == AUDIENCES
    assert EVERY_SESSION.reader.every_session
    assert not EVERY_SESSION.names_sessions


@pytest.mark.asyncio
async def test_fetch_value_passes_the_query_and_returns_its_scalar() -> None:
    conn = _Scalar(7)

    assert await fetch_value(conn, "SELECT $1", 7) == 7
    assert conn.asked == [("SELECT $1", (7,))]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_appended_messages_read_back_in_order_within_the_bound(
    integ_store: Store,
) -> None:
    session_id = await _session(integ_store)

    await append_user_message(integ_store, session_id, idx=0, text="one")
    await append_user_message(integ_store, session_id, idx=1, text="two")

    rows = await integ_store.read_session_records(session_id, part=0)
    assert [row.record() for row in rows] == [
        UserMessage(content="one"),
        UserMessage(content="two"),
    ]
    (manifest,) = await integ_store.read_session_manifests(session_id)
    assert (manifest.name, manifest.records) == ("part0.jsonl", 2)


@pytest.mark.asyncio
async def test_seeding_a_user_inserts_an_active_writer_unless_one_exists() -> None:
    store, engine = make_store()

    await seed_active_user(store, "ada@example.com")

    (call,) = engine.conn.execute.call_args_list
    assert call.args[0] == (
        "INSERT INTO users (id, email, name, role, status) "
        "VALUES ($1, $2, 'u', 'writer', 'active') "
        "ON CONFLICT (email) DO NOTHING"
    )
    assert isinstance(call.args[1], UUID)
    assert call.args[2:] == ("ada@example.com",)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_seeding_an_active_user_twice_keeps_one_row(integ_store: Store) -> None:
    await seed_active_user(integ_store, "ada@example.com")
    await seed_active_user(integ_store, "ada@example.com")

    async with integ_store.engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT role, status FROM users WHERE email = 'ada@example.com'",
        )
    assert [(row["role"], row["status"]) for row in rows] == [("writer", "active")]


@pytest.mark.asyncio
async def test_opening_an_agent_session_starts_an_open_agent_run() -> None:
    store, _engine = make_store()
    session_id = uuid4()
    start = AsyncMock(return_value=(session_id, "agent", 0))

    with patch.object(store, "start_session", new=start):
        assert await open_agent_session(store, cli_session_id="cli-1") == session_id

    (call,) = start.call_args_list
    assert call.args == (
        SubmitAgentSession(
            title="reaper",
            cli="claude",
            account="t@e",
            cli_session_id="cli-1",
        ),
    )
    assert call.kwargs == {"requested_actor": "agent", "audience": "open"}


@pytest.mark.asyncio
async def test_last_seen_reads_the_liveness_row() -> None:
    store, engine = make_store()
    session_id = uuid4()
    engine.conn.fetchval = AsyncMock(return_value=_SEEN)

    assert await last_seen(store, session_id) == _SEEN
    engine.conn.fetchval.assert_awaited_once_with(
        "SELECT last_seen FROM session_liveness WHERE session_id = $1",
        session_id,
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_polled_session_is_watched_from_its_poll_and_resumes_by_cli_id(
    store: Store,
) -> None:
    session_id = await polled_session(store, at=_SEEN, cli_session_id="cli-1")

    assert await last_seen(store, session_id) == _SEEN
    assert await open_agent_session(store, cli_session_id="cli-1") == session_id
    assert await open_agent_session(store) != session_id


async def _session(store: Store) -> UUID:
    session_id = uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title, "
            "agentsession_audience) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "'tester@example.com', 'support', 'open')",
            session_id,
        )
    return session_id


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
