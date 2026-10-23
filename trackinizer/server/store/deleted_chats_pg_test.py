"""A chat's delete racing a session start under its id, on real PostgreSQL.

PGlite has one connection, so a start and a delete never overlap there: only two
backends show a start that found no tombstone inserting after the delete read what it
takes, or a start that waited on the delete inserting after it committed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import asyncio
import uuid

from treekle import from_plain

import pytest

from trackinizer.server.api.canvas_test_support import (
    ASSISTANT,
    KB_ACTOR,
    KB_EMAIL,
    KB_KEY_ID,
    seed_accounts,
    seed_session,
)
from trackinizer.server.api.conftest import TEST_USER_EMAIL
from trackinizer.server.auth import AuthIdentity
from trackinizer.server.config import Assistant
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.store.deleted_chats import (
    lock_chat_id,
    refuse_a_deleted_chat,
)
from trackinizer.server.visuals.chat_shares import delete_chat
from trackinizer.types.errors import ChatDeletedError
from trackinizer.wire.bodies import SubmitAgentSession
from trackinizer.wire.wire_science_chat import SCIENCE_CHAT_LABEL, chat_session_id


if TYPE_CHECKING:
    from trackinizer.server.store.core import Store


pytestmark = [
    pytest.mark.db_postgres,
    pytest.mark.asyncio(loop_scope="session"),
]

_ADMIN: Final = AuthIdentity(
    user_id=uuid.UUID("77777777-7777-7777-7777-777777777777"),
    api_key_id=None,
    email="root@example.com",
    role="admin",
)
_SCOUT: Final = Assistant(actor=KB_ACTOR, email=KB_EMAIL)


async def test_a_start_that_found_no_tombstone_is_taken_by_the_delete_it_raced(
    integ_store: Store,
) -> None:
    """The delete waits on the start's lock, then takes the session it inserted."""
    conversation = uuid.uuid4()
    await seed_accounts(integ_store)
    _ = await seed_session(
        integ_store,
        ASSISTANT,
        actor="chat-first",
        cli_session_id=chat_session_id(conversation),
        labels=[SCIENCE_CHAT_LABEL],
        polled=False,
    )

    async with integ_store.engine.acquire() as starting:
        await starting.execute("BEGIN")
        await refuse_a_deleted_chat(starting, conversation_id=conversation)
        deleting = asyncio.create_task(_delete(integ_store, conversation))
        await _until_a_lock_is_awaited(integ_store)
        _ = await integ_store.submit_agentsession(
            _start(conversation),
            audience="open",
            api_key_id=KB_KEY_ID,
            actor="chat-late",
            conn=starting,
        )
        await starting.execute("COMMIT")
    canvases = await deleting

    assert canvases == []
    assert await _sessions_under(integ_store, conversation) == 0


async def test_a_start_that_waited_on_a_delete_finds_its_tombstone(
    integ_store: Store,
) -> None:
    """The start waits on the delete's lock, then refuses the id it left behind."""
    conversation = uuid.uuid4()
    await seed_accounts(integ_store)

    async with integ_store.engine.acquire() as deleting:
        await deleting.execute("BEGIN")
        await lock_chat_id(deleting, conversation_id=conversation)
        starting = asyncio.create_task(
            integ_store.start_session(
                _start(conversation),
                requested_actor="chat-late",
                audience="open",
                api_key_id=KB_KEY_ID,
            ),
        )
        await _until_a_lock_is_awaited(integ_store)
        await deleting.execute(
            "INSERT INTO science_chat_tombstones (conversation_id, deleted_by) "
            "VALUES ($1, $2)",
            conversation,
            _ADMIN.email,
        )
        await deleting.execute("COMMIT")

    with pytest.raises(ChatDeletedError):
        _ = await starting
    assert await _sessions_under(integ_store, conversation) == 0


async def test_a_fork_waits_on_the_delete_of_the_chat_it_forks(
    integ_store: Store,
) -> None:
    """The original's delete commits before the fork is checked, never after."""
    origin, fork = uuid.uuid4(), uuid.uuid4()

    async with (
        integ_store.engine.acquire() as deleting,
        integ_store.engine.acquire() as forking,
    ):
        await deleting.execute("BEGIN")
        await lock_chat_id(deleting, conversation_id=origin)
        await forking.execute("BEGIN")
        checking = asyncio.create_task(
            refuse_a_deleted_chat(forking, conversation_id=fork, forked_from=origin),
        )
        await _until_a_lock_is_awaited(integ_store)
        waited = not checking.done()
        await deleting.execute("COMMIT")
        await checking
        await forking.execute("COMMIT")

    assert waited


def _start(conversation: uuid.UUID) -> SubmitAgentSession:
    """Return the body the assistant opens a conversation's session with."""
    return SubmitAgentSession(
        title="late",
        cli="sagent",
        cli_session_id=chat_session_id(conversation),
        account=TEST_USER_EMAIL,
    )


async def _delete(store: Store, conversation: uuid.UUID) -> object:
    """Delete a chat as an admin."""
    return await delete_chat(
        store,
        assistant=_SCOUT,
        caller=_ADMIN,
        conversation_id=conversation,
        inbound=InboundQueue(),
    )


async def _until_a_lock_is_awaited(store: Store) -> None:
    """Return once another backend waits on an advisory lock."""
    async with store.engine.acquire() as conn:
        for _ in range(400):
            if await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_locks "
                "WHERE locktype = 'advisory' AND NOT granted)",
            ):
                return
            await asyncio.sleep(0.005)
    raise AssertionError("no backend waited on the chat's lock")


async def _sessions_under(store: Store, conversation: uuid.UUID) -> int:
    """Count the sessions under a conversation's id."""
    async with store.engine.acquire() as conn:
        return from_plain(
            await conn.fetchval(
                "SELECT count(*) FROM inquiries WHERE agentsession_cli_session_id = $1",
                chat_session_id(conversation),
            ),
            int,
        )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
