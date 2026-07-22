-- Migration 011: experiment_metrics -- step-grained metric time-series for
-- Experiment runs (the wandb ``log({key: value}, step=)`` analogue).
--
-- Source of truth: types/experiment_metrics.py (ExperimentMetric). Like
-- ``agent_session_events`` (schema.001), this is a flat, fixed-shape side
-- table, so its DDL is written here directly rather than generated from
-- per-kind ColumnSpec metadata the way ``inquiries`` is.
--
-- Deliberately OUTSIDE ``inquiries``: an append-only telemetry stream, not a
-- knowledge row. The owning experiment is the ``Experiment`` artifact in
-- ``inquiries``; these rows hang off it by ``experiment_id`` and carry no
-- edges, cost, supersession, or ``change_log`` audit -- a logged metric is
-- telemetry, not a knowledge mutation (the same exemption
-- ``agent_session_events`` takes). Tenant scope is derived by joining to
-- inquiries (no denormalized org_id column).
--
-- ``PRIMARY KEY (experiment_id, key, step)`` is the per-point dedup mechanism:
-- the producer assigns ``step`` monotonically per key, and a retried batch
-- ``ON CONFLICT DO NOTHING``s. The same index serves the hot read
-- (``WHERE experiment_id = ? AND key = ? ORDER BY step``) and the
-- latest-value-per-key roll-up (``DISTINCT ON (key) ... ORDER BY key, step
-- DESC``), so no summary column is denormalized onto the Experiment row.
--
-- ``value`` is a bare scalar; ``kind`` ('scalar' today) reserves room for
-- non-scalar points (histograms, media references) without a later migration.
-- Media BYTES never ride inline here -- they will belong in a blob store this
-- table only references.
--
-- Plain Postgres table (bit-identical on PGlite). A Timescale hypertable, if
-- ever wanted for high-frequency runs, is a deploy-time ALTER, not bootstrap
-- DDL -- exactly as noted for agent_session_events.
CREATE TABLE IF NOT EXISTS experiment_metrics (
    experiment_id UUID NOT NULL REFERENCES inquiries(id) ON DELETE CASCADE,
    key           TEXT NOT NULL,
    step          BIGINT NOT NULL CHECK (step >= 0),
    value         DOUBLE PRECISION NOT NULL,
    -- Closed to 'scalar' today: the wire ``MetricPoint.kind`` is
    -- ``Literal["scalar"]`` and ``read_metrics`` reconstructs that Literal from
    -- this column, so a non-scalar row would 500 the read (same failure class
    -- the value finiteness guard closes). This CHECK backstops the wire the way
    -- ``CHECK (step >= 0)`` backstops ``Field(ge=0)`` -- and mirrors the sibling
    -- ``agent_session_events.kind`` CHECK. Widen wire + this CHECK + the readers
    -- together when non-scalar payloads land.
    kind          TEXT NOT NULL DEFAULT 'scalar' CHECK (kind = 'scalar'),
    timestamp     TIMESTAMPTZ,
    created       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (experiment_id, key, step)
);

-- ``PRIMARY KEY (experiment_id, key, step)`` already indexes ordered
-- per-(experiment, key) reads and the DISTINCT ON latest-value roll-up, so no
-- separate index is needed for the common access paths.
