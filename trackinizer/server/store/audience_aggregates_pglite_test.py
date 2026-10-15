"""Each aggregate reader leaves out a session its caller may not read.

Two sessions share a database: an open one, and a staff one whose owner, room, manifest
name, records and slash command carry a canary string. Every test reads through one
reader as a caller that reads only ``open`` and as one that reads ``staff`` too. The
first sees neither the canary nor the staff session's id and still sees the open
session, so a reader that returns nothing fails; the second sees both.

A science chat (:func:`seed_chat`) is open, and its transcript carries a canary of its
own: its starter reads it through every reader, and a stranger who reads every
audience reads none of it. A private session (:func:`seed_private`) carries a third:
its owner and a person who oversees read it through every reader, and a colleague of
the owner's who reads every open session reads none of it.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

import asyncio
import json
import math

from treekle import from_plain, json_loads

import pytest
import pytest_asyncio

from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.api.export_routes import export_lines
from trackinizer.server.audience import SessionReader, TranscriptPings
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.notify import iter_sse_events
from trackinizer.server.semantic_mapper_footprint import FootprintMapper
from trackinizer.server.store.core import Store
from trackinizer.server.store.read import _record_texts
from trackinizer.server.store.session_embed import sweep_session_embeddings
from trackinizer.server.store.session_feed import FeedScope
from trackinizer.server.store.session_search import (
    _ITERATIVE_SCAN,
    SessionSearchHit,
    search_session_records,
)
from trackinizer.server.values import vec_to_text
from trackinizer.types.inquiries import AgentSession
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.types.streams import Stdout
from trackinizer.wire.audience import AUDIENCES
from trackinizer.wire.filters import Filter


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Sequence

    from trackinizer.lib.postgres import PGliteEngine
    from trackinizer.server.store.export import GraphExport
    from trackinizer.wire.filters import FilterOp
    from trackinizer.wire.wire_sessions import FeedHistogramResponse


pytestmark = [pytest.mark.db_pglite, pytest.mark.asyncio(loop_scope="session")]

CANARY: Final = "canary7f3a"
CHAT_CANARY: Final = "chatcanary5e2d"
PRIVATE_CANARY: Final = "privatecanary9c1b"
ASSISTANT_EMAIL: Final = "scout@service.invalid"
STARTER_EMAIL: Final = "ann@lab.example"
OWNER_EMAIL: Final = "owen@example.com"
_HELD: Final = frozenset({"open", "private"})
# Readers on a server that is one organisation: one who reads the open sessions, and
# one who also holds staff. Neither owns a session the tests seed.
_OPEN: Final = SessionReader(audiences=_HELD, every_session=True)
_BOTH: Final = SessionReader(audiences=AUDIENCES, every_session=True)
# Reads every open session, and the chat as the one who began it, on that alone.
_STARTER: Final = SessionReader(
    audiences=_HELD,
    email=STARTER_EMAIL,
    every_session=True,
    assistant=ASSISTANT_EMAIL,
)
# A colleague of the sessions' account who holds staff, and no member of the chat.
_STRANGER: Final = SessionReader(
    audiences=AUDIENCES,
    email="sid@example.com",
    org="example.com",
    assistant=ASSISTANT_EMAIL,
)
_OWNER: Final = SessionReader(audiences=_HELD, email=OWNER_EMAIL, org="example.com")
_OVERSEER: Final = SessionReader(
    audiences=_HELD,
    email="ada@else.example",
    org="else.example",
    oversight=True,
)

_OPEN_TEXT: Final = "an open transcript"
_STAFF_TEXT: Final = f"{CANARY} the staff transcript"

_MAPPER: Final = FootprintMapper().name
_MODEL: Final = StubEmbedder(dim=1024).name
_DIM: Final = StubEmbedder(dim=1024).dim

# A UTC midnight, so an hour's bucket starts on it.
_ORIGIN: Final = datetime(2026, 9, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True, kw_only=True)
class Pair:
    """The two sessions a test reads, and when each wrote its records.

    The staff session writes first, two hours before the open one, so a reader that
    counted it would start its histogram two hours early.
    """

    open_id: UUID
    staff_id: UUID
    origin: datetime

    @property
    def staff_at(self) -> datetime:
        """When the staff session wrote its first record."""
        return self.origin + timedelta(hours=1)

    @property
    def open_at(self) -> datetime:
        """When the open session wrote its only record."""
        return self.origin + timedelta(hours=3)

    @property
    def until(self) -> datetime:
        """The end of the span a histogram counts."""
        return self.origin + timedelta(hours=6)


@pytest_asyncio.fixture(loop_scope="session")
async def store(pglite_engine: PGliteEngine) -> AsyncIterator[Store]:
    """Return a bootstrapped Store over the session's shared PGlite engine."""
    await reset_schema(pglite_engine)
    built = Store(pglite_engine, embed=StubEmbedder())
    await built.bootstrap()
    yield built


