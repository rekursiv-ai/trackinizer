"""Where each ``(column, op)`` filter clause may be evaluated.

A clause runs in SQL or in Python (:func:`row_filter.match_filter`), and the
two must select the same rows. :func:`sql_template` answers with the SQL that
does so, or ``None`` when no such SQL exists -- for a JSONB column, whose
``str(dict)`` repr nothing reproduces, and for an op a shape cannot order.

The SQL is the answer, not a separate table keyed by the same question: a
caller with no SQL of its own (``match_filter``, deciding whether it may
evaluate a clause at all) asks :func:`lowers_into_sql`, so the two cannot
disagree about which clauses lower.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Literal, get_args, get_origin

from trackinizer.types.columns import (
    FlatColumn,
    flat_column_specs,
    storage_name,
)
from trackinizer.types.inquiries import INQUIRY_CLASSES
from trackinizer.wire.session_record_fields import (
    SESSION_RECORD_FIELDS,
    record_kind_for,
)


__all__ = [
    "COLUMN_SHAPES",
    "FILTERABLE_COLUMNS",
    "ColumnShape",
    "compares_as_float",
    "lowers_into_sql",
    "requires_numeric_operand",
    "sql_template",
]


class ColumnShape(StrEnum):
    """How a column's values compare, which decides its SQL form."""

    TEXT = "text"
    """A scalar string: ``=`` and the regex operators apply directly."""

    TEXT_ARRAY = "text_array"
    """A text list, where ``is`` is GIN-indexed membership."""

    UUID_ARRAY = "uuid_array"
    """A UUID list, whose string operand needs PostgreSQL type inference."""

    INTEGER = "integer"
    """A whole number. ``::text`` renders it exactly as Python's ``str``, and
    the order ops compare numerically in both evaluators."""

    REAL = "real"
    """A fractional number, rendered by :data:`_REAL_TEXT` to match
    ``str(float)``: SQL prints a ``NUMERIC(14, 6)`` as ``0.500000`` and a whole
    ``float8`` as ``100``, where Python prints ``0.5`` and ``100.0``."""

    RENDERED = "rendered"
    """A non-text scalar whose SQL ``::text`` equals Python's ``str()`` -- UUID
    today. ``match_filter`` stringifies the row value, so only a type whose two
    renderings agree may lower, through the ``::text`` the predicate implies."""

    TIMESTAMP = "timestamp"
    """An instant, rendered by :data:`_TS_TEXT` to match ``str(datetime)``.

    A bare ``::text`` renders the session time zone as ``-08`` where Python
    prints ``+00:00``. asyncpg hands back UTC-aware datetimes whatever the
    stored offset, so the predicate never sees a local offset to normalize.
    """

    SESSION_RECORD = "session_record"
    """One IR record kind's ``text``, over the session's stored records.

    A LIST-shaped column like :attr:`TEXT_ARRAY`, differing only in where the
    elements live: a side table rather than an array on the row. Every op maps
    onto the array row's meaning -- ``is`` is membership, ``re`` is an EXISTS
    over the elements, ``notnull`` asks whether the session has any such
    record at all.
    """


# Reproduces ``str(datetime)``: UTC, space separator, ``+00:00``, microseconds
# only when non-zero.
#
# ``to_char`` and ``date_part`` both answer NULL on the two infinities, making
# the whole concatenation NULL -- every comparison against it is NULL, so the
# WHERE drops a row Python keeps, even under a NEGATED filter. asyncpg encodes
# ``datetime.min`` / ``datetime.max`` as ``-infinity`` / ``infinity`` and
# decodes them back NAIVE, hence no ``+00:00`` on those two branches.
_TS_TEXT: Final = (
    "(CASE WHEN {col} = '-infinity' THEN '0001-01-01 00:00:00'"
    " WHEN {col} = 'infinity' THEN '9999-12-31 23:59:59.999999'"
    " ELSE to_char({col} AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')"
    " || CASE WHEN date_part('microsecond', {col})::int % 1000000 = 0 THEN ''"
    " ELSE '.' || lpad((date_part('microsecond', {col})::int % 1000000)::text,"
    " 6, '0') END || '+00:00' END)"
)

