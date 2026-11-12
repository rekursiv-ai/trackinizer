"""Tests for the helpers that write asks' records into a session for other tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock
from uuid import uuid4

from treekle import immutable

import pytest

from trackinizer.conftest import make_store
from trackinizer.runs.asks import Option, Resolved, opened_record
from trackinizer.sagent.session.types import (
    AssistantMessage,
    SystemMessage,
    UserMessage,
)
from trackinizer.server import asks_test_support
from trackinizer.server.asks import open_ask_ids
from trackinizer.server.asks_test_support import (
    command_ask,
    ended_ask,
    open_ask,
    resolve_ask,
    write_records,
)
from trackinizer.server.store.session_ir import SessionManifest
from trackinizer.types.session_records import SessionRecordRow
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


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("bound", [None, 1])
async def test_write_records_preserves_part_identity_and_explicit_live_bound(
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
    bound: int | None,
) -> None:
    store, _ = make_store()
    session, ir = uuid4(), uuid4()
    manifest = (
        SessionManifest(
            part=2,
            name="run.jsonl",
            metadata=immutable({}),
            ir_id=ir,
            format="",
            records=3,
        )
        if existing
        else None
    )
    upsert = AsyncMock(return_value=2)
    append = AsyncMock(return_value=1)
    monkeypatch.setattr(
        store,
        "read_session_manifest",
        AsyncMock(return_value=manifest),
    )
    monkeypatch.setattr(store, "upsert_session_manifest", upsert)
    monkeypatch.setattr(store, "append_session_records", append)
    record = UserMessage(content="one")
    await write_records(store, session, [record], bound=bound)
    start = 3 if existing else 0
    assert upsert.call_args is not None
    assert append.call_args is not None
    assert upsert.call_args.args == (session,)
    assert upsert.call_args.kwargs["records"] == (start + 1 if bound is None else bound)
    assert upsert.call_args.kwargs["name"] == "run.jsonl"
    assert upsert.call_args.kwargs["format"] == ""
    assert upsert.call_args.kwargs["metadata"] == {}
    if existing:
        assert upsert.call_args.kwargs["ir_id"] == ir
    else:
        assert isinstance(upsert.call_args.kwargs["ir_id"], type(ir))
    rows = cast(list[SessionRecordRow], append.call_args.args[1])
    assert rows == [
        SessionRecordRow.of(session_id=session, part=2, idx=start, record=record),
    ]


@pytest.mark.asyncio
async def test_open_and_resolve_write_the_expected_interaction_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _ = make_store()
    session = uuid4()
    write = AsyncMock()
    monkeypatch.setattr(asks_test_support, "write_records", write)
    await open_ask(store, session, ask_id="a")
    await resolve_ask(store, session, ask_id="a", by="released")
    records = [
        cast(list[SystemMessage], call.args[2])[0] for call in write.call_args_list
    ]
    assert (
        records[0].extra
        == opened_record(
            command_ask("a"),
            at=datetime.now(UTC),
        ).extra
    )
    assert records[1].extra["$interaction_resolved"] == {
        "id": "a",
        "option": "",
        "by": "released",
        "who": "",
        "decision": "released",
    }
    assert all(record.timestamp for record in records)
    assert all(call.args[:2] == (store, session) for call in write.call_args_list)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