async def _session(
    store: Store,
    session_id: UUID,
    *,
    audience: str,
    owner: str,
    rooms: list[str],
    cli: str,
    account: str = "tester@example.com",
) -> None:
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title, owner, "
            "agentsession_cli, agentsession_rooms, agentsession_audience) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "$6, $2, $2, $3, $4, $5)",
            session_id,
            owner,
            cli,
            rooms,
            audience,
            account,
        )


async def _records(
    store: Store,
    session_id: UUID,
    *,
    manifest: str,
    records: Sequence[tuple[str, datetime, str]],
) -> None:
    """Store ``(kind, created, text)`` records at idx 0.. under one live manifest."""
    async with store.engine.acquire() as conn:
        for idx, (kind, created, text) in enumerate(records):
            await conn.execute(
                "INSERT INTO session_records "
                "(session_id, part, idx, kind, created, payload, text) "
                "VALUES ($1, 0, $2, $3, $4, $5::json, $6)",
                session_id,
                idx,
                kind,
                created,
                json.dumps({"content": text}),
                text,
            )
        await conn.execute(
            "INSERT INTO session_manifests "
            "(session_id, part, name, metadata, ir_id, format, records) "
            "VALUES ($1, 0, $2, '{}'::json, $3, 'claude', $4)",
            session_id,
            manifest,
            uuid4(),
            len(records),
        )


async def seed(store: Store, *, origin: datetime = _ORIGIN) -> Pair:
    """Insert the open session and the staff session that carries the canary."""
    pair = Pair(open_id=uuid4(), staff_id=uuid4(), origin=origin)
    await _session(
        store,
        pair.open_id,
        audience="open",
        owner="helper",
        rooms=["lab"],
        cli="claude",
    )
    await _records(
        store,
        pair.open_id,
        manifest="open.jsonl",
        records=[("UserMessage", pair.open_at, _OPEN_TEXT)],
    )
    await _session(
        store,
        pair.staff_id,
        audience="staff",
        owner=f"{CANARY}-lead",
        rooms=[f"{CANARY}-room"],
        cli="codex",
    )
    await _records(
        store,
        pair.staff_id,
        manifest=f"{CANARY}.jsonl",
        records=[
            ("UserMessage", pair.staff_at, _STAFF_TEXT),
            ("ToolCall", pair.staff_at + timedelta(minutes=1), f"{CANARY} ls"),
        ],
    )
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO session_slash_commands "
            "(session_id, seq, timestamp, command, args) VALUES ($1, 0, $2, 'run', $3)",
            pair.staff_id,
            pair.staff_at,
            CANARY,
        )
    return pair


async def seed_chat(store: Store, *, at: datetime) -> UUID:
    """Insert a science chat Ann began with the assistant and shared with Sam.

    The assistant's key opened it, as it opens every chat. Its two records, a line
    written at ``at`` and an answer four hours later, and its manifest name carry
    :data:`CHAT_CANARY`; its row does not, so a reader of the graph sees its envelope
    only.

    Args:
      store: The test database.
      at: When the chat wrote its first record.

    Returns:
      session_id: The chat's session.

    """
    user, key, conversation, session_id = uuid4(), uuid4(), uuid4(), uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, $2, 'Scout', 'writer', 'active')",
            user,
            ASSISTANT_EMAIL,
        )
        await conn.execute(
            "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
            "VALUES ($1, $2, 'scout', 'test-hash', 'trax_test', 'writer')",
            key,
            user,
        )
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title, owner, "
            "agentsession_cli, agentsession_rooms, agentsession_audience, "
            "agentsession_cli_session_id, agentsession_opened_by_api_key_id) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "$2, 'a chat', 'chat-ann', 'sagent', '{}', 'open', $3, $4)",
            session_id,
            STARTER_EMAIL,
            f"chat:{conversation}",
            key,
        )
        await conn.execute(
            "INSERT INTO science_chat_shares (conversation_id, email, shared_by) "
            "VALUES ($1, 'sam@gmail.com', $2)",
            conversation,
            STARTER_EMAIL,
        )
    await _records(
        store,
        session_id,
        manifest=f"{CHAT_CANARY}.jsonl",
        records=[
            ("UserMessage", at, f"{CHAT_CANARY} the chat transcript"),
            ("AssistantMessage", at + timedelta(hours=4), f"{CHAT_CANARY} an answer"),
        ],
    )
    return session_id


