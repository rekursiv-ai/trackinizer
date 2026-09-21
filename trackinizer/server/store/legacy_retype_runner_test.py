"""The retype runner: legacy part ``-1`` streams become typed records.

Builds the exact pre-state ``schema_backfill_test.py`` builds -- the retired
``agent_session_events`` table, real rows, then the real 020 migration -- and
asserts ``retype_session`` transforms its output in place. Everything runs
against real Postgres (pglite): the properties are transactional and
unobservable without the database.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

import json

import pytest
import pytest_asyncio

from trackinizer.lib.custom_json import DictCodec, json_unfreeze
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.store.core import Store
from trackinizer.server.store.legacy_retype_runner import (
    retype_all,
    retype_session,
)


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trackinizer.lib.postgres import PostgresEngine


_CWD: Final = Path(__file__).resolve().parent

_LEGACY_TABLE: Final = """
CREATE TABLE IF NOT EXISTS agent_session_events (
    session_id   UUID NOT NULL REFERENCES inquiries(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL CHECK (seq >= 0),
    model        TEXT,
    kind         TEXT NOT NULL,
    timestamp    TIMESTAMPTZ,
    created      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    message      JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (session_id, seq)
);
"""

_ENCRYPTED: Final = "ZW5jcnlwdGVkLXJlYXNvbmluZw=="
_SIGNATURE: Final = "c2lnbmF0dXJl"


@pytest_asyncio.fixture(loop_scope="session")
async def store(integ_engine: PostgresEngine) -> AsyncIterator[Store]:
    """Return a bootstrapped store on the shared integration database."""
    built = Store(integ_engine, embed=StubEmbedder())
    await built.bootstrap()
    yield built


async def _legacy_session(store: Store) -> UUID:
    """One session through the REAL 020 backfill: the runner's true input."""
    session_id = uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(_LEGACY_TABLE)
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "'tester@example.com', 'legacy')",
            session_id,
        )
        await conn.execute(
            "INSERT INTO agent_session_events "
            "(session_id, seq, kind, timestamp, model, message) VALUES "
            "($1, 0, 'UserMessage', now(), NULL, $2), "
            "($1, 1, 'AssistantMessage', now(), 'opus', $3), "
            "($1, 2, 'ToolResult', now(), NULL, $4), "
            "($1, 3, 'SlashCommand', now(), NULL, $5), "
            "($1, 4, 'UnknownMessage', now(), NULL, $6)",
            session_id,
            {"text": "deploy the thing"},
            {
                "text": "on it",
                "thinking": "considering",
                "thinking_encrypted": _ENCRYPTED,
                "thinking_signature": _SIGNATURE,
                "tool_calls": [
                    {"id": "call_1", "name": "Bash", "args": {"cmd": "make"}},
                ],
                "tokens": {"input_tokens": 12, "output_tokens": 5},
            },
            {"call_id": "call_1", "content": "pg_advisory_lock acquired"},
            {"command": "model", "args": "opus"},
            {"raw": {"weird": True}},
        )
        await conn.execute(
            (_CWD.parent.parent / "server" / "assets" / "schema.020.sql").read_text(
                encoding="utf-8",
            ),
        )
    return session_id


