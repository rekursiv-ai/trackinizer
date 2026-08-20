"""Statement-timeout bound for queries carrying a caller-supplied regex.

``MAX_FILTER_VALUE_CHARS`` bounds a pattern's LENGTH, not its run time:
compiling the worst 512-char pattern measures 0.5ms.

This bounds a query carrying caller input generally. It does NOT bound a
backtracking match: Postgres uses a hybrid NFA/DFA that answers
``((((a+)+)+)+)+$`` over 2000 characters in 48ms, so the catastrophic case
this was first written for cannot arise there. The engine that DOES backtrack
is Python's, on filters that never lower -- and no statement timeout reaches
it, which is why ``wire.filters`` refuses regex on those columns outright.

This lives beside the store rather than under ``api/`` because the store owns
the connection the bound must be set on; the matching HTTP status mapping is
``api/_regex_guard``. ``/api/web/search`` grew both first, as a documented
incident -- these are that fix lifted out so a second caller cannot drift.
"""

from __future__ import annotations

from typing import Final

import os

from trackinizer.lib.postgres import Conn


__all__ = ["STATEMENT_TIMEOUT_MS", "apply_regex_statement_timeout"]


def _statement_timeout_ms() -> int:
    """Per-query timeout in milliseconds (default 5s, env-overridable).

    ``TRACKINIZER_SEARCH_TIMEOUT_MS`` overrides the 5000ms default; a
    non-positive or non-integer value is an operator typo and raises rather
    than silently disabling the guard (``0`` means "no timeout" in Postgres,
    which is exactly the DoS this closes).
    """
    raw = os.environ.get("TRACKINIZER_SEARCH_TIMEOUT_MS", "").strip()
    if not raw:
        return 5_000
    timeout_ms = int(raw)
    if timeout_ms < 1:
        raise ValueError(
            f"TRACKINIZER_SEARCH_TIMEOUT_MS must be a positive integer, got {raw!r}"
        )
    return timeout_ms


STATEMENT_TIMEOUT_MS: Final = _statement_timeout_ms()
"""Resolved once at import; the ``SET LOCAL statement_timeout`` bound."""


async def apply_regex_statement_timeout(conn: Conn) -> None:
    """Cap the next statement on ``conn``, bounding a pathological match.

    ``SET LOCAL`` is transaction-scoped, so the caller must already be in one;
    outside a transaction Postgres warns and the setting does not stick. The
    bound is a server constant, never client input, so it interpolates safely.
    """
    await conn.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
