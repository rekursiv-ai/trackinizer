"""Unit coverage of edge upserts, inferred provenance, and readback."""

from __future__ import annotations

from unittest.mock import AsyncMock

from treekle.absent import ABSENT

import pytest

from trackinizer.conftest import make_conn, make_store, new_uuid, set_field_row
from trackinizer.server.store import edge
from trackinizer.types.errors import ConflictError


@pytest.mark.asyncio
@pytest.mark.parametrize("annotated", [False, True])
async def test_existing_edge_upsert_keeps_unsupplied_annotations(
    monkeypatch: pytest.MonkeyPatch,
    *,
    annotated: bool,
) -> None:
    conn = make_conn()
    set_field_row(conn, _row())
    store, _ = make_store(conn)
    monkeypatch.setattr(edge, "lookup_kind", AsyncMock(return_value="Issue"))
    monkeypatch.setattr(edge, "insert_edge", AsyncMock(return_value=(False, "Issue")))
    left, right = new_uuid(), new_uuid()
    async with store.engine.acquire() as held:
        change, created = await store.add_edge(
            from_id=left,
            to_id=right,
            edge_kind="requires",
            conn=held,
            note="new" if annotated else "",
            labels=("x",) if annotated else (),
            actor="ada",
        )
    assert created is False
    assert (change is not None) == annotated
    updates = [
        c for c in conn.execute.call_args_list if "UPDATE edges" in str(c.args[0])
    ]
    if annotated:
        assert updates[0].args[1:] == (
            None,
            "new",
            None,
            ["x"],
            left,
            right,
            "requires",
        )
    else:
        assert updates == []


@pytest.mark.asyncio
async def test_inferred_provenance_reverses_producer_and_produced_and_stops_recursion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = make_conn()
    store, _ = make_store(conn)
    producer, produced, cause = new_uuid(), new_uuid(), new_uuid()
    infer = AsyncMock(return_value=(producer, produced, "requires"))
    insert = AsyncMock(return_value=(True, "Issue"))
    monkeypatch.setattr(edge, "infer_produced_endpoints", infer)
    monkeypatch.setattr(edge, "lookup_kind", AsyncMock(return_value="Issue"))
    monkeypatch.setattr(edge, "insert_edge", insert)
    async with store.engine.acquire() as held:
        await store._infer_produced_on_conn(
            held,
            from_id=producer,
            to_id=produced,
            edge_kind="requires",
            caused_by=cause,
            api_key_id=None,
            actor="ada",
        )
    infer.assert_awaited_once()
    call = insert.await_args
    assert call is not None
    assert (call.kwargs["from_id"], call.kwargs["to_id"], call.kwargs["edge_kind"]) == (
        produced,
        producer,
        "produced_by",
    )
    audits = [c for c in conn.execute.call_args_list if "change_log" in str(c.args[0])]
    assert [(c.args[4], c.args[6]) for c in audits] == [
        (produced, "edge_added"),
        (producer, "edge_added"),
    ]
    assert audits[0].args[7] == cause
    assert str(audits[0].args[8]).startswith(edge.INFERRED_PROVENANCE_REASON)
    conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("inserted", [False, True])
