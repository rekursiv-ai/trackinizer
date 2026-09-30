"""Concurrent report publication needs fresh revision and replay reads."""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio
import uuid

import pytest

from trackinizer.server.visuals.reports import (
    ArtifactCitationRef,
    ArtifactContentRevision,
    PublishArtifactContent,
    publish_artifact_content,
)
from trackinizer.wire.bodies import SubmitIssue


if TYPE_CHECKING:
    from trackinizer.server.store.core import Store


async def _issue(store: Store, user_id: uuid.UUID, email: str) -> uuid.UUID:
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, $2, 'Publisher', 'writer', 'active')",
            user_id,
            email,
        )
    return await store.submit_issue(
        SubmitIssue(title="Concurrent report owner", account=email),
        actor=email,
    )


def _body(
    issue_id: uuid.UUID,
    previous_artifact_id: uuid.UUID | None = None,
) -> PublishArtifactContent:
    return PublishArtifactContent(
        previous_artifact_id=previous_artifact_id,
        issue_id=issue_id,
        title="Direction atlas",
        summary="A cited report.",
        format="html",
        html="<h1>Atlas</h1>",
        citations=[ArtifactCitationRef(record_id=issue_id)],
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_concurrent_same_key_replays_one_report(integ_store: Store) -> None:
    """A retry arriving during publication returns the original revision."""
    store = integ_store
    user_id = uuid.uuid4()
    email = "publisher@example.com"
    body = _body(await _issue(store, user_id, email))
    key = uuid.uuid4()
    start = asyncio.Event()

    async def publish() -> ArtifactContentRevision:
        await start.wait()
        return await publish_artifact_content(
            store,
            user_id=user_id,
            author=email,
            api_key_id=None,
            body=body,
            key=key,
        )

    tasks = [asyncio.create_task(publish()) for _ in range(6)]
    start.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(result, ArtifactContentRevision) for result in results), (
        results
    )
    assert (
        len(
            {
                result.artifact_id
                for result in results
                if isinstance(result, ArtifactContentRevision)
            },
        )
        == 1
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_concurrent_append_allocates_distinct_revisions(
    integ_store: Store,
) -> None:
    """Writers appending one report each receive a distinct revision."""
    store = integ_store
    user_id = uuid.uuid4()
    email = "publisher@example.com"
    issue_id = await _issue(store, user_id, email)
    first = await publish_artifact_content(
        store,
        user_id=user_id,
        author=email,
        api_key_id=None,
        body=_body(issue_id),
        key=uuid.uuid4(),
    )
    start = asyncio.Event()

    async def append() -> ArtifactContentRevision:
        await start.wait()
        return await publish_artifact_content(
            store,
            user_id=user_id,
            author=email,
            api_key_id=None,
            body=_body(issue_id, first.artifact_id),
            key=uuid.uuid4(),
        )

    tasks = [asyncio.create_task(append()) for _ in range(6)]
    start.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(result, ArtifactContentRevision) for result in results), (
        results
    )
    assert sorted(
        result.revision
        for result in results
        if isinstance(result, ArtifactContentRevision)
    ) == list(range(2, 8))


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
