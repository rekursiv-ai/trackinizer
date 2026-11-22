"""The sweep against real Postgres: what it embeds, rewrites and deletes, and when.

Every test runs the sweep over real ``session_records`` rows: the change stamps,
the snapshot visibility and the md5 comparison are the database's, and the
``halfvec`` rows exist only against a live pgvector. The model is
:class:`~trackinizer.addons.embedding.fakes.HashEmbedder`, which records the
texts of every call.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, Final, cast, override
from uuid import UUID, uuid4

import asyncio
import logging

import pytest
import pytest_asyncio

from trackinizer.addons.addon import ServerContext
from trackinizer.addons.embedding import sweep
from trackinizer.addons.embedding.fakes import HashEmbedder, vector_store
from trackinizer.addons.embedding.search import Nearest
from trackinizer.addons.embedding.sweep import Sweep, SweepState, sweep_once
from trackinizer.addons.embedding.units import Settings, settings_key
from trackinizer.conftest import FakeEngine, make_conn
from trackinizer.lib.postgres import Conn, DatabaseEngine
from trackinizer.server.values import vec_to_text, vetted_sql


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from asyncpg import Record

    from trackinizer.lib.postgres import PostgresEngine
    from trackinizer.server.store.core import Store


@pytest_asyncio.fixture(loop_scope="session")
async def store(integ_engine: PostgresEngine) -> Store:
    """Return a store whose database holds the vector table, every table empty."""
    return await vector_store(integ_engine)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_chunk_size_change_marks_every_record_pending(store: Store) -> None:
    session_id = await _session(store)
    await _record(store, session_id, idx=0, text="a" * 30)
    await _record(store, session_id, idx=1, text="short")
    model = HashEmbedder(dim=4)
    await _sweep(store, model, _settings(model, chunk_chars=1_500))
    model.calls.clear()

    rechunked = _settings(model, chunk_chars=20)
    await _sweep(store, model, rechunked)

    assert sorted(text for call in model.calls for text in call) == sorted(
        ["a" * 20, "a" * 17, "a" * 4, "short"],
    )
    assert {key for *_, key, _md5 in await _rows(store)} == {rechunked.key}


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_unchanged_record_is_not_reembedded_and_an_edited_one_is(
    store: Store,
) -> None:
    session_id = await _session(store)
    await _record(store, session_id, idx=0, text="kept as it was")
    await _record(store, session_id, idx=1, text="first draft")
    model = HashEmbedder(dim=4)
    settings = _settings(model)
    state = await _sweep(store, model, settings)
    model.calls.clear()

    await _edit(store, session_id, idx=1, text="second draft")
    await _sweep(store, model, settings, state=state)

    assert model.calls == [["second draft"]]
    assert [
        (idx, md5) for _owner, _part, idx, _chunk, _key, md5 in await _rows(store)
    ] == [
        (0, await _md5(store, "kept as it was")),
        (1, await _md5(store, "second draft")),
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_emptied_record_loses_its_vectors(store: Store) -> None:
    session_id = await _session(store)
    await _record(store, session_id, idx=0, text="said and then erased")
    await _record(store, session_id, idx=1, text="still said")
    model = HashEmbedder(dim=4)
    settings = _settings(model)
    state = await _sweep(store, model, settings)

    await _edit(store, session_id, idx=0, text="")
    await _sweep(store, model, settings, state=state)

    assert [idx for _owner, _part, idx, *_ in await _rows(store)] == [1]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_search_reads_only_rows_of_the_configured_key(store: Store) -> None:
    """Under new settings search finds only what the fill has rewritten so far."""
    session_id = await _session(store)
    await _record(store, session_id, idx=0, text="older words")
    await _record(store, session_id, idx=1, text="newer words")
    model = HashEmbedder(dim=4)
    await _sweep(store, model, _settings(model))
    rechunked = _settings(model, chunk_chars=999)
    loaded = asyncio.Event()
    loaded.set()
    search = Nearest(model=model, settings=rechunked, loaded=loaded)

    before = await _found(search, store, "older words")
    _ = await sweep_once(
        store.engine,
        model=model,
        settings=rechunked,
        state=SweepState(),
        slice_units=1,
    )
    during = await _found(search, store, "older words")

    assert before == []
    # The backlog runs newest first, so the newer record is the one rewritten.
    assert during == [(session_id, 1)]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_rows_of_another_key_are_deleted_after_a_complete_walk(
    store: Store,
) -> None:
    """A record a compaction left past the live prefix is never walked again."""
    session_id = await _session(store)
    await _record(store, session_id, idx=0, text="one")
    await _record(store, session_id, idx=1, text="two")
    model = HashEmbedder(dim=4)
    old = _settings(model)
    await _sweep(store, model, old)
    await _index(store, old)
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE session_manifests SET records = 1 WHERE session_id = $1",
            session_id,
        )
    new = _settings(model, chunk_chars=999)

    first = await sweep_once(
        store.engine,
        model=model,
        settings=new,
        state=SweepState(),
        slice_units=1,
    )
    kept = await _rows(store)
    await _sweep(store, model, new, state=first)

    assert [(idx, key) for _owner, _part, idx, _chunk, key, _md5 in kept] == [
        (0, new.key),
        (1, old.key),
    ]
    assert [
        (idx, key) for _owner, _part, idx, _chunk, key, _md5 in await _rows(store)
    ] == [
        (0, new.key),
    ]
    assert await _indexes(store) == []


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_no_connection_is_held_during_a_model_call(store: Store) -> None:
    session_id = await _session(store)
    await _record(store, session_id, idx=0, text="embedded with no connection held")
    engine = _CountingEngine(engine=store.engine)
    model = _Watching(engine=engine)

    await _sweep_engine(cast("DatabaseEngine", engine), model, _settings(model))

    assert model.held == [0]
    assert engine.acquired > 0


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_page_is_written_a_few_records_a_transaction(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each transaction holds whole records, two units at most here; a long one alone."""
    session_id = await _session(store)
    for idx, text in enumerate(["a", "b", "c" * 40, "d"]):
        await _record(store, session_id, idx=idx, text=text)
    monkeypatch.setattr(sweep, "_WRITE_UNITS", 2)
    engine = _CountingEngine(engine=store.engine)
    model = HashEmbedder(dim=4)

    state = await sweep_once(
        cast("DatabaseEngine", engine),
        model=model,
        settings=_settings(model, chunk_chars=20),
        state=SweepState(),
        slice_units=10,
    )

    # One snapshot, one page, then three writes: [d, c] would hold four units.
    assert (state.written, engine.acquired) == (7, 5)
    assert len(await _rows(store)) == 7


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_new_records_are_embedded_before_the_backlog(store: Store) -> None:
    session_id = await _session(store)
    for idx in range(3):
        await _record(store, session_id, idx=idx, text=f"backlog {idx}")
    model = HashEmbedder(dim=4)
    settings = _settings(model)
    first = await sweep_once(
        store.engine,
        model=model,
        settings=settings,
        state=SweepState(),
        slice_units=1,
    )
    await _record(store, session_id, idx=3, text="just said")

    second = await sweep_once(
        store.engine,
        model=model,
        settings=settings,
        state=first,
        slice_units=1,
    )

    assert model.calls == [["backlog 2"], ["just said"], ["backlog 1"]]
    assert (first.worked, second.worked, second.walked) == (True, True, False)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_only_configured_kinds_inside_the_live_prefix_get_vectors(
    store: Store,
) -> None:
    session_id = await _session(store)
    await _record(store, session_id, idx=0, text="a message")
    await _record(store, session_id, idx=1, text="ls -la", kind="ToolCall")
    await _record(store, session_id, idx=2, text="beyond the prefix")
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE session_manifests SET records = 2 WHERE session_id = $1",
            session_id,
        )
    model = HashEmbedder(dim=4)

    await _sweep(store, model, _settings(model))

    assert model.calls == [["a message"]]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_long_record_gets_a_row_per_chunk_and_calls_hold_a_slice(
    store: Store,
) -> None:
    session_id = await _session(store)
    await _record(store, session_id, idx=0, text="b" * 50)
    model = HashEmbedder(dim=4)

    _ = await sweep_once(
        store.engine,
        model=model,
        settings=_settings(model, chunk_chars=20),
        state=SweepState(),
        slice_units=2,
    )

    assert model.calls == [["b" * 20, "b" * 20], ["b" * 20, "b" * 11]]
    assert [chunk for _owner, _part, _idx, chunk, *_ in await _rows(store)] == [
        0,
        1,
        2,
        3,
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_idle_round_did_no_work(store: Store) -> None:
    model = HashEmbedder(dim=4)

    state = await sweep_once(
        store.engine,
        model=model,
        settings=_settings(model),
        state=SweepState(),
        slice_units=4,
    )

    assert (state.worked, state.walked, state.since is not None) == (False, True, True)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_service_loads_the_model_makes_the_index_then_sweeps(
    store: Store,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It sweeps until a round finds nothing, then waits its interval."""
    session_id = await _session(store)
    await _record(store, session_id, idx=0, text="served")
    model = HashEmbedder(dim=4)
    settings = _settings(model)
    service = Sweep(
        model=model,
        settings=settings,
        slice_units=4,
        interval_sec=0.5,
        loaded=asyncio.Event(),
    )
    waits: list[float] = []
    monkeypatch.setattr(sweep, "asyncio", SimpleNamespace(sleep=partial(_idle, waits)))

    with caplog.at_level(logging.INFO), pytest.raises(_IdleError):
        await service.run(_context(store))

    assert (service.name, model.calls, waits) == ("sweep", [["served"]], [0.5])
    assert service.loaded.is_set()
    assert len(await _rows(store)) == 1
    assert await _indexes(store) == [f"vectors_hnsw_{settings.key}"]
    assert f"embedding settings {settings.key}" in caplog.text


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_edge_removed_by_cascade_loses_its_vector(store: Store) -> None:
    """Deleting the inquiry an edge points to takes the note's vector from its owner."""
    owner = await _inquiry(store, "Issue", description="the owner")
    target = await _inquiry(store, "Issue", description="the target")
    await _edge(store, owner, target, note="why the owner needs it")
    model = HashEmbedder(dim=4)
    state = await _sweep(store, model, _sources(model))
    before = await _units(store)

    async with store.engine.acquire() as conn:
        await conn.execute("DELETE FROM inquiries WHERE id = $1", target)
    await _sweep(store, model, _sources(model), state=state)

    assert (owner, f"edge:produced_by:{target}") in before
    assert await _units(store) == [(owner, "text")]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_edge_note_change_reembeds_that_note_only(store: Store) -> None:
    owner = await _inquiry(store, "Issue", description="the owner")
    first = await _inquiry(store, "Issue", description="first")
    second = await _inquiry(store, "Issue", description="second")
    await _edge(store, owner, first, note="kept note")
    await _edge(store, owner, second, note="old note")
    model = HashEmbedder(dim=4)
    state = await _sweep(store, model, _sources(model))
    model.calls.clear()

    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE edges SET note = 'new note' WHERE from_id = $1 AND to_id = $2",
            owner,
            second,
        )
    await _sweep(store, model, _sources(model), state=state)

    assert model.calls == [["new note"]]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_deleted_edge_loses_its_vector(store: Store) -> None:
    owner = await _inquiry(store, "Issue", description="the owner")
    target = await _inquiry(store, "Issue", description="the target")
    await _edge(store, owner, target, note="soon gone")
    model = HashEmbedder(dim=4)
    state = await _sweep(store, model, _sources(model))
    model.calls.clear()

    async with store.engine.acquire() as conn:
        await conn.execute("DELETE FROM edges WHERE from_id = $1", owner)
    await _sweep(store, model, _sources(model), state=state)

    assert model.calls == []
    assert await _units(store) == sorted([(owner, "text"), (target, "text")])


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_emptied_inquiry_field_loses_its_vector(store: Store) -> None:
    emptied = await _inquiry(store, "Issue", description="said and then erased")
    kept = await _inquiry(store, "Issue", description="still said")
    model = HashEmbedder(dim=4)
    state = await _sweep(store, model, _sources(model))

    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET description = NULL WHERE id = $1",
            emptied,
        )
    await _sweep(store, model, _sources(model), state=state)

    assert await _units(store) == [(kept, "text")]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_each_kind_embeds_its_fields_joined_and_a_session_its_notes(
    store: Store,
) -> None:
    """A kind not configured gets no text vector; a session's edge note gets one."""
    issue = await _inquiry(store, "Issue", description="issue words")
    belief = await _inquiry(store, "Belief", description="belief reasons")
    paper = await _inquiry(store, "Paper", abstract="paper abstract")
    artifact = await _inquiry(store, "Artifact", description="artifact words")
    session = await _session(store)
    await _edge(store, belief, issue, note="belief note")
    await _edge(store, paper, artifact, note="paper note")
    await _edge(store, session, issue, note="session note")
    model = HashEmbedder(dim=4)

    await _sweep(store, model, _sources(model))

    assert sorted(text for call in model.calls for text in call) == sorted(
        [
            "issue words",
            "Belief title\n\nbelief reasons",
            "Paper title\n\npaper abstract",
            "belief note",
            "paper note",
            "session note",
        ],
    )
    assert await _units(store) == sorted(
        [
            (issue, "text"),
            (belief, "text"),
            (belief, f"edge:produced_by:{issue}"),
            (paper, "text"),
            (paper, f"edge:produced_by:{artifact}"),
            (session, f"edge:produced_by:{issue}"),
        ],
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    ("fields", "notes", "embedded"),
    [
        ((("Issue", ("description",)),), False, ["issue words"]),
        ((), True, ["issue note"]),
    ],
)
async def test_a_source_left_out_of_the_settings_gets_no_vector(
    store: Store,
    fields: tuple[tuple[str, tuple[str, ...]], ...],
    notes: bool,
    embedded: list[str],
) -> None:
    issue = await _inquiry(store, "Issue", description="issue words")
    other = await _inquiry(store, "Issue")
    await _edge(store, issue, other, note="issue note")
    model = HashEmbedder(dim=4)
    settings = replace(_sources(model), inquiry_fields=fields, edge_notes=notes)

    await _sweep(store, model, settings)

    assert model.calls == [embedded]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_two_writers_of_one_source_both_finish_and_leave_its_rows_once(
    store: Store,
) -> None:
    """A re-embed beside the server's sweep: the second writer meets the first's rows.

    Writer A has read the record pending and deleted its rows, none yet; writer B then
    embeds and commits the same record; A's insert meets B's rows.
    """
    session_id = await _session(store)
    await _record(store, session_id, idx=0, text="c" * 30)
    model = HashEmbedder(dim=4)
    settings = _settings(model, chunk_chars=20)
    second = partial(
        sweep_once,
        store.engine,
        model=model,
        settings=settings,
        state=SweepState(),
        slice_units=4,
    )

    _ = await sweep_once(
        cast("DatabaseEngine", _Pausing(engine=store.engine, between=second)),
        model=model,
        settings=settings,
        state=SweepState(),
        slice_units=4,
    )

    assert [(idx, chunk) for _owner, _part, idx, chunk, *_ in await _rows(store)] == [
        (0, 0),
        (0, 1),
        (0, 2),
    ]


