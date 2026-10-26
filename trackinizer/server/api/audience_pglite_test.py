"""A session's audience gates its transcript on every route that names it by id.

A ``staff`` session is inserted directly, except in the last tests, which open one
through a key pinned to ``staff``.
Seven principals share one PGlite database: staff, an open writer, an open viewer, a
viewer that opened a session of its own, a campaign lead's staff-owned key that reads
only ``open``, the assistant, and an anonymous caller. Each acts through the
real app, so a route that forgets the check answers 200 here.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final, cast

import uuid

from treekle import from_plain, immutable

import pytest
import pytest_asyncio

from trackinizer.sagent.session.types import UserMessage
from trackinizer.server.api.app import app
from trackinizer.server.api.canvas_test_support import ASSISTANT_CONFIG
from trackinizer.server.api.conftest import (
    clear_identity_override,
    install_identity,
    make_test_identity,
)
from trackinizer.server.auth import create_api_key
from trackinizer.server.config import Config
from trackinizer.server.route_iter import iter_routes
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.audience import OPEN, STAFF


if TYPE_CHECKING:
    import httpx2

    from trackinizer.server.auth import AuthIdentity
    from trackinizer.server.inbound import InboundQueue
    from trackinizer.server.store.core import Store


pytestmark = [pytest.mark.db_pglite, pytest.mark.asyncio(loop_scope="session")]

_CANARY: Final = "CANARY-7f3a"

type _Client = tuple[httpx2.AsyncClient, Store]


@dataclass(frozen=True, slots=True, kw_only=True)
class _Principal:
    """One caller: a user, the API key it acts with, and what it reads."""

    email: str
    role: str
    read: frozenset[str]
    user_id: uuid.UUID
    key_id: uuid.UUID

    def identity(self) -> AuthIdentity:
        """Build the identity the app resolves this principal to."""
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


STAFF_USER: Final = _principal("lee@staff.example", "writer", OPEN, STAFF)
WRITER: Final = _principal("wes@open.example", "writer", OPEN)
VIEWER: Final = _principal("vi@open.example", "viewer", OPEN)
CAMPAIGN: Final = _principal("cam@staff.example", "writer", OPEN)
ASSISTANT: Final = _principal("scout@example.com", "writer", OPEN)
ALL: Final = (STAFF_USER, WRITER, VIEWER, CAMPAIGN, ASSISTANT)
# The configuration under which the server itself finds the staff user to be staff, as
# it must for an asker the assistant names.
_SERVING_STAFF: Final = replace(ASSISTANT_CONFIG, staff=(STAFF_USER.email,))


def _inbound() -> InboundQueue:
    """Return the queue the app under test is serving."""
    return cast("InboundQueue", app.state.inbound)


def _as(principal: _Principal) -> None:
    install_identity(principal.identity())


@dataclass(frozen=True, slots=True, kw_only=True)
class _Hidden:
    """The staff sessions of one test, by who opened them."""

    lead: uuid.UUID
    campaign_own: uuid.UUID
    viewer_own: uuid.UUID


@pytest_asyncio.fixture(loop_scope="session")
async def hidden(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> _Hidden:
    """Seed the principals and three live staff sessions holding the canary."""
    _, store = pglite_route_client
    monkeypatch.setattr(app.state, "config", ASSISTANT_CONFIG, raising=False)
    async with store.engine.acquire() as conn:
        for who in ALL:
            await conn.execute(
                "INSERT INTO users (id, email, name, role, status, "
                "visual_canvas_enabled) VALUES ($1, $2, 'n', 'writer', 'active', "
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
    seeded = _Hidden(
        lead=await _staff_session(store, STAFF_USER, actor="lead"),
        campaign_own=await _staff_session(store, CAMPAIGN, actor="campaign"),
        viewer_own=await _staff_session(store, VIEWER, actor="viewer-run"),
    )
    for session_id in (seeded.lead, seeded.campaign_own, seeded.viewer_own):
        _inbound().mark_poller(session_id)
    return seeded


# A session with no ``actor`` is unowned, which is what a purge requires.
async def _staff_session(
    store: Store,
    opener: _Principal,
    *,
    actor: str | None,
    account: str = "tester@example.com",
) -> uuid.UUID:
    """Insert a live ``staff`` session opened by ``opener`` that holds the canary."""
    session_id = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title, owner, "
            "agentsession_cli, agentsession_rooms, agentsession_opened_by_api_key_id, "
            "agentsession_audience) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "$4, coalesce($2, 'unowned'), $2, 'codex', ARRAY['ops'], $3, 'staff')",
            session_id,
            actor,
            opener.key_id,
            account,
        )
    _ = await store.upsert_session_manifest(
        session_id,
        name="s.jsonl",
        metadata=immutable({}),
        ir_id=uuid.uuid4(),
        format="claude",
        records=1,
    )
    await store.append_session_records(
        session_id,
        [
            SessionRecordRow.of(
                session_id=session_id,
                part=0,
                idx=0,
                record=UserMessage(content=_CANARY),
            ),
        ],
    )
    return session_id


def _refusal(response: httpx2.Response) -> dict[str, object]:
    """Return the body of a 403 and assert it names the audience, not the content."""
    assert response.status_code == 403, response.text
    assert _CANARY not in response.text
    detail = from_plain(response.json(), dict[str, object])["detail"]
    return from_plain(detail, dict[str, object])


async def _audience_of(store: Store, session_id: uuid.UUID) -> str:
    async with store.engine.acquire() as conn:
        found = await conn.fetchval(
            "SELECT agentsession_audience FROM inquiries WHERE id = $1",
            session_id,
        )
    return from_plain(found, str)


@pytest.mark.parametrize("route", ["parts", "records", "turns"])
async def test_an_open_reader_is_refused_a_staff_sessions_transcript(
    pglite_route_client: _Client,
    hidden: _Hidden,
    route: str,
) -> None:
    client, _ = pglite_route_client
    url = f"/api/sessions/{hidden.lead}/{route}"

    refusals: list[dict[str, object]] = []
    for principal in (WRITER, VIEWER, ASSISTANT):
        _as(principal)
        refusals.append(_refusal(await client.get(url)))
    _as(STAFF_USER)
    allowed = await client.get(url)

    assert refusals == [{"code": "session_audience", "audience": "staff"}] * 3
    assert allowed.status_code == 200
    assert route == "parts" or _CANARY in allowed.text


async def test_the_key_that_opened_a_staff_session_reads_appends_drains_and_ends_it(
    pglite_route_client: _Client,
    hidden: _Hidden,
) -> None:
    client, _ = pglite_route_client
    session = hidden.campaign_own
    _as(CAMPAIGN)

    reads = [
        (await client.get(f"/api/sessions/{session}/{route}")).status_code
        for route in ("parts", "records", "turns")
    ]
    appended = await client.post(f"/api/sessions/{session}/records", json={})
    drained = await client.get(f"/api/sessions/{session}/inbound")
    ended = await client.post(f"/api/sessions/{session}/end", json={})

    assert reads == [200, 200, 200]
    assert (appended.status_code, drained.status_code, ended.status_code) == (
        200,
        200,
        200,
    )


async def test_a_viewer_key_runs_the_staff_session_it_opened(
    pglite_route_client: _Client,
    hidden: _Hidden,
) -> None:
    client, _ = pglite_route_client
    session = hidden.viewer_own
    _as(VIEWER)

    read = await client.get(f"/api/sessions/{session}/records")
    appended = await client.post(f"/api/sessions/{session}/records", json={})
    ended = await client.post(f"/api/sessions/{session}/end", json={})

    assert (read.status_code, appended.status_code, ended.status_code) == (
        200,
        200,
        200,
    )


async def test_a_writer_who_neither_opened_nor_reads_it_cannot_append_to_it(
    pglite_route_client: _Client,
    hidden: _Hidden,
) -> None:
    client, _ = pglite_route_client
    url = f"/api/sessions/{hidden.lead}/records"

    _as(WRITER)
    refused = await client.post(url, json={})
    _as(CAMPAIGN)
    other_key = await client.post(url, json={})
    _as(STAFF_USER)
    allowed = await client.post(url, json={})

    assert _refusal(refused) == {"code": "session_audience", "audience": "staff"}
    assert other_key.status_code == 403
    assert allowed.status_code == 200


async def test_ending_a_staff_session_needs_its_audience_or_its_opener(
    pglite_route_client: _Client,
    hidden: _Hidden,
) -> None:
    client, store = pglite_route_client
    url = f"/api/sessions/{hidden.lead}/end"

    _as(WRITER)
    refused = await client.post(url, json={})
    _as(STAFF_USER)
    allowed = await client.post(url, json={})

    assert _refusal(refused) == {"code": "session_audience", "audience": "staff"}
    assert allowed.status_code == 200
    assert await _audience_of(store, hidden.lead) == "staff"


async def test_purging_or_editing_a_staff_session_by_id_is_refused(
    pglite_route_client: _Client,
    hidden: _Hidden,
) -> None:
    client, store = pglite_route_client
    session = hidden.lead
    _as(WRITER)

    purge = await client.request("DELETE", f"/api/inquiries/{session}", json={})
    edits = [
        await client.put(f"/api/inquiries/{session}/title", json={"value": "mine"}),
        await client.put(f"/api/inquiries/{session}/owner", json={"value": "wes"}),
        await client.put(f"/api/agentsession/{session}/cli", json={"value": "sh"}),
        await client.patch(
            f"/api/agentsession/{session}/rooms",
            json={"op": "add", "value": "x"},
        ),
        await client.request(
            "DELETE",
            f"/api/agentsession/{session}/cli_session_id",
            json={},
        ),
    ]

    assert [_refusal(purge)["code"]] + [_refusal(r)["code"] for r in edits] == [
        "session_audience",
    ] * 6
    async with store.engine.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT title, owner, agentsession_cli FROM inquiries WHERE id = $1",
            session,
        )
    assert row is not None
    assert (row["title"], row["owner"], row["agentsession_cli"]) == (
        "lead",
        "lead",
        "codex",
    )


async def test_staff_edits_and_purges_a_staff_session_by_id(
    pglite_route_client: _Client,
    hidden: _Hidden,
) -> None:
    client, store = pglite_route_client
    # A row is purged by its account, so the session is the staff user's own.
    unowned = await _staff_session(
        store,
        STAFF_USER,
        actor=None,
        account=STAFF_USER.email,
    )
    _as(STAFF_USER)

    edited = await client.put(
        f"/api/inquiries/{hidden.lead}/title",
        json={"value": "renamed"},
    )
    purged = await client.request("DELETE", f"/api/inquiries/{unowned}", json={})

    assert (edited.status_code, purged.status_code) == (200, 200)


async def test_input_to_a_staff_session_needs_its_audience_or_the_assistant(
    pglite_route_client: _Client,
    hidden: _Hidden,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The assistant relays for an asker who holds staff, as staff sends itself.

    The askers it is refused for are in ``assistant_asker_pglite_test``.
    """
    monkeypatch.setattr(app.state, "config", _SERVING_STAFF, raising=False)
    client, _ = pglite_route_client
    url = f"/api/sessions/{hidden.lead}/inbound"
    answers = {}
    for name, principal in (
        ("writer", WRITER),
        ("viewer", VIEWER),
        ("staff", STAFF_USER),
        ("assistant", ASSISTANT),
    ):
        _as(principal)
        body = {"text": "hi"}
        if name == "assistant":
            body["asker"] = STAFF_USER.email
        answers[name] = (await client.post(url, json=body)).status_code

    assert answers["writer"] == answers["viewer"] == 403
    assert answers["staff"] == answers["assistant"] == 200
    assert _inbound().pending(hidden.lead) == 2