async def seed_private(store: Store, *, at: datetime) -> UUID:
    """Insert Owen's private session, whose one record carries :data:`PRIVATE_CANARY`.

    Args:
      store: The test database.
      at: When it wrote its record.

    Returns:
      session_id: The session.

    """
    session_id = uuid4()
    await _session(
        store,
        session_id,
        audience="private",
        owner="hidden-run",
        rooms=[],
        cli="claude",
        account=OWNER_EMAIL,
    )
    await _records(
        store,
        session_id,
        manifest=f"{PRIVATE_CANARY}.jsonl",
        records=[("UserMessage", at, f"{PRIVATE_CANARY} the hidden transcript")],
    )
    return session_id


async def _records_after(store: Store, session_id: UUID, *, texts: list[str]) -> None:
    """Append ``texts`` as records after the seeded ones and widen the manifest."""
    async with store.engine.acquire() as conn:
        first = from_plain(
            await conn.fetchval(
                "SELECT records FROM session_manifests WHERE session_id = $1",
                session_id,
            ),
            int,
        )
        for idx, text in enumerate(texts, start=first):
            await conn.execute(
                "INSERT INTO session_records "
                "(session_id, part, idx, kind, payload, text) "
                "VALUES ($1, 0, $2, 'UserMessage', '{}'::json, $3)",
                session_id,
                idx,
                text,
            )
        await conn.execute(
            "UPDATE session_manifests SET records = $2 WHERE session_id = $1",
            session_id,
            first + len(texts),
        )


async def index(store: Store) -> None:
    """Embed every seeded record through the production sweep."""
    await sweep_session_embeddings(
        store.engine,
        mapper=FootprintMapper(),
        embedder=StubEmbedder(dim=1024),
    )


def _open_only(shown: str, pair: Pair) -> None:
    """Assert ``shown`` holds the open session and nothing of the staff one."""
    assert CANARY not in shown
    assert str(pair.staff_id) not in shown
    assert str(pair.open_id) in shown


def _with_staff(shown: str, pair: Pair) -> None:
    """Assert ``shown`` holds both sessions and the canary."""
    assert CANARY in shown
    assert str(pair.staff_id) in shown
    assert str(pair.open_id) in shown


async def test_the_feed_omits_a_session_its_reader_may_not_read(store: Store) -> None:
    pair = await seed(store)

    opened = await store.read_feed(scope=FeedScope(reader=_OPEN))
    staffed = await store.read_feed(scope=FeedScope(reader=_BOTH))

    _open_only("\n".join(e.model_dump_json() for e in opened), pair)
    _with_staff("\n".join(e.model_dump_json() for e in staffed), pair)


async def test_the_facets_omit_a_session_its_reader_may_not_read(store: Store) -> None:
    pair = await seed(store)

    opened = await store.read_feed_facets(
        since=None,
        until=None,
        scope=FeedScope(reader=_OPEN),
    )
    staffed = await store.read_feed_facets(
        since=None,
        until=None,
        scope=FeedScope(reader=_BOTH),
    )

    _open_only(opened.model_dump_json(), pair)
    _with_staff(staffed.model_dump_json(), pair)
    assert {k.kind: k.count for k in opened.kinds} == {"UserMessage": 1}
    assert {k.kind: k.count for k in staffed.kinds} == {"UserMessage": 2, "ToolCall": 1}


async def test_the_histogram_does_not_start_at_a_hidden_sessions_first_record(
    store: Store,
) -> None:
    """With no ``since`` the grid starts at the earliest record the reader may count."""
    pair = await seed(store)

    assert (await _histogram(store, pair, _OPEN, since=None)).start == pair.open_at
    assert (await _histogram(store, pair, _BOTH, since=None)).start == pair.staff_at


@pytest.mark.parametrize(
    ("kinds", "staff_records"),
    [((), 2), (("UserMessage",), 1)],
    ids=["every kind", "one kind"],
)
async def test_the_bucket_counts_omit_a_session_their_reader_may_not_read(
    store: Store,
    kinds: tuple[str, ...],
    staff_records: int,
) -> None:
    """A bucket only the staff session wrote counts nothing for an open reader."""
    pair = await seed(store)

    opened = await _histogram(store, pair, _OPEN, since=pair.staff_at, kinds=kinds)
    staffed = await _histogram(store, pair, _BOTH, since=pair.staff_at, kinds=kinds)

    assert [bucket.count for bucket in opened.counts] == [0, 0, 1, 0, 0, 0]
    assert [bucket.count for bucket in staffed.counts] == [
        staff_records,
        0,
        1,
        0,
        0,
        0,
    ]


