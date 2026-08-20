r"""Map Postgres-side filter failures to the 400 they are.

Two routes lower caller text into a Postgres query: ``/api/web/search`` and
``/api/inquiries``. A regex becomes a ``~`` operand and an order operand
becomes a ``::numeric`` cast, so in both cases Postgres parses the caller's
string at query time and can reject it.

Postgres runs POSIX ARE, so a pattern that compiles in Python can still be
rejected there (``(?P<x>a)``, ``\z``) -- as SQLSTATE 2201B, an
``InvalidRegularExpressionError``, NOT the 42601 ``PostgresSyntaxError`` the
phrase "syntax error" suggests. A pathological pattern can also exhaust the
statement timeout set by ``server.regex_timeout``. An order operand adds
22P02 (a spelling ``numeric`` will not read) and 22003 (a value too large for
the column's type). All four are caller mistakes and must not be 500s.

The timeout itself lives in ``server.regex_timeout`` because it needs the
connection, which the store owns; this half is the route's, because mapping a
failure to an HTTP status is the route's job.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from fastapi import HTTPException

import asyncpg


__all__ = ["regex_failures_as_400"]


@contextmanager
def regex_failures_as_400() -> Generator[None]:
    """Report every Postgres-side operand failure as 400, not 500.

    Anything else propagates untouched: a real server fault must not be
    relabelled as the caller's mistake.
    """
    try:
        yield
    except asyncpg.InvalidRegularExpressionError as exc:
        # SQLSTATE 2201B, a ``DataError``. NOT ``PostgresSyntaxError`` (42601),
        # which the phrase "syntax error" suggests and which the two classes
        # do not share: catching that instead is a silent no-op, and was --
        # ``(?P<x>a)`` kept reaching FastAPI as a 500 while a unit test built
        # on the wrong class passed.
        raise HTTPException(
            status_code=400, detail=f"invalid regex for Postgres: {exc!s}"
        ) from exc
    except asyncpg.QueryCanceledError as exc:
        # The timeout wraps the whole statement, not the regex alone -- a slow
        # scan hits it with no pattern in the query at all -- so the message
        # names the query. Blaming the regex sent a caller to edit something
        # their request may not contain.
        raise HTTPException(
            status_code=400,
            detail=(
                "query exceeded the time budget; narrow the filters "
                "or add more specific terms"
            ),
        ) from exc
    except (
        asyncpg.InvalidTextRepresentationError,
        asyncpg.NumericValueOutOfRangeError,
    ) as exc:
        # An ORDER operand, not a pattern: the templates cast it (``col <
        # $1::numeric``), so Postgres parses caller text at query time.
        # 22P02 is a spelling ``numeric`` will not read, and 22003 an operand
        # too large for the COLUMN's own type -- live PG16 raises it for
        # ``belief_confidence < '1e400'::numeric``, since a DOUBLE PRECISION
        # column casts the operand down. The wire guard refuses what it can
        # decide without the schema; these two need the column, so only the
        # engine can raise them. Unmapped, each reached the caller as a 500.
        raise HTTPException(
            status_code=400, detail=f"invalid filter operand for Postgres: {exc!s}"
        ) from exc
