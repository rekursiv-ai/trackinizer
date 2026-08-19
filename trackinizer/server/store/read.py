""":class:`_ReadMixin` -- read-only inquiry, cost, and change queries.

A pure leaf: :meth:`get_inquiry`, :meth:`list_kind`, :meth:`next_issue`,
:meth:`cost_for`, :meth:`proves_belief`, :meth:`what_changed_for_me`,
:meth:`get_change`, and :meth:`list_changes` all read through
``self.engine`` and call no other mixin.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Literal, cast, get_args, get_origin
from uuid import UUID

from trackinizer.server.projection import (
    fetch_edges,
    fetch_edges_bulk,
    materialize,
)
from trackinizer.server.sql_fragments import (
    _COST_SUBTREE_SQL,
    _NEXT_ISSUE_SQL,
    _PROVES_BELIEF_SQL,
)
from trackinizer.server.store.shared import _StoreShared
from trackinizer.server.values import vetted_sql
from trackinizer.types.change_log import Change
from trackinizer.types.columns import (
    FlatColumn,
    flat_column_specs,
    storage_name,
)
from trackinizer.types.cost import Cost
from trackinizer.types.errors import NotFoundError
from trackinizer.types.inquiries import KIND_TO_CLASS, Inquiry, Issue
from trackinizer.wire.filters import canonical_filter_field
from trackinizer.wire.row_filter import RowFilter, match_filter
from trackinizer.wire.seq_ranges import SeqRange


__all__ = [
    "_ReadMixin",
    "_seq_range_clause",
]


@dataclass(frozen=True, kw_only=True, slots=True)
class Lowering:
    """Whether filters may be pushed into SQL.

    Disabled only by the equivalence tests, which run the same query through
    both evaluators and compare the rows. A field rather than a bare module
    flag so the test's override is obvious at the assignment site.
    """

    enabled: bool = True


LOWERING: Lowering = Lowering()


class _ColumnShape(StrEnum):
    """How a column's values compare, which decides its SQL form."""

    TEXT = "text"
    """A scalar string: ``=`` and the regex operators apply directly."""

    ARRAY = "array"
    """A list column, where ``is`` means MEMBERSHIP, not equality."""

    INTEGER = "integer"
    """A whole number. ``::text`` renders it exactly as Python's ``str``, and
    the order ops compare numerically in both evaluators."""

    REAL = "real"
    """A fractional number, whose rendering the two disagree on by default.

    ``match_filter`` compares ``str(value)``, so ``0.5`` must render ``0.5``
    -- but a ``NUMERIC(14, 6)`` column's ``::text`` is ``0.500000`` and a
    float's ``str(100.0)`` is ``100.0`` against SQL's ``100``. The template in
    :data:`_SQL_BY_SHAPE` restores Python's shape: cast to ``float8`` (which
    drops NUMERIC's trailing zeros) and re-append ``.0`` to a whole value."""

    RENDERED = "rendered"
    """A non-text scalar whose SQL ``::text`` rendering equals Python's
    ``str()`` -- UUID today. ``match_filter`` stringifies the column value, so
    only a type whose two renderings agree can lower, and it lowers through
    the same ``::text`` cast the predicate implies."""

    TIMESTAMP = "timestamp"
    """An instant, rendered to match ``str(datetime)`` exactly.

    A bare ``::text`` will NOT do: it renders in the session time zone with a
    ``-08`` style offset, where Python prints ``+00:00``. The template in
    :data:`_SQL_BY_SHAPE` forces UTC and reproduces Python's microsecond rule
    (omitted when zero, else six digits).

    This holds because asyncpg hands back UTC-aware datetimes whatever the
    stored offset -- verified by round-tripping a ``-07:00`` value, which
    reads back as ``+00:00`` -- so the predicate never sees a local offset
    that SQL would normalize away."""


