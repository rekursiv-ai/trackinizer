"""Row-vs-filter predicate for callers holding rows rather than a query.

Postgres evaluates almost everything: of the clauses the list route accepts,
242 lower into SQL and 164 are refused, leaving 4 (``experiment_config``
equality and presence, whose ``str(dict)`` no SQL reproduces) for the store to
answer here after the fetch.

This predicate exists for the callers with no database to lean on -- the trax
CLI test fake filtering in-memory dicts, and downstream orchestrators
filtering a single row delivered by a change event. Each would otherwise grow
its own filter semantics, so all of them share this one, and
:func:`reject_inadmissible` refuses whatever this predicate would answer
differently from SQL.

:func:`match_filter` resolves the filter field through
:func:`canonical_filter_field`, so a ``Filter`` carrying either an alias
or a canonical name hits the same column. NULL columns follow SQL
three-valued logic; other comparisons treat the value as a string unless
both sides parse cleanly as numbers.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from functools import lru_cache
from typing import Final, Protocol

import math
import re
import warnings

from trackinizer.types.errors import ValidationError
from trackinizer.wire.column_shapes import (
    FILTERABLE_COLUMNS,
    compares_as_float,
    lowers_into_sql,
    requires_numeric_operand,
)
from trackinizer.wire.filters import (
    ORDER_OPS,
    REGEX_OPS,
    FilterOp,
    as_numeric,
    canonical_filter_field,
    folds_case,
    is_nan,
    validate_clause,
)
from trackinizer.wire.posix_regex import posix_pattern
from trackinizer.wire.routes import MAX_LIST_LIMIT


__all__ = [
    "RowFilter",
    "match_filter",
    "reject_inadmissible",
]


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

    def get(self, key: str, /) -> object | None: ...


# Each negated op is exactly its affirmative twin's complement, so the
# affirmative predicate carries the semantics and the NULL handling while the
# negation is a mechanical ``not``.
_NEGATED_OPS: Final[dict[FilterOp, FilterOp]] = {
    "ne": "is",
    "nre": "re",
}


def reject_inadmissible(filt: RowFilter) -> None:
    """Refuse a filter whose two evaluators would not agree.

    This predicate must select the rows the store's SQL would have. A filter
    it answers DIFFERENTLY is not a slower path to the same result -- it is a
    wrong one, and the caller cannot tell which evaluator ran. Two classes are
    refused, being the two that break here:

    * A REGEX or ORDER op with no SQL for the column's shape. A regex is
      unbounded in Python (``(a+)+$`` over 30 characters measures 79.89
      seconds), and an order comparison is meaningless -- ``labels gt x``
      compares ``str(['a', 'b']) > 'x'``, the repr of a list, and
      ``title gt 10`` reads both sides as numbers where Postgres compares
      lexically. Equality and presence stay legal wherever they land:
      ``experiment_config is x`` has no SQL at all and is what this predicate
      is FOR.
    * An op outside ``FILTER_OPS``, so it never reaches ``_ordered``'s
      assert. An unrecognized op is caller input, not a broken invariant.

    A NaN operand is refused ONLY here, and only when the column's template
    casts the operand: ordering a timestamp compares TEXT, where ``nan`` is a
    well-defined string in both engines. Deciding that needs the column, which
    ``validate_clause`` does not see.

    Asked at the evaluator rather than upstream, because ``list_kind`` takes
    ``Sequence[RowFilter]`` -- a Protocol -- so a bare structural filter that
    never ran ``Filter.__post_init__`` still arrives here. It asks the shared
    ``column_shapes`` table rather than re-deriving lowerability from a
    declared ``sql_type``, which the alias ``config`` and the spelling
    ``"jsonb"`` both defeat.

    Raises:
      ValidationError: The filter cannot be evaluated here faithfully.

    """
    # The column-free half of the contract, shared with ``Filter`` so a bare
    # structural filter cannot ask what the wire type refuses.
    if (err := validate_clause(filt.field, filt.op, filt.value)) is not None:
        raise ValidationError(err)
    column = canonical_filter_field(filt.field)
    # A field NO column answers is a typo, and answering it is worse than
    # refusing it: ``row.get`` returns ``None`` for a key the row never had,
    # absent satisfies no affirmative predicate, so ``ne`` KEPT every row.
    # SQL cannot make that mistake -- ``WHERE owenr ...`` is an error there --
    # so the two evaluators disagreed on every row in the table.
    if column not in FILTERABLE_COLUMNS:
        raise ValidationError(
            f"unknown filter field {filt.field!r}: no column answers it, so "
            "SQL would error where this evaluator reads every row as NULL"
        )
    if filt.op in REGEX_OPS | ORDER_OPS and not lowers_into_sql(column, filt.op):
        detail = (
            "the pattern would be matched in Python, where a pathological "
            "expression cannot be bounded"
            if filt.op in REGEX_OPS
            else "the two evaluators order those values differently"
        )
        raise ValidationError(
            f"filter op {filt.op!r} is not supported on {column!r}: {detail}. "
            "Use 'is' / 'ne', or apply it to a column whose SQL declares the op."
        )
    # Only a template that CASTS the operand constrains it. Ordering a
    # timestamp compares text, where every operand is well defined in both
    # engines (live: ``'2026-01-01...' < 'nan'`` is true, as in Python).
    if not requires_numeric_operand(column, filt.op):
        return
    if is_nan(filt.value):
        raise ValidationError(
            f"filter value {filt.value!r} is NaN, which orders as the largest "
            "value in Postgres and compares false in Python, so the two "
            "evaluators would select different rows"
        )
    if (parsed := as_numeric(filt.value)) is None:
        raise ValidationError(
            f"filter value {filt.value!r} is not a number Postgres can read, "
            f"but ordering {column!r} casts the operand to numeric: Postgres "
            "rejects it outright (SQLSTATE 22P02), which no handler maps, "
            "while Python compares it as text"
        )
    # Parsing as ``numeric`` is not enough: the COLUMN's type decides what it
    # can hold. A float8 column compares ``{col}::float8 < $1::numeric``, so
    # the operand must survive the float8 range in BOTH directions -- live
    # PG16 answers "out of range for type double precision" (22003) for
    # ``1e400`` and for ``1e-400``, while Python reads them as ``inf`` and
    # ``0.0`` and answers. An INTEGER column compares as ``numeric``, which
    # has neither ceiling nor floor, so the same operands are legal there and
    # refusing them everywhere would remove capability the engine has.
    if compares_as_float(column, filt.op) and _overflows_float(parsed):
        raise ValidationError(
            f"filter value {filt.value!r} is out of range for {column!r}, "
            "whose SQL compares it as double precision: Postgres rejects it "
            "(SQLSTATE 22003) while Python silently rounds it"
        )


def _overflows_float(parsed: Decimal) -> bool:
    """Whether ``parsed`` is a ``numeric`` no ``float8`` can represent.

    Both ends: a magnitude past the ceiling becomes ``inf`` and one past the
    floor becomes ``0.0``, and Postgres raises 22003 for either rather than
    rounding. A non-finite operand is exempt -- ``'inf'::float8`` is
    representable, so both engines answer it.
    """
    if not parsed.is_finite():
        return False
    rendered = float(parsed)
    return not math.isfinite(rendered) or (rendered == 0.0 and parsed != 0)


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
    reject_inadmissible(filt)
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


def _reject_non_ascii_fold(value: object, pattern: str) -> None:
    """Refuse a case-insensitive match against a non-ASCII value.

    The wire type gates on the pattern, which is all it can see; the other
    half of the divergence needs the ROW. Live PG16 says
    ``'\u0130' ~ '(?i)i'`` is FALSE where Python says true, with an ASCII
    pattern -- the two fold Unicode differently and both ANSWER, so nothing
    downstream catches it.

    Raises:
      ValidationError: The value carries non-ASCII text and the pattern folds
        case.

    """
    if not folds_case(pattern):
        return
    for item in _candidate_items(value):
        if not str(item).isascii():
            raise ValidationError(
                "regex flag 'i' case-folds non-ASCII text differently in the "
                "two engines, which both answer rather than error, and this "
                "row carries non-ASCII text. Drop '(?i)', or compare with "
                "'is' / 'ne'"
            )


def _matches_regex(value: object, pattern: str) -> bool:
    """Search each candidate item, in the dialect the STORE evaluates.

    The server lowers ``re`` / ``nre`` into Postgres' ``~`` operator, so this
    predicate -- which the CLI's test fake runs to mirror route semantics --
    must read the pattern the same way or the two disagree on real input.
    Postgres uses POSIX ARE; the differences are translated by
    :func:`posix_pattern` and refused by :func:`filters.validate_clause`.
    """
    _reject_non_ascii_fold(value, pattern)
    compiled = _compiled(pattern)
    return any(
        compiled.search(str(item)) is not None for item in _candidate_items(value)
    )


# BOUNDED because the key is a caller's operand, and sized above the route's
# per-request filter cap so one request cannot evict its own entries -- which
# is how ``re``'s own 512-entry cache fails here.
@lru_cache(maxsize=2 * MAX_LIST_LIMIT)
def _compiled(pattern: str) -> re.Pattern[str]:
    """Translate and compile ``pattern`` once per distinct operand.

    ``match_filter`` runs per ROW, so the same operand is otherwise recompiled
    for every row of a page: measured with 600 distinct operands over 200
    rows, every ``re``-cache lookup missed and evaluation cost 9.34us against
    1.00us here.

    The warning suppression is not redundant with the validator's: ``[[]`` is
    a valid pattern Python only warns about, and under this repo's
    ``filterwarnings = ["error"]`` that warning is an exception. Relying on
    the validator having warmed ``re``'s cache first would only hide it.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        return re.compile(posix_pattern(pattern))


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
    """Compare in the arithmetic the row's own SQL type selects.

    Which arithmetic is not a detail. Postgres resolves ``col < $1::numeric``
    by the COLUMN's type: an ``integer`` or ``numeric`` column compares as
    ``numeric`` and keeps every digit, while a ``double precision`` column
    casts the operand DOWN to float8 and loses them -- live PG16 says
    ``1::int < '1.00000000000000001'::numeric`` is TRUE and
    ``1::float8 < '1.00000000000000001'::numeric`` is FALSE. Comparing
    everything as ``float`` reproduces only the second.

    The row value arrives typed (asyncpg hands back ``int`` for INTEGER,
    ``Decimal`` for NUMERIC, ``float`` for DOUBLE PRECISION), so its type
    picks the arithmetic without anything here having to know the schema.
    """
    parsed = as_numeric(expected)
    if parsed is None:
        return _ordered(str(value), op, str(expected))
    if isinstance(value, float):
        return _ordered(value, op, float(parsed))
    if isinstance(value, int | Decimal):
        return _ordered(Decimal(value), op, parsed)
    return _ordered(str(value), op, str(expected))


def _ordered[T: (float, Decimal, str)](left: T, op: str, right: T) -> bool:
    """Apply one order op, asserting the op is one.

    An assert rather than a trailing ``return left >= right``: that
    fallthrough would make an unrecognized op MEAN ``ge`` and return a boolean
    the caller cannot tell from a real answer. ``reject_inadmissible`` refuses
    an unknown op upstream, so reaching here with one is a broken invariant.
    """
    if op == "lt":
        return left < right
    if op == "le":
        return left <= right
    if op == "gt":
        return left > right
    assert op == "ge", f"unreachable order op {op!r}"
    return left >= right
