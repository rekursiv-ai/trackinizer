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
from datetime import datetime
from typing import cast

import uuid

import pytest
import pytest_asyncio

from trackinizer.lib.postgres import PGliteEngine
from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.store import read
from trackinizer.server.store.core import Store, StubEmbedder
from trackinizer.server.values import vetted_sql
from trackinizer.types.cost import Cost
from trackinizer.types.errors import ValidationError
from trackinizer.types.inquiries import Inquiry
from trackinizer.wire.bodies import (
    SubmitBelief,
    SubmitCodeChange,
    SubmitExperiment,
    SubmitIssue,
    SubmitPaper,
)
from trackinizer.wire.filters import (
    FILTER_OPS,
    Filter,
    FilterOp,
    validate_regex_dialect,
)


@pytest_asyncio.fixture(loop_scope="session")
async def store(pglite_engine: PGliteEngine) -> AsyncIterator[Store]:
    """A bootstrapped Store over the session's shared PGlite engine."""
    await reset_schema(pglite_engine)
    store = Store(pglite_engine, embed=StubEmbedder())
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
    return await store.list_kind("Issue", filters=filters, limit=limit, lowering=False)


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
    ("isnull", "issue_priority", ""),
    ("notnull", "issue_priority", ""),
    ("lt", "issue_priority", "20"),
    ("le", "issue_priority", "20"),
    ("gt", "issue_priority", "5"),
    ("ge", "issue_priority", "5"),
    ("re", "title", "^a"),
    ("re", "title", "[0-9]+"),
    ("nre", "title", "^a"),
    # A negated regex against a NULLABLE column. ``match_filter`` treats NULL
    # as absent and KEEPS the row (``nre`` is the complement of ``re``), while
    # SQL ``NULL !~ 'Dan'`` is NULL, which the WHERE drops. Every other case
    # here filters a NOT-NULL column, so the disagreement had no witness.
    ("nre", "owner", "Dan"),
    ("nre", "owner", "Josh"),
    # A negated regex against a NULL ARRAY. The array templates take a
    # different NULL route than the scalars -- ``NOT EXISTS (unnest(NULL))``
    # rather than an ``IS NULL`` disjunct -- so the agreement is a separate
    # fact, and was untested.
    ("nre", "labels", "absent"),
    ("nre", "labels", "bug"),
    ("re", "account", "rekursiv"),
    # The escape classes, not a sample of them. Postgres runs POSIX ARE and
    # ``match_filter`` runs Python ``re``; the two disagree on exactly the
    # escapes below, and every one of them reaches here as caller input:
    #
    #   \y \Y  word boundary in POSIX, a bad escape in Python -- translated
    #          by ``row_filter._POSIX_TRANSLATIONS``.
    #   \m \M  start/end of word in POSIX, likewise a bad escape in Python.
    #   \b     BACKSPACE in POSIX, WORD BOUNDARY in Python. The only escape
    #          both engines accept while MEANING different things, so it is
    #          the one that returns different rows instead of raising.
    #
    # Verified against a live engine: ``'foo bar' ~ '\ybar'`` is true and
    # ``'foo bar' ~ '\bbar'`` is FALSE, while Python answers true to both.
    ("re", "title", r"\yalpha"),
    ("re", "title", r"\malpha"),
    ("re", "title", r"alpha\M"),
    # An order op on a TEXT column is absent by construction: the two
    # evaluators disagree, so it is refused rather than run. See
    # ``test_an_order_op_on_text_is_refused_by_both_paths``.
    #
    # A UUID renders identically in both evaluators, so it lowers via ::text.
    # A presence op on ``id`` is absent by construction: the column is NOT
    # NULL, so ``notnull`` matches every row and ``isnull`` none, whatever the
    # data -- refused now rather than answered. See
    # ``test_a_presence_op_on_a_not_null_column_is_refused``.
    ("re", "id", "-"),
    # A timestamp: SQL must reproduce ``str(datetime)`` EXACTLY -- UTC, a
    # ``+00:00`` offset, microseconds only when non-zero. A bare ``::text``
    # renders in the session zone as ``-08``, so every equality and regex
    # filter on a time column would silently miss.
    ("re", "created", r"^20\d\d-"),
    ("re", "created", r"\+00:00$"),
    ("gt", "created", "2000-01-01 00:00:00+00:00"),
    ("lt", "created", "2099-01-01 00:00:00+00:00"),
    # A flattened cost axis, whose spec carries the COMPOSITE's empty
    # sql_type -- the Python annotation is the only evidence it is numeric.
    ("ge", "marginal_cost_agent_usd", "0"),
    ("gt", "marginal_cost_agent_usd", "-1"),
    # Equality and regex on a NUMBER compare RENDERINGS, and the two
    # evaluators render differently unless the SQL is told not to: Python's
    # ``str(0.5)`` is ``0.5`` where a NUMERIC column's ``::text`` is
    # ``0.500000`` and a float's ``str(100.0)`` is ``100.0`` against SQL's
    # ``100``.
    ("is", "marginal_cost_agent_usd", "0.5"),
    ("is", "marginal_cost_agent_usd", "0"),
    ("ne", "marginal_cost_agent_usd", "0.5"),
    # A WHOLE number through the REAL renderer: the ``bigint::text || '.0'``
    # branch must produce ``0.0`` for ``ne`` exactly as it does for ``is``,
    # or the two ops disagree about the same stored value.
    ("ne", "marginal_cost_agent_usd", "0"),
    ("ne", "marginal_cost_agent_usd", "0.0"),
    ("re", "marginal_cost_agent_usd", r"^0\.5$"),
    ("is", "issue_priority", "20"),
    ("re", "issue_priority", "^2"),
    # A float column, to catch a classifier that only knows integers.
    ("notnull", "belief_confidence", ""),
    # An operand with more digits than a float can hold. ``numeric`` keeps
    # them and ``float`` does not, so a guard that parsed the operand with
    # ``float()`` answered the OPPOSITE of its own SQL: live PG16 says
    # ``1 < '1.00000000000000001'::numeric`` is true, and the rounded operand
    # said false. Both engines answer, so nothing else catches it.
    ("lt", "seq", "1.00000000000000001"),
    ("gt", "seq", "0.99999999999999999"),
    ("le", "seq", "2.00000000000000001"),
    ("ge", "seq", "1.00000000000000001"),
    ("lt", "marginal_cost_agent_usd", "0.50000000000000001"),
    ("ge", "marginal_cost_agent_usd", "0.50000000000000001"),
)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
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
@pytest.mark.asyncio(loop_scope="session")
async def test_a_uuid_array_column_filters_against_a_real_engine(
    store: Store,
) -> None:
    """``experiment_codechanges`` is ``UUID[]``, not ``TEXT[]``.

    The array templates were written for the five ``TEXT[]`` columns and only
    ever exercised against those, so nothing caught that the same SQL is
    invalid for a uuid element: a live engine answers ``operator does not
    exist: uuid ~ unknown`` for the regex form and ``could not convert type
    text[] to uuid[]`` for the negated one. A mock store cannot see either.
    """
    change_id = await store.submit_codechange(
        SubmitCodeChange(title="commit", account="josh@rekursiv.ai", sha="a" * 40)
    )
    await store.submit_experiment(
        SubmitExperiment(
            title="exp", account="josh@rekursiv.ai", codechanges=[change_id]
        )
    )

    cases: tuple[tuple[FilterOp, str, int], ...] = (
        ("is", str(change_id), 1),
        ("re", str(change_id)[:8], 1),
        ("nre", "no-such-prefix", 1),
        ("ne", str(uuid.uuid4()), 1),
    )
    for op, value, expected in cases:
        rows = await store.list_kind(
            "Experiment",
            filters=(Filter(field="experiment_codechanges", op=op, value=value),),
        )
        assert len(rows) == expected, f"{op} {value}"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    ("op", "value"), [("is", "-0.0"), ("is", "0.0"), ("ne", "-0.0"), ("re", r"^-0\.0$")]
)
async def test_negative_zero_renders_alike_in_both_evaluators(
    store: Store, op: FilterOp, value: str
) -> None:
    r"""``-0.0`` is a value the column accepts and the renderings disagreed on.

    The REAL renderer's whole-number branch goes through ``::bigint``, which
    has no negative zero, so SQL rendered ``0.0`` where ``str(-0.0)`` is
    ``-0.0``. ``belief_confidence`` is DOUBLE PRECISION under a ``CHECK (0..1)``
    that ``-0.0`` passes (live PG16: ``-0.0 >= 0 AND -0.0 <= 1`` is true), so a
    caller can store one.

    The harm is not on the odd operand. Filtering for a plain ``0.0`` MATCHED
    the ``-0.0`` row in SQL and missed it in Python -- a caller who never types
    a minus sign gets a different row set depending on which evaluator ran.
    """
    await store.submit_belief(
        SubmitBelief(account="a@b.c", title="negzero", confidence=-0.0)
    )
    await store.submit_belief(
        SubmitBelief(account="a@b.c", title="poszero", confidence=0.0)
    )
    filters = (Filter(field="belief_confidence", op=op, value=value),)

    lowered = await store.list_kind("Belief", filters=filters, limit=50)
    in_python = await store.list_kind(
        "Belief", filters=filters, limit=50, lowering=False
    )

    assert [row.seq for row in lowered] == [row.seq for row in in_python]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    ("op", "value"),
    [
        ("is", "0001-01-01 00:00:00+00:00"),
        ("ne", "0001-01-01 00:00:00+00:00"),
        ("re", "^0001"),
        ("nre", "no-such-text"),
        ("lt", "2026-01-01 00:00:00+00:00"),
        ("gt", "2026-01-01 00:00:00+00:00"),
    ],
)
async def test_an_extreme_date_renders_alike_in_both_evaluators(
    store: Store, op: FilterOp, value: str
) -> None:
    """``datetime.min`` stores as Postgres ``-infinity``, which renders NULL.

    asyncpg encodes ``datetime(1, 1, 1)`` as ``-infinity``, and ``to_char`` and
    ``date_part('microsecond', ...)`` both answer NULL on it -- so the whole
    ``_TS_TEXT`` concatenation is NULL, every comparison against it is NULL,
    and the WHERE drops the row. Python holds the datetime and stringifies it
    normally, so it keeps the row.

    ``nre`` is the sharpest case: a row invisible to every affirmative
    timestamp filter is dropped by the NEGATED one too, making it unreachable
    through SQL while Python returns it.
    """
    await store.submit_paper(
        SubmitPaper(
            account="a@b.c",
            title="ancient",
            publish_date=datetime.fromisoformat("0001-01-01T00:00:00+00:00"),
        )
    )
    await store.submit_paper(
        SubmitPaper(
            account="a@b.c",
            title="modern",
            publish_date=datetime.fromisoformat("2026-01-01T00:00:00+00:00"),
        )
    )
    filters = (Filter(field="paper_publish_date", op=op, value=value),)

    lowered = await store.list_kind("Paper", filters=filters, limit=50)
    in_python = await store.list_kind(
        "Paper", filters=filters, limit=50, lowering=False
    )

    assert [row.seq for row in lowered] == [row.seq for row in in_python]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_order_op_on_text_is_refused_by_both_paths(store: Store) -> None:
    """Neither evaluator may order text, because they order it differently.

    Python compares ``"10"`` and ``"9"`` as NUMBERS whenever both parse; SQL
    compares them lexically, so ``'10' < '9'`` is true in SQL and false in
    Python. The pair therefore cannot appear in ``CASES`` -- there is no
    agreed answer to compare against -- and the refusal is what this asserts.
    """
    await seed(store)
    filters = (Filter(field="title", op="lt", value="9"),)

    with pytest.raises(ValidationError, match="title"):
        await store.list_kind("Issue", filters=filters, limit=50)
    with pytest.raises(ValidationError, match="title"):
        await rows_via_python(store, filters)


