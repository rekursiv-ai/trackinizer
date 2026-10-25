"""A science chat's transcript over the real app: its members read it, no one else.

Ann began the chat with the assistant and shared it with Sam; Olu is in Ann's
organisation. Sid reads ``open``, Stu ``open`` and ``staff``, Ada is an admin, the agent
is a service account, and Scout is the assistant on a key it rotated to after the chat
began. Beside the chat sit the agent's ordinary ``open`` session and a ``staff`` one.
Who else reads each is the server's organisation mode: everyone on one organisation,
Ann's organisation by domain, or no one else when each person is their own.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Final
from uuid import UUID, uuid4

from treekle import from_plain, json_loads

import httpx2
import pytest
import pytest_asyncio

from trackinizer.lib.agent.types.sessions import AgentToAgentMessage, AssistantMessage
from trackinizer.server.api.app import app
from trackinizer.server.api.canvas_test_support import (
    Agent,
    chat_labels,
    open_canvas,
    seed_session,
    send_chat,
)
from trackinizer.server.api.conftest import install_identity
from trackinizer.server.auth import AuthIdentity, Role
from trackinizer.server.config import Assistant, ChatOrgs, Config
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.store.core import Store
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.wire_export import EXPORT_API_PATH
from trackinizer.wire.wire_science_chat import chat_session_id
from trackinizer.wire.wire_session_ir import ManifestBody, RecordBody


pytestmark = [pytest.mark.db_pglite, pytest.mark.asyncio(loop_scope="session")]

_CANARY: Final = "chatline4c1f"
_SCOUT: Final = Assistant(actor="scout", email="scout@service.invalid")
_EMAILS: Final[Mapping[str, str]] = {
    "ann": "ann@lab.example",
    "sam": "sam@gmail.com",
    "olu": "olu@lab.example",
    "sid": "sid@else.example",
    "stu": "stu@else.example",
    "ada": "ada@ops.example",
    "scout": _SCOUT.email,
    "agent": "agent@service.invalid",
}
_STAFF_READERS: Final = frozenset({"stu", "ada", "agent", "scout"})
_TRANSCRIPT_READS: Final = ("parts", "records", "turns")


@dataclass(frozen=True, slots=True, kw_only=True)
class _Person:
    """A user and the API key they act through."""

    user_id: UUID
    key_id: UUID
    email: str


@dataclass(frozen=True, slots=True, kw_only=True)
class _World:
    """The app, the people, Ann's chat, and the two sessions that are no chat."""

    client: httpx2.AsyncClient
    store: Store
    inbound: InboundQueue
    people: Mapping[str, _Person]
    rotated_key: UUID
    conversation: UUID
    chat: UUID
    open: UUID
    staff: UUID

    def act_as(self, name: str, *, browser: bool = False) -> None:
        """Answer every following request as ``name``, by key or from a browser."""
        person = self.people[name]
        install_identity(
            AuthIdentity(
                user_id=person.user_id,
                api_key_id=None if browser else self._key_of(name),
                email=person.email,
                role=_role(name),
                read_audiences=frozenset(
                    ("open", "staff") if name in _STAFF_READERS else ("open",),
                ),
                # Sam signs in with a consumer account, which Google names no member
                # of an organisation; everyone else with their domain's.
                hosted_domain=None
                if name == "sam"
                else person.email.rpartition("@")[2],
            ),
        )

    def serve_chats_as(self, orgs: ChatOrgs) -> None:
        """Group chats by ``orgs`` from the next request on."""
        app.state.config = _config(orgs)

    async def status(self, name: str, *, read: str, session: UUID) -> int:
        """Return the status ``name`` gets reading ``session``'s ``read`` route."""
        self.act_as(name)
        return (await self.client.get(f"/api/sessions/{session}/{read}")).status_code

    def _key_of(self, name: str) -> UUID:
        """Return the key ``name`` acts through: Scout's is the one it rotated to."""
        return self.rotated_key if name == "scout" else self.people[name].key_id


