r"""Map Postgres-side regex failures to the 400 they are.

Two routes lower a caller-supplied pattern into a Postgres ``~`` operator:
``/api/web/search`` and ``/api/inquiries``. Postgres runs POSIX ARE, so a
pattern that compiles in Python can still be rejected there (``(?P<x>a)``,
``\z``) -- as SQLSTATE 2201B, an ``InvalidRegularExpressionError``, NOT the
42601 ``PostgresSyntaxError`` the phrase "syntax error" suggests. A
pathological pattern can also exhaust the statement timeout set by
``server.regex_timeout``. Both are caller mistakes and must not be 500s.

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
    """Report the two Postgres-side regex failures as 400, not 500.

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
        raise HTTPException(
            status_code=400,
            detail=(
                "regex exceeded the time budget; "
                "narrow the pattern or add more specific terms"
            ),
        ) from exc
