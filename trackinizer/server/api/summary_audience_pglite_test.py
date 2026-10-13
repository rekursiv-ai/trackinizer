"""A campaign's summary is open to every signed-in reader; its transcript is not.

The summarizer files a staff session's summary as an Artifact, the way the
summary service sends it: labels, ``produced_by`` edges to the brief and the session,
and the document as the description. A reader who does not read the
staff audience reads that row and can name an item in it, but every route that resolves
an item to the transcript answers 403 ``session_audience``. The page does none of this
checking: the routes do, so a page that links every item still opens none for them.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final

import json
import uuid

from treekle import from_plain, immutable

import pytest
import pytest_asyncio

from trackinizer.lib.agent.types.sessions import UserMessage
from trackinizer.server.api.conftest import install_identity, make_test_identity
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.audience import OPEN, STAFF


if TYPE_CHECKING:
    import httpx2

    from trackinizer.server.auth import AuthIdentity
    from trackinizer.server.store.core import Store


pytestmark = [pytest.mark.db_pglite, pytest.mark.asyncio(loop_scope="session")]

_CANARY: Final = "CANARY-9c1e"

type _Client = tuple[httpx2.AsyncClient, Store]


@dataclass(frozen=True, slots=True, kw_only=True)
class _Principal:
    email: str
    role: str
    read: frozenset[str]
    user_id: uuid.UUID
    key_id: uuid.UUID

    def identity(self) -> AuthIdentity:
        base = make_test_identity(
            user_id=self.user_id,
            api_key_id=self.key_id,
            email=self.email,
            role="viewer" if self.role == "viewer" else "writer",
        )
        return replace(
            base,
            read_audiences=self.read,
            open_audiences=frozenset({OPEN, STAFF})
            if STAFF in self.read
            else self.read,
        )


def _principal(email: str, role: str, *read: str) -> _Principal:
    return _Principal(
        email=email,
        role=role,
        read=frozenset(read),
        user_id=uuid.uuid4(),
        key_id=uuid.uuid4(),
    )


SUMMARIZER: Final = _principal("summarizer@staff.example", "writer", OPEN, STAFF)
WRITER: Final = _principal("wes@open.example", "writer", OPEN)
VIEWER: Final = _principal("vi@open.example", "viewer", OPEN)


def _as(principal: _Principal) -> None:
    install_identity(principal.identity())


@dataclass(frozen=True, slots=True, kw_only=True)
class _Filed:
    session: uuid.UUID
    artifact: uuid.UUID
    label: str
    description: str


def _document(session: uuid.UUID) -> str:
    """Return a summary's description: words, counts and item numbers, no lines."""
    step = {"start": 0, "end": 0, "title": "Fix", "text": "Fixed the loader."}
    return json.dumps(
        {
            "kind": "session",
            "session": str(session),
            "overview": "The loader was fixed.",
            "steps": [step],
            "phases": [step],
            "beliefs": [
                {
                    "id": "b0",
                    "claim": "The loader reads CSV.",
                    "evidence": [{"at": 0, "valence": 1}],
                },
            ],
        },
    )


@pytest_asyncio.fixture(loop_scope="session")
async def filed(pglite_route_client: _Client) -> _Filed:
    """Seed a staff session holding the canary and the summary filed for it."""
    client, store = pglite_route_client
    async with store.engine.acquire() as conn:
        for who in (SUMMARIZER, WRITER, VIEWER):
            await conn.execute(
                "INSERT INTO users (id, email, name, role, status, "
                "visual_workspace_enabled) VALUES ($1, $2, 'n', 'writer', 'active', "
                "TRUE)",
                who.user_id,
                who.email,
            )
            await conn.execute(
                "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
                "VALUES ($1, $2, 'k', 'hash', $3, 'writer')",
                who.key_id,
                who.user_id,
                f"trax_{who.key_id.hex[:6]}",
            )
        session = uuid.uuid4()
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title, owner, "
            "agentsession_cli, agentsession_rooms, "
            "agentsession_opened_by_api_key_id, agentsession_audience) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "'lead@example.com', 'lead', 'lead', 'codex', ARRAY['camp'], $2, "
            "'staff')",
            session,
            SUMMARIZER.key_id,
        )
    _ = await store.upsert_session_manifest(
        session,
        name="s.jsonl",
        metadata=immutable({}),
        ir_id=uuid.uuid4(),
        format="claude",
        records=1,
    )
    await store.append_session_records(
        session,
        [
            SessionRecordRow.of(
                session_id=session,
                part=0,
                idx=0,
                record=UserMessage(content=_CANARY),
            ),
        ],
    )
    description = _document(session)
    label = f"summary:session:{session}"
    _as(SUMMARIZER)
    made = await client.post(
        "/api/inquiries/artifact",
        json={
            "title": "Summary of AgentSession#1: Fix the loader",
            "description": description,
            "labels": [label, "summary", "campaign:camp"],
            "status": "complete",
            "actor": "summarizer",
            "idempotency_key": str(uuid.uuid4()),
        },
    )
    assert made.status_code in {200, 201}, made.text
    artifact = uuid.UUID(
        from_plain(from_plain(made.json(), dict[str, object])["id"], str),
    )
    edge = await client.post(
        f"/api/edges/{artifact}/produced_by/{session}",
        json={"actor": "summarizer"},
    )
    assert edge.status_code in {200, 201}, edge.text
    return _Filed(
        session=session,
        artifact=artifact,
        label=label,
        description=description,
    )


@pytest.mark.parametrize("reader", [WRITER, VIEWER])
async def test_an_open_reader_finds_and_reads_the_summary(
    pglite_route_client: _Client,
    filed: _Filed,
    reader: _Principal,
) -> None:
    client, _ = pglite_route_client
    _as(reader)

    listed = await client.get(
        "/api/inquiries",
        params={
            "kind": "Artifact",
            "filter": json.dumps({"field": "labels", "op": "is", "value": filed.label}),
        },
    )
    one = await client.get(f"/api/inquiries/{filed.artifact}")

    assert listed.status_code == 200, listed.text
    [row] = from_plain(listed.json(), list[dict[str, object]])
    assert row["description"] == filed.description
    assert one.status_code == 200
    assert _CANARY not in listed.text + one.text


@pytest.mark.parametrize("reader", [WRITER, VIEWER])
@pytest.mark.parametrize("route", ["parts", "records", "turns"])
async def test_an_item_in_the_summary_does_not_open_for_an_open_reader(
    pglite_route_client: _Client,
    filed: _Filed,
    reader: _Principal,
    route: str,
) -> None:
    client, _ = pglite_route_client
    _as(reader)

    refused = await client.get(f"/api/sessions/{filed.session}/{route}")

    assert refused.status_code == 403, refused.text
    assert _CANARY not in refused.text
    detail = from_plain(
        from_plain(refused.json(), dict[str, object])["detail"],
        dict[str, object],
    )
    assert detail == {"code": "session_audience", "audience": "staff"}


async def test_the_same_item_opens_for_staff(
    pglite_route_client: _Client,
    filed: _Filed,
) -> None:
    client, _ = pglite_route_client
    _as(SUMMARIZER)

    opened = await client.get(
        f"/api/sessions/{filed.session}/records",
        params={"part": 0},
    )

    assert opened.status_code == 200
    assert _CANARY in opened.text


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