async def test_only_the_opener_or_a_holder_drains_a_staff_sessions_queue(
    pglite_route_client: _Client,
    hidden: _Hidden,
) -> None:
    client, _ = pglite_route_client
    url = f"/api/sessions/{hidden.lead}/inbound"

    _as(WRITER)
    refused = await client.get(url)
    _as(ASSISTANT)
    assistant = await client.get(url)
    _as(STAFF_USER)
    allowed = await client.get(url)

    assert _refusal(refused) == {"code": "session_audience", "audience": "staff"}
    assert assistant.status_code == 403
    assert allowed.status_code == 200


async def test_a_message_to_a_staff_actor_is_refused_whole_and_queues_nothing(
    pglite_route_client: _Client,
    hidden: _Hidden,
) -> None:
    client, _ = pglite_route_client
    _as(WRITER)

    refused = await client.post("/api/messages", json={"actor": "lead", "text": "hi"})
    by_room = await client.post(
        "/api/messages",
        json={"actor": "lead", "room": "ops", "text": "hi"},
    )

    assert _refusal(refused) == {"code": "session_audience", "audience": "staff"}
    assert by_room.status_code == 403
    assert _inbound().pending(hidden.lead) == 0


async def test_the_assistant_and_staff_message_a_staff_actor(
    pglite_route_client: _Client,
    hidden: _Hidden,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app.state, "config", _SERVING_STAFF, raising=False)
    client, _ = pglite_route_client

    delivered: list[object] = []
    for principal in (ASSISTANT, STAFF_USER):
        _as(principal)
        sent = await client.post(
            "/api/messages",
            json={
                "actor": "lead",
                "room": "ops",
                "text": "hi",
                **({"asker": STAFF_USER.email} if principal is ASSISTANT else {}),
            },
        )
        assert sent.status_code == 200, sent.text
        delivered.append(from_plain(sent.json(), dict[str, object])["delivered"])

    assert delivered == [[str(hidden.lead)]] * 2
    assert _inbound().pending(hidden.lead) == 2


