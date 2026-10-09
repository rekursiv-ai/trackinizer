"""Forking a science chat over the app: who continues, who forks, what is counted."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import uuid

import pytest

from trackinizer.lib.agent.types.sessions import AgentToAgentMessage, AssistantMessage
from trackinizer.lib.codec import from_plain, loads
from trackinizer.server.api.app import app
from trackinizer.server.api.canvas_test_support import (
    ASSISTANT_CONFIG,
    KB_ACTOR,
    OTHER_EMAIL,
    OTHER_USER_ID,
    act_as_assistant,
    act_as_other_agent,
    browser,
    conversation_of,
    drain,
    open_science_chat,
    open_workspace,
    seed_accounts,
    send_chat,
    show_chat,
    start_session,
)
from trackinizer.server.api.conftest import TEST_USER_EMAIL
from trackinizer.server.inbound import InboundQueue
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.wire_science_chat import (
    FORK_EDGE_LABEL,
    chat_session_id,
    fork_point_label,
)
from trackinizer.wire.wire_session_ir import ManifestBody, RecordBody


if TYPE_CHECKING:
    from collections.abc import Sequence

    import httpx2

    from trackinizer.server.store.core import Store


type _Client = tuple[httpx2.AsyncClient, Store]

JAN: Final = "jan@other.org"
ALICE: Final = "alice@gmail.com"
BOB: Final = "bob@gmail.com"
DOMAIN_CONFIG: Final = replace(ASSISTANT_CONFIG, chat_orgs="domain")


@pytest.fixture(autouse=True)
def domain_orgs_served(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve with scout configured, each email domain its own organisation."""
    monkeypatch.setattr(app.state, "config", DOMAIN_CONFIG, raising=False)
    monkeypatch.setattr(app.state, "inbound", InboundQueue(), raising=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class _Original:
    """A science chat of three lines, and the assistant's service session."""

    conversation: uuid.UUID
    session: uuid.UUID
    service: uuid.UUID

    def at(self, idx: int) -> dict[str, object]:
        """Name one of the lines as the body of a fork does."""
        return {"session_id": str(self.session), "part": 0, "idx": idx}


async def _accounts(store: Store, *emails: str) -> None:
    """Create a user for each email, since a session is attributed to a real account."""
    async with store.engine.acquire() as conn:
        for email in emails:
            await conn.execute(
                "INSERT INTO users (id, email, name, role, status, "
                "visual_workspace_enabled) "
                "VALUES ($1, $2, 'Test', 'writer', 'active', TRUE) ON CONFLICT DO NOTHING",
                uuid.uuid4(),
                email,
            )


async def _original(
    client: httpx2.AsyncClient,
    store: Store,
    *,
    starter: str,
) -> _Original:
    """Open a chat ``starter`` began: a question, an answer and a second question."""
    await seed_accounts(store)
    await _accounts(store, ALICE, JAN)
    act_as_assistant()
    service = await start_session(client, actor=KB_ACTOR)
    conversation = uuid.uuid4()
    session = await open_science_chat(
        client,
        store,
        conversation_id=conversation,
        account=starter,
    )
    act_as_assistant("writer")
    appended = await client.post(
        f"/api/sessions/{session}/records",
        json=_append(
            AgentToAgentMessage(sender=starter, content="q1", timestamp=_now()),
            AssistantMessage(content="a1", timestamp=_now()),
            AgentToAgentMessage(sender=starter, content="q2", timestamp=_now()),
        ),
    )
    assert appended.status_code == 200, appended.text
    return _Original(conversation=conversation, session=session, service=service)


async def _canvas(
    client: httpx2.AsyncClient,
    *,
    email: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Open the second user's canvas, signed in as ``email``; return it and its Chat."""
    workspace_id = await open_workspace(client, user_id=OTHER_USER_ID, email=email)
    return workspace_id, await show_chat(client, workspace_id=workspace_id)


async def _heard(
    client: httpx2.AsyncClient,
    *,
    session_id: uuid.UUID,
) -> list[dict[str, object]]:
    """Drain a session as the assistant; return its messages."""
    act_as_assistant()
    response = await drain(client, session_id=session_id)
    assert response.status_code == 200
    return from_plain(
        from_plain(loads(response.content), dict[str, object])["messages"],
        list[dict[str, object]],
    )


async def _head(
    client: httpx2.AsyncClient,
    *,
    conversation: uuid.UUID,
    email: str,
) -> dict[str, object]:
    """Read a conversation's head as the second user, signed in as ``email``."""
    browser(OTHER_USER_ID, email=email)
    response = await client.get(f"/api/chats/{conversation}")
    assert response.status_code == 200, response.text
    return from_plain(loads(response.content), dict[str, object])


async def _link_fork(
    client: httpx2.AsyncClient,
    store: Store,
    *,
    original: _Original,
    fork: uuid.UUID,
    at: int,
    starter: str,
    labels: Sequence[str] | None = None,
) -> uuid.UUID:
    """Open a fork's session and link it to the original, as the assistant does."""
    session = await open_science_chat(
        client,
        store,
        conversation_id=fork,
        account=starter,
    )
    act_as_assistant("writer")
    linked = await client.post(
        f"/api/edges/{session}/produced_by/{original.session}",
        json={
            "actor": KB_ACTOR,
            "labels": [FORK_EDGE_LABEL, fork_point_label(part=0, idx=at)]
            if labels is None
            else list(labels),
        },
    )
    assert linked.status_code == 200, linked.text
    return session


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_typing_in_a_chat_of_my_organisation_posts_into_it(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    chat = await _original(client, store, starter=TEST_USER_EMAIL)
    workspace_id, chat_id = await _canvas(client, email=OTHER_EMAIL)

    joined = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="and from a colleague",
        conversation_id=chat.conversation,
    )

    assert from_plain(loads(joined.content), dict[str, object]) == {
        "conversation_id": str(chat.conversation),
        "session_id": str(chat.session),
    }
    [heard] = await _heard(client, session_id=chat.session)
    assert heard["source"] == OTHER_EMAIL
    assert (await _heard(client, session_id=chat.service)) == []
    head = await _head(client, conversation=chat.conversation, email=OTHER_EMAIL)
    assert (head["forks_on_typing"], head["forks"]) == (False, 0)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_fork_from_here_in_my_organisation_starts_a_new_chat_at_that_line(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    chat = await _original(client, store, starter=TEST_USER_EMAIL)
    workspace_id, chat_id = await _canvas(client, email=OTHER_EMAIL)
    key = uuid.uuid4()

    forked = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="what if a1 were wrong?",
        key=key,
        fork=chat.at(1),
    )

    # The fork is a new conversation named by the key; it has no session until the
    # assistant opens it, and the original's own queue hears nothing.
    assert from_plain(loads(forked.content), dict[str, object]) == {
        "conversation_id": str(key),
        "session_id": None,
    }
    [heard] = await _heard(client, session_id=chat.service)
    assert (heard["text"], heard["source"]) == ("what if a1 were wrong?", OTHER_EMAIL)
    context = from_plain(heard["context"], dict[str, object])
    assert context["conversation_id"] == str(key)
    assert context["fork"] == chat.at(1)
    assert (await _heard(client, session_id=chat.session)) == []


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_typing_in_a_foreign_chat_is_refused_and_forking_it_is_not(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    chat = await _original(client, store, starter=TEST_USER_EMAIL)
    workspace_id, chat_id = await _canvas(client, email=JAN)
    head = await _head(client, conversation=chat.conversation, email=JAN)
    assert head["forks_on_typing"] is True

    posted = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="jan joins in",
        conversation_id=chat.conversation,
    )
    forked = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="jan forks",
        fork=chat.at(2),
    )

    # The server refuses the post, whatever the browser did, and queues nothing for
    # the original; the fork is a conversation of Jan's own at the latest line.
    assert posted.status_code == 403
    assert (await _heard(client, session_id=chat.session)) == []
    assert forked.status_code == 200
    assert conversation_of(forked) != chat.conversation
    [heard] = await _heard(client, session_id=chat.service)
    assert (heard["text"], heard["source"]) == ("jan forks", JAN)
    assert from_plain(heard["context"], dict[str, object])["fork"] == chat.at(2)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_consumer_domains_are_no_organisation_but_my_own_chat_stays_mine(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    chat = await _original(client, store, starter=ALICE)
    workspace_id, chat_id = await _canvas(client, email=BOB)

    posted = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="bob joins in",
        conversation_id=chat.conversation,
    )
    forked = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="bob forks",
        fork=chat.at(2),
    )
    own = await _canvas(client, email=ALICE)
    mine = await send_chat(
        client,
        workspace_id=own[0],
        chat_id=own[1],
        text="alice goes on",
        conversation_id=chat.conversation,
    )

    assert (posted.status_code, forked.status_code, mine.status_code) == (403, 200, 200)
    assert conversation_of(mine) == chat.conversation


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_single_organisation_server_never_makes_typing_a_fork(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app.state, "config", ASSISTANT_CONFIG, raising=False)
    client, store = pglite_route_client
    chat = await _original(client, store, starter=TEST_USER_EMAIL)
    workspace_id, chat_id = await _canvas(client, email=JAN)

    posted = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="jan joins in",
        conversation_id=chat.conversation,
    )

    assert posted.status_code == 200
    head = await _head(client, conversation=chat.conversation, email=JAN)
    assert head["forks_on_typing"] is False


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_fork_needs_a_line_of_a_science_chat_and_names_no_conversation(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    chat = await _original(client, store, starter=TEST_USER_EMAIL)
    workspace_id, chat_id = await _canvas(client, email=OTHER_EMAIL)

    # A record beyond the session's end, a session that is no chat, and a fork that
    # also names the conversation to post into.
    body = {
        "workspace_id": str(workspace_id),
        "text": "fork",
        "chat_instance_id": str(chat_id),
    }
    assert await _fork(client, body, {**chat.at(0), "idx": 9}) == 422
    other = {**chat.at(0), "session_id": str(chat.service)}
    assert await _fork(client, body, other) == 422
    named = {"conversation_id": str(chat.conversation)}
    assert await _fork(client, body, chat.at(0), **named) == 422
    assert await _fork(client, body, chat.at(0)) == 200
    assert (await _heard(client, session_id=chat.session)) == []


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_fork_leaves_the_original_as_it_was_and_is_counted_by_it(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    chat = await _original(client, store, starter=TEST_USER_EMAIL)
    assert (await _head(client, conversation=chat.conversation, email=JAN))[
        "forks"
    ] == 0
    first, second = uuid.uuid4(), uuid.uuid4()
    forked = await _link_fork(
        client,
        store,
        original=chat,
        fork=first,
        at=1,
        starter=JAN,
    )
    _ = await _link_fork(client, store, original=chat, fork=second, at=2, starter=ALICE)
    # An edge without the fork label, as a campaign's session has, is no fork.
    _ = await _link_fork(
        client,
        store,
        original=chat,
        fork=uuid.uuid4(),
        at=0,
        starter=ALICE,
        labels=[],
    )

    head = await _head(client, conversation=chat.conversation, email=JAN)
    fork_head = await _head(client, conversation=first, email=JAN)
    async with store.engine.acquire() as conn:
        edge = await conn.fetchrow(
            "SELECT edge_kind, labels FROM edges WHERE from_id = $1 AND to_id = $2",
            forked,
            chat.session,
        )
        records = await conn.fetch(
            "SELECT part, idx, kind FROM session_records WHERE session_id = $1 "
            "ORDER BY part, idx",
            chat.session,
        )

    # The original shows two forks and holds the three lines it had; the fork
    # carries the edge and the line it starts after, and names its origin.
    assert (head["forks"], head["forked_from"]) == (2, None)
    assert [(row["part"], row["idx"], row["kind"]) for row in records] == [
        (0, 0, "AgentToAgentMessage"),
        (0, 1, "AssistantMessage"),
        (0, 2, "AgentToAgentMessage"),
    ]
    assert edge is not None
    assert (edge["edge_kind"], from_plain(edge["labels"], list[str])) == (
        "produced_by",
        [FORK_EDGE_LABEL, fork_point_label(part=0, idx=1)],
    )
    assert (fork_head["forked_from"], fork_head["forks"]) == (str(chat.conversation), 0)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_key_that_names_the_original_posts_nothing_into_it(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    chat = await _original(client, store, starter=TEST_USER_EMAIL)
    workspace_id, chat_id = await _canvas(client, email=JAN)

    # The conversation id is in the link Jan was given; sent as the idempotency key of
    # a post that names no conversation, or of a fork, it is still the original.
    typed = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="jan sneaks in",
        key=chat.conversation,
    )
    forked = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="jan forks into it",
        key=chat.conversation,
        fork=chat.at(0),
    )

    assert (typed.status_code, forked.status_code) == (403, 409)
    assert (await _heard(client, session_id=chat.session)) == []
    assert (await _heard(client, session_id=chat.service)) == []


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_chat_the_assistant_has_not_opened_yet_is_its_starters_already(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    await _accounts(store, ALICE, JAN)
    act_as_assistant()
    service = await start_session(client, actor=KB_ACTOR)
    first = await _canvas(client, email=ALICE)
    started = await send_chat(
        client,
        workspace_id=first[0],
        chat_id=first[1],
        text="alice starts",
    )
    conversation = conversation_of(started)
    second = await _canvas(client, email=JAN)

    # No session carries the conversation yet, only the line that began it.
    posted = await send_chat(
        client,
        workspace_id=second[0],
        chat_id=second[1],
        text="jan joins in",
        conversation_id=conversation,
    )
    again = await _canvas(client, email=ALICE)
    mine = await send_chat(
        client,
        workspace_id=again[0],
        chat_id=again[1],
        text="alice goes on",
        conversation_id=conversation,
    )

    assert (posted.status_code, mine.status_code) == (403, 200)
    heard = await _heard(client, session_id=service)
    assert [(each["text"], each["source"]) for each in heard] == [
        ("alice starts", ALICE),
        ("alice goes on", ALICE),
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_forker_may_retry_their_own_fork_under_its_key(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    chat = await _original(client, store, starter=TEST_USER_EMAIL)
    workspace_id, chat_id = await _canvas(client, email=JAN)
    key = uuid.uuid4()

    first = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="jan forks",
        key=key,
        fork=chat.at(2),
    )
    retry = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="jan forks",
        key=key,
        fork=chat.at(2),
    )

    assert (first.status_code, retry.status_code) == (200, 200)
    assert len(await _heard(client, session_id=chat.service)) == 1


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_only_an_edge_the_forks_own_opener_added_counts_as_a_fork(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    chat = await _original(client, store, starter=TEST_USER_EMAIL)
    real, other = uuid.uuid4(), uuid.uuid4()
    _ = await _link_fork(client, store, original=chat, fork=real, at=1, starter=JAN)
    stranger = await open_science_chat(
        client,
        store,
        conversation_id=other,
        account=ALICE,
    )

    # A writer outside the chat adds the same edge from a chat that is not a fork of it.
    act_as_other_agent("writer")
    forged = await client.post(
        f"/api/edges/{stranger}/produced_by/{chat.session}",
        json={
            "actor": "jan",
            "labels": [FORK_EDGE_LABEL, fork_point_label(part=0, idx=2)],
        },
    )
    assert forged.status_code == 200, forged.text

    head = await _head(client, conversation=chat.conversation, email=JAN)
    unforked = await _head(client, conversation=other, email=JAN)
    assert head["forks"] == 1
    assert unforked["forked_from"] is None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_session_with_two_fork_edges_names_the_first_as_its_origin(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    first = await _original(client, store, starter=TEST_USER_EMAIL)
    older = uuid.uuid4()
    second = replace(
        first,
        conversation=older,
        session=await open_science_chat(client, store, conversation_id=older),
    )
    fork = uuid.uuid4()
    forked = await _link_fork(
        client,
        store,
        original=second,
        fork=fork,
        at=1,
        starter=JAN,
    )
    act_as_assistant("writer")
    again = await client.post(
        f"/api/edges/{forked}/produced_by/{first.session}",
        json={"actor": KB_ACTOR, "labels": [FORK_EDGE_LABEL]},
    )
    assert again.status_code == 200, again.text

    heads = [await _head(client, conversation=fork, email=JAN) for _ in range(3)]

    assert {each["forked_from"] for each in heads} == {str(second.conversation)}


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_session_another_key_opened_under_the_id_does_not_make_its_owner_the_starter(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    await _accounts(store, JAN)
    conversation = uuid.uuid4()
    # Jan's own session carries the id, and is older than the assistant's.
    act_as_other_agent("writer")
    _ = await start_session(
        client,
        actor="decoy",
        cli_session_id=chat_session_id(conversation),
        account=JAN,
    )
    _ = await open_science_chat(
        client,
        store,
        conversation_id=conversation,
        account=TEST_USER_EMAIL,
    )
    workspace_id, chat_id = await _canvas(client, email=JAN)

    posted = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="jan claims it",
        conversation_id=conversation,
    )

    assert posted.status_code == 403


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_writer_outside_the_chat_cannot_drain_its_session_or_the_assistants(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    chat = await _original(client, store, starter=TEST_USER_EMAIL)
    workspace_id, chat_id = await _canvas(client, email=ALICE)
    _ = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="alice forks",
        fork=chat.at(0),
    )

    act_as_other_agent("writer")
    taken = [
        (await drain(client, session_id=session)).status_code
        for session in (chat.session, chat.service)
    ]

    assert taken == [403, 403]
    [heard] = await _heard(client, session_id=chat.service)
    assert heard["text"] == "alice forks"


async def _fork(
    client: httpx2.AsyncClient,
    body: dict[str, str],
    point: dict[str, object],
    **extra: object,
) -> int:
    """Post a line that forks ``point``; return the status."""
    response = await client.post(
        "/api/chats",
        json={**body, "fork": point, **extra},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    return response.status_code


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _append(*records: AgentToAgentMessage | AssistantMessage) -> dict[str, object]:
    """Return the body that appends ``records`` as the first records of a part."""
    manifest = ManifestBody(
        name="chat.jsonl",
        metadata={},
        ir_id=uuid.uuid4(),
        format="sagent",
        records=len(records),
    )
    return {
        "name": "chat.jsonl",
        "manifest": manifest.model_dump(mode="json"),
        "records": [
            RecordBody.of(
                SessionRecordRow.of(
                    session_id=uuid.UUID(int=0),
                    part=0,
                    idx=idx,
                    record=record,
                ),
            ).model_dump(mode="json")
            for idx, record in enumerate(records)
        ],
    }


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