async def test_submit_edge_audits_logical_endpoints_and_upserts_existing_annotations(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reverse: bool,
    inserted: bool,
) -> None:
    conn = make_conn()
    set_field_row(conn, _row(from_kind="Experiment", to_kind="Belief", valence=0.2))
    store, _ = make_store(conn)
    subject, peer, cause = new_uuid(), new_uuid(), new_uuid()
    insert = AsyncMock(return_value=(inserted, "Belief"))
    monkeypatch.setattr(edge, "lookup_kind", AsyncMock(return_value="Experiment"))
    monkeypatch.setattr(edge, "insert_edge", insert)
    monkeypatch.setattr(edge, "infer_produced_endpoints", AsyncMock(return_value=None))
    async with store.engine.acquire() as held:
        result = await store.insert_edge_and_audit(
            held,
            subject_id=subject,
            subject_kind="Belief" if reverse else "Experiment",
            to_id=peer,
            edge_kind="proves",
            api_key_id=None,
            actor="ada",
            caused_by=cause,
            valence=0.8,
            cite_peer_as_from=reverse,
            require_to_kind="Experiment" if reverse else "Belief",
        )
    assert result is inserted
    call = insert.await_args
    assert call is not None
    assert (call.kwargs["from_id"], call.kwargs["to_id"]) == (
        (peer, subject) if reverse else (subject, peer)
    )
    audits = [c for c in conn.execute.call_args_list if "change_log" in str(c.args[0])]
    assert [c.args[6] for c in audits] == [
        "edge_added" if inserted else "edge_annotation_changed",
    ] * 2
    if inserted:
        assert [c.args[4] for c in audits] == [subject, peer]
        assert audits[0].args[7] == cause
        assert audits[1].args[7] == audits[0].args[1]
    else:
        update = next(
            c for c in conn.execute.call_args_list if "UPDATE edges" in str(c.args[0])
        )
        assert update.args[3] == 0.8


@pytest.mark.asyncio
async def test_declared_peer_kind_mismatch_refuses_before_auditing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = make_conn()
    store, _ = make_store(conn)
    monkeypatch.setattr(edge, "insert_edge", AsyncMock(return_value=(True, "Issue")))
    async with store.engine.acquire() as held:
        with pytest.raises(ConflictError, match="declared as Paper but is a Issue"):
            await store.insert_edge_and_audit(
                held,
                subject_id=new_uuid(),
                subject_kind="Paper",
                to_id=new_uuid(),
                edge_kind="cites_paper",
                api_key_id=None,
                actor="ada",
                caused_by=new_uuid(),
                require_to_kind="Paper",
            )
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("last", [False, True])
async def test_removing_a_label_preserves_order_and_clears_the_last_to_null(
    *,
    last: bool,
) -> None:
    conn = make_conn()
    set_field_row(conn, _row(labels=["a"] if last else ["b", "a", "c"]))
    store, _ = make_store(conn)
    assert (
        await store.remove_edge_label(
            from_id=new_uuid(),
            to_id=new_uuid(),
            edge_kind="requires",
            label=" a ",
            actor="ada",
        )
        is not None
    )
    update = next(
        c for c in conn.execute.call_args_list if "UPDATE edges" in str(c.args[0])
    )
    assert update.args[4] == (None if last else ["b", "c"])


@pytest.mark.asyncio
async def test_unchanged_annotation_emits_no_update_or_audit() -> None:
    conn = make_conn()
    set_field_row(conn, _row(note="same"))
    store, _ = make_store(conn)
    assert (
        await store.set_edge_annotation(
            from_id=new_uuid(),
            to_id=new_uuid(),
            edge_kind="requires",
            note="same",
            labels=ABSENT,
            actor="ada",
        )
        is None
    )
    assert [str(c.args[0]) for c in conn.execute.call_args_list] == ["BEGIN", "COMMIT"]


@pytest.mark.asyncio
@pytest.mark.parametrize("present", [False, True])
async def test_get_edge_returns_none_or_the_complete_edge(*, present: bool) -> None:
    conn = make_conn()
    left, right = new_uuid(), new_uuid()
    set_field_row(
        conn,
        {
            **_row(note="need", labels=["x"]),
            "from_id": left,
            "to_id": right,
            "edge_kind": "requires",
        }
        if present
        else None,
    )
    store, _ = make_store(conn)
    found = await store.get_edge(from_id=left, to_id=right, edge_kind="requires")
    if present:
        assert found is not None
        assert (found.from_id, found.to_id, found.note, found.labels) == (
            left,
            right,
            "need",
            ("x",),
        )
    else:
        assert found is None
    assert conn.fetchrow.call_args_list[0].args[1:] == (left, right, "requires")


def _row(**overrides: object) -> dict[str, object]:
    return {
        "from_kind": "Issue",
        "to_kind": "Issue",
        "priority": None,
        "note": None,
        "valence": None,
        "labels": None,
    } | overrides


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
