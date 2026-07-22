"""Tests for ``Store`` edge mutations -- add/remove edge, annotations."""

from __future__ import annotations

import pytest

from trackinizer.conftest import (
    executed_sql,
    make_conn,
    make_store,
    new_uuid,
    set_field_row,
)
from trackinizer.types.errors import ConflictError, NotFoundError


class TestEdge:
    @pytest.mark.asyncio
    async def test_add_edge_emits_change_after_endpoint_lookup(self) -> None:
        conn = make_conn()
        # Both endpoints have the same kind so the narrows-edge cycle
        # check runs through ``_reject_edge_cycle`` (advisory lock + CTE walk).
        conn.fetchval.side_effect = ["Issue", "Issue", False, new_uuid()]
        store, _engine = make_store(conn)
        await store.add_edge(
            from_id=new_uuid(),
            to_id=new_uuid(),
            edge_kind="narrows",
            actor="user",
        )
        sqls = executed_sql(conn)
        # INSERT INTO edges moved to fetchval (RETURNING from_id).
        assert any(
            "INSERT INTO edges" in c.args[0] for c in conn.fetchval.call_args_list
        )
        # Two change_log rows: one against from-side, one against to-side.
        change_log_inserts = [s for s in sqls if "INSERT INTO change_log" in s]
        assert len(change_log_inserts) == 2
        change_kinds = [
            call.args[6]
            for call in conn.execute.call_args_list
            if "INSERT INTO change_log" in call.args[0]
        ]
        assert change_kinds == ["edge_added", "edge_added"]

    @pytest.mark.asyncio
    async def test_add_edge_missing_endpoint_raises(self) -> None:
        conn = make_conn()
        # First lookup (from-side) returns None -> ConflictError.
        set_field_row(conn, None)
        store, _engine = make_store(conn)
        with pytest.raises(ConflictError):
            await store.add_edge(
                from_id=new_uuid(),
                to_id=new_uuid(),
                edge_kind="narrows",
                actor="user",
            )

    @pytest.mark.asyncio
    async def test_remove_edge_silent_when_missing(self) -> None:
        conn = make_conn()
        set_field_row(conn, None)
        store, _engine = make_store(conn)
        await store.remove_edge(
            from_id=new_uuid(),
            to_id=new_uuid(),
            edge_kind="narrows",
            actor="user",
        )
        sqls = executed_sql(conn)
        assert not any("DELETE FROM edges" in s for s in sqls)

    @pytest.mark.asyncio
    async def test_set_edge_annotation_emits_both_endpoints(self) -> None:
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
        store, engine = make_store(conn)
        await store.set_edge_annotation(
            from_id=new_uuid(),
            to_id=new_uuid(),
            edge_kind="proves",
            note="new",
            valence=0.8,
            labels=["new-label"],
            actor="alice",
        )
        sqls = executed_sql(conn)
        assert any("UPDATE edges SET priority" in sql for sql in sqls)
        assert sum("INSERT INTO change_log" in s for s in sqls) == 2
        change_kinds = [
            call.args[6]
            for call in conn.execute.call_args_list
            if "INSERT INTO change_log" in call.args[0]
        ]
        assert change_kinds == ["edge_annotation_changed", "edge_annotation_changed"]
        assert len(engine.notify_calls) == 2

    @pytest.mark.asyncio
    async def test_set_edge_annotation_unknown_edge_raises_not_found(self) -> None:
        # A missing edge is not-found (404), one rule with the inquiry
        # not-found paths -- not a 409 state-clash.
        conn = make_conn()
        set_field_row(conn, None)
        store, _engine = make_store(conn)
        with pytest.raises(NotFoundError, match="edge not found"):
            await store.set_edge_annotation(
                from_id=new_uuid(),
                to_id=new_uuid(),
                edge_kind="proves",
                note="new",
                actor="alice",
            )

    @pytest.mark.asyncio
    async def test_set_edge_annotation_null_clears_nullable_fields(self) -> None:
        conn = make_conn()
        set_field_row(
            conn,
            {
                "from_kind": "Issue",
                "to_kind": "Issue",
                "priority": 10,
                "note": "old",
                "valence": 0.2,
                "labels": ["old-label"],
            },
        )
        store, _engine = make_store(conn)
        await store.set_edge_annotation(
            from_id=new_uuid(),
            to_id=new_uuid(),
            edge_kind="narrows",
            priority=None,
            note=None,
            valence=None,
            labels=None,
            actor="alice",
        )
        update = next(
            c for c in conn.execute.call_args_list if "UPDATE edges SET" in c.args[0]
        )
        assert update.args[1:5] == (None, None, None, None)

    @pytest.mark.asyncio
    async def test_add_label_on_legacy_null_valence_does_not_heal(self) -> None:
        # A legacy ``proves`` row stored with NULL valence: adding a label
        # must touch only ``labels`` -- it must NOT silently rewrite valence to
        # the citation default and fire a phantom edge_annotation_changed
        # attributing a valence change to the label-mutator.
        conn = make_conn()
        set_field_row(
            conn,
            {
                "from_kind": "Belief",
                "to_kind": "Experiment",
                "priority": None,
                "note": "old",
                "valence": None,
                "labels": None,
            },
        )
        store, _engine = make_store(conn)
        await store.add_edge_label(
            from_id=new_uuid(),
            to_id=new_uuid(),
            edge_kind="proves",
            label="mechanism",
            actor="alice",
        )
        update = next(
            (c for c in conn.execute.call_args_list if "UPDATE edges SET" in c.args[0]),
            None,
        )
        assert update is not None, "label add should issue an UPDATE for labels"
        # args: (sql, priority, note, valence, labels, from, to, edge_kind)
        assert update.args[3] is None, "valence must stay NULL, not heal to default"
        assert update.args[4] == ["mechanism"]

    @pytest.mark.asyncio
    async def test_remove_edge_deletes_and_emits_with_annotation(self) -> None:
        conn = make_conn()
        set_field_row(
            conn,
            {
                "from_kind": "Belief",
                "to_kind": "Experiment",
                "priority": None,
                "note": "mechanistic framing",
                "valence": 0.8,
                "labels": ["mechanism"],
            },
        )
        store, _engine = make_store(conn)
        await store.remove_edge(
            from_id=new_uuid(),
            to_id=new_uuid(),
            edge_kind="proves",
            actor="alice",
        )
        sqls = executed_sql(conn)
        assert any("DELETE FROM edges" in sql for sql in sqls)
        assert sum("INSERT INTO change_log" in s for s in sqls) == 2
        change_kinds = [
            call.args[6]
            for call in conn.execute.call_args_list
            if "INSERT INTO change_log" in call.args[0]
        ]
        assert change_kinds == ["edge_removed", "edge_removed"]


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
