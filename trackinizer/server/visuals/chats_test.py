"""Chat storage: lines, threads, replays, and the partner's replies."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, cast
from unittest.mock import AsyncMock

import asyncio
import uuid

import pytest

from trackinizer.conftest import make_store
from trackinizer.lib.codec import from_plain
from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.chat_hub import (
    ChatHub,
    HighlightFrame,
    MessageFrame,
    StatusFrame,
)
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.notify import tx
from trackinizer.server.store.core import Store
from trackinizer.server.visuals import chats
from trackinizer.server.visuals.chats import (
    NOT_DELIVERED,
    ChatConversationNotFoundError,
    ChatReplyForbiddenError,
    ChatRequestConflictError,
    add_message,
    conversations_of,
    delete_conversation,
    list_conversations,
    post_reply,
    read_partner_thread,
    read_thread,
    release_session,
    replay_of,
    start_or_continue,
)
from trackinizer.wire.wire_chats import ChatReply


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn, DatabaseEngine, PGliteEngine
    from trackinizer.server.chat_hub import Frame


_USER: Final = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OTHER: Final = uuid.UUID("22222222-2222-2222-2222-222222222222")
_KEY: Final = uuid.UUID("44444444-4444-4444-4444-444444444441")
_OTHER_KEY: Final = uuid.UUID("44444444-4444-4444-4444-444444444442")
_WORKSPACE: Final = uuid.UUID("55555555-5555-5555-5555-555555555555")
_CONVERSATION: Final = uuid.UUID("66666666-6666-6666-6666-666666666666")


@pytest.mark.asyncio
async def test_history_asks_for_the_users_newest_fifty_and_maps_the_rows() -> None:
    """The query is the user's, newest change first, bounded; rows become summaries."""
    _, engine = make_store()
    when = datetime(2026, 10, 3, tzinfo=UTC)
    conversation_id = uuid.uuid4()
    engine.conn.fetch = AsyncMock(
        return_value=[
            {
                "id": conversation_id,
                "title": "A chat",
                "partner_actor": None,
                "workspace_id": _WORKSPACE,
                "created": when,
                "modified": when,
            },
        ],
    )

    listed = await list_conversations(cast("DatabaseEngine", engine), user_id=_USER)

    assert [(s.id, s.title, s.partner_actor, s.created) for s in listed] == [
        (conversation_id, "A chat", None, when),
    ]
    args = _args(engine.conn.fetch)
    sql = from_plain(args[0], str)
    user_id, limit = args[1:]
    assert sql == (
        "SELECT id, title, partner_actor, workspace_id, "
        "created_at AS created, modified_at AS modified "
        "FROM chat_conversations WHERE user_id = $1 "
        "ORDER BY modified_at DESC, id DESC LIMIT $2"
    )
    assert (user_id, limit) == (_USER, 50)


_LINES_SQL: Final = "SELECT id, seq, role, author, text, created_at FROM chat_messages "


def _args(mock: AsyncMock) -> tuple[object, ...]:
    """Return the arguments of a mock's last await."""
    call = mock.await_args
    assert call is not None
    return call.args


def _line_rows(*seqs: int) -> list[dict[str, object]]:
    return [
        {
            "id": uuid.uuid4(),
            "seq": seq,
            "role": "user",
            "author": "u",
            "text": f"line {seq}",
            "created_at": datetime(2026, 10, 3, tzinfo=UTC),
        }
        for seq in seqs
    ]


def _fake_thread_engine(
    rows: list[dict[str, object]],
    *,
    older: bool,
) -> tuple[DatabaseEngine, AsyncMock]:
    _, engine = make_store()
    engine.conn.fetchrow = AsyncMock(
        return_value={
            "id": _CONVERSATION,
            "title": "T",
            "partner_actor": "agent",
            "partner_session_id": None,
        },
    )
    engine.conn.fetch = AsyncMock(return_value=rows)
    engine.conn.fetchval = AsyncMock(return_value=older)
    return cast("DatabaseEngine", engine), engine.conn


