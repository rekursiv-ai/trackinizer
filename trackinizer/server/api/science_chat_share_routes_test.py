"""Private science chats over the app: who posts, who shares, who deletes."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import uuid

from treekle import from_plain, immutable, json_loads

import pytest

from trackinizer.lib.agent.types.sessions import (
    AgentToAgentMessage,
    AssistantMessage,
    ToolCall,
    UncategorizedToolResult,
    UserMessage,
)
from trackinizer.runs.asks import Ask, Option, opened_record
from trackinizer.server.api.app import app
from trackinizer.server.api.canvas_test_support import (
    ASSISTANT,
    ASSISTANT_CONFIG,
    KB_ACTOR,
    KB_EMAIL,
    KB_USER_ID,
    OTHER_AGENT,
    OTHER_EMAIL,
    OTHER_USER_ID,
    USER_AGENT,
    Agent,
    act_as_assistant,
    act_as_other_agent,
    act_as_user_agent,
    browser,
    drain,
    open_workspace,
    seed_accounts,
    seed_converse,
    seed_science_chat,
    seed_session,
    send_chat,
)
from trackinizer.server.api.conftest import (
    TEST_USER_EMAIL,
    install_identity,
    make_test_identity,
)
from trackinizer.server.asks_test_support import write_records
from trackinizer.server.chat_hub import ChatHub, WorkspaceFrame
from trackinizer.server.inbound import InboundQueue
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.audience import OPEN, STAFF
from trackinizer.wire.wire_science_chat import (
    FORK_EDGE_LABEL,
    SCIENCE_CHAT_LABEL,
    chat_session_id,
    fork_point_label,
)
from trackinizer.wire.wire_session_ir import ManifestBody, RecordBody


if TYPE_CHECKING:
    from collections.abc import Sequence

    import httpx2

    from trackinizer.lib.agent.types.sessions import SessionRecord
    from trackinizer.server.store.core import Store


type _Client = tuple[httpx2.AsyncClient, Store]

INDIVIDUAL_CONFIG: Final = replace(ASSISTANT_CONFIG, chat_orgs="individual")
ADA: Final = TEST_USER_EMAIL
BOB: Final = OTHER_EMAIL
ROOT: Final = "root@example.com"
ROOT_ID: Final = uuid.UUID("77777777-7777-7777-7777-777777777777")


@pytest.fixture(autouse=True)
def individual_chats_served(monkeypatch: pytest.MonkeyPatch) -> ChatHub:
    """Serve with scout configured, each chat its starter's alone, and a fresh hub."""
    hub = ChatHub()
    monkeypatch.setattr(app.state, "config", INDIVIDUAL_CONFIG, raising=False)
    monkeypatch.setattr(app.state, "inbound", InboundQueue(), raising=False)
    monkeypatch.setattr(app.state, "hub", hub, raising=False)
    return hub


@dataclass(frozen=True, slots=True, kw_only=True)
class _Chat:
    """Ada's chat, the assistant's service session, and Ada's station that holds it."""

    conversation: uuid.UUID
    session: uuid.UUID
    service: uuid.UUID
    station: uuid.UUID


