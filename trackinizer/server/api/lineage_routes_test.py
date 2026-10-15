"""Historical research lineage through bounded pages over the real inquiry schema."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from treekle import from_plain, json_loads

import pytest


if TYPE_CHECKING:
    import httpx2

    from trackinizer.lib.postgres import Conn
    from trackinizer.server.store.core import Store


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_historical_campaigns_keep_nested_issues_and_unnumbered_results(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """VCC, Craftax and older nanochat shapes work without launch labels."""
    client, store = pglite_route_client
    root, arm, trial, experiment = (uuid4() for _ in range(4))
    async with store.engine.acquire() as conn:
        for index, record_id in enumerate((root, arm, trial, experiment)):
            await _row(
                conn,
                record_id=record_id,
                kind="Experiment" if index == 3 else "Issue",
                seq=index + 1,
            )
        for child, parent, child_kind in (
            (arm, root, "Issue"),
            (trial, arm, "Issue"),
            (experiment, trial, "Experiment"),
        ):
            await _edge(conn, child=child, parent=parent, child_kind=child_kind)
        await _edge(conn, child=arm, parent=root, edge_kind="narrows")

    for selected, expected in ((root, arm), (arm, trial), (trial, experiment)):
        response = await client.get(
            f"/api/visuals/lineage/{selected}",
            params={"direction": "in"},
        )
        assert response.status_code == 200, response.text
        body = json_loads(response.content, dict[str, object])
        assert "records" in body, (
            "Lineage must expose produced Issue and Experiment records"
        )
        assert [
            row["id"] for row in from_plain(body["records"], list[dict[str, object]])
        ] == [str(expected)]
        assert from_plain(body["target"], dict[str, object])["id"] == str(selected)
        assert body["next_cursor"] is None
    first = await client.get(
        f"/api/visuals/lineage/{root}",
        params={"direction": "in"},
    )
    edges = from_plain(
        json_loads(first.content, dict[str, object])["edges"],
        list[dict[str, object]],
    )
    assert {edge["edge_kind"] for edge in edges} == {"narrows", "produced_by"}
    assert all(
        edge["from_id"] == str(arm) and edge["to_id"] == str(root) for edge in edges
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_experiment_keeps_every_origin_and_signed_citation_direction(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A shared nanochat result retains two producers and both citation signs."""
    client, store = pglite_route_client
    parents = [uuid4(), uuid4()]
    experiment, claim, evidence = uuid4(), uuid4(), uuid4()
    async with store.engine.acquire() as conn:
        for index, parent in enumerate(parents):
            await _row(conn, record_id=parent, kind="Issue", seq=index + 1)
        await _row(conn, record_id=experiment, kind="Experiment", seq=1)
        await _row(conn, record_id=claim, kind="Belief", seq=1)
        await _row(conn, record_id=evidence, kind="Experiment", seq=2)
        for parent in parents:
            await _edge(conn, child=experiment, parent=parent, child_kind="Experiment")
        await _edge(
            conn,
            child=experiment,
            parent=claim,
            child_kind="Experiment",
            parent_kind="Belief",
            edge_kind="proves",
            valence=-0.9,
        )
        await _edge(
            conn,
            child=evidence,
            parent=experiment,
            child_kind="Experiment",
            parent_kind="Experiment",
            edge_kind="favors",
            valence=0.8,
        )

    response = await client.get(
        f"/api/visuals/lineage/{experiment}",
        params={"direction": "out", "peer_kind": "Issue"},
    )
    assert response.status_code == 200, response.text
    body = json_loads(response.content, dict[str, object])
    assert "records" in body, (
        "Lineage must preserve every producer rather than selecting one"
    )
    assert {
        row["id"] for row in from_plain(body["records"], list[dict[str, object]])
    } == {str(parent) for parent in parents}
    assert from_plain(body["target"], dict[str, object])["id"] == str(experiment)
    overview = await client.get(f"/api/visuals/timeline/{experiment}")
    assert overview.status_code == 200, overview.text
    assert json_loads(overview.content, dict[str, object])["issue"] is None
    for direction, kind, expected_sign in (
        ("out", "proves", -0.9),
        ("in", "favors", 0.8),
    ):
        read = await client.get(
            f"/api/visuals/lineage/{experiment}",
            params={"direction": direction, "edge_kind": kind},
        )
        assert read.status_code == 200, read.text
        citation = from_plain(
            json_loads(read.content, dict[str, object])["edges"],
            list[dict[str, object]],
        )[0]
        assert citation["valence"] == expected_sign
        assert citation["from_id" if direction == "out" else "to_id"] == str(experiment)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_keyset_pages_keep_tied_dates_and_bind_the_cursor_to_its_section(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Every peer appears once; parallel edge kinds consume one card slot."""
    client, store = pglite_route_client
    root = uuid4()
    children = [UUID(int=index + 1) for index in range(5)]
    async with store.engine.acquire() as conn:
        await _row(conn, record_id=root, kind="Issue", seq=1)
        for index, child in enumerate(children):
            await _row(conn, record_id=child, kind="Issue", seq=index + 2)
            await _edge(conn, child=child, parent=root)
            await _edge(conn, child=child, parent=root, edge_kind="narrows")
    cursor = ""
    found: list[str] = []
    first_cursor = ""
    for page_index in range(3):
        params = {"direction": "in", "page_size": "2"}
        if cursor:
            params["cursor"] = cursor
        response = await client.get(f"/api/visuals/lineage/{root}", params=params)
        assert response.status_code == 200, response.text
        body = json_loads(response.content, dict[str, object])
        assert "records" in body, "Large campaigns need paged records"
        records = from_plain(body["records"], list[dict[str, object]])
        assert len(records) <= 2
        found.extend(from_plain(record["id"], str) for record in records)
        edges = from_plain(body["edges"], list[dict[str, object]])
        expected_edges = {
            (record["id"], kind, str(root))
            for record in records
            for kind in ("narrows", "produced_by")
        }
        assert {
            (edge["from_id"], edge["edge_kind"], edge["to_id"]) for edge in edges
        } == expected_edges
        assert len(edges) == len(expected_edges)
        cursor = from_plain(body["next_cursor"], str, default="")
        if page_index == 0:
            assert cursor
            first_cursor = cursor
        if page_index == 2:
            assert body["next_cursor"] is None
    assert found == [str(child) for child in children]
    assert cursor == ""
    for changed in (
        {"direction": "out"},
        {"peer_kind": "Experiment"},
        {"edge_kind": "requires"},
    ):
        response = await client.get(
            f"/api/visuals/lineage/{root}",
            params={"cursor": first_cursor, "direction": "in", **changed},
        )
        assert response.status_code == 422, response.text
    for invalid in (
        {"page_size": "51"},
        {"page_size": "0"},
        {"cursor": "bad"},
        {"edge_kind": "made-up"},
    ):
        response = await client.get(f"/api/visuals/lineage/{root}", params=invalid)
        assert response.status_code == 422, response.text


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_orphan_records_remain_visible_and_missing_records_are_404(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """No producer and no launch metadata are valid states for every record kind."""
    client, store = pglite_route_client
    for kind in ("Experiment", "Paper", "Issue"):
        record_id = uuid4()
        async with store.engine.acquire() as conn:
            await _row(conn, record_id=record_id, kind=kind, seq=1)
        response = await client.get(f"/api/visuals/lineage/{record_id}")
        assert response.status_code == 200, response.text
        body = json_loads(response.content, dict[str, object])
        assert "records" in body, "An unlinked record must remain selected"
        assert body["records"] == body["edges"] == []
        assert from_plain(body["target"], dict[str, object])["id"] == str(record_id)
    assert (await client.get(f"/api/visuals/lineage/{uuid4()}")).status_code == 404


async def _row(conn: Conn, *, record_id: UUID, kind: str, seq: int) -> None:
    await conn.execute(
        "INSERT INTO inquiries (id, kind, seq, account, title, created, modified) "
        "VALUES ($1, $2, $3, $4, $5, $6, $6)",
        record_id,
        kind,
        seq,
        "lineage-test@example.com",
        f"{kind} {seq}",
        datetime(2026, 1, 1, tzinfo=UTC),
    )


async def _edge(
    conn: Conn,
    *,
    child: UUID,
    parent: UUID,
    child_kind: str = "Issue",
    parent_kind: str = "Issue",
    edge_kind: str = "produced_by",
    valence: float | None = None,
) -> None:
    await conn.execute(
        "INSERT INTO edges (from_id, from_kind, to_id, to_kind, edge_kind, valence) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        child,
        child_kind,
        parent,
        parent_kind,
        edge_kind,
        valence,
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