@pytest.mark.asyncio
async def test_without_a_cursor_a_thread_reads_the_newest_then_orders_them() -> None:
    """The query takes the newest 500; the thread lists them oldest first."""
    engine, conn = _fake_thread_engine(_line_rows(9, 8, 7), older=True)

    thread = await read_thread(
        engine,
        user_id=_USER,
        conversation_id=_CONVERSATION,
        after_seq=None,
    )

    assert thread is not None
    assert [m.seq for m in thread.messages] == [7, 8, 9]
    assert thread.earlier is True
    assert (thread.title, thread.partner_actor) == ("T", "agent")
    assert _args(conn.fetchrow) == (
        (
            "SELECT id, title, partner_actor, partner_session_id "
            "FROM chat_conversations WHERE id = $1 AND user_id = $2"
        ),
        _CONVERSATION,
        _USER,
    )
    sql, conversation, limit = _args(conn.fetch)
    assert sql == (f"{_LINES_SQL}WHERE conversation_id = $1 ORDER BY seq DESC LIMIT $2")
    assert (conversation, limit) == (_CONVERSATION, 500)
    exists = _args(conn.fetchval)
    assert from_plain(exists[0], str) == (
        "SELECT EXISTS (SELECT 1 FROM chat_messages "
        "WHERE conversation_id = $1 AND seq < $2)"
    )
    assert exists[1:] == (_CONVERSATION, 7)


@pytest.mark.asyncio
async def test_with_a_cursor_a_thread_reads_forward_and_empty_has_no_earlier() -> None:
    """The query is oldest first after the cursor; no rows means no gap to report."""
    engine, conn = _fake_thread_engine(_line_rows(6, 7), older=True)
    thread = await read_thread(
        engine,
        user_id=_USER,
        conversation_id=_CONVERSATION,
        after_seq=5,
    )
    assert thread is not None
    assert [m.seq for m in thread.messages] == [6, 7]
    sql, conversation, after, limit = _args(conn.fetch)
    assert sql == (
        f"{_LINES_SQL}WHERE conversation_id = $1 AND seq > $2 ORDER BY seq LIMIT $3"
    )
    assert (conversation, after, limit) == (_CONVERSATION, 5, 500)

    engine, conn = _fake_thread_engine([], older=True)
    empty = await read_thread(
        engine,
        user_id=_USER,
        conversation_id=_CONVERSATION,
        after_seq=5,
    )
    assert empty is not None
    assert (empty.messages, empty.earlier) == ([], False)
    conn.fetchval.assert_not_awaited()


async def _seed(engine: PGliteEngine) -> uuid.UUID:
    """Seed two users, a canvas and a partner session; return the session."""
    await reset_schema(engine)
    await Store(engine, embed=StubEmbedder()).bootstrap()
    async with engine.acquire() as conn:
        for user, email, key in (
            (_USER, "user@example.com", _KEY),
            (_OTHER, "other@example.com", _OTHER_KEY),
        ):
            await conn.execute(
                "INSERT INTO users (id, email, name, role, status) "
                "VALUES ($1, $2, 'T', 'writer', 'active')",
                user,
                email,
            )
            await conn.execute(
                "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
                "VALUES ($1, $2, 'k', 'h', $3, 'writer')",
                key,
                user,
                f"trax_{key.hex[-4:]}",
            )
        await conn.execute(
            "INSERT INTO visual_workspaces (id, user_id, state) VALUES ($1, $2, $3)",
            _WORKSPACE,
            _USER,
            {"visuals": []},
        )
        return await _session(conn, key=_KEY, seq=1)


async def _session(
    conn: Conn,
    *,
    key: uuid.UUID,
    seq: int,
    ended: bool = False,
) -> uuid.UUID:
    session_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO inquiries (id, kind, seq, status, account, title, owner, "
        "agentsession_opened_by_api_key_id, agentsession_cli, agentsession_ended) "
        "VALUES ($1, 'AgentSession', $2, $3, 'user@example.com', 'S', $4, $5, "
        "'codex', CASE WHEN $6 THEN clock_timestamp() END)",
        session_id,
        seq,
        "complete" if ended else "active",
        f"agent-{seq}",
        key,
        ended,
    )
    return session_id


async def _conversation(
    engine: PGliteEngine,
    *,
    session_id: uuid.UUID,
    text: str = "hello",
) -> uuid.UUID:
    async with engine.acquire() as conn, tx(conn):
        conversation_id = await start_or_continue(
            conn,
            user_id=_USER,
            workspace_id=_WORKSPACE,
            conversation_id=None,
            text=text,
            partner_session_id=session_id,
            partner_actor="agent-1",
        )
        await add_message(
            conn,
            conversation_id=conversation_id,
            role="user",
            author="user@example.com",
            text=text,
        )
    return conversation_id


