"""Row-vs-filter predicate shared by the server and the CLI test fake.

The server route applies this against each asyncpg row before the LIMIT
clause; the CLI test fake runs the same predicate over its in-memory rows
so unit-test semantics match the route's.

:func:`match_filter` resolves the filter field through
:func:`canonical_filter_field`, so a ``Filter`` carrying either an alias
or a canonical name hits the same column. NULL columns follow SQL
three-valued logic; other comparisons treat the value as a string unless
both sides parse cleanly as numbers.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, cast

import re

from trackinizer.wire.filters import (
    FilterOp,
    canonical_filter_field,
)


__all__ = ["RowFilter", "match_filter"]


class RowFilter(Protocol):
    """Structural shape of a parsed filter clause.

    Any frozen dataclass with these three attributes satisfies it;
    :class:`wire.filters.Filter` is the canonical implementation. The
    attributes are read-only ``@property`` so a frozen dataclass, whose
    synthesized attributes are also read-only, matches structurally.
    """

    @property
    def field(self) -> str: ...
    @property
    def op(self) -> FilterOp: ...
    @property
    def value(self) -> str: ...


class _Row(Protocol):
    """A row we can evaluate filters against.

    Both ``asyncpg.Record`` and ``dict`` test fakes satisfy this. Keys
    are the canonical SQL column names; :func:`match_filter` canonicalizes
    the filter field before lookup so an aliased ``Filter`` still resolves
    to the right column.
    """

    def __getitem__(self, key: str, /) -> object: ...
    def get(self, key: str, /) -> object | None: ...


# Each negated op is exactly its affirmative twin's complement. Defining
# the pair in one place is what makes ``ne``/``nre`` single-sourced: the
# affirmative predicate carries the semantics, and NULL handling, and the
# negation is a mechanical ``not``.
_NEGATED_OPS: dict[FilterOp, FilterOp] = {"ne": "is", "nre": "re"}


def match_filter(row: _Row, filt: RowFilter) -> bool:
    """Return whether ``row`` satisfies ``filt``.

    - ``is`` / ``ne``: equality, or membership for list-shaped values.
    - ``re`` / ``nre``: :func:`re.search` against the string form of the
      value, and its negation.
    - ``lt`` / ``le`` / ``gt`` / ``ge``: numeric when both sides parse as
      numbers, string compare otherwise.

    A NULL column (asyncpg returns ``None`` for an unset nullable field) is
    *absent*: every affirmative predicate (``is`` / ``re`` / the order ops)
    is false against it, and each negated op is the exact complement of its
    affirmative twin, so ``ne`` / ``nre`` keep the row. ``priority gt 5``
    still drops NULL rows because the order predicate itself is false there
    -- otherwise the string fallback in :func:`_matches_order` would make
    ``"None" > "5"`` true (``'N' > '5'`` in ASCII). ``isnull`` / ``notnull``
    test presence directly and ignore ``filt.value``.
    """
    if filt.op in ("isnull", "notnull"):
        absent = row.get(canonical_filter_field(filt.field)) is None
        return absent if filt.op == "isnull" else not absent
    affirmative = _NEGATED_OPS.get(filt.op)
    if affirmative is not None:
        return not _matches_affirmative(row, affirmative, filt)
    return _matches_affirmative(row, filt.op, filt)


def _matches_affirmative(row: _Row, op: FilterOp, filt: RowFilter) -> bool:
    """Evaluate one *affirmative* op (never ``ne`` / ``nre``).

    A NULL value is absent and satisfies no affirmative predicate, so this
    returns ``False`` for it across the board; the negated ops in
    :func:`match_filter` invert that into ``True``.
    """
    value = row.get(canonical_filter_field(filt.field))
    if value is None:
        return False
    if op == "re":
        return _matches_regex(value, filt.value)
    if op == "is":
        return _matches_eq(value, filt.value)
    return _matches_order(value, op, filt.value)


def _matches_regex(value: object, pattern: str) -> bool:
    return any(
        re.search(pattern, str(item)) is not None for item in _candidate_items(value)
    )


def _matches_eq(value: object, expected: str) -> bool:
    return any(str(item) == expected for item in _candidate_items(value))


def _candidate_items(value: object) -> tuple[object, ...]:
    """Yield the comparable items from a single row value.

    A scalar stands alone. A flat list (``labels``, ``subscribers``,
    ``codechanges``) exposes every element, so ``label is <x>`` matches any
    element rather than the whole-list repr.
    """
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        return (value,)
    return tuple(value)


def _matches_order(value: object, op: str, expected: str) -> bool:
    if _is_numeric(value) and _is_numeric(expected):
        left = float(cast(float | int | str, value))
        right = float(cast(float | int | str, expected))
        if op == "lt":
            return left < right
        if op == "le":
            return left <= right
        if op == "gt":
            return left > right
        return left >= right
    s_left = str(value)
    s_right = str(expected)
    if op == "lt":
        return s_left < s_right
    if op == "le":
        return s_left <= s_right
    if op == "gt":
        return s_left > s_right
    return s_left >= s_right


def _is_numeric(value: object) -> bool:
    try:
        float(cast(float | int | str, value))
    except (TypeError, ValueError):
        return False
    return True