async def _histogram(
    store: Store,
    pair: Pair,
    reader: SessionReader,
    *,
    since: datetime | None,
    kinds: tuple[str, ...] = (),
) -> FeedHistogramResponse:
    """Read six hourly buckets of ``pair``'s span as ``reader``."""
    return await store.read_feed_histogram(
        since=since,
        until=pair.until,
        earliest=pair.origin,
        buckets=6,
        scope=FeedScope(kinds=kinds, reader=reader),
    )


async def _search(
    store: Store,
    reader: SessionReader,
    *,
    text: str = "",
    vector: list[float] | None = None,
    limit: int = 20,
) -> list[SessionSearchHit]:
    return await search_session_records(
        store.engine,
        query_vector=vector,
        query_text=text,
        mapper=_MAPPER,
        model=_MODEL,
        dim=_DIM,
        limit=limit,
        scope=FeedScope(reader=reader),
    )


async def test_the_full_text_arm_omits_a_session_its_reader_may_not_read(
    store: Store,
) -> None:
    pair = await seed(store)

    opened = await _search(store, _OPEN, text="transcript")
    staffed = await _search(store, _BOTH, text="transcript")

    _open_only(repr(opened), pair)
    _with_staff(repr(staffed), pair)


async def test_the_semantic_arm_omits_a_session_its_reader_may_not_read(
    store: Store,
) -> None:
    pair = await seed(store)
    await index(store)
    nearest_staff = await StubEmbedder(dim=1024).embed(_STAFF_TEXT)

    opened = await _search(store, _OPEN, vector=nearest_staff)
    staffed = await _search(store, _BOTH, vector=nearest_staff)

    _open_only(repr(opened), pair)
    _with_staff(repr(staffed), pair)
    assert staffed[0].session_id == pair.staff_id


async def test_staff_hits_that_rank_higher_do_not_use_up_an_open_readers_limit(
    store: Store,
) -> None:
    """The reader's audiences filter before ``LIMIT``, not after it."""
    pair = await seed(store)
    await _records_after(store, pair.staff_id, texts=["lock lock lock lock"] * 5)
    await _records_after(store, pair.open_id, texts=["lock"] * 3)

    opened = await _search(store, _OPEN, text="lock", limit=3)
    staffed = await _search(store, _BOTH, text="lock", limit=3)

    assert [hit.session_id for hit in opened] == [pair.open_id] * 3
    assert [hit.session_id for hit in staffed] == [pair.staff_id] * 3


async def _move_near(store: Store, session_id: UUID, vector: list[float]) -> None:
    """Put every embedded unit of ``session_id`` just beside ``vector``, each distinct."""
    async with store.engine.acquire() as conn:
        units = await conn.fetch(
            "SELECT part, idx, field, chunk FROM session_embeddings "
            "WHERE session_id = $1",
            session_id,
        )
        for number, unit in enumerate(units, start=1):
            # The sine of an integer tells the units apart as a draw would, without one.
            nearby = [
                value + 0.002 * math.sin(number * 1_009 + dimension)
                for dimension, value in enumerate(vector)
            ]
            await conn.execute(
                "UPDATE session_embeddings SET embedding = $1::halfvec "
                "WHERE session_id = $2 AND part = $3 AND idx = $4 "
                "AND field = $5 AND chunk = $6",
                vec_to_text(nearby),
                session_id,
                unit["part"],
                unit["idx"],
                unit["field"],
                unit["chunk"],
            )


async def _staff_units_nearest(store: Store) -> tuple[Pair, list[float]]:
    """Seed more staff units than the index offers, each nearer the query than any open one."""
    pair = await seed(store)
    async with store.engine.acquire() as conn:
        offered = int(from_plain(await conn.fetchval("SHOW hnsw.ef_search"), str))
    await _records_after(
        store,
        pair.open_id,
        texts=[f"open note {i}" for i in range(5)],
    )
    await _records_after(
        store,
        pair.staff_id,
        texts=[f"{CANARY} staff line {i}" for i in range(offered + 20)],
    )
    nearest_staff = await StubEmbedder(dim=1024).embed(_STAFF_TEXT)
    # PGlite keeps an HNSW index current at about 13 ms per 1024-dim unit written, and
    # each staff unit is written twice here; one build afterwards takes about 50 ms.
    async with _index_built_last(store):
        await index(store)
        await _move_near(store, pair.staff_id, nearest_staff)
    return pair, nearest_staff


