"""Unit tests for keyed audit success and dependent-side edge routing."""

from __future__ import annotations

import pytest

from trackinizer.conftest import make_conn, make_store, new_uuid, set_field_row
from trackinizer.server.store.change_id_slot import set_client_change_id


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
    }
    self_dependent = {**requires, "from_id": child, "to_id": dependent}
    narrows = {**requires, "from_id": child, "to_id": broader, "edge_kind": "narrows"}
    conn.fetch.return_value = [requires, self_dependent, narrows]
    store, _ = make_store(conn)
    async with store.engine.acquire() as held:
        parents = await store._parent_edges(held, child, None)
    assert parents == [(dependent, "Issue", requires), (broader, "Issue", narrows)]
    assert conn.fetch.call_args_list[0].args[1:] == (child,)


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


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
