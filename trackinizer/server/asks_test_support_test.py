"""Tests for the helpers that write asks' records into a session for other tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from trackinizer.runs.asks import Option, Resolved
from trackinizer.sagent.session.types import AssistantMessage, UserMessage
from trackinizer.server.asks import open_ask_ids
from trackinizer.server.asks_test_support import (
    command_ask,
    ended_ask,
    open_ask,
    resolve_ask,
    write_records,
)
from trackinizer.wire.bodies import SubmitAgentSession


if TYPE_CHECKING:
    from uuid import UUID

    from trackinizer.server.store.core import Store


def test_a_command_ask_offers_one_allow_option() -> None:
    ask = command_ask("a1")

    assert (ask.id, ask.provider, ask.kind, ask.command) == (
        "a1",
        "claude",
        "command",
        "ls",
    )
    assert ask.options == (Option(id="yes", label="Yes", kind="allow_once"),)


def test_an_ended_ask_takes_no_option() -> None:
    assert ended_ask("a1", by="released") == Resolved(
        ask_id="a1",
        option="",
        by="released",
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestWritten:
    """What the helpers write reads back as the append route stores it."""

    async def test_batches_append_after_the_records_the_file_holds(
        self,
        store: Store,
    ) -> None:
        session = await _session(store)

        await write_records(store, session, [UserMessage(content="one")])
        await write_records(store, session, [AssistantMessage(content="two")])

        manifest = await store.read_session_manifest(session, name="run.jsonl")
        assert manifest is not None
        assert manifest.records == 2
        rows = await store.read_session_records(session, part=manifest.part)
        assert [(row.idx, row.record()) for row in rows] == [
            (0, UserMessage(content="one")),
            (1, AssistantMessage(content="two")),
        ]

    async def test_a_bound_declares_a_shorter_live_prefix(self, store: Store) -> None:
        session = await _session(store)

        await write_records(
            store,
            session,
            [UserMessage(content="a"), UserMessage(content="b")],
            name="other.jsonl",
            bound=1,
        )

        manifest = await store.read_session_manifest(session, name="other.jsonl")
        assert manifest is not None
        assert manifest.records == 1
        assert await store.read_session_manifest(session, name="run.jsonl") is None

    async def test_an_opened_ask_is_open_until_it_is_resolved(
        self,
        store: Store,
    ) -> None:
        session = await _session(store)

        await open_ask(store, session, ask_id="a")
        await open_ask(store, session, ask_id="b")
        opened = await _open(store, session)
        await resolve_ask(store, session, ask_id="a")

        assert opened == ["a", "b"]
        assert await _open(store, session) == ["b"]


async def _session(store: Store) -> UUID:
    session_id, _, _ = await store.start_session(
        SubmitAgentSession(title="asks", cli="claude", account="t@e"),
        requested_actor="agent",
        audience="open",
    )
    return session_id


async def _open(store: Store, session_id: UUID) -> list[str]:
    async with store.engine.acquire() as conn:
        return await open_ask_ids(conn, session_id=session_id)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
