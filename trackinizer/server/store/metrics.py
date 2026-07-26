""":class:`_MetricsMixin` -- experiment-metric log and read-back.

Owns the append/read seam for step-grained metric points logged against an
:class:`Experiment` run: :meth:`log_metrics` and :meth:`read_metrics`. A
pure leaf like :class:`_ReadMixin` -- it reads and writes through
``self.engine`` and calls no other mixin. Metrics are telemetry, not a
knowledge mutation, so this path emits no ``change_log`` audit, no cost, and
no ``LISTEN/NOTIFY`` fanout (the same exemption ``agent_session_events``
takes).
"""

from __future__ import annotations

from collections.abc import Sequence
from math import isfinite
from typing import Literal, cast
from uuid import UUID

import asyncpg

from trackinizer.lib.postgres import Conn
from trackinizer.server.notify import tx
from trackinizer.server.store.shared import _StoreShared
from trackinizer.server.values import vetted_sql
from trackinizer.types.errors import ConflictError, NotFoundError
from trackinizer.wire.routes import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
)
from trackinizer.wire.wire_metrics import MetricPoint
from trackinizer.wire.wire_metrics_query import (
    METRIC_COMPARE_OPS,
    MetricMaskClause,
)


__all__ = [
    "_MetricsMixin",
]


# The six ``FilterOp`` comparators a metric axis supports, mapped to their SQL
# operator. ``re`` / ``nre`` (text regex) and ``isnull`` / ``notnull``
# (presence) are absent: a metric axis is neither text-regex-matchable nor
# nullable (every column is NOT NULL in the PK / CHECK-guarded grid), so those
# ops are rejected rather than silently mistranslated. ``is`` is equality,
# uniform with the inquiry filter grammar.
_OP_TO_SQL: dict[
    str, str
] = {  # config-globals: ignore -- FilterOp->SQL operator map (structural), not a tunable
    "is": "=",
    "ne": "<>",
    "lt": "<",
    "le": "<=",
    "gt": ">",
    "ge": ">=",
}

# The SQL map must cover exactly the wire's metric comparator set: a mismatch
# (an op in one but not the other) is the drift class this asserts away at
# import. ``METRIC_COMPARE_OPS`` is the single definition; if it gains an op,
# this fails until the SQL symbol is added here too.
assert set(_OP_TO_SQL) == set(METRIC_COMPARE_OPS), (
    "metric SQL operator map drifted from METRIC_COMPARE_OPS: "
    f"{set(_OP_TO_SQL) ^ set(METRIC_COMPARE_OPS)}"
)


# Each grid axis and the SQL cast its bound operand takes: ``key`` compares as
# text, ``step`` as bigint (the storage column), ``value`` as float8. Casting
# the parameter (not the column) keeps the predicate index-eligible on the
# ``(experiment_id, key, step)`` PK.
_AXIS_CAST: dict[
    str, str
] = {  # config-globals: ignore -- axis->SQL cast map (structural, tied to storage columns), not a tunable
    "key": "text",
    "step": "bigint",
    "value": "float8",
}


