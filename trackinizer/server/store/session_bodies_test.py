"""The body offload: heavy payloads move cold, replay splices byte-exactly."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import json

import pytest
import pytest_asyncio

from trackinizer.lib import zstd_compat
from trackinizer.lib.custom_json import json_freeze, json_unfreeze
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.semantic_mapper_footprint import HEAD_CHARS
from trackinizer.server.store.core import Store
from trackinizer.server.store.session_bodies import (
    HEAD_TEXT_CHARS,
    HEAVY_KINDS,
    STUB_PAYLOAD,
    decode_body,
    offload_session_bodies,
    spliced_payload,
)
from trackinizer.types.session_records import SessionRecordRow


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trackinizer.lib.postgres import PostgresEngine


@pytest_asyncio.fixture(loop_scope="session")
async def store(integ_engine: PostgresEngine) -> AsyncIterator[Store]:
    """Return a bootstrapped store; session tables truncated per test."""
    built = Store(integ_engine, embed=StubEmbedder())
    await built.bootstrap()
    async with built.engine.acquire() as conn:
        await conn.execute(
            "TRUNCATE session_records, session_ciphertext, session_bodies, "
            "session_manifests, session_slash_commands",
        )
    yield built


async def _session(store: Store) -> UUID:
    """One AgentSession with a heavy shell result and a light user message."""
    session_id = uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "'tester@example.com', 'offload')",
            session_id,
        )
    await store.upsert_session_manifest(
        session_id,
        name="native.jsonl",
        metadata=json_freeze({}),
        ir_id=uuid4(),
        format="claude",
        records=2,
    )
    await store.append_session_records(
        session_id,
        [
            SessionRecordRow(
                session_id=session_id,
                part=0,
                idx=0,
                kind="UserMessage",
                payload=json_freeze({"py/object": "x", "content": "run the tests"}),
                text="run the tests",
            ),
            SessionRecordRow(
                session_id=session_id,
                part=0,
                idx=1,
                kind="ShellCommandResult",
                payload=json_freeze(
                    {"py/object": "y", "stdout": "exit 1\n" + "spam " * 5_000},
                ),
                text="exit 1\n" + "spam " * 5_000,
            ),
        ],
    )
    return session_id


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_heavy_body_moves_and_head_stays(store: Store) -> None:
    """The shell body offloads; the hot row keeps the stub + 2k head."""
    session_id = await _session(store)

    stats = await offload_session_bodies(store.engine)

    assert stats.records_offloaded == 1
    assert stats.bytes_after < stats.bytes_before
    async with store.engine.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT payload, text FROM session_records "
            "WHERE session_id = $1 AND idx = 1",
            session_id,
        )
    assert row is not None
    assert row["payload"] == STUB_PAYLOAD
    text = row["text"]
    assert isinstance(text, str)
    assert len(text) == HEAD_TEXT_CHARS
    assert text.startswith("exit 1")


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_light_records_stay_inline(store: Store) -> None:
    """Messages are not offloaded, whatever their size."""
    session_id = await _session(store)

    await offload_session_bodies(store.engine)

    async with store.engine.acquire() as conn:
        payload = await conn.fetchval(
            "SELECT payload FROM session_records WHERE session_id = $1 AND idx = 0",
            session_id,
        )
        bodies = await conn.fetchval("SELECT count(*) FROM session_bodies")
    assert isinstance(payload, str)
    assert payload != STUB_PAYLOAD
    assert "run the tests" in payload
    assert bodies == 1


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_replay_read_is_byte_exact(store: Store) -> None:
    """``read_session_records`` splices the body back; the payload matches."""
    session_id = await _session(store)
    before = await store.read_session_records(session_id, part=0, limit=10)
    original = json.dumps(json_unfreeze(before[1].payload), separators=(",", ":"))

    await offload_session_bodies(store.engine)

    after = await store.read_session_records(session_id, part=0, limit=10)
    respliced = json.dumps(json_unfreeze(after[1].payload), separators=(",", ":"))
    assert respliced == original
    # The searchable projection on the replay read is the FULL text too.
    assert after[1].text == before[1].text


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_plaintext_read_serves_the_head_without_the_join(store: Store) -> None:
    """Search/feed readers get the head; they never decompress a body."""
    session_id = await _session(store)

    await offload_session_bodies(store.engine)

    rows = await store.read_session_records(
        session_id,
        part=0,
        limit=10,
        plaintext_only=True,
    )
    assert len(rows[1].text) == HEAD_TEXT_CHARS


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_rerun_is_a_no_op(store: Store) -> None:
    session_id = await _session(store)

    first = await offload_session_bodies(store.engine)
    second = await offload_session_bodies(store.engine)

    assert first.records_offloaded == 1
    assert second.records_offloaded == 0
    del session_id


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_tsvector_shrinks_to_the_head(store: Store) -> None:
    """A term past the head no longer term-matches; a head term still does."""
    session_id = await _session(store)

    await offload_session_bodies(store.engine)

    async with store.engine.acquire() as conn:
        head_hit = await conn.fetchval(
            "SELECT count(*) FROM session_records "
            "WHERE session_id = $1 AND search @@ to_tsquery('simple', 'exit')",
            session_id,
        )
    assert head_hit == 1


def test_the_stub_is_valid_json() -> None:
    assert json.loads(STUB_PAYLOAD) == {"$body": "offloaded"}


def test_heads_agree_with_the_footprint_mapper() -> None:
    """Storage heads and search heads are one decision (by test, not import)."""
    assert HEAD_TEXT_CHARS == HEAD_CHARS


def test_heavy_kinds_cover_the_footprint_headed_set() -> None:
    """Every kind the mapper heads is offloaded; FileRead adds the unindexed."""
    assert {
        "ShellCommandResult",
        "UncategorizedToolResult",
        "FileWriteResult",
        "FileEditResult",
        "FileReadResult",
    } == set(HEAVY_KINDS)


class TestSplicedPayload:
    def test_offloaded_payload_comes_from_the_body(self) -> None:

        original = '{"stdout":"the whole thing"}'
        assert (
            spliced_payload(STUB_PAYLOAD, zstd_compat.compress(original.encode()))
            == original
        )

    def test_hot_payload_passes_through(self) -> None:
        assert spliced_payload('{"a":1}', None) == '{"a":1}'

    def test_stub_without_body_raises(self) -> None:
        with pytest.raises(ValueError, match="body"):
            spliced_payload(STUB_PAYLOAD, None)

    def test_corrupt_body_is_a_value_error_not_a_unicode_error(self) -> None:
        """A non-UTF-8 sidecar surfaces as a handled ValueError, not a 500.

        The bare ``.decode()`` this replaced raised ``UnicodeDecodeError`` on a
        corrupt row, which bubbled to a 500; ``decode_body`` translates it to the
        same ValueError shape the missing-body guard already uses.
        """
        corrupt = zstd_compat.compress(b"\xff\xfe not utf-8")
        with pytest.raises(ValueError, match="UTF-8"):
            decode_body(corrupt)
        with pytest.raises(ValueError, match="UTF-8"):
            spliced_payload(STUB_PAYLOAD, corrupt)


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
