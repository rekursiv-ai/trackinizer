"""Filter-lowering equivalence against a real Postgres substrate.

``list_kind`` evaluates a filter one of two ways: pushed into the SQL
``WHERE`` clause, or applied in Python over every materialized row. The
first is far cheaper -- it keeps ``LIMIT`` in the query instead of fetching
the whole kind -- but it is only admissible while the two produce the SAME
rows. Postgres equality and Python's ``str(value) == operand`` agree for a
scalar text column and disagree for arrays, numbers, and NULLs, so this
pins the equivalence against a live database rather than a mock.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest
import pytest_asyncio

from trackinizer.lib.postgres import PGliteEngine
from trackinizer.server.store import read
from trackinizer.server.store.core import Store, StubEmbedder
from trackinizer.types.inquiries import Inquiry
from trackinizer.wire.bodies import SubmitIssue
from trackinizer.wire.filters import Filter


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    """A bootstrapped Store over an ephemeral in-process PGlite engine."""
    async with PGliteEngine(
        workdir=tmp_path / "pglite", persist=False, extensions=("pgvector",)
    ) as engine:
        store = Store(engine, embed=StubEmbedder())
        await store.bootstrap()
        yield store


async def seed(store: Store) -> None:
    """A population where each filter under test excludes something."""
    for index, (account, owner, title) in enumerate(
        [
            ("josh@rekursiv.ai", "Josh", "first"),
            ("josh@rekursiv.ai", "Agent", "second"),
            ("other@rekursiv.ai", "Josh", "third"),
            ("josh@rekursiv.ai", None, "fourth"),
            ("other@rekursiv.ai", None, "fifth"),
        ]
    ):
        del index
        await store.submit_issue(SubmitIssue(account=account, owner=owner, title=title))


async def rows_via_python(
    store: Store, filters: Sequence[Filter], *, limit: int, offset: int = 0
) -> list[Inquiry]:
    """Force the post-filter path by disabling the lowering."""
    original = read._LOWERABLE_EQUALITY_COLUMNS
    read._LOWERABLE_EQUALITY_COLUMNS = frozenset()
    try:
        return await store.list_kind(
            "Issue", filters=filters, limit=limit, offset=offset
        )
    finally:
        read._LOWERABLE_EQUALITY_COLUMNS = original


@pytest.mark.db_pglite
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filters",
    [
        pytest.param(
            (Filter(field="account", op="is", value="josh@rekursiv.ai"),),
            id="account",
        ),
        pytest.param((Filter(field="owner", op="is", value="Josh"),), id="owner"),
        pytest.param((Filter(field="status", op="is", value="active"),), id="status"),
        pytest.param((Filter(field="title", op="is", value="third"),), id="title"),
        pytest.param(
            (
                Filter(field="account", op="is", value="josh@rekursiv.ai"),
                Filter(field="owner", op="is", value="Josh"),
            ),
            id="two-lowered",
        ),
        pytest.param(
            (Filter(field="account", op="is", value="nobody@example.com"),),
            id="matches-nothing",
        ),
        pytest.param(
            (Filter(field="owner", op="is", value="None"),),
            id="null-column-vs-the-string-None",
        ),
    ],
)
async def test_lowered_and_python_filters_agree(
    store: Store, filters: Sequence[Filter]
) -> None:
    """The SQL and Python evaluators must select the same rows.

    The ``None`` case is the sharp one: three rows have a NULL ``owner``, and
    Python compares ``str(value)``. If the predicate ever reached a NULL it
    would stringify to ``"None"`` and match; ``match_filter`` returns False
    for NULL first, and SQL equality never matches NULL either -- so both
    exclude it, and this proves they still do.
    """
    await seed(store)

    lowered = await store.list_kind("Issue", filters=filters, limit=50)
    in_python = await rows_via_python(store, filters, limit=50)

    assert [row.seq for row in lowered] == [row.seq for row in in_python]


@pytest.mark.db_pglite
@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [0, 1, 2, 3])
async def test_lowered_and_python_paths_page_identically(
    store: Store, offset: int
) -> None:
    """Pagination must not shift when a filter moves into SQL.

    The two paths window differently -- Python slices the kept rows, SQL
    applies ``OFFSET`` -- so an off-by-one in either would skip or repeat a
    row midway through a pager, which no single-page test can see.
    """
    await seed(store)
    filters = (Filter(field="account", op="is", value="josh@rekursiv.ai"),)

    lowered = await store.list_kind("Issue", filters=filters, limit=2, offset=offset)
    in_python = await rows_via_python(store, filters, limit=2, offset=offset)

    assert [row.seq for row in lowered] == [row.seq for row in in_python]


@pytest.mark.db_pglite
@pytest.mark.asyncio
async def test_a_status_filter_ands_with_the_native_status_argument(
    store: Store,
) -> None:
    """``status`` is both a native parameter and a filterable column.

    Both now emit ``status = $n``, so a caller supplying each must get their
    conjunction -- empty when they disagree -- rather than a malformed query
    or a silently dropped clause.
    """
    await seed(store)

    agreeing = await store.list_kind(
        "Issue",
        status="active",
        filters=(Filter(field="status", op="is", value="active"),),
        limit=50,
    )
    disagreeing = await store.list_kind(
        "Issue",
        status="active",
        filters=(Filter(field="status", op="is", value="complete"),),
        limit=50,
    )

    assert agreeing
    assert not disagreeing


@pytest.mark.db_pglite
@pytest.mark.asyncio
async def test_a_lowered_filter_windows_after_the_predicate(store: Store) -> None:
    """``LIMIT`` may only ride along once the filter runs first.

    Pushing the window ahead of the predicate is the truncation bug the
    post-filter path exists to prevent (Issue#256): the limit must bound
    MATCHES, never the rows scanned to find them.
    """
    await seed(store)
    filters = (Filter(field="account", op="is", value="josh@rekursiv.ai"),)

    windowed = await store.list_kind("Issue", filters=filters, limit=2)

    assert len(windowed) == 2
    assert all(row.account == "josh@rekursiv.ai" for row in windowed)


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