_SNAPSHOT: Final = "SELECT pg_current_snapshot()::text"
_DELETE_OTHER_KEYS: Final = (
    "WITH gone AS (DELETE FROM embedding.vectors WHERE ctid IN "
    "(SELECT ctid FROM embedding.vectors WHERE settings <> $1 LIMIT 10000) "
    "RETURNING 1) SELECT count(*) FROM gone"
)
_JOIN: Final = (
    "JOIN session_manifests m ON m.session_id = r.session_id AND m.part = r.part "
)
_CHANGED_SQL: Final = vetted_sql(
    "SELECT ",
    sweep._COLUMNS,
    " FROM session_records r ",
    _JOIN,
    "WHERE r.changed_xid >= pg_snapshot_xmin($3::text::pg_snapshot) "
    "AND r.changed_xid < pg_snapshot_xmax($4::text::pg_snapshot) "
    "AND NOT pg_visible_in_snapshot(r.changed_xid, $3::text::pg_snapshot) "
    "AND pg_visible_in_snapshot(r.changed_xid, $4::text::pg_snapshot) "
    "AND r.idx < m.records AND ",
    sweep._PENDING,
    " ORDER BY r.changed_xid DESC, r.session_id, r.part, r.idx LIMIT $5",
)
_BACKLOG_ORDER: Final = (
    " ORDER BY r.created DESC, r.session_id DESC, r.part DESC, r.idx DESC LIMIT $3"
)
_KEYSET: Final = "(r.created, r.session_id, r.part, r.idx) < ($4, $5, $6, $7) AND "


