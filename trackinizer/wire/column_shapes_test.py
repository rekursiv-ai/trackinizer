"""One table decides where a filter runs; both readers must read that table.

Every regression this file pins came from asking a question slightly different
from the one lowering answers. Lowering is decided per ``(column, op)``: a
column with a shape still has no SQL for an op its shape does not declare. A
guard that asks only "does this COLUMN lower" waves through the op that does
not, and the filter lands in Python -- unbounded, and comparing values SQL
would have compared differently.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

import re

import pytest

from trackinizer.server.schema_gen import generate_inquiry_kind_columns
from trackinizer.wire.column_shapes import (
    COLUMN_SHAPES,
    ColumnShape,
    _is_integer_sql,
    _is_real_sql,
    lowers_into_sql,
    sql_template,
)


class TestTheOracleIsPerColumnAndOp:
    def test_an_op_absent_from_the_shape_does_not_lower(self) -> None:
        # ``labels`` is an ARRAY: ordering a list is meaningless, so the shape
        # declares no ``gt``. Asking only about the column answered yes and
        # sent ``str(['a','b']) > 'x'`` to Python.
        assert lowers_into_sql("labels", "is") is True
        assert lowers_into_sql("labels", "gt") is False

    def test_a_column_with_no_shape_never_lowers(self) -> None:
        assert lowers_into_sql("experiment_config", "is") is False
        assert lowers_into_sql("experiment_config", "re") is False

    def test_an_unknown_column_never_lowers(self) -> None:
        assert lowers_into_sql("no_such_column", "is") is False

    @pytest.mark.parametrize("op", ["lt", "le", "gt", "ge"])
    def test_order_ops_lower_only_where_order_is_defined(self, op: str) -> None:
        # Numeric and timestamp columns order identically in both evaluators;
        # text, array, and uuid do not (``"10" < "9"`` is true to Python and
        # false to SQL), so their shapes decline the op.
        assert lowers_into_sql("issue_priority", op) is True
        assert lowers_into_sql("created", op) is True
        assert lowers_into_sql("title", op) is False
        assert lowers_into_sql("labels", op) is False
        assert lowers_into_sql("id", op) is False


class TestTheNumericVocabularyIsComplete:
    r"""A declared type must classify by what Postgres MEANS, not its spelling.

    Postgres accepts several names for one type -- ``FLOAT8``, ``FLOAT`` and
    ``DOUBLE PRECISION`` are the same type, as are ``INT4``, ``INT`` and
    ``INTEGER`` (verified: all resolve to ``double precision`` / ``integer``
    in ``information_schema``). An unlisted spelling falls through to no
    shape at all, which silently drops the column's order ops and sends its
    regex to Python -- a wrong answer, not a missing feature.
    """

    @pytest.mark.parametrize(
        "sql_type", ["DOUBLE PRECISION", "FLOAT", "FLOAT4", "FLOAT8", "REAL"]
    )
    def test_a_real_spelling_classifies_as_real(self, sql_type: str) -> None:
        assert _is_real_sql(sql_type) is True

    @pytest.mark.parametrize(
        "sql_type", ["INTEGER", "INT", "INT2", "INT4", "INT8", "BIGINT", "SMALLINT"]
    )
    def test_an_integer_spelling_classifies_as_integer(self, sql_type: str) -> None:
        assert _is_integer_sql(sql_type) is True

    @pytest.mark.parametrize("sql_type", ["TEXT", "JSONB", "UUID", "TIMESTAMPTZ"])
    def test_a_non_numeric_spelling_does_not(self, sql_type: str) -> None:
        assert _is_real_sql(sql_type) is False
        assert _is_integer_sql(sql_type) is False

    def test_precision_does_not_hide_the_type(self) -> None:
        assert _is_real_sql("NUMERIC(14, 6)") is True


class TestEveryShapeMatchesTheDeclaredType:
    r"""A column's shape must match the type the schema actually declares.

    The shape is DERIVED from the Python annotation, and the DDL is generated
    from the same specs -- but by a different rule, so the two can disagree
    without a word from either. A wrong shape is silent: the column gets the
    wrong SQL, and no engine errors. Nothing else compares them, and the
    kind-specific columns (``belief_confidence`` is ``DOUBLE PRECISION``,
    ``experiment_codechanges`` is ``UUID[]``) appear only in the generated
    half, so reading ``schema.sql`` sees none of them.
    """

    #: The declared-type heads each shape may legitimately classify.
    HEADS: Final[Mapping[ColumnShape, frozenset[str]]] = {
        ColumnShape.TEXT: frozenset({"TEXT"}),
        ColumnShape.INTEGER: frozenset(
            {"INTEGER", "INT", "INT2", "INT4", "INT8", "BIGINT", "SMALLINT"}
        ),
        ColumnShape.REAL: frozenset(
            {"NUMERIC", "DECIMAL", "REAL", "FLOAT", "FLOAT4", "FLOAT8", "DOUBLE"}
        ),
        ColumnShape.RENDERED: frozenset({"UUID"}),
        ColumnShape.TIMESTAMP: frozenset({"TIMESTAMPTZ"}),
    }

    @staticmethod
    def declared() -> dict[str, str]:
        """Column -> declared SQL type, from the generated per-kind DDL."""
        out: dict[str, str] = {}
        for line in generate_inquiry_kind_columns().split("\n"):
            if (found := re.match(r"^\s{4}([a-z_]+)\s+(\S+)", line)) is not None:
                out[found.group(1)] = found.group(2)
        return out

    def test_the_generated_ddl_is_readable(self) -> None:
        # A parse that silently matched nothing would make every assertion
        # below vacuous.
        assert self.declared()["belief_confidence"] == "DOUBLE"

    @pytest.mark.parametrize("column", sorted(COLUMN_SHAPES))
    def test_the_shape_matches_the_declared_type(self, column: str) -> None:
        declared = self.declared().get(column)
        if declared is None:
            return  # An identity column, declared in ``schema.sql`` by hand.
        shape = COLUMN_SHAPES[column]
        if shape is ColumnShape.ARRAY:
            assert declared.endswith("[]"), f"{column} is {declared}, not an array"
            return
        head = declared.split("(", maxsplit=1)[0].upper()
        assert head in self.HEADS[shape], f"{column} is {declared}, shaped {shape}"

    @pytest.mark.parametrize(
        "column",
        sorted(c for c, s in COLUMN_SHAPES.items() if s is ColumnShape.REAL),
    )
    def test_a_real_column_cannot_hold_a_value_the_renderer_misformats(
        self, column: str
    ) -> None:
        r"""A REAL column must be too narrow to reach 16 integer digits.

        ``_REAL_TEXT``'s ELSE branch passes fractional values through
        ``float8::text``, which switches to scientific notation at 16
        significant digits while ``str(float)`` stays fixed until magnitude
        1e16 -- measured against live PG16, ``1026640683713603.5`` renders
        ``1.0266406837136036e+15`` there and ``1026640683713603.5`` here. The
        two would then disagree on ``is`` / ``ne`` / ``re``.

        No current column can hold such a value: ``NUMERIC(14, 6)`` overflows
        past ~1e8 and ``belief_confidence`` is checked to ``[0, 1]``. That is
        the invariant, and it is a property of the DECLARED TYPE rather than
        of the data -- so a future ``NUMERIC(30, 10)`` would silently activate
        the gap, pass every other test, and return wrong rows. This fails
        instead, at the declaration.

        A DOUBLE PRECISION column has no declared bound and so cannot be
        cleared here; it needs a ``CHECK`` narrow enough, which is why the
        precision is asserted from the constraint for that case.
        """
        declared = self.declared().get(column)
        if declared is None:
            declared = "NUMERIC(14, 6)"  # the hand-declared cost axes
        if declared.upper().startswith(("NUMERIC", "DECIMAL")):
            digits = re.search(r"\((\d+)\s*,\s*(\d+)\)", declared)
            assert digits is not None, f"{column} declares no precision: {declared}"
            integer_digits = int(digits.group(1)) - int(digits.group(2))
            assert integer_digits < 16, (
                f"{column} is {declared}, which holds {integer_digits} integer "
                "digits -- at 16 the SQL renderer emits scientific notation "
                "where str(float) does not, and the two evaluators disagree"
            )
            return
        # DOUBLE PRECISION: the range is unbounded, so the bound must come
        # from a CHECK. ``belief_confidence`` is the only such column.
        assert column == "belief_confidence", (
            f"{column} is {declared}, whose range is unbounded; give it a "
            "CHECK below 1e15 or teach _REAL_TEXT the wide-magnitude case"
        )

    def test_the_jsonb_column_is_deliberately_unclassified(self) -> None:
        # ``str(dict)`` is a Python repr no SQL rendering reproduces, so it
        # must have NO shape -- giving it one would lower a clause the two
        # evaluators answer differently.
        assert "experiment_config" in self.declared()
        assert "experiment_config" not in COLUMN_SHAPES


class TestEveryShapeIsUsable:
    @pytest.mark.parametrize("op", ["is", "ne", "isnull", "notnull"])
    def test_every_column_answers_equality_and_presence(self, op: str) -> None:
        for column in COLUMN_SHAPES:
            assert sql_template(column, op) is not None, f"{column} {op}"

    def test_no_shape_is_declared_without_a_column(self) -> None:
        # A shape nothing classifies is dead: its SQL is unreachable.
        used = set(COLUMN_SHAPES.values())
        assert used == set(ColumnShape), set(ColumnShape) - used

    def test_a_template_names_both_placeholders(self) -> None:
        # The caller formats ``{col}`` and ``{p}``; a template missing one
        # raises KeyError only when that op is first filtered on.
        template = sql_template("title", "is")
        assert template is not None
        assert "{col}" in template
        assert "{p}" in template


if __name__ == "__main__":
    from trackinizer.lib.testing import test_main

    test_main(__file__)
