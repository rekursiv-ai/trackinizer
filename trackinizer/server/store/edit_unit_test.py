"""Unit tests for replay boundaries, compare-and-set edits, lists, and cost."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import asyncpg
import pytest

from trackinizer.conftest import (
    executed_sql,
    make_conn,
    make_store,
    new_uuid,
    queue_field_rows,
    set_field_row,
)
from trackinizer.server.store.change_id_slot import set_client_change_id
from trackinizer.server.store.edit import _same_json
from trackinizer.types.cost import Cost
from trackinizer.types.errors import ConflictError


if TYPE_CHECKING:
    from uuid import UUID

    from trackinizer.mock_test_support import MockConnection
    from trackinizer.server.store.core import Store


type _Operation = Literal[
    "title",
    "description",
    "owner",
    "judgement",
    "app",
    "label",
    "cost",
    "axis",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("before_read", [False, True])
@pytest.mark.parametrize(
    ("operation", "column", "row"),
    [
        ("title", "title", {"kind": "Issue", "title": "old"}),
        ("description", "description", {"kind": "Issue", "description": "old"}),
        ("owner", "owner", {"kind": "Issue", "owner": None}),
        ("judgement", "belief_judgement", {"kind": "Belief", "belief_judgement": None}),
        ("app", "app", {"kind": "Issue", "app": None}),
        ("label", "labels", {"kind": "Issue", "labels": []}),
        ("cost", "marginal_cost", {"kind": "Issue"}),
        ("axis", "marginal_cost", {"kind": "Issue", "current": 0.0}),
    ],
)
async def test_retry_before_or_after_lock_returns_the_committed_change_without_writes(
    operation: _Operation,
    column: str,
    row: dict[str, object],
    *,
    before_read: bool,
) -> None:
    conn = make_conn()
    target, key = new_uuid(), new_uuid()
    committed = {"actor": "ada", "subject_id": target, "kind": column}
    if before_read:
        queue_field_rows(conn, committed)
    elif operation == "cost":
        queue_field_rows(conn, None, committed)
    else:
        queue_field_rows(conn, None, row, committed)
    conn.fetchval.return_value = "Issue"
    store, _ = make_store(conn)
    set_client_change_id(key)
    try:
        assert await _perform(store, target, operation=operation) == key
    finally:
        set_client_change_id(None)
    assert executed_sql(conn) == ["BEGIN", "COMMIT"]
    assert len(conn.fetchrow.call_args_list) == (
        1 if before_read else 2 if operation == "cost" else 3
    )


@pytest.mark.asyncio
async def test_owner_transition_writes_the_locked_owner_and_audits_attribution() -> (
    None
):
    conn = make_conn()
    set_field_row(conn, {"owner": None, "kind": "Issue"})
    store, _ = make_store(conn)
    target, key = new_uuid(), new_uuid()
    changed = await store.transition_owner(
        target,
        expected_from=None,
        to="ada",
        actor="admin",
        api_key_id=key,
        reason="claim",
    )
    update = next(
        c
        for c in conn.execute.call_args_list
        if "UPDATE inquiries SET owner" in str(c.args[0])
    )
    assert update.args[1:] == ("ada", target)
    [audit] = _audits(conn)
    assert {
        name: audit[name]
        for name in (
            "id",
            "subject_id",
            "api_key_id",
            "actor",
            "reason",
            "old_owner",
            "new_owner",
        )
    } == {
        "id": changed,
        "subject_id": target,
        "api_key_id": key,
        "actor": "admin",
        "reason": "claim",
        "old_owner": None,
        "new_owner": "ada",
    }


@pytest.mark.asyncio
async def test_owner_transition_refuses_a_stale_expected_owner() -> None:
    conn = make_conn()
    set_field_row(conn, {"owner": "bob", "kind": "Issue"})
    store, _ = make_store(conn)
    with pytest.raises(ConflictError, match="expected None, found 'bob'"):
        await store.transition_owner(
            new_uuid(),
            expected_from=None,
            to="ada",
            actor="ada",
        )
    assert not _audits(conn)
    assert not any("UPDATE inquiries" in sql for sql in executed_sql(conn))


@pytest.mark.asyncio
async def test_judgement_transition_audits_the_old_and_new_verdict() -> None:
    conn = make_conn()
    set_field_row(conn, {"belief_judgement": None, "kind": "Belief"})
    store, _ = make_store(conn)
    changed = await store.transition_judgement(
        new_uuid(),
        expected_from=None,
        to="proven",
        actor="ada",
        reason="proof",
    )
    [audit] = _audits(conn)
    assert audit["id"] == changed
    assert (audit["old_belief_judgement"], audit["new_belief_judgement"]) == (
        None,
        "proven",
    )
    assert audit["reason"] == "proof"


@pytest.mark.asyncio
async def test_app_transition_compares_key_order_independently_and_audits_new_object() -> (
    None
):
    conn = make_conn()
    old: dict[str, object] = {"a": 1, "b": 2}
    new: dict[str, object] = {"a": 2}
    set_field_row(conn, {"app": old, "kind": "Issue"})
    store, _ = make_store(conn)
    target = new_uuid()
    changed = await store.transition_app(
        target,
        expected_from={"b": 2, "a": 1},
        to=new,
        actor="ada",
        reason="update",
    )
    [audit] = _audits(conn)
    assert (audit["id"], audit["old_app"], audit["new_app"]) == (changed, old, new)
    update = next(
        c
        for c in conn.execute.call_args_list
        if "UPDATE inquiries SET app" in str(c.args[0])
    )
    assert update.args[1:] == (new, target)


@pytest.mark.asyncio
@pytest.mark.parametrize("unchanged", [False, True])
async def test_app_transition_does_not_write_on_noop_or_stale_json(
    *,
    unchanged: bool,
) -> None:
    conn = make_conn()
    set_field_row(conn, {"app": {"a": True}, "kind": "Issue"})
    store, _ = make_store(conn)
    if unchanged:
        assert (
            await store.transition_app(
                new_uuid(),
                expected_from={"a": True},
                to={"a": True},
                actor="ada",
            )
            is None
        )
    else:
        with pytest.raises(ConflictError, match="app changed since it was read"):
            await store.transition_app(
                new_uuid(),
                expected_from={"a": 1},
                to=None,
                actor="ada",
            )
    assert not _audits(conn)
    assert not any("UPDATE inquiries" in sql for sql in executed_sql(conn))


@pytest.mark.parametrize(
    ("left", "right", "equal"),
    [(True, 1, False), (1, 1.0, False), ({"a": 1, "b": 2}, {"b": 2, "a": 1}, True)],
)
def test_json_comparison_preserves_scalar_types_but_ignores_object_order(
    left: object,
    right: object,
    *,
    equal: bool,
) -> None:
    assert _same_json(left, right) is equal


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["description", "label", "app"])
async def test_database_check_refusal_is_a_conflict_and_rolls_back_without_audit(
    operation: _Operation,
) -> None:
    conn = make_conn()
    set_field_row(
        conn,
        {"kind": "Issue", "description": "old", "labels": [], "app": None},
    )
    conn.execute.side_effect = _reject_update
    store, _ = make_store(conn)
    with pytest.raises(ConflictError, match="check constraint violated"):
        await _perform(store, new_uuid(), operation=operation)
    assert not _audits(conn)
    assert executed_sql(conn)[-1] == "ROLLBACK"


@pytest.mark.asyncio
async def test_removing_subscriber_keeps_both_previous_subscribers_in_notification_snapshot() -> (
    None
):
    conn = make_conn()
    set_field_row(conn, {"kind": "Issue", "subscribers": ["ada", "bob"]})
    store, _ = make_store(conn)
    target = new_uuid()
    changed = await store.remove_subscriber(target, "ada", actor="admin")
    update = next(
        c
        for c in conn.execute.call_args_list
        if "UPDATE inquiries SET subscribers" in str(c.args[0])
    )
    assert update.args[1:] == (["bob"], target)
    [audit] = _audits(conn)
    assert audit["id"] == changed
    assert (audit["old_subscribers"], audit["new_subscribers"]) == (
        ["ada", "bob"],
        ["bob"],
    )
    assert audit["subscribers_snapshot"] == ["ada", "bob"]


@pytest.mark.asyncio
@pytest.mark.parametrize("include", [False, True])
async def test_issue_kind_mutation_preserves_the_other_categories(
    *,
    include: bool,
) -> None:
    conn = make_conn()
    set_field_row(conn, {"kind": "Issue", "issue_kind": ["bug", "task"]})
    store, _ = make_store(conn)
    target = new_uuid()
    changed = (
        await store.add_issue_kind(target, "feature", actor="ada")
        if include
        else await store.remove_issue_kind(target, "bug", actor="ada")
    )
    update = next(
        c
        for c in conn.execute.call_args_list
        if "UPDATE inquiries SET issue_kind" in str(c.args[0])
    )
    assert update.args[1] == (["bug", "task", "feature"] if include else ["task"])
    [audit] = _audits(conn)
    assert audit["id"] == changed
    assert audit["old_issue_kind"] == ["bug", "task"]
    assert audit["new_issue_kind"] == update.args[1]


@pytest.mark.asyncio
async def test_codechange_add_validates_reference_before_writing_it() -> None:
    conn = make_conn()
    set_field_row(conn, {"kind": "Experiment", "experiment_codechanges": None})
    codechange, target = new_uuid(), new_uuid()
    conn.fetch.side_effect = [[{"id": codechange, "kind": "CodeChange"}], []]
    store, _ = make_store(conn)
    changed = await store.add_codechange(target, codechange, actor="ada")
    lookup = conn.fetch.call_args_list[0]
    assert lookup.args[1:] == ([codechange],)
    assert "FOR SHARE" in str(lookup.args[0])
    [audit] = _audits(conn)
    assert audit["id"] == changed
    assert audit["old_experiment_codechanges"] is None
    assert audit["new_experiment_codechanges"] == [codechange]


@pytest.mark.asyncio
async def test_malformed_source_is_refused_before_any_database_access() -> None:
    store, engine = make_store()
    with pytest.raises(ConflictError, match="scheme-tagged identifier"):
        await store.set_source(new_uuid(), "no-scheme", actor="ada")
    assert engine.conn.mock_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("axis", ["agent_usd", "resource_usd"])
@pytest.mark.parametrize("value", [0.0, 3.0])
async def test_cost_axis_writes_only_the_delta_to_the_requested_axis(
    axis: Literal["agent_usd", "resource_usd"],
    value: float,
) -> None:
    conn = make_conn()
    set_field_row(conn, {"kind": "Issue", "current": 2.0})
    store, _ = make_store(conn)
    target = new_uuid()
    changed = await store.set_cost_axis(target, axis, value, actor="ada")
    cost = next(
        c for c in conn.fetchrow.call_args_list if "WITH probe" in str(c.args[0])
    )
    assert cost.args[1:] == (
        value - 2.0 if axis == "agent_usd" else 0.0,
        value - 2.0 if axis == "resource_usd" else 0.0,
        target,
    )
    [audit] = _audits(conn)
    assert (audit["id"], audit["kind"], audit["actor"]) == (
        changed,
        "marginal_cost",
        "ada",
    )


@pytest.mark.asyncio
async def test_add_cost_returns_the_emitted_change_for_a_nonzero_delta() -> None:
    conn = make_conn()
    conn.fetchval.return_value = "Issue"
    store, _ = make_store(conn)
    changed = await store.add_cost(new_uuid(), Cost(agent_usd=0.5), actor="ada")
    [audit] = _audits(conn)
    assert audit["id"] == changed
    assert audit["new_marginal_cost_agent_usd"] == 0.5
    assert audit["new_marginal_cost_resource_usd"] == 0.0


async def _perform(store: Store, target: UUID, *, operation: _Operation) -> UUID | None:
    match operation:
        case "title":
            return await store.set_title(target, "new", actor="ada")
        case "description":
            return await store.set_description(target, "new", actor="ada")
        case "owner":
            return await store.transition_owner(
                target,
                expected_from=None,
                to="ada",
                actor="ada",
            )
        case "judgement":
            return await store.transition_judgement(
                target,
                expected_from=None,
                to="proven",
                actor="ada",
            )
        case "app":
            return await store.transition_app(
                target,
                expected_from=None,
                to={"a": 1},
                actor="ada",
            )
        case "label":
            return await store.add_label(target, "new", actor="ada")
        case "cost":
            return await store.add_cost(target, Cost(agent_usd=1.0), actor="ada")
        case "axis":
            return await store.set_cost_axis(target, "agent_usd", 1.0, actor="ada")


def _audits(conn: MockConnection) -> list[dict[str, object]]:
    return [
        dict(
            zip(
                str(call.args[0]).split("(", 1)[1].split(")", 1)[0].split(", "),
                call.args[1:],
                strict=True,
            ),
        )
        for call in conn.execute.call_args_list
        if "INSERT INTO change_log" in str(call.args[0])
    ]


async def _reject_update(query: str, *args: object) -> str:
    del args
    if query.startswith("UPDATE inquiries SET"):
        raise asyncpg.CheckViolationError("refused")
    return "OK"


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
