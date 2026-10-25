"""Owned Issue workspaces and their per-user bar preferences."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

import asyncio
import uuid

from pydantic import ValidationError

import pytest
import pytest_asyncio

from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.preferences import WorkspacesPreference
from trackinizer.server.store.core import Store
from trackinizer.server.visuals.canvas_store import new_canvas
from trackinizer.server.visuals.catalog import default_catalog
from trackinizer.server.visuals.workspaces import (
    UpdateWorkspace,
    WorkspaceOrder,
    _toggle_id,
    check_workspace,
    create_workspace,
    list_workspaces,
    next_untitled_title,
    open_default_workspace,
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


@pytest.mark.parametrize(
    ("titles", "expected"),
    [
        ([], "Untitled workspace"),
        (["Untitled workspace"], "Untitled workspace 2"),
        (["Untitled workspace", "Untitled workspace 3"], "Untitled workspace 2"),
        (["Untitled workspace 2", "Research"], "Untitled workspace"),
        (["untitled workspace", "Untitled workspace 2x"], "Untitled workspace"),
    ],
)
def test_an_untitled_workspace_takes_the_lowest_number_not_taken(
    titles: list[str],
    expected: str,
) -> None:
    assert next_untitled_title(titles) == expected


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
async def test_two_first_calls_make_one_untitled_default(
    context: tuple[PGliteEngine, Store],
) -> None:
    engine, store = context
    first, second = await asyncio.gather(
        open_default_workspace(engine, store=store, user_id=_USER, email=_EMAIL),
        open_default_workspace(engine, store=store, user_id=_USER, email=_EMAIL),
    )
    assert first == second
    async with engine.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM inquiries WHERE owner = $1 "
            "AND labels @> ARRAY['workspace', 'scratch']::text[]",
            _EMAIL,
        )
    issue = await store.get_inquiry(first)
    assert count == 1
    assert issue is not None
    assert issue.title == "Untitled workspace"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_opening_the_default_again_writes_no_preference_and_makes_no_issue(
    context: tuple[PGliteEngine, Store],
) -> None:
    engine, store = context
    first = await open_default_workspace(
        engine,
        store=store,
        user_id=_USER,
        email=_EMAIL,
    )
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

    second = await open_default_workspace(
        engine,
        store=store,
        user_id=_USER,
        email=_EMAIL,
    )

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
    default = await open_default_workspace(
        engine,
        store=store,
        user_id=_USER,
        email=_EMAIL,
    )
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
    # The default sorts as any workspace does: not pinned, so after the pins, oldest.
    assert [item.id for item in all_streams] == [beta.id, alpha.id, default]
    assert all_streams[0].hidden
    assert all_streams[0].pinned
    assert (all_streams[2].scratch, all_streams[2].pinned) == (True, False)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_default_hides_and_pins_as_any_workspace_does(
    context: tuple[PGliteEngine, Store],
) -> None:
    engine, store = context
    default = await open_default_workspace(
        engine,
        store=store,
        user_id=_USER,
        email=_EMAIL,
    )
    change = partial(
        update_workspace,
        engine,
        store=store,
        user_id=_USER,
        email=_EMAIL,
        workspace_id=default,
    )

    hidden = await change(hidden=True)
    pinned = await change(pinned=True)
    unpinned = await change(pinned=False)
    shown = await list_workspaces(engine, user_id=_USER, email=_EMAIL)

    assert hidden is not None
    assert pinned is not None
    assert unpinned is not None
    assert (hidden.hidden, pinned.pinned, unpinned.pinned) == (True, True, False)
    assert hidden.scratch
    assert shown == []


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_renaming_the_default_promotes_it_with_its_canvases(
    context: tuple[PGliteEngine, Store],
) -> None:
    """A renamed default is ordinary and keeps every canvas it held; the next is new."""
    engine, store = context
    first = await open_default_workspace(
        engine,
        store=store,
        user_id=_USER,
        email=_EMAIL,
    )
    loose = uuid.uuid4()
    async with engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO visual_canvases (id, user_id, metadata, state) "
            "VALUES ($1, $2, $3, $4)",
            loose,
            _USER,
            {"name": "loose"},
            {"visuals": []},
        )

    renamed = await update_workspace(
        engine,
        store=store,
        user_id=_USER,
        email=_EMAIL,
        workspace_id=first,
        title="Research",
    )
    second = await open_default_workspace(
        engine,
        store=store,
        user_id=_USER,
        email=_EMAIL,
    )
    kept = await workspace_info(
        engine,
        user_id=_USER,
        email=_EMAIL,
        workspace_id=first,
    )
    fresh = await workspace_info(
        engine,
        user_id=_USER,
        email=_EMAIL,
        workspace_id=second,
    )
    issue = await store.get_inquiry(first)

    assert renamed is not None
    assert (renamed.title, renamed.scratch) == ("Research", False)
    assert issue is not None
    assert issue.labels == ("workspace",)
    assert second != first
    assert kept is not None
    assert fresh is not None
    assert [canvas.id for canvas in kept.canvases] == [loose]
    assert (fresh.title, fresh.scratch, fresh.canvases) == (
        "Untitled workspace",
        True,
        [],
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_next_default_skips_the_numbers_the_users_own_workspaces_hold(
    context: tuple[PGliteEngine, Store],
) -> None:
    engine, store = context
    for title in ("Untitled workspace", "Untitled workspace 3"):
        await create_workspace(
            engine,
            store=store,
            user_id=_USER,
            email=_EMAIL,
            title=title,
        )
    await store.submit_issue(
        SubmitIssue(
            title="Untitled workspace 2",
            account="other@example.com",
            owner="other@example.com",
            labels=["workspace"],
        ),
        actor="other@example.com",
    )

    default = await open_default_workspace(
        engine,
        store=store,
        user_id=_USER,
        email=_EMAIL,
    )

    issue = await store.get_inquiry(default)
    assert issue is not None
    assert issue.title == "Untitled workspace 2"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_canvas_made_in_a_hidden_workspace_shows_it_again(
    context: tuple[PGliteEngine, Store],
) -> None:
    """Nothing lands out of sight; a hidden workspace no canvas lands in stays hidden."""
    engine, store = context
    default = await open_default_workspace(
        engine,
        store=store,
        user_id=_USER,
        email=_EMAIL,
    )
    other = await create_workspace(
        engine,
        store=store,
        user_id=_USER,
        email=_EMAIL,
        title="Other",
    )
    for workspace_id in (default, other.id):
        await update_workspace(
            engine,
            store=store,
            user_id=_USER,
            email=_EMAIL,
            workspace_id=workspace_id,
            hidden=True,
        )

    await new_canvas(
        engine,
        user_id=_USER,
        catalog=default_catalog(),
        inbound=InboundQueue(),
        assistant=None,
        workspace_id=default,
    )

    listed = await list_workspaces(
        engine,
        user_id=_USER,
        email=_EMAIL,
        include_hidden=True,
    )
    assert {entry.id: entry.hidden for entry in listed} == {
        default: False,
        other.id: True,
    }


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
async def test_info_includes_canvases_and_puts_one_in_no_workspace_in_the_default(
    context: tuple[PGliteEngine, Store],
) -> None:
    engine, store = context
    default = await open_default_workspace(
        engine,
        store=store,
        user_id=_USER,
        email=_EMAIL,
    )
    other = await create_workspace(
        engine,
        store=store,
        user_id=_USER,
        email=_EMAIL,
        title="Other",
    )
    async with engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO visual_canvases (id, user_id, metadata, state) "
            "VALUES ($1, $2, $3, $4)",
            uuid.uuid4(),
            _USER,
            {"name": "loose-canvas"},
            {"visuals": []},
        )
    info = await workspace_info(
        engine,
        user_id=_USER,
        email=_EMAIL,
        workspace_id=default,
    )
    elsewhere = await workspace_info(
        engine,
        user_id=_USER,
        email=_EMAIL,
        workspace_id=other.id,
    )
    assert info is not None
    assert elsewhere is not None
    assert [canvas.name for canvas in info.canvases] == ["loose-canvas"]
    assert elsewhere.canvases == []


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_opened_canvas_becomes_its_workspaces_last_and_the_bar_keeps_the_rest(
    context: tuple[PGliteEngine, Store],
) -> None:
    engine, store = context
    default = await open_default_workspace(
        engine,
        store=store,
        user_id=_USER,
        email=_EMAIL,
    )
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
    # A canvas with no workspace is the default's; the pin set before is kept.
    assert listed[research.id].last_canvas_id == str(second)
    assert listed[default].last_canvas_id == str(loose)
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