async def _ada_chat(client: httpx2.AsyncClient, store: Store) -> _Chat:
    """Have Ada start a chat from her station, and the assistant open and record it."""
    await seed_accounts(store)
    service = await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    station = await open_workspace(client)
    session = await seed_converse(client, store, workspace_id=station)
    conversation = await _conversation_of(store, session)
    await _record(store, session, ADA)
    return _Chat(
        conversation=conversation,
        session=session,
        service=service,
        station=station,
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_colleagues_line_is_refused_until_the_chat_is_shared_with_them(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    chat = await _ada_chat(client, store)
    bob_station = await open_workspace(client, user_id=OTHER_USER_ID, email=BOB)
    before = await _head(client, chat.conversation, as_bob=True)

    refused = await _bob_says(client, chat, station=bob_station, text="may I?")
    browser()
    shared = await client.put(_share_path(chat, "Other-User@Example.com"))
    ada_head = await _head(client, chat.conversation)
    after = await _head(client, chat.conversation, as_bob=True)
    posted = await _bob_says(client, chat, station=bob_station, text="thanks")

    # Bob shares Ada's domain, which counts for nothing on an individual server.
    assert (before["forks_on_typing"], before["shared_with"]) == (True, [])
    assert _code(refused) == "chat_not_shared"
    assert json_loads(shared.content, list[str]) == [BOB]
    assert (ada_head["shared_with"], ada_head["may_share"]) == ([BOB], True)
    assert (after["forks_on_typing"], after["shared_with"], after["may_share"]) == (
        False,
        [],
        False,
    )
    assert posted.status_code == 200
    heard = await _heard(client, chat.session)
    assert [(each["text"], each["source"]) for each in heard] == [("thanks", BOB)]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_sharers_post_goes_by_the_chats_provider_not_their_stations(
    pglite_route_client: _Client,
) -> None:
    """Bob's station is on claude, but Ada's chat is the assistant's: it drains it."""
    client, store = pglite_route_client
    chat = await _ada_chat(client, store)
    bob_station = await open_workspace(client, user_id=OTHER_USER_ID, email=BOB)
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE visual_workspaces SET state = state || "
            "jsonb_build_object('provider', 'claude') WHERE id = $1",
            bob_station,
        )
    browser()
    _ = await client.put(_share_path(chat, BOB))

    browser(OTHER_USER_ID, email=BOB)
    command = await client.post(
        "/api/chats",
        json={
            "workspace_id": str(bob_station),
            "conversation_id": str(chat.conversation),
            "text": "/compact",
            "kind": "command",
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    posted = await _bob_says(client, chat, station=bob_station, text="thanks")

    assert command.status_code == 422, command.text
    assert "The assistant takes a person's line or an answer, not a command" in (
        command.text
    )
    assert posted.status_code == 200
    heard = await _heard(client, chat.session)
    assert [(each["text"], each["kind"]) for each in heard] == [("thanks", "line")]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_only_the_starter_answers_a_question_card_of_the_assistants_chat(
    pglite_route_client: _Client,
) -> None:
    """A sharer reads and posts, but the chat's cards are its starter's, as a run's."""
    client, store = pglite_route_client
    chat = await _ada_chat(client, store)
    bob_station = await open_workspace(client, user_id=OTHER_USER_ID, email=BOB)
    browser()
    _ = await client.put(_share_path(chat, BOB))
    answer: dict[str, object] = {"prompt_id": "q1", "option": "1"}

    def post(station: uuid.UUID) -> dict[str, object]:
        return {
            "workspace_id": str(station),
            "conversation_id": str(chat.conversation),
            "text": "Blue",
            "kind": "answer",
            "payload": answer,
        }

    browser(OTHER_USER_ID, email=BOB)
    refused = await client.post(
        "/api/chats",
        json=post(bob_station),
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    browser()
    answered = await client.post(
        "/api/chats",
        json=post(chat.station),
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )

    assert refused.status_code == 403, refused.text
    assert json_loads(refused.content, dict[str, object])["detail"] == {
        "code": "answer_not_owner",
        "message": f"Only {ADA} can answer this chat's questions.",
    }
    assert answered.status_code == 200, answered.text
    heard = await _heard(client, chat.session)
    assert [(each["text"], each["kind"], each["payload"]) for each in heard] == [
        ("Blue", "answer", answer),
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_card_the_assistant_asked_a_sharer_is_theirs_to_answer(
    pglite_route_client: _Client,
) -> None:
    """The card lists the asker's own choices, so their click alone is heard."""
    client, store = pglite_route_client
    chat = await _ada_chat(client, store)
    bob_station = await open_workspace(client, user_id=OTHER_USER_ID, email=BOB)
    browser()
    _ = await client.put(_share_path(chat, BOB))
    asked = Ask(
        id="q2",
        provider="assistant",
        kind="question",
        input={"asker": BOB.upper()},
        options=(Option(id="1", label="gx10", kind="allow_once"),),
    )
    await write_records(
        store,
        chat.session,
        [opened_record(asked, at=datetime.now(UTC))],
        name="cards.jsonl",
    )
    answer: dict[str, object] = {"prompt_id": "q2", "option": "1"}

    def post(station: uuid.UUID) -> dict[str, object]:
        return {
            "workspace_id": str(station),
            "conversation_id": str(chat.conversation),
            "text": "gx10",
            "kind": "answer",
            "payload": answer,
        }

    refused = await client.post(
        "/api/chats",
        json=post(chat.station),
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    browser(OTHER_USER_ID, email=BOB)
    answered = await client.post(
        "/api/chats",
        json=post(bob_station),
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )

    assert refused.status_code == 403, refused.text
    assert json_loads(refused.content, dict[str, object])["detail"] == {
        "code": "answer_not_owner",
        "message": f"Only {BOB.upper()} can answer this question.",
    }
    assert answered.status_code == 200, answered.text
    heard = await _heard(client, chat.session)
    assert [(each["text"], each["source"]) for each in heard] == [("gx10", BOB)]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_unsharing_stops_the_next_line_and_keeps_the_ones_before(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    chat = await _ada_chat(client, store)
    bob_station = await open_workspace(client, user_id=OTHER_USER_ID, email=BOB)
    browser()
    _ = await client.put(_share_path(chat, BOB))
    await _record(store, chat.session, BOB)

    unshared = await client.delete(_share_path(chat, BOB))
    refused = await _bob_says(client, chat, station=bob_station, text="still here?")

    assert json_loads(unshared.content, list[str]) == []
    assert _code(refused) == "chat_not_shared"
    assert await _senders(store, chat.session) == [ADA, BOB]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_only_the_starter_shares_a_chat_and_an_admin_only_stops_sharing_it(
    pglite_route_client: _Client,
) -> None:
    """A share lets its person read the chat, so an admin cannot grant one, itself."""
    client, store = pglite_route_client
    chat = await _ada_chat(client, store)

    browser(OTHER_USER_ID, email=BOB)
    stranger = await client.put(_share_path(chat, "jan@other.org"))
    _act_as_root()
    by_admin = await client.put(_share_path(chat, ROOT))
    admin_head = await client.get(f"/api/chats/{chat.conversation}")
    browser()
    shared = await client.put(_share_path(chat, "jan@other.org"))
    again = await client.put(_share_path(chat, "Jan@Other.org"))
    second = await client.put(_share_path(chat, BOB))
    _act_as_root()
    unshared_by_admin = await client.delete(_share_path(chat, BOB))
    browser()
    not_shared = await client.delete(_share_path(chat, "nobody@example.com"))
    unknown = await client.put(f"/api/chats/{uuid.uuid4()}/shares/{BOB}")
    malformed = await client.put(_share_path(chat, "not-an-email"))

    assert (_code(stranger), _code(by_admin)) == (
        "chat_not_starter",
        "chat_not_starter",
    )
    assert json_loads(admin_head.content, dict[str, object])["may_share"] is False
    assert json_loads(shared.content, list[str]) == ["jan@other.org"]
    assert json_loads(again.content, list[str]) == ["jan@other.org"]
    assert json_loads(second.content, list[str]) == ["jan@other.org", BOB]
    assert json_loads(unshared_by_admin.content, list[str]) == ["jan@other.org"]
    assert json_loads(not_shared.content, list[str]) == ["jan@other.org"]
    assert (unknown.status_code, malformed.status_code) == (404, 422)
    async with store.engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT email, shared_by FROM science_chat_shares "
            "ORDER BY shared_at, email",
        )
    assert [(row["email"], row["shared_by"]) for row in rows] == [
        ("jan@other.org", ADA),
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_starter_deletes_a_chat_only_they_posted_in_and_nothing_of_it_stays(
    pglite_route_client: _Client,
    individual_chats_served: ChatHub,
) -> None:
    client, store = pglite_route_client
    chat = await _ada_chat(client, store)
    browser()
    _ = await client.put(_share_path(chat, BOB))
    browser(OTHER_USER_ID, email=BOB)
    bound = await client.post(f"/api/workspaces/for-chat/{chat.conversation}")
    bob_station = uuid.UUID(
        from_plain(json_loads(bound.content, dict[str, object])["id"], str),
    )
    head = await _head(client, chat.conversation)

    with (
        individual_chats_served.subscribe(chat.station) as ada_tab,
        individual_chats_served.subscribe(bob_station) as bob_tab,
    ):
        browser()
        deleted = await client.delete(f"/api/chats/{chat.conversation}")
        frames = [ada_tab.queue.get_nowait(), bob_tab.queue.get_nowait()]
    gone = await client.get(f"/api/chats/{chat.conversation}")

    assert (head["may_delete"], head["may_share"]) == (True, True)
    assert (deleted.status_code, deleted.content) == (204, b"")
    assert _code(gone, status=410) == "chat_deleted"
    assert await _remains(store, chat) == (0, 0, 0, 0)
    assert [
        (f.state.id, f.state.conversation_id)
        for f in frames
        if isinstance(f, WorkspaceFrame)
    ] == [(chat.station, None), (bob_station, None)]
    # The assistant's service session is no part of the chat.
    async with store.engine.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM inquiries WHERE id = $1",
            chat.service,
        )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_each_station_a_deleted_chat_leaves_moves_to_a_new_revision(
    pglite_route_client: _Client,
    individual_chats_served: ChatHub,
) -> None:
    """A tab orders canvas frames by revision, so an unchanged one would be ignored."""
    client, store = pglite_route_client
    chat = await _ada_chat(client, store)
    other = await open_workspace(client, new=True)
    before = await _revisions(store, chat.station, other)

    with individual_chats_served.subscribe(chat.station) as tab:
        browser()
        deleted = await client.delete(f"/api/chats/{chat.conversation}")
        frame = tab.queue.get_nowait()
    after = await _revisions(store, chat.station, other)

    assert deleted.status_code == 204
    assert isinstance(frame, WorkspaceFrame)
    assert after == [before[0] + 1, before[1]]
    assert frame.state.revision == after[0]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_once_someone_else_posted_only_an_admin_or_its_key_deletes_the_chat(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    chat = await _ada_chat(client, store)
    await _record(store, chat.session, BOB)

    browser()
    head = await _head(client, chat.conversation)
    starter = await client.delete(f"/api/chats/{chat.conversation}")
    browser(OTHER_USER_ID, email=BOB)
    poster = await client.delete(f"/api/chats/{chat.conversation}")
    _act_as_root()
    admin = await client.delete(f"/api/chats/{chat.conversation}")
    again = await client.delete(f"/api/chats/{chat.conversation}")

    assert head["may_delete"] is False
    assert [_code(r) for r in (starter, poster)] == [
        "chat_has_other_posters",
        "chat_not_starter",
    ]
    assert (admin.status_code, again.status_code) == (204, 404)
    assert await _remains(store, chat) == (0, 0, 0, 0)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    ("extra", "may_delete"),
    [
        ({"$inbound": {"source": BOB, "room": None}}, False),
        ({"author": BOB}, False),
        ({"$inbound": {"source": ADA, "room": None}}, True),
        ({}, True),
    ],
    ids=["stamped-bob", "authored-bob", "stamped-ada", "typed"],
)
async def test_a_cli_user_turn_naming_another_sender_is_their_post(
    pglite_route_client: _Client,
    extra: dict[str, object],
    may_delete: bool,
) -> None:
    """A CLI part's user turn names its sender by the agent runner's stamp or an author."""
    client, store = pglite_route_client
    chat = await _ada_chat(client, store)
    turn = UserMessage(
        content="q",
        extra=immutable(extra),
        timestamp=datetime.now(UTC).isoformat(),
    )
    await _append(store, chat.session, [turn])

    head = await _head(client, chat.conversation)

    assert head["may_delete"] is may_delete


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_session_its_starter_may_not_read_counts_as_someone_elses_line(
    pglite_route_client: _Client,
) -> None:
    """Lines are read only off sessions the caller reads; a hidden one is not theirs."""
    client, store = pglite_route_client
    chat = await _ada_chat(client, store)
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET agentsession_audience = 'staff' WHERE id = $1",
            chat.session,
        )

    browser()
    unseen = await client.delete(f"/api/chats/{chat.conversation}")
    install_identity(
        replace(
            make_test_identity(api_key_id=None),
            read_audiences=frozenset({OPEN, STAFF}),
        ),
    )
    seen = await client.delete(f"/api/chats/{chat.conversation}")

    assert _code(unseen) == "chat_has_other_posters"
    assert seen.status_code == 204


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_starters_lines_and_the_assistants_tools_are_no_one_elses(
    pglite_route_client: _Client,
) -> None:
    """Her own line in other case, and a tool call's result, leave her the delete."""
    client, store = pglite_route_client
    chat = await _ada_chat(client, store)
    await _record(store, chat.session, ADA.upper())
    await _record_tools(store, chat.session)

    browser()
    deleted = await client.delete(f"/api/chats/{chat.conversation}")

    assert deleted.status_code == 204


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_another_posters_line_in_an_older_session_of_the_chat_counts(
    pglite_route_client: _Client,
) -> None:
    """A rotated key opens a newer head; Bob's line in the older session still counts."""
    client, store = pglite_route_client
    chat = await _ada_chat(client, store)
    newer = await seed_session(
        store,
        ASSISTANT,
        actor="chat-rotated",
        cli_session_id=chat_session_id(chat.conversation),
        account=ADA,
        labels=[SCIENCE_CHAT_LABEL],
        polled=False,
    )
    await _record(store, newer, ADA)
    await _record(store, chat.session, BOB)

    head = await _head(client, chat.conversation)
    deleted = await client.delete(f"/api/chats/{chat.conversation}")

    assert head["session_id"] == str(newer)
    assert _code(deleted) == "chat_has_other_posters"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("helper", [False, True], ids=["assistant-chat", "helper-chat"])
async def test_a_staff_session_planted_under_a_chats_id_does_not_block_its_delete(
    pglite_route_client: _Client,
    *,
    helper: bool,
) -> None:
    """A writer cannot make the starter's delete need an admin by a hidden session."""
    client, store = pglite_route_client
    if helper:
        await seed_accounts(store)
        conversation = uuid.uuid4()
        _ = await _helper_chat(store, conversation)
    else:
        conversation = (await _ada_chat(client, store)).conversation
    planted = await seed_session(
        store,
        OTHER_AGENT,
        actor="bob-plant",
        cli_session_id=chat_session_id(conversation),
        labels=[SCIENCE_CHAT_LABEL],
        polled=False,
    )
    await _record(store, planted, BOB)
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET agentsession_audience = 'staff' WHERE id = $1",
            planted,
        )

    browser()
    deleted = await client.delete(f"/api/chats/{conversation}")

    assert deleted.status_code == 204
    assert await _sessions_under(store, conversation) == 0


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_chats_shares_are_listed_oldest_share_first(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    chat = await _ada_chat(client, store)

    browser()
    _ = await client.put(_share_path(chat, "zed@example.org"))
    shared = await client.put(_share_path(chat, "amy@example.org"))

    assert shared.json() == ["zed@example.org", "amy@example.org"]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_key_that_opened_every_session_of_a_chat_deletes_it(
    pglite_route_client: _Client,
) -> None:
    """Every session the delete would take, a stranger's under the id included."""
    client, store = pglite_route_client
    chat = await _ada_chat(client, store)
    await _record(store, chat.session, BOB)
    squat = await seed_session(
        store,
        OTHER_AGENT,
        actor="bob-squat",
        cli_session_id=chat_session_id(chat.conversation),
        labels=[SCIENCE_CHAT_LABEL],
    )

    act_as_assistant("writer")
    refused = await client.delete(f"/api/chats/{chat.conversation}")
    async with store.engine.acquire() as conn:
        await conn.execute("DELETE FROM inquiries WHERE id = $1", squat)
    deleted = await client.delete(f"/api/chats/{chat.conversation}")

    assert refused.status_code == 403
    assert deleted.status_code == 204
    assert await _remains(store, chat) == (0, 0, 0, 0)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_starters_own_api_key_neither_shares_nor_deletes_the_chat(
    pglite_route_client: _Client,
) -> None:
    """An agent holding the starter's key acts unseen; a person's browser decides."""
    client, store = pglite_route_client
    chat = await _ada_chat(client, store)

    act_as_user_agent("writer")
    shared = await client.put(_share_path(chat, "mallory@example.org"))
    deleted = await client.delete(f"/api/chats/{chat.conversation}")

    assert (shared.status_code, deleted.status_code) == (403, 403)
    assert (await _remains(store, chat))[:3] == (1, 2, 0)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_line_a_fork_copied_counts_as_its_original_posters(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    fork = uuid.uuid4()
    session = await seed_science_chat(store, conversation_id=fork, account=ADA)
    # The fork opens with Bob's line from the chat Ada forked, then Ada's own.
    await _record(store, session, BOB, ADA)

    browser()
    refused = await client.delete(f"/api/chats/{fork}")

    assert _code(refused) == "chat_has_other_posters"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_deleted_chats_forks_still_open_and_name_no_origin(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    chat = await _ada_chat(client, store)
    fork = uuid.uuid4()
    fork_session = await seed_science_chat(store, conversation_id=fork, account=BOB)
    await _record(store, fork_session, ADA, BOB)
    _, created = await store.add_edge(
        from_id=fork_session,
        to_id=chat.session,
        edge_kind="produced_by",
        labels=[FORK_EDGE_LABEL, fork_point_label(part=0, idx=0)],
        api_key_id=ASSISTANT.key_id,
        actor=KB_ACTOR,
    )
    assert created
    before = await _head(client, fork)

    browser()
    deleted = await client.delete(f"/api/chats/{chat.conversation}")
    after = await _head(client, fork)

    assert before["forked_from"] == str(chat.conversation)
    assert deleted.status_code == 204
    assert after["forked_from"] is None
    assert await _senders(store, fork_session) == [ADA, BOB]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_deleted_chats_id_loses_every_session_and_starts_no_chat_again(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session anyone's key opened under the id goes too, and no line reopens it."""
    client, store = pglite_route_client
    chat = await _ada_chat(client, store)
    _ = await seed_session(
        store,
        OTHER_AGENT,
        actor="bob-squat",
        cli_session_id=chat_session_id(chat.conversation),
        labels=[SCIENCE_CHAT_LABEL],
    )
    bobs = await seed_science_chat(store, conversation_id=uuid.uuid4(), account=BOB)
    await _record(store, bobs, BOB)
    browser()
    deleted = await client.delete(f"/api/chats/{chat.conversation}")
    # A restart forgets which key sent each line, so only the tombstone remembers.
    inbound = InboundQueue()
    inbound.mark_poller(chat.service)
    monkeypatch.setattr(app.state, "inbound", inbound)
    bob_station = await open_workspace(client, user_id=OTHER_USER_ID, email=BOB)

    squat = await _bob_says(client, chat, station=bob_station, text="mine now")
    fork = await send_chat(
        client,
        workspace_id=bob_station,
        text="from mine",
        key=chat.conversation,
        fork={"session_id": str(bobs), "part": 0, "idx": 0},
    )
    browser()
    again = await send_chat(
        client,
        workspace_id=await open_workspace(client, new=True),
        text="back",
        conversation_id=chat.conversation,
    )

    assert deleted.status_code == 204
    assert (await _remains(store, chat))[0] == 0
    assert [_code(r, status=410) for r in (squat, fork, again)] == ["chat_deleted"] * 3


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_line_on_its_way_into_a_chat_counts_as_posted_and_goes_with_it(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another person's unread line stops the starter's delete; the starter's goes."""
    client, store = pglite_route_client
    inbound = InboundQueue()
    monkeypatch.setattr(app.state, "inbound", inbound)
    chat = await _ada_chat(client, store)
    browser()
    _ = await client.put(_share_path(chat, BOB))
    bob_station = await open_workspace(client, user_id=OTHER_USER_ID, email=BOB)
    bobs = await _bob_says(client, chat, station=bob_station, text="on its way")
    browser()
    refused = await client.delete(f"/api/chats/{chat.conversation}")
    _ = inbound.drain(chat.session)
    _ = inbound.drain(chat.service)
    adas = await send_chat(
        client,
        workspace_id=chat.station,
        text="never mind",
        conversation_id=chat.conversation,
    )
    queued = inbound.conversation_senders(chat.conversation)
    deleted = await client.delete(f"/api/chats/{chat.conversation}")

    assert (bobs.status_code, adas.status_code) == (200, 200)
    assert _code(refused) == "chat_has_other_posters"
    assert (queued, deleted.status_code) == ([ADA], 204)
    assert inbound.conversation_senders(chat.conversation) == []


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_deleted_chat_leaves_nothing_to_search(
    pglite_route_client: _Client,
) -> None:
    """Its records, and every index search reads them by, go with its sessions."""
    client, store = pglite_route_client
    chat = await _ada_chat(client, store)
    await _index(store, chat.session)
    before = await _indexed(store, chat.session)

    browser()
    deleted = await client.delete(f"/api/chats/{chat.conversation}")

    assert before == {
        "records": 2,
        "manifests": 1,
        "bodies": 1,
        "embeddings": 1,
        "indexed": 1,
        "ciphertext": 1,
        "title_embeddings": 1,
    }
    assert deleted.status_code == 204
    assert await _indexed(store, chat.session) == dict.fromkeys(before, 0)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_line_the_assistant_took_counts_as_posted_until_it_is_recorded(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Between its drain and its record, Bob's line is in no queue and no session."""
    client, store = pglite_route_client
    inbound = InboundQueue()
    monkeypatch.setattr(app.state, "inbound", inbound)
    chat = await _ada_chat(client, store)
    browser()
    _ = await client.put(_share_path(chat, BOB))
    bob_station = await open_workspace(client, user_id=OTHER_USER_ID, email=BOB)
    bobs = await _bob_says(client, chat, station=bob_station, text="taken")
    taken = [*await _heard(client, chat.session), *await _heard(client, chat.service)]

    browser()
    refused = await client.delete(f"/api/chats/{chat.conversation}")
    act_as_assistant("writer")
    recorded = await client.post(
        f"/api/sessions/{chat.session}/records",
        json=_line_of(chat.session, BOB),
    )

    assert (
        bobs.status_code,
        sorted(from_plain(line["source"], str) for line in taken),
    ) == (
        200,
        sorted([ADA, BOB]),
    )
    assert _code(refused) == "chat_has_other_posters"
    assert recorded.status_code == 200
    # Ada's own first line was recorded straight into the store, not appended.
    assert inbound.conversation_senders(chat.conversation) == [ADA]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_only_a_session_the_chat_counts_records_a_line_the_assistant_took(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bob's line written into Bob's own squat stays on its way; into Ada's, it lands."""
    client, store = pglite_route_client
    inbound = InboundQueue()
    monkeypatch.setattr(app.state, "inbound", inbound)
    chat = await _ada_chat(client, store)
    browser()
    _ = await client.put(_share_path(chat, BOB))
    bob_station = await open_workspace(client, user_id=OTHER_USER_ID, email=BOB)
    _ = await _bob_says(client, chat, station=bob_station, text="taken")
    _ = [*await _heard(client, chat.session), *await _heard(client, chat.service)]
    squat, helper = [
        await seed_session(
            store,
            agent,
            actor=actor,
            cli_session_id=chat_session_id(chat.conversation),
        )
        for agent, actor in ((OTHER_AGENT, "bob-squat"), (USER_AGENT, "ada-helper"))
    ]

    act_as_other_agent("writer")
    into_squat = await client.post(
        f"/api/sessions/{squat}/records",
        json=_line_of(squat, BOB),
    )
    after_squat = sorted(map(str, inbound.conversation_senders(chat.conversation)))
    browser()
    refused = await client.delete(f"/api/chats/{chat.conversation}")
    act_as_user_agent("writer")
    into_helper = await client.post(
        f"/api/sessions/{helper}/records",
        json=_line_of(helper, BOB),
    )

    assert (into_squat.status_code, into_helper.status_code) == (200, 200)
    assert after_squat == sorted([ADA, BOB])
    assert _code(refused) == "chat_has_other_posters"
    assert inbound.conversation_senders(chat.conversation) == [ADA]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_deleted_chats_id_and_lines_open_no_session_station_or_fork(
    pglite_route_client: _Client,
) -> None:
    """Its head, a station for it, a session under its id, a fork from its line."""
    client, store = pglite_route_client
    chat = await _ada_chat(client, store)
    browser()
    deleted = await client.delete(f"/api/chats/{chat.conversation}")

    head = await client.get(f"/api/chats/{chat.conversation}")
    station = await client.post(f"/api/workspaces/for-chat/{chat.conversation}")
    fork = await send_chat(
        client,
        workspace_id=chat.station,
        text="from a deleted line",
        fork={"session_id": str(chat.session), "part": 0, "idx": 0},
    )
    act_as_assistant("writer")
    reopened = await client.post(
        "/api/sessions/start",
        json={
            "cli": "sagent",
            "cli_session_id": chat_session_id(chat.conversation),
            "account": ADA,
            "actor": "chat-reopened",
        },
    )
    appended = await client.post(
        f"/api/sessions/{chat.session}/records",
        json={"records": []},
    )

    assert deleted.status_code == 204
    assert [_code(r, status=410) for r in (head, station, fork, reopened)] == [
        "chat_deleted",
    ] * 4
    assert appended.status_code == 404
    assert await _sessions_under(store, chat.conversation) == 0


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_deleted_chats_change_log_keeps_its_rows_but_not_its_words(
    pglite_route_client: _Client,
) -> None:
    """Nothing enforces the change log as append-only, so the chat's words leave it."""
    client, store = pglite_route_client
    chat = await _ada_chat(client, store)
    act_as_assistant("writer")
    edits = [
        await client.put(
            f"/api/inquiries/{chat.session}/{field}",
            json={"value": "my results say X"},
        )
        for field in ("title", "description")
    ]

    browser()
    deleted = await client.delete(f"/api/chats/{chat.conversation}")
    async with store.engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT kind, old_title, new_title, old_description, new_description "
            "FROM change_log WHERE subject_id = $1 ORDER BY created, id",
            chat.session,
        )

    assert [edit.status_code for edit in edits] == [200, 200]
    assert deleted.status_code == 204
    assert [tuple(row) for row in rows] == [
        ("title", "", "", None, None),
        ("description", None, None, None, None),
        ("purged", None, None, None, None),
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_locked_chat_is_deleted_by_an_admin_alone(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    chat = await _ada_chat(client, store)
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET locked = TRUE WHERE id = $1",
            chat.session,
        )

    browser()
    starter = await client.delete(f"/api/chats/{chat.conversation}")
    _act_as_root()
    admin = await client.delete(f"/api/chats/{chat.conversation}")

    assert (starter.status_code, admin.status_code) == (403, 204)
    assert "is locked" in json_loads(starter.content, dict[str, str])["detail"]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_admin_purges_a_chats_session_by_id_and_another_writer_does_not(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    chat = await _ada_chat(client, store)
    # A session's routing name is its owner, which a purge first needs released.
    act_as_assistant("writer")
    released = await client.request(
        "DELETE",
        f"/api/inquiries/{chat.session}/owner",
        json={},
    )
    assert released.status_code == 200, released.text

    browser()
    starter = await client.request("DELETE", f"/api/inquiries/{chat.session}", json={})
    _act_as_root()
    admin = await client.request("DELETE", f"/api/inquiries/{chat.session}", json={})

    assert (starter.status_code, admin.status_code) == (403, 200)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_share_lets_another_organisation_continue_a_domain_servers_chat(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app.state,
        "config",
        replace(ASSISTANT_CONFIG, chat_orgs="domain"),
        raising=False,
    )
    client, store = pglite_route_client
    chat = await _ada_chat(client, store)
    jan = "jan@other.org"
    station = await open_workspace(client, user_id=OTHER_USER_ID, email=jan)

    refused = await _bob_says(client, chat, station=station, text="hi", email=jan)
    browser()
    _ = await client.put(_share_path(chat, jan))
    posted = await _bob_says(client, chat, station=station, text="hi", email=jan)

    assert (refused.status_code, posted.status_code) == (403, 200)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_session_a_writers_own_key_opens_under_a_chats_id_gives_them_nothing(
    pglite_route_client: _Client,
) -> None:
    """Any writer's key may open a session under a ``chat:`` id; it starts nothing."""
    client, store = pglite_route_client
    chat = await _ada_chat(client, store)
    bob_station = await open_workspace(client, user_id=OTHER_USER_ID, email=BOB)
    _ = await seed_session(
        store,
        OTHER_AGENT,
        actor="bob-squat",
        cli_session_id=chat_session_id(chat.conversation),
        labels=[SCIENCE_CHAT_LABEL],
    )

    bobs = await _head(client, chat.conversation, as_bob=True)
    share = await client.put(_share_path(chat, BOB))
    delete = await client.delete(f"/api/chats/{chat.conversation}")
    post = await _bob_says(client, chat, station=bob_station, text="mine now")
    adas = await _head(client, chat.conversation)

    assert {key: bobs[key] for key in ("account", "may_share", "may_delete")} == {
        "account": ADA,
        "may_share": False,
        "may_delete": False,
    }
    assert [_code(share), _code(delete), _code(post)] == [
        "chat_not_starter",
        "chat_not_starter",
        "chat_not_shared",
    ]
    assert (adas["session_id"], adas["account"]) == (str(chat.session), ADA)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_strangers_session_under_a_helper_chats_id_neither_finds_nor_keeps_it(
    pglite_route_client: _Client,
) -> None:
    """Only its starter finds a helper's chat; a stranger's session goes with it."""
    client, store = pglite_route_client
    await seed_accounts(store)
    conversation = uuid.uuid4()
    _ = await _helper_chat(store, conversation)
    squat = await seed_session(
        store,
        OTHER_AGENT,
        actor="bob-squat",
        cli_session_id=chat_session_id(conversation),
        labels=[SCIENCE_CHAT_LABEL],
    )
    await _record(store, squat, BOB)

    browser(OTHER_USER_ID, email=BOB)
    by_browser = await client.delete(f"/api/chats/{conversation}")
    act_as_other_agent("writer")
    by_key = await client.delete(f"/api/chats/{conversation}")
    kept = await _sessions_under(store, conversation)
    browser()
    by_starter = await client.delete(f"/api/chats/{conversation}")

    assert (by_browser.status_code, by_key.status_code, kept) == (404, 404, 2)
    assert by_starter.status_code == 204
    assert await _sessions_under(store, conversation) == 0


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_chat_its_starters_key_began_stays_theirs_whoever_the_assistant_serves(
    pglite_route_client: _Client,
) -> None:
    """Ada's own key's session began it; one the assistant opens later for Bob does not."""
    client, store = pglite_route_client
    await seed_accounts(store)
    _ = await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    conversation = uuid.uuid4()
    _ = await _helper_chat(store, conversation)
    browser()
    shared = await client.put(f"/api/chats/{conversation}/shares/{BOB}")
    served = await seed_session(
        store,
        ASSISTANT,
        actor=f"chat-{conversation.hex[:12]}",
        cli_session_id=chat_session_id(conversation),
        account=BOB,
        labels=[SCIENCE_CHAT_LABEL],
    )
    await _record(store, served, BOB)

    bobs = await _head(client, conversation, as_bob=True)
    share = await client.put(f"/api/chats/{conversation}/shares/carol@example.org")
    delete = await client.delete(f"/api/chats/{conversation}")

    assert shared.status_code == 200
    assert (bobs["account"], bobs["may_share"], bobs["may_delete"]) == (
        ADA,
        False,
        False,
    )
    assert [_code(share), _code(delete)] == ["chat_not_starter"] * 2
    assert await _sessions_under(store, conversation) == 2


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_no_session_is_moved_under_a_chats_id_to_go_with_its_delete(
    pglite_route_client: _Client,
) -> None:
    """Bob cannot put Ada's run under his chat's id, nor rename it at all."""
    client, store = pglite_route_client
    await seed_accounts(store)
    conversation = uuid.uuid4()
    bobs = await seed_science_chat(store, conversation_id=conversation, account=BOB)
    await _record(store, bobs, BOB)
    adas = await seed_session(
        store,
        USER_AGENT,
        actor="ada-run",
        cli="claude",
        cli_session_id="claude-abc",
        polled=False,
    )

    browser(OTHER_USER_ID, email=BOB)
    moved = await client.put(
        f"/api/agentsession/{adas}/cli_session_id",
        json={"value": chat_session_id(conversation)},
    )
    renamed = await client.put(
        f"/api/agentsession/{adas}/cli_session_id",
        json={"value": "claude-def"},
    )
    deleted = await client.delete(f"/api/chats/{conversation}")
    act_as_user_agent("writer")
    own = await client.put(
        f"/api/agentsession/{adas}/cli_session_id",
        json={"value": "claude-def"},
    )

    assert (moved.status_code, renamed.status_code) == (403, 403)
    assert (deleted.status_code, own.status_code) == (204, 200)
    async with store.engine.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT agentsession_cli_session_id FROM inquiries WHERE id = $1",
                adas,
            )
            == "claude-def"
        )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_no_one_names_a_chat_id_by_an_edit_not_even_an_admin(
    pglite_route_client: _Client,
) -> None:
    """A chat is named when its session starts: an edit never moves a session in or out."""
    client, store = pglite_route_client
    chat = await _ada_chat(client, store)
    other = uuid.uuid4()

    browser(OTHER_USER_ID, email=BOB)
    service = await client.put(
        f"/api/agentsession/{chat.service}/cli_session_id",
        json={"value": chat_session_id(chat.conversation)},
    )
    _act_as_root()
    into = await client.put(
        f"/api/agentsession/{chat.service}/cli_session_id",
        json={"value": chat_session_id(other)},
    )
    act_as_assistant("writer")
    out = await client.request(
        "DELETE",
        f"/api/agentsession/{chat.session}/cli_session_id",
        json={},
    )
    head = await _head(client, chat.conversation)

    assert [r.status_code for r in (service, into, out)] == [403, 403, 403]
    assert (head["session_id"], head["account"]) == (str(chat.session), ADA)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_labelled_squat_does_not_start_a_chat_whose_first_session_is_not(
    pglite_route_client: _Client,
) -> None:
    """The assistant labels a chat's session after starting it; a squat may be first."""
    client, store = pglite_route_client
    await seed_accounts(store)
    _ = await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    conversation = uuid.uuid4()
    adas = await seed_session(
        store,
        ASSISTANT,
        actor="chat-unlabelled",
        cli_session_id=chat_session_id(conversation),
        account=ADA,
        polled=False,
    )
    await _record(store, adas, ADA)
    _ = await seed_session(
        store,
        OTHER_AGENT,
        actor="bob-squat",
        cli_session_id=chat_session_id(conversation),
        labels=[SCIENCE_CHAT_LABEL],
        polled=False,
    )
    bob_station = await open_workspace(client, user_id=OTHER_USER_ID, email=BOB)

    browser(OTHER_USER_ID, email=BOB)
    shared = await client.put(f"/api/chats/{conversation}/shares/{BOB}")
    posted = await send_chat(
        client,
        workspace_id=bob_station,
        text="mine now",
        conversation_id=conversation,
    )

    assert shared.status_code == 404
    assert _code(posted) == "chat_not_shared"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_chats_starter_is_its_first_session_by_creation_not_by_started(
    pglite_route_client: _Client,
) -> None:
    """``started`` is written by a route; ``created``, which orders sessions, is not."""
    client, store = pglite_route_client
    chat = await _ada_chat(client, store)
    later = await seed_session(
        store,
        ASSISTANT,
        actor="chat-later",
        cli_session_id=chat_session_id(chat.conversation),
        account=BOB,
        labels=[SCIENCE_CHAT_LABEL],
        polled=False,
    )

    act_as_assistant("writer")
    backdated = await client.put(
        f"/api/agentsession/{later}/started",
        json={"value": "2001-01-01T00:00:00Z"},
    )
    head = await _head(client, chat.conversation, as_bob=True)

    assert backdated.status_code == 200
    assert (head["account"], head["may_share"]) == (ADA, False)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_rotated_assistant_key_does_not_make_its_first_poster_the_starter(
    pglite_route_client: _Client,
) -> None:
    """A new key's session is attributed to the first poster it hears, here Bob."""
    client, store = pglite_route_client
    chat = await _ada_chat(client, store)
    browser()
    _ = await client.put(_share_path(chat, BOB))
    rotated = Agent(key_id=uuid.uuid4(), email=KB_EMAIL)
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
            "VALUES ($1, $2, 'rotated', 'test-hash', 'trax_rot', 'writer')",
            rotated.key_id,
            KB_USER_ID,
        )
    _ = await seed_session(
        store,
        rotated,
        actor="chat-rotated",
        cli_session_id=chat_session_id(chat.conversation),
        account=BOB,
        labels=[SCIENCE_CHAT_LABEL],
    )

    adas = await _head(client, chat.conversation)
    bobs = await _head(client, chat.conversation, as_bob=True)
    unshare = await client.delete(_share_path(chat, BOB))

    assert (adas["account"], adas["may_share"]) == (ADA, True)
    assert (bobs["account"], bobs["may_share"]) == (ADA, False)
    assert _code(unshare) == "chat_not_starter"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_planted_idempotency_key_does_not_make_a_stranger_the_starter(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a restart the key map is empty, so any send can claim a chat's id."""
    client, store = pglite_route_client
    chat = await _ada_chat(client, store)
    bob_station = await open_workspace(client, user_id=OTHER_USER_ID, email=BOB)
    inbound = InboundQueue()
    monkeypatch.setattr(app.state, "inbound", inbound)
    for session in (chat.service, chat.session):
        inbound.mark_poller(session)
    bob_run = await seed_session(store, OTHER_AGENT, actor="bob-run")
    act_as_other_agent("writer")
    planted = await client.post(
        f"/api/sessions/{bob_run}/inbound",
        json={"text": "plant"},
        headers={"Idempotency-Key": str(chat.conversation)},
    )

    bob = await _bob_says(client, chat, station=bob_station, text="mine now")
    browser()
    ada = await send_chat(
        client,
        workspace_id=chat.station,
        text="still mine",
        conversation_id=chat.conversation,
    )

    assert planted.status_code == 200, planted.text
    assert inbound.sender_of(chat.conversation) == BOB
    assert _code(bob) == "chat_not_shared"
    assert ada.status_code == 200, ada.text


async def _bob_says(
    client: httpx2.AsyncClient,
    chat: _Chat,
    *,
    station: uuid.UUID,
    text: str,
    email: str = BOB,
) -> httpx2.Response:
    """Post a line into Ada's chat from the second user's station, as ``email``."""
    browser(OTHER_USER_ID, email=email)
    return await send_chat(
        client,
        workspace_id=station,
        text=text,
        conversation_id=chat.conversation,
    )


async def _head(
    client: httpx2.AsyncClient,
    conversation: uuid.UUID,
    *,
    as_bob: bool = False,
) -> dict[str, object]:
    """Read a chat's head as Ada, or as Bob."""
    if as_bob:
        browser(OTHER_USER_ID, email=BOB)
    else:
        browser()
    response = await client.get(f"/api/chats/{conversation}")
    assert response.status_code == 200, response.text
    return json_loads(response.content, dict[str, object])


async def _heard(
    client: httpx2.AsyncClient,
    session: uuid.UUID,
) -> list[dict[str, object]]:
    """Drain a session as the assistant; return its messages."""
    act_as_assistant()
    response = await drain(client, session_id=session)
    assert response.status_code == 200
    return from_plain(
        json_loads(response.content, dict[str, object])["messages"],
        list[dict[str, object]],
    )


def _line_of(session: uuid.UUID, sender: str) -> dict[str, object]:
    """Build the append that records one person's line, as the assistant sends it."""
    line = SessionRecordRow.of(
        session_id=session,
        part=0,
        idx=0,
        record=AgentToAgentMessage(
            sender=sender,
            content="taken",
            timestamp=datetime.now(UTC).isoformat(),
        ),
    )
    return {
        "name": "taken.jsonl",
        "manifest": ManifestBody(
            name="taken.jsonl",
            ir_id=uuid.uuid4(),
            format="sagent",
            records=1,
        ).model_dump(mode="json"),
        "records": [RecordBody.of(line).model_dump(mode="json")],
    }


async def _record(store: Store, session: uuid.UUID, *senders: str) -> None:
    """Record a person's line from each sender, each answered, as the assistant does."""
    lines: list[AgentToAgentMessage | AssistantMessage] = []
    for sender in senders:
        now = datetime.now(UTC).isoformat()
        lines += [
            AgentToAgentMessage(sender=sender, content="q", timestamp=now),
            AssistantMessage(content="a", timestamp=now),
        ]
    await _append(store, session, lines)


async def _record_tools(store: Store, session: uuid.UUID) -> None:
    """Record a tool call and its result, as the assistant does between its lines."""
    now = datetime.now(UTC).isoformat()
    await _append(
        store,
        session,
        [
            ToolCall(call_id="c1", name="ShowRecord", timestamp=now),
            UncategorizedToolResult(call_id="c1", content="Shown.", timestamp=now),
        ],
    )


async def _append(
    store: Store,
    session: uuid.UUID,
    lines: Sequence[SessionRecord],
) -> None:
    """Append records to a session in a part of their own."""
    part = await store.upsert_session_manifest(
        session,
        name=f"{uuid.uuid4()}.jsonl",
        metadata={},
        ir_id=uuid.uuid4(),
        format="sagent",
        records=len(lines),
    )
    written, _skipped, _slash = await store.append_session_records(
        session,
        [
            SessionRecordRow.of(session_id=session, part=part, idx=idx, record=record)
            for idx, record in enumerate(lines)
        ],
    )
    assert written == len(lines)


async def _helper_chat(store: Store, conversation: uuid.UUID) -> uuid.UUID:
    """Open Ada's chat as her own helper does, under her key, with a line of hers."""
    session = await seed_session(
        store,
        USER_AGENT,
        actor="ada-helper-chat",
        cli_session_id=chat_session_id(conversation),
        labels=[SCIENCE_CHAT_LABEL],
    )
    await _record(store, session, ADA)
    return session


async def _sessions_under(store: Store, conversation: uuid.UUID) -> int:
    """Count the sessions under a conversation's id, whoever opened them."""
    async with store.engine.acquire() as conn:
        return from_plain(
            await conn.fetchval(
                "SELECT count(*) FROM inquiries WHERE agentsession_cli_session_id = $1",
                chat_session_id(conversation),
            ),
            int,
        )


async def _senders(store: Store, session: uuid.UUID) -> list[str]:
    """Name the poster of each person's line in a session, in order."""
    async with store.engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT payload ->> 'sender' AS sender FROM session_records "
            "WHERE session_id = $1 AND kind = 'AgentToAgentMessage' ORDER BY part, idx",
            session,
        )
    return [from_plain(row["sender"], str) for row in rows]


async def _remains(store: Store, chat: _Chat) -> tuple[int, int, int, int]:
    """Count what a chat left: sessions, records, shares, and stations holding it."""
    async with store.engine.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT (SELECT count(*) FROM inquiries "
            "WHERE agentsession_cli_session_id = $1) AS sessions, "
            "(SELECT count(*) FROM session_records WHERE session_id = $2) AS records, "
            "(SELECT count(*) FROM science_chat_shares "
            "WHERE conversation_id = $3) AS shares, "
            "(SELECT count(*) FROM visual_workspaces "
            "WHERE station->>'conversation_id' = $3::text) AS stations",
            chat_session_id(chat.conversation),
            chat.session,
            chat.conversation,
        )
    assert row is not None
    return (
        from_plain(row["sessions"], int),
        from_plain(row["records"], int),
        from_plain(row["shares"], int),
        from_plain(row["stations"], int),
    )


async def _index(store: Store, session: uuid.UUID) -> None:
    """Give a session's first line a row in each table search reads it by."""
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO session_bodies (session_id, part, idx, payload_zst, text_zst) "
            "VALUES ($1, 0, 0, '\\x00', '\\x00')",
            session,
        )
        await conn.execute(
            "INSERT INTO session_embeddings (session_id, part, idx, field, mapper, "
            "model, embedding, text_md5) "
            "VALUES ($1, 0, 0, 'text', 'm', 'stub', '[1,0]'::halfvec, 'md5')",
            session,
        )
        await conn.execute(
            "INSERT INTO session_index_state (session_id, part, idx, mapper, model, "
            "text_md5) VALUES ($1, 0, 0, 'm', 'stub', 'md5')",
            session,
        )
        await conn.execute(
            "INSERT INTO session_ciphertext (session_id, idx, bytes) "
            "VALUES ($1, 0, '\\x00')",
            session,
        )
        await conn.execute(
            "INSERT INTO inquiry_embeddings (inquiry_id, model, embedding) "
            "VALUES ($1, 'stub', array_fill(0, ARRAY[384])::vector)",
            session,
        )


async def _indexed(store: Store, session: uuid.UUID) -> dict[str, int]:
    """Count a session's rows in each table that holds its words or an index of them."""
    async with store.engine.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT (SELECT count(*) FROM session_records WHERE session_id = $1) "
            "AS records, "
            "(SELECT count(*) FROM session_manifests WHERE session_id = $1) "
            "AS manifests, "
            "(SELECT count(*) FROM session_bodies WHERE session_id = $1) AS bodies, "
            "(SELECT count(*) FROM session_embeddings WHERE session_id = $1) "
            "AS embeddings, "
            "(SELECT count(*) FROM session_index_state WHERE session_id = $1) "
            "AS indexed, "
            "(SELECT count(*) FROM session_ciphertext WHERE session_id = $1) "
            "AS ciphertext, "
            "(SELECT count(*) FROM inquiry_embeddings WHERE inquiry_id = $1) "
            "AS title_embeddings",
            session,
        )
    assert row is not None
    return {key: from_plain(value, int) for key, value in dict(row).items()}


async def _revisions(store: Store, *stations: uuid.UUID) -> list[int]:
    """Read each station's revision, in the order named."""
    async with store.engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, revision FROM visual_workspaces WHERE id = ANY($1::uuid[])",
            list(stations),
        )
    held = {
        from_plain(row["id"], uuid.UUID): from_plain(row["revision"], int)
        for row in rows
    }
    return [held[station] for station in stations]


async def _conversation_of(store: Store, session: uuid.UUID) -> uuid.UUID:
    """Read the conversation a chat's session carries."""
    async with store.engine.acquire() as conn:
        named = await conn.fetchval(
            "SELECT agentsession_cli_session_id FROM inquiries WHERE id = $1",
            session,
        )
    return uuid.UUID(from_plain(named, str).removeprefix("chat:"))


def _share_path(chat: _Chat, email: str) -> str:
    return f"/api/chats/{chat.conversation}/shares/{email}"


def _code(response: httpx2.Response, *, status: int = 403) -> object:
    assert response.status_code == status, response.text
    return json_loads(response.content, dict[str, dict[str, object]])["detail"]["code"]


def _act_as_root() -> None:
    """Act as an admin's signed-in browser."""
    install_identity(
        make_test_identity(user_id=ROOT_ID, api_key_id=None, email=ROOT, role="admin"),
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