def _column_shapes() -> dict[str, _ColumnShape]:
    """Classify every filterable column, derived from the specs.

    Derived rather than hand-listed so a new column is classified without an
    edit here, and a type change reclassifies it automatically.
    """
    # Identity/housekeeping columns the schema declares directly. They carry
    # no ColumnSpec, so the spec walk below cannot see them -- and they are
    # among the most filtered (``created``, ``seq``). Mirrors
    # ``grammar._IDENTITY_FILTER_COLUMNS``.
    out: dict[str, _ColumnShape] = {
        "kind": _ColumnShape.TEXT,
        "seq": _ColumnShape.INTEGER,
        "id": _ColumnShape.RENDERED,
        "created": _ColumnShape.TIMESTAMP,
        "modified": _ColumnShape.TIMESTAMP,
    }
    for source in (Inquiry, *KIND_TO_CLASS.values()):
        for name, flat in flat_column_specs(source).items():
            column = storage_name(name, flat.spec)
            if (shape := _classify(column, flat)) is not None:
                out[column] = shape
    return out


def _classify(column: str, flat: FlatColumn) -> _ColumnShape | None:
    """Shape for one column, or ``None`` when it cannot lower.

    A flattened axis (the ``marginal_cost_*`` pair) carries the COMPOSITE's
    empty ``sql_type``, so the declared type says nothing; its Python
    annotation does, and both axes are floats.
    """
    sql_type = flat.spec.sql_type
    if sql_type.endswith("[]"):
        return _ColumnShape.ARRAY
    if _is_integer_sql(sql_type) or _is_valued(flat.value_type, int):
        return _ColumnShape.INTEGER
    if _is_real_sql(sql_type) or _is_valued(flat.value_type, float):
        return _ColumnShape.REAL
    if sql_type == "UUID":
        return _ColumnShape.RENDERED
    if sql_type == "TIMESTAMPTZ":
        return _ColumnShape.TIMESTAMP
    if sql_type == "TEXT" and _is_text_valued(flat.value_type):
        return _ColumnShape.TEXT
    # JSONB is the one shape left in Python: ``str(dict)`` is a Python repr
    # with single quotes, which no SQL rendering reproduces.
    del column
    return None


def _sql_head(sql_type: str) -> str:
    """Leading word of a declared type, so ``NUMERIC(14, 6)`` reads as one."""
    return sql_type.split("(", maxsplit=1)[0].strip().upper()


def _is_integer_sql(sql_type: str) -> bool:
    """Whether a declared type holds whole numbers."""
    return _sql_head(sql_type) in {"INTEGER", "BIGINT", "SMALLINT"}


def _is_real_sql(sql_type: str) -> bool:
    """Whether a declared type holds fractional numbers."""
    return _sql_head(sql_type) in {
        "NUMERIC",
        "DECIMAL",
        "REAL",
        "DOUBLE",
        "DOUBLE PRECISION",
    }


def _is_text_valued(annotation: object) -> bool:
    """Whether a column's values are plain strings in Python."""
    if get_origin(annotation) is Literal:
        return all(
            isinstance(arg, str)
            for arg in get_args(annotation)
            if arg is not type(None)
        )
    return _is_valued(annotation, str)