@pytest.mark.asyncio
@pytest.mark.parametrize(("deleted", "worked"), [(0, False), (3, True)])
async def test_a_walked_round_deletes_other_keys_then_drops_their_indexes(
    deleted: int,
    worked: bool,
) -> None:
    conn = make_conn()
    conn.fetchval.side_effect = ["5:9:", deleted]
    conn.fetch.side_effect = [[], []]
    model = HashEmbedder(dim=4)
    settings = _settings(model)

    state = await sweep_once(
        cast("DatabaseEngine", FakeEngine(conn)),
        model=model,
        settings=settings,
        state=SweepState(since="5:8:", walked=True),
        slice_units=2,
    )

    assert state == SweepState(since="5:9:", walked=True, worked=worked)
    assert [call.args for call in conn.fetchval.await_args_list] == [
        (_SNAPSHOT,),
        (_DELETE_OTHER_KEYS, settings.key),
    ]
    fetched = [call.args for call in conn.fetch.await_args_list]
    assert fetched[0] == (
        _CHANGED_SQL,
        ["UserMessage", "AssistantMessage"],
        settings.key,
        "5:8:",
        "5:9:",
        2,
    )
    # With rows deleted the indexes wait for the round that deletes none.
    assert len(fetched) == (2 if deleted == 0 else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "after",
    [None, (datetime(2026, 1, 2, tzinfo=UTC), UUID(int=7), 1, 2)],
)
async def test_a_backlog_page_is_embedded_written_and_its_last_record_kept(
    after: tuple[datetime, UUID, int, int] | None,
) -> None:
    conn = make_conn()
    created = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        _row(UUID(int=1), idx=3, created=created, text="hi", source=True),
        _row(UUID(int=3), idx=1, created=created, text="", source=False),
        _row(UUID(int=2), idx=0, created=created, text="", source=False),
    ]
    conn.fetchval.side_effect = ["5:9:"]
    conn.fetch.side_effect = [rows]
    model = HashEmbedder(dim=2)
    settings = _settings(model)

    state = await sweep_once(
        cast("DatabaseEngine", FakeEngine(conn)),
        model=model,
        settings=settings,
        state=SweepState(after=after),
        slice_units=2,
    )

    assert state == SweepState(
        since="5:9:",
        after=(created, UUID(int=2), 0, 0),
        worked=True,
        written=1,
    )
    [page] = [call.args for call in conn.fetch.await_args_list]
    assert page == (
        vetted_sql(
            "SELECT ",
            sweep._COLUMNS,
            " FROM session_records r ",
            _JOIN,
            "WHERE ",
            "" if after is None else _KEYSET,
            "r.idx < m.records AND ",
            sweep._PENDING,
            _BACKLOG_ORDER,
        ),
        ["UserMessage", "AssistantMessage"],
        settings.key,
        2,
        *(() if after is None else after),
    )
    assert model.calls == [["hi"]]
    begin, delete, insert, commit = (call.args for call in conn.execute.await_args_list)
    assert (begin, commit) == (("BEGIN",), ("COMMIT",))
    assert delete == (
        (
            "DELETE FROM embedding.vectors v USING unnest($1::uuid[], $2::int[], "
            "$3::int[], $4::text[]) AS t(owner_id, part, idx, field) "
            "WHERE v.owner_id = t.owner_id AND v.part = t.part AND v.idx = t.idx "
            "AND v.field = t.field"
        ),
        [UUID(int=1), UUID(int=3), UUID(int=2)],
        [0, 0, 0],
        [3, 1, 0],
        ["content", "content", "content"],
    )
    assert insert == (
        (
            "INSERT INTO embedding.vectors "
            "(owner_id, part, idx, field, chunk, text_md5, settings, embedding) "
            "SELECT owner_id, part, idx, field, chunk, text_md5, $8, "
            "embedding::halfvec FROM unnest($1::uuid[], $2::int[], $3::int[], "
            "$4::text[], $5::int[], $6::text[], $7::text[]) "
            "AS t(owner_id, part, idx, field, chunk, text_md5, embedding) "
            "ON CONFLICT (owner_id, part, idx, field, chunk) DO UPDATE SET "
            "text_md5 = EXCLUDED.text_md5, settings = EXCLUDED.settings, "
            "embedding = EXCLUDED.embedding, created = EXCLUDED.created"
        ),
        [UUID(int=1)],
        [0],
        [3],
        ["content"],
        [0],
        ["md5-hi"],
        [vec_to_text(await model.embed_query("hi"))],
        settings.key,
    )