async def test_a_staff_sessions_envelope_and_filed_rows_stay_open(
    pglite_route_client: _Client,
    hidden: _Hidden,
) -> None:
    client, _ = pglite_route_client
    _as(STAFF_USER)
    filed = await client.post("/api/inquiries/issue", json={"title": "found by lead"})
    assert filed.status_code in {200, 201}, filed.text
    issue = from_plain(filed.json(), dict[str, object])["id"]

    _as(WRITER)
    session = await client.get(f"/api/inquiries/{hidden.lead}")
    row = await client.get(f"/api/inquiries/{issue}")

    assert session.status_code == 200
    body = from_plain(session.json(), dict[str, object])
    assert (body["title"], body["audience"]) == ("lead", "staff")
    assert row.status_code == 200
    assert _CANARY not in session.text


async def test_no_route_sets_an_audience_field(
    pglite_route_client: _Client,
    hidden: _Hidden,
) -> None:
    """A session's owner hides it by its own route, which lowers no staff session."""
    client, store = pglite_route_client
    _as(STAFF_USER)
    path = f"/api/agentsession/{hidden.lead}/audience"

    put = await client.put(path, json={"value": "open"})
    delete = await client.request("DELETE", path, json={})
    lowered = await client.post(
        f"/api/sessions/{hidden.lead}/audience",
        json={"audience": "open"},
    )

    assert (put.status_code, delete.status_code) == (404, 404)
    assert [(p, sorted(m)) for p, m in iter_routes(app) if p.endswith("/audience")] == [
        ("/api/sessions/{session_id}/audience", ["POST"]),
    ]
    assert lowered.status_code == 409
    assert await _audience_of(store, hidden.lead) == "staff"