@pytest.fixture(autouse=True)
def assistant_served(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve with Scout configured, each email domain its own organisation."""
    monkeypatch.setattr(app.state, "config", _config("domain"), raising=False)


@pytest_asyncio.fixture(loop_scope="session")
async def world(pglite_route_client: tuple[httpx2.AsyncClient, Store]) -> _World:
    """Seed the people, Ann's chat with its share, and an open and a staff session."""
    client, store = pglite_route_client
    # The route client serves a fresh queue, which a seeded session is polled on.
    inbound = getattr(app.state, "inbound", None)
    assert isinstance(inbound, InboundQueue)
    people = {name: await _person(store, email) for name, email in _EMAILS.items()}
    conversation = uuid4()
    chat = await _seed_chat(
        store,
        conversation=conversation,
        opener=people["scout"],
        starter=_EMAILS["ann"],
    )
    agent = Agent(key_id=people["agent"].key_id, email=_EMAILS["agent"])
    opened = await seed_session(store, agent, actor="open-run")
    staff = await seed_session(store, agent, actor="staff-run")
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET agentsession_audience = 'staff' WHERE id = $1",
            staff,
        )
        await conn.execute(
            "INSERT INTO science_chat_shares (conversation_id, email, shared_by) "
            "VALUES ($1, $2, $3)",
            conversation,
            _EMAILS["sam"],
            _EMAILS["ann"],
        )
    return _World(
        client=client,
        store=store,
        inbound=inbound,
        people=people,
        rotated_key=await _key(store, people["scout"].user_id),
        conversation=conversation,
        chat=chat,
        open=opened,
        staff=staff,
    )


# Who reads Ann's chat in each mode, besides Ann, Sam and the assistant; and whether
# Sid, who is no staff and of no organisation of the agent's, reads its open session.
_READERS_BY_MODE: Final[Mapping[ChatOrgs, tuple[frozenset[str], bool]]] = {
    "single": (frozenset({"olu", "sid", "stu", "ada"}), True),
    "domain": (frozenset({"olu"}), False),
    "individual": (frozenset[str](), False),
}


@pytest.mark.parametrize("orgs", ["single", "domain", "individual"])
@pytest.mark.parametrize("read", _TRANSCRIPT_READS)
async def test_another_user_is_refused_a_chats_transcript_by_id_as_the_mode_says(
    world: _World,
    read: str,
    orgs: ChatOrgs,
) -> None:
    """Staff and admins read every session but no one's chat; the mode sets the rest."""
    world.serve_chats_as(orgs)
    others, sid_reads_open = _READERS_BY_MODE[orgs]
    seen: dict[str, tuple[int, object, bool]] = {}
    for name in _EMAILS:
        world.act_as(name)
        response = await world.client.get(f"/api/sessions/{world.chat}/{read}")
        body = json_loads(response.content, dict[str, object])
        seen[name] = (
            response.status_code,
            body.get("detail") if response.status_code == 403 else None,
            _CANARY in response.text,
        )
    sessions = {
        (name, audience): await world.status(name, read=read, session=session)
        for name in ("stu", "sid")
        for audience, session in (("open", world.open), ("staff", world.staff))
    }

    hidden = {"code": "session_audience", "audience": "open"}
    readers = {"ann", "sam", "scout"} | others
    assert seen == {
        name: (200, None, read != "parts") if name in readers else (403, hidden, False)
        for name in _EMAILS
    }
    assert sessions == {
        ("stu", "open"): 200,
        ("stu", "staff"): 200,
        ("sid", "open"): 200 if sid_reads_open else 403,
        ("sid", "staff"): 403,
    }


