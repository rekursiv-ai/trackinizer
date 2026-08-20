"""Every rendering template must reproduce ``str(value)`` across its domain.

Two templates in :mod:`wire.column_shapes` do not compare a column -- they
RENDER it to text, so that ``is`` / ``ne`` / ``re`` can compare against the
same string :func:`~wire.row_filter.match_filter` builds with ``str()``. When
the two renderings disagree the engines answer differently and neither errors,
which is the one failure mode nothing downstream catches.

Four such disagreements have been found by hand, one at a time: a whole number
lost its ``.0``, ``-0.0`` lost its sign, ``±infinity`` rendered NULL, and a
16-digit value switches to scientific notation. Each took a person guessing
the right edge value. This sweeps the domain instead -- every boundary that
has historically produced a bug, plus a seeded sample of the interior -- so
the next one fails here rather than in production.

The rendering is tested in ISOLATION, against a real engine: the value goes in
through asyncpg (the production encoder), the template runs in SQL, and the
returned string is compared to ``str()`` of the value asyncpg decodes back.
The full ``list_kind`` pipeline is covered separately by
``read_lowering_pglite_test.py``; what is checked here is the one claim those
templates make.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import math

import pytest
import pytest_asyncio

from trackinizer.lib.postgres import PGliteEngine
from trackinizer.wire.column_shapes import _REAL_TEXT, _TS_TEXT


@pytest_asyncio.fixture
async def engine(tmp_path: Path) -> AsyncIterator[PGliteEngine]:
    """A bare PGlite engine; no schema needed to exercise a rendering."""
    async with PGliteEngine(
        workdir=tmp_path / "pglite", persist=False, extensions=("pgvector",)
    ) as running:
        yield running


def _spread(count: int) -> Iterator[float]:
    """``count`` points spread over ``[0, 1)``, deterministically.

    The golden-ratio low-discrepancy sequence rather than a PRNG: it needs no
    seed to be reproducible, and it fills the interval more evenly than random
    sampling at the same count, which is the whole point of the interior
    sweep. A rendering bug lives in a narrow band of the domain, so coverage
    is what finds it.
    """
    for index in range(count):
        yield (index * ((math.sqrt(5.0) - 1.0) / 2.0)) % 1.0


def _reals() -> tuple[float, ...]:
    """Boundary floats plus an evenly spread interior sample.

    The boundaries are the values that have produced a bug or sit one step
    from one: both zeros, whole numbers on each side of the ``.0`` branch, the
    subnormal floor, and the largest magnitude ``NUMERIC(14, 6)`` can hold.
    The interior sample covers the two reachable ranges -- ``[0, 1]`` for
    ``belief_confidence`` and the cost axes' full span.
    """
    boundaries = (
        -0.0,
        0.0,
        1.0,
        -1.0,
        0.1,
        0.5,
        5e-324,
        2.2250738585072014e-308,
        1e-7,
        0.000001,
        0.999999,
        100.0,
        -100.0,
        99999999.0,
        99999999.999999,
        -99999999.999999,
        0.30000000000000004,
    )
    unit = tuple(_spread(100))
    costs = tuple(
        round(point * 199_999_998.0 - 99_999_999.0, 6) for point in _spread(100)
    )
    return boundaries + unit + costs


def _timestamps() -> tuple[datetime, ...]:
    """Boundary datetimes plus an evenly spread interior sample.

    ``datetime.min`` / ``datetime.max`` are the ones that mattered: asyncpg
    encodes them as Postgres ``±infinity``, where ``to_char`` answers NULL.
    The rest bracket the microsecond branch -- exactly zero, exactly one, and
    the maximum -- since that branch is what decides whether a fractional part
    is printed at all.
    """
    boundaries = (
        datetime.min.replace(tzinfo=UTC),
        datetime.max.replace(tzinfo=UTC),
        datetime(1, 1, 1, 0, 0, 0, 1, tzinfo=UTC),
        datetime(2000, 1, 1, tzinfo=UTC),
        datetime(2026, 6, 15, 12, 30, 45, 123_456, tzinfo=UTC),
        datetime(2026, 6, 15, 12, 30, 45, tzinfo=UTC),
        datetime(2026, 6, 15, 12, 30, 45, 100_000, tzinfo=UTC),
        datetime(2026, 6, 15, 12, 30, 45, 999_999, tzinfo=UTC),
    )
    base = datetime(1970, 1, 1, tzinfo=UTC)
    # The microsecond field is swept independently of the date: it is what
    # selects the fractional branch, and a sweep that derived it from the same
    # point would correlate the two and test one axis twice.
    sample = tuple(
        base
        + timedelta(
            days=int(point * 25_000),
            seconds=int(point * 86_400) % 86_400,
            microseconds=(index * 9_901) % 1_000_000,
        )
        for index, point in enumerate(_spread(100))
    )
    return boundaries + sample


async def _rendered(
    engine: PGliteEngine, sql_type: str, template: str, values: Sequence[object]
) -> list[tuple[object, str | None]]:
    """Render every value through ``template`` on a real engine.

    The values are BOUND, not interpolated: asyncpg's binary encoder is the
    production path, and a literal would test a different parser. The decoded
    value comes back alongside its rendering so the comparison is against what
    Python would actually hold for that row -- which is the whole claim, and
    is not the same object for ``±infinity``, where asyncpg returns a naive
    datetime.
    """
    async with engine.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT val, {template.format(col='val')} AS rendered "  # noqa: S608
            f"FROM unnest($1::{sql_type}[]) AS val",
            list(values),
        )
    return [(row["val"], row["rendered"]) for row in rows]


@pytest.mark.db_pglite
@pytest.mark.asyncio
async def test_the_real_template_renders_what_python_str_renders(
    engine: PGliteEngine,
) -> None:
    """``_REAL_TEXT`` must equal ``str(float)`` for every reachable float.

    Three of its branches exist only because ``float8::text`` and
    ``str(float)`` format the same value differently -- the missing ``.0`` on
    a whole number, the dropped sign on ``-0.0``, and the magnitude guard.
    Each was found by hand after it shipped.
    """
    values = _reals()
    disagreed = [
        (value, rendered)
        for value, rendered in await _rendered(engine, "float8", _REAL_TEXT, values)
        if rendered != str(value)
    ]

    assert not disagreed, (
        f"{len(disagreed)} of {len(values)} floats render differently in SQL "
        f"than str() does, e.g. {disagreed[:3]}"
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio
async def test_the_timestamp_template_renders_what_python_str_renders(
    engine: PGliteEngine,
) -> None:
    """``_TS_TEXT`` must equal ``str(datetime)`` for every reachable instant.

    Including the two infinities, where ``to_char`` answers NULL and the whole
    concatenation collapses -- a row that then matched no timestamp filter at
    all, not even a negated one.
    """
    values = _timestamps()
    disagreed = [
        (value, rendered)
        for value, rendered in await _rendered(engine, "timestamptz", _TS_TEXT, values)
        if rendered != str(value)
    ]

    assert not disagreed, (
        f"{len(disagreed)} of {len(values)} instants render differently in SQL "
        f"than str() does, e.g. {disagreed[:3]}"
    )


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
