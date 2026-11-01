"""Tests for who opened a session and what its live run is doing."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import uuid

import pytest

from trackinizer.server.asks_test_support import open_ask
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.inbound import InboundQueue, PollerRun
from trackinizer.server.store.core import Store
from trackinizer.server.visuals.session_runs import (
    live_run_states,
    opener_email,
    settled,
)


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn, PGliteEngine
    from trackinizer.wire.wire_sessions import RunState


_USER: Final = uuid.UUID("11111111-1111-1111-1111-111111111111")
_USER_KEY: Final = uuid.UUID("44444444-4444-4444-4444-444444444441")


async def _seed(engine: PGliteEngine) -> Store:
    store = Store(engine, embed=StubEmbedder())
    async with engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, 'user@example.com', 'T', 'writer', 'active')",
            _USER,
        )
        await conn.execute(
            "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
            "VALUES ($1, $2, 'k', 'h', 'trax_0441', 'writer')",
            _USER_KEY,
            _USER,
        )
    return store


async def _session(conn: Conn, *, owner: str, seq: int) -> uuid.UUID:
    session_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO inquiries (id, kind, seq, status, account, title, owner, "
        "agentsession_opened_by_api_key_id, agentsession_cli, agentsession_audience) "
        "VALUES ($1, 'AgentSession', $2, 'active', 'user@example.com', 'S', $3, $4, "
        "'claude', 'open')",
        session_id,
        seq,
        owner,
        _USER_KEY,
    )
    return session_id


@pytest.mark.parametrize(
    ("said", "waiting", "state"),
    [
        ("idle", False, "idle"),
        ("running", False, "running"),
        ("running", True, "requires_action"),
        ("idle", True, "requires_action"),
        ("taken_over", True, "taken_over"),
        ("taken_over", False, "taken_over"),
    ],
)
def test_a_run_waits_on_an_open_ask_unless_it_was_taken_over(
    said: RunState,
    waiting: bool,
    state: RunState,
) -> None:
    assert settled(PollerRun(state=said), waiting=waiting) == state


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_states_of_some_live_runs_are_read_together(
    pglite_db: PGliteEngine,
) -> None:
    """A session no run polls has no state; the rest say what they do, or what they wait on."""
    store = await _seed(pglite_db)
    inbound = InboundQueue()
    run = "a" * 32
    async with pglite_db.acquire() as conn:
        running, taken, waiting, gone = [
            await _session(conn, owner=f"run-{seq}", seq=seq) for seq in range(1, 5)
        ]
    for session in (running, taken, waiting):
        inbound.mark_poller(session, cli="claude", instance=run)
    assert inbound.report_state(running, instance=run, state="running")
    assert inbound.report_state(taken, instance=run, state="taken_over")
    for session in (taken, waiting, gone):
        await open_ask(store, session, ask_id="a1")

    async with pglite_db.acquire() as conn:
        states = await live_run_states(
            conn,
            inbound=inbound,
            session_ids=[running, taken, waiting, gone],
        )
        nothing = await live_run_states(conn, inbound=inbound, session_ids=[])

    assert states == {
        running: "running",
        taken: "taken_over",
        waiting: "requires_action",
    }
    assert nothing == {}


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_opener_email_names_the_account_of_a_key(
    pglite_db: PGliteEngine,
) -> None:
    """The opening key says whose session it is."""
    await _seed(pglite_db)
    async with pglite_db.acquire() as conn:
        assert await opener_email(conn, api_key_id=_USER_KEY) == "user@example.com"
        assert await opener_email(conn, api_key_id=uuid.uuid4()) is None
        assert await opener_email(conn, api_key_id=None) is None


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
