"""A backfilled row keeps its own chronology without lying about the audit.

Against a real PGlite engine rather than the mock connection: what ``recorded``
promises is about what the database stores, what it still stamps by itself, and
whether a range query over the column answers -- none of which a mock can stand
in for. The range query is the point of the column: an ``orig-date:`` label is
lossless too, and cannot be asked "which entries fall in this fortnight".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest
import pytest_asyncio

from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.store.core import Store
from trackinizer.wire.bodies import SubmitIssue
from trackinizer.wire.filters import Filter


if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from uuid import UUID

    from trackinizer.lib.postgres import PGliteEngine


_WORKLOG_START = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
"""The oldest entry in the corpus being backfilled, weeks before the import."""


@pytest_asyncio.fixture(loop_scope="session")
async def store(pglite_engine: PGliteEngine) -> AsyncIterator[Store]:
    """Return a bootstrapped Store over the session's shared PGlite engine."""
    await reset_schema(pglite_engine)
    built = Store(pglite_engine, embed=StubEmbedder())
    await built.bootstrap()
    yield built


async def _backfill(store: Store) -> dict[str, UUID]:
    """Three worklog entries written weeks apart, imported in one batch."""
    return {
        name: await store.submit_issue(
            SubmitIssue(
                account="importer@example.com",
                title=f"Worklog: {name}",
                recorded=_WORKLOG_START + timedelta(days=days),
            ),
        )
        for name, days in (("first", 0), ("second", 9), ("third", 23))
    }


async def _column(store: Store, row_id: UUID, column: str) -> object:
    """Read one column straight from the row, past any wire type."""
    async with store.engine.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {column} FROM inquiries WHERE id = $1",  # noqa: S608
            row_id,
        )
    assert row is not None
    return row[column]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_submitted_recorded_is_stored_as_given(store: Store) -> None:
    """The client's declared time survives the round trip, to the second."""
    ids = await _backfill(store)

    assert await _column(store, ids["first"], "recorded") == _WORKLOG_START


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_unset_recorded_stays_null_rather_than_stamping_now(
    store: Store,
) -> None:
    """A row born here declares no origin; ``created`` already answers that."""
    live = await store.submit_issue(
        SubmitIssue(account="tester@example.com", title="Asked just now"),
    )

    assert await _column(store, live, "recorded") is None
    assert await _column(store, live, "created") is not None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_backfill_does_not_move_the_server_stamped_times(
    store: Store,
) -> None:
    """The audit stays honest: these rows really did arrive today, and say so."""
    ids = await _backfill(store)

    created = cast(datetime, await _column(store, ids["first"], "created"))
    # A minute of slack, not an ordering against a clock read just before the
    # write: the stored value is truncated to milliseconds, so an exact
    # ``created >= before`` loses to its own rounding.
    assert abs(created - datetime.now(UTC)) < timedelta(minutes=1)
    # Weeks apart in the corpus, moments apart in the database.
    assert created - cast(datetime, await _column(store, ids["first"], "recorded")) > (
        timedelta(days=1)
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_corpus_sorts_by_its_own_clock_not_the_import(
    store: Store,
) -> None:
    """The gap the request names: ``created`` cannot order a backfilled corpus.

    Every row was imported in one batch, so ``created`` ranks them by the
    order the importer happened to walk its files. ``recorded`` ranks them the
    way the worklog was written.
    """
    ids = await _backfill(store)
    async with store.engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id FROM inquiries WHERE recorded IS NOT NULL ORDER BY recorded",
        )

    assert [row["id"] for row in rows] == [ids["first"], ids["second"], ids["third"]]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_range_query_answers_over_the_declared_time(store: Store) -> None:
    """What a label could never do: ask the corpus for one fortnight."""
    ids = await _backfill(store)
    cutoff = (_WORKLOG_START + timedelta(days=14)).isoformat()

    found = await store.list_kind(
        "Issue",
        filters=(Filter(field="recorded", op="lt", value=cutoff),),
    )

    assert {row.id for row in found} == {ids["first"], ids["second"]}


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_correcting_a_wrong_backfill_date_is_an_audited_edit(
    store: Store,
) -> None:
    """Editable, not set-once: a mis-dated batch is fixed without supersession."""
    ids = await _backfill(store)
    corrected = _WORKLOG_START - timedelta(days=30)

    change_id = await store.set_recorded(ids["first"], corrected, actor="importer")

    assert change_id is not None
    assert await _column(store, ids["first"], "recorded") == corrected
    async with store.engine.acquire() as conn:
        change = await conn.fetchrow(
            "SELECT kind, old_recorded, new_recorded FROM change_log WHERE id = $1",
            change_id,
        )
    assert change is not None
    assert change["kind"] == "recorded"
    assert change["old_recorded"] == _WORKLOG_START
    assert change["new_recorded"] == corrected


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_clearing_recorded_is_allowed_and_audited(store: Store) -> None:
    """A row wrongly marked as backfilled can say it was born here after all."""
    ids = await _backfill(store)

    change_id = await store.set_recorded(ids["second"], None, actor="importer")

    assert change_id is not None
    assert await _column(store, ids["second"], "recorded") is None


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
