"""Tests for cascade emission -- ``emit_change``, ``purge``, edge cascades."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import json
import uuid

import pytest

from trackinizer.conftest import (
    executed_sql,
    make_conn,
    make_store,
    new_uuid,
    queue_field_rows,
    set_field_row,
)
from trackinizer.server.store.core import Store
from trackinizer.types.cost import Cost
from trackinizer.types.errors import ConflictError


def _spy_cascade(store: Store, monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Replace ``Store._cascade_dependency_changed`` with a recording stub.

    Returns the list each invocation appends to, so a test asserts the
    cascade fired exactly N times via ``len(...)``. A real typed ``async
    def`` (not an ``AsyncMock``) keeps the patch type-correct under ty.
    """
    calls: list[object] = []

    async def stub(*args: object, **kwargs: object) -> None:
        del args, kwargs
        calls.append(None)

    monkeypatch.setattr(store, "_cascade_dependency_changed", stub)
    return calls


class TestPurge:
    @pytest.mark.asyncio
    async def test_purge_missing_raises(self) -> None:
        conn = make_conn()
        set_field_row(conn, None)
        store, _engine = make_store(conn)
        with pytest.raises(ConflictError):
            await store.purge(new_uuid(), actor="user")

    @pytest.mark.asyncio
    async def test_purge_alerts_parents_before_deleting_edges(self) -> None:
        target_id = new_uuid()
        parent_id = new_uuid()
        conn = make_conn()
        queue_field_rows(conn, {"kind": "Experiment"})
        # ``proves`` stores Artifact -> claim: the purged Experiment is the
        # from-side citing evidence, the parent Belief the to-side cited claim.
        # ``cascade_dependent="to"`` alerts the claim when its evidence moves.
        purge_row = {
            "from_id": target_id,
            "from_kind": "Experiment",
            "to_id": parent_id,
            "to_kind": "Belief",
            "edge_kind": "proves",
            "priority": None,
            "note": "load-bearing",
            "valence": 0.9,
            "labels": ["important"],
        }
        # Cascade re-walks the graph for the parent; that walk uses the
        # aliased ``parent_id``/``parent_kind`` shape. The mock returns
        # rows that satisfy both consumers.
        conn.fetch.side_effect = [[purge_row], []]
        store, engine = make_store(conn)
        await store.purge(target_id, actor="alice")
        sqls = executed_sql(conn)
        assert any("DELETE FROM inquiries" in sql for sql in sqls)
        assert sum("INSERT INTO change_log" in s for s in sqls) == 3
        change_kinds = [
            call.args[6]
            for call in conn.execute.call_args_list
            if "INSERT INTO change_log" in call.args[0]
        ]
        assert change_kinds == ["purged", "dependency_changed", "edge_removed"]
        # Three change_log rows touch two distinct subjects (the purged target
        # and its parent, which receives both the dependency_changed and the
        # edge_removed). Post-commit fanout dedups by subject_id, so each
        # affected inquiry wakes its subscribers exactly once -- two NOTIFYs,
        # not one per buffered entry.
        assert [json.loads(payload)["id"] for _, payload in engine.notify_calls] == [
            str(target_id),
            str(parent_id),
        ]