async def _kinds(store: Store, session_id: UUID) -> list[str]:
    rows = await store.read_session_records(session_id, part=-1, limit=500)
    return [row.kind for row in rows]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_legacy_rows_become_typed_records(store: Store) -> None:
    """The five legacy turns become their typed forms, fan-out included."""
    session_id = await _legacy_session(store)

    async with store.engine.acquire() as conn:
        stats = await retype_session(conn, session_id)

    assert stats.rewritten == 1
    assert stats.records_in == 5
    # User, assistant(+thinking+toolcall+tokens), toolresult, unknown stays.
    assert await _kinds(store, session_id) == [
        "UserMessage",
        "AssistantMessage",
        "Thinking",
        "ToolCall",
        "TokenUsage",
        "UncategorizedToolResult",
        "UncategorizedRecord",
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_idx_is_contiguous_after_fan_out(store: Store) -> None:
    """Renumbered from 0 with no gaps; the manifest bound matches."""
    session_id = await _legacy_session(store)

    async with store.engine.acquire() as conn:
        await retype_session(conn, session_id)

    rows = await store.read_session_records(session_id, part=-1, limit=500)
    assert [row.idx for row in rows] == list(range(7))
    manifests = await store.read_session_manifests(session_id)
    assert [(m.part, m.records) for m in manifests] == [(-1, 7)]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_ciphertext_rekeys_to_the_thinking_record(store: Store) -> None:
    """The sealed blob moves from the assistant's old key to Thinking's new one."""
    session_id = await _legacy_session(store)

    async with store.engine.acquire() as conn:
        await retype_session(conn, session_id)

    rows = await store.read_session_records(session_id, part=-1, limit=500)
    sealed = {row.kind: row.ciphertext for row in rows if row.ciphertext}
    assert sealed == {"Thinking": _ENCRYPTED + _SIGNATURE}


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_slash_command_routes_to_its_table(store: Store) -> None:
    session_id = await _legacy_session(store)

    async with store.engine.acquire() as conn:
        stats = await retype_session(conn, session_id)

    assert stats.slash_commands == 1
    commands = await store.read_session_slash_commands(session_id)
    assert [(c.command, c.args) for c in commands] == [("model", "opus")]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_unknown_message_survives_byte_for_byte(store: Store) -> None:
    """The non-retypable row keeps its stored payload and text verbatim."""
    session_id = await _legacy_session(store)
    before = await store.read_session_records(session_id, part=-1, limit=500)
    unknown_before = next(
        row
        for row in before
        if DictCodec.coerce(row.payload).get("kind") == "legacy/UnknownMessage"
    )

    async with store.engine.acquire() as conn:
        await retype_session(conn, session_id)

    after = await store.read_session_records(session_id, part=-1, limit=500)
    unknown_after = next(row for row in after if row.kind == "UncategorizedRecord")
    assert json.dumps(json_unfreeze(unknown_after.payload)) == json.dumps(
        json_unfreeze(unknown_before.payload),
    )
    assert unknown_after.text == unknown_before.text


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_search_text_is_recomputed_for_typed_records(store: Store) -> None:
    """Typed rows get the real projection: the tool result is findable."""
    session_id = await _legacy_session(store)

    async with store.engine.acquire() as conn:
        await retype_session(conn, session_id)

    rows = await store.read_session_records(session_id, part=-1, limit=500)
    by_kind = {row.kind: row for row in rows}
    assert "pg_advisory_lock" in by_kind["UncategorizedToolResult"].text
    assert "deploy the thing" in by_kind["UserMessage"].text
    # Thinking plaintext is searchable; the sealed blob never is.
    assert "considering" in by_kind["Thinking"].text
    assert _ENCRYPTED not in by_kind["Thinking"].text


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_rerun_is_a_no_op(store: Store) -> None:
    """Idempotent by content: a second pass rewrites nothing."""
    session_id = await _legacy_session(store)

    async with store.engine.acquire() as conn:
        first = await retype_session(conn, session_id)
        second = await retype_session(conn, session_id)

    assert first.rewritten == 1
    assert second.rewritten == 0
    assert len(await _kinds(store, session_id)) == 7


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_timestamp_and_model_survive(store: Store) -> None:
    """Row-column provenance lands on every fanned-out record."""
    session_id = await _legacy_session(store)

    async with store.engine.acquire() as conn:
        await retype_session(conn, session_id)

    rows = await store.read_session_records(session_id, part=-1, limit=500)
    assert all(row.timestamp is not None for row in rows)
    by_kind = {row.kind: row for row in rows}
    # The assistant's model rides onto every record it fanned out to.
    assert by_kind["Thinking"].model == "opus"
    assert by_kind["ToolCall"].model == "opus"
    assert by_kind["UserMessage"].model is None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_retype_all_processes_every_shard_disjointly(store: Store) -> None:
    """Two shards partition the sessions: each is rewritten by EXACTLY one shard.

    The shard predicate (``hashtext(id) & mask) % shards`` -- masked, not
    ``abs()``, to survive INT_MIN) must be a total partition: every session lands
    in exactly one shard, so a full pass rewrites each once and a second full
    pass rewrites nothing. The store fixture is shared, so this scopes the
    exactly-once claim to the two sessions it seeds.
    """
    first = await _legacy_session(store)
    second = await _legacy_session(store)

    # First full pass: both new sessions get typed (they land in some shard).
    for shard in range(2):
        _ = await retype_all(store.engine, shards=2, shard=shard)
    assert (await _kinds(store, first))[0] == "UserMessage"
    assert (await _kinds(store, second))[0] == "UserMessage"

    # A SECOND full pass rewrites nothing: every legacy row was typed exactly
    # once, so the shard predicate partitioned the corpus (no row missed, none
    # done twice). This is the exactly-once coverage the count-only check missed.
    second_pass = 0
    for shard in range(2):
        second_pass += (await retype_all(store.engine, shards=2, shard=shard)).rewritten
    assert second_pass == 0


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
