"""Bounded evidence timeline endpoint tests over the real inquiry schema."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import uuid

import pytest

from trackinizer.lib.codec import from_plain, loads
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
    body = from_plain(loads(response.content), dict[str, object])
    target = from_plain(body["target"], dict[str, object])
    assert len(from_plain(target["title"], str)) == 2_000
    assert len(from_plain(target["description"], str)) == 2_000
    directions = from_plain(body["directions"], list[dict[str, object]])
    assert [
        from_plain(item["issue"], dict[str, object])["title"] for item in directions
    ] == [
        "Direction 0",
        "Direction 1",
    ]
    assert body["directions_truncated"] is True
    assert all(direction["results_truncated"] is True for direction in directions)
    first_result = from_plain(
        from_plain(directions[0]["results"], list[dict[str, object]])[0],
        dict[str, object],
    )
    outcome = from_plain(
        from_plain(first_result["record"], dict[str, object])["outcome"],
        str,
    )
    assert len(outcome) == 2_000
    assert outcome.startswith("Measured outcome ")
    evidence = from_plain(first_result["evidence"], list[dict[str, object]])
    assert len(evidence) == 6
    assert first_result["evidence_truncated"] is True
    assert {(item["edge_kind"], item["valence"]) for item in evidence} == {
        ("proves", 0.8),
        ("favors", -0.7),
    }
    assert {
        from_plain(item["claim"], dict[str, object])["title"] for item in evidence
    } == {f"Claim 0-{index}" for index in range(6)}
    note = from_plain(evidence[0]["note"], str)
    assert len(note) == 2_000
    assert note.startswith("bounded evidence note ")
    assert [
        from_plain(item, dict[str, object])["title"]
        for item in from_plain(body["unresolved_questions"], list[dict[str, object]])
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
    body = from_plain(loads(response.content), dict[str, object])
    assert from_plain(body["issue"], dict[str, object])["id"] == str(issue_id)
    results = from_plain(body["root_results"], list[dict[str, object]])
    assert len(results) == 1
    assert from_plain(results[0]["record"], dict[str, object])["id"] == str(
        other_ids[-1],
    )
    selected = from_plain(body["selected_result"], dict[str, object])
    assert from_plain(selected["record"], dict[str, object])["id"] == str(selected_id)


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
    body = from_plain(loads(response.content), dict[str, object])
    assert body["issue"] is None
    selected = from_plain(body["selected_result"], dict[str, object])
    assert from_plain(selected["record"], dict[str, object])["id"] == str(experiment_id)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_leads_are_the_narrows_ancestors_farthest_first_and_bounded(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Leads climb `narrows` at most three levels and end at the nearest parent."""
    client, store = pglite_route_client
    started = datetime(2026, 1, 1, tzinfo=UTC)
    chain = [uuid.uuid4() for _ in range(5)]
    async with store.engine.acquire() as conn:
        for index, record_id in enumerate(chain):
            await _insert_row(
                conn,
                record_id=record_id,
                kind="Issue",
                seq=index + 1,
                title=f"Level {index}",
                created=started + timedelta(days=index),
            )
            if index:
                await _insert_edge(
                    conn,
                    from_id=record_id,
                    from_kind="Issue",
                    to_id=chain[index - 1],
                    to_kind="Issue",
                    edge_kind="narrows",
                )

    deep = await client.get(f"/api/visuals/timeline/{chain[4]}")
    assert deep.status_code == 200, deep.text
    assert _titles(deep.content, field="leads") == ["Level 1", "Level 2", "Level 3"]
    top = await client.get(f"/api/visuals/timeline/{chain[0]}")
    assert _titles(top.content, field="leads") == []
    middle = await client.get(f"/api/visuals/timeline/{chain[2]}")
    assert _titles(middle.content, field="leads") == ["Level 0", "Level 1"]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_leads_stop_at_a_narrows_cycle(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A cyclic `narrows` chain lists each Issue once, never the record itself."""
    client, store = pglite_route_client
    started = datetime(2026, 1, 1, tzinfo=UTC)
    first, second = uuid.uuid4(), uuid.uuid4()
    async with store.engine.acquire() as conn:
        for index, record_id in enumerate((first, second)):
            await _insert_row(
                conn,
                record_id=record_id,
                kind="Issue",
                seq=index + 1,
                title=f"Cycle {index}",
                created=started + timedelta(days=index),
            )
        for child, parent in ((first, second), (second, first)):
            await _insert_edge(
                conn,
                from_id=child,
                from_kind="Issue",
                to_id=parent,
                to_kind="Issue",
                edge_kind="narrows",
            )

    response = await client.get(f"/api/visuals/timeline/{first}")
    assert response.status_code == 200, response.text
    assert _titles(response.content, field="leads") == ["Cycle 1"]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_experiment_centres_on_its_producer_with_the_producers_leads(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """An Experiment keeps today's centre and gains the producer's leads."""
    client, store = pglite_route_client
    started = datetime(2026, 1, 1, tzinfo=UTC)
    lead, producer, experiment = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with store.engine.acquire() as conn:
        for seq, (record_id, kind, title) in enumerate(
            (
                (lead, "Issue", "Lead"),
                (producer, "Issue", "Producer"),
                (experiment, "Experiment", "Run"),
            ),
            start=1,
        ):
            await _insert_row(
                conn,
                record_id=record_id,
                kind=kind,
                seq=seq,
                title=title,
                created=started + timedelta(days=seq),
            )
        await _insert_edge(
            conn,
            from_id=producer,
            from_kind="Issue",
            to_id=lead,
            to_kind="Issue",
            edge_kind="narrows",
        )
        await _insert_edge(
            conn,
            from_id=experiment,
            from_kind="Experiment",
            to_id=producer,
            to_kind="Issue",
            edge_kind="produced_by",
        )

    response = await client.get(f"/api/visuals/timeline/{experiment}")
    assert response.status_code == 200, response.text
    body = from_plain(loads(response.content), dict[str, object])
    assert from_plain(body["target"], dict[str, object])["title"] == "Run"
    assert from_plain(body["issue"], dict[str, object])["title"] == "Producer"
    assert _titles(response.content, field="leads") == ["Lead"]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_other_kinds_centre_on_themselves_with_the_linked_issues_context(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A Belief stays the record row; its produced_by Issue supplies the rest."""
    client, store = pglite_route_client
    started = datetime(2026, 1, 1, tzinfo=UTC)
    lead, anchor, direction, belief = (uuid.uuid4() for _ in range(4))
    async with store.engine.acquire() as conn:
        for seq, (record_id, kind, title) in enumerate(
            (
                (lead, "Issue", "Lead"),
                (anchor, "Issue", "Anchor"),
                (direction, "Issue", "Direction"),
                (belief, "Belief", "Claim"),
            ),
            start=1,
        ):
            await _insert_row(
                conn,
                record_id=record_id,
                kind=kind,
                seq=seq,
                title=title,
                created=started + timedelta(days=seq),
            )
        await _insert_edge(
            conn,
            from_id=anchor,
            from_kind="Issue",
            to_id=lead,
            to_kind="Issue",
            edge_kind="narrows",
        )
        await _insert_edge(
            conn,
            from_id=direction,
            from_kind="Issue",
            to_id=anchor,
            to_kind="Issue",
            edge_kind="narrows",
        )
        await _insert_edge(
            conn,
            from_id=belief,
            from_kind="Belief",
            to_id=anchor,
            to_kind="Issue",
            edge_kind="produced_by",
        )

    response = await client.get(f"/api/visuals/timeline/{belief}")
    assert response.status_code == 200, response.text
    body = from_plain(loads(response.content), dict[str, object])
    assert from_plain(body["target"], dict[str, object])["title"] == "Claim"
    assert from_plain(body["issue"], dict[str, object])["title"] == "Anchor"
    assert body["selected_result"] is None
    assert body["root_results"] == []
    assert _titles(response.content, field="leads") == ["Lead", "Anchor"]
    assert _direction_titles(response.content) == ["Direction"]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_unlinked_record_of_any_kind_is_shown_alone(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A Paper with no Issue link has no leads and no directions."""
    client, store = pglite_route_client
    paper_id = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await _insert_row(
            conn,
            record_id=paper_id,
            kind="Paper",
            seq=1,
            title="A paper",
            created=datetime(2026, 1, 1, tzinfo=UTC),
        )
    response = await client.get(f"/api/visuals/timeline/{paper_id}")
    assert response.status_code == 200, response.text
    body = from_plain(loads(response.content), dict[str, object])
    assert from_plain(body["target"], dict[str, object])["kind"] == "Paper"
    assert body["issue"] is None
    assert body["leads"] == []
    assert body["directions"] == []
    assert body["root_results"] == []


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


def _titles(content: bytes, *, field: str) -> list[object]:
    """Return the titles of a timeline response's list of records, in order."""
    return [
        item["title"]
        for item in from_plain(
            from_plain(loads(content), dict[str, object])[field],
            list[dict[str, object]],
        )
    ]


def _direction_titles(content: bytes) -> list[object]:
    """Return the Issue titles of a timeline response's directions, in order."""
    body = from_plain(loads(content), dict[str, object])
    directions = from_plain(body["directions"], list[dict[str, object]])
    return [
        from_plain(item["issue"], dict[str, object])["title"] for item in directions
    ]


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
