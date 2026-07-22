"""The experiment metric: one logged scalar of an :class:`Experiment` run.

This is the typed contract for the ``experiment_metrics`` table -- the
append-only, step-grained time-series produced when an experiment logs a
metric (the wandb ``log({key: value}, step=)`` analogue). It is the source
of truth for that table the same way :mod:`types.inquiries` is for
``inquiries`` and :mod:`types.agent_session_events` is for
``agent_session_events``: the SQL columns, the wire body, and the Store all
derive from the :class:`ExperimentMetric` dataclass here.

It is **not** an :class:`~trackinizer.types.inquiries.Inquiry`.
The owning experiment is the :class:`Experiment` artifact row in
``inquiries``; these points hang off it by ``experiment_id`` and carry no
edges, cost, supersession, or ``change_log`` audit -- a logged metric is
telemetry, not a knowledge mutation (the same exemption
:class:`~trackinizer.types.agent_session_events.AgentSessionEvent`
takes; see ``docs/design.md``, "Everything is provenance").

Identity is ``(experiment_id, key, step)``: one value per metric key per
step per experiment. ``step`` is caller-assigned and monotonic per key, so
a re-sent point collides and dedups (``ON CONFLICT DO NOTHING``) rather than
duplicating -- the same trick ``(session_id, seq)`` plays for session events.

:attr:`value` is a bare finite ``float`` scalar; :attr:`kind` is a
discriminator closed to ``"scalar"`` today. Both are constrained on the wire
(``MetricPoint``) and by a DB CHECK; widening ``kind`` to non-scalar points
(histograms, media references) is a later migration that widens the wire
Literal, the CHECK, and the readers together. Media bytes, when added, will
not ride inline here -- they belong in a blob store this table only references.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from trackinizer.types.columns import ColumnSpec


@dataclass(frozen=True, slots=True, kw_only=True)
class ExperimentMetric:
    """One logged metric point: one ``experiment_metrics`` row.

    Identity is ``(experiment_id, key, step)``. ``value`` is the scalar for
    that ``(key, step)``; ``kind`` discriminates the value shape
    (``"scalar"`` for now). ``timestamp`` is the producer's wall clock when
    the point was logged, distinct from :attr:`created` (when trackinizer
    wrote the row).

    This dataclass is the field/type contract the schema files and the wire
    ``MetricPoint`` cite as their source of truth. Its ``ColumnSpec`` metadata
    rides along for symmetry with the rest of ``types/``, but -- like
    :class:`AgentSessionEvent` and unlike an :class:`Inquiry` -- this side
    table's DDL is hand-written SQL (``assets/schema.NNN.sql``), not generated
    from these specs. The read path builds the wire ``MetricPoint`` from rows
    directly (as ``read_session_events`` builds ``EventBody``), so this type
    carries no ``from_row``.
    """

    experiment_id: UUID = field(default_factory=uuid4)
    """The :class:`Experiment` (an ``inquiries`` row) this point belongs to."""

    key: str = field(
        default="",
        metadata=ColumnSpec(sql_type="TEXT", required=True),
    )
    """The metric name (``"loss"``, ``"val/acc"``, ``"gpu.0.mem"``). System
    metrics are ordinary keys, not a separate table."""

    step: int = field(
        default=0,
        metadata=ColumnSpec(sql_type="BIGINT", required=True),
    )
    """The x-axis ordinal for this point, caller-assigned and monotonic per
    key. With ``experiment_id`` and ``key`` it is the primary key and the
    dedup key."""

    value: float = field(
        default=0.0,
        metadata=ColumnSpec(sql_type="DOUBLE PRECISION", required=True),
    )
    """The logged scalar at this ``(key, step)``."""

    kind: str = field(
        default="scalar",
        metadata=ColumnSpec(sql_type="TEXT", required=True),
    )
    """The value shape. ``"scalar"`` today; reserved so histogram / media
    points can join this table without a migration."""

    timestamp: datetime | None = field(
        default=None,
        metadata=ColumnSpec(sql_type="TIMESTAMPTZ"),
    )
    """When the producer logged the point, on its own clock. Distinct from
    :attr:`created` (the DB write clock)."""

    created: datetime | None = None
    """When trackinizer wrote the row (DB clock). Server-managed; ``None``
    until persisted."""