@pytest.mark.asyncio
async def test_changed_pages_come_first_and_every_page_counts_its_units() -> None:
    conn = make_conn()
    created = datetime(2026, 1, 1, tzinfo=UTC)
    changed = [
        _row(UUID(int=1), idx=idx, created=created, text=f"new {idx}", source=True)
        for idx in (0, 1)
    ]
    backlog = [
        _row(UUID(int=2), idx=idx, created=created, text=f"old {idx}", source=True)
        for idx in (2, 1)
    ]
    conn.fetchval.side_effect = ["5:9:"]
    conn.fetch.side_effect = [[changed[0]], [changed[1]], [], backlog]
    model = HashEmbedder(dim=2)

    state = await sweep_once(
        cast("DatabaseEngine", FakeEngine(conn)),
        model=model,
        settings=_settings(model),
        state=SweepState(since="5:8:"),
        slice_units=4,
    )

    assert model.calls == [["new 0"], ["new 1"], ["old 2", "old 1"]]
    assert state == SweepState(
        since="5:9:",
        after=(created, UUID(int=2), 0, 1),
        worked=True,
        written=4,
    )


@pytest.mark.asyncio
async def test_a_changed_page_alone_is_work() -> None:
    """After the walk, a round that embeds only changed records still worked."""
    conn = make_conn()
    created = datetime(2026, 1, 1, tzinfo=UTC)
    conn.fetchval.side_effect = ["5:9:", 0]
    conn.fetch.side_effect = [
        [_row(UUID(int=1), idx=0, created=created, text="new", source=True)],
        [],
        [],
    ]
    model = HashEmbedder(dim=2)

    state = await sweep_once(
        cast("DatabaseEngine", FakeEngine(conn)),
        model=model,
        settings=_settings(model),
        state=SweepState(since="5:8:", walked=True),
        slice_units=4,
    )

    assert state == SweepState(since="5:9:", walked=True, worked=True, written=1)


