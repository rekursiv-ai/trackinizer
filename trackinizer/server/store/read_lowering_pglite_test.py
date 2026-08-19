"""Every filter op must select the same rows in SQL as it does in Python.

``list_kind`` lowers what it can into the ``WHERE`` clause so the query keeps
``LIMIT`` instead of materializing the whole kind. That is only admissible
while the SQL and the in-process :func:`~wire.row_filter.match_filter` agree,
and the awkward values are exactly the ones a hand-written test forgets:
NULLs, empty strings, numeric-looking text, empty arrays.

This drives every op through both evaluators against a real substrate. A new
op, or a change to either evaluator, fails here rather than silently
returning different rows in production than in the CLI's test fake.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest
import pytest_asyncio

from trackinizer.lib.postgres import PGliteEngine
from trackinizer.server.store import read
from trackinizer.server.store.core import Store, StubEmbedder
from trackinizer.types.cost import Cost
from trackinizer.types.inquiries import Inquiry
from trackinizer.wire.bodies import SubmitIssue
from trackinizer.wire.filters import FILTER_OPS, Filter, FilterOp


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
    """Rows spanning the values that break naive lowerings.

    Numeric-looking titles catch an order op comparing text lexically where
    Python compared numerically; a NULL owner catches three-valued logic; an
    empty label set catches array membership against ``{}``.
    """
    rows = [
        ("josh@rekursiv.ai", "Josh", "10", ("bug", "ml")),
        ("josh@rekursiv.ai", "Agent", "9", ("bug",)),
        ("other@rekursiv.ai", "Josh", "alpha", ()),
        ("josh@rekursiv.ai", None, "Alpha", ("ml",)),
        ("other@rekursiv.ai", None, "", ()),
    ]
    for account, owner, title, labels in rows:
        await store.submit_issue(
            SubmitIssue(
                account=account,
                owner=owner,
                title=title or "untitled",
                labels=list(labels),
            )
        )
    # Give one row a FRACTIONAL cost. Python renders ``0.5``; a NUMERIC
    # column's ``::text`` renders ``0.500000`` and a float's drops the
    # trailing ``.0``, so an equality or regex filter on a number diverges
    # unless the SQL reproduces Python's rendering. Every row keeps the
    # default 0 without this, which no rendering difference can expose.
    first = (await store.list_kind("Issue", limit=1))[0]
    await store.add_cost(first.id, Cost(agent_usd=0.5), actor="tester")


async def rows_via_python(
    store: Store, filters: Sequence[Filter], *, limit: int = 50
) -> list[Inquiry]:
    """Run the query with lowering disabled, forcing the Python predicate."""
    original = read.LOWERING
    read.LOWERING = read.Lowering(enabled=False)
    try:
        return await store.list_kind("Issue", filters=filters, limit=limit)
    finally:
        read.LOWERING = original


# One filter per op, aimed at the value that most often diverges.
CASES: tuple[tuple[FilterOp, str, str], ...] = (
    ("is", "account", "josh@rekursiv.ai"),
    ("is", "owner", "Josh"),
    ("is", "owner", "None"),
    ("is", "labels", "bug"),
    ("is", "labels", "absent"),
    ("is", "title", "10"),
    ("ne", "account", "josh@rekursiv.ai"),
    ("ne", "owner", "Josh"),
    ("ne", "labels", "bug"),
    ("isnull", "owner", ""),
    ("notnull", "owner", ""),
    ("lt", "issue_priority", "20"),
    ("le", "issue_priority", "20"),
    ("gt", "issue_priority", "5"),
    ("ge", "issue_priority", "5"),
    ("re", "title", "^a"),
    ("re", "title", "[0-9]+"),
    ("nre", "title", "^a"),
    ("re", "account", "rekursiv"),
    # An order op on a TEXT column. Python compares "10" and "9" as NUMBERS
    # whenever both parse; SQL compares them lexically, so '10' < '9' is true
    # in SQL and false in Python. Lowering these would silently change the
    # result set, which is why the order ops are numeric-only.
    ("lt", "title", "9"),
    ("gt", "title", "9"),
    # A UUID renders identically in both evaluators, so it lowers via ::text.
    ("notnull", "id", ""),
    ("re", "id", "-"),
    # A timestamp: SQL must reproduce ``str(datetime)`` EXACTLY -- UTC, a
    # ``+00:00`` offset, microseconds only when non-zero. A bare ``::text``
    # renders in the session zone as ``-08``, so every equality and regex
    # filter on a time column would silently miss.
    ("notnull", "created", ""),
    ("re", "created", r"^20\d\d-"),
    ("re", "created", r"\+00:00$"),
    ("gt", "created", "2000-01-01 00:00:00+00:00"),
    ("lt", "created", "2099-01-01 00:00:00+00:00"),
    # A flattened cost axis, whose spec carries the COMPOSITE's empty
    # sql_type -- the Python annotation is the only evidence it is numeric.
    ("ge", "marginal_cost_agent_usd", "0"),
    ("gt", "marginal_cost_agent_usd", "-1"),
    ("isnull", "marginal_cost_agent_usd", ""),
    # Equality and regex on a NUMBER compare RENDERINGS, and the two
    # evaluators render differently unless the SQL is told not to: Python's
    # ``str(0.5)`` is ``0.5`` where a NUMERIC column's ``::text`` is
    # ``0.500000`` and a float's ``str(100.0)`` is ``100.0`` against SQL's
    # ``100``.
    ("is", "marginal_cost_agent_usd", "0.5"),
    ("is", "marginal_cost_agent_usd", "0"),
    ("ne", "marginal_cost_agent_usd", "0.5"),
    ("re", "marginal_cost_agent_usd", r"^0\.5$"),
    ("is", "issue_priority", "20"),
    ("re", "issue_priority", "^2"),
    # A float column, to catch a classifier that only knows integers.
    ("notnull", "belief_confidence", ""),
)


@pytest.mark.db_pglite
@pytest.mark.asyncio
@pytest.mark.parametrize(("op", "field", "value"), CASES)
async def test_sql_and_python_select_the_same_rows(
    store: Store, op: FilterOp, field: str, value: str
) -> None:
    await seed(store)
    filters = (Filter(field=field, op=op, value=value),)

    lowered = await store.list_kind("Issue", filters=filters, limit=50)
    in_python = await rows_via_python(store, filters)

    assert [row.seq for row in lowered] == [row.seq for row in in_python], (
        f"{field} {op} {value!r} selected different rows in SQL than in Python"
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio
async def test_every_op_is_covered_by_a_case() -> None:
    """A new ``FilterOp`` must arrive with an equivalence case.

    Without this the set can grow and the new op silently lowers -- or
    silently does not -- with nothing comparing the two evaluators.
    """
    assert {op for op, _field, _value in CASES} == set(FILTER_OPS)


@pytest.mark.db_pglite
@pytest.mark.asyncio
async def test_a_fully_lowered_query_keeps_limit_in_sql(store: Store) -> None:
    """The point of lowering: window in the query, not after the fetch."""
    await seed(store)

    windowed = await store.list_kind(
        "Issue",
        filters=(Filter(field="account", op="is", value="josh@rekursiv.ai"),),
        limit=2,
    )

    assert len(windowed) == 2


@pytest.mark.db_pglite
@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [0, 1, 2])
async def test_paging_agrees_between_the_two_paths(store: Store, offset: int) -> None:
    """Python slices the kept rows; SQL uses OFFSET. They must land alike."""
    await seed(store)
    filters = (Filter(field="account", op="is", value="josh@rekursiv.ai"),)

    lowered = await store.list_kind("Issue", filters=filters, limit=2, offset=offset)
    read_original = read.LOWERING
    read.LOWERING = read.Lowering(enabled=False)
    try:
        in_python = await store.list_kind(
            "Issue", filters=filters, limit=2, offset=offset
        )
    finally:
        read.LOWERING = read_original

    assert [row.seq for row in lowered] == [row.seq for row in in_python]


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