# Reproduces ``str(float)``: the ``float8`` cast drops NUMERIC's trailing zeros
# (``0.500000`` -> ``0.5``) and the appended ``.0`` restores what Postgres omits
# (``100`` -> ``100.0``). The magnitude guard keeps the ``bigint`` cast in range.
#
# ``bigint`` has no negative zero, so the sign is re-read from
# ``float8::text`` (which renders ``-0``) and re-attached. ``belief_confidence``
# is DOUBLE PRECISION under ``CHECK (0..1)``, which ``-0.0`` passes, and the
# damage lands on the ORDINARY operand: a filter for ``0.0`` would match that
# row in SQL and miss it in Python.
_REAL_TEXT: Final = (
    "(CASE WHEN {col}::float8 = trunc({col}::float8)"
    " AND abs({col}::float8) < 1e16"
    " THEN CASE WHEN {col}::float8::text = '-0' THEN '-' ELSE '' END"
    " || {col}::float8::bigint::text || '.0'"
    " ELSE {col}::float8::text END)"
)

# One session's records of one kind. Correlated on ``inquiries.id``, which is
# the row the filter is selecting; ``{col}`` is the record KIND, not a column.
_RECORDS_WHERE: Final = (
    "SELECT 1 FROM session_records r "
    "WHERE r.session_id = inquiries.id AND r.kind = '{col}'"
)