async def test_another_user_cannot_end_append_drain_message_or_send_input_to_a_chat(
    world: _World,
) -> None:
    world.act_as("sid")
    on_chat = await _write_everything(world, session=world.chat, actor="chat-ann")
    world.act_as("stu")
    on_staff = await _write_everything(world, session=world.staff, actor="staff-run")

    assert on_chat == dict.fromkeys(on_chat, (403, True))
    assert on_staff == dict.fromkeys(on_staff, (200, False))
    inbound = world.inbound
    assert (inbound.pending(world.chat), inbound.has_poller(world.chat)) == (0, True)
    async with world.store.engine.acquire() as conn:
        ended = await conn.fetchval(
            "SELECT agentsession_ended IS NOT NULL FROM inquiries WHERE id = $1",
            world.chat,
        )
    assert ended is False


async def test_a_share_admits_at_once_and_an_unshare_refuses_at_once(
    world: _World,
) -> None:
    share = f"/api/chats/{world.conversation}/shares/{_EMAILS['sam']}"

    shared = await world.status("sam", read="records", session=world.chat)
    world.act_as("ann", browser=True)
    assert (await world.client.delete(share)).status_code == 200
    unshared = await world.status("sam", read="records", session=world.chat)
    world.act_as("ann", browser=True)
    assert (await world.client.put(share)).status_code == 200
    reshared = await world.status("sam", read="records", session=world.chat)

    assert (shared, unshared, reshared) == (200, 403, 200)


async def test_a_fork_cannot_copy_a_chat_its_poster_may_not_read(world: _World) -> None:
    """The assistant copies a fork's lines from its original, so the poster must read it."""
    _ = await seed_session(
        world.store,
        Agent(key_id=world.rotated_key, email=_SCOUT.email),
        actor=_SCOUT.actor,
    )
    own = await _seed_chat(
        world.store,
        conversation=uuid4(),
        opener=world.people["scout"],
        starter=_EMAILS["sid"],
    )

    forks = {
        (name, session): await _fork(world, name=name, session=session)
        for name, session in (("sid", world.chat), ("sam", world.chat), ("sid", own))
    }

    assert forks == {
        ("sid", world.chat): 422,
        ("sam", world.chat): 200,
        ("sid", own): 200,
    }


async def test_the_admin_reads_no_other_persons_chat_but_still_deletes_it(
    world: _World,
) -> None:
    world.act_as("ada", browser=True)
    shared = await world.client.put(
        f"/api/chats/{world.conversation}/shares/{_EMAILS['ada']}",
    )
    records = await world.client.get(f"/api/sessions/{world.chat}/records")
    exported = await world.client.get(EXPORT_API_PATH)
    deleted = await world.client.delete(f"/api/chats/{world.conversation}")

    assert (shared.status_code, records.status_code) == (403, 403)
    assert exported.status_code == 200
    # The file says it is not whole: it leaves out the chat's transcript.
    header = json_loads(exported.text.partition("\n")[0], dict[str, object])
    assert header["withheld_sessions"] == 1
    assert str(world.staff) in exported.text
    assert _CANARY not in exported.text
    assert f'"session_id":"{world.chat}"' not in exported.text
    assert deleted.status_code == 204


async def test_a_chats_session_row_carries_none_of_the_line_that_began_it(
    world: _World,
) -> None:
    """Every key reads a session's row, so the chat's first line is not its title."""
    conversation = uuid4()
    world.act_as("scout")
    started = await world.client.post(
        "/api/sessions/start",
        json={
            "cli": "sagent",
            "actor": "chat-titled",
            "cli_session_id": chat_session_id(conversation),
            "account": _EMAILS["ann"],
            "title": f"{_CANARY} my diagnosis is",
        },
    )
    session = from_plain(json_loads(started.content, dict[str, object])["id"], str)
    async with world.store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET labels = $2 WHERE id = $1",
            UUID(session),
            chat_labels(),
        )
    paths = (
        f"/api/inquiries/{session}",
        "/api/inquiries?kind=AgentSession",
        f"/api/chats/{conversation}",
        EXPORT_API_PATH,
    )

    world.act_as("sid")
    read = {path: await world.client.get(path) for path in paths}

    assert started.status_code == 201
    shown = {path: (got.status_code, _CANARY in got.text) for path, got in read.items()}
    assert shown == dict.fromkeys(paths, (200, False))


