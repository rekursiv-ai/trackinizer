"""Owned Issue workstreams and their per-user bar preferences."""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio
import uuid

from pydantic import ValidationError

import pytest
import pytest_asyncio

from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.preferences import WorkstreamsPreference
from trackinizer.server.store.core import Store
from trackinizer.server.visuals.workstreams import (
    ScratchWorkstreamError,
    UpdateWorkstream,
    WorkstreamOrder,
    _toggle_id,
    check_workstream,
    create_workstream,
    list_workstreams,
    open_scratch,
    set_workstream_order,
    update_workstream,
    workstream_info,
)
from trackinizer.wire.bodies import SubmitIssue


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trackinizer.lib.postgres import PGliteEngine


_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")
_EMAIL = "streams@example.com"


def test_workstream_preference_toggle_changes_only_the_named_flag() -> None:
    hidden_id, pinned_id = uuid.uuid4(), uuid.uuid4()
    preference = WorkstreamsPreference(hidden=[str(hidden_id)])

    pinned = _toggle_id(preference, "pinned", pinned_id, True)
    unhidden = _toggle_id(pinned, "hidden", hidden_id, False)

    assert pinned.hidden == [str(hidden_id)]
    assert pinned.pinned == [str(pinned_id)]
    assert unhidden.hidden == []
    assert unhidden.pinned == [str(pinned_id)]


def test_workstream_update_and_order_models_refuse_empty_or_duplicate_changes() -> None:
    with pytest.raises(ValidationError):
        UpdateWorkstream()
    with pytest.raises(ValidationError):
        WorkstreamOrder(ids=[_USER, _USER])


