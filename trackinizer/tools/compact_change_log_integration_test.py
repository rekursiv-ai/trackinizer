"""Real PostgreSQL tests for offline ``change_log`` compaction and FK re-parenting."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import uuid

from treekle import from_plain

import pytest

from trackinizer.tools import compact_change_log
from trackinizer.wire.bodies import SubmitIssue


if TYPE_CHECKING:
    from trackinizer.server.store.core import Store


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestCompactChangeLogPostgres:
    """Exercise ``inspect_change_log`` and ``compact_change_log`` on real PostgreSQL."""

    async def test_dry_run_and_apply_reparent_caused_by_and_preserve_schema(
        self,
        integ_store: Store,
    ) -> None:
        """Compact pruned rows, re-parent ``caused_by`` across hops, preserve indexes & FKs."""
        grandparent_id = await integ_store.submit_issue(
            SubmitIssue(
                account="tester@example.com",
                title="grandparent",
                subscribers=["alice"],
            ),
        )
        parent_id = await integ_store.submit_issue(
            SubmitIssue(
                account="tester@example.com",
                title="unsubscribed parent",
                narrows=[(grandparent_id, None)],
            ),
        )
        child_id = await integ_store.submit_issue(
            SubmitIssue(
                account="tester@example.com",
                title="leaf",
                narrows=[(parent_id, None)],
            ),
        )
        origin_change = await integ_store.set_status(child_id, "complete", actor="user")
        assert origin_change is not None

        # Synthesize historical rows written BEFORE Option A:
        # 1. Single-hop pruned intermediate: C0 (origin_change, kept) <- C1 (pruned) <- C2 (kept)
        # 2. Multi-hop pruned chain: C0 <- C1a (pruned) <- C1b (pruned) <- C3 (kept)
        # 3. Pruned root with NULL caused_by: C_pruned_root (pruned, NULL) <- C4 (kept)
        # 4. Unsubscribed dependency_changed leaf with no children (pruned)
        c1_id = uuid.uuid4()
        c2_id = uuid.uuid4()
        c1a_id = uuid.uuid4()
        c1b_id = uuid.uuid4()
        c3_id = uuid.uuid4()
        c_pruned_root_id = uuid.uuid4()
        c4_id = uuid.uuid4()
        c_leaf_pruned_id = uuid.uuid4()

        insert_dep = (
            "INSERT INTO change_log ("
            "id, actor, subject_id, subject_kind, kind, caused_by, "
            "subscribers_snapshot, new_peer_id, new_peer_kind, "
            "new_peer_edge_kind, new_edge_note, new_edge_labels"
            ") VALUES ($1, 'librarian', $2, 'Issue', 'dependency_changed', $3, "
            "$4, $5, 'Issue', 'narrows', '', '{}')"
        )
        async with integ_store.engine.acquire() as conn:
            await conn.execute(
                insert_dep,
                c1_id,
                parent_id,
                origin_change,
                [],
                child_id,
            )
            await conn.execute(
                insert_dep,
                c2_id,
                grandparent_id,
                c1_id,
                ["alice"],
                parent_id,
            )
            await conn.execute(
                insert_dep,
                c1a_id,
                parent_id,
                origin_change,
                [],
                child_id,
            )
            await conn.execute(
                insert_dep,
                c1b_id,
                parent_id,
                c1a_id,
                [],
                child_id,
            )
            await conn.execute(
                insert_dep,
                c3_id,
                grandparent_id,
                c1b_id,
                ["alice"],
                parent_id,
            )
            await conn.execute(
                insert_dep,
                c_pruned_root_id,
                parent_id,
                None,
                [],
                child_id,
            )
            await conn.execute(
                insert_dep,
                c4_id,
                grandparent_id,
                c_pruned_root_id,
                ["alice"],
                parent_id,
            )
            await conn.execute(
                insert_dep,
                c_leaf_pruned_id,
                parent_id,
                origin_change,
                [],
                child_id,
            )

            since = datetime(2000, 1, 1, tzinfo=UTC)
            before_alice = [
                (c.id, c.subject_id, c.kind)
                for c in await integ_store.what_changed_for_me("alice", since)
            ]
            before_anyone = [
                (c.id, c.subject_id, c.kind, seq)
                for c, seq in await integ_store.what_changed_for_anyone(since)
            ]

            before_indexes = {
                str(r["indexname"]): str(r["indexdef"])
                for r in await conn.fetch(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE schemaname = 'public' AND tablename = 'change_log'",
                )
            }
            before_fks = {
                str(r["conname"]): str(r["condef"])
                for r in await conn.fetch(
                    "SELECT conname, pg_get_constraintdef(oid) AS condef "
                    "FROM pg_constraint "
                    "WHERE conrelid = 'public.change_log'::regclass AND contype = 'f'",
                )
            }

            plan = await compact_change_log.inspect_change_log(conn)
            assert plan.blockers == ()
            assert plan.prune_rows == 5
            assert plan.reparent_rows == 3
            assert plan.total_rows == plan.keep_rows + plan.prune_rows

            # Dry-run left the table completely untouched.
            assert (
                await conn.fetchval("SELECT COUNT(*) FROM change_log")
                == plan.total_rows
            )

            result = await compact_change_log.compact_change_log(
                conn,
                plan,
                allow_active_connections=True,
            )
            assert result.pruned_rows == 5
            assert result.kept_rows == plan.keep_rows
            assert result.reparented_rows == 3

            # Pruned rows are gone; kept rows are re-parented to nearest kept ancestor.
            c2_cause = await conn.fetchval(
                "SELECT caused_by FROM change_log WHERE id = $1",
                c2_id,
            )
            c3_cause = await conn.fetchval(
                "SELECT caused_by FROM change_log WHERE id = $1",
                c3_id,
            )
            c4_cause = await conn.fetchval(
                "SELECT caused_by FROM change_log WHERE id = $1",
                c4_id,
            )
            assert c2_cause == origin_change
            assert c3_cause == origin_change
            assert c4_cause is None
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM change_log "
                    "WHERE kind = 'dependency_changed' AND subscribers_snapshot = '{}'",
                )
                == 0
            )
            # Backup table kept intact for operator verification / manual drop.
            assert (
                await conn.fetchval("SELECT COUNT(*) FROM public.change_log_old")
                == plan.total_rows
            )

            # All indexes and foreign keys are preserved with identical names & definitions.
            after_indexes = {
                str(r["indexname"]): str(r["indexdef"])
                for r in await conn.fetch(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE schemaname = 'public' AND tablename = 'change_log'",
                )
            }
            after_fks = {
                str(r["conname"]): str(r["condef"])
                for r in await conn.fetch(
                    "SELECT conname, pg_get_constraintdef(oid) AS condef "
                    "FROM pg_constraint "
                    "WHERE conrelid = 'public.change_log'::regclass AND contype = 'f'",
                )
            }
            assert after_indexes == before_indexes
            assert after_fks == before_fks

            # Subsequent inspect refuses until operator drops change_log_old.
            post_plan = await compact_change_log.inspect_change_log(conn)
            assert any("change_log_old" in b for b in post_plan.blockers)

            # Clean up change_log_old so session baseline restore stays clean.
            await conn.execute("DROP TABLE public.change_log_old")

        # Subscriber catch-up queries return the exact same rows before and after.
        after_alice = [
            (c.id, c.subject_id, c.kind)
            for c in await integ_store.what_changed_for_me("alice", since)
        ]
        after_anyone = [
            (c.id, c.subject_id, c.kind, seq)
            for c, seq in await integ_store.what_changed_for_anyone(since)
        ]
        assert after_alice == before_alice
        assert after_anyone == before_anyone

        # Live Store writes continue working cleanly after table swap.
        follow_up = await integ_store.set_priority(child_id, 5, actor="user")
        assert follow_up is not None

    async def test_preserves_reloptions_and_detects_dependent_view_blocker(
        self,
        integ_store: Store,
    ) -> None:
        """Compaction preserves ``reloptions`` and refuses to swap under a dependent view."""
        async with integ_store.engine.acquire() as conn:
            await conn.execute(
                "ALTER TABLE public.change_log SET (autovacuum_vacuum_scale_factor = 0.05)",
            )
            await conn.execute(
                "CREATE VIEW public.v_change_log_probe AS SELECT id FROM public.change_log",
            )
            try:
                blocked = await compact_change_log.inspect_change_log(conn)
                assert any("v_change_log_probe" in b for b in blocked.blockers)
            finally:
                await conn.execute("DROP VIEW public.v_change_log_probe")

            plan = await compact_change_log.inspect_change_log(conn)
            assert plan.blockers == ()
            assert "autovacuum_vacuum_scale_factor=0.05" in plan.table_schema.reloptions

            await compact_change_log.compact_change_log(
                conn,
                plan,
                allow_active_connections=True,
            )
            reloptions = await conn.fetchval(
                "SELECT reloptions FROM pg_class WHERE oid = 'public.change_log'::regclass",
            )
            assert "autovacuum_vacuum_scale_factor=0.05" in from_plain(
                reloptions,
                list[str],
            )
            await conn.execute("DROP TABLE public.change_log_old")
            await conn.execute(
                "ALTER TABLE public.change_log RESET (autovacuum_vacuum_scale_factor)",
            )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
