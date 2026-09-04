"""The 020 backfill: legacy turns become IR records, losing nothing reachable.

Every test runs the real migration against real Postgres. The properties are
the migration's -- the codec tag it writes, the ciphertext it moves out, the
conflict clause that makes a partial re-run safe -- and none of them is
observable without executing the SQL.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4

import json

import pytest
import pytest_asyncio

from trackinizer.lib.agent.types.sessions import UncategorizedRecord
from trackinizer.lib.custom_json import (
    DataclassCodec,
    DictCodec,
    json_freeze,
    json_unfreeze,
)
from trackinizer.lib.postgres import PostgresEngine
from trackinizer.server.store.core import Store, StubEmbedder
from trackinizer.types.session_records import SessionRecordRow


_MIGRATION: Final = Path(__file__).resolve().parent / "assets" / "schema.020.sql"

# The table as it stood before 021 dropped it, trimmed to the columns 020
# reads. Not imported from the baseline schema (which no longer has it) and
# not a fixture file: the migration's contract is with THESE columns, so the
# test states them.
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
    """A bootstrapped store on the shared integration database."""
    built = Store(integ_engine, embed=StubEmbedder())
    await built.bootstrap()
    yield built


async def _legacy_session(store: Store) -> UUID:
    """A session holding legacy ``agent_session_events`` rows only.

    Written directly, because the append path this exercises is the one phase
    7 deletes: the point is what the migration does with rows that already
    exist in a deployed database.
    """
    session_id = uuid4()
    async with store.engine.acquire() as conn:
        # RECREATE the retired table. 021 drops it and the baseline no longer
        # declares it, so a bootstrapped database has none -- but the whole
        # point of 020 is what it does to a database that PREDATES that drop.
        # Building the pre-migration state is the only way to exercise it.
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
            "($1, 2, 'ToolResult', now(), NULL, $4)",
            session_id,
            # DICTS, not JSON strings: asyncpg encodes a ``str`` bound to a
            # jsonb parameter as a JSON *string*, so ``$1::jsonb`` would store
            # the scalar ``"{...}"`` rather than an object -- which is not what
            # the production append path writes.
            {"text": "deploy the thing"},
            {
                "text": "on it",
                "thinking": "considering",
                "thinking_encrypted": _ENCRYPTED,
                "thinking_signature": _SIGNATURE,
            },
            {"content": "pg_advisory_lock acquired"},
        )
    return session_id


async def _run_backfill(store: Store) -> None:
    """Execute 020 exactly as ``_bootstrap_once`` would."""
    async with store.engine.acquire() as conn:
        await conn.execute(_MIGRATION.read_text(encoding="utf-8"))


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_fresh_install_has_no_table_to_convert(store: Store) -> None:
    """020 must be a no-op where the legacy table never existed.

    The baseline no longer declares ``agent_session_events`` (021 retired it),
    so on a NEW database every statement in 020 references a missing relation.
    A plain ``IF to_regclass(...) THEN`` does not save it: PL/pgSQL parses the
    whole block at compile time, so the reference fails before the guard runs
    -- which means a fresh install cannot bootstrap at all. The statements are
    ``EXECUTE``'d as text for exactly this, and this is the test that bites.
    """
    async with store.engine.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS agent_session_events")

    await _run_backfill(store)  # must not raise


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_legacy_turns_become_records_at_part_minus_one(store: Store) -> None:
    """A reserved namespace, so a resumed session's own parts cannot collide."""
    session_id = await _legacy_session(store)

    await _run_backfill(store)

    rows = await store.read_session_records(session_id, part=-1, limit=500)
    assert [row.idx for row in rows] == [0, 1, 2]
    assert {row.kind for row in rows} == {"UncategorizedRecord"}


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_backfilled_row_decodes_through_the_same_path(store: Store) -> None:
    """The migration writes what ``DataclassCodec`` reads -- proven by reading.

    A tag that drifts from the class's dotted name leaves every legacy row
    undecodable, and nothing else in the system would notice until a reader
    tried.
    """
    session_id = await _legacy_session(store)

    await _run_backfill(store)

    rows = await store.read_session_records(session_id, part=-1, limit=500)
    records = [row.record() for row in rows]
    assert all(isinstance(record, UncategorizedRecord) for record in records)


