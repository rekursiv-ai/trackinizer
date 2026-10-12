"""A chat's delete racing a line being recorded into it, on real PostgreSQL.

PGlite has one connection, so a record and a delete never overlap there: only two
backends show a delete that read who posted before a line still being written landed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import asyncio
import uuid

import pytest

from trackinizer.server.api.canvas_test_support import (
    ASSISTANT,
    KB_ACTOR,
    KB_EMAIL,
    OTHER_EMAIL,
    seed_accounts,
    seed_session,
)
from trackinizer.server.api.conftest import TEST_USER_EMAIL, TEST_USER_ID
from trackinizer.server.auth import AuthIdentity
from trackinizer.server.config import Assistant
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.visuals.chat_forks import ChatRefusedError
from trackinizer.server.visuals.chat_shares import delete_chat
from trackinizer.wire.wire_science_chat import SCIENCE_CHAT_LABEL, chat_session_id


if TYPE_CHECKING:
    from trackinizer.server.store.core import Store


pytestmark = [
    pytest.mark.db_postgres,
    pytest.mark.asyncio(loop_scope="session"),
]

_ADA: Final = AuthIdentity(
    user_id=TEST_USER_ID,
    api_key_id=None,
    email=TEST_USER_EMAIL,
    role="writer",
)


async def test_a_line_being_recorded_when_the_starter_deletes_counts_as_posted(
    integ_store: Store,
) -> None:
    """The delete waits for the session a record holds, then reads Bob's line."""
    conversation = uuid.uuid4()
    await seed_accounts(integ_store)
    session = await seed_session(
        integ_store,
        ASSISTANT,
        actor="chat-ada",
        cli_session_id=chat_session_id(conversation),
        account=TEST_USER_EMAIL,
        labels=[SCIENCE_CHAT_LABEL],
        polled=False,
    )

    async with integ_store.engine.acquire() as recording:
        await recording.execute("BEGIN")
        await recording.execute(
            "SELECT id FROM inquiries WHERE id = $1 FOR UPDATE",
            session,
        )
        await recording.execute(
            "INSERT INTO session_records (session_id, part, idx, kind, payload, text) "
            "VALUES ($1, 0, 0, 'AgentToAgentMessage', $2::json, 'q')",
            session,
            f'{{"sender": "{OTHER_EMAIL}", "content": "q"}}',
        )
        deleting = asyncio.create_task(
            delete_chat(
                integ_store,
                assistant=Assistant(actor=KB_ACTOR, email=KB_EMAIL),
                caller=_ADA,
                conversation_id=conversation,
                inbound=InboundQueue(),
            ),
        )
        await _until_a_lock_is_awaited(integ_store)
        await recording.execute("COMMIT")

    with pytest.raises(ChatRefusedError) as refused:
        _ = await deleting
    assert refused.value.code == "chat_has_other_posters"


async def _until_a_lock_is_awaited(store: Store) -> None:
    """Return once another backend waits on a lock."""
    async with store.engine.acquire() as conn:
        for _ in range(400):
            if await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE NOT granted)",
            ):
                return
            await asyncio.sleep(0.005)
    raise AssertionError("no backend waited on a lock")


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