@pytest.mark.asyncio
async def test_an_empty_backlog_ends_the_walk() -> None:
    conn = make_conn()
    conn.fetchval.side_effect = ["5:9:", 0]
    conn.fetch.side_effect = [[], []]
    model = HashEmbedder(dim=2)

    state = await sweep_once(
        cast("DatabaseEngine", FakeEngine(conn)),
        model=model,
        settings=_settings(model),
        state=SweepState(),
        slice_units=2,
    )

    assert state == SweepState(since="5:9:", walked=True)


def _row(
    owner_id: UUID,
    *,
    idx: int,
    created: datetime,
    text: str,
    source: bool,
) -> dict[str, object]:
    """Return a page row as the sweep's queries answer."""
    return {
        "created": created,
        "owner_id": owner_id,
        "part": 0,
        "idx": idx,
        "field": "content",
        "text": text,
        "text_md5": f"md5-{text}",
        "source": source,
    }


class _IdleError(Exception):
    """The service reached its wait between idle rounds."""


async def _idle(waits: list[float], seconds: float) -> None:
    """Stand in for the idle wait: note it, then stop the service."""
    waits.append(seconds)
    raise _IdleError


def _settings(model: HashEmbedder, *, chunk_chars: int = 1_500) -> Settings:
    """Return the settings ``model`` embeds messages under, at ``chunk_chars``."""
    kinds = ("UserMessage", "AssistantMessage")
    return Settings(
        key=settings_key(
            model=model.name,
            dim=model.dim,
            chunk_chars=chunk_chars,
            overlap_chars=7,
            session_kinds=kinds,
        ),
        dim=model.dim,
        chunk_chars=chunk_chars,
        overlap_chars=7,
        session_kinds=kinds,
    )