# An op is absent where the two evaluators would order differently:
# ``match_filter`` compares numerically whenever both sides parse as numbers,
# so on text Python reads ``"10" < "9"`` as 10 < 9 while SQL compares
# lexically. Adding a template without that agreement silently changes which
# rows a lowered query returns.
_SQL_BY_SHAPE: Final[dict[ColumnShape, dict[str, str]]] = {
    ColumnShape.TEXT: {
        "is": "{col} = {p}",
        "ne": "{col} IS DISTINCT FROM {p}",
        "re": "{col} ~ {p}",
        # ``NULL !~ p`` is NULL, which the WHERE drops; ``match_filter`` treats
        # NULL as absent and KEEPS the row, ``nre`` being ``re``'s complement.
        "nre": "({col} IS NULL OR {col} !~ {p})",
        "isnull": "{col} IS NULL",
        "notnull": "{col} IS NOT NULL",
    },
    ColumnShape.TEXT_ARRAY: {
        # ``label is x`` matches ANY element, mirroring ``_candidate_items``.
        # Containment preserves that behavior while selecting the GIN indexes
        # on labels, subscribers, and issue_kind.
        "is": "{col} @> ARRAY[{p}]::text[]",
        "ne": "NOT ({p} = ANY(COALESCE({col}::text[], ARRAY[]::text[])))",
        "re": "EXISTS (SELECT 1 FROM unnest({col}::text[]) AS e WHERE e ~ {p})",
        "nre": "NOT EXISTS (SELECT 1 FROM unnest({col}::text[]) AS e WHERE e ~ {p})",
        "isnull": "{col} IS NULL",
        "notnull": "{col} IS NOT NULL",
    },
    ColumnShape.UUID_ARRAY: {
        # ANY lets PostgreSQL infer UUID from the column for membership. The
        # text casts on the other ops preserve their string-filter semantics.
        "is": "{p} = ANY({col})",
        "ne": "NOT ({p} = ANY(COALESCE({col}::text[], ARRAY[]::text[])))",
        "re": "EXISTS (SELECT 1 FROM unnest({col}::text[]) AS e WHERE e ~ {p})",
        "nre": "NOT EXISTS (SELECT 1 FROM unnest({col}::text[]) AS e WHERE e ~ {p})",
        "isnull": "{col} IS NULL",
        "notnull": "{col} IS NOT NULL",
    },
    ColumnShape.INTEGER: {
        "is": "{col}::text = {p}",
        "ne": "{col}::text IS DISTINCT FROM {p}",
        "re": "{col}::text ~ {p}",
        "nre": "({col} IS NULL OR {col}::text !~ {p})",
        "lt": "{col} < {p}::numeric",
        "le": "{col} <= {p}::numeric",
        "gt": "{col} > {p}::numeric",
        "ge": "{col} >= {p}::numeric",
        "isnull": "{col} IS NULL",
        "notnull": "{col} IS NOT NULL",
    },
    # The order ops cast the COLUMN to ``float8`` first. The substrate installs
    # a ``numeric`` codec decoding to ``float`` (``lib/postgres/substrate.py``),
    # so the Python evaluator only ever holds a float: a ``numeric``-precision
    # comparison here would answer what its own row value cannot represent.
    # Live PG16 says ``0.5::numeric < '0.50000000000000001'::numeric`` is TRUE
    # and the ``::float8`` form is FALSE, and only the second is reproducible.
    # INTEGER needs no such cast -- asyncpg hands back an exact ``int``.
    ColumnShape.REAL: {
        "is": f"{_REAL_TEXT} = {{p}}",
        "ne": f"{_REAL_TEXT} IS DISTINCT FROM {{p}}",
        "re": f"{_REAL_TEXT} ~ {{p}}",
        "nre": f"({{col}} IS NULL OR {_REAL_TEXT} !~ {{p}})",
        "lt": "{col}::float8 < {p}::numeric",
        "le": "{col}::float8 <= {p}::numeric",
        "gt": "{col}::float8 > {p}::numeric",
        "ge": "{col}::float8 >= {p}::numeric",
        "isnull": "{col} IS NULL",
        "notnull": "{col} IS NOT NULL",
    },
    # No order ops: Python compares the rendered string, SQL compares the
    # uuid, and the two order differently.
    ColumnShape.RENDERED: {
        "is": "{col}::text = {p}",
        "ne": "{col}::text IS DISTINCT FROM {p}",
        "re": "{col}::text ~ {p}",
        "nre": "({col} IS NULL OR {col}::text !~ {p})",
        "isnull": "{col} IS NULL",
        "notnull": "{col} IS NOT NULL",
    },
    # The elements of one session's records of a given kind. ``{col}`` is NOT
    # a column here -- it is the record kind, quoted into the subquery by
    # ``sql_template``, drawn from the closed derived set in
    # ``session_record_fields``. A kind never reaches the query as user input.
    #
    # NO tsvector prefilter. A tsvector matches whole LEXEMES and a regex
    # matches SUBSTRINGS, so narrowing by ``search @@ plainto_tsquery`` drops
    # rows the regex keeps: measured on PG16, ``'Read\n/tmp/gamma'`` lexes as
    # ``'/tmp/gamma'``, which the tsquery ``'gamma'`` does NOT match while
    # ``~ 'gamma'`` does. The scan is bounded instead by the correlation --
    # each subquery reads one session's records of one kind, which
    # ``idx_session_records_kind`` on ``(session_id, kind)`` serves. The
    # 15.2s figure that motivated a prefilter was an UNCORRELATED regex over
    # all 3.08M rows, which no clause here issues.
    ColumnShape.SESSION_RECORD: {
        "is": f"EXISTS ({_RECORDS_WHERE} AND r.text = {{p}})",
        "ne": f"NOT EXISTS ({_RECORDS_WHERE} AND r.text = {{p}})",
        "re": f"EXISTS ({_RECORDS_WHERE} AND r.text ~ {{p}})",
        "nre": f"NOT EXISTS ({_RECORDS_WHERE} AND r.text ~ {{p}})",
        # Presence of the RECORD KIND, not of a text: "did a compact happen".
        "isnull": f"NOT EXISTS ({_RECORDS_WHERE})",
        "notnull": f"EXISTS ({_RECORDS_WHERE})",
    },
    ColumnShape.TIMESTAMP: {
        "is": f"{_TS_TEXT} = {{p}}",
        "ne": f"{_TS_TEXT} IS DISTINCT FROM {{p}}",
        "re": f"{_TS_TEXT} ~ {{p}}",
        "nre": f"({{col}} IS NULL OR {_TS_TEXT} !~ {{p}})",
        # Compared as TEXT, which agrees because the rendering is ISO-8601 in
        # a fixed zone. Casting the operand (``{p}::timestamptz``) would make
        # asyncpg infer a datetime and reject the string the CLI sends.
        "lt": f"{_TS_TEXT} < {{p}}",
        "le": f"{_TS_TEXT} <= {{p}}",
        "gt": f"{_TS_TEXT} > {{p}}",
        "ge": f"{_TS_TEXT} >= {{p}}",
        "isnull": "{col} IS NULL",
        "notnull": "{col} IS NOT NULL",
    },
}


