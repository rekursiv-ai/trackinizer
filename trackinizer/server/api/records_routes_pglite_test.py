"""Record reads' routes over PGlite, called with a real agent process's key.

Ada's agent holds the key a start minted for its process, named for her session. It
reads her rows through every route ``RECORDS_ROUTES`` lists. Bob filed a row from a
private chat of his, which Ada does not read: no listing, change or search shows it to
her agent, and every read of it by id answers 404. The key writes nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

from fastapi import FastAPI
from treekle import from_plain, json_dumps

import httpx2
import pytest
import pytest_asyncio

from trackinizer.server import web
from trackinizer.server.api.conftest import clear_identity_override, patch_state
from trackinizer.server.auth import (
    PROCESS_KEY_PREFIX,
    create_api_key,
    process_key_name,
)
from trackinizer.server.config import Config
from trackinizer.wire.wire_process_keys import RECORDS_ROUTES


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trackinizer.server.store.core import Store


pytestmark = [
    pytest.mark.db_pglite,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.usefixtures("cheap_scrypt"),
]

_MARKER: Final = "recordsmark4b1e"


@dataclass(frozen=True, slots=True, kw_only=True)
class _Rows:
    """One account's session, the Issue and Belief it filed, and the Issue's seq."""

    session: UUID
    issue: UUID
    belief: UUID
    seq: int


@dataclass(frozen=True, slots=True, kw_only=True)
class _World:
    """The app and its web surface, Ada's and Bob's rows, and Ada's process key."""

    api: httpx2.AsyncClient
    web: httpx2.AsyncClient
    ada: _Rows
    bob: _Rows
    agent: Mapping[str, str]

    async def get(
        self,
        path: str,
        params: Mapping[str, str | list[str]] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> httpx2.Response:
        """Send a GET to whichever app serves ``path``, as Ada's agent by default."""
        client = self.web if path.startswith("/api/web/") else self.api
        return await client.get(path, params=params, headers=headers or self.agent)

    def reads(self, rows: _Rows) -> dict[str, tuple[str, dict[str, str | list[str]]]]:
        """Return a call of each listed route, by template, that reads ``rows``."""
        return {
            "/api/inquiries": ("/api/inquiries", {"kind": ["Issue", "Belief"]}),
            "/api/inquiries/{kind}/{seq}": (f"/api/inquiries/Issue/{rows.seq}", {}),
            "/api/inquiries/next_issue": ("/api/inquiries/next_issue", {}),
            "/api/inquiries/{target_id}/cost": (
                f"/api/inquiries/{rows.issue}/cost",
                {},
            ),
            "/api/inquiries/{target_id}/confidence": (
                f"/api/inquiries/{rows.belief}/confidence",
                {},
            ),
            "/api/inquiries/{target_id}/authority": (
                f"/api/inquiries/{rows.issue}/authority",
                {},
            ),
            "/api/web/lookup/{target_id}": (f"/api/web/lookup/{rows.issue}", {}),
            "/api/web/get/{target_id}": (f"/api/web/get/{rows.issue}", {}),
            "/api/web/recent_changes": ("/api/web/recent_changes", {"limit": "1000"}),
            "/api/web/search_sessions": (
                "/api/web/search_sessions",
                {
                    "q": _MARKER,
                    "mode": "keyword",
                    "whose": "everyone",
                    "hidden": "true",
                },
            ),
            "/api/web/search_inquiries": (
                "/api/web/search_inquiries",
                {"q": _MARKER, "mode": "keyword"},
            ),
            "/api/version": ("/api/version", {}),
            "/api/sessions/{session_id}/parts": (
                f"/api/sessions/{rows.session}/parts",
                {},
            ),
            "/api/sessions/{session_id}/records": (
                f"/api/sessions/{rows.session}/records",
                {},
            ),
        }


@pytest_asyncio.fixture(loop_scope="session")
async def world(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[_World]:
    """Seed Bob's private row first, so a read that left it in would find it first."""
    api, store = pglite_route_client
    clear_identity_override()
    config = Config()
    patch_state(monkeypatch, "config", config)
    bob = await _filed(store, api, "bob@x.y", audience="private")
    ada = await _filed(store, api, "ada@x.y", audience="open")
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET filed_from_chat = $1 WHERE id = ANY($2::uuid[])",
            bob.session,
            [bob.issue, bob.belief],
        )
    surface = FastAPI()
    surface.state.store = store
    surface.state.engine = store.engine
    surface.state.config = config
    surface.state.vector_search = None
    web.attach(surface)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=surface),
        base_url="http://testserver",
    ) as web_client:
        yield _World(
            api=api,
            web=web_client,
            ada=ada,
            bob=bob,
            agent=await _process_key(store, "ada@x.y", session_id=ada.session),
        )


async def test_its_key_reads_its_own_rows_through_every_listed_route(
    world: _World,
) -> None:
    reads = world.reads(world.ada)
    answered = {
        template: (await world.get(path, params)).status_code
        for template, (path, params) in reads.items()
    }

    assert set(reads) == {template for _, template in RECORDS_ROUTES}
    assert answered == dict.fromkeys(reads, 200)


async def test_its_key_finds_its_own_rows_in_every_listing(world: _World) -> None:
    shown = {
        template: (await world.get(path, params)).text
        for template, (path, params) in world.reads(world.ada).items()
    }

    assert str(world.ada.issue) in shown["/api/inquiries"]
    assert str(world.ada.belief) in shown["/api/inquiries"]
    assert str(world.ada.issue) in shown["/api/inquiries/next_issue"]
    assert str(world.ada.issue) in shown["/api/web/recent_changes"]
    assert str(world.ada.session) in shown["/api/web/search_sessions"]
    assert str(world.ada.belief) in shown["/api/web/search_inquiries"]