def _sources(model: HashEmbedder) -> Settings:
    """Return the settings ``model`` embeds messages, inquiries and edge notes under."""
    kinds = ("UserMessage", "AssistantMessage")
    fields = (
        ("Issue", ("description",)),
        ("Belief", ("title", "description")),
        ("Paper", ("title", "paper_abstract")),
    )
    return Settings(
        key=settings_key(
            model=model.name,
            dim=model.dim,
            chunk_chars=1_500,
            overlap_chars=7,
            session_kinds=kinds,
            inquiry_fields=fields,
            edge_notes=True,
        ),
        dim=model.dim,
        chunk_chars=1_500,
        overlap_chars=7,
        session_kinds=kinds,
        inquiry_fields=fields,
        edge_notes=True,
    )


async def _sweep(
    store: Store,
    model: HashEmbedder,
    settings: Settings,
    *,
    state: SweepState | None = None,
) -> SweepState:
    """Run rounds until one does no work; return the last state."""
    return await _sweep_engine(store.engine, model, settings, state=state)


async def _sweep_engine(
    engine: DatabaseEngine,
    model: HashEmbedder,
    settings: Settings,
    *,
    state: SweepState | None = None,
) -> SweepState:
    """Run rounds over ``engine`` until one does no work; return the last state."""
    state = SweepState() if state is None else state
    while True:
        state = await sweep_once(
            engine,
            model=model,
            settings=settings,
            state=state,
            slice_units=4,
        )
        if not state.worked:
            return state


