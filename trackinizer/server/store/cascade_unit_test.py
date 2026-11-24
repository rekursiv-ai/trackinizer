"""Unit tests for keyed audit success and dependent-side edge routing."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from trackinizer.conftest import make_conn, make_store, new_uuid, set_field_row
from trackinizer.server.store.change_id_slot import set_client_change_id


if TYPE_CHECKING:
    from uuid import UUID

    from trackinizer.mock_test_support import MockConnection


@pytest.mark.asyncio
async def test_successful_keyed_change_releases_its_savepoint() -> None:
    conn = make_conn()
    store, _ = make_store(conn)
    subject, key = new_uuid(), new_uuid()
    set_client_change_id(key)
    try:
        async with store.engine.acquire() as held:
            changed = await store.emit_change(
                held,
                subject_id=subject,
                subject_kind="Issue",
                kind="created",
            )
    finally:
        set_client_change_id(None)
    assert changed == (key, ())
    sql = [str(c.args[0]) for c in conn.execute.call_args_list]
    assert sql[0] == "SAVEPOINT emit_change"
    assert sql[-1] == "RELEASE SAVEPOINT emit_change"
    assert conn.execute.call_args_list[1].args[1] == key


@pytest.mark.asyncio
async def test_parent_edges_choose_the_dependent_and_suppress_self_alerts() -> None:
    conn = make_conn()
    child, dependent, broader = new_uuid(), new_uuid(), new_uuid()
    requires = {
        "from_id": dependent,
        "from_kind": "Issue",
        "to_id": child,
        "to_kind": "Issue",
        "edge_kind": "requires",
        "peer_subscribed": True,
    }
    self_dependent = {**requires, "from_id": child, "to_id": dependent}
    narrows = {
        **requires,
        "from_id": child,
        "to_id": broader,
        "edge_kind": "narrows",
        "peer_subscribed": False,
    }
    conn.fetch.return_value = [requires, self_dependent, narrows]
    store, _ = make_store(conn)
    async with store.engine.acquire() as held:
        parents = await store._parent_edges(held, child, None)
    assert parents == [
        (dependent, "Issue", requires, True),
        (broader, "Issue", narrows, False),
    ]
    assert conn.fetch.call_args_list[0].args[1:] == (child,)


def _edge_row(
    *,
    from_id: object,
    to_id: object,
    edge_kind: str = "narrows",
    from_kind: str = "Issue",
    to_kind: str = "Issue",
    peer_subscribed: bool,
) -> dict[str, object]:
    return {
        "from_id": from_id,
        "from_kind": from_kind,
        "to_id": to_id,
        "to_kind": to_kind,
        "edge_kind": edge_kind,
        "priority": None,
        "note": None,
        "valence": None,
        "labels": None,
        "peer_subscribed": peer_subscribed,
    }


def _change_log_rows(conn: MockConnection) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for call in conn.execute.call_args_list:
        sql = str(call.args[0])
        if "INSERT INTO change_log" not in sql:
            continue
        columns = sql.split("(", 1)[1].split(")", 1)[0].split(", ")
        rows.append(dict(zip(columns, call.args[1:], strict=True)))
    return rows


@pytest.mark.asyncio
async def test_cascade_skips_emit_and_locks_for_unsubscribed_parent() -> None:
    conn = make_conn()
    child, parent, origin = new_uuid(), new_uuid(), new_uuid()
    conn.fetch.side_effect = [
        [_edge_row(from_id=child, to_id=parent, peer_subscribed=False)],
        [],
    ]
    store, _ = make_store(conn)
    async with store.engine.acquire() as held:
        await store._cascade_dependency_changed(
            held,
            child_id=child,
            child_kind="Issue",
            caused_by=origin,
        )
    assert _change_log_rows(conn) == []
    conn.fetchrow.assert_not_awaited()
    assert [c.args[1] for c in conn.fetch.call_args_list] == [child, parent]


@pytest.mark.asyncio
async def test_cascade_emits_for_subscribed_parent() -> None:
    conn = make_conn()
    experiment, belief, origin = new_uuid(), new_uuid(), new_uuid()
    conn.fetch.side_effect = [
        [
            _edge_row(
                from_id=experiment,
                to_id=belief,
                edge_kind="proves",
                from_kind="Experiment",
                to_kind="Belief",
                peer_subscribed=True,
            ),
        ],
        [],
    ]
    store, _ = make_store(conn)
    async with store.engine.acquire() as held:
        await store._cascade_dependency_changed(
            held,
            child_id=experiment,
            child_kind="Experiment",
            caused_by=origin,
        )
    logged = _change_log_rows(conn)
    assert len(logged) == 1
    assert logged[0]["subject_id"] == belief
    assert logged[0]["subject_kind"] == "Belief"
    assert logged[0]["kind"] == "dependency_changed"
    assert logged[0]["actor"] == "librarian"
    assert logged[0]["caused_by"] == origin
    assert logged[0]["new_peer_id"] == experiment
    assert logged[0]["new_peer_kind"] == "Experiment"
    assert logged[0]["new_peer_edge_kind"] == "proves"


@pytest.mark.asyncio
async def test_cascade_alerts_every_edge_but_walks_a_shared_ancestor_once() -> None:
    conn = make_conn()
    child, left, right, shared, top, origin = (new_uuid() for _ in range(6))
    edges: dict[UUID, list[dict[str, object]]] = {
        child: [
            _edge_row(from_id=child, to_id=left, peer_subscribed=True),
            _edge_row(from_id=child, to_id=right, peer_subscribed=True),
        ],
        left: [_edge_row(from_id=left, to_id=shared, peer_subscribed=True)],
        right: [_edge_row(from_id=right, to_id=shared, peer_subscribed=True)],
        shared: [_edge_row(from_id=shared, to_id=top, peer_subscribed=True)],
    }

    async def touching(sql: str, inquiry_id: UUID) -> list[dict[str, object]]:
        del sql
        return edges.get(inquiry_id, [])

    conn.fetch.side_effect = touching
    store, _ = make_store(conn)
    async with store.engine.acquire() as held:
        await store._cascade_dependency_changed(
            held,
            child_id=child,
            child_kind="Issue",
            caused_by=origin,
        )
    # The diamond's apex hears from both sides; its own ancestor hears once.
    assert [row["subject_id"] for row in _change_log_rows(conn)] == [
        left,
        right,
        shared,
        shared,
        top,
    ]
    assert [c.args[1] for c in conn.fetch.call_args_list] == [
        child,
        left,
        right,
        shared,
        top,
    ]


@pytest.mark.asyncio
async def test_cascade_walks_through_unsubscribed_parent_to_subscribed_grandparent() -> (
    None
):
    conn = make_conn()
    child, parent, grandparent, origin = new_uuid(), new_uuid(), new_uuid(), new_uuid()
    conn.fetch.side_effect = [
        [_edge_row(from_id=child, to_id=parent, peer_subscribed=False)],
        [_edge_row(from_id=parent, to_id=grandparent, peer_subscribed=True)],
        [],
    ]
    store, _ = make_store(conn)
    async with store.engine.acquire() as held:
        await store._cascade_dependency_changed(
            held,
            child_id=child,
            child_kind="Issue",
            caused_by=origin,
        )
    logged = _change_log_rows(conn)
    assert len(logged) == 1
    assert logged[0]["subject_id"] == grandparent
    assert logged[0]["kind"] == "dependency_changed"
    assert logged[0]["caused_by"] == origin
    assert logged[0]["new_peer_id"] == parent
    # Only the subscribed grandparent was locked/updated in ``inquiries``.
    assert [c.args[3] for c in conn.fetchrow.call_args_list] == [grandparent]


@pytest.mark.asyncio
async def test_cascade_chains_cause_to_last_emitted_subscribed_ancestor() -> None:
    conn = make_conn()
    child, parent, middle, top, origin = (
        new_uuid(),
        new_uuid(),
        new_uuid(),
        new_uuid(),
        new_uuid(),
    )
    conn.fetch.side_effect = [
        [_edge_row(from_id=child, to_id=parent, peer_subscribed=True)],
        [_edge_row(from_id=parent, to_id=middle, peer_subscribed=False)],
        [_edge_row(from_id=middle, to_id=top, peer_subscribed=True)],
        [],
    ]
    store, _ = make_store(conn)
    async with store.engine.acquire() as held:
        await store._cascade_dependency_changed(
            held,
            child_id=child,
            child_kind="Issue",
            caused_by=origin,
        )
    logged = _change_log_rows(conn)
    assert [(row["subject_id"], row["caused_by"]) for row in logged] == [
        (parent, origin),
        (top, logged[0]["id"]),
    ]


@pytest.mark.asyncio
async def test_purge_of_to_endpoint_audits_the_surviving_from_endpoint() -> None:
    conn = make_conn()
    target, peer = new_uuid(), new_uuid()
    set_field_row(conn, {"kind": "Issue", "owner": None})
    conn.fetch.side_effect = [
        [
            {
                "from_id": peer,
                "from_kind": "Issue",
                "to_id": target,
                "to_kind": "Issue",
                "edge_kind": "requires",
                "priority": 10,
                "note": "need",
                "valence": None,
                "labels": ["dependency"],
                "peer_subscribed": True,
            },
        ],
        [],
    ]
    store, _ = make_store(conn)
    cause = await store.purge(target, actor="ada")
    audits = [c for c in conn.execute.call_args_list if "change_log" in str(c.args[0])]
    assert [(c.args[4], c.args[6]) for c in audits] == [
        (target, "purged"),
        (peer, "dependency_changed"),
        (peer, "edge_removed"),
    ]
    assert audits[-1].args[7] == cause
    columns = str(audits[-1].args[0]).split("(", 1)[1].split(")", 1)[0].split(", ")
    logged = dict(zip(columns, audits[-1].args[1:], strict=True))
    assert logged["old_peer_id"] == target
    assert logged["old_edge_note"] == "need"
    assert logged["old_edge_labels"] == ["dependency"]
    assert conn.execute.call_args_list[-2].args == (
        "DELETE FROM inquiries WHERE id = $1",
        target,
    )


@pytest.mark.asyncio
async def test_purge_skips_unsubscribed_dependent_cascade_and_reaches_grandparent() -> (
    None
):
    conn = make_conn()
    target, peer, grandparent = new_uuid(), new_uuid(), new_uuid()
    set_field_row(conn, {"kind": "Issue", "owner": None})
    conn.fetch.side_effect = [
        [
            _edge_row(
                from_id=peer,
                to_id=target,
                edge_kind="requires",
                peer_subscribed=False,
            ),
        ],
        [_edge_row(from_id=peer, to_id=grandparent, peer_subscribed=True)],
        [],
    ]
    store, _ = make_store(conn)
    cause = await store.purge(target, actor="ada")
    logged = _change_log_rows(conn)
    assert [(row["subject_id"], row["kind"]) for row in logged] == [
        (target, "purged"),
        (grandparent, "dependency_changed"),
        (peer, "edge_removed"),
    ]
    assert logged[1]["caused_by"] == cause


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