def sql_template(column: str, op: str) -> str | None:
    """SQL for ``(column, op)``, or ``None`` when the pair cannot lower.

    ``column`` must already be canonical. ``{col}`` and ``{p}`` are the
    caller's to format: the column name (drawn from the closed set this module
    derives, never user input) and the bound placeholder. ``{p}`` may appear
    several times -- a repeated ``$N`` binds one operand, which is how the
    record shapes prefilter on the same pattern they match.

    A RECORD field has no column of its own, so its ``{col}`` is resolved
    HERE, to the record kind its subquery selects. Baking it into the template
    rather than passing it as an operand is what keeps a kind out of the
    query's parameters: the value comes from a closed derived set, never from
    the caller.

    Args:
      column: Canonical storage column name, or a record field.
      op: A ``FilterOp`` spelling.

    Returns:
      template: The SQL form, or ``None`` when no SQL selects the same rows
        the Python predicate would.

    """
    shape = COLUMN_SHAPES.get(column)
    if shape is None:
        return None
    template = _SQL_BY_SHAPE[shape].get(op)
    if template is None or shape is not ColumnShape.SESSION_RECORD:
        return template
    kind = record_kind_for(column)
    assert kind is not None, "a SESSION_RECORD shape is seeded only for a record field"
    # ``{col}`` consumed here; ``{p}`` is left for the caller to bind.
    return template.replace("{col}", kind)


def compares_as_float(column: str, op: str) -> bool:
    """Whether ordering ``column`` compares it as ``double precision``.

    Read off the TEMPLATE, like every other question about lowering: the REAL
    shape casts ``{col}::float8`` to match the codec that decodes NUMERIC to a
    Python float, and that cast is also a range ceiling the operand must clear.
    """
    template = sql_template(column, op)
    return template is not None and "::float8" in template


def requires_numeric_operand(column: str, op: str) -> bool:
    """Whether ordering ``column`` casts the operand to ``numeric`` in SQL.

    A timestamp orders by comparing TEXT, so any operand is well defined
    there; the numeric shapes cast, and Postgres rejects a non-number outright
    (``invalid input syntax for type numeric``) where Python falls back to a
    string compare. The distinction is the TEMPLATE's, so it is read off the
    template rather than inferred from the op.
    """
    template = sql_template(column, op)
    return template is not None and "::numeric" in template


def lowers_into_sql(column: str, op: str) -> bool:
    """Whether ``(column, op)`` has a SQL form, so both evaluators agree."""
    return sql_template(column, op) is not None


def _column_shapes() -> dict[str, ColumnShape]:
    """Classify every filterable column, derived from the specs.

    Derived rather than hand-listed so a new column is classified without an
    edit here, and a type change reclassifies it automatically.
    """
    # Identity/housekeeping columns the schema declares directly. They carry
    # no ColumnSpec, so the spec walk below cannot see them -- and they are
    # among the most filtered (``created``, ``seq``). Mirrors
    # ``grammar._IDENTITY_FILTER_COLUMNS``.
    out: dict[str, ColumnShape] = {
        "kind": ColumnShape.TEXT,
        "seq": ColumnShape.INTEGER,
        "id": ColumnShape.RENDERED,
        "created": ColumnShape.TIMESTAMP,
        "modified": ColumnShape.TIMESTAMP,
    }
    # IR record kinds, seeded for the same reason as the identity columns
    # above: ``_classify`` reads a ``ColumnSpec.sql_type``, and a record field
    # has no column on ``inquiries`` at all -- its values live in
    # ``session_records``. The SET is still derived (from which record classes
    # project any ``text``), so a new record class needs no edit here.
    out.update(dict.fromkeys(SESSION_RECORD_FIELDS, ColumnShape.SESSION_RECORD))
    for source in INQUIRY_CLASSES:
        for name, flat in flat_column_specs(source).items():
            column = storage_name(name, flat.spec)
            if (shape := _classify(flat)) is not None:
                out[column] = shape
    return out