async def test_an_unauthenticated_caller_is_refused_before_the_audience_is_read(
    pglite_route_client: _Client,
    hidden: _Hidden,
) -> None:
    client, _ = pglite_route_client
    clear_identity_override()

    response = await client.get(f"/api/sessions/{hidden.lead}/records")

    assert response.status_code == 401
    assert _CANARY not in response.text


async def test_every_session_opened_today_is_open(
    pglite_route_client: _Client,
    hidden: _Hidden,
) -> None:
    client, store = pglite_route_client
    _as(STAFF_USER)

    started = await client.post("/api/sessions/start", json={"cli": "codex"})
    submitted = await client.post(
        "/api/inquiries/agentsession",
        json={"title": "run", "cli": "codex"},
    )
    batched = await client.post(
        "/api/inquiries/batch",
        json={
            "items": [
                {
                    "kind": "AgentSession",
                    "title": "b",
                    "cli": "codex",
                    "idempotency_key": str(uuid.uuid4()),
                },
            ],
        },
    )

    assert (started.status_code, submitted.status_code) == (201, 201), submitted.text
    assert batched.status_code in {200, 201}, batched.text
    async with store.engine.acquire() as conn:
        found = await conn.fetch(
            "SELECT DISTINCT agentsession_audience FROM inquiries "
            "WHERE kind = 'AgentSession' AND id <> ALL($1)",
            [hidden.lead, hidden.campaign_own, hidden.viewer_own],
        )
    assert [row["agentsession_audience"] for row in found] == ["open"]


async def test_the_audience_of_each_session_is_read_by_id(
    pglite_route_client: _Client,
    hidden: _Hidden,
) -> None:
    _, store = pglite_route_client
    no_session = uuid.uuid4()

    found = await store.read_push_audiences([hidden.lead, no_session])

    assert found == {hidden.lead: STAFF}