class TestEmitChangeFloorIsAtomic:
    """``emit_change``'s negative-floor guard lives inside the UPDATE.

    The floor enforcement must run as part of the cost ``UPDATE``
    itself -- as ``WHERE marginal_cost_* + delta >= 0`` predicates --
    not as a post-UPDATE application check. Without an outer
    transaction, a post-UPDATE check leaves a negative running total
    persisted before the raise propagates: there is nothing to roll
    back. The guarded UPDATE is inherently atomic: a violating delta
    matches no row, so the running total stays put.
    """

    @pytest.mark.asyncio
    async def test_update_carries_nonnegative_guard(self) -> None:
        conn = make_conn()
        store, _engine = make_store(conn)
        async with store.engine.acquire() as acquired:
            await store.emit_change(
                acquired,
                subject_id=new_uuid(),
                subject_kind="Issue",
                kind="marginal_cost",
                cost_delta=Cost(agent_usd=0.5),
            )
        cost_updates = [
            call.args[0]
            for call in conn.fetchrow.call_args_list
            if "marginal_cost_agent_usd" in call.args[0]
            and "UPDATE inquiries" in call.args[0]
        ]
        assert cost_updates, "expected the cost UPDATE to fire"
        sql = cost_updates[0]
        # The guard must be in the WHERE clause so Postgres refuses
        # to modify the row; an after-the-fact app check is the bug.
        assert "marginal_cost_agent_usd    + $1 >= 0" in sql
        assert "marginal_cost_resource_usd + $2 >= 0" in sql

    @pytest.mark.asyncio
    async def test_direct_emit_change_without_tx_leaves_no_audit_or_update(
        self,
    ) -> None:
        """Direct ``emit_change`` without an outer ``tx`` is the bug surface.

        The earlier implementation ran the UPDATE first, then raised
        from application code -- with no surrounding transaction the
        UPDATE auto-committed and the row was left negative. With the
        guard moved into the UPDATE and the probe folded into the same
        statement, the floor-violating delta produces a single row whose
        ``existing_id`` is non-NULL (row present at snapshot) and whose
        ``new_*`` cost columns are NULL (LEFT JOIN against the empty
        update CTE). No audit row is inserted.
        """
        conn = make_conn()

        async def fetchrow(sql: str, *args: Any) -> Any:
            if "marginal_cost_agent_usd" in sql and "RETURNING" in sql:
                # Mirror Postgres: the modifying CTE rejects the floor-
                # violating row; the outer SELECT still emits one row
                # (because the probe matched) with NULL cost columns.
                return {
                    "existing_id": args[2],
                    "old_agent": None,
                    "old_resource": None,
                    "new_agent": None,
                    "new_resource": None,
                    "current_subscribers": None,
                }
            return None

        conn.fetchrow = AsyncMock(side_effect=fetchrow)
        store, _engine = make_store(conn)
        subject_id = new_uuid()
        async with store.engine.acquire() as acquired:
            with pytest.raises(ConflictError, match="negative"):
                await store.emit_change(
                    acquired,
                    subject_id=subject_id,
                    subject_kind="Issue",
                    kind="marginal_cost",
                    cost_delta=Cost(agent_usd=-1.0),
                )
        sqls = executed_sql(conn)
        assert not any("INSERT INTO change_log" in sql for sql in sqls), (
            "audit row must not be written when the floor refused the delta"
        )
        # No ROLLBACK was issued either -- the caller never opened a
        # transaction. Correctness must come from the UPDATE itself,
        # not from a surrounding ``tx`` block.
        assert not any(sql == "ROLLBACK" for sql in sqls)

    @pytest.mark.asyncio
    async def test_floor_refused_race_with_purge_still_raises(self) -> None:
        """A row purged mid-disambiguation must still raise ConflictError.

        Under autocommit a two-statement (guarded UPDATE then probe
        SELECT) disambiguation has a race: the floor-refused UPDATE
        auto-commits as a no-op, a concurrent purge then drops the row
        before the probe runs, the probe returns ``None``, and the
        caller treats the result as a tombstone -- silently emitting a
        zero-delta audit row and swallowing the user's negative-delta
        intent. The fix folds the disambiguation into a single SQL
        statement whose modifying CTE and presence read share one
        snapshot: a row that was present-and-floor-refused at snapshot
        time stays observable as such even if it is purged
        concurrently, so the ConflictError signal cannot be lost.

        The mock simulates that race for both implementations.
        Two-statement code: the cost UPDATE returns no row (floor
        refused at original snapshot) and the probe returns ``None``
        (purge raced in between). One-statement code: the single CTE
        statement is dispatched by SQL shape and returns the same
        "row present, floor refused" view a fresh snapshot would yield
        before the purge committed. Either implementation must raise.
        """
        conn = make_conn()

        async def fetchrow(sql: str, *args: Any) -> Any:
            del args
            # The fix uses a single ``WITH ... SELECT`` statement that
            # carries the cost UPDATE inside a modifying CTE. Detect it
            # by the presence of both the CTE keyword and the cost
            # columns; reply with the snapshot a fresh single-statement
            # read would see when the row exists and the floor refused.
            if (
                "marginal_cost_agent_usd" in sql
                and "RETURNING" in sql
                and "WITH" in sql.upper()
            ):
                return {
                    "existing_id": uuid.uuid4(),
                    "old_agent": None,
                    "old_resource": None,
                    "new_agent": None,
                    "new_resource": None,
                    "current_subscribers": None,
                }
            # Legacy two-statement code path: the bare guarded UPDATE.
            # Returns no row to mirror "floor refused", matching the
            # auto-commit + race scenario at original snapshot.
            if "marginal_cost_agent_usd" in sql and "RETURNING" in sql:
                return None
            return None

        conn.fetchrow = AsyncMock(side_effect=fetchrow)
        # Legacy probe (``SELECT 1 FROM inquiries WHERE id = $1``)
        # observes the post-purge snapshot and returns ``None``.
        conn.fetchval = AsyncMock(return_value=None)
        store, _engine = make_store(conn)
        subject_id = new_uuid()
        async with store.engine.acquire() as acquired:
            with pytest.raises(ConflictError, match="negative"):
                await store.emit_change(
                    acquired,
                    subject_id=subject_id,
                    subject_kind="Issue",
                    kind="marginal_cost",
                    cost_delta=Cost(agent_usd=-1.0),
                )
        sqls = executed_sql(conn)
        assert not any("INSERT INTO change_log" in sql for sql in sqls), (
            "audit row must not be written when the floor refused the delta "
            "even if the row was purged between statements"
        )

    @pytest.mark.asyncio
    async def test_probe_cte_locks_subject_row_for_share(self) -> None:
        """The presence-probe CTE must take ``FOR SHARE`` on the subject row.

        Without a row lock the probe ``SELECT id FROM inquiries WHERE id=$3``
        can find the row, a concurrent ``purge`` (which holds ``FOR UPDATE``)
        can then commit the DELETE, and the CTE's modifying ``UPDATE`` sub-
        statement matches nothing -- collapsing into a phantom zero-delta
        ``change_log`` audit for an already-deleted subject. ``FOR SHARE``
        serializes the probe against purge's ``FOR UPDATE`` so the two cannot
        interleave: either the probe blocks until purge commits and then sees
        no row (genuine tombstone), or purge blocks until this tx commits.
        """
        conn = make_conn()
        store, _engine = make_store(conn)
        async with store.engine.acquire() as acquired:
            await store.emit_change(
                acquired,
                subject_id=new_uuid(),
                subject_kind="Issue",
                kind="marginal_cost",
                cost_delta=Cost(agent_usd=0.5),
            )
        probe = next(
            call.args[0]
            for call in conn.fetchrow.call_args_list
            if "marginal_cost_agent_usd" in call.args[0]
            and "WITH probe" in call.args[0]
        )
        assert "FOR SHARE" in probe, (
            "the presence-probe CTE must lock the subject row FOR SHARE so it "
            "serializes against purge's FOR UPDATE"
        )


