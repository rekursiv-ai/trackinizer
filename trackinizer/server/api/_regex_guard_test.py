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
from pathlib import Path

from fastapi import HTTPException

import asyncpg
import pytest
import pytest_asyncio

from trackinizer.lib.postgres import PGliteEngine
from trackinizer.server.api._regex_guard import regex_failures_as_400
from trackinizer.server.store.core import Store, StubEmbedder
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


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    """A bootstrapped Store over an ephemeral in-process PGlite engine."""
    async with PGliteEngine(
        workdir=tmp_path / "pglite", persist=False, extensions=("pgvector",)
    ) as engine:
        store = Store(engine, embed=StubEmbedder())
        await store.bootstrap()
        yield store


@pytest.mark.db_pglite
@pytest.mark.asyncio
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
@pytest.mark.asyncio
async def test_an_out_of_range_order_operand_is_a_400(store: Store) -> None:
    """An operand too large for the COLUMN's type is the caller's, not ours.

    ``belief_confidence`` is ``DOUBLE PRECISION``, so Postgres resolves
    ``col < $1::numeric`` by casting the operand DOWN to float8 -- and
    ``1e400`` does not fit, raising SQLSTATE 22003 rather than answering.
    The operand parses as ``numeric`` perfectly well, so the wire guard has
    nothing to refuse; only the engine knows the column cannot hold it.
    Unmapped, it reached the caller as a 500.
    """
    with pytest.raises(HTTPException) as caught, regex_failures_as_400():
        await store.list_kind(
            "Belief",
            filters=(Filter(field="belief_confidence", op="lt", value="1e400"),),
        )
    assert caught.value.status_code == 400


if __name__ == "__main__":
    from trackinizer.lib.testing import test_main

    test_main(__file__)
