r"""A regex filter must reach only an engine that can bound it.

``re``/``nre`` lower into a Postgres ``~`` when the column has a SQL shape.
Postgres uses a hybrid NFA/DFA: ``((((a+)+)+)+)+$`` over 2000 characters
returns in 48ms, and a statement timeout bounds it regardless. A filter that
does NOT lower is evaluated by ``match_filter`` -- Python, which backtracks.
The same pattern against 40 stored characters measured **20.11 seconds**
through ``Store.list_kind``, doubling per character, blocking the event loop
for every concurrent request, with no deadline able to reach it.

The refusal lives in ``_partition_filters`` because that is where lowerability
stops being a prediction and becomes a fact. An earlier attempt guessed the
same fact in the wire layer from ``sql_type == "JSONB"``, and every bypass
below is a way that guess was wrong:

* the alias ``config`` never equals ``experiment_config``;
* a bare ``RowFilter`` skips ``Filter.__post_init__`` entirely;
* ``"jsonb"`` or ``"JSONB NOT NULL"`` fails an exact-string match while
  ``read._classify`` still refuses to lower it.

Asking the store removes all three at once: it canonicalizes the field itself
and answers from its own shape table.
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

    @pytest.mark.parametrize(
        "op", ["is", "ne", "lt", "le", "gt", "ge", "isnull", "notnull"]
    )
    def test_non_regex_ops_on_jsonb_still_work(self, op: str) -> None:
        # These also evaluate in Python on this column, but they are string
        # comparisons: measured 0.8-3.1ms end-to-end where ``re`` took 20.11s.
        # Refusing them would remove capability for no safety gain.
        _, remaining = read._partition_filters(
            (
                _BareFilter(
                    field="experiment_config", op=cast("FilterOp", op), value="x"
                ),
            ),
            [],
        )
        assert remaining

    def test_lowering_disabled_refuses_by_column_not_by_mode(self) -> None:
        # ``LOWERING.enabled=False`` sends every filter to Python, but it is
        # reachable ONLY from the equivalence tests (see ``Lowering``), never
        # from a request -- so it is not the DoS surface the refusal guards.
        # What makes a pattern unboundable is the COLUMN, and that verdict is
        # the same in both modes. Refusing on the mode instead made the
        # SQL/Python parity suite -- which runs each filter both ways -- unable
        # to compare any regex at all, so the translation it exists to guard
        # went unchecked.
        original = read.LOWERING
        read.LOWERING = read.Lowering(enabled=False)
        try:
            with pytest.raises(ValidationError, match="experiment_config"):
                read._partition_filters(
                    (_BareFilter(field="experiment_config", op="re", value=EVIL),), []
                )
            _, remaining = read._partition_filters(
                (_BareFilter(field="title", op="re", value=EVIL),), []
            )
            assert remaining
        finally:
            read.LOWERING = original


if __name__ == "__main__":
    from trackinizer.lib.testing import test_main

    test_main(__file__)