async def test_its_key_reads_nothing_of_a_row_its_person_may_not(
    world: _World,
) -> None:
    """Bob's row is left out of every listing and is not found by id."""
    reads = world.reads(world.bob)
    shown = {
        template: await world.get(path, params)
        for template, (path, params) in reads.items()
    }
    listings = (
        "/api/inquiries",
        "/api/inquiries/next_issue",
        "/api/web/recent_changes",
        "/api/web/search_sessions",
        "/api/web/search_inquiries",
    )
    by_id = (
        "/api/inquiries/{kind}/{seq}",
        "/api/inquiries/{target_id}/cost",
        "/api/inquiries/{target_id}/confidence",
        "/api/inquiries/{target_id}/authority",
        "/api/web/lookup/{target_id}",
        "/api/web/get/{target_id}",
    )
    hidden = {str(world.bob.issue), str(world.bob.belief), str(world.bob.session)}

    assert {
        template: leaked
        for template in listings
        if (leaked := [row for row in hidden if row in shown[template].text])
    } == {}
    assert {
        template: shown[template].status_code for template in by_id
    } == dict.fromkeys(
        by_id,
        404,
    )
    assert {
        template: shown[template].status_code
        for template in (
            "/api/sessions/{session_id}/parts",
            "/api/sessions/{session_id}/records",
        )
    } == {
        "/api/sessions/{session_id}/parts": 403,
        "/api/sessions/{session_id}/records": 403,
    }


async def test_its_key_writes_nothing(world: _World) -> None:
    filed = await world.api.post(
        "/api/inquiries/batch",
        json=_batch("Issue"),
        headers=world.agent,
    )

    assert filed.status_code == 403, filed.text


async def test_a_process_key_that_names_no_session_reads_nothing(
    world: _World,
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A key minted before keys named their session acts for none."""
    _, store = pglite_route_client
    unnamed = await _process_key(store, "ada@x.y", session_id=None)

    listed = await world.get(
        "/api/inquiries",
        {"kind": ["Issue"]},
        headers=unnamed,
    )

    assert listed.status_code == 403, listed.text


async def _filed(
    store: Store,
    api: httpx2.AsyncClient,
    email: str,
    *,
    audience: str,
) -> _Rows:
    """Seed ``email`` with a session whose transcript says the marker, and file rows."""
    user_id = uuid4()
    session = uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, $2, 'n', 'writer', 'active')",
            user_id,
            email,
        )
        _, secret, _, _ = await create_api_key(
            conn,
            user_id=user_id,
            name="k",
            ceiling="writer",
        )
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title, "
            "agentsession_audience, agentsession_cli) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "$2, 's', $3, 'sagent')",
            session,
            email,
            audience,
        )
        await conn.execute(
            "INSERT INTO session_records "
            "(session_id, part, idx, kind, created, payload, text) "
            "VALUES ($1, 0, 0, 'UserMessage', now(), $2::json, $3)",
            session,
            json_dumps({"content": _MARKER}),
            _MARKER,
        )
        await conn.execute(
            "INSERT INTO session_manifests "
            "(session_id, part, name, metadata, ir_id, format, records) "
            "VALUES ($1, 0, 'main.jsonl', '{}'::json, $2, 'claude', 1)",
            session,
            uuid4(),
        )
    ids: list[UUID] = []
    for kind in ("Issue", "Belief"):
        filed = await api.post(
            "/api/inquiries/batch",
            json=_batch(kind),
            headers={"Authorization": f"Bearer {secret}"},
        )
        assert filed.status_code == 200, filed.text
        [row] = from_plain(filed.json(), dict[str, list[UUID]])["ids"]
        ids.append(row)
    issue, belief = ids
    async with store.engine.acquire() as conn:
        seq = from_plain(
            await conn.fetchval("SELECT seq FROM inquiries WHERE id = $1", issue),
            int,
        )
    return _Rows(session=session, issue=issue, belief=belief, seq=seq)


async def _process_key(
    store: Store,
    email: str,
    *,
    session_id: UUID | None,
) -> dict[str, str]:
    """Mint the key a start mints for a process of ``email``'s on a host of theirs."""
    process_id = uuid4()
    async with store.engine.acquire() as conn:
        user_id = from_plain(
            await conn.fetchval("SELECT id FROM users WHERE email = $1", email),
            UUID,
        )
        host_id = from_plain(
            await conn.fetchval(
                "INSERT INTO hosts (name, updated_by, owner, facts) "
                "VALUES ($1, $2, $2, $3) RETURNING id",
                f"host-{process_id.hex[:8]}",
                email,
                {"sessions": True},
            ),
            UUID,
        )
        _, secret, _, _ = await create_api_key(
            conn,
            user_id=user_id,
            name=f"{PROCESS_KEY_PREFIX}{process_id}"
            if session_id is None
            else process_key_name(process_id, session_id=session_id),
            ceiling="writer",
            host_id=host_id,
        )
    return {"Authorization": f"Bearer {secret}"}


def _batch(kind: str) -> dict[str, object]:
    """Return a batch that files one ``kind`` row."""
    return {
        "items": [
            {
                "kind": kind,
                "title": f"{_MARKER} {kind}",
                "idempotency_key": str(uuid4()),
            },
        ],
    }


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