class _MetricsMixin(_StoreShared):
    """Experiment-metric log and read-back for :class:`Store`."""

    async def log_metrics(
        self,
        experiment_id: UUID,
        points: Sequence[MetricPoint],
    ) -> tuple[int, int]:
        """Append metric points idempotently; return ``(logged, skipped)``.

        The storage seam for experiment-metric ingest (the wandb
        ``log({key: value}, step=)`` analogue). Points attach only to an
        existing ``Experiment`` row; ``(experiment_id, key, step)`` is the
        dedup key, so a retried batch ``ON CONFLICT DO NOTHING``s and reports
        ``logged=0``.

        ``ON CONFLICT DO NOTHING RETURNING`` reports exactly the rows this
        call newly wrote, so ``logged`` and ``skipped`` are exact even under a
        concurrent same-experiment appender (no count-subtraction race) --
        mirroring :meth:`Store.append_events`.

        Raises:
          NotFoundError: ``experiment_id`` is not an existing inquiry.
          ConflictError: ``experiment_id`` is not an ``Experiment`` row (a
            metric may only attach to an experiment).

        """
        if not points:
            return (0, 0)
        rows = [
            (experiment_id, p.key, p.step, p.value, p.kind, p.timestamp) for p in points
        ]
        # One transaction so the kind check and the insert are atomic: a
        # concurrent metric write cannot slip points under a non-experiment
        # guard between the check and the insert. No ``notify_after_commit``:
        # metrics are telemetry, not an audited mutation, so nothing subscribes.
        async with self.engine.acquire() as conn, tx(conn):
            # Read kind under the row lock so a concurrent purge can't slip
            # between the check and the insert: points attach only to an
            # Experiment.
            experiment = await conn.fetchrow(
                "SELECT kind FROM inquiries WHERE id = $1 FOR UPDATE",
                experiment_id,
            )
            if experiment is None:
                raise NotFoundError(f"experiment {experiment_id} not found")
            if experiment["kind"] != "Experiment":
                raise ConflictError(
                    f"inquiry {experiment_id} is not an Experiment "
                    f"(kind={experiment['kind']!r}); "
                    "metrics may only attach to an experiment"
                )
            # A single ``unnest`` INSERT (not ``executemany``, which cannot
            # RETURNING) makes the accounting exact and race-free: a concurrent
            # same-experiment commit cannot inflate the count because we count
            # returned rows, not a whole-experiment ``count(*)`` delta.
            try:
                inserted = await conn.fetch(
                    "INSERT INTO experiment_metrics "
                    "(experiment_id, key, step, value, kind, timestamp) "
                    "SELECT * FROM unnest("
                    "$1::uuid[], $2::text[], $3::bigint[], $4::float8[], "
                    "$5::text[], $6::timestamptz[]) "
                    "ON CONFLICT (experiment_id, key, step) DO NOTHING "
                    "RETURNING step",
                    [r[0] for r in rows],
                    [r[1] for r in rows],
                    [r[2] for r in rows],
                    [r[3] for r in rows],
                    [r[4] for r in rows],
                    [r[5] for r in rows],
                )
            except asyncpg.ForeignKeyViolationError as exc:
                # The ``experiment_id`` FK failed: the row was purged in the
                # race window between the kind check and the insert. It is
                # gone, so this is a clean 404 -- not a raw 409 leaking the
                # constraint name.
                raise NotFoundError(f"experiment {experiment_id} not found") from exc
            logged = len(inserted)
        return (logged, len(points) - logged)

    async def read_metrics(
        self,
        experiment_id: UUID,
        *,
        key: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[MetricPoint]:
        """Read a window of one experiment's metric points, ``(key, step)`` order.

        The read half of the metrics seam. Returns typed :class:`MetricPoint`s
        ordered by ``(key, step)`` (the primary-key order, so the read is an
        index scan). ``key`` narrows to one metric; ``limit`` windows the result
        (``None`` defaults to ``DEFAULT_LIST_LIMIT``). The store trusts the
        caller's ``limit`` / ``offset`` -- the *route* enforces the
        ``[1, MAX_LIST_LIMIT]`` ceiling, matching the sibling
        ``read_session_events`` seam; a direct in-process caller may pass a
        larger window deliberately.
        """
        clauses = ["experiment_id = $1"]
        params: list[object] = [experiment_id]
        if key is not None:
            params.append(key)
            clauses.append(f"key = ${len(params)}")
        where = " AND ".join(clauses)
        # Default to the shared list page size, matching the sibling
        # ``read_session_events`` seam -- the route/client pass an explicit
        # limit, so this default only applies to direct in-process callers, and
        # a divergent metrics-only default silently disagreed with the wire.
        params.append(limit if limit is not None else DEFAULT_LIST_LIMIT)
        limit_pos = len(params)
        params.append(offset)
        offset_pos = len(params)
        sql = vetted_sql(
            "SELECT key, step, value, kind, timestamp FROM experiment_metrics WHERE ",
            where,
            " ORDER BY key, step LIMIT $",
            str(limit_pos),
            " OFFSET $",
            str(offset_pos),
        )
        async with self.engine.acquire() as conn:
            fetched = await conn.fetch(sql, *params)
        return [
            MetricPoint(
                key=cast(str, r["key"]),
                step=r["step"],
                value=r["value"],
                kind=r["kind"],
                timestamp=r["timestamp"],
            )
            for r in fetched
        ]

    async def query_metrics(
        self,
        experiment_ids: Sequence[UUID],
        *,
        masks: Sequence[MetricMaskClause],
        sort: Literal["asc", "desc"] | None = None,
        limit: int | None = None,
        max_query_experiments: int = 1000,
    ) -> list[tuple[UUID, MetricPoint]]:
        """Read the masked cells of one or more experiments' metric grids.

        The read/rank seam behind the ``trax metric`` grammar. Each mask adds a
        conjoined predicate on its axis (``key`` / ``step`` / ``value``); the six
        ``FilterOp`` comparators map through :data:`_OP_TO_SQL`. A single
        ``step`` ``max`` / ``min`` reduction picks the highest / lowest step per
        ``(experiment_id, key)`` among the rows the other masks match (via
        ``DISTINCT ON``); with no reduction, every matching cell is returned in
        ``(experiment_id, key, step)`` order. ``sort`` then orders the selection
        by ``value`` and ``limit`` windows it. Returning
        ``(experiment_id, MetricPoint)`` lets a cross-experiment (leaderboard)
        caller attribute each cell to its run.

        The query always leads with ``experiment_id = ANY($1)`` so it stays an
        index scan on the ``(experiment_id, key, step)`` primary key.

        Args:
          experiment_ids: The runs to read across (one = single-experiment,
            many = cross-experiment / rank). Capped at
            ``max_query_experiments``.
          masks: ``at <axis> <op> <value>`` clauses, ANDed into one predicate.
          sort: Order the selected cells by value ascending / descending; when
            ``None`` the ``(experiment_id, key, step)`` order is kept.
          limit: Row ceiling applied after selection; ``None`` defaults to
            ``DEFAULT_LIST_LIMIT``, and any value is capped at
            ``MAX_LIST_LIMIT``.
          max_query_experiments: Upper bound on experiment ids in one
            cross-experiment query. Bounds the ``experiment_id = ANY($1)`` array
            (and thus the index-scan fan-out) so one leaderboard call cannot
            sweep an unbounded id set; mirrors the row ceiling ``MAX_LIST_LIMIT``
            the list endpoint enforces.

        Returns:
          cells: ``(experiment_id, MetricPoint)`` per selected cell.

        Raises:
          ConflictError: An unsupported op on a metric axis, a ``max`` / ``min``
            reduction off the ``step`` axis, a non-numeric ``step`` / ``value``
            operand, or more than ``max_query_experiments`` experiments.

        """
        if len(experiment_ids) > max_query_experiments:
            raise ConflictError(
                f"too many experiments ({len(experiment_ids)}); "
                f"cap is {max_query_experiments}"
            )
        params: list[object] = [list(experiment_ids)]
        predicates: list[str] = []
        reduce_order: str | None = None
        for mask in masks:
            if mask.op in ("max", "min"):
                reduce_order = self._reduction_order(mask)
                continue
            predicates.append(self._mask_predicate(mask, params))
        where = " AND ".join(["experiment_id = ANY($1::uuid[])", *predicates])
        select = (
            "SELECT DISTINCT ON (experiment_id, key) "
            if reduce_order is not None
            else "SELECT "
        )
        inner_order = (
            f" ORDER BY experiment_id, key, step {reduce_order}"
            if reduce_order is not None
            else " ORDER BY experiment_id, key, step"
        )
        cols = "experiment_id, key, step, value, kind, timestamp"
        inner = vetted_sql(
            select, cols, " FROM experiment_metrics WHERE ", where, inner_order
        )
        params.append(
            min(limit if limit is not None else DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT)
        )
        limit_pos = len(params)
        # A reduction already carries a DISTINCT ON ordering; a value ``sort``
        # would fight it, so it must run over the reduced set. Wrapping the
        # reduction as a subquery lets the value ordering / limit apply to its
        # output without disturbing the per-key pick.
        if sort is not None:
            direction = "DESC" if sort == "desc" else "ASC"
            sql = vetted_sql(
                "SELECT * FROM (",
                inner,
                ") reduced ORDER BY value ",
                direction,
                " LIMIT $",
                str(limit_pos),
            )
        else:
            sql = vetted_sql(inner, " LIMIT $", str(limit_pos))
        async with self.engine.acquire() as conn:
            fetched = await conn.fetch(sql, *params)
        return [
            (
                cast(UUID, r["experiment_id"]),
                MetricPoint(
                    key=cast(str, r["key"]),
                    step=r["step"],
                    value=r["value"],
                    kind=r["kind"],
                    timestamp=r["timestamp"],
                ),
            )
            for r in fetched
        ]

    async def write_metrics_masked(
        self,
        experiment_id: UUID,
        *,
        masks: Sequence[MetricMaskClause],
        value: float,
    ) -> int:
        """Assign ``value`` to every masked cell of one experiment's grid.

        The write half of the ``trax metric`` grammar (``... to <value>``). Two
        shapes, chosen by the masks:

        * **single cell** -- ``key`` and ``step`` both pinned to one exact value
          with ``is`` -- upserts (``INSERT ... ON CONFLICT DO UPDATE``), so the
          common ``at loss at step is 3 to 0.5`` creates the cell if absent.
        * **bulk** -- any comparator (or a repeated axis) that could match many
          cells -- ``UPDATE ... WHERE <mask>``, which only overwrites cells that
          already exist (a range cannot invent coordinates).

        A metric point has no default step, so the masks must constrain ``step``.
        ``value`` must be finite (the DB CHECK forbids ``NaN`` / ``Inf``; this
        guards a direct caller so it 409s rather than 500s). ``kind`` is
        ``'scalar'`` on insert.

        Args:
          experiment_id: The run whose grid is written; must be an Experiment.
          masks: ``at <axis> <op> <value>`` clauses selecting the target cells.
          value: The finite scalar assigned to every selected cell.

        Returns:
          written: The number of cells inserted or updated.

        Raises:
          NotFoundError: ``experiment_id`` is not an existing inquiry.
          ConflictError: ``experiment_id`` is not an Experiment, ``value`` is
            non-finite, the masks omit ``step``, or a mask uses an unsupported
            op.

        """
        if not isfinite(value):
            raise ConflictError(f"metric value must be finite, got {value!r}")
        pins: dict[str, list[MetricMaskClause]] = {"key": [], "step": [], "value": []}
        for mask in masks:
            if mask.op in ("max", "min"):
                raise ConflictError(
                    "reduction max/min is a read-only step selection; "
                    "a write cannot reduce"
                )
            if mask.op not in _OP_TO_SQL:
                raise ConflictError(f"op {mask.op!r} not supported on metric axis")
            pins[mask.axis].append(mask)
        if not pins["step"]:
            raise ConflictError("write requires a step mask")
        single_cell = (
            len(pins["key"]) == 1
            and len(pins["step"]) == 1
            and not pins["value"]
            and pins["key"][0].op == "is"
            and pins["step"][0].op == "is"
        )
        async with self.engine.acquire() as conn, tx(conn):
            experiment = await conn.fetchrow(
                "SELECT kind FROM inquiries WHERE id = $1 FOR UPDATE",
                experiment_id,
            )
            if experiment is None:
                raise NotFoundError(f"experiment {experiment_id} not found")
            if experiment["kind"] != "Experiment":
                raise ConflictError(
                    f"inquiry {experiment_id} is not an Experiment "
                    f"(kind={experiment['kind']!r}); "
                    "metrics may only attach to an experiment"
                )
            if single_cell:
                status = await self._upsert_single_cell(
                    conn, experiment_id, pins["key"][0], pins["step"][0], value
                )
            else:
                status = await self._update_masked_cells(
                    conn, experiment_id, masks, value
                )
        return _rowcount(status)

    @staticmethod
    def _reduction_order(mask: MetricMaskClause) -> str:
        """SQL ``DISTINCT ON`` step direction for a ``max`` / ``min`` reduction."""
        if mask.axis != "step":
            raise ConflictError(
                f"reduction {mask.op} applies only to the step axis, not {mask.axis!r}"
            )
        return "DESC" if mask.op == "max" else "ASC"

    @staticmethod
    def _mask_predicate(mask: MetricMaskClause, params: list[object]) -> str:
        """Build one ``<axis> <op> $N::<cast>`` predicate, binding the operand.

        Appends the coerced operand to ``params`` and returns the SQL fragment.
        """
        if mask.op not in _OP_TO_SQL:
            raise ConflictError(f"op {mask.op!r} not supported on metric axis")
        params.append(_coerce_operand(mask))
        return vetted_sql(
            mask.axis,
            " ",
            _OP_TO_SQL[mask.op],
            " $",
            str(len(params)),
            "::",
            _AXIS_CAST[mask.axis],
        )

    @staticmethod
    async def _upsert_single_cell(
        conn: Conn,
        experiment_id: UUID,
        key_mask: MetricMaskClause,
        step_mask: MetricMaskClause,
        value: float,
    ) -> str:
        """Insert-or-overwrite the one ``(experiment_id, key, step)`` cell."""
        return await conn.execute(
            "INSERT INTO experiment_metrics "
            "(experiment_id, key, step, value, kind) "
            "VALUES ($1, $2, $3::bigint, $4::float8, 'scalar') "
            "ON CONFLICT (experiment_id, key, step) "
            "DO UPDATE SET value=EXCLUDED.value",
            experiment_id,
            key_mask.value,
            _coerce_operand(step_mask),
            value,
        )

    @staticmethod
    async def _update_masked_cells(
        conn: Conn,
        experiment_id: UUID,
        masks: Sequence[MetricMaskClause],
        value: float,
    ) -> str:
        """UPDATE existing cells the masks select; a range cannot invent rows."""
        params: list[object] = [experiment_id, value]
        predicates: list[str] = []
        for mask in masks:
            params.append(_coerce_operand(mask))
            predicates.append(
                vetted_sql(
                    mask.axis,
                    " ",
                    _OP_TO_SQL[mask.op],
                    " $",
                    str(len(params)),
                    "::",
                    _AXIS_CAST[mask.axis],
                )
            )
        where = " AND ".join(["experiment_id = $1", *predicates])
        sql = vetted_sql(
            "UPDATE experiment_metrics SET value = $2::float8 WHERE ", where
        )
        return await conn.execute(sql, *params)


def _coerce_operand(mask: MetricMaskClause) -> object:
    """Coerce a mask's string operand to its axis's Python type.

    ``key`` stays text; ``step`` becomes ``int`` and ``value`` ``float`` so the
    bound parameter matches the ``bigint`` / ``float8`` cast. A non-numeric
    ``step`` / ``value`` operand is a caller error (409), not a DB ``DataError``
    (500).
    """
    if mask.axis == "key":
        return mask.value
    try:
        return int(mask.value) if mask.axis == "step" else float(mask.value)
    except ValueError as exc:
        raise ConflictError(
            f"{mask.axis} operand {mask.value!r} is not numeric"
        ) from exc


def _rowcount(status: str) -> int:
    """Parse the affected-row count from an asyncpg command status tag.

    ``execute`` returns the command tag (``"UPDATE 3"``, ``"INSERT 0 1"``); the
    row count is its final whitespace-delimited field.
    """
    return int(status.rsplit(maxsplit=1)[-1])