@pytest_asyncio.fixture(loop_scope="session")
async def staff_owner(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seed the staff user whose keys the tests below present, and serve as staff."""
    _, store = pglite_route_client
    monkeypatch.setattr("trackinizer.server.auth._SCRYPT_N", 2)
    monkeypatch.setattr(
        app.state,
        "config",
        Config(staff=(STAFF_USER.email,)),
        raising=False,
    )
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status, "
            "visual_canvas_enabled) VALUES ($1, $2, 'n', 'writer', 'active', TRUE)",
            STAFF_USER.user_id,
            STAFF_USER.email,
        )
    clear_identity_override()


async def _key(store: Store, *, open_into: str | None) -> dict[str, str]:
    """Mint a real key for the staff user, pinned to ``open_into``, as a header."""
    async with store.engine.acquire() as conn:
        _, secret, _, _ = await create_api_key(
            conn,
            user_id=STAFF_USER.user_id,
            name="pinned",
            ceiling="admin",
            open_into=open_into,
        )
    return {"Authorization": f"Bearer {secret}"}


def _batch_of(*items: tuple[str, str]) -> dict[str, object]:
    """Build a batch body of ``(kind, title)`` items, each with its own key."""
    return {
        "items": [
            {"kind": kind, "title": title, "idempotency_key": str(uuid.uuid4())}
            for kind, title in items
        ],
    }


async def _open_each_way(
    client: httpx2.AsyncClient,
    headers: dict[str, str],
) -> list[httpx2.Response]:
    """Open a session by the start route, the submit route and the batch route."""
    return [
        await client.post(
            "/api/sessions/start",
            json={"cli": "codex"},
            headers=headers,
        ),
        await client.post(
            "/api/inquiries/agentsession",
            json={"title": "run", "cli": "codex"},
            headers=headers,
        ),
        await client.post(
            "/api/inquiries/batch",
            json=_batch_of(("AgentSession", "b")),
            headers=headers,
        ),
    ]


async def _audiences_opened(store: Store, opened: list[httpx2.Response]) -> list[str]:
    """Return the audience of the session each response of the three routes opened."""
    start, submit, batch = (from_plain(r.json(), dict[str, object]) for r in opened)
    ids = [
        from_plain(start["id"], str),
        from_plain(submit["id"], str),
        from_plain(batch["ids"], list[str])[0],
    ]
    return [await _audience_of(store, uuid.UUID(session_id)) for session_id in ids]


async def _count(store: Store, kind: str) -> int:
    async with store.engine.acquire() as conn:
        found = await conn.fetchval(
            "SELECT count(*) FROM inquiries WHERE kind = $1",
            kind,
        )
    return from_plain(found, int)


@pytest.mark.usefixtures("staff_owner")
async def test_a_pinned_key_opens_its_pin(pglite_route_client: _Client) -> None:
    client, store = pglite_route_client
    headers = await _key(store, open_into="staff")

    opened = await _open_each_way(client, headers)

    assert [r.status_code for r in opened] == [201, 201, 200], [r.text for r in opened]
    assert await _audiences_opened(store, opened) == ["staff"] * 3


@pytest.mark.usefixtures("staff_owner")
async def test_an_unpinned_key_opens_open(pglite_route_client: _Client) -> None:
    """A staff-owned key with no pin still opens ``open``: staff is never a default."""
    client, store = pglite_route_client
    headers = await _key(store, open_into=None)

    opened = await _open_each_way(client, headers)

    assert [r.status_code for r in opened] == [201, 201, 200], [r.text for r in opened]
    assert await _audiences_opened(store, opened) == ["open"] * 3


@pytest.mark.usefixtures("staff_owner")
async def test_a_pin_the_owner_lost_is_403(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A demoted owner's staff key is refused, not quietly opened into ``open``."""
    client, store = pglite_route_client
    headers = await _key(store, open_into="staff")
    monkeypatch.setattr(app.state, "config", Config())

    refused = await _open_each_way(client, headers)

    assert [_refusal(r) for r in refused] == [
        {"code": "audience_not_held", "audience": "staff"},
    ] * 3
    assert await _count(store, "AgentSession") == 0


@pytest.mark.usefixtures("staff_owner")
async def test_a_pin_the_owner_lost_still_files_rows_that_are_not_sessions(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pin decides where a session opens; it does not gate an Issue."""
    client, store = pglite_route_client
    headers = await _key(store, open_into="staff")
    monkeypatch.setattr(app.state, "config", Config())

    single = await client.post(
        "/api/inquiries/issue",
        json={"title": "found"},
        headers=headers,
    )
    batch = await client.post(
        "/api/inquiries/batch",
        json=_batch_of(("Issue", "i")),
        headers=headers,
    )
    mixed = await client.post(
        "/api/inquiries/batch",
        json=_batch_of(("Issue", "j"), ("AgentSession", "s")),
        headers=headers,
    )

    assert (single.status_code, batch.status_code) == (201, 200)
    assert _refusal(mixed) == {"code": "audience_not_held", "audience": "staff"}
    assert await _count(store, "Issue") == 2


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