async def test_individual_and_single_servers_set_who_else_reads(world: _World) -> None:
    world.serve_chats_as("individual")
    individual = {
        name: await world.status(name, read="records", session=world.chat)
        for name in ("olu", "sam")
    }
    world.serve_chats_as("single")
    single = {
        name: await world.status(name, read="records", session=world.chat)
        for name in ("sid", "agent")
    }

    assert individual == {"olu": 403, "sam": 200}
    assert single == {"sid": 200, "agent": 403}


async def test_a_member_without_staff_neither_reads_nor_posts_in_a_staff_chat(
    world: _World,
) -> None:
    """Its assistant read a staff session into it: its members who lack staff are out.

    Stu holds staff, and Ann shared the chat with him, so he still posts in it.
    """
    world.serve_chats_as("single")
    await _make_staff(world)
    async with world.store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO science_chat_shares (conversation_id, email, shared_by) "
            "VALUES ($1, $2, $3)",
            world.conversation,
            _EMAILS["stu"],
            _EMAILS["ann"],
        )
    read: dict[str, int] = {}
    posted: dict[str, tuple[int, bool]] = {}
    for name in ("ann", "olu", "stu"):
        read[name] = await world.status(name, read="records", session=world.chat)
        person = world.people[name]
        canvas_id = await open_canvas(
            world.client,
            user_id=person.user_id,
            email=person.email,
        )
        world.act_as(name, browser=True)
        response = await send_chat(
            world.client,
            canvas_id=canvas_id,
            text="hi",
            conversation_id=world.conversation,
        )
        posted[name] = (response.status_code, "chat_not_shared" in response.text)

    assert read == {"ann": 403, "olu": 403, "stu": 200}
    assert (posted["ann"], posted["olu"]) == ((403, True), (403, True))
    assert posted["stu"][1] is False


async def test_a_staff_chat_is_shared_with_staff_only(world: _World) -> None:
    """Its starter shares it with a person who holds staff now, and no one else."""
    app.state.config = replace(_config("single"), staff=(_EMAILS["stu"],))
    await _make_staff(world)
    world.act_as("ann", browser=True)

    to_olu = await world.client.put(
        f"/api/chats/{world.conversation}/shares/{_EMAILS['olu']}",
    )
    to_stu = await world.client.put(
        f"/api/chats/{world.conversation}/shares/{_EMAILS['stu']}",
    )

    assert (to_olu.status_code, to_olu.json()) == (
        409,
        {"detail": {"code": "chat_staff_only"}},
    )
    assert to_stu.status_code == 200
    assert _EMAILS["stu"] in from_plain(to_stu.json(), list[str])


async def test_the_head_says_audience_and_who_may_hide(world: _World) -> None:
    """Its starter may hide an open chat; nobody a staff one; a stranger reads none."""
    world.serve_chats_as("domain")
    heads: list[tuple[str, object, object, object]] = []
    for staffed in (False, True):
        if staffed:
            await _make_staff(world)
        for name in ("ann", "sid"):
            world.act_as(name, browser=True)
            head = json_loads(
                (await world.client.get(f"/api/chats/{world.conversation}")).content,
                dict[str, object],
            )
            heads.append(
                (name, head["audience"], head["may_hide"], head["forks_on_typing"]),
            )

    assert heads == [
        ("ann", "open", True, False),
        ("sid", "open", False, True),
        ("ann", "staff", False, True),
        ("sid", "staff", False, True),
    ]


async def _make_staff(world: _World) -> None:
    """Make every session under Ann's chat's id staff, as a staff read for one does."""
    async with world.store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET agentsession_audience = 'staff' "
            "WHERE agentsession_cli_session_id = $1",
            chat_session_id(world.conversation),
        )


def _config(orgs: ChatOrgs) -> Config:
    """Return the server's configuration: Scout, and chats grouped by ``orgs``."""
    return Config(assistant=_SCOUT, chat_orgs=orgs)


