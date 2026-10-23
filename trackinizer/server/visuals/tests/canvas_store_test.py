"""Canvas key access follows the live chat and its canonical starter."""

from __future__ import annotations

from typing import TYPE_CHECKING

import uuid

import pytest

from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.config import Assistant
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.primitives import insert_inquiry
from trackinizer.server.store.core import Store
from trackinizer.server.visuals.canvas_store import (
    CanvasKeyRefusedError,
    agent_may_operate,
)
from trackinizer.wire.wire_science_chat import SCIENCE_CHAT_LABEL, chat_session_id


if TYPE_CHECKING:
    from trackinizer.lib.postgres import PGliteEngine


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    "case",
    [
        "live",
        "other-key",
        "assistant-opener",
        "starter-case",
        "foreign-key",
        "another-chat",
        "no-chat",
        "assistant-provider",
        "unlabelled",
        "ended",
        "revoked-caller",
        "revoked-opener",
        "no-poller",
        "wrong-cli",
        "wrong-starter",
    ],
)
async def test_a_canvas_key_needs_its_starters_live_selected_chat(
    pglite_engine: PGliteEngine,
    case: str,
) -> None:
    """Exercise the key authorization contract against real persisted sessions."""
    await reset_schema(pglite_engine)
    await Store(pglite_engine, embed=StubEmbedder()).bootstrap()
    owner_id, assistant_id = uuid.uuid4(), uuid.uuid4()
    owner_key, assistant_key, second_key = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    canvas_id, conversation_id, session_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assistant = Assistant(actor="helper", email="helper@example.com")
    caller_key = second_key if case in {"other-key", "revoked-opener"} else owner_key
    if case == "foreign-key":
        caller_key = assistant_key
    inbound = InboundQueue()
    if case != "no-poller":
        inbound.mark_poller(
            session_id,
            cli="assistant"
            if case == "assistant-provider"
            else "claude"
            if case == "wrong-cli"
            else "codex",
        )
    async with pglite_engine.acquire() as conn:
        for user_id, email in (
            (owner_id, "owner@example.com"),
            (assistant_id, assistant.email),
        ):
            await conn.execute(
                "INSERT INTO users (id, email, name, role, status) "
                "VALUES ($1, $2, 'Test', 'writer', 'active')",
                user_id,
                email,
            )
        for key_id, user_id in (
            (owner_key, owner_id),
            (second_key, owner_id),
            (assistant_key, assistant_id),
        ):
            await conn.execute(
                "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
                "VALUES ($1, $2, 'test', 'hash', 'test', 'writer')",
                key_id,
                user_id,
            )
        await conn.execute(
            "INSERT INTO visual_canvases (id, user_id, metadata, state) "
            "VALUES ($1, $2, $3, $4)",
            canvas_id,
            owner_id,
            {
                "name": "canvas",
                "conversation_id": None if case == "no-chat" else str(conversation_id),
            },
            {
                "visuals": [],
                "provider": "assistant" if case == "assistant-provider" else "codex",
            },
        )
        await insert_inquiry(
            conn,
            session_id,
            "AgentSession",
            values={
                "title": "canvas chat",
                "owner": "chat-owner",
                "account": assistant.email
                if case == "wrong-starter"
                else "OWNER@example.com"
                if case == "starter-case"
                else "owner@example.com",
                "labels": [] if case == "unlabelled" else [SCIENCE_CHAT_LABEL],
                "subscribers": (),
                "agentsession_audience": "open",
                "agentsession_cli": "codex",
                "agentsession_cli_session_id": chat_session_id(
                    uuid.uuid4() if case == "another-chat" else conversation_id,
                ),
                "agentsession_rooms": (),
                "agentsession_opened_by_api_key_id": assistant_key
                if case in {"assistant-opener", "wrong-starter"}
                else owner_key,
            },
        )
        if case == "ended":
            await conn.execute(
                "UPDATE inquiries SET status = 'complete', agentsession_ended = now() "
                "WHERE id = $1",
                session_id,
            )
        elif case in {"revoked-caller", "revoked-opener"}:
            await conn.execute(
                "UPDATE api_keys SET revoked_at = now() WHERE id = $1",
                owner_key,
            )

    if case in {"live", "other-key", "assistant-opener", "starter-case", "foreign-key"}:
        allowed = await agent_may_operate(
            pglite_engine,
            user_id=assistant_id if case == "foreign-key" else owner_id,
            canvas_id=canvas_id,
            agent_api_key_id=caller_key,
            inbound=inbound,
            assistant=assistant,
        )
        assert allowed == (case != "foreign-key")
    else:
        with pytest.raises(CanvasKeyRefusedError):
            await agent_may_operate(
                pglite_engine,
                user_id=owner_id,
                canvas_id=canvas_id,
                agent_api_key_id=caller_key,
                inbound=inbound,
                assistant=assistant,
            )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
