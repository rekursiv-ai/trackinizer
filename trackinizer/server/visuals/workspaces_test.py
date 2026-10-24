"""Owned Issue workspaces and their per-user bar preferences."""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio
import uuid

from pydantic import ValidationError

import pytest
import pytest_asyncio

from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.preferences import WorkspacesPreference
from trackinizer.server.store.core import Store
from trackinizer.server.visuals.workspaces import (
    ScratchWorkspaceError,
    UpdateWorkspace,
    WorkspaceOrder,
    _toggle_id,
    check_workspace,
    create_workspace,
    list_workspaces,
    open_scratch,
    remember_opened_canvas,
    set_workspace_order,
    update_workspace,
    workspace_info,
)
from trackinizer.wire.bodies import SubmitIssue


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trackinizer.lib.postgres import PGliteEngine


_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")
_EMAIL = "streams@example.com"


def test_workspace_preference_toggle_changes_only_the_named_flag() -> None:
    hidden_id, pinned_id = uuid.uuid4(), uuid.uuid4()
    preference = WorkspacesPreference(hidden=[str(hidden_id)])

    pinned = _toggle_id(preference, "pinned", pinned_id, True)
    unhidden = _toggle_id(pinned, "hidden", hidden_id, False)

    assert pinned.hidden == [str(hidden_id)]
    assert pinned.pinned == [str(pinned_id)]
    assert unhidden.hidden == []
    assert unhidden.pinned == [str(pinned_id)]


def test_workspace_update_and_order_models_refuse_empty_or_duplicate_changes() -> None:
    with pytest.raises(ValidationError):
        UpdateWorkspace()
    with pytest.raises(ValidationError):
        WorkspaceOrder(ids=[_USER, _USER])


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
            "AND labels @> ARRAY['workspace', 'scratch']::text[]",
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
            "AND labels @> ARRAY['workspace', 'scratch']::text[]",
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
            "AND labels @> ARRAY['workspace', 'scratch']::text[]",
            _EMAIL,
        )

    assert second == first
    assert after["preferences"] == before["preferences"]
    assert after["version"] == before["version"]
    assert count_after == count_before == 1


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_created_workspace_is_an_owned_active_issue(
    context: tuple[PGliteEngine, Store],
) -> None:
    engine, store = context
    made = await create_workspace(
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
        ("workspace",),
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_listing_searches_and_orders_pins_and_hidden_workspaces(
    context: tuple[PGliteEngine, Store],
) -> None:
    engine, store = context
    scratch = await open_scratch(engine, store=store, user_id=_USER, email=_EMAIL)
    alpha = await create_workspace(
        engine,
        store=store,
        user_id=_USER,
        email=_EMAIL,
        title="Alpha",
    )
    beta = await create_workspace(
        engine,
        store=store,
        user_id=_USER,
        email=_EMAIL,
        title="Beta",
    )
    await update_workspace(
        engine,
        store=store,
        user_id=_USER,
        email=_EMAIL,
        workspace_id=beta.id,
        hidden=True,
        pinned=True,
    )
    visible = await list_workspaces(
        engine,
        user_id=_USER,
        email=_EMAIL,
        q="alp",
    )
    all_streams = await list_workspaces(
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
    with pytest.raises(ScratchWorkspaceError):
        await update_workspace(
            engine,
            store=store,
            user_id=_USER,
            email=_EMAIL,
            workspace_id=scratch,
            hidden=True,
        )
    updated = await update_workspace(
        engine,
        store=store,
        user_id=_USER,
        email=_EMAIL,
        workspace_id=scratch,
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
    first = await create_workspace(
        engine,
        store=store,
        user_id=_USER,
        email=_EMAIL,
        title="First",
    )
    second = await create_workspace(
        engine,
        store=store,
        user_id=_USER,
        email=_EMAIL,
        title="Second",
    )
    ordered = await set_workspace_order(
        engine,
        user_id=_USER,
        email=_EMAIL,
        ids=[second.id, first.id],
    )
    assert ordered is not None
    assert [item.id for item in ordered[:2]] == [second.id, first.id]
    assert (
        await set_workspace_order(
            engine,
            user_id=_USER,
            email=_EMAIL,
            ids=[uuid.uuid4()],
        )
        is None
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_foreign_and_unlabelled_issues_are_not_workspaces(
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
    assert not await check_workspace(engine, email=_EMAIL, workspace_id=issue_id)
    assert not await check_workspace(
        engine,
        email="other@example.com",
        workspace_id=issue_id,
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_info_includes_canvases_and_defaults_legacy_canvases_to_scratch(
    context: tuple[PGliteEngine, Store],
) -> None:
    engine, store = context
    scratch = await open_scratch(engine, store=store, user_id=_USER, email=_EMAIL)
    async with engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO visual_canvases (id, user_id, metadata, state) "
            "VALUES ($1, $2, $3, $4)",
            uuid.uuid4(),
            _USER,
            {"name": "scratch-canvas"},
            {"visuals": []},
        )
    info = await workspace_info(
        engine,
        user_id=_USER,
        email=_EMAIL,
        workspace_id=scratch,
    )
    assert info is not None
    assert [canvas.name for canvas in info.canvases] == ["scratch-canvas"]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_opened_canvas_becomes_its_workspaces_last_and_the_bar_keeps_the_rest(
    context: tuple[PGliteEngine, Store],
) -> None:
    engine, store = context
    scratch = await open_scratch(engine, store=store, user_id=_USER, email=_EMAIL)
    research = await create_workspace(
        engine,
        store=store,
        user_id=_USER,
        email=_EMAIL,
        title="Research",
    )
    await update_workspace(
        engine,
        store=store,
        user_id=_USER,
        email=_EMAIL,
        workspace_id=research.id,
        pinned=True,
    )
    other = uuid.UUID("44444444-4444-4444-4444-444444444444")
    first, second, loose, theirs = (uuid.uuid4() for _ in range(4))
    async with engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, 'other@example.com', 'Other', 'writer', 'active')",
            other,
        )
        for canvas_id, owner, metadata in (
            (first, _USER, {"name": "first", "workspace_id": str(research.id)}),
            (second, _USER, {"name": "second", "workspace_id": str(research.id)}),
            (loose, _USER, {"name": "loose"}),
            (theirs, other, {"name": "theirs", "workspace_id": str(research.id)}),
        ):
            await conn.execute(
                "INSERT INTO visual_canvases (id, user_id, metadata, state) "
                "VALUES ($1, $2, $3, $4)",
                canvas_id,
                owner,
                metadata,
                {"visuals": []},
            )

    opened = [
        await remember_opened_canvas(
            engine,
            user_id=_USER,
            email=_EMAIL,
            canvas_id=canvas_id,
        )
        for canvas_id in (first, loose, second, theirs, uuid.uuid4())
    ]

    assert opened == [True, True, True, False, False]
    listed = {
        entry.id: entry
        for entry in await list_workspaces(
            engine,
            user_id=_USER,
            email=_EMAIL,
            include_hidden=True,
        )
    }
    # A canvas with no workspace is Scratch's; the pin set before is kept.
    assert listed[research.id].last_canvas_id == str(second)
    assert listed[scratch].last_canvas_id == str(loose)
    assert listed[research.id].pinned
    info = await workspace_info(
        engine,
        user_id=_USER,
        email=_EMAIL,
        workspace_id=research.id,
    )
    assert info is not None
    assert info.last_canvas_id == str(second)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
