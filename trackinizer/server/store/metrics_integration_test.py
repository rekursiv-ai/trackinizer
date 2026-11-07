"""Metric logging and reads against a session-scoped Postgres, through a real Store."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import asyncio

import pytest

from trackinizer.conftest import new_uuid
from trackinizer.types.errors import ConflictError, NotFoundError
from trackinizer.wire.bodies import SubmitBelief, SubmitExperiment
from trackinizer.wire.wire_metrics import MetricPoint


if TYPE_CHECKING:
    from trackinizer.server.store.core import Store


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestAgainstPostgres:
    """Metric logging and reads, each read back from the database."""

    async def test_log_metrics_append_read_and_dedup(self, integ_store: Store) -> None:
        """Metric points log, read back in (key, step) order, and dedup.

        The wandb ``log()`` analogue: ``(experiment_id, key, step)`` is the
        dedup key, so a retried batch is a no-op and reports ``logged=0``.
        """
        eid = await integ_store.submit_experiment(
            SubmitExperiment(account="tester@example.com", title="run"),
        )
        batch = [
            MetricPoint(key="loss", step=0, value=0.9),
            MetricPoint(key="loss", step=1, value=0.5),
            MetricPoint(key="acc", step=0, value=0.1, kind="scalar"),
        ]
        logged, skipped = await integ_store.log_metrics(eid, batch)
        assert (logged, skipped) == (3, 0)

        # A retried batch is a no-op on (experiment_id, key, step).
        logged2, skipped2 = await integ_store.log_metrics(eid, batch)
        assert (logged2, skipped2) == (0, 3)

        # Read back in (key, step) order: acc before loss, loss ascending.
        points = await integ_store.read_metrics(eid)
        assert [(p.key, p.step, p.value) for p in points] == [
            ("acc", 0, 0.1),
            ("loss", 0, 0.9),
            ("loss", 1, 0.5),
        ]

        # ``key`` narrows to one metric.
        loss_only = await integ_store.read_metrics(eid, key="loss")
        assert [p.step for p in loss_only] == [0, 1]

    async def test_log_metrics_within_batch_duplicate_first_wins(
        self,
        integ_store: Store,
    ) -> None:
        """Two points with the same (key, step) in ONE batch: first wins.

        ``ON CONFLICT DO NOTHING`` dedups an intra-statement duplicate against
        the first-inserted row, so the batch reports ``logged=1, skipped=1`` and
        the first value is stored (the second is silently dropped) -- the same
        idempotency contract as a cross-batch retry, pinned here because the
        intra-batch case has subtle Postgres semantics.
        """
        eid = await integ_store.submit_experiment(
            SubmitExperiment(account="tester@example.com", title="run"),
        )
        logged, skipped = await integ_store.log_metrics(
            eid,
            [
                MetricPoint(key="loss", step=0, value=0.1),
                MetricPoint(key="loss", step=0, value=0.2),
            ],
        )
        assert (logged, skipped) == (1, 1)
        points = await integ_store.read_metrics(eid)
        assert [(p.key, p.step, p.value) for p in points] == [("loss", 0, 0.1)]

    async def test_log_metrics_timestamp_round_trips(self, integ_store: Store) -> None:
        """A tz-aware ``timestamp`` round-trips through ``TIMESTAMPTZ``."""
        eid = await integ_store.submit_experiment(
            SubmitExperiment(account="tester@example.com", title="run"),
        )
        ts = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        await integ_store.log_metrics(
            eid,
            [MetricPoint(key="loss", step=0, value=1.0, timestamp=ts)],
        )
        points = await integ_store.read_metrics(eid)
        assert points[0].timestamp == ts

    async def test_read_metrics_offset_skips_rows(self, integ_store: Store) -> None:
        """``offset`` skips leading rows in (key, step) order."""
        eid = await integ_store.submit_experiment(
            SubmitExperiment(account="tester@example.com", title="run"),
        )
        await integ_store.log_metrics(
            eid,
            [MetricPoint(key="loss", step=i, value=float(i)) for i in range(3)],
        )
        page = await integ_store.read_metrics(eid, limit=1, offset=1)
        assert [(p.key, p.step) for p in page] == [("loss", 1)]

    async def test_log_metrics_concurrent_appenders_exact_no_loss(
        self,
        integ_store: Store,
    ) -> None:
        """Concurrent overlapping batches: no lost writes, exact per-call counts.

        Two ``log_metrics`` calls race on one experiment with overlapping
        ``(key, step)`` ranges. The union must be stored exactly once (no loss,
        no duplicate), and each call's ``logged + skipped`` must equal its own
        batch size -- proving the ``ON CONFLICT DO NOTHING RETURNING`` accounting
        is race-correct (counts returned rows, not a whole-table delta).
        """
        eid = await integ_store.submit_experiment(
            SubmitExperiment(account="tester@example.com", title="run"),
        )
        a = [MetricPoint(key="loss", step=i, value=1.0) for i in range(100)]
        b = [MetricPoint(key="loss", step=i, value=2.0) for i in range(50, 150)]
        (la, sa), (lb, sb) = await asyncio.gather(
            integ_store.log_metrics(eid, a),
            integ_store.log_metrics(eid, b),
        )
        back = await integ_store.read_metrics(eid, limit=10_000)
        assert len(back) == 150  # Union 0..149: no loss, no duplicate.
        assert la + lb == 150  # Total newly-written == distinct rows.
        assert (la + sa, lb + sb) == (100, 100)  # Each call accounts for its batch.

    async def test_log_metrics_waits_out_a_purge_and_reports_not_found(
        self,
        integ_store: Store,
    ) -> None:
        """A purge in flight holds ``log_metrics`` at its kind check, then 404s.

        ``log_metrics`` reads the experiment ``FOR UPDATE``, so it queues behind
        an uncommitted purge and then finds the row gone. Without that lock it
        would pass the kind check against its snapshot and die on the
        ``experiment_id`` foreign key instead.
        """
        eid = await integ_store.submit_experiment(
            SubmitExperiment(account="tester@example.com", title="run"),
        )
        point = [MetricPoint(key="loss", step=0, value=1.0)]
        async with integ_store.engine.acquire() as purger:
            await purger.execute("BEGIN")
            await purger.execute("SET LOCAL statement_timeout = '10s'")
            await purger.execute("DELETE FROM inquiries WHERE id = $1", eid)
            logging = asyncio.create_task(integ_store.log_metrics(eid, point))
            # Commit only once ``log_metrics`` is queued on the row lock, so the
            # purge lands between its kind check and its INSERT.
            await purger.execute(
                "DO $$ BEGIN "
                "WHILE NOT EXISTS (SELECT 1 FROM pg_locks WHERE NOT granted) LOOP "
                "PERFORM pg_sleep(0.01); END LOOP; END $$",
            )
            await purger.execute("COMMIT")
        with pytest.raises(NotFoundError, match="not found"):
            await logging

    async def test_log_metrics_large_batch_at_cap(self, integ_store: Store) -> None:
        """A max-size batch inserts and reads back in order at scale."""
        eid = await integ_store.submit_experiment(
            SubmitExperiment(account="tester@example.com", title="run"),
        )
        pts = [MetricPoint(key="loss", step=i, value=float(i)) for i in range(10_000)]
        logged, skipped = await integ_store.log_metrics(eid, pts)
        assert (logged, skipped) == (10_000, 0)
        back = await integ_store.read_metrics(eid, limit=10_000)
        assert len(back) == 10_000
        assert back[0].step == 0
        assert back[-1].step == 9999

    async def test_log_metrics_latest_value_per_key_via_join(
        self,
        integ_store: Store,
    ) -> None:
        """The latest-value roll-up is a DISTINCT ON over the PK index.

        No summary column: ``DISTINCT ON (key) ... ORDER BY key, step DESC``
        reads the last point per key straight off ``(experiment_id, key,
        step)``.
        """
        eid = await integ_store.submit_experiment(
            SubmitExperiment(account="tester@example.com", title="run"),
        )
        await integ_store.log_metrics(
            eid,
            [
                MetricPoint(key="loss", step=0, value=0.9),
                MetricPoint(key="loss", step=5, value=0.2),
                MetricPoint(key="acc", step=3, value=0.7),
            ],
        )
        async with integ_store.engine.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT ON (key) key, value FROM experiment_metrics "
                "WHERE experiment_id = $1 ORDER BY key, step DESC",
                eid,
            )
        assert {r["key"]: r["value"] for r in rows} == {"loss": 0.2, "acc": 0.7}

    async def test_log_metrics_rejects_non_experiment(self, integ_store: Store) -> None:
        """A metric may only attach to an Experiment; other kinds 409, missing 404."""
        belief = await integ_store.submit_belief(
            SubmitBelief(account="tester@example.com", title="b"),
        )
        with pytest.raises(ConflictError, match="not an Experiment"):
            await integ_store.log_metrics(
                belief,
                [MetricPoint(key="loss", step=0, value=1.0)],
            )
        with pytest.raises(NotFoundError, match="not found"):
            await integ_store.log_metrics(
                new_uuid(),
                [MetricPoint(key="loss", step=0, value=1.0)],
            )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