async def _session(store: Store) -> UUID:
    """Return a new open AgentSession the records can hang off."""
    session_id = uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title, "
            "agentsession_audience) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "'tester@example.com', 'embed test', 'open')",
            session_id,
        )
    return session_id


async def _inquiry(
    store: Store,
    kind: str,
    *,
    description: str | None = None,
    abstract: str | None = None,
) -> UUID:
    """Insert an inquiry of ``kind`` titled ``"<kind> title"``; return its id."""
    inquiry_id = uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            vetted_sql(
                "INSERT INTO inquiries (id, kind, seq, status, account, title, "
                "description, paper_abstract) VALUES ($1, $2, nextval('seq_",
                kind.lower(),
                "'), 'active', 'tester@example.com', $2 || ' title', $3, $4)",
            ),
            inquiry_id,
            kind,
            description,
            abstract,
        )
    return inquiry_id


async def _edge(store: Store, from_id: UUID, to_id: UUID, *, note: str) -> None:
    """Insert a ``produced_by`` edge between two inquiries, with ``note``."""
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO edges (from_id, from_kind, to_id, to_kind, edge_kind, note) "
            "SELECT f.id, f.kind, t.id, t.kind, 'produced_by', $3 "
            "FROM inquiries f, inquiries t WHERE f.id = $1 AND t.id = $2",
            from_id,
            to_id,
            note,
        )


async def _units(store: Store) -> list[tuple[UUID, str]]:
    """Return every inquiry and edge-note unit's ``(owner, field)``, in order."""
    async with store.engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT owner_id, field FROM embedding.vectors "
            "WHERE field <> 'content' ORDER BY owner_id, field",
        )
    return [cast("tuple[UUID, str]", tuple(row.values())) for row in rows]


# The sweep reads only the live manifest prefix (``idx < records``), so a record
# needs a manifest that covers it -- production never writes one without the other.
async def _record(
    store: Store,
    session_id: UUID,
    *,
    idx: int,
    text: str,
    kind: str = "UserMessage",
) -> None:
    """Insert one ``session_records`` row plus the manifest that bounds it."""
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO session_records (session_id, part, idx, kind, payload, text) "
            "VALUES ($1, 0, $2, $3, '{}'::json, $4)",
            session_id,
            idx,
            kind,
            text,
        )
        await conn.execute(
            "INSERT INTO session_manifests "
            "(session_id, part, name, metadata, ir_id, format, records) "
            "VALUES ($1, 0, 's.jsonl', '{}'::json, gen_random_uuid(), 'claude', $2) "
            "ON CONFLICT (session_id, part) DO UPDATE SET "
            "records = GREATEST(session_manifests.records, $2)",
            session_id,
            idx + 1,
        )


async def _edit(store: Store, session_id: UUID, *, idx: int, text: str) -> None:
    """Rewrite one record's text, as a compaction's restart does."""
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE session_records SET text = $3 WHERE session_id = $1 AND idx = $2",
            session_id,
            idx,
            text,
        )