@asynccontextmanager
async def _index_built_last(store: Store) -> AsyncGenerator[None]:
    """Drop the stub model's HNSW index for the block, then build it from its definition."""
    name = "idx_session_embeddings_hnsw_stub_1024"
    async with store.engine.acquire() as conn:
        definition = from_plain(
            await conn.fetchval(
                "SELECT indexdef FROM pg_indexes WHERE indexname = $1",
                name,
            ),
            str,
        )
        await conn.execute(f"DROP INDEX {name}")
    try:
        yield
    finally:
        async with store.engine.acquire() as conn:
            await conn.execute(definition)


@asynccontextmanager
async def _index_plan(store: Store) -> AsyncGenerator[None]:
    """Rule out a plan that reads every row: the index is what a real corpus gets."""
    async with store.engine.acquire() as conn:
        await conn.execute("SET enable_seqscan = off")
        await conn.execute("SET enable_sort = off")
    try:
        yield
    finally:
        async with store.engine.acquire() as conn:
            await conn.execute("RESET enable_seqscan")
            await conn.execute("RESET enable_sort")


async def test_staff_units_nearest_the_query_leave_an_open_reader_its_limit(
    store: Store,
) -> None:
    """More staff units than the index offers sit nearest; the open reader still gets 3."""
    pair, nearest_staff = await _staff_units_nearest(store)

    async with _index_plan(store):
        opened = await _search(store, _OPEN, vector=nearest_staff, limit=3)
        staffed = await _search(store, _BOTH, vector=nearest_staff, limit=3)

    assert len(staffed) == 3
    assert [hit.session_id for hit in opened] == [pair.open_id] * 3


