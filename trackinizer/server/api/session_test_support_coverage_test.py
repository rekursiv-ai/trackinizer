"""Unit coverage for cheap session seeding without a database."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from trackinizer.conftest import make_store
from trackinizer.server.api import session_test_support
from trackinizer.server.api.conftest import patch_state


@pytest.mark.asyncio
@pytest.mark.parametrize("polled", [False, True])
@pytest.mark.parametrize("custom", [False, True])
async def test_seed_inserts_canonical_envelope_and_optionally_starts_the_watch(
    monkeypatch: pytest.MonkeyPatch,
    polled: bool,
    custom: bool,
) -> None:
    store, engine = make_store()
    sightings: list[tuple[UUID, datetime, bool]] = []

    async def record_seen(session_id: UUID, *, at: datetime, polled: bool) -> None:
        sightings.append((session_id, at, polled))

    insert = AsyncMock()
    seen = AsyncMock(side_effect=record_seen)
    monkeypatch.setattr(session_test_support, "insert_inquiry", insert)
    monkeypatch.setattr(store, "record_session_seen", seen)
    agent = session_test_support.USER_AGENT
    before = datetime.now(UTC)
    session_id = await session_test_support.seed_session(
        store,
        agent,
        actor="runner" if custom else None,
        account="owner@example.com" if custom else None,
        cli="codex",
        cli_session_id="vendor-id",
        labels=("z", "a", "z"),
        polled=polled,
    )
    insert.assert_awaited_once_with(
        engine.conn,
        session_id,
        "AgentSession",
        values={
            "title": "codex session",
            "owner": "runner" if custom else agent.email,
            "account": "owner@example.com" if custom else agent.email,
            "labels": ("z", "a"),
            "subscribers": (),
            "agentsession_audience": "open",
            "agentsession_cli": "codex",
            "agentsession_cli_session_id": "vendor-id",
            "agentsession_rooms": (),
            "agentsession_opened_by_api_key_id": agent.key_id,
        },
    )
    if polled:
        seen.assert_awaited_once()
        assert seen.call_args.args == (session_id,)
        assert seen.call_args.kwargs["polled"] is True
        assert isinstance(seen.call_args.kwargs["at"], datetime)
        assert len(sightings) == 1
        assert before <= sightings[0][1] <= datetime.now(UTC)
    else:
        seen.assert_not_awaited()


@pytest.mark.asyncio
async def test_unpolled_seed_needs_no_runtime_hub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _engine = make_store()
    patch_state(monkeypatch, "hub", None)
    insert, seen = AsyncMock(), AsyncMock()
    monkeypatch.setattr(session_test_support, "insert_inquiry", insert)
    monkeypatch.setattr(store, "record_session_seen", seen)
    await session_test_support.seed_session(
        store,
        session_test_support.OTHER_AGENT,
        polled=False,
    )
    assert insert.await_count == 1
    seen.assert_not_awaited()


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
