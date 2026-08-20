r"""A regex filter must reach only an engine that can bound it.

``re``/``nre`` lower into a Postgres ``~`` when the column has a SQL shape.
Postgres uses a hybrid NFA/DFA: ``((((a+)+)+)+)+$`` over 2000 characters
returns in 48ms, and a statement timeout bounds it regardless. A filter that
does NOT lower is evaluated by ``match_filter`` -- Python, which backtracks.
The same pattern against 40 stored characters measured **20.11 seconds**
through ``Store.list_kind``, doubling per character, blocking the event loop
for every concurrent request, with no deadline able to reach it.

``_partition_filters`` refuses such a filter before a row is fetched. It does
so by asking the shared classification (``wire.column_shapes``) rather than
predicting it: an earlier attempt guessed from ``sql_type == "JSONB"``, and
every bypass below is a way that guess was wrong:

* the alias ``config`` never equals ``experiment_config``;
* a bare ``RowFilter`` skips ``Filter.__post_init__`` entirely;
* ``"jsonb"`` or ``"JSONB NOT NULL"`` fails an exact-string match while the
  classifier still refuses to lower it.

The same question is asked again inside ``match_filter`` itself, which is the
backstop for callers that never reach the store; this file covers the store's
half. See ``wire/row_filter_bound_test.py`` for the evaluator's.
"""

from __future__ import annotations

from typing import cast

import dataclasses

import pytest

from trackinizer.server.store import read
from trackinizer.types.errors import ValidationError
from trackinizer.wire.filters import FilterOp


@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class _BareFilter:
    """The structural ``RowFilter`` the store accepts, minus the wire type.

    ``Store.list_kind`` is typed ``Sequence[RowFilter]`` -- a Protocol -- so
    ANY frozen dataclass carrying ``field``/``op``/``value`` reaches the
    evaluator. That is why the refusal cannot live on ``wire.Filter``: this
    object never runs its ``__post_init__``.
    """

    field: str
    op: FilterOp
    value: str


EVIL = "(a+)+$"


class TestUnboundableRegexIsRefused:
    def test_canonical_jsonb_column_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="experiment_config"):
            read._partition_filters(
                (_BareFilter(field="experiment_config", op="re", value=EVIL),), []
            )

    def test_the_alias_is_refused_too(self) -> None:
        # ``config`` -> ``experiment_config`` (``_kind_specific_aliases``).
        # The store canonicalizes when it lowers, so asking it closes the
        # alias hole without a second spelling table to keep in step.
        with pytest.raises(ValidationError):
            read._partition_filters(
                (_BareFilter(field="config", op="re", value=EVIL),), []
            )

    def test_negated_regex_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            read._partition_filters(
                (_BareFilter(field="experiment_config", op="nre", value=EVIL),), []
            )

    def test_regex_on_a_lowering_column_is_allowed(self) -> None:
        # ``title`` lowers to ``~``; Postgres and the statement timeout bound
        # it. The restriction is about reachability, not about regex.
        clauses, remaining = read._partition_filters(
            (_BareFilter(field="title", op="re", value=EVIL),), []
        )
        assert clauses
        assert not remaining

    @pytest.mark.parametrize(("op", "value"), [("is", "x"), ("ne", "x")])
    def test_equality_on_jsonb_still_works(self, op: str, value: str) -> None:
        # These evaluate in Python on this column too, but they are string
        # comparisons: measured 0.8-3.1ms end-to-end where ``re`` took 20.11s.
        # Refusing them would remove capability for no safety gain.
        _, remaining = read._partition_filters(
            (
                _BareFilter(
                    field="experiment_config", op=cast("FilterOp", op), value=value
                ),
            ),
            [],
        )
        assert remaining

    @pytest.mark.parametrize("op", ["isnull", "notnull"])
    def test_presence_on_jsonb_still_works(self, op: str) -> None:
        # A presence op carries no operand, so it is spelled with ``""``.
        _, remaining = read._partition_filters(
            (
                _BareFilter(
                    field="experiment_config", op=cast("FilterOp", op), value=""
                ),
            ),
            [],
        )
        assert remaining

    @pytest.mark.parametrize("op", ["lt", "le", "gt", "ge"])
    def test_order_ops_on_jsonb_are_refused(self, op: str) -> None:
        # ``str(dict)`` has no order any SQL reproduces, so Python's answer
        # would be one no lowered query could return.
        with pytest.raises(ValidationError, match="experiment_config"):
            read._partition_filters(
                (
                    _BareFilter(
                        field="experiment_config", op=cast("FilterOp", op), value="x"
                    ),
                ),
                [],
            )

    @pytest.mark.parametrize("op", ["lt", "le", "gt", "ge"])
    def test_a_nan_operand_is_refused_before_it_lowers(self, op: str) -> None:
        # ``seq`` is INTEGER, so this DOES lower -- and that is the danger:
        # live PG16 sorts NaN largest, answering ``5 < 'nan'`` true where
        # Python answers false. A guard on the declined branch never saw it.
        with pytest.raises(ValidationError, match="NaN"):
            read._partition_filters(
                (_BareFilter(field="seq", op=cast("FilterOp", op), value="nan"),), []
            )

    def test_lowering_disabled_still_refuses_an_unboundable_column(self) -> None:
        # ``lowering=False`` sends EVERY filter to Python -- the
        # equivalence-test mode. A regex is at its most dangerous there, so
        # the early return must not skip the check.
        with pytest.raises(ValidationError):
            read._partition_filters(
                (_BareFilter(field="experiment_config", op="re", value=EVIL),),
                [],
                lowering=False,
            )

    def test_lowering_disabled_does_not_refuse_a_lowering_column(self) -> None:
        # The refusal is about the COLUMN, never about the mode: a filter on
        # ``title`` is admissible because the column has a SQL form, and the
        # test-only kwarg does not change that. Refusing it here made the
        # equivalence suite's own regex cases unrunnable -- it never showed
        # because ``db_pglite`` is deselected by default.
        clauses, remaining = read._partition_filters(
            (_BareFilter(field="title", op="re", value=EVIL),), [], lowering=False
        )
        assert not clauses
        assert remaining


if __name__ == "__main__":
    from trackinizer.lib.testing import test_main

    test_main(__file__)