async def _texts(engine: PGliteEngine, *, conversation_id: uuid.UUID) -> list[str]:
    thread = await read_thread(
        engine,
        user_id=_USER,
        conversation_id=conversation_id,
        after_seq=0,
    )
    assert thread is not None
    return [message.text for message in thread.messages]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_new_conversation_is_titled_by_its_first_words(
    pglite_engine: PGliteEngine,
) -> None:
    """Whitespace collapses and the title is cut to 80 characters."""
    session = await _seed(pglite_engine)
    conversation_id = await _conversation(
        pglite_engine,
        session_id=session,
        text="  Show   me\n the   timeline " + "z" * 100,
    )
    (summary,) = await list_conversations(pglite_engine, user_id=_USER)
    assert summary.id == conversation_id
    assert summary.title == ("Show me the timeline " + "z" * 100)[:80]
    assert (summary.partner_actor, summary.workspace_id) == ("agent-1", _WORKSPACE)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_continuing_needs_your_own_conversation_on_the_same_canvas(
    pglite_engine: PGliteEngine,
) -> None:
    """A foreign conversation, or one of another canvas, is not found."""
    session = await _seed(pglite_engine)
    conversation_id = await _conversation(pglite_engine, session_id=session)
    other_canvas = uuid.uuid4()
    async with pglite_engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO visual_workspaces (id, user_id, is_default, state) "
            "VALUES ($1, $2, FALSE, $3)",
            other_canvas,
            _USER,
            {"visuals": []},
        )
        for user, canvas in ((_OTHER, _WORKSPACE), (_USER, other_canvas)):
            with pytest.raises(ChatConversationNotFoundError):
                await start_or_continue(
                    conn,
                    user_id=user,
                    workspace_id=canvas,
                    conversation_id=conversation_id,
                    text="x",
                    partner_session_id=session,
                    partner_actor=None,
                )
        again = await start_or_continue(
            conn,
            user_id=_USER,
            workspace_id=_WORKSPACE,
            conversation_id=conversation_id,
            text="x",
            partner_session_id=session,
            partner_actor="renamed",
        )
    assert again == conversation_id
    (summary,) = await list_conversations(pglite_engine, user_id=_USER)
    assert summary.partner_actor == "renamed"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_lines_are_numbered_from_one_and_a_known_key_stores_nothing(
    pglite_engine: PGliteEngine,
) -> None:
    """Seq counts the stored lines; a repeated request key adds none."""
    session = await _seed(pglite_engine)
    conversation_id = await _conversation(pglite_engine, session_id=session)
    key = uuid.uuid4()
    async with pglite_engine.acquire() as conn, tx(conn):
        second = await add_message(
            conn,
            conversation_id=conversation_id,
            role="assistant",
            author="agent-1",
            text="two",
            request_key=key,
            request_hash="h",
        )
        key_again = await add_message(
            conn,
            conversation_id=conversation_id,
            role="user",
            author="u",
            text="dup",
            request_key=key,
            request_hash="h",
        )
        gone = await add_message(
            conn,
            conversation_id=uuid.uuid4(),
            role="user",
            author="u",
            text="nowhere",
        )
    assert second is not None
    assert second.seq == 2
    assert (key_again, gone) == (None, None)
    assert await _texts(pglite_engine, conversation_id=conversation_id) == [
        "hello",
        "two",
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_concurrent_lines_take_consecutive_numbers(
    pglite_engine: PGliteEngine,
) -> None:
    """The conversation row is locked, so two writers never collide on a seq."""
    session = await _seed(pglite_engine)
    conversation_id = await _conversation(pglite_engine, session_id=session)

    async def write(text: str) -> None:
        async with pglite_engine.acquire() as conn, tx(conn):
            stored = await add_message(
                conn,
                conversation_id=conversation_id,
                role="assistant",
                author="a",
                text=text,
            )
            assert stored is not None

    await asyncio.gather(*(write(f"line {n}") for n in range(4)))
    thread = await read_thread(
        pglite_engine,
        user_id=_USER,
        conversation_id=conversation_id,
        after_seq=0,
    )
    assert thread is not None
    assert [m.seq for m in thread.messages] == [1, 2, 3, 4, 5]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_stored_send_replays_for_its_own_request_only(
    pglite_engine: PGliteEngine,
) -> None:
    """The key finds the original; another request, or another user, does not."""
    session = await _seed(pglite_engine)
    conversation_id = await _conversation(pglite_engine, session_id=session)
    key = uuid.uuid4()
    async with pglite_engine.acquire() as conn, tx(conn):
        stored = await add_message(
            conn,
            conversation_id=conversation_id,
            role="user",
            author="user@example.com",
            text="send",
            request_key=key,
            request_hash="same",
        )
    assert stored is not None
    async with pglite_engine.acquire() as conn:
        replay = await replay_of(
            conn,
            user_id=_USER,
            request_key=key,
            request_hash="same",
        )
        assert replay is not None
        assert (replay.conversation_id, replay.session_id) == (conversation_id, session)
        assert replay.message == stored
        assert (
            await replay_of(
                conn,
                user_id=_USER,
                request_key=uuid.uuid4(),
                request_hash="same",
            )
            is None
        )
        assert (
            await replay_of(conn, user_id=_OTHER, request_key=key, request_hash="same")
            is None
        )
        with pytest.raises(ChatRequestConflictError):
            await replay_of(conn, user_id=_USER, request_key=key, request_hash="other")


async def _fill(
    engine: PGliteEngine,
    *,
    conversation_id: uuid.UUID,
    total: int,
) -> None:
    async with engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO chat_messages (id, conversation_id, seq, role, author, text) "
            "SELECT gen_random_uuid(), $1, n, 'user', 'u', 'line ' || n "
            "FROM generate_series(2, $2::int) AS n",
            conversation_id,
            total,
        )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_thread_reads_its_newest_500_and_says_older_ones_exist(
    pglite_engine: PGliteEngine,
) -> None:
    """With no cursor, the newest 500 in order, and `earlier` for the rest."""
    session = await _seed(pglite_engine)
    conversation_id = await _conversation(pglite_engine, session_id=session)
    await _fill(pglite_engine, conversation_id=conversation_id, total=520)
    thread = await read_thread(
        pglite_engine,
        user_id=_USER,
        conversation_id=conversation_id,
        after_seq=None,
    )
    assert thread is not None
    assert [m.seq for m in thread.messages] == list(range(21, 521))
    assert thread.earlier is True
    assert (thread.partner_actor, thread.partner_session_id) == ("agent-1", session)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_thread_after_a_seq_reads_up_to_500_forward(
    pglite_engine: PGliteEngine,
) -> None:
    """A cursor pages forward, oldest first, and never reports a gap it skipped."""
    session = await _seed(pglite_engine)
    conversation_id = await _conversation(pglite_engine, session_id=session)
    await _fill(pglite_engine, conversation_id=conversation_id, total=520)

    async def seqs(after: int) -> tuple[list[int], bool]:
        thread = await read_thread(
            pglite_engine,
            user_id=_USER,
            conversation_id=conversation_id,
            after_seq=after,
        )
        assert thread is not None
        return [m.seq for m in thread.messages], thread.earlier

    assert await seqs(0) == (list(range(1, 501)), False)
    assert await seqs(500) == (list(range(501, 521)), True)
    assert await seqs(519) == ([520], True)
    assert await seqs(520) == ([], False)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_short_thread_has_nothing_earlier_and_a_foreign_one_is_none(
    pglite_engine: PGliteEngine,
) -> None:
    """Few lines are all returned; someone else's conversation is not found."""
    session = await _seed(pglite_engine)
    conversation_id = await _conversation(pglite_engine, session_id=session)
    thread = await read_thread(
        pglite_engine,
        user_id=_USER,
        conversation_id=conversation_id,
        after_seq=None,
    )
    assert thread is not None
    assert (len(thread.messages), thread.earlier) == (1, False)
    assert (
        await read_thread(
            pglite_engine,
            user_id=_OTHER,
            conversation_id=conversation_id,
            after_seq=None,
        )
        is None
    )
    assert await conversations_of(
        pglite_engine,
        user_id=_USER,
        workspace_id=_WORKSPACE,
    ) == [conversation_id]
    assert (
        await conversations_of(pglite_engine, user_id=_OTHER, workspace_id=_WORKSPACE)
        == []
    )
    assert not await delete_conversation(
        pglite_engine,
        user_id=_OTHER,
        conversation_id=conversation_id,
    )
    assert await delete_conversation(
        pglite_engine,
        user_id=_USER,
        conversation_id=conversation_id,
    )
    assert await list_conversations(pglite_engine, user_id=_USER) == []


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_history_lists_the_newest_change_first_and_at_most_fifty(
    pglite_engine: PGliteEngine,
) -> None:
    """The history is bounded and ordered by modification."""
    session = await _seed(pglite_engine)
    first = await _conversation(pglite_engine, session_id=session, text="first")
    async with pglite_engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO chat_conversations "
            "(id, user_id, workspace_id, title, modified_at) "
            "SELECT gen_random_uuid(), $1, $2, 'c' || n, "
            "clock_timestamp() + n * interval '1 second' "
            "FROM generate_series(1, 55) AS n",
            _USER,
            _WORKSPACE,
        )
    listed = await list_conversations(pglite_engine, user_id=_USER)
    assert len(listed) == 50
    assert listed[0].title == "c55"
    assert first not in {summary.id for summary in listed}


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_answer_is_stored_published_and_clears_the_status(
    pglite_engine: PGliteEngine,
) -> None:
    """The partner's key posts the answer, which replaces what it was doing."""
    session = await _seed(pglite_engine)
    conversation_id = await _conversation(pglite_engine, session_id=session)
    hub = ChatHub()
    hub.set_status(conversation_id, text="thinking")
    with hub.subscribe(_WORKSPACE) as stream:
        stored = await post_reply(
            pglite_engine,
            conversation_id=conversation_id,
            api_key_id=_KEY,
            reply=ChatReply(text="done", kind="answer"),
            hub=hub,
        )
        frame = stream.queue.get_nowait()
        assert stream.queue.empty()
    assert stored is not None
    assert (stored.role, stored.author, stored.seq) == ("assistant", "agent-1", 2)
    assert isinstance(frame, MessageFrame)
    assert frame.message == stored
    assert hub.status_of(conversation_id) == ""


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_answer_highlights_the_records_it_names(
    pglite_engine: PGliteEngine,
) -> None:
    """Each existing `Kind#seq` an answer names is marked, once, in its order."""
    session = await _seed(pglite_engine)
    conversation_id = await _conversation(pglite_engine, session_id=session)
    first, second = uuid.uuid4(), uuid.uuid4()
    async with pglite_engine.acquire() as conn:
        for row, kind, seq in ((first, "Issue", 7), (second, "Experiment", 7)):
            await conn.execute(
                "INSERT INTO inquiries (id, kind, seq, status, account, title) "
                "VALUES ($1, $2, $3, 'active', 'user@example.com', 'T')",
                row,
                kind,
                seq,
            )
    hub = ChatHub()
    with hub.subscribe(_WORKSPACE) as stream:
        await post_reply(
            pglite_engine,
            conversation_id=conversation_id,
            api_key_id=_KEY,
            reply=ChatReply(
                text=(
                    "Experiment#7 tests Issue#7; see Issue#7 again, not Issue#99, "
                    "x#Issue#7, issue#7 or Paper#7."
                ),
                kind="answer",
            ),
            hub=hub,
        )
        await post_reply(
            pglite_engine,
            conversation_id=conversation_id,
            api_key_id=_KEY,
            reply=ChatReply(text="Nothing named here.", kind="answer"),
            hub=hub,
        )
        frames = [stream.queue.get_nowait() for _ in range(stream.queue.qsize())]
    assert [type(frame).__name__ for frame in frames] == [
        "MessageFrame",
        "HighlightFrame",
        "MessageFrame",
    ]
    highlight = frames[1]
    assert isinstance(highlight, HighlightFrame)
    assert highlight.ids == [second, first]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_status_is_kept_and_pushed_and_never_stored(
    pglite_engine: PGliteEngine,
) -> None:
    """A status shows what the partner is doing, until the next or an empty one."""
    session = await _seed(pglite_engine)
    conversation_id = await _conversation(pglite_engine, session_id=session)
    hub = ChatHub()
    with hub.subscribe(_WORKSPACE) as stream:
        assert (
            await post_reply(
                pglite_engine,
                conversation_id=conversation_id,
                api_key_id=_KEY,
                reply=ChatReply(text="thinking", kind="status"),
                hub=hub,
            )
            is None
        )
        frame = stream.queue.get_nowait()
    assert isinstance(frame, StatusFrame)
    assert (frame.conversation_id, frame.text) == (conversation_id, "thinking")
    assert hub.status_of(conversation_id) == "thinking"
    assert await _texts(pglite_engine, conversation_id=conversation_id) == ["hello"]
    await post_reply(
        pglite_engine,
        conversation_id=conversation_id,
        api_key_id=_KEY,
        reply=ChatReply(text="", kind="status"),
        hub=hub,
    )
    assert hub.status_of(conversation_id) == ""


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_answer_that_cannot_be_stored_is_not_found_and_pushes_nothing(
    pglite_engine: PGliteEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A conversation deleted mid-reply is a 404, never a status frame."""
    session = await _seed(pglite_engine)
    conversation_id = await _conversation(pglite_engine, session_id=session)

    async def vanished(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(chats, "add_message", vanished)
    hub = ChatHub()
    with (
        hub.subscribe(_WORKSPACE) as stream,
        pytest.raises(ChatConversationNotFoundError),
    ):
        await post_reply(
            pglite_engine,
            conversation_id=conversation_id,
            api_key_id=_KEY,
            reply=ChatReply(text="lost", kind="answer"),
            hub=hub,
        )
    assert stream.queue.empty()
    assert hub.status_of(conversation_id) == ""


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_only_the_partner_sessions_live_key_may_reply(
    pglite_engine: PGliteEngine,
) -> None:
    """Another key, a revoked key, an ended session and an unknown id are refused."""
    session = await _seed(pglite_engine)
    conversation_id = await _conversation(pglite_engine, session_id=session)
    hub = ChatHub()
    reply = ChatReply(text="x", kind="answer")

    async def attempt(key: uuid.UUID, *, conversation: uuid.UUID) -> None:
        await post_reply(
            pglite_engine,
            conversation_id=conversation,
            api_key_id=key,
            reply=reply,
            hub=hub,
        )

    with pytest.raises(ChatReplyForbiddenError):
        await attempt(_OTHER_KEY, conversation=conversation_id)
    with pytest.raises(ChatConversationNotFoundError):
        await attempt(_KEY, conversation=uuid.uuid4())
    async with pglite_engine.acquire() as conn:
        await conn.execute(
            "UPDATE api_keys SET revoked_at = clock_timestamp() WHERE id = $1",
            _KEY,
        )
    with pytest.raises(ChatReplyForbiddenError):
        await attempt(_KEY, conversation=conversation_id)
    async with pglite_engine.acquire() as conn:
        await conn.execute("UPDATE api_keys SET revoked_at = NULL WHERE id = $1", _KEY)
        await conn.execute(
            "UPDATE inquiries SET status = 'complete', "
            "agentsession_ended = clock_timestamp() "
            "WHERE id = $1",
            session,
        )
    with pytest.raises(ChatReplyForbiddenError):
        await attempt(_KEY, conversation=conversation_id)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_partner_key_reads_the_thread_and_others_are_refused(
    pglite_engine: PGliteEngine,
) -> None:
    """Reseeding a thread is for the live partner session's key alone."""
    session = await _seed(pglite_engine)
    conversation_id = await _conversation(pglite_engine, session_id=session)
    thread = await read_partner_thread(
        pglite_engine,
        api_key_id=_KEY,
        conversation_id=conversation_id,
        after_seq=None,
    )
    assert [m.text for m in thread.messages] == ["hello"]
    assert (thread.partner_session_id, thread.earlier) == (session, False)
    with pytest.raises(ChatReplyForbiddenError):
        await read_partner_thread(
            pglite_engine,
            api_key_id=_OTHER_KEY,
            conversation_id=conversation_id,
            after_seq=None,
        )
    with pytest.raises(ChatConversationNotFoundError):
        await read_partner_thread(
            pglite_engine,
            api_key_id=_KEY,
            conversation_id=uuid.uuid4(),
            after_seq=None,
        )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_release_tells_lost_conversations_and_clears_the_rest(
    pglite_engine: PGliteEngine,
) -> None:
    """Undelivered conversations get the not-delivered status; others lose theirs."""
    session = await _seed(pglite_engine)
    lost = await _conversation(pglite_engine, session_id=session, text="lost")
    quiet = await _conversation(pglite_engine, session_id=session, text="quiet")
    idle = await _conversation(pglite_engine, session_id=session, text="idle")
    elsewhere = uuid.uuid4()
    hub = ChatHub()
    hub.set_status(lost, text="thinking")
    hub.set_status(quiet, text="thinking")
    with hub.subscribe(_WORKSPACE) as stream:
        await release_session(
            pglite_engine,
            session_id=session,
            undelivered=[
                (lost, _WORKSPACE),
                (lost, _WORKSPACE),
                (elsewhere, _WORKSPACE),
            ],
            hub=hub,
        )
        frames: list[Frame | None] = []
        while not stream.queue.empty():
            frames.append(stream.queue.get_nowait())
    said = {f.conversation_id: f.text for f in frames if isinstance(f, StatusFrame)}
    assert said == {lost: NOT_DELIVERED, elsewhere: NOT_DELIVERED, quiet: ""}
    assert (hub.status_of(lost), hub.status_of(quiet), hub.status_of(idle)) == (
        NOT_DELIVERED,
        "",
        "",
    )
    assert len(frames) == 3


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
