"""Bounded evidence timeline endpoint tests over the real inquiry schema."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import uuid

import pytest

from trackinizer.lib.custom_json import convert, parse
from trackinizer.server.api.app import app
from trackinizer.server.visuals.catalog import (
    StaticVisual,
    TimelineVisual,
    Workspace,
)


if TYPE_CHECKING:
    import httpx2

    from trackinizer.lib.postgres import Conn
    from trackinizer.server.store.core import Store


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_timeline_caps_rows_and_preserves_signed_claim_edges(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A requested timeline returns bounded chronological directions and evidence."""
    client, store = pglite_route_client
    root_id = uuid.uuid4()
    direction_ids = [uuid.uuid4() for _ in range(3)]
    result_ids = [[uuid.uuid4() for _ in range(3)] for _ in direction_ids]
    claim_ids = [[uuid.uuid4() for _ in range(8)] for _ in direction_ids]
    started = datetime(2026, 1, 1, tzinfo=UTC)

    async with store.engine.acquire() as conn:
        await _insert_row(
            conn,
            record_id=root_id,
            kind="Issue",
            seq=1,
            title="Root investigation " * 300,
            created=started,
            description="Root description " * 300,
        )
        for direction_index, direction_id in enumerate(direction_ids):
            issue_kind = ["question"] if direction_index == 0 else ["task"]
            await _insert_row(
                conn,
                record_id=direction_id,
                kind="Issue",
                seq=direction_index + 2,
                title=f"Direction {direction_index}",
                created=started + timedelta(days=direction_index + 1),
                issue_kind=issue_kind,
            )
            await _insert_edge(
                conn,
                from_id=direction_id,
                from_kind="Issue",
                to_id=root_id,
                to_kind="Issue",
                edge_kind="narrows",
            )
            for claim_index, claim_id in enumerate(claim_ids[direction_index]):
                await _insert_row(
                    conn,
                    record_id=claim_id,
                    kind="Belief",
                    seq=direction_index * 8 + claim_index + 1,
                    title=f"Claim {direction_index}-{claim_index}",
                    created=started
                    + timedelta(days=20 + direction_index * 8 + claim_index),
                )
            for result_index, result_id in enumerate(result_ids[direction_index]):
                await _insert_row(
                    conn,
                    record_id=result_id,
                    kind="Experiment",
                    seq=direction_index * 3 + result_index + 1,
                    title=f"Result {direction_index}-{result_index}",
                    created=started
                    + timedelta(days=10 + direction_index * 3 + result_index),
                    outcome=(
                        "Measured outcome " * 300
                        if direction_index == 0 and result_index == 2
                        else f"Measured {direction_index}-{result_index}"
                    ),
                )
                await _insert_edge(
                    conn,
                    from_id=result_id,
                    from_kind="Experiment",
                    to_id=direction_id,
                    to_kind="Issue",
                    edge_kind="produced_by",
                )
                for claim_index, claim_id in enumerate(claim_ids[direction_index]):
                    await _insert_edge(
                        conn,
                        from_id=result_id,
                        from_kind="Experiment",
                        to_id=claim_id,
                        to_kind="Belief",
                        edge_kind="proves" if claim_index == 0 else "favors",
                        valence=0.8 if claim_index == 0 else -0.7,
                        note="bounded evidence note " * 200
                        if claim_index == 0
                        else None,
                    )

    response = await client.get(
        f"/api/visuals/timeline/{root_id}",
        params={"direction_limit": 2, "results_per_direction": 1},
    )
    assert response.status_code == 200, response.text
    body = parse(response.content, dict[str, object])
    target = convert(body["target"], dict[str, object])
    assert len(convert(target["title"], str)) == 2_000
    assert len(convert(target["description"], str)) == 2_000
    directions = convert(body["directions"], list[dict[str, object]])
    assert [
        convert(item["issue"], dict[str, object])["title"] for item in directions
    ] == [
        "Direction 0",
        "Direction 1",
    ]
    assert body["directions_truncated"] is True
    assert all(direction["results_truncated"] is True for direction in directions)
    first_result = convert(
        convert(directions[0]["results"], list[dict[str, object]])[0],
        dict[str, object],
    )
    outcome = convert(
        convert(first_result["record"], dict[str, object])["outcome"],
        str,
    )
    assert len(outcome) == 2_000
    assert outcome.startswith("Measured outcome ")
    evidence = convert(first_result["evidence"], list[dict[str, object]])
    assert len(evidence) == 6
    assert first_result["evidence_truncated"] is True
    assert {(item["edge_kind"], item["valence"]) for item in evidence} == {
        ("proves", 0.8),
        ("favors", -0.7),
    }
    assert {
        convert(item["claim"], dict[str, object])["title"] for item in evidence
    } == {f"Claim 0-{index}" for index in range(6)}
    note = convert(evidence[0]["note"], str)
    assert len(note) == 2_000
    assert note.startswith("bounded evidence note ")
    assert [
        convert(item, dict[str, object])["title"]
        for item in convert(body["unresolved_questions"], list[dict[str, object]])
    ] == ["Direction 0"]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_experiment_anchor_keeps_the_selected_result_in_its_issue_timeline(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """An older selected Experiment remains visible when newer siblings fill the cap."""
    client, store = pglite_route_client
    issue_id = uuid.uuid4()
    selected_id = uuid.uuid4()
    other_ids = [uuid.uuid4() for _ in range(2)]
    started = datetime(2026, 1, 1, tzinfo=UTC)
    async with store.engine.acquire() as conn:
        await _insert_row(
            conn,
            record_id=issue_id,
            kind="Issue",
            seq=1,
            title="Direction",
            created=started,
        )
        await _insert_row(
            conn,
            record_id=selected_id,
            kind="Experiment",
            seq=1,
            title="Selected result",
            created=started,
            outcome="Selected outcome",
        )
        await _insert_edge(
            conn,
            from_id=selected_id,
            from_kind="Experiment",
            to_id=issue_id,
            to_kind="Issue",
            edge_kind="produced_by",
        )
        for index, result_id in enumerate(other_ids):
            await _insert_row(
                conn,
                record_id=result_id,
                kind="Experiment",
                seq=index + 2,
                title=f"Newer result {index}",
                created=started + timedelta(days=index + 1),
                outcome="Newer outcome",
            )
            await _insert_edge(
                conn,
                from_id=result_id,
                from_kind="Experiment",
                to_id=issue_id,
                to_kind="Issue",
                edge_kind="produced_by",
            )

    response = await client.get(
        f"/api/visuals/timeline/{selected_id}",
        params={"results_per_direction": 1},
    )
    assert response.status_code == 200, response.text
    body = parse(response.content, dict[str, object])
    assert convert(body["issue"], dict[str, object])["id"] == str(issue_id)
    results = convert(body["root_results"], list[dict[str, object]])
    assert len(results) == 1
    assert convert(results[0]["record"], dict[str, object])["id"] == str(other_ids[-1])
    selected = convert(body["selected_result"], dict[str, object])
    assert convert(selected["record"], dict[str, object])["id"] == str(selected_id)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_orphan_experiment_has_no_issue_but_keeps_its_selected_result(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """An Experiment without a producer Issue remains visible on its own."""
    client, store = pglite_route_client
    experiment_id = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await _insert_row(
            conn,
            record_id=experiment_id,
            kind="Experiment",
            seq=1,
            title="Standalone result",
            created=datetime(2026, 1, 1, tzinfo=UTC),
            outcome="Standalone outcome",
        )

    response = await client.get(f"/api/visuals/timeline/{experiment_id}")

    assert response.status_code == 200, response.text
    body = parse(response.content, dict[str, object])
    assert body["issue"] is None
    selected = convert(body["selected_result"], dict[str, object])
    assert convert(selected["record"], dict[str, object])["id"] == str(experiment_id)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_timeline_explains_unsupported_record_kind(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """An existing non-Issue/Experiment target gets an actionable response."""
    client, store = pglite_route_client
    belief_id = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await _insert_row(
            conn,
            record_id=belief_id,
            kind="Belief",
            seq=1,
            title="A belief",
            created=datetime(2026, 1, 1, tzinfo=UTC),
        )
    response = await client.get(f"/api/visuals/timeline/{belief_id}")
    assert response.status_code == 422
    assert "Issue and Experiment" in response.text


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_timeline_rejects_parameters_outside_the_catalog_bounds(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The data route enforces its advertised hard limits for direct callers."""
    client, _store = pglite_route_client
    response = await client.get(
        f"/api/visuals/timeline/{uuid.uuid4()}",
        params={"direction_limit": 13},
    )
    assert response.status_code == 422


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_timeline_limits_come_from_the_configured_catalog(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raised ceiling serves more than twelve directions; a lowered one rejects."""
    client, store = pglite_route_client
    root_id = uuid.uuid4()
    started = datetime(2026, 1, 1, tzinfo=UTC)
    async with store.engine.acquire() as conn:
        await _insert_row(
            conn,
            record_id=root_id,
            kind="Issue",
            seq=1,
            title="Root",
            created=started,
        )
        for index in range(14):
            direction_id = uuid.uuid4()
            await _insert_row(
                conn,
                record_id=direction_id,
                kind="Issue",
                seq=index + 2,
                title=f"Direction {index}",
                created=started + timedelta(days=index + 1),
            )
            await _insert_edge(
                conn,
                from_id=direction_id,
                from_kind="Issue",
                to_id=root_id,
                to_kind="Issue",
                edge_kind="narrows",
            )
    monkeypatch.setattr(
        app.state,
        "visual_catalog",
        Workspace.Config(
            visuals=[
                StaticVisual.Config(type="x.notes", title="Notes"),
                TimelineVisual.Config(direction_limit=20),
            ],
            default_visual="x.notes",
        ).make(),
        raising=False,
    )
    raised = await client.get(
        f"/api/visuals/timeline/{root_id}",
        params={"direction_limit": 14},
    )
    assert raised.status_code == 200, raised.text
    assert _direction_titles(raised.content) == [
        f"Direction {index}" for index in range(14)
    ]
    defaulted = await client.get(f"/api/visuals/timeline/{root_id}")
    assert _direction_titles(defaulted.content) == [
        f"Direction {index}" for index in range(8)
    ]
    too_many = await client.get(
        f"/api/visuals/timeline/{root_id}",
        params={"direction_limit": 21},
    )
    assert too_many.status_code == 422


async def _insert_row(
    conn: Conn,
    *,
    record_id: uuid.UUID,
    kind: str,
    seq: int,
    title: str,
    created: datetime,
    issue_kind: list[str] | None = None,
    outcome: str | None = None,
    description: str | None = None,
) -> None:
    await conn.execute(
        "INSERT INTO inquiries (id, kind, seq, account, title, created, modified, "
        "issue_kind, experiment_outcome, description) "
        "VALUES ($1, $2, $3, $4, $5, $6, $6, $7, $8, $9)",
        record_id,
        kind,
        seq,
        "timeline-test@example.com",
        title,
        created,
        issue_kind,
        outcome,
        description,
    )


async def _insert_edge(
    conn: Conn,
    *,
    from_id: uuid.UUID,
    from_kind: str,
    to_id: uuid.UUID,
    to_kind: str,
    edge_kind: str,
    valence: float | None = None,
    note: str | None = None,
) -> None:
    await conn.execute(
        "INSERT INTO edges (from_id, from_kind, to_id, to_kind, edge_kind, valence, note) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7)",
        from_id,
        from_kind,
        to_id,
        to_kind,
        edge_kind,
        valence,
        note,
    )


def _direction_titles(content: bytes) -> list[object]:
    """Return the Issue titles of a timeline response's directions, in order."""
    body = parse(content, dict[str, object])
    directions = convert(body["directions"], list[dict[str, object]])
    return [convert(item["issue"], dict[str, object])["title"] for item in directions]


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