def _filterable_columns() -> frozenset[str]:
    """Every column a filter may name, whether or not it lowers.

    A SUPERSET of :data:`COLUMN_SHAPES`, which holds only what has SQL: the
    JSONB payload is filterable and has no shape, so classifying is the wrong
    question to ask about a field's EXISTENCE. Deriving both from one walk is
    what keeps them from disagreeing about which columns are real.
    """
    return frozenset(COLUMN_SHAPES) | {
        storage_name(name, flat.spec)
        for source in INQUIRY_CLASSES
        for name, flat in flat_column_specs(source).items()
    }


def _classify(flat: FlatColumn) -> ColumnShape | None:
    """Shape for one column, or ``None`` when it cannot lower.

    A flattened axis (the ``marginal_cost_*`` pair) carries the COMPOSITE's
    empty ``sql_type``, so the declared type says nothing; its Python
    annotation does, and both axes are floats.
    """
    sql_type = flat.spec.sql_type
    if sql_type == "TEXT[]":
        return ColumnShape.TEXT_ARRAY
    if sql_type == "UUID[]":
        return ColumnShape.UUID_ARRAY
    if sql_type.endswith("[]"):
        return None
    if _is_integer_sql(sql_type) or _is_valued(flat.value_type, int):
        return ColumnShape.INTEGER
    if _is_real_sql(sql_type) or _is_valued(flat.value_type, float):
        return ColumnShape.REAL
    if sql_type == "UUID":
        return ColumnShape.RENDERED
    if sql_type == "TIMESTAMPTZ":
        return ColumnShape.TIMESTAMP
    if sql_type == "TEXT" and _is_valued(flat.value_type, str):
        return ColumnShape.TEXT
    # JSONB is the one shape left in Python: ``str(dict)`` is a Python repr
    # with single quotes, which no SQL rendering reproduces.
    return None


def _sql_head(sql_type: str) -> str:
    """A declared type minus any precision, so ``NUMERIC(14, 6)`` reads as one.

    Keeps multi-word spellings intact (``DOUBLE PRECISION``), which the sets
    below match verbatim.
    """
    return sql_type.split("(", maxsplit=1)[0].strip().upper()


def _is_integer_sql(sql_type: str) -> bool:
    """Whether a declared type holds whole numbers.

    Postgres' whole vocabulary for the type, not the subset declared today:
    ``INT`` / ``INT4`` name the SAME type, and an unlisted spelling gets no
    shape at all -- silently dropping the column's order ops and sending its
    regex to Python, a wrong answer rather than a missing feature.
    """
    return _sql_head(sql_type) in {
        "INTEGER",
        "INT",
        "INT2",
        "INT4",
        "INT8",
        "BIGINT",
        "SMALLINT",
    }


def _is_real_sql(sql_type: str) -> bool:
    """Whether a declared type holds fractional numbers.

    The full vocabulary, like :func:`_is_integer_sql`: ``FLOAT8`` and ``FLOAT``
    name the same type as ``DOUBLE PRECISION``.
    """
    return _sql_head(sql_type) in {
        "NUMERIC",
        "DECIMAL",
        "REAL",
        "FLOAT",
        "FLOAT4",
        "FLOAT8",
        "DOUBLE",
        "DOUBLE PRECISION",
    }


def _is_valued(annotation: object, target: type) -> bool:
    """Whether every value a column can hold is a ``target``.

    Unwraps the two indirections the model uses: a ``type X = ...`` alias
    (``Actor``, ``Status``) hides its real type behind ``__value__``, and a
    nullable column is ``X | None``. An identity comparison misses both, and a
    flattened axis has ONLY its annotation, its spec carrying the composite's
    empty ``sql_type``.

    ``bool`` never counts as ``int``: it is a subclass, but comparing a flag
    as a number is not what any filter means.
    """
    if annotation is bool:
        return False
    if annotation is target:
        return True
    if get_origin(annotation) is Literal:
        return all(
            isinstance(arg, target)
            for arg in get_args(annotation)
            if arg is not type(None)
        )
    if (aliased := getattr(annotation, "__value__", None)) is not None:
        return _is_valued(aliased, target)
    args = [arg for arg in get_args(annotation) if arg is not type(None)]
    return bool(args) and all(_is_valued(arg, target) for arg in args)


COLUMN_SHAPES: Final[dict[str, ColumnShape]] = _column_shapes()

FILTERABLE_COLUMNS: Final[frozenset[str]] = _filterable_columns()
"""Every column a filter may name; a superset of :data:`COLUMN_SHAPES`."""
