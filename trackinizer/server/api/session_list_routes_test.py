"""``GET /api/sessions``: the caller's own sessions, over the whole app."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict, cast

from treekle import json_loads

import pytest

from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import patch_state
from trackinizer.server.api.session_test_support import (
    ASSISTANT_CONFIG,
    OTHER_AGENT,
    USER_AGENT,
    attached_process,
    browser,
    seed_accounts,
    seed_session,
)
from trackinizer.server.session_relay import SessionRelay


if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    import httpx2

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.store.core import Store


@pytest.fixture(autouse=True)
def served(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve with a fresh session relay."""
    patch_state(monkeypatch, "config", ASSISTANT_CONFIG)
    patch_state(monkeypatch, "session_relay", SessionRelay())


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
    """Live while the relay holds an agent process for it; then where it last ran."""
    client, store = pglite_route_client
    await seed_accounts(store)
    mine = await seed_session(store, USER_AGENT, actor="Agent", polled=False)
    theirs = await seed_session(store, OTHER_AGENT, actor="Other", polled=False)
    relay = cast(SessionRelay, app.state.session_relay)
    browser()

    async with (
        attached_process(relay, mine, host="workstation"),
        attached_process(relay, theirs, host="theirs"),
    ):
        served = await client.get("/api/sessions")
    left = await client.get("/api/sessions")

    listings = [json_loads(each.content, _Listing) for each in (served, left)]
    assert (served.status_code, left.status_code) == (200, 200)
    assert [
        [(each["id"], each["live"], each["host"]) for each in listed["sessions"]]
        for listed in listings
    ] == [[(str(mine), True, "workstation")], [(str(mine), False, "workstation")]]
    assert (listings[0]["issues"], listings[0]["more"]) == ([], False)


class _Session(TypedDict):
    id: str
    live: bool
    host: str | None


class _Listing(TypedDict):
    sessions: list[_Session]
    issues: list[object]
    more: bool


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
