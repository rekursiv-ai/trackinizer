"""Immutable reports stay readable and cited across users and revisions."""

from __future__ import annotations

from typing import TYPE_CHECKING

import json
import uuid

import pytest

from trackinizer.lib.codec import from_plain, loads
from trackinizer.server.api.conftest import (
    TEST_USER_EMAIL,
    TEST_USER_ID,
    install_identity,
    make_test_identity,
)
from trackinizer.server.visuals.reports import read_artifact_content_on_conn
from trackinizer.wire.bodies import (
    SubmitArtifact,
    SubmitBelief,
    SubmitIssue,
    SubmitPaper,
)


if TYPE_CHECKING:
    import httpx2

    from trackinizer.server.store.core import Store


@pytest.mark.parametrize("other_publisher", [False, True])
@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_report_key_does_not_reuse_an_unrelated_artifact(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    other_publisher: bool,
) -> None:
    """A report retry key belongs to the report, not an inquiry creation."""
    client, store = pglite_route_client
    teammate_id = uuid.uuid4()
    teammate_email = "teammate@example.com"
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) VALUES "
            "($1, $2, 'Publisher', 'writer', 'active'), "
            "($3, $4, 'Teammate', 'writer', 'active')",
            TEST_USER_ID,
            TEST_USER_EMAIL,
            teammate_id,
            teammate_email,
        )
    key = uuid.uuid4()
    unrelated_id = await store.submit_artifact(
        SubmitArtifact(
            title="Unrelated artifact",
            account=TEST_USER_EMAIL,
            idempotency_key=key,
        ),
        actor=TEST_USER_EMAIL,
    )
    author = teammate_email if other_publisher else TEST_USER_EMAIL
    issue_id = await store.submit_issue(
        SubmitIssue(title="Report owner", account=author),
        actor=author,
    )
    if other_publisher:
        install_identity(
            make_test_identity(
                user_id=teammate_id,
                email=teammate_email,
                role="writer",
                api_key_id=None,
            ),
        )
    payload = {
        "issue_id": str(issue_id),
        "title": "Direction atlas",
        "summary": "A cited report.",
        "format": "html",
        "html": "<h1>Atlas</h1>",
        "citations": [{"record_id": str(issue_id)}],
    }
    published = await client.post(
        "/api/artifacts/content",
        json=payload,
        headers={"Idempotency-Key": str(key)},
    )
    assert published.status_code == 201, published.text
    artifact_id = uuid.UUID(
        from_plain(
            from_plain(loads(published.content), dict[str, object])["artifact_id"],
            str,
        ),
    )
    assert artifact_id != unrelated_id
    async with store.engine.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT account FROM inquiries WHERE id = $1",
                artifact_id,
            )
            == author
        )
        context = await read_artifact_content_on_conn(
            conn,
            artifact_id,
            include_html=False,
        )
        assert context is not None
        assert context.html is None
        assert context.title == "Direction atlas"
    replay = await client.post(
        "/api/artifacts/content",
        json=payload,
        headers={"Idempotency-Key": str(key)},
    )
    assert replay.status_code == 201
    assert json.loads(replay.content) == json.loads(published.content)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_artifact_user_storage_quota_is_atomic_and_replayable(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A user at 500 MB cannot publish another file; an old retry still works."""
    client, store = pglite_route_client
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, $2, 'Publisher', 'writer', 'active')",
            TEST_USER_ID,
            TEST_USER_EMAIL,
        )
    issue_id = await store.submit_issue(
        SubmitIssue(title="Artifact owner", account=TEST_USER_EMAIL),
        actor=TEST_USER_EMAIL,
    )
    payload = {
        "issue_id": str(issue_id),
        "title": "Atlas",
        "summary": "One direction.",
        "format": "html",
        "html": "<h1>Atlas</h1>",
    }
    key = str(uuid.uuid4())
    first = await client.post(
        "/api/artifacts/content",
        json=payload,
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 201, first.text
    async with store.engine.acquire() as conn:
        stored = await conn.fetchval(
            "SELECT content_bytes FROM visual_report_revisions WHERE artifact_id = $1",
            uuid.UUID(
                from_plain(
                    from_plain(loads(first.content), dict[str, object])["artifact_id"],
                    str,
                ),
            ),
        )
        assert from_plain(stored, int) > 0
        await conn.execute(
            "UPDATE visual_report_revisions SET content_bytes = 499999999 "
            "WHERE author_id = $1",
            TEST_USER_ID,
        )
    denied = await client.post(
        "/api/artifacts/content",
        json={**payload, "title": "Next atlas"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert denied.status_code == 422
    assert "500 MB" in denied.text
    async with store.engine.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM visual_report_revisions WHERE author_id = $1",
                TEST_USER_ID,
            )
            == 1
        )
    replay = await client.post(
        "/api/artifacts/content",
        json=payload,
        headers={"Idempotency-Key": key},
    )
    assert replay.status_code == 201
    assert json.loads(replay.content) == json.loads(first.content)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_report_publish_is_atomic_immutable_and_team_readable(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """An agent publish links its Artifact and leaves prior revisions unchanged."""
    client, store = pglite_route_client
    teammate_id = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) VALUES "
            "($1, $2, 'Publisher', 'writer', 'active'), "
            "($3, 'teammate@example.com', 'Teammate', 'viewer', 'active')",
            TEST_USER_ID,
            TEST_USER_EMAIL,
            teammate_id,
        )
    issue_id = await store.submit_issue(
        SubmitIssue(title="ARC3 direction", account=TEST_USER_EMAIL),
        actor=TEST_USER_EMAIL,
    )
    first_payload = {
        "issue_id": str(issue_id),
        "title": "ARC3 direction atlas",
        "summary": "Earlier directions and measured outcomes.",
        "format": "html",
        "html": "<h1>Atlas revision one</h1>",
        "citations": [{"record_id": str(issue_id)}],
    }
    invalid = await client.post(
        "/api/artifacts/content",
        json={**first_payload, "citations": [{"record_id": str(uuid.uuid4())}]},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert invalid.status_code == 422
    async with store.engine.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM visual_reports") == 0
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM inquiries WHERE kind = 'Artifact'",
            )
            == 0
        )
    key = str(uuid.uuid4())
    first = await client.post(
        "/api/artifacts/content",
        json=first_payload,
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 201, first.text
    published = from_plain(loads(first.content), dict[str, object])
    artifact_id = from_plain(published["artifact_id"], str)
    assert published["revision"] == 1
    assert published["html"] == first_payload["html"]

    replay = await client.post(
        "/api/artifacts/content",
        json=first_payload,
        headers={"Idempotency-Key": key},
    )
    assert replay.status_code == 201
    assert json.loads(replay.content) == json.loads(first.content)
    conflict = await client.post(
        "/api/artifacts/content",
        json={**first_payload, "title": "Changed"},
        headers={"Idempotency-Key": key},
    )
    assert conflict.status_code == 409

    async with store.engine.acquire() as conn:
        edge = await conn.fetchrow(
            "SELECT artifact.kind, edge.edge_kind FROM inquiries AS artifact "
            "JOIN edges AS edge ON edge.from_id = artifact.id "
            "WHERE artifact.id = $1 AND edge.to_id = $2",
            uuid.UUID(artifact_id),
            issue_id,
        )
    assert edge is not None
    assert dict(edge) == {"kind": "Artifact", "edge_kind": "produced_by"}

    second = await client.post(
        "/api/artifacts/content",
        json={
            **first_payload,
            "previous_artifact_id": artifact_id,
            "html": "<h1>Atlas revision two</h1>",
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert second.status_code == 201, second.text
    second_revision = from_plain(loads(second.content), dict[str, object])
    assert second_revision["revision"] == 2
    assert second_revision["artifact_id"] != artifact_id
    latest = await client.get(
        f"/api/artifacts/{second_revision['artifact_id']}/content",
    )
    assert latest.status_code == 200
    assert from_plain(loads(latest.content), dict[str, object])["html"] == (
        "<h1>Atlas revision two</h1>"
    )

    install_identity(
        make_test_identity(
            user_id=teammate_id,
            email="teammate@example.com",
            role="viewer",
            api_key_id=None,
        ),
    )
    earlier = await client.get(f"/api/artifacts/{artifact_id}/content")
    assert earlier.status_code == 200
    revision = from_plain(loads(earlier.content), dict[str, object])
    assert revision["html"] == "<h1>Atlas revision one</h1>"
    assert from_plain(revision["revision"], int) == 1
    assert from_plain(revision["author"], str) == TEST_USER_EMAIL
    async with store.engine.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE id = $1", TEST_USER_ID)
    preserved = await client.get(f"/api/artifacts/{artifact_id}/content")
    assert preserved.status_code == 200
    assert (
        from_plain(loads(preserved.content), dict[str, object])["author"]
        == TEST_USER_EMAIL
    )
    await store.purge(issue_id, actor=TEST_USER_EMAIL)
    await store.purge(uuid.UUID(artifact_id), actor=TEST_USER_EMAIL)
    historical = await client.get(f"/api/artifacts/{artifact_id}/content")
    assert historical.status_code == 200
    assert from_plain(loads(historical.content), dict[str, object])["issue_id"] == str(
        issue_id,
    )
    denied = await client.post(
        "/api/artifacts/content",
        json=first_payload,
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert denied.status_code == 403


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_next_revision_supersedes_the_one_it_updates(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """An old revision's link leads to its successor; a replay adds no edge."""
    client, store = pglite_route_client
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, $2, 'Publisher', 'writer', 'active')",
            TEST_USER_ID,
            TEST_USER_EMAIL,
        )
    issue_id = await store.submit_issue(
        SubmitIssue(title="Site owner", account=TEST_USER_EMAIL),
        actor=TEST_USER_EMAIL,
    )
    payload = {
        "issue_id": str(issue_id),
        "title": "Site",
        "summary": "A published site.",
        "format": "html",
        "html": "<h1>One</h1>",
    }
    first = await client.post(
        "/api/artifacts/content",
        json=payload,
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert first.status_code == 201, first.text
    first_id = uuid.UUID(
        from_plain(
            from_plain(loads(first.content), dict[str, object])["artifact_id"],
            str,
        ),
    )
    second_payload = {
        **payload,
        "html": "<h1>Two</h1>",
        "previous_artifact_id": str(first_id),
    }
    key = str(uuid.uuid4())
    second = await client.post(
        "/api/artifacts/content",
        json=second_payload,
        headers={"Idempotency-Key": key},
    )
    assert second.status_code == 201, second.text
    second_id = uuid.UUID(
        from_plain(
            from_plain(loads(second.content), dict[str, object])["artifact_id"],
            str,
        ),
    )
    replay = await client.post(
        "/api/artifacts/content",
        json=second_payload,
        headers={"Idempotency-Key": key},
    )
    assert replay.status_code == 201
    async with store.engine.acquire() as conn:
        edges = await conn.fetch(
            "SELECT from_id, to_id FROM edges WHERE edge_kind = 'supersedes'",
        )
    assert [(row["from_id"], row["to_id"]) for row in edges] == [
        (second_id, first_id),
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_html_route_serves_the_revision_as_a_sandboxed_page(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A signed-in teammate opens the exact HTML as a page, never same-origin."""
    client, store = pglite_route_client
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, $2, 'Publisher', 'writer', 'active')",
            TEST_USER_ID,
            TEST_USER_EMAIL,
        )
    issue_id = await store.submit_issue(
        SubmitIssue(title="Site owner", account=TEST_USER_EMAIL),
        actor=TEST_USER_EMAIL,
    )
    page = "<!doctype html><title>Site</title><h1>Hello</h1>"
    published = await client.post(
        "/api/artifacts/content",
        json={
            "issue_id": str(issue_id),
            "title": "Site",
            "summary": "A published site.",
            "format": "html",
            "html": page,
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert published.status_code == 201, published.text
    artifact_id = from_plain(
        from_plain(loads(published.content), dict[str, object])["artifact_id"],
        str,
    )

    response = await client.get(f"/api/artifacts/{artifact_id}/html")
    assert response.status_code == 200
    assert response.text == page
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    policy = [
        part.strip() for part in response.headers["content-security-policy"].split(";")
    ]
    assert policy[0] == "sandbox allow-scripts allow-popups"
    assert "frame-ancestors 'self'" in policy
    assert "connect-src 'none'" in policy
    assert any(
        part.startswith("script-src") and "https://cdnjs.cloudflare.com" in part
        for part in policy
    )
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "private"

    missing = await client.get(f"/api/artifacts/{uuid.uuid4()}/html")
    assert missing.status_code == 404

    teammate = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, 'viewer@example.com', 'Viewer', 'viewer', 'active')",
            teammate,
        )
    install_identity(
        make_test_identity(
            user_id=teammate,
            email="viewer@example.com",
            role="viewer",
            api_key_id=None,
        ),
    )
    shared = await client.get(f"/api/artifacts/{artifact_id}/html")
    assert shared.status_code == 200
    assert shared.text == page


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_structured_report_freezes_signed_evidence(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Later graph edits cannot rewrite a published conclusion's evidence."""
    client, store = pglite_route_client
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, $2, 'Publisher', 'writer', 'active')",
            TEST_USER_ID,
            TEST_USER_EMAIL,
        )
    issue_id = await store.submit_issue(
        SubmitIssue(title="ARC3 evidence", account=TEST_USER_EMAIL),
        actor=TEST_USER_EMAIL,
    )
    belief_id = await store.submit_belief(
        SubmitBelief(title="Scaling helps", account=TEST_USER_EMAIL),
        actor=TEST_USER_EMAIL,
    )
    paper_id = await store.submit_paper(
        SubmitPaper(title="Measured result", account=TEST_USER_EMAIL),
        actor=TEST_USER_EMAIL,
    )
    await store.add_edge(
        from_id=paper_id,
        to_id=belief_id,
        edge_kind="favors",
        valence=0.75,
        actor=TEST_USER_EMAIL,
    )
    payload = {
        "issue_id": str(issue_id),
        "title": "Scaling evidence",
        "summary": "One measured direction.",
        "format": "structured",
        "sections": [
            {
                "title": "Scaling",
                "summary": "The held-out split improved.",
                "details": "Matched comparison.",
                "findings": [
                    {
                        "claim": "The approach improves the score",
                        "outcome": {
                            "result": "12 wins",
                            "denominator": 16,
                            "split": "held-out",
                        },
                        "uncertainty": "Small sample.",
                        "citations": [
                            {
                                "record_id": str(paper_id),
                                "claim_id": str(belief_id),
                                "edge_kind": "favors",
                            },
                        ],
                    },
                ],
            },
        ],
    }
    published = await client.post(
        "/api/artifacts/content",
        json=payload,
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert published.status_code == 201, published.text
    revision = from_plain(loads(published.content), dict[str, object])
    artifact_id = from_plain(revision["artifact_id"], str)
    sections = from_plain(revision["sections"], list[object])
    finding = from_plain(
        from_plain(
            from_plain(sections[0], dict[str, object])["findings"],
            list[object],
        )[0],
        dict[str, object],
    )
    citation = from_plain(
        from_plain(finding["citations"], list[object])[0],
        dict[str, object],
    )
    assert citation["title"] == "Measured result"
    assert citation["claim_title"] == "Scaling helps"
    assert citation["valence"] == 0.75
    assert finding["outcome"] == {
        "result": "12 wins",
        "denominator": 16,
        "split": "held-out",
    }

    await store.add_edge(
        from_id=paper_id,
        to_id=belief_id,
        edge_kind="favors",
        valence=-0.5,
        actor=TEST_USER_EMAIL,
    )
    earlier = await client.get(f"/api/artifacts/{artifact_id}/content")
    assert earlier.status_code == 200
    assert from_plain(loads(earlier.content), dict[str, object])["sections"] == sections
    assert (await client.get(f"/api/artifacts/{artifact_id}/html")).status_code == 404

    await store.add_edge(
        from_id=paper_id,
        to_id=belief_id,
        edge_kind="favors",
        valence=-0.5,
        note="x" * 4_001,
        actor=TEST_USER_EMAIL,
    )
    oversized_note = await client.post(
        "/api/artifacts/content",
        json={**payload, "previous_artifact_id": artifact_id},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert oversized_note.status_code == 422
    async with store.engine.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM visual_report_revisions",
            )
            == 1
        )


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
