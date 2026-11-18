"""Creation-only backfill on the in-process PGlite substrate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx2
import pytest
import pytest_asyncio

from trackinizer.conftest import new_uuid
from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.api.app import app
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.store.core import Store
from trackinizer.wire.bodies import (
    BatchEdge,
    SubmitAgentSession,
    SubmitArtifact,
    SubmitBase,
    SubmitBatch,
    SubmitBelief,
    SubmitCodeChange,
    SubmitExperiment,
    SubmitIssue,
    SubmitPaper,
    SubmitWebResult,
    SubmitWebSearch,
)


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trackinizer.lib.postgres import PGliteEngine
    from trackinizer.wire.bodies import SubmitItem


@pytest_asyncio.fixture(loop_scope="session")
async def store(pglite_engine: PGliteEngine) -> AsyncIterator[Store]:
    """Yield an isolated Store using the shared PGlite engine."""
    await reset_schema(pglite_engine)
    store = Store(pglite_engine, embed=StubEmbedder())
    await store.bootstrap()
    yield store


async def _database_now(store: Store) -> datetime:
    """Read the clock and timestamp precision that the database uses."""
    async with store.engine.acquire() as conn:
        now: object = await conn.fetchval("SELECT clock_timestamp()")
    assert isinstance(now, datetime)
    return now


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("batch", [False, True])
async def test_backfill_reaches_every_kind_through_single_and_batch_submit(
    store: Store,
    batch: bool,
) -> None:
    models: tuple[type[SubmitBase], ...] = (
        SubmitIssue,
        SubmitArtifact,
        SubmitExperiment,
        SubmitPaper,
        SubmitBelief,
        SubmitCodeChange,
        SubmitWebResult,
        SubmitWebSearch,
        SubmitAgentSession,
    )
    instants = [datetime(2020, 1, i + 1, tzinfo=UTC) for i in range(len(models))]
    items = [
        model.model_validate(
            {
                "title": model.__name__,
                "account": "tester@example.com",
                "created": instant,
                "idempotency_key": new_uuid(),
                "reason": "" if index % 2 == 0 else "  uploaded history\noriginal  ",
            },
        )
        for index, (model, instant) in enumerate(zip(models, instants, strict=True))
    ]
    before = await _database_now(store)
    if batch:
        request = SubmitBatch.model_validate({"items": items})
        ids = await store.submit_batch(request.items, session_audience="private")
    else:
        ids = [
            await store.submit(
                item,
                api_key_id=None,
                actor="importer",
                session_audience="private",
            )
            for item in items
        ]
    after = await _database_now(store)
    for index, (row_id, instant) in enumerate(zip(ids, instants, strict=True)):
        row = await store.get_inquiry(row_id)
        assert row is not None
        assert row.created == instant
        assert before <= row.modified <= after
        changes = await store.list_changes(subject_id=row_id)
        assert len(changes) == 1
        assert changes[0].kind == "created"
        assert before <= changes[0].created <= after
        expected = f"backfill: created={instant.isoformat()}"
        if index % 2:
            expected += ";   uploaded history\noriginal  "
        assert changes[0].reason == expected


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("reason", ["", "  regular creation\ncontext  "])
async def test_omitted_and_null_created_use_current_server_defaults(
    store: Store,
    reason: str,
) -> None:
    before = await _database_now(store)
    ids = [
        await store.submit_issue(
            SubmitIssue.model_validate(
                {"account": "tester@example.com", "title": "new", "reason": reason},
            ),
        ),
        await store.submit_issue(
            SubmitIssue.model_validate(
                {
                    "account": "tester@example.com",
                    "title": "null",
                    "created": None,
                    "reason": reason,
                },
            ),
        ),
    ]
    after = await _database_now(store)
    for row_id in ids:
        row = await store.get_inquiry(row_id)
        assert row is not None
        assert before <= row.created <= row.modified <= after
        changes = await store.list_changes(subject_id=row_id)
        assert len(changes) == 1
        assert changes[0].reason == ""


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("initial_backfill", [False, True])
@pytest.mark.parametrize("retry_backfill", [False, True])
async def test_backfill_survives_edits_and_retries_without_rewriting_clocks(
    store: Store,
    initial_backfill: bool,
    retry_backfill: bool,
) -> None:
    instant = datetime(2020, 1, 2, tzinfo=UTC)
    key = new_uuid()
    request = SubmitIssue.model_validate(
        {
            "account": "tester@example.com",
            "title": "imported",
            "created": instant if initial_backfill else None,
            "idempotency_key": key,
            "reason": "original explanation",
        },
    )
    row_id = await store.submit_issue(request)
    original = await store.get_inquiry(row_id)
    assert original is not None
    if initial_backfill:
        assert original.created == instant
    creation_change = await store.get_change(key)
    assert creation_change is not None
    assert creation_change.reason == (
        f"backfill: created={instant.isoformat()}; original explanation"
        if initial_backfill
        else ""
    )

    retry = request.model_copy(
        update={
            "created": instant - timedelta(days=1) if retry_backfill else None,
            "reason": "replacement explanation",
            "title": "replacement title",
        },
    )
    assert await store.submit_issue(retry) == row_id
    assert await store.get_inquiry(row_id) == original

    change_id = await store.set_description(row_id, "edited", actor="editor")
    edited = await store.get_inquiry(row_id)
    assert edited is not None
    assert edited.created == original.created
    assert edited.modified > original.modified
    assert change_id is not None
    edit_change = await store.get_change(change_id)
    assert edit_change is not None
    assert edit_change.created >= edited.modified

    assert await store.submit_batch([retry]) == [row_id]
    assert await store.get_inquiry(row_id) == edited
    assert len(await store.list_changes(subject_id=row_id)) == 2
    assert await store.get_change(key) == creation_change


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_backfilled_created_has_no_field_mutation_route(store: Store) -> None:
    row_id = await store.submit_issue(
        SubmitIssue.model_validate(
            {
                "account": "tester@example.com",
                "title": "imported",
                "created": "2020-01-02T03:04:05Z",
            },
        ),
    )
    original = await store.get_inquiry(row_id)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        for method in ("PUT", "PATCH", "DELETE"):
            response = await client.request(
                method,
                f"/api/inquiries/{row_id}/created",
                json={"value": "2019-01-02T03:04:05Z"},
            )
            assert response.status_code == 405, response.text
    assert await store.get_inquiry(row_id) == original


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_backfill_dates_drive_provenance_instead_of_import_order(
    store: Store,
) -> None:
    items: list[SubmitItem] = [
        SubmitIssue.model_validate(
            {
                "account": "tester@example.com",
                "title": title,
                "created": created,
                "idempotency_key": new_uuid(),
            },
        )
        for title, created in (
            ("younger", "2021-01-01T00:00:00Z"),
            ("older imported later", "2020-01-01T00:00:00Z"),
        )
    ]
    request = SubmitBatch(
        items=items,
        edges=[BatchEdge(from_index=0, to_index=1, edge_kind="requires")],
    )
    younger_id, older_id = await store.submit_batch(request.items, edges=request.edges)
    younger = await store.get_inquiry(younger_id)
    older = await store.get_inquiry(older_id)
    assert younger is not None
    assert older is not None
    assert [edge.id for edge in younger.produced_by] == [older_id]
    assert [edge.id for edge in older.produces] == [younger_id]
    assert older.produced_by == ()


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_backfill_arrives_after_the_live_change_cursor(store: Store) -> None:
    cursor = await _database_now(store)
    key = new_uuid()
    historical = datetime(2020, 1, 2, tzinfo=UTC)
    row_id = await store.submit_issue(
        SubmitIssue(
            account="tester@example.com",
            title="historical record arriving now",
            created=historical,
            subscribers=["observer"],
            idempotency_key=key,
        ),
    )

    personal = await store.what_changed_for_me("observer", cursor)
    broadcast = await store.what_changed_for_anyone(cursor)
    recent = await store.list_changes(since=cursor)

    assert [change.id for change in personal] == [key]
    assert [(change.id, seq) for change, seq in broadcast] == [(key, 1)]
    assert [change.id for change in recent] == [key]
    assert personal[0].subject_id == row_id
    assert personal[0].created >= cursor
    assert (
        await store.what_changed_for_me(
            "observer",
            personal[0].created,
            after_id=key,
        )
        == []
    )
    row = await store.get_inquiry(row_id)
    assert row is not None
    assert row.created == historical
    assert row.modified >= cursor


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
