"""``GET /api/sessions``: the caller's own sessions, over the whole app."""

from __future__ import annotations

from typing import TYPE_CHECKING

from treekle import json_loads

import pytest

from trackinizer.server.api.app import app
from trackinizer.server.api.canvas_test_support import (
    ASSISTANT_CONFIG,
    OTHER_AGENT,
    USER_AGENT,
    browser,
    seed_accounts,
    seed_session,
)
from trackinizer.server.chat_hub import ChatHub
from trackinizer.server.inbound import InboundQueue


if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    import httpx2

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.store.core import Store


@pytest.fixture(autouse=True)
def served(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve with a fresh poller table and event hub."""
    monkeypatch.setattr(app.state, "config", ASSISTANT_CONFIG, raising=False)
    monkeypatch.setattr(app.state, "inbound", InboundQueue(), raising=False)
    monkeypatch.setattr(app.state, "hub", ChatHub(), raising=False)


@pytest.mark.parametrize(
    "query",
    ["days=0", "days=366", "limit=0", "limit=501"],
)
def test_a_window_or_limit_out_of_bounds_is_refused(
    route_client: tuple[TestClient, Store, FakeEngine],
    query: str,
) -> None:
    client, _, _ = route_client

    assert client.get(f"/api/sessions?{query}").status_code == 422


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_caller_reads_their_own_live_session_and_no_one_elses(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    mine = await seed_session(store, USER_AGENT, actor="Agent", cli="claude")
    _ = await seed_session(store, OTHER_AGENT, actor="Other", cli="codex")
    inbound = app.state.inbound
    assert isinstance(inbound, InboundQueue)
    inbound.mark_poller(mine, machine="workstation", cli="claude")
    browser()

    answer = await client.get("/api/sessions")

    assert answer.status_code == 200, answer.text
    listed = json_loads(answer.content, dict[str, object])
    sessions = listed["sessions"]
    assert isinstance(sessions, list)
    assert [(each["id"], each["live"], each["machine"]) for each in sessions] == [
        (str(mine), True, "workstation"),
    ]
    assert (listed["issues"], listed["more"]) == ([], False)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