async def test_an_open_reader_keeps_its_limit_where_pgvector_has_no_iterative_scan(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before 0.8 the arm asks the index for more candidates until the limit is filled."""
    monkeypatch.setitem(_ITERATIVE_SCAN, store.engine, False)
    pair, nearest_staff = await _staff_units_nearest(store)

    async with _index_plan(store):
        opened = await _search(store, _OPEN, vector=nearest_staff, limit=3)

    assert [hit.session_id for hit in opened] == [pair.open_id] * 3


async def test_the_installed_pgvector_is_asked_once_and_the_answer_kept(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bundled extension is 0.8 or later, so a restricted reader scans iteratively."""
    monkeypatch.delitem(_ITERATIVE_SCAN, store.engine, raising=False)

    _ = await _search(store, _OPEN, vector=await StubEmbedder(dim=1024).embed("any"))

    assert _ITERATIVE_SCAN[store.engine] is True


@pytest.mark.parametrize("iterative", [True, False], ids=["iterative", "widening"])
async def test_a_restricted_search_leaves_the_connections_index_settings_as_found(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    iterative: bool,
) -> None:
    """The setting lasts the search's transaction, not the connection's life.

    The single PGlite connection, like a pooled one, serves the next query, which a
    setting left behind would change.
    """
    monkeypatch.setitem(_ITERATIVE_SCAN, store.engine, iterative)
    _, nearest_staff = await _staff_units_nearest(store)

    # The engine outlives the test, so a setting an earlier one left would be "found".
    async with store.engine.acquire() as conn:
        await conn.execute("RESET hnsw.iterative_scan")
        await conn.execute("RESET hnsw.ef_search")
    before = await _index_settings(store)
    async with _index_plan(store):
        found = await _search(store, _OPEN, vector=nearest_staff, limit=3)

    # The search needed its setting: the default scan finds none of the open units.
    assert len(found) == 3
    assert await _index_settings(store) == before


async def _index_settings(store: Store) -> list[str]:
    """Read the connection's two index scan settings."""
    async with store.engine.acquire() as conn:
        return [
            from_plain(await conn.fetchval("SHOW hnsw.iterative_scan"), str),
            from_plain(await conn.fetchval("SHOW hnsw.ef_search"), str),
        ]


def _transcript(graph: GraphExport) -> str:
    """Return the exported lines of the three session tables, as text."""
    session_tables = {"session_manifests", "session_records", "session_slash_commands"}
    lines = b"".join(export_lines(graph)).decode().splitlines()
    return "\n".join(
        line
        for line in lines
        if json_loads(line, dict[str, object]).get("table") in session_tables
    )


def _row_counts(graph: GraphExport) -> dict[str, int]:
    """Return how many rows each session table of ``graph`` holds."""
    return {
        table: len(rows) for table, rows in graph.tables if table.startswith("session_")
    }


def _header(graph: GraphExport) -> dict[str, object]:
    """Return the first line of the export of ``graph``."""
    return json_loads(next(iter(export_lines(graph))), dict[str, object])


async def test_the_export_omits_a_hidden_sessions_transcript_but_not_its_envelope(
    store: Store,
) -> None:
    pair = await seed(store)

    opened = await store.export_graph(reader=_OPEN)
    staffed = await store.export_graph(reader=_BOTH)

    _open_only(_transcript(opened), pair)
    _with_staff(_transcript(staffed), pair)
    for graph in (opened, staffed):
        envelope = {row["id"]: row for row in dict(graph.tables)["inquiries"]}
        assert envelope[pair.staff_id]["owner"] == f"{CANARY}-lead"
    # A reader of every audience gets every row, and an open one only the open session's.
    assert _row_counts(opened) == {
        "session_manifests": 1,
        "session_records": 1,
        "session_slash_commands": 0,
    }
    assert _row_counts(staffed) == {
        "session_manifests": 2,
        "session_records": 3,
        "session_slash_commands": 1,
    }


async def test_the_export_header_names_the_audiences_it_was_read_for(
    store: Store,
) -> None:
    """A file taken as an open reader says so, instead of looking complete."""
    _ = await seed(store)

    opened = await store.export_graph(reader=_OPEN)
    staffed = await store.export_graph(reader=_BOTH)

    assert _header(opened)["audiences"] == ["open", "private"]
    assert _header(staffed)["audiences"] == ["open", "private", "staff"]


async def test_an_export_read_for_every_audience_holds_every_row(
    store: Store,
) -> None:
    """Its session tables are the ones an export with no hidden session writes."""
    _ = await seed(store)
    everything = _transcript(await store.export_graph(reader=_BOTH))

    # The same graph with its staff session open: nothing is hidden from anyone.
    async with store.engine.acquire() as conn:
        await conn.execute("UPDATE inquiries SET agentsession_audience = 'open'")
    ungated = _transcript(await store.export_graph(reader=_OPEN))

    assert everything == ungated
    assert CANARY in everything


async def test_the_record_texts_omit_a_session_their_reader_may_not_read(
    store: Store,
) -> None:
    pair = await seed(store)
    ids = [pair.open_id, pair.staff_id]
    clauses = [Filter(field="user_message", op="re", value="transcript")]

    async with store.engine.acquire() as conn:
        opened = await _record_texts(conn, ids, clauses, reader=_OPEN)
        staffed = await _record_texts(conn, ids, clauses, reader=_BOTH)

    _open_only(repr(opened), pair)
    _with_staff(repr(staffed), pair)


async def _named_until(stream: AsyncIterator[bytes], *, last: UUID) -> list[str]:
    """Return the ids a live stream names, up to and including its frame for ``last``."""
    named: list[str] = []
    async with asyncio.timeout(10):
        async for frame in stream:
            if not frame.startswith(b"data: "):
                continue
            body = json_loads(frame.removeprefix(b"data: "), dict[str, object])
            named.append(from_plain(body["id"], str))
            if named[-1] == str(last):
                break
    return named


async def test_a_record_upload_to_a_hidden_session_does_not_wake_a_reader_of_open(
    store: Store,
) -> None:
    """A live stream names an upload only to a viewer who reads the session.

    A staff session's to a viewer who reads staff, and a private one's to its owner,
    though everyone holds ``private``.
    """
    pair = await seed(store)
    hidden = await seed_private(store, at=_ORIGIN)
    opened, staffed, owned = (
        iter_sse_events(
            store.engine,
            hears=TranscriptPings(engine=store.engine, reader=reader).hears,
        )
        for reader in (_OPEN, _BOTH, _OWNER)
    )
    try:
        # A stream says ``open`` once it is subscribed, so no upload below is missed.
        for stream in (opened, staffed, owned):
            assert await anext(stream) == b": open\n\n"
        for session_id in (pair.staff_id, hidden, pair.open_id):
            written = SessionRecordRow.of(
                session_id=session_id,
                part=0,
                idx=9,
                record=Stdout(text="uploaded"),
            )
            _ = await store.append_session_records(session_id, [written])
        # The open session's upload comes last, so a hidden session's ping that was
        # relayed to a stream would have arrived before it.
        seen_by_open = await _named_until(opened, last=pair.open_id)
        seen_by_staff = await _named_until(staffed, last=pair.open_id)
        seen_by_owner = await _named_until(owned, last=pair.open_id)
    finally:
        for stream in (opened, staffed, owned):
            await stream.aclose()

    assert seen_by_open == [str(pair.open_id)]
    assert seen_by_staff == [str(pair.staff_id), str(pair.open_id)]
    assert seen_by_owner == [str(hidden), str(pair.open_id)]


def _clause(field: str, op: FilterOp, value: str = "") -> Filter:
    return Filter(field=field, op=op, value=value)


# Each clause is one a hidden session could answer: by its text, by what it lacks, or
# by whether it holds a kind of record at all. The flags say which of the two sessions
# the clause selects when nothing is hidden.
@pytest.mark.parametrize(
    ("clause", "open_matches", "staff_matches"),
    [
        pytest.param(_clause("user_message", "re", "transcript"), True, True, id="re"),
        pytest.param(_clause("user_message", "is", _STAFF_TEXT), False, True, id="is"),
        pytest.param(_clause("user_message", "ne", _OPEN_TEXT), False, True, id="ne"),
        pytest.param(_clause("user_message", "nre", "open"), False, True, id="nre"),
        pytest.param(_clause("tool_call", "notnull"), False, True, id="notnull"),
        pytest.param(_clause("tool_call", "isnull"), True, False, id="isnull"),
        pytest.param(_clause("context_compaction", "isnull"), True, True, id="none"),
        pytest.param(_clause("context_compaction", "notnull"), False, False, id="any"),
    ],
)
@pytest.mark.parametrize("lowering", [True, False], ids=["sql", "python"])
async def test_a_record_filter_cannot_find_or_probe_a_hidden_session(
    store: Store,
    clause: Filter,
    open_matches: bool,
    staff_matches: bool,
    lowering: bool,
) -> None:
    """Neither a hit nor an ``isnull`` answers for a session the reader may not read."""
    pair = await seed(store)

    opened = await _listed(store, clause, lowering=lowering, reader=_OPEN)
    staffed = await _listed(store, clause, lowering=lowering, reader=_BOTH)

    assert opened == ({pair.open_id} if open_matches else set())
    assert staffed == {
        session_id
        for session_id, matches in (
            (pair.open_id, open_matches),
            (pair.staff_id, staff_matches),
        )
        if matches
    }


async def _listed(
    store: Store,
    clause: Filter,
    *,
    lowering: bool,
    reader: SessionReader,
) -> set[UUID]:
    """List the sessions ``clause`` selects for ``reader``."""
    rows = await store.list_kind(
        "AgentSession",
        filters=[clause],
        limit=500,
        lowering=lowering,
        reader=reader,
    )
    return {row.id for row in rows if isinstance(row, AgentSession)}


async def test_a_listing_with_no_record_filter_shows_every_envelope(
    store: Store,
) -> None:
    """Only a filter on the transcript hides a session; a plain list is open."""
    pair = await seed(store)

    rows = await store.list_kind("AgentSession", limit=500, reader=_OPEN)

    assert {row.id for row in rows} == {pair.open_id, pair.staff_id}


async def test_a_read_that_names_no_reader_reads_nothing(store: Store) -> None:
    """Leaving the reader out hides every transcript rather than showing one."""
    pair = await seed(store)
    await index(store)
    nearest_staff = await StubEmbedder(dim=1024).embed(_STAFF_TEXT)

    feed = await store.read_feed()
    facets = await store.read_feed_facets(since=None, until=None, scope=FeedScope())
    histogram = await store.read_feed_histogram(
        since=pair.staff_at,
        until=pair.until,
        earliest=pair.origin,
        buckets=6,
        scope=FeedScope(),
    )
    hits = await search_session_records(
        store.engine,
        query_vector=nearest_staff,
        query_text="transcript",
        mapper=_MAPPER,
        model=_MODEL,
        dim=_DIM,
    )
    exported = await store.export_graph()
    listed = await store.list_kind(
        "AgentSession",
        filters=[Filter(field="user_message", op="re", value="transcript")],
        limit=500,
    )

    assert feed == []
    assert (facets.actors, facets.kinds) == ([], [])
    assert [bucket.count for bucket in histogram.counts] == [0] * 6
    assert hits == []
    assert set(_row_counts(exported).values()) == {0}
    assert exported.withheld_sessions == 2
    assert listed == []


@pytest.mark.parametrize(
    "view",
    [
        "feed",
        "facets",
        "full text",
        "semantic",
        "export",
        "record texts",
        "record filter",
    ],
)
async def test_each_reader_shows_a_chat_to_its_starter_and_not_to_a_stranger(
    store: Store,
    view: str,
) -> None:
    """A stranger who reads every audience reads the staff session and not the chat."""
    pair = await seed(store)
    chat = await seed_chat(store, at=pair.origin + timedelta(minutes=30))

    starter, stranger = await _shown(view, store, pair, chat=chat)

    assert str(chat) in starter
    assert (str(chat) in stranger, str(pair.staff_id) in stranger) == (False, True)
    assert CHAT_CANARY not in stranger


async def test_the_histogram_counts_a_chat_for_its_starter_and_not_for_a_stranger(
    store: Store,
) -> None:
    """The chat writes first, so a grid that counted it for the stranger starts there."""
    pair = await seed(store)
    _ = await seed_chat(store, at=pair.origin + timedelta(minutes=30))

    starter = await _histogram(store, pair, _STARTER, since=None)
    stranger = await _histogram(store, pair, _STRANGER, since=None)

    # The starter's grid holds the chat's two records and the open one, from the
    # two-hour bucket the chat's first falls in; the stranger's the open and staff ones,
    # from the staff session's first, and not the chat's answer inside its span.
    assert _start_and_count(starter) == (pair.origin, 3)
    assert _start_and_count(stranger) == (pair.staff_at, 3)


@pytest.mark.parametrize(
    "view",
    [
        "feed",
        "facets",
        "full text",
        "semantic",
        "export",
        "record texts",
        "record filter",
    ],
)
async def test_each_reader_shows_a_private_session_to_its_owner_and_oversight_only(
    store: Store,
    view: str,
) -> None:
    """A colleague who holds staff and reads Owen's open sessions reads none of it."""
    pair = await seed(store)
    hidden = await seed_private(store, at=pair.origin + timedelta(minutes=30))
    if view == "semantic":
        await index(store)

    shown = {
        name: await _view(view, store, pair, chat=hidden, reader=reader)
        for name, reader in (
            ("owner", _OWNER),
            ("overseer", _OVERSEER),
            ("colleague", _STRANGER),
        )
    }

    assert {name: str(hidden) in text for name, text in shown.items()} == {
        "owner": True,
        "overseer": True,
        "colleague": False,
    }
    assert PRIVATE_CANARY not in shown["colleague"]
    assert str(pair.open_id) in shown["colleague"]


async def test_the_histogram_counts_a_private_session_for_its_owner_alone(
    store: Store,
) -> None:
    """The private session writes first, so a grid that counted it starts there."""
    pair = await seed(store)
    _ = await seed_private(store, at=pair.origin + timedelta(minutes=30))

    owner = await _histogram(store, pair, _OWNER, since=None)
    colleague = await _histogram(store, pair, _STRANGER, since=None)

    assert _start_and_count(owner) == (pair.origin, 2)
    assert _start_and_count(colleague) == (pair.staff_at, 3)


def _start_and_count(histogram: FeedHistogramResponse) -> tuple[datetime, int]:
    """Return where a histogram's grid starts and how many records it counts."""
    return histogram.start, sum(bucket.count for bucket in histogram.counts)


async def _shown(view: str, store: Store, pair: Pair, *, chat: UUID) -> tuple[str, str]:
    """Return what the aggregate reader named ``view`` shows the starter, the stranger."""
    if view == "semantic":
        # Only this arm reads the embeddings, and a sweep is most of a case's time.
        await index(store)
    return (
        await _view(view, store, pair, chat=chat, reader=_STARTER),
        await _view(view, store, pair, chat=chat, reader=_STRANGER),
    )


async def _view(
    view: str,
    store: Store,
    pair: Pair,
    *,
    chat: UUID,
    reader: SessionReader,
) -> str:
    """Return what the aggregate reader named ``view`` shows ``reader``, as text."""
    said = Filter(field="user_message", op="re", value="transcript")
    match view:
        case "feed":
            feed = await store.read_feed(scope=FeedScope(reader=reader))
            return "\n".join(event.model_dump_json() for event in feed)
        case "facets":
            facets = await store.read_feed_facets(
                since=None,
                until=None,
                scope=FeedScope(reader=reader),
            )
            return facets.model_dump_json()
        case "full text":
            return repr(await _search(store, reader, text="transcript"))
        case "semantic":
            nearest = await StubEmbedder(dim=1024).embed(f"{CHAT_CANARY} the chat")
            return repr(await _search(store, reader, vector=nearest))
        case "export":
            return _transcript(await store.export_graph(reader=reader))
        case "record texts":
            async with store.engine.acquire() as conn:
                texts = await _record_texts(
                    conn,
                    [pair.open_id, pair.staff_id, chat],
                    [said],
                    reader=reader,
                )
            return repr(texts)
        case "record filter":
            return repr(await _listed(store, said, lowering=True, reader=reader))
        case _:
            raise ValueError(f"no aggregate reader is named {view!r}")


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