@pytest_asyncio.fixture(loop_scope="session")
async def context(
    pglite_engine: PGliteEngine,
) -> AsyncIterator[tuple[PGliteEngine, Store]]:
    """Return a fresh store with one active preference owner."""
    await reset_schema(pglite_engine)
    store = Store(pglite_engine, embed=StubEmbedder())
    await store.bootstrap()
    async with pglite_engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, $2, 'Streams', 'writer', 'active')",
            _USER,
            _EMAIL,
        )
    yield pglite_engine, store


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_two_first_calls_create_one_scratch_issue(
    context: tuple[PGliteEngine, Store],
) -> None:
    engine, store = context
    first, second = await asyncio.gather(
        open_scratch(engine, store=store, user_id=_USER, email=_EMAIL),
        open_scratch(engine, store=store, user_id=_USER, email=_EMAIL),
    )
    assert first == second
    async with engine.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM inquiries WHERE owner = $1 "
            "AND labels @> ARRAY['workstream', 'scratch']::text[]",
            _EMAIL,
        )
    assert count == 1


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_open_scratch_fast_path_does_not_write_preferences_or_create_an_issue(
    context: tuple[PGliteEngine, Store],
) -> None:
    engine, store = context
    first = await open_scratch(engine, store=store, user_id=_USER, email=_EMAIL)
    async with engine.acquire() as conn:
        before = await conn.fetchrow(
            "SELECT preferences, xmin::text AS version FROM users WHERE id = $1",
            _USER,
        )
        assert before is not None
        count_before = await conn.fetchval(
            "SELECT count(*) FROM inquiries WHERE owner = $1 "
            "AND labels @> ARRAY['workstream', 'scratch']::text[]",
            _EMAIL,
        )

    second = await open_scratch(engine, store=store, user_id=_USER, email=_EMAIL)

    async with engine.acquire() as conn:
        after = await conn.fetchrow(
            "SELECT preferences, xmin::text AS version FROM users WHERE id = $1",
            _USER,
        )
        assert after is not None
        count_after = await conn.fetchval(
            "SELECT count(*) FROM inquiries WHERE owner = $1 "
            "AND labels @> ARRAY['workstream', 'scratch']::text[]",
            _EMAIL,
        )

    assert second == first
    assert after["preferences"] == before["preferences"]
    assert after["version"] == before["version"]
    assert count_after == count_before == 1


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_created_workstream_is_an_owned_active_issue(
    context: tuple[PGliteEngine, Store],
) -> None:
    engine, store = context
    made = await create_workstream(
        engine,
        store=store,
        user_id=_USER,
        email=_EMAIL,
        title="  Research  ",
    )
    issue = await store.get_inquiry(made.id)
    assert (made.title, made.scratch, made.hidden, made.pinned) == (
        "Research",
        False,
        False,
        False,
    )
    assert issue is not None
    assert (issue.owner, issue.account, issue.status, issue.labels) == (
        _EMAIL,
        _EMAIL,
        "active",
        ("workstream",),
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_listing_searches_and_orders_pins_and_hidden_workstreams(
    context: tuple[PGliteEngine, Store],
) -> None:
    engine, store = context
    scratch = await open_scratch(engine, store=store, user_id=_USER, email=_EMAIL)
    alpha = await create_workstream(
        engine,
        store=store,
        user_id=_USER,
        email=_EMAIL,
        title="Alpha",
    )
    beta = await create_workstream(
        engine,
        store=store,
        user_id=_USER,
        email=_EMAIL,
        title="Beta",
    )
    await update_workstream(
        engine,
        store=store,
        user_id=_USER,
        email=_EMAIL,
        workstream_id=beta.id,
        hidden=True,
        pinned=True,
    )
    visible = await list_workstreams(
        engine,
        user_id=_USER,
        email=_EMAIL,
        q="alp",
    )
    all_streams = await list_workstreams(
        engine,
        user_id=_USER,
        email=_EMAIL,
        include_hidden=True,
    )
    assert [item.id for item in visible] == [alpha.id]
    assert [item.id for item in all_streams] == [scratch, beta.id, alpha.id]
    assert all_streams[1].hidden
    assert all_streams[1].pinned


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_scratch_cannot_be_hidden_but_can_be_pinned(
    context: tuple[PGliteEngine, Store],
) -> None:
    engine, store = context
    scratch = await open_scratch(engine, store=store, user_id=_USER, email=_EMAIL)
    with pytest.raises(ScratchWorkstreamError):
        await update_workstream(
            engine,
            store=store,
            user_id=_USER,
            email=_EMAIL,
            workstream_id=scratch,
            hidden=True,
        )
    updated = await update_workstream(
        engine,
        store=store,
        user_id=_USER,
        email=_EMAIL,
        workstream_id=scratch,
        pinned=False,
    )
    assert updated is not None
    assert updated.pinned


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_order_is_saved_and_foreign_or_duplicate_ids_are_refused(
    context: tuple[PGliteEngine, Store],
) -> None:
    engine, store = context
    first = await create_workstream(
        engine,
        store=store,
        user_id=_USER,
        email=_EMAIL,
        title="First",
    )
    second = await create_workstream(
        engine,
        store=store,
        user_id=_USER,
        email=_EMAIL,
        title="Second",
    )
    ordered = await set_workstream_order(
        engine,
        user_id=_USER,
        email=_EMAIL,
        ids=[second.id, first.id],
    )
    assert ordered is not None
    assert [item.id for item in ordered[:2]] == [second.id, first.id]
    assert (
        await set_workstream_order(
            engine,
            user_id=_USER,
            email=_EMAIL,
            ids=[uuid.uuid4()],
        )
        is None
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_foreign_and_unlabelled_issues_are_not_workstreams(
    context: tuple[PGliteEngine, Store],
) -> None:
    engine, store = context
    issue_id = await store.submit_issue(
        SubmitIssue(
            title="Ordinary issue",
            account=_EMAIL,
            owner=_EMAIL,
        ),
        actor=_EMAIL,
    )
    assert not await check_workstream(engine, email=_EMAIL, workstream_id=issue_id)
    assert not await check_workstream(
        engine,
        email="other@example.com",
        workstream_id=issue_id,
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_info_includes_stations_and_defaults_legacy_stations_to_scratch(
    context: tuple[PGliteEngine, Store],
) -> None:
    engine, store = context
    scratch = await open_scratch(engine, store=store, user_id=_USER, email=_EMAIL)
    async with engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO visual_workspaces (id, user_id, station, state) "
            "VALUES ($1, $2, $3, $4)",
            uuid.uuid4(),
            _USER,
            {"name": "scratch-station"},
            {"visuals": []},
        )
    info = await workstream_info(
        engine,
        user_id=_USER,
        email=_EMAIL,
        workstream_id=scratch,
    )
    assert info is not None
    assert [station.name for station in info.stations] == ["scratch-station"]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