def _role(name: str) -> Role:
    """Return ``name``'s role: Ada is the admin, everyone else a writer."""
    return "admin" if name == "ada" else "writer"


async def _person(store: Store, email: str) -> _Person:
    """Create a user with one key."""
    user_id = uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, $2, 'Test', 'writer', 'active')",
            user_id,
            email,
        )
    return _Person(user_id=user_id, key_id=await _key(store, user_id), email=email)


async def _key(store: Store, user_id: UUID) -> UUID:
    """Mint one more key for ``user_id``."""
    key_id = uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
            "VALUES ($1, $2, 'agent', 'test-hash', $3, 'writer')",
            key_id,
            user_id,
            f"trax_{key_id.hex[:6]}",
        )
    return key_id


async def _seed_chat(
    store: Store,
    *,
    conversation: UUID,
    opener: _Person,
    starter: str,
) -> UUID:
    """Open a chat ``starter`` began with the assistant: a question and its answer."""
    session = await seed_session(
        store,
        Agent(key_id=opener.key_id, email=opener.email),
        actor=f"chat-{starter.partition('@')[0]}",
        cli="sagent",
        cli_session_id=chat_session_id(conversation),
        account=starter,
        labels=chat_labels(),
    )
    now = datetime.now(UTC).isoformat()
    records = (
        AgentToAgentMessage(sender=starter, content=f"{_CANARY} q1", timestamp=now),
        AssistantMessage(content=f"{_CANARY} a1", timestamp=now),
    )
    part = await store.upsert_session_manifest(
        session,
        name="chat.jsonl",
        metadata={},
        ir_id=uuid4(),
        format="sagent",
        records=len(records),
    )
    _ = await store.append_session_records(
        session,
        [
            SessionRecordRow.of(session_id=session, part=part, idx=idx, record=record)
            for idx, record in enumerate(records)
        ],
    )
    return session


async def _write_everything(
    world: _World,
    *,
    session: UUID,
    actor: str,
) -> dict[str, tuple[int, bool]]:
    """Drain, send input to, message, append to and end ``session``, as it answers."""
    client = world.client
    line = AgentToAgentMessage(
        sender="x",
        content="hi",
        timestamp="2026-10-14T00:00:00",
    )
    record = RecordBody.of(
        SessionRecordRow.of(session_id=UUID(int=0), part=0, idx=0, record=line),
    )
    manifest = ManifestBody(
        name="run.jsonl",
        metadata={},
        ir_id=uuid4(),
        format="sagent",
        records=1,
    )
    appended = {
        "name": "run.jsonl",
        "manifest": manifest.model_dump(mode="json"),
        "records": [record.model_dump(mode="json")],
    }
    return {
        "drain": _answer(await client.get(f"/api/sessions/{session}/inbound")),
        "input": _answer(
            await client.post(f"/api/sessions/{session}/inbound", json={"text": "hi"}),
        ),
        "message": _answer(
            await client.post("/api/messages", json={"actor": actor, "text": "hi"}),
        ),
        "append": _answer(
            await client.post(f"/api/sessions/{session}/records", json=appended),
        ),
        "end": _answer(
            await client.post(f"/api/sessions/{session}/end", json={"status": "done"}),
        ),
    }


def _answer(response: httpx2.Response) -> tuple[int, bool]:
    """Return a response's status and whether it refuses by the session's transcript."""
    return response.status_code, "session_audience" in response.text


async def _fork(world: _World, *, name: str, session: UUID) -> int:
    """Fork the first line of ``session`` from ``name``'s own canvas; return the status."""
    person = world.people[name]
    canvas_id = await open_canvas(
        world.client,
        user_id=person.user_id,
        email=person.email,
    )
    response = await send_chat(
        world.client,
        canvas_id=canvas_id,
        text="what if?",
        fork={"session_id": str(session), "part": 0, "idx": 0},
    )
    return response.status_code


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
