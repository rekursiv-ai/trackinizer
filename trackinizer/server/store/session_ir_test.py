"""Storing and reading a captured session as IR records.

Every test runs against real Postgres, because the properties under test are
the database's: the derived ``idx`` making a re-append idempotent, ciphertext
landing in its own table, and the generated tsvector never seeing it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import asyncio

from treekle import immutable, json_loads

import pytest
import pytest_asyncio

from trackinizer.lib.agent.types.sessions import (
    SessionRecord,
    Thinking,
    ToolCall,
    TurnContext,
    UserMessage,
)
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.store import session_ir
from trackinizer.server.store.core import Store
from trackinizer.server.store.session_ir import SlashCommandRow
from trackinizer.types.errors import ConflictError, NotFoundError
from trackinizer.types.session_records import SessionRecordRow


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from trackinizer.lib.postgres import PostgresEngine


_CIPHERTEXT = "gAAAAABqPBiCY9-vjMraAiiOTNS8xKmaodTJ4D2l6XR2pMszVFyz"


@pytest_asyncio.fixture(loop_scope="session")
async def store(integ_engine: PostgresEngine) -> AsyncIterator[Store]:
    """Return a bootstrapped store on the shared integration database."""
    built = Store(integ_engine, embed=StubEmbedder())
    await built.bootstrap()
    yield built


async def _session(store: Store) -> UUID:
    """Return an AgentSession row the records can hang off."""
    session_id = uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title, "
            "agentsession_audience) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "'tester@example.com', 'ir test', 'open')",
            session_id,
        )
    return session_id


def _rows(
    session_id: UUID,
    records: Sequence[SessionRecord],
    *,
    part: int = 0,
) -> list[SessionRecordRow]:
    """Rows for ``records``, numbered by their stream position."""
    return [
        SessionRecordRow.of(session_id=session_id, part=part, idx=idx, record=record)
        for idx, record in enumerate(records)
    ]


# Records are readable only up to their manifest's ``records`` (the live prefix bound),
# and production never writes one without the other -- the append body rejects records
# that name no file, and the route upserts the manifest first. A fixture that stored
# records alone would exercise a state the wire cannot produce, and read back nothing.
async def _bounded(
    store: Store,
    session_id: UUID,
    records: Sequence[SessionRecord],
    *,
    part: int = 0,
    name: str = "s.jsonl",
) -> list[SessionRecordRow]:
    """Rows for ``records``, with the manifest that bounds their part."""
    _ = await store.upsert_session_manifest(
        session_id,
        name=name,
        metadata=immutable({}),
        ir_id=uuid4(),
        format="claude",
        records=len(records),
    )
    return _rows(session_id, records, part=part)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_appended_records_read_back_in_order(store: Store) -> None:
    """Records return in ``idx`` order, which is stream order."""
    session_id = await _session(store)
    records: list[SessionRecord] = [
        UserMessage(content="first"),
        ToolCall(call_id="c1", name="Read", arguments={"path": "x"}),
        UserMessage(content="third"),
    ]

    await store.append_session_records(
        session_id,
        await _bounded(store, session_id, records),
    )
    read = await store.read_session_records(session_id, part=0)

    assert [row.record() for row in read] == records


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_recent_turns_cross_parts_and_ignore_tool_bursts(store: Store) -> None:
    """The latest messages remain visible after hundreds of tool records."""
    session_id = await _session(store)
    first = [UserMessage(content="earlier direction")]
    second: list[SessionRecord] = [
        UserMessage(content="latest question"),
        *[
            ToolCall(call_id=f"c{index}", name="Read", arguments={"path": "x"})
            for index in range(350)
        ],
        UserMessage(content="trailing question"),
    ]
    await store.append_session_records(
        session_id,
        await _bounded(store, session_id, first, name="first.jsonl"),
    )
    await store.append_session_records(
        session_id,
        await _bounded(store, session_id, second, part=1, name="second.jsonl"),
    )

    turns = await store.read_recent_session_turns(session_id, limit=20)

    assert [(turn.part, turn.idx, turn.kind, turn.content) for turn in turns] == [
        (0, 0, "UserMessage", "earlier direction"),
        (1, 0, "UserMessage", "latest question"),
        (1, 351, "UserMessage", "trailing question"),
    ]
    _ = await store.upsert_session_manifest(
        session_id,
        name="second.jsonl",
        metadata=immutable({}),
        ir_id=uuid4(),
        format="claude",
        records=1,
    )
    live = await store.read_recent_session_turns(session_id, limit=20)
    assert [turn.content for turn in live] == ["earlier direction", "latest question"]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_part_longer_than_one_page_needs_paging(store: Store) -> None:
    """One store read is ONE page; a whole part is the caller's loop.

    ``MAX_LIST_LIMIT`` (1000) is the ceiling the route enforces, and a real
    transcript exceeds it. ``Client.read_session_records`` pages until an
    empty page for exactly this reason: a replay handed a truncated part
    writes a file the provider rejects, or worse, accepts as a shorter
    conversation than the one that happened.

    Pinned so the bound stays visible: a future default that silently
    returned everything would make the client's loop look redundant, and
    deleting it would reintroduce the truncation.
    """
    session_id = await _session(store)
    total = 1200
    records = [UserMessage(content=f"r{i}") for i in range(total)]
    await store.append_session_records(
        session_id,
        await _bounded(store, session_id, records),
    )

    page = await store.read_session_records(session_id, part=0, limit=1000)
    rest = await store.read_session_records(
        session_id,
        part=0,
        after_idx=page[-1].idx,
        limit=1000,
    )

    assert len(page) == 1000
    assert [row.idx for row in rest] == list(range(1000, total))


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_shrunk_part_reads_only_its_live_prefix(store: Store) -> None:
    """A file that shrank leaves its tail INERT, not readable.

    ``session_manifests.records`` is the live prefix bound, and a compaction
    is what produces one: claude rewrites the transcript shorter, so the
    re-derived batch overwrites positions 0..n while the old rows at n.. stay
    on disk. Nothing deletes them -- ``restart`` is an overwrite, not a
    truncate -- so the bound is the only thing separating the current file
    from the one it replaced.

    A reader that ignores it returns a transcript that never existed: the new
    prefix followed by the old suffix. ``resume`` rebuilds the native file
    from exactly these rows, so the stale tail would be handed back to the
    CLI as conversation.
    """
    session_id = await _session(store)
    part = await store.upsert_session_manifest(
        session_id,
        name="s.jsonl",
        metadata=immutable({}),
        ir_id=uuid4(),
        format="claude",
        records=5,
    )
    await store.append_session_records(
        session_id,
        _rows(session_id, [UserMessage(content=f"before-{i}") for i in range(5)]),
    )

    # The compaction: rewritten shorter, so the manifest bounds it at 2.
    _ = await store.upsert_session_manifest(
        session_id,
        name="s.jsonl",
        metadata=immutable({}),
        ir_id=uuid4(),
        format="claude",
        records=2,
    )
    await store.append_session_records(
        session_id,
        _rows(session_id, [UserMessage(content=f"after-{i}") for i in range(2)]),
        restart=True,
    )

    read = await store.read_session_records(session_id, part=part)

    assert [row.text for row in read] == ["after-0", "after-1"]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_part_with_no_manifest_reads_nothing(store: Store) -> None:
    """No manifest is a bound of zero, not an unbounded read.

    The manifest is upserted BEFORE its records precisely so a part always
    has one; rows without it are a torn write, and serving them would expose
    exactly the half-written state the ordering exists to prevent.
    """
    session_id = await _session(store)
    await store.append_session_records(
        session_id,
        _rows(session_id, [UserMessage(content="orphan")]),
    )

    assert not await store.read_session_records(session_id, part=0)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_ended_session_refuses_new_records(store: Store) -> None:
    """A closed session takes no more capture (#465's zombie guard).

    ``ended`` is what ``resolve_live_sessions`` reads to decide a session is
    gone. A record accepted afterwards lands in a transcript nothing is
    watching and, worse, silently reopens the question of whether the session
    is live: the row says ended, the records say otherwise. The retired
    per-turn append enforced this under a row lock; the record append must
    too, or the guard was lost in the migration rather than retired.

    Under a row lock rather than a plain read, so a concurrent ``end`` cannot
    slip between the check and the insert.
    """
    session_id = await _session(store)
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET agentsession_ended = now(), status = 'complete' "
            "WHERE id = $1",
            session_id,
        )

    with pytest.raises(ConflictError, match="has ended"):
        _ = await store.append_session_records(
            session_id,
            _rows(session_id, [UserMessage(content="zombie")]),
        )

    assert not await store.read_session_records(session_id, part=0)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_slash_command_alone_also_respects_the_end(store: Store) -> None:
    """The guard covers the whole append, not only its records.

    A command rides the same request as the turns around it, so admitting one
    into an ended session stores half a transcript the records half refused.
    """
    session_id = await _session(store)
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET agentsession_ended = now(), status = 'complete' "
            "WHERE id = $1",
            session_id,
        )

    with pytest.raises(ConflictError, match="has ended"):
        _ = await store.append_session_records(
            session_id,
            [],
            slash_commands=[
                SlashCommandRow(timestamp=datetime.now(UTC), command="exit"),
            ],
        )

    assert not await store.read_session_slash_commands(session_id)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_missing_session_is_not_found(store: Store) -> None:
    """A purged session is a 404, never a leaked constraint name."""
    with pytest.raises(NotFoundError):
        _ = await store.append_session_records(
            uuid4(),
            _rows(uuid4(), [UserMessage(content="orphan")]),
        )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_re_appending_the_same_records_writes_nothing(store: Store) -> None:
    """A re-fed file is idempotent, because ``idx`` is derived.

    The runner re-reads a claude session file whenever the CLI compacts it, so
    the same records arrive twice. A counter-assigned key would append a second
    copy of every retained record; a derived one lands each back on the key it
    already holds.
    """
    session_id = await _session(store)
    rows = await _bounded(
        store,
        session_id,
        [UserMessage(content="a"), UserMessage(content="b")],
    )

    first = await store.append_session_records(session_id, rows)
    second = await store.append_session_records(session_id, rows)

    assert first == (2, 0, 0)
    assert second == (0, 2, 0)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_restart_overwrites_because_disk_is_truth(store: Store) -> None:
    """A restarted part UPDATES: a compaction rewrites what a record holds.

    Claude keeps the turns it did not summarize away, so a record re-derived
    at a given ``idx`` may legitimately differ from the stored one. On a
    restart the file is authoritative.
    """
    session_id = await _session(store)
    await store.append_session_records(
        session_id,
        await _bounded(store, session_id, [UserMessage(content="before")]),
    )

    await store.append_session_records(
        session_id,
        await _bounded(store, session_id, [UserMessage(content="after")]),
        restart=True,
    )
    read = await store.read_session_records(session_id, part=0)

    assert [row.record() for row in read] == [UserMessage(content="after")]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_parts_stay_separate(store: Store) -> None:
    """Two files are two parts; ``idx`` restarts within each."""
    session_id = await _session(store)
    # Two FILES, so two manifests: the server resolves a part from the
    # basename, and one name can only ever resolve to one part.
    await store.append_session_records(
        session_id,
        await _bounded(
            store,
            session_id,
            [UserMessage(content="p0")],
            part=0,
            name="a.jsonl",
        ),
    )
    await store.append_session_records(
        session_id,
        await _bounded(
            store,
            session_id,
            [UserMessage(content="p1")],
            part=1,
            name="b.jsonl",
        ),
    )

    first = await store.read_session_records(session_id, part=0)
    second = await store.read_session_records(session_id, part=1)

    assert [row.record() for row in first] == [UserMessage(content="p0")]
    assert [row.record() for row in second] == [UserMessage(content="p1")]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_ciphertext_lands_in_its_own_table(store: Store) -> None:
    """``Thinking.encrypted`` is stored apart and spliced back on read."""
    session_id = await _session(store)
    record = Thinking(content="visible", encrypted=_CIPHERTEXT)

    await store.append_session_records(
        session_id,
        await _bounded(store, session_id, [record]),
    )
    read = await store.read_session_records(session_id, part=0)

    assert read[0].record() == Thinking(content="visible", encrypted="")
    assert read[0].ciphertext == _CIPHERTEXT


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_dropping_ciphertext_leaves_the_record_searchable(store: Store) -> None:
    """The retention lever: DELETE the bytes, keep the record.

    This is why ciphertext is a separate table at all. After the delete the
    plaintext still matches a search and the record still reads -- only the
    encrypted half is gone.
    """
    session_id = await _session(store)
    await store.append_session_records(
        session_id,
        await _bounded(
            store,
            session_id,
            [Thinking(content="findable", encrypted=_CIPHERTEXT)],
        ),
    )

    async with store.engine.acquire() as conn:
        await conn.execute(
            "DELETE FROM session_ciphertext WHERE session_id = $1",
            session_id,
        )
    read = await store.read_session_records(session_id, part=0)

    assert read[0].ciphertext is None
    assert "findable" in read[0].text


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_search_never_indexes_ciphertext(store: Store) -> None:
    """The generated tsvector holds plaintext and not the encrypted bytes.

    Asserted against the COLUMN, not the projection: the projection is unit
    tested, but this is the property that would leak if the column were ever
    generated from a different expression.
    """
    session_id = await _session(store)
    await store.append_session_records(
        session_id,
        await _bounded(
            store,
            session_id,
            [Thinking(content="plaintext", encrypted=_CIPHERTEXT)],
        ),
    )

    async with store.engine.acquire() as conn:
        vector = await conn.fetchval(
            "SELECT search::text FROM session_records WHERE session_id = $1",
            session_id,
        )

    assert isinstance(vector, str)
    assert "plaintext" in vector
    assert _CIPHERTEXT.lower() not in vector.lower()


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_manifest_upserts_per_batch(store: Store) -> None:
    """The manifest is re-written as the file grows, not once at the end.

    ``metadata`` is only correct for the prefix consumed -- claude's ascii
    majority is not final until EOF -- so every batch re-upserts it.
    """
    session_id = await _session(store)
    await store.upsert_session_manifest(
        session_id,
        name="a.jsonl",
        metadata=immutable({}),
        ir_id=uuid4(),
        format="claude",
        records=1,
    )

    part = await store.upsert_session_manifest(
        session_id,
        name="a.jsonl",
        metadata=immutable({}),
        ir_id=uuid4(),
        format="claude",
        records=9,
    )
    manifests = await store.read_session_manifests(session_id)

    assert part == 0
    assert [(m.name, m.records) for m in manifests] == [("a.jsonl", 9)]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_second_file_gets_the_next_part(store: Store) -> None:
    """``part`` is assigned server-side from the file's basename.

    The client never sends one: it names the file, and the server resolves it,
    so a restarted or resumed client cannot invent a conflicting number.
    """
    session_id = await _session(store)
    first = await store.upsert_session_manifest(
        session_id,
        name="a.jsonl",
        metadata=immutable({}),
        ir_id=uuid4(),
        format="claude",
        records=1,
    )

    second = await store.upsert_session_manifest(
        session_id,
        name="b.jsonl",
        metadata=immutable({}),
        ir_id=uuid4(),
        format="claude",
        records=1,
    )

    assert (first, second) == (0, 1)


@pytest.mark.db_postgres
@pytest.mark.asyncio(loop_scope="session")
async def test_concurrent_new_files_keep_distinct_parts(store: Store) -> None:
    session_id = await _session(store)
    tasks: list[asyncio.Task[int]] = []
    async with store.engine.acquire() as conn:
        await conn.execute("BEGIN")
        try:
            # Before the fix both allocators read zero, then their INSERTs wait on
            # this row's FK lock. Allocation itself must wait on the session instead.
            _ = await conn.fetchval(
                "SELECT id FROM inquiries WHERE id = $1 FOR UPDATE",
                session_id,
            )
            tasks = [
                asyncio.create_task(
                    _upsert(store, session_id, name=name, key=None, records=records),
                )
                for name, records in (("first.jsonl", 2), ("second.jsonl", 3))
            ]
            async with asyncio.timeout(5):
                while True:
                    await conn.execute("SELECT pg_stat_clear_snapshot()")
                    blocked = await conn.fetchval(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() "
                        "AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()",
                    )
                    assert isinstance(blocked, int)
                    if blocked == 2:
                        break
                    await asyncio.sleep(0.001)
        finally:
            await conn.execute("COMMIT")
            parts = await asyncio.gather(*tasks)
    assert len(set(parts)) == 2
    manifests = await store.read_session_manifests(session_id)
    assert {m.name: m.records for m in manifests} == {
        "first.jsonl": 2,
        "second.jsonl": 3,
    }


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_purging_the_session_cascades_every_table(store: Store) -> None:
    """All four tables hang off ``inquiries`` and go with it."""
    session_id = await _session(store)
    await store.append_session_records(
        session_id,
        _rows(session_id, [Thinking(content="x", encrypted=_CIPHERTEXT)]),
    )
    await store.upsert_session_manifest(
        session_id,
        name="a.jsonl",
        metadata=immutable({}),
        ir_id=uuid4(),
        format="claude",
        records=1,
    )

    async with store.engine.acquire() as conn:
        await conn.execute("DELETE FROM inquiries WHERE id = $1", session_id)
        counts = await conn.fetchrow(
            "SELECT (SELECT count(*) FROM session_records WHERE session_id = $1) "
            "AS records, "
            "(SELECT count(*) FROM session_manifests WHERE session_id = $1) "
            "AS manifests, "
            "(SELECT count(*) FROM session_ciphertext WHERE session_id = $1) "
            "AS ciphertext",
            session_id,
        )

    assert counts is not None
    assert [counts["records"], counts["manifests"], counts["ciphertext"]] == [0, 0, 0]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_context_naming_itself_is_accepted(store: Store) -> None:
    """A claude ``TurnContext`` sits at its own ``idx`` and names it."""
    session_id = await _session(store)
    record = TurnContext(context_id=0, model="opus")

    await store.append_session_records(
        session_id,
        await _bounded(store, session_id, [record]),
    )
    read = await store.read_session_records(session_id, part=0)

    assert read[0].context_id == 0


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_killed_run_leaves_a_valid_prefix(store: Store) -> None:
    """A run that died mid-file stores what it got, readable as a prefix.

    Capture is incremental, so the alternative to a partial transcript is no
    transcript: nothing about the schema requires a session to be complete,
    and the manifest's ``records`` bounds readers at the prefix that exists.
    """
    session_id = await _session(store)
    # The manifest FIRST, as the route writes it: it assigns the part the
    # records are keyed by and bounds every reader.
    part = await store.upsert_session_manifest(
        session_id,
        name="killed.jsonl",
        metadata=immutable({}),
        ir_id=uuid4(),
        format="claude",
        records=2,
    )
    await store.append_session_records(
        session_id,
        _rows(
            session_id,
            [UserMessage(content="a"), UserMessage(content="b")],
            part=part,
        ),
    )

    read = await store.read_session_records(session_id, part=part)
    manifests = await store.read_session_manifests(session_id)

    assert [row.idx for row in read] == [0, 1]
    assert manifests[0].records == 2


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_resumed_run_resolves_to_the_existing_part(store: Store) -> None:
    """The same basename resolves to the same part, across runs.

    This is what lets a resumed run APPEND to the file it materialized rather
    than forking a second part: the client names the file and the server
    resolves the number, so a restarted client cannot invent a conflicting one.
    """
    session_id = await _session(store)
    ir_id = uuid4()
    first = await store.upsert_session_manifest(
        session_id,
        name="s.jsonl",
        metadata=immutable({}),
        ir_id=ir_id,
        format="claude",
        records=1,
    )
    # A second run of the same session: fresh client, same file.
    second = await store.upsert_session_manifest(
        session_id,
        name="s.jsonl",
        metadata=immutable({}),
        ir_id=ir_id,
        format="claude",
        records=4,
    )

    assert first == second
    manifests = await store.read_session_manifests(session_id)
    assert len(manifests) == 1, "a resumed run forked a second part"
    assert manifests[0].records == 4, "the manifest did not grow with the file"


async def _upsert(
    store: Store,
    session_id: UUID,
    *,
    name: str,
    key: UUID | None,
    records: int = 1,
    exclusive: bool = False,
) -> int:
    """Upsert one part's manifest as ``key`` writes it."""
    return await store.upsert_session_manifest(
        session_id,
        name=name,
        metadata=immutable({}),
        ir_id=uuid4(),
        format="claude",
        records=records,
        api_key_id=key,
        exclusive=exclusive,
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_part_keeps_the_key_whose_first_write_opened_it(store: Store) -> None:
    """A later batch, by any key, grows the part and leaves its opener as it was."""
    session_id = await _session(store)
    opener, later = uuid4(), uuid4()
    _ = await _upsert(store, session_id, name="a.jsonl", key=opener)
    _ = await _upsert(store, session_id, name="a.jsonl", key=later, records=3)
    _ = await _upsert(store, session_id, name="b.jsonl", key=None)

    first = await store.read_session_manifest(session_id, name="a.jsonl")
    listed = await store.read_session_manifests(session_id)

    assert first is not None
    assert (first.part, first.records, first.opened_by_api_key_id) == (0, 3, opener)
    assert [(m.name, m.opened_by_api_key_id) for m in listed] == [
        ("a.jsonl", opener),
        ("b.jsonl", None),
    ]
    assert await store.read_session_manifest(session_id, name="c.jsonl") is None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_exclusive_write_refuses_another_keys_part(store: Store) -> None:
    """A science chat's part is its opener's: another key's batch changes nothing.

    A part with no recorded opener is left to the route's rule, so it is written.
    """
    session_id = await _session(store)
    opener, stranger = uuid4(), uuid4()
    _ = await _upsert(store, session_id, name="a.jsonl", key=opener)
    _ = await _upsert(store, session_id, name="old.jsonl", key=None)

    with pytest.raises(ConflictError, match="opened by another key"):
        _ = await _upsert(
            store,
            session_id,
            name="a.jsonl",
            key=stranger,
            records=9,
            exclusive=True,
        )
    own = await _upsert(
        store,
        session_id,
        name="a.jsonl",
        key=opener,
        records=2,
        exclusive=True,
    )
    old = await _upsert(
        store,
        session_id,
        name="old.jsonl",
        key=stranger,
        records=5,
        exclusive=True,
    )

    assert (own, old) == (0, 1)
    assert [
        (m.records, m.opened_by_api_key_id)
        for m in await store.read_session_manifests(session_id)
    ] == [(2, opener), (5, None)]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_slash_commands_commit_with_their_records(store: Store) -> None:
    """One transaction: a command sits BETWEEN the turns around it.

    Storing one half would record a transcript that never happened.
    """
    session_id = await _session(store)
    at = datetime(2026, 6, 1, tzinfo=UTC)

    written, _skipped, slash = await store.append_session_records(
        session_id,
        await _bounded(store, session_id, [UserMessage(content="before")]),
        slash_commands=[SlashCommandRow(timestamp=at, command="model", args="opus")],
    )
    stored = await store.read_session_slash_commands(session_id)

    assert (written, slash) == (1, 1)
    assert [(c.command, c.args) for c in stored] == [("model", "opus")]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_resumed_runs_slash_seq_continues(store: Store) -> None:
    """``seq`` is server-assigned, so a resumed run does not collide at 0.

    A sink counter restarts with the process; numbering from the session's own
    ``max(seq)+1`` is what keeps the second run's commands from landing on the
    first run's keys and being dropped by the primary key.
    """
    session_id = await _session(store)
    at = datetime(2026, 6, 1, tzinfo=UTC)
    await store.append_session_records(
        session_id,
        [],
        slash_commands=[SlashCommandRow(timestamp=at, command="first")],
    )

    # A second run: its own sink would mint seq 0 again.
    _w, _s, slash = await store.append_session_records(
        session_id,
        [],
        slash_commands=[SlashCommandRow(timestamp=at, command="second")],
    )

    assert slash == 1, "the resumed run's command was dropped as a duplicate"
    stored = await store.read_session_slash_commands(session_id)
    assert [c.command for c in stored] == ["first", "second"]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_slash_command_consumes_no_record_position(store: Store) -> None:
    """It is absent from the log, so it holds no ``idx``.

    Consuming one would renumber every record after it, and a replay -- which
    re-derives positions from the file, where the command does not appear --
    would then disagree with what was stored.
    """
    session_id = await _session(store)
    at = datetime(2026, 6, 1, tzinfo=UTC)

    await store.append_session_records(
        session_id,
        await _bounded(
            store,
            session_id,
            [UserMessage(content="a"), UserMessage(content="b")],
        ),
        slash_commands=[SlashCommandRow(timestamp=at, command="exit")],
    )
    read = await store.read_session_records(session_id, part=0)

    assert [row.idx for row in read] == [0, 1]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_captured_nul_is_stored_as_a_replacement_character(
    store: Store,
) -> None:
    """A NUL in a record or a slash command must not fail the append.

    Postgres text cannot hold NUL. A refused batch is re-sent by the capturing
    client forever, so the append stores U+FFFD in its place.
    """
    session_id = await _session(store)
    at = datetime(2026, 6, 1, tzinfo=UTC)
    records: list[SessionRecord] = [
        UserMessage(content="a\x00b"),
        ToolCall(call_id="c1", name="Read", arguments={"pa\x00th": "x\x00"}),
    ]

    await store.append_session_records(
        session_id,
        await _bounded(store, session_id, records),
        slash_commands=[SlashCommandRow(timestamp=at, command="model", args="o\x00")],
    )
    read = await store.read_session_records(session_id, part=0)
    turns = await store.read_recent_session_turns(session_id, limit=20)
    stored = await store.read_session_slash_commands(session_id)

    assert [row.record() for row in read] == [
        UserMessage(content="a\ufffdb"),
        ToolCall(call_id="c1", name="Read", arguments={"pa\ufffdth": "x\ufffd"}),
    ]
    assert read[0].text == "a\ufffdb"
    assert [turn.content for turn in turns] == ["a\ufffdb"]
    assert [command.args for command in stored] == ["o\ufffd"]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_copy_takes_each_parts_prefix_into_the_next_part_of_a_new_session(
    store: Store,
) -> None:
    """Positions, stamps, ciphertext and bodies come along; the rest of a part stays."""
    source, target, key = await _session(store), await _session(store), uuid4()
    at = datetime(2026, 10, 20, 9, tzinfo=UTC).isoformat()
    parts: list[list[SessionRecord]] = [
        [
            UserMessage(content="a0", timestamp=at),
            Thinking(content="why", encrypted=_CIPHERTEXT, timestamp=at),
            UserMessage(content="a2", timestamp=at),
        ],
        [UserMessage(content="b0")],
        [UserMessage(content="c0"), UserMessage(content="c1")],
    ]
    for part, (name, records) in enumerate(zip("abc", parts, strict=True)):
        _ = await store.upsert_session_manifest(
            source,
            name=f"{name}.jsonl",
            metadata=immutable({"newline_terminated": part == 2}),
            ir_id=uuid4(),
            format="sagent" if part == 2 else "claude",
            records=len(records),
        )
        _ = await store.append_session_records(
            source,
            _rows(source, records, part=part),
        )
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO session_bodies (session_id, part, idx, payload_zst, text_zst) "
            "SELECT $1, 0, idx, '\\x00', '\\x01' FROM unnest(ARRAY[0, 2]) AS idx",
            source,
        )
        await store.copy_session_parts(
            conn,
            source=source,
            target=target,
            prefixes=[
                session_ir.PartPrefix(part=0, records=2),
                session_ir.PartPrefix(part=2, records=1),
            ],
            api_key_id=key,
        )
        sealed = await conn.fetch(
            "SELECT part, idx FROM session_ciphertext WHERE session_id = $1",
            target,
        )
        bodies = await conn.fetch(
            "SELECT part, idx FROM session_bodies WHERE session_id = $1",
            target,
        )
        created = await conn.fetch(
            "SELECT created FROM session_records WHERE part = 0 AND idx < 2 "
            "AND session_id = ANY(ARRAY[$1, $2]::uuid[]) ORDER BY idx, session_id = $2",
            source,
            target,
        )

    copied = await store.read_session_manifests(target)
    sources = await store.read_session_manifests(source)
    assert [
        (m.part, m.name, m.format, m.records, m.opened_by_api_key_id, m.ir_id)
        for m in copied
    ] == [
        (0, "a.jsonl", "claude", 2, key, sources[0].ir_id),
        (1, "c.jsonl", "sagent", 1, key, sources[2].ir_id),
    ]
    assert copied[1].metadata == {"newline_terminated": True}
    assert [
        row.record()
        for part in (0, 1)
        for row in await store.read_session_records(
            target,
            part=part,
            plaintext_only=True,
        )
    ] == [parts[0][0], Thinking(content="why", encrypted="", timestamp=at), parts[2][0]]
    assert [(row["part"], row["idx"]) for row in sealed] == [(0, 1)]
    assert [(row["part"], row["idx"]) for row in bodies] == [(0, 0)]
    # Each copy keeps the moment its original was recorded at.
    moments = [row["created"] for row in created]
    assert moments[::2] == moments[1::2]


def test_a_payload_nul_is_replaced_in_keys_and_nested_strings() -> None:
    r"""Only NUL changes: an escaped ``\u0000`` written as text stays as it was."""
    payload = immutable({"a\x00": ["x\x00", {"k": "\x00"}], "n": 1, "lit": "\\u0000"})

    encoded = session_ir._encoded_payload(payload)

    assert json_loads(encoded) == {
        "a\ufffd": ["x\ufffd", {"k": "\ufffd"}],
        "n": 1,
        "lit": "\\u0000",
    }


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