def _is_valued(annotation: object, target: type) -> bool:
    """Whether every value a column can hold is a ``target``.

    Unwraps the two indirections the model uses: a ``type X = ...`` alias
    (``Actor``, ``Status``) hides its real type behind ``__value__``, and a
    nullable column is ``X | None``. Comparing the annotation to the type by
    identity misses both, which silently excludes the columns most worth
    lowering -- and a flattened axis has ONLY its annotation, since its spec
    carries the composite's empty ``sql_type``.

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


_COLUMN_SHAPES: dict[str, _ColumnShape] = _column_shapes()

# Render a ``timestamptz`` exactly as Python's ``str(datetime)`` does: UTC,
# space separator, ``+00:00`` offset, and microseconds only when non-zero.
# ``match_filter`` compares that string, so any other rendering would make the
# two evaluators disagree on every timestamp filter.
_TS_TEXT: Final = (
    "(to_char({col} AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')"
    " || CASE WHEN date_part('microsecond', {col})::int % 1000000 = 0 THEN ''"
    " ELSE '.' || lpad((date_part('microsecond', {col})::int % 1000000)::text,"
    " 6, '0') END || '+00:00')"
)

# Render a fractional number the way Python's ``str(float)`` does. The
# ``float8`` cast drops the trailing zeros a ``NUMERIC(p, s)`` column prints
# (``0.500000`` -> ``0.5``); re-appending ``.0`` to a whole value restores the
# part Postgres omits (``100`` -> ``100.0``). The magnitude guard keeps the
# ``bigint`` cast in range and leaves exponent-form values (``1e+20``) to
# Postgres, which already spells them as Python does.
_REAL_TEXT: Final = (
    "(CASE WHEN {col}::float8 = trunc({col}::float8)"
    " AND abs({col}::float8) < 1e16"
    " THEN {col}::float8::bigint::text || '.0'"
    " ELSE {col}::float8::text END)"
)

# SQL for each op, per column shape. ``{col}`` is the column name (drawn from
# the closed set above, never user input) and ``{p}`` the bound placeholder.
#
# The order ops appear ONLY under NUMERIC: ``match_filter`` compares
# numerically whenever both sides parse as numbers, so on a text column
# Python reads ``"10" < "9"`` as 10 < 9 while SQL compares them lexically --
# measured, and the two disagree. Restricting them to numeric columns is what
# keeps the evaluators identical.
_SQL_BY_SHAPE: dict[_ColumnShape, dict[str, str]] = {
    _ColumnShape.TEXT: {
        "is": "{col} = {p}",
        "ne": "{col} IS DISTINCT FROM {p}",
        "re": "{col} ~ {p}",
        "nre": "{col} !~ {p}",
        "isnull": "{col} IS NULL",
        "notnull": "{col} IS NOT NULL",
    },
    _ColumnShape.ARRAY: {
        # ``label is x`` matches ANY element, mirroring ``_candidate_items``.
        "is": "{p} = ANY({col})",
        "ne": "NOT ({p} = ANY(COALESCE({col}, ARRAY[]::text[])))",
        "re": "EXISTS (SELECT 1 FROM unnest({col}) AS e WHERE e ~ {p})",
        "nre": "NOT EXISTS (SELECT 1 FROM unnest({col}) AS e WHERE e ~ {p})",
        "isnull": "{col} IS NULL",
        "notnull": "{col} IS NOT NULL",
    },
    _ColumnShape.INTEGER: {
        "is": "{col}::text = {p}",
        "ne": "{col}::text IS DISTINCT FROM {p}",
        "re": "{col}::text ~ {p}",
        "nre": "{col}::text !~ {p}",
        "lt": "{col} < {p}::numeric",
        "le": "{col} <= {p}::numeric",
        "gt": "{col} > {p}::numeric",
        "ge": "{col} >= {p}::numeric",
        "isnull": "{col} IS NULL",
        "notnull": "{col} IS NOT NULL",
    },
    _ColumnShape.REAL: {
        "is": f"{_REAL_TEXT} = {{p}}",
        "ne": f"{_REAL_TEXT} IS DISTINCT FROM {{p}}",
        "re": f"{_REAL_TEXT} ~ {{p}}",
        "nre": f"{_REAL_TEXT} !~ {{p}}",
        # Ordering compares NUMBERS in both evaluators, so it needs none of
        # the rendering above.
        "lt": "{col} < {p}::numeric",
        "le": "{col} <= {p}::numeric",
        "gt": "{col} > {p}::numeric",
        "ge": "{col} >= {p}::numeric",
        "isnull": "{col} IS NULL",
        "notnull": "{col} IS NOT NULL",
    },
    # A UUID renders identically in both evaluators (verified), so it lowers
    # through the ``::text`` cast ``match_filter``'s ``str(value)`` implies.
    # The order ops are absent: Python would compare the rendered strings,
    # and SQL would compare UUIDs, which order differently.
    _ColumnShape.RENDERED: {
        "is": "{col}::text = {p}",
        "ne": "{col}::text IS DISTINCT FROM {p}",
        "re": "{col}::text ~ {p}",
        "nre": "{col}::text !~ {p}",
        "isnull": "{col} IS NULL",
        "notnull": "{col} IS NOT NULL",
    },
    _ColumnShape.TIMESTAMP: {
        "is": f"{_TS_TEXT} = {{p}}",
        "ne": f"{_TS_TEXT} IS DISTINCT FROM {{p}}",
        "re": f"{_TS_TEXT} ~ {{p}}",
        "nre": f"{_TS_TEXT} !~ {{p}}",
        # Compared as TEXT, not as instants. ``match_filter`` finds neither
        # side numeric and falls through to a string compare of the rendered
        # value, so SQL must compare the same rendering. Casting the operand
        # (``{p}::timestamptz``) would also make asyncpg infer a datetime
        # parameter and reject the string the CLI actually sends.
        #
        # The two agree because this rendering is ISO-8601 in a fixed zone,
        # where lexical order IS chronological order.
        "lt": f"{_TS_TEXT} < {{p}}",
        "le": f"{_TS_TEXT} <= {{p}}",
        "gt": f"{_TS_TEXT} > {{p}}",
        "ge": f"{_TS_TEXT} >= {{p}}",
        "isnull": "{col} IS NULL",
        "notnull": "{col} IS NOT NULL",
    },
}

# Ops that test presence and carry no operand, so they bind no parameter.
_VALUELESS_OPS: frozenset[str] = frozenset({"isnull", "notnull"})


def _lower_filter(filt: RowFilter, params: list[object]) -> str | None:
    """Render one filter as a SQL clause, or ``None`` if it cannot lower.

    Appends the operand to ``params`` (keeping positional placeholders in
    lockstep with the caller's list) only when the clause actually takes one.
    """
    column = canonical_filter_field(filt.field)
    shape = _COLUMN_SHAPES.get(column)
    if shape is None:
        return None
    template = _SQL_BY_SHAPE[shape].get(filt.op)
    if template is None:
        return None
    if filt.op in _VALUELESS_OPS:
        return template.format(col=column, p="")
    params.append(filt.value)
    return template.format(col=column, p=f"${len(params)}")


def _partition_filters(
    filters: Sequence[RowFilter], params: list[object]
) -> tuple[Sequence[str], Sequence[RowFilter]]:
    """Split ``filters`` into SQL clauses and clauses Python must still run.

    Returns ``(clauses, remaining)``. The caller may only push ``LIMIT`` into
    SQL when ``remaining`` is empty: a predicate still evaluated in Python
    must run before the window, or matches past the limit are dropped unseen
    (Issue#256).
    """
    if not LOWERING.enabled:
        return (), list(filters)
    clauses: list[str] = []
    remaining: list[RowFilter] = []
    for filt in filters:
        clause = _lower_filter(filt, params)
        if clause is None:
            remaining.append(filt)
        else:
            clauses.append(clause)
    return clauses, remaining


def _seq_range_clause(
    params: list[object], seq_ranges: Sequence[SeqRange]
) -> str | None:
    """Lower a seq-range union to one parenthesized ``OR`` group, or ``None``.

    Appends each interval's present bounds to ``params`` (so positional
    placeholders stay in lockstep with the caller's running list) and
    returns the ``(... OR ...)`` clause. Empty ``seq_ranges`` returns
    ``None`` so the caller omits the clause entirely. Shared by every
    ``seq``-windowed reader (:meth:`Store.list_kind`,
    :meth:`Store.read_session_events`) so the disjoint-union lowering has
    exactly one implementation.
    """
    if not seq_ranges:
        return None
    disjuncts: list[str] = []
    for interval in seq_ranges:
        bounds: list[str] = []
        if interval.start is not None:
            params.append(interval.start)
            bounds.append(f"seq >= ${len(params)}")
        if interval.stop is not None:
            params.append(interval.stop)
            bounds.append(f"seq <= ${len(params)}")
        # The wire rejects bare ``..``, so ``bounds`` is never empty.
        disjuncts.append("(" + " AND ".join(bounds) + ")")
    return "(" + " OR ".join(disjuncts) + ")"


class _ReadMixin(_StoreShared):
    """Read-only inquiry, cost, and change queries for :class:`Store`."""

    async def get_inquiry(self, target_id: UUID) -> Inquiry | None:
        """Fetch any Inquiry by id; subclass dispatched from ``kind`` column."""
        async with self.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM inquiries WHERE id = $1",
                target_id,
            )
            if row is None:
                return None
            outbound, inbound = await fetch_edges(conn, target_id)
        return materialize(row, {target_id: outbound}, {target_id: inbound})

    async def list_kind(
        self,
        kind: Inquiry.InquiryKind,
        *,
        status: Inquiry.Status | None = None,
        limit: int = 50,
        offset: int = 0,
        seq_ranges: Sequence[SeqRange] = (),
        filters: Sequence[RowFilter] = (),
    ) -> list[Inquiry]:
        """Paginated list of one kind, optionally filtered.

        ``filters`` is the canonical filter pipeline shared with the
        trax CLI: every clause is evaluated before ``LIMIT``/``OFFSET``
        is applied, so a matching row is never silently dropped
        because it falls outside the natural recency window. Status /
        seq-range filters that lower cleanly into SQL are folded into
        the SELECT for efficiency; the rest are materialized and
        post-filtered in-process via :func:`match_filter`. The full
        scan is acceptable at this project's row-count (trackinizer is
        a personal/team inquiry log; rows are O(thousands)). If that
        ever changes, add SQL lowering for the hot ops without
        changing the contract here.

        ``seq_ranges`` is a union of inclusive intervals: the row's
        ``seq`` must fall in at least one. The intervals lower to one
        parenthesized ``OR`` group over the ``(kind, seq)`` index, so a
        disjoint selection (``222..260,279..``) is one indexed query, not
        one round-trip per interval.
        """
        params: list[object] = [kind]
        clauses = ["kind = $1"]
        if status is not None:
            params.append(status)
            clauses.append(f"status = ${len(params)}")
        if (seq_clause := _seq_range_clause(params, seq_ranges)) is not None:
            clauses.append(seq_clause)
        # Every filter whose SQL form provably selects the same rows as the
        # Python predicate joins the prefilter, so the query keeps its LIMIT.
        # Whatever cannot lower stays in ``filters`` and forces the
        # post-filter path below.
        lowered, filters = _partition_filters(filters, params)
        clauses.extend(lowered)
        # ``id`` tie-breaks rows that share a ``created`` timestamp (always
        # true in a single transaction, sometimes true across them) so
        # offset pagination doesn't drop or duplicate rows under
        # concurrent inserts.
        order_sql = "ORDER BY created DESC, id DESC"
        if filters:
            # Post-filter pipeline: pull every row matching the SQL
            # prefilter (kind / status / seq-range), apply each clause
            # in-process, then window. This is the only shape that
            # keeps ``LIMIT`` honest -- pushing it into the SQL would
            # re-introduce the pre-filter truncation bug (Issue#256).
            #
            # One acquire spans both fetches so the row set and the
            # edge bulk-fetch hit the same pooled connection back to
            # back; a concurrent ``purge`` then can't slip in between
            # and leave us materializing edges for a row that no
            # longer exists. The non-filter branch already shared one
            # acquire for the same reason.
            sql = vetted_sql(
                "SELECT * FROM inquiries WHERE ",
                " AND ".join(clauses),
                " ",
                order_sql,
            )
            async with self.engine.acquire() as conn:
                rows = await conn.fetch(sql, *params)
                kept = [r for r in rows if all(match_filter(r, f) for f in filters)]
                window = kept[offset : offset + limit]
                outbound, inbound = await fetch_edges_bulk(
                    conn, [r["id"] for r in window]
                )
            return [materialize(row, outbound, inbound) for row in window]
        params.extend([limit, offset])
        sql = vetted_sql(
            "SELECT * FROM inquiries WHERE ",
            " AND ".join(clauses),
            " ",
            order_sql,
            " LIMIT $",
            str(len(params) - 1),
            " OFFSET $",
            str(len(params)),
        )
        async with self.engine.acquire() as conn:
            rows = await conn.fetch(sql, *params)
            outbound, inbound = await fetch_edges_bulk(conn, [r["id"] for r in rows])
        return [materialize(row, outbound, inbound) for row in rows]

    async def next_issue(self) -> Issue | None:
        """Return the next active Issue whose prerequisites are all terminal."""
        async with self.engine.acquire() as conn:
            row = await conn.fetchrow(_NEXT_ISSUE_SQL)
            if row is None:
                return None
            rid = row["id"]
            outbound, inbound = await fetch_edges(conn, rid)
        return cast(Issue, materialize(row, {rid: outbound}, {rid: inbound}))

    async def cost_for(self, subject_id: UUID, *, deep: bool = False) -> Cost | None:
        """Return the running cost for one inquiry, or ``None`` if missing.

        ``deep=True`` rolls up the Issue decomposition subtree via
        ``cost_subtree.sql``; ``deep=False`` reads the row's own
        running totals. Both paths return ``None`` for an unknown id so
        callers can distinguish "no such row" from "zero recorded cost".
        """
        async with self.engine.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT 1 FROM inquiries WHERE id = $1", subject_id
            )
            if exists is None:
                return None
            if deep:
                row = await conn.fetchrow(_COST_SUBTREE_SQL, subject_id)
            else:
                row = await conn.fetchrow(
                    "SELECT marginal_cost_agent_usd AS agent_usd, "
                    "marginal_cost_resource_usd AS resource_usd "
                    "FROM inquiries WHERE id = $1",
                    subject_id,
                )
        if row is None:
            return Cost()
        return Cost(
            agent_usd=float(row["agent_usd"]),
            resource_usd=float(row["resource_usd"]),
        )

    async def proves_belief(self, belief_id: UUID) -> list[Inquiry]:
        """Active load-bearing ``proves`` Artifacts for ``belief_id``.

        Each returned row is projected through :func:`fetch_edges_bulk`
        so its own provenance / citation fields are populated -- callers
        can drill into evidence chains without re-fetching.
        """
        async with self.engine.acquire() as conn:
            rows = await conn.fetch(_PROVES_BELIEF_SQL, belief_id)
            outbound, inbound = await fetch_edges_bulk(conn, [r["id"] for r in rows])
        return [materialize(row, outbound, inbound) for row in rows]

    async def what_changed_for_me(
        self,
        agent: Inquiry.Actor,
        since: datetime,
        *,
        after_id: UUID | None = None,
        limit: int = 200,
    ) -> list[Change]:
        """Changes since ``(since, after_id)`` for ``agent``, paginated.

        The cursor is ``(created, id)``: cursor predicate is
        ``(c.created, c.id) > ($since, $after_id)``. Reading
        ``subscribers_snapshot`` keeps ``purged`` events visible
        because the subject row no longer exists to join against.

        Args:
          agent: Subscriber id to filter on.
          since: Lower bound on ``created``; combined with ``after_id``
            forms a (timestamp, id) cursor.
          after_id: Tie-breaker id for changes that share ``since``'s
            timestamp. Use the last id from the previous page; omit on
            the first page.
          limit: Maximum rows to return (1-1000). Default 200.

        Returns:
          changes: Up to ``limit`` matching :class:`Change` rows ordered
            by ``(created, id)`` ascending.

        """
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be in [1, 1000]")
        # Single (created, id) tuple cursor for both first and
        # subsequent pages. The first-page form previously used
        # ``c.created > $1`` which dropped rows tied at exactly
        # ``since`` -- a caller polling with the last-seen ``created``
        # would miss those ties because with ``clock_timestamp()`` a
        # multi-row transaction shares one timestamp. The tuple
        # comparison with a min-UUID sentinel on page one mirrors the
        # next-page semantics exactly minus the tie.
        cursor_id = after_id if after_id is not None else UUID(int=0)
        async with self.engine.acquire() as conn:
            rows = await conn.fetch(
                "SELECT c.* FROM change_log c "
                "WHERE (c.created, c.id) > ($1, $2) "
                "AND $3 = ANY(c.subscribers_snapshot) "
                "ORDER BY c.created, c.id LIMIT $4",
                since,
                cursor_id,
                agent,
                limit,
            )
        return [Change.from_row(r) for r in rows]

    async def get_change(self, change_id: UUID) -> Change | None:
        """Fetch one ``change_log`` row by id; ``None`` when absent."""
        async with self.engine.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM change_log WHERE id = $1", change_id
            )
        return None if row is None else Change.from_row(row)

    async def list_changes(
        self,
        *,
        since: datetime | None = None,
        after_id: UUID | None = None,
        actor: Inquiry.Actor | None = None,
        subject_id: UUID | None = None,
        subject_kind: Inquiry.InquiryKind | None = None,
        kind: Change.Kind | None = None,
        limit: int = 200,
    ) -> list[Change]:
        """Filtered, newest-first ``change_log`` slice.

        Backs ``GET /api/change_log`` (``docs/api.md`` 1.14-1.15). Every
        filter is optional and ANDed; ``since`` is an inclusive lower
        bound on ``created`` and ``after_id`` an exclusive id cursor for
        stable pagination within one timestamp.

        Args:
          since: Inclusive lower bound on ``created``.
          after_id: Exclusive id cursor; rows with this id or earlier in
            the ``(created DESC, id DESC)`` order are skipped.
          actor: Filter on the change author.
          subject_id: Filter on the mutated row id.
          subject_kind: Filter on the mutated row kind.
          kind: Filter on the change discriminator.
          limit: Maximum rows (1-1000). Default 200.

        Returns:
          changes: Up to ``limit`` matching :class:`Change` rows ordered
            by ``(created, id)`` descending.

        """
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be in [1, 1000]")
        async with self.engine.acquire() as conn:
            clauses: list[str] = []
            args: list[object] = []
            for column, value in (
                ("created >=", since),
                ("actor =", actor),
                ("subject_id =", subject_id),
                ("subject_kind =", subject_kind),
                ("kind =", kind),
            ):
                if value is not None:
                    args.append(value)
                    clauses.append(f"{column} ${len(args)}")
            if after_id is not None:
                # The page order is ``(created, id)`` descending, so the cursor
                # must compare the same tuple: a bare ``id < after_id`` skips
                # older rows with larger UUIDs and dupes newer rows with
                # smaller ones (UUID order is unrelated to ``created``). Look
                # up the cursor row's ``created`` and bind both halves.
                cursor_created = await conn.fetchval(
                    "SELECT created FROM change_log WHERE id = $1", after_id
                )
                if cursor_created is None:
                    # An unknown cursor would make ``(created, id) < (NULL, ...)``
                    # NULL for every row -> a silent empty page that a pager
                    # mistakes for "end of results". 404 instead.
                    raise NotFoundError(f"change {after_id} not found")
                args.append(cursor_created)
                created_param = len(args)
                args.append(after_id)
                clauses.append(f"(created, id) < (${created_param}, ${len(args)})")
            where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
            args.append(limit)
            rows = await conn.fetch(
                vetted_sql(
                    "SELECT * FROM change_log ",
                    where,
                    "ORDER BY created DESC, id DESC LIMIT $",
                    str(len(args)),
                ),
                *args,
            )
        return [Change.from_row(r) for r in rows]