@pytest.mark.parametrize(
    "column", ["id", "created", "marginal_cost_agent_usd", "seq", "account"]
)
def test_a_presence_op_on_a_not_null_column_is_refused(column: str) -> None:
    """``isnull`` on a NOT-NULL column has one answer before any row is read.

    It selects nothing and ``notnull`` selects everything, so neither is a
    filter -- and the two evaluators agreeing on a meaningless answer is not a
    reason to run it. These pairs therefore cannot appear in ``CASES``.

    No engine is needed: the refusal is on the wire type, so the clause never
    reaches a query. That is the point -- it used to be refused only by the
    HTTP route, leaving the store and the CLI to run it.
    """
    for op in ("isnull", "notnull"):
        with pytest.raises(ValueError, match="NOT NULL"):
            Filter(field=column, op=cast(FilterOp, op), value="")


def test_ambiguous_escape_is_refused_rather_than_translated() -> None:
    r"""``\b`` must be rejected, because no translation of it is correct.

    It is the one escape both engines accept while meaning different things --
    BACKSPACE in POSIX, a word boundary in Python -- so a filter carrying it
    selects different rows depending on whether the clause lowered. Every
    other divergent escape is translated; this one cannot be, so the wire
    layer refuses it and names the spelling that works.
    """
    message = validate_regex_dialect(r"\balpha")
    assert message is not None
    assert r"\y" in message
    assert validate_regex_dialect(r"\yalpha") is None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_every_op_is_covered_by_a_case() -> None:
    """A new ``FilterOp`` must arrive with an equivalence case.

    Without this the set can grow and the new op silently lowers -- or
    silently does not -- with nothing comparing the two evaluators.
    """
    assert {op for op, _field, _value in CASES} == set(FILTER_OPS)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
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
@pytest.mark.asyncio(loop_scope="session")
async def test_text_array_membership_can_use_the_gin_index(store: Store) -> None:
    """The lowered labels predicate must remain eligible for its GIN index."""
    params: list[object] = []
    clause = read._lower_filter(Filter(field="labels", op="is", value="needle"), params)
    assert clause is not None

    async with store.engine.acquire() as conn:
        await conn.execute("CREATE TEMP TABLE filter_probe (labels TEXT[])")
        await conn.execute(
            "CREATE INDEX filter_probe_labels_gin ON filter_probe USING gin(labels)"
        )
        await conn.execute("INSERT INTO filter_probe VALUES (ARRAY['needle']::text[])")
        await conn.execute("SET enable_seqscan = off")
        rows = await conn.fetch(
            vetted_sql(
                "EXPLAIN (COSTS OFF) SELECT * FROM filter_probe WHERE ",
                clause,
            ),
            *params,
        )

    plan = "\n".join(str(row[0]) for row in rows)
    assert "filter_probe_labels_gin" in plan


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_uuid_array_membership_preserves_parameter_typing(store: Store) -> None:
    """Optimizing text arrays must not cast UUID-array operands to text."""
    target = uuid.uuid4()
    params: list[object] = []
    clause = read._lower_filter(
        Filter(field="experiment_codechanges", op="is", value=str(target)),
        params,
    )
    assert clause is not None

    async with store.engine.acquire() as conn:
        await conn.execute(
            "CREATE TEMP TABLE filter_probe (experiment_codechanges UUID[])"
        )
        await conn.execute(
            "INSERT INTO filter_probe VALUES (ARRAY[$1]::uuid[])",
            target,
        )
        count = await conn.fetchval(
            vetted_sql("SELECT count(*) FROM filter_probe WHERE ", clause),
            *params,
        )

    assert count == 1


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("offset", [0, 1, 2])
async def test_paging_agrees_between_the_two_paths(store: Store, offset: int) -> None:
    """Python slices the kept rows; SQL uses OFFSET. They must land alike."""
    await seed(store)
    filters = (Filter(field="account", op="is", value="josh@rekursiv.ai"),)

    lowered = await store.list_kind("Issue", filters=filters, limit=2, offset=offset)
    in_python = await store.list_kind(
        "Issue", filters=filters, limit=2, offset=offset, lowering=False
    )

    assert [row.seq for row in lowered] == [row.seq for row in in_python]


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
