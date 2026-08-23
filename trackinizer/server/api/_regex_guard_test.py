r"""Tests for :func:`regex_failures_as_400`.

The guard exists so a caller's bad regex is a 400 rather than a 500. Which
exception Postgres actually raises is therefore the whole contract, and it is
not guessable from the name: an invalid pattern is SQLSTATE **2201B**
(``InvalidRegularExpressionError``, a ``DataError``), NOT the 42601
``PostgresSyntaxError`` that "syntax error" suggests. The two classes are
unrelated, so catching the wrong one is a silent no-op.

The pglite test at the bottom is the one that cannot be fooled by a wrong
belief about the class hierarchy: it drives a real invalid pattern through a
real engine.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import HTTPException

import asyncpg
import pytest
import pytest_asyncio

from trackinizer.lib.postgres import PGliteEngine
from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.api._regex_guard import regex_failures_as_400
from trackinizer.server.store.core import Store, StubEmbedder
from trackinizer.types.errors import ValidationError
from trackinizer.wire.bodies import SubmitIssue
from trackinizer.wire.filters import Filter


def _postgres_error(cls: type[asyncpg.PostgresError], message: str) -> Exception:
    """Build one asyncpg error of ``cls`` without a live connection."""
    return cls(message)


class TestRegexFailuresAs400:
    def test_invalid_regular_expression_becomes_400(self) -> None:
        # SQLSTATE 2201B. This is what Postgres raises for ``(?P<x>a)``,
        # ``\z``, and every other pattern its POSIX engine rejects.
        with pytest.raises(HTTPException) as caught, regex_failures_as_400():
            raise _postgres_error(
                asyncpg.InvalidRegularExpressionError,
                "invalid regular expression: invalid embedded option",
            )
        assert caught.value.status_code == 400

    def test_query_canceled_becomes_400(self) -> None:
        with pytest.raises(HTTPException) as caught, regex_failures_as_400():
            raise _postgres_error(asyncpg.QueryCanceledError, "canceling statement")
        assert caught.value.status_code == 400

    @pytest.mark.parametrize(
        "cls",
        [
            asyncpg.InvalidTextRepresentationError,
            asyncpg.NumericValueOutOfRangeError,
        ],
    )
    def test_a_bad_order_operand_becomes_400(
        self, cls: type[asyncpg.PostgresError]
    ) -> None:
        # SQLSTATE 22P02 / 22003. The order templates cast the operand
        # (``col < $1::numeric``), so caller text is parsed at query time and
        # can fail for a reason only the column's type decides.
        with pytest.raises(HTTPException) as caught, regex_failures_as_400():
            raise _postgres_error(cls, "invalid input syntax for type numeric")
        assert caught.value.status_code == 400

    def test_unrelated_server_fault_propagates(self) -> None:
        # A generated-SQL defect is OUR bug, not the caller's; relabelling it
        # 400 would hide a server fault behind a client error.
        with pytest.raises(asyncpg.PostgresSyntaxError), regex_failures_as_400():
            raise _postgres_error(
                asyncpg.PostgresSyntaxError, 'syntax error at or near "FROM"'
            )

    def test_unrelated_exception_propagates(self) -> None:
        with pytest.raises(ValueError, match="unrelated"), regex_failures_as_400():
            raise ValueError("unrelated")


@pytest_asyncio.fixture(loop_scope="session")
async def store(pglite_engine: PGliteEngine) -> AsyncIterator[Store]:
    """A bootstrapped Store over the session's shared PGlite engine."""
    await reset_schema(pglite_engine)
    store = Store(pglite_engine, embed=StubEmbedder())
    await store.bootstrap()
    yield store


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("pattern", ["[a-b-c]", "(?=a)*", r"[\1]"])
async def test_real_engine_rejection_is_a_400(store: Store, pattern: str) -> None:
    r"""A pattern Python accepts and Postgres does not must reach the caller as 400.

    Driving the real engine is the point: the guard was previously written
    against ``PostgresSyntaxError``, which reads plausibly and never fires,
    so every unit test built on that belief passed while the live route 500'd.

    The patterns are ones the wire validator still ADMITS -- ``(?P<n>a)`` and
    ``\\z`` used to serve here and no longer reach an engine, since the
    dialect gate refuses them by name. POSIX's grammar is not enumerable, so
    this residual class is what the guard is for: live PG16 answers "invalid
    character range" / "quantifier operand invalid" / "invalid escape \\
    sequence" for these three.
    """
    await store.submit_issue(SubmitIssue(account="a@b.c", title="alpha"))
    with pytest.raises(HTTPException) as caught, regex_failures_as_400():
        await store.list_kind(
            "Issue", filters=(Filter(field="title", op="re", value=pattern),)
        )
    assert caught.value.status_code == 400


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_out_of_range_order_operand_is_refused_before_the_engine(
    store: Store,
) -> None:
    """An operand no ``float8`` can hold is refused where the column is known.

    ``belief_confidence`` is ``DOUBLE PRECISION``, so Postgres resolves
    ``col < $1::numeric`` by casting the operand DOWN to float8, and neither
    ``1e400`` nor ``1e-400`` survives that -- live PG16 raises SQLSTATE 22003
    for both. It answered them here as ``inf`` and ``0.0``, which is the
    divergence; the refusal is now local, since only the COLUMN decides what
    fits and ``reject_inadmissible`` is where the column is in hand.

    ``regex_failures_as_400`` still maps 22003, for the same reason the POSIX
    grammar is not enumerable: it is the backstop for what the guard cannot
    decide. Nothing reaches it from this path any more, which is the point.
    """
    for operand in ("1e400", "1e-400"):
        with pytest.raises(ValidationError, match="out of range"):
            await store.list_kind(
                "Belief",
                filters=(Filter(field="belief_confidence", op="lt", value=operand),),
            )


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