async def _rows(store: Store) -> list[tuple[UUID, int, int, int, str, str]]:
    """Return every vector row's ``(owner, part, idx, chunk, settings, md5)``."""
    async with store.engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT owner_id, part, idx, chunk, settings, text_md5 "
            "FROM embedding.vectors ORDER BY owner_id, part, idx, chunk, settings",
        )
    return [
        cast("tuple[UUID, int, int, int, str, str]", tuple(row.values()))
        for row in rows
    ]


async def _md5(store: Store, text: str) -> str:
    """Return ``text``'s md5 as the database computes it."""
    async with store.engine.acquire() as conn:
        return str(await conn.fetchval("SELECT md5($1)", text))


async def _index(store: Store, settings: Settings) -> None:
    """Make ``settings``' vector index, as a server under them did."""
    async with store.engine.acquire() as conn:
        await conn.execute(
            f"CREATE INDEX vectors_hnsw_{settings.key} ON embedding.vectors "
            f"USING hnsw ((embedding::halfvec({settings.dim})) halfvec_cosine_ops) "
            f"WHERE settings = '{settings.key}'",
        )


async def _indexes(store: Store) -> list[str]:
    """Return the vector table's HNSW indexes, by name."""
    async with store.engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'embedding' "
            "AND indexname LIKE 'vectors_hnsw_%' ORDER BY indexname",
        )
    return [str(row["indexname"]) for row in rows]


async def _found(search: Nearest, store: Store, query: str) -> list[tuple[UUID, int]]:
    """Return the ``(owner, idx)`` of each unit ``search`` finds for ``query``."""
    hits = await search.nearest(
        store.engine,
        query=query,
        kept=_keep_all,
        filtered=False,
        limit=5,
    )
    assert hits is not None
    return [(hit.owner_id, hit.idx) for hit in hits]


def _keep_all(params: list[object]) -> str:
    """Keep every unit; bind nothing."""
    del params
    return "TRUE"


def _context(store: Store) -> ServerContext:
    """Return a server context holding ``store`` and nothing else of use."""
    return ServerContext(
        store=store,
        host_idle_sec=_never_idle,
        hosts_changed=_ignored,
        host_closed=_closed,
    )


def _never_idle(host_id: UUID) -> float:
    del host_id
    return 0.0


def _ignored(email: str) -> None:
    del email


async def _closed(host_id: UUID) -> None:
    del host_id


@dataclass(slots=True, kw_only=True)
class _CountingEngine:
    """An engine that counts the connections it lends and how many are out."""

    engine: DatabaseEngine
    out: int = 0
    acquired: int = 0

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[Conn]:
        """Lend one of the wrapped engine's connections, counted."""
        self.out += 1
        self.acquired += 1
        try:
            async with self.engine.acquire() as conn:
                yield conn
        finally:
            self.out -= 1


@dataclass(slots=True, kw_only=True)
class _Pausing:
    """An engine whose connections run ``between`` once, right after a vector delete."""

    engine: DatabaseEngine
    between: Callable[[], Awaitable[object]] | None

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[_PausingConn]:
        """Lend one of the wrapped engine's connections, paused after its delete."""
        async with self.engine.acquire() as conn:
            yield _PausingConn(conn=conn, engine=self)


@dataclass(slots=True, kw_only=True)
class _PausingConn:
    """A connection that hands its engine's ``between`` the turn after a delete."""

    conn: Conn
    engine: _Pausing

    async def execute(self, sql: str, *args: object) -> str:
        """Run ``sql``; after the first vector delete, run ``between`` to its end."""
        done = await self.conn.execute(sql, *args)
        if sql.startswith("DELETE FROM embedding.vectors") and self.engine.between:
            between, self.engine.between = self.engine.between, None
            _ = await between()
        return done

    async def fetch(self, sql: str, *args: object) -> list[Record]:
        """Run ``sql`` on the wrapped connection."""
        return await self.conn.fetch(sql, *args)

    async def fetchval(self, sql: str, *args: object) -> object:
        """Run ``sql`` on the wrapped connection."""
        return await self.conn.fetchval(sql, *args)


class _Watching(HashEmbedder):
    """A hash model that records how many connections are out at each call."""

    def __init__(self, *, engine: _CountingEngine) -> None:
        super().__init__(dim=4)
        self.engine = engine
        self.held: list[int] = []

    @override
    async def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """Note the connections out, then embed."""
        self.held.append(self.engine.out)
        return await super().embed_batch(texts)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