class TestEdgeCascadeSymmetry:
    """Structural edge mutations cascade ``dependency_changed`` exactly once.

    A paired-endpoint edge audit emits on both endpoints, but only the
    primary (subject / from) emit drives the ancestor cascade; the peer
    emit passes ``cascade=False``. Annotation changes are not structural
    and do not cascade at all.
    """

    @pytest.mark.asyncio
    async def test_insert_edge_and_audit_cascades_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = make_conn()
        # ``insert_edge``'s fetchval sequence: lookup_kind(to) -> kind,
        # cycle-EXISTS -> falsy, INSERT ... RETURNING from_id -> non-None.
        conn.fetchval.side_effect = ["Experiment", False, new_uuid()]
        store, _engine = make_store(conn)
        cascades = _spy_cascade(store, monkeypatch)
        async with store.engine.acquire() as held:
            await store.insert_edge_and_audit(
                held,
                subject_id=new_uuid(),
                subject_kind="Belief",
                to_id=new_uuid(),
                edge_kind="proves",
                api_key_id=None,
                actor="alice",
                caused_by=new_uuid(),
            )
        # The to-side emit must pass ``cascade=False``; only the from-side
        # cascades. Two cascades is the double-alert bug.
        assert len(cascades) == 1

    @pytest.mark.asyncio
    async def test_set_edge_annotation_does_not_cascade(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = make_conn()
        set_field_row(
            conn,
            {
                "from_kind": "Belief",
                "to_kind": "Experiment",
                "priority": None,
                "note": "old",
                "valence": 0.2,
                "labels": ["old-label"],
            },
        )
        store, _engine = make_store(conn)
        cascades = _spy_cascade(store, monkeypatch)
        await store.set_edge_annotation(
            from_id=new_uuid(),
            to_id=new_uuid(),
            edge_kind="proves",
            note="new",
            actor="alice",
        )
        # Re-noting an edge is not a structural change; it must not fire
        # the ancestor re-assessment cascade (which add/remove do).
        assert len(cascades) == 0

    @pytest.mark.asyncio
    async def test_add_edge_audit_note_is_normalized_to_none(self) -> None:
        conn = make_conn()
        conn.fetchval.side_effect = ["Belief", "Experiment", False, new_uuid()]
        store, _engine = make_store(conn)
        async with store.engine.acquire() as held:
            await store._add_edge_on_conn(
                held,
                from_id=new_uuid(),
                to_id=new_uuid(),
                edge_kind="proves",
                priority=None,
                note="   ",
                valence=None,
                labels=(),
                api_key_id=None,
                actor="alice",
            )
        insert = next(
            c
            for c in conn.execute.call_args_list
            if "INSERT INTO change_log" in c.args[0]
        )
        cols = insert.args[0]
        names = cols[cols.index("(") + 1 : cols.index(")")].split(", ")
        new_edge_note = insert.args[1 + names.index("new_edge_note")]
        # A whitespace note normalizes to absence: ``edges`` stores NULL
        # (insert_edge runs empty_optional_to_none) and the audit mirror
        # coerces that to "" for its peer-present CHECK -- never the raw
        # "   ". An empty note yields the same "".
        assert new_edge_note == ""


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