def test_the_dotted_tag_matches_what_the_codec_emits() -> None:
    """Pinned against the codec, not restated: a moved class fails HERE.

    No database: the claim is about the MIGRATION TEXT agreeing with what the
    codec emits, which is decidable by reading both.
    """
    emitted = DataclassCodec.to_json(UncategorizedRecord(kind="x"))
    assert (
        emitted["py/object"]
        == "trackinizer.lib.agent.types.sessions.UncategorizedRecord"
    )
    assert str(emitted["py/object"]) in _MIGRATION.read_text(encoding="utf-8")


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_original_kind_survives_as_provenance(store: Store) -> None:
    """Every legacy turn is Uncategorized; the union it came from is kept.

    Promoting to a typed IR record would assert structure the lossy capture
    never recorded.
    """
    session_id = await _legacy_session(store)

    await _run_backfill(store)

    rows = await store.read_session_records(session_id, part=-1, limit=500)
    kinds = [DictCodec.coerce(row.payload).get("kind") for row in rows]
    assert kinds == [
        "legacy/UserMessage",
        "legacy/AssistantMessage",
        "legacy/ToolResult",
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_backfill_is_idempotent(store: Store) -> None:
    """A migration that partially applied and re-ran must not double a session."""
    session_id = await _legacy_session(store)

    await _run_backfill(store)
    await _run_backfill(store)

    rows = await store.read_session_records(session_id, part=-1, limit=500)
    assert [row.idx for row in rows] == [0, 1, 2]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_legacy_tool_result_is_findable_by_its_content(store: Store) -> None:
    """Searchability is the point of the backfill, not just preservation."""
    session_id = await _legacy_session(store)

    await _run_backfill(store)

    rows = await store.read_session_records(session_id, part=-1, limit=500)
    assert any("pg_advisory_lock" in row.text for row in rows)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_no_payload_retains_ciphertext(store: Store) -> None:
    """Left inline it would be indexed by nothing and deletable by nothing."""
    session_id = await _legacy_session(store)

    await _run_backfill(store)

    rows = await store.read_session_records(session_id, part=-1, limit=500)
    for row in rows:
        # ``json_unfreeze`` first: the stored payload is frozen (mappingproxy
        # / tuple), which ``json.dumps`` refuses.
        rendered = json.dumps(json_unfreeze(row.payload))
        assert _ENCRYPTED not in rendered
        assert _SIGNATURE not in rendered


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_ciphertext_moves_to_its_own_table(store: Store) -> None:
    """Moved, not dropped: the retention lever must be able to reach it.

    Concatenated in the order the provider wrote them -- claude folds its
    signature into the blob, and the legacy union split the two apart.
    """
    session_id = await _legacy_session(store)

    await _run_backfill(store)

    rows = await store.read_session_records(session_id, part=-1, limit=500)
    sealed = [row.ciphertext for row in rows if row.ciphertext]
    assert sealed == [_ENCRYPTED + _SIGNATURE]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_legacy_created_and_model_survive(store: Store) -> None:
    """Both are columns on the record, not payload -- the feed reads them."""
    session_id = await _legacy_session(store)

    await _run_backfill(store)

    rows = await store.read_session_records(session_id, part=-1, limit=500)
    assert [row.model for row in rows] == [None, "opus", None]
    assert all(row.timestamp is not None for row in rows)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_manifest_makes_the_session_readable(store: Store) -> None:
    """Without one the session reads as zero records: readers bound by it."""
    session_id = await _legacy_session(store)

    await _run_backfill(store)

    manifests = await store.read_session_manifests(session_id)
    assert [(m.part, m.records) for m in manifests] == [(-1, 3)]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_legacy_session_is_never_resumable(store: Store) -> None:
    """Empty ``format``: there is no native file these turns rewrite as.

    Phase 6 refuses a part with no format rather than producing a file the CLI
    would reject.
    """
    session_id = await _legacy_session(store)

    await _run_backfill(store)

    manifests = await store.read_session_manifests(session_id)
    assert manifests[0].format == ""


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_mixed_session_keeps_its_parts_separate(store: Store) -> None:
    """A session captured before phase 4 and resumed after holds both.

    ``-1`` is reserved precisely so the two numbering schemes -- turn-space
    and record-space -- never land on one key.
    """
    session_id = await _legacy_session(store)
    await store.upsert_session_manifest(
        session_id,
        name="native.jsonl",
        metadata=json_freeze({}),
        ir_id=uuid4(),
        format="claude",
        records=1,
    )
    await store.append_session_records(
        session_id,
        [
            SessionRecordRow(
                session_id=session_id,
                part=0,
                idx=0,
                kind="UserMessage",
                payload=json_freeze({"py/object": "x"}),
                text="native turn",
            )
        ],
    )

    await _run_backfill(store)

    legacy = await store.read_session_records(session_id, part=-1, limit=500)
    native = await store.read_session_records(session_id, part=0, limit=500)
    assert len(legacy) == 3
    assert [row.text for row in native] == ["native turn"]


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
