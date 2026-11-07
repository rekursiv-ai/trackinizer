"""Tiny unit tests for legacy rewrite routing and batched persistence."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import json

from treekle import json_loads, to_plain

import pytest

from trackinizer.conftest import make_conn, make_store, new_uuid
from trackinizer.sagent.session.types import Thinking
from trackinizer.server.store import legacy_retype_runner
from trackinizer.server.store.legacy_retype import SlashCommandOut
from trackinizer.server.store.legacy_retype_runner import RetypeStats, _Source
from trackinizer.types.session_records import payload_record


if TYPE_CHECKING:
    from uuid import UUID

    from trackinizer.lib.postgres import Conn
    from trackinizer.server.store.legacy_retype_runner import _Output


@pytest.mark.asyncio
async def test_shard_totals_include_unchanged_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = make_conn()
    first, second = new_uuid(), new_uuid()
    conn.fetch.return_value = [{"session_id": first}, {"session_id": second}]
    store, _ = make_store(conn)
    rewrite = AsyncMock(
        side_effect=[
            RetypeStats(
                sessions=1,
                rewritten=1,
                records_in=2,
                records_out=3,
                slash_commands=1,
            ),
            RetypeStats(sessions=1),
        ],
    )
    monkeypatch.setattr(legacy_retype_runner, "retype_session", rewrite)
    stats = await legacy_retype_runner.retype_all(store.engine, shards=3, shard=2)
    assert stats == RetypeStats(
        sessions=2,
        rewritten=1,
        records_in=2,
        records_out=3,
        slash_commands=1,
    )
    assert [call.args[1] for call in rewrite.await_args_list] == [first, second]
    assert conn.fetch.call_args_list[0].args[1:] == (-1, 3, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_session_replaces_only_parts_holding_retypable_sources(
    monkeypatch: pytest.MonkeyPatch,
    *,
    legacy: bool,
) -> None:
    conn = make_conn()
    store, _ = make_store(conn)
    source = _source("legacy/UserMessage" if legacy else "legacy/UnknownMessage")
    written: list[list[_Output]] = []

    async def replace_part(
        conn: Conn,
        session_id: UUID,
        outputs: list[_Output],
    ) -> None:
        del conn, session_id
        written.append(outputs)

    read = AsyncMock(return_value=[source])
    swap, commands = AsyncMock(side_effect=replace_part), AsyncMock()
    monkeypatch.setattr(legacy_retype_runner, "_read_sources", read)
    monkeypatch.setattr(legacy_retype_runner, "_replace_part", swap)
    monkeypatch.setattr(legacy_retype_runner, "_insert_slash_commands", commands)
    session = new_uuid()
    async with store.engine.acquire() as held:
        stats = await legacy_retype_runner.retype_session(held, session)
    assert stats == RetypeStats(
        sessions=1,
        rewritten=int(legacy),
        records_in=int(legacy),
        records_out=int(legacy),
    )
    if legacy:
        output = written[0]
        assert len(output) == 1
        assert output[0].kind == "UserMessage"
        assert output[0].text == "hi"
        assert commands.await_args_list[0].args[2] == []
    else:
        swap.assert_not_awaited()
        commands.assert_not_awaited()
    assert [c.args[0] for c in conn.execute.call_args_list] == ["BEGIN", "COMMIT"]


def test_foreign_rows_copy_payload_search_text_and_ciphertext_verbatim() -> None:
    source = replace(
        _source("foreign"),
        kind="Foreign",
        payload_text="{  }",
        ciphertext="sealed",
    )
    commands: list[SlashCommandOut] = []
    [output] = legacy_retype_runner._source_outputs(source, commands)
    assert (output.payload_text, output.text, output.ciphertext) == (
        "{  }",
        "hi",
        "sealed",
    )
    assert (output.created, output.timestamp, output.model, output.context_id) == (
        source.created,
        source.timestamp,
        source.model,
        source.context_id,
    )
    assert commands == []


@pytest.mark.parametrize("stamped", [False, True])
def test_slash_commands_route_out_of_stream_with_capture_time_fallback(
    *,
    stamped: bool,
) -> None:
    source = replace(
        _source("legacy/SlashCommand"),
        timestamp=datetime(2026, 9, 2, tzinfo=UTC) if stamped else None,
        payload_text='{"kind":"legacy/SlashCommand","payload":{"command":"model","args":"tiny"}}',
    )
    commands: list[SlashCommandOut] = []
    assert legacy_retype_runner._source_outputs(source, commands) == []
    assert commands == [
        SlashCommandOut(
            timestamp=(source.timestamp or source.created).isoformat(),
            command="model",
            args="tiny",
        ),
    ]


def test_assistant_fanout_projects_text_and_moves_ciphertext_to_thinking() -> None:
    source = replace(
        _source("legacy/AssistantMessage"),
        ciphertext="sealed",
        timestamp=datetime(2026, 9, 2, tzinfo=UTC),
        payload_text='{"kind":"legacy/AssistantMessage","payload":{"text":"hi","thinking":"why"}}',
    )
    outputs = legacy_retype_runner._source_outputs(source, [])
    assert [(o.kind, o.text, o.ciphertext) for o in outputs] == [
        ("AssistantMessage", "hi", ""),
        ("Thinking", "why", "sealed"),
    ]
    assert all(o.created == source.created and o.model == "tiny" for o in outputs)
    assert all(
        o.timestamp == source.timestamp and o.context_id is None for o in outputs
    )
    thinking = payload_record(json_loads(outputs[1].payload_text), Thinking)
    assert thinking.encrypted == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("sealed", [False, True])
async def test_part_replacement_batches_rows_and_preserves_ciphertext_indexes(
    *,
    sealed: bool,
) -> None:
    conn = make_conn()
    store, _ = make_store(conn)
    output = legacy_retype_runner._source_outputs(_source("legacy/UserMessage"), [])[0]
    outputs = [output, replace(output, ciphertext="sealed" if sealed else "")]
    session = new_uuid()
    async with store.engine.acquire() as held:
        await legacy_retype_runner._replace_part(held, session, outputs)
    calls = conn.execute.call_args_list
    assert [str(c.args[0]).split()[0] for c in calls] == (
        ["DELETE", "DELETE", "INSERT", "INSERT", "UPDATE"]
        if sealed
        else ["DELETE", "DELETE", "INSERT", "UPDATE"]
    )
    assert calls[2].args[1:5] == (
        session,
        -1,
        ["UserMessage", "UserMessage"],
        [None, None],
    )
    if sealed:
        assert calls[3].args[1:] == (session, -1, [1], [b"sealed"])
    assert calls[-1].args[1:] == (session, -1, 2)


@pytest.mark.asyncio
async def test_empty_replacement_clears_the_manifest_without_inserting_rows() -> None:
    conn = make_conn()
    store, _ = make_store(conn)
    session = new_uuid()
    async with store.engine.acquire() as held:
        await legacy_retype_runner._replace_part(held, session, [])
        await legacy_retype_runner._insert_slash_commands(held, session, [])
    assert len(conn.execute.call_args_list) == 3
    assert conn.execute.call_args_list[-1].args[1:] == (session, -1, 0)


@pytest.mark.asyncio
async def test_commands_are_written_as_parallel_arrays_after_the_existing_sequence() -> (
    None
):
    conn = make_conn()
    store, _ = make_store(conn)
    session = new_uuid()
    async with store.engine.acquire() as held:
        await legacy_retype_runner._insert_slash_commands(
            held,
            session,
            [
                SlashCommandOut(timestamp="then", command="model", args="tiny"),
            ],
        )
    [call] = conn.execute.call_args_list
    assert call.args[1:] == (session, ["then"], ["model"], ["tiny"])
    assert "max(seq) + 1" in str(call.args[0])


def _source(kind: str) -> _Source:
    return _Source(
        kind="UncategorizedRecord",
        context_id=None,
        timestamp=None,
        model="tiny",
        created=datetime(2026, 9, 1, tzinfo=UTC),
        payload_text=json.dumps(to_plain({"kind": kind, "payload": {"text": "hi"}})),
        text="hi",
        legacy_kind=kind,
        ciphertext="",
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
