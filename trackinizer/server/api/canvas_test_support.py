"""Seeding helpers shared by the canvas and science chat route tests.

Three principals share one PGlite database: the browser user, a second user, and
the account the assistant runs under. Each acts through a browser
identity (no API key) or an agent identity (an API key that opened sessions).
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Final

import uuid

from trackinizer.lib.codec import from_plain, loads
from trackinizer.server.api.conftest import (
    TEST_API_KEY_ID,
    TEST_USER_EMAIL,
    TEST_USER_ID,
    install_identity,
    make_test_identity,
)
from trackinizer.server.config import Assistant, Config
from trackinizer.wire.wire_science_chat import (
    SCIENCE_CHAT_LABEL,
    chat_session_id,
    poster_label,
)


if TYPE_CHECKING:
    from collections.abc import Mapping

    import httpx2

    from trackinizer.server.auth import Role
    from trackinizer.server.store.core import Store


OTHER_USER_ID: Final = uuid.UUID("33333333-3333-3333-3333-333333333333")
OTHER_KEY_ID: Final = uuid.UUID("44444444-4444-4444-4444-444444444444")
OTHER_EMAIL: Final = "other-user@example.com"

KB_USER_ID: Final = uuid.UUID("55555555-5555-5555-5555-555555555555")
KB_KEY_ID: Final = uuid.UUID("66666666-6666-6666-6666-666666666666")
KB_EMAIL: Final = "scout@example.com"
KB_ACTOR: Final = "scout"

ASSISTANT_CONFIG: Final = replace(
    Config(),
    assistant=Assistant(actor=KB_ACTOR, email=KB_EMAIL),
    chat_orgs="single",
)


def browser(user_id: uuid.UUID = TEST_USER_ID, *, email: str = TEST_USER_EMAIL) -> None:
    """Act as a signed-in browser.

    Args:
      user_id: The signed-in user.
      email: Their email.

    """
    install_identity(make_test_identity(user_id=user_id, api_key_id=None, email=email))


def act_as_user_agent(role: Role = "viewer") -> None:
    """Act as the browser user's own `trax run` key, as a viewer or a writer."""
    _agent(TEST_API_KEY_ID, user_id=TEST_USER_ID, email=TEST_USER_EMAIL, role=role)


def act_as_other_agent(role: Role = "viewer") -> None:
    """Act as the second user's key."""
    _agent(OTHER_KEY_ID, user_id=OTHER_USER_ID, email=OTHER_EMAIL, role=role)


def act_as_assistant(role: Role = "viewer") -> None:
    """Act as the assistant account's key."""
    _agent(KB_KEY_ID, user_id=KB_USER_ID, email=KB_EMAIL, role=role)


async def seed_accounts(store: Store) -> None:
    """Create the three users, each with one API key.

    Args:
      store: The test database.

    """
    users = (
        (TEST_USER_ID, TEST_USER_EMAIL, TEST_API_KEY_ID),
        (OTHER_USER_ID, OTHER_EMAIL, OTHER_KEY_ID),
        (KB_USER_ID, KB_EMAIL, KB_KEY_ID),
    )
    async with store.engine.acquire() as conn:
        for user_id, email, key_id in users:
            await conn.execute(
                "INSERT INTO users (id, email, name, role, status, "
                "visual_workspace_enabled) "
                "VALUES ($1, $2, 'Test', 'writer', 'active', TRUE)",
                user_id,
                email,
            )
            await conn.execute(
                "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
                "VALUES ($1, $2, 'agent', 'test-hash', $3, 'writer')",
                key_id,
                user_id,
                f"trax_{key_id.hex[:6]}",
            )


async def start_session(
    client: httpx2.AsyncClient,
    *,
    actor: str | None = None,
    cli: str = "codex",
    cli_session_id: str | None = None,
    account: str | None = None,
) -> uuid.UUID:
    """Open a session as the installed agent identity and poll once so it is live.

    Args:
      client: The test client.
      actor: The routing name to ask for.
      cli: The CLI the session says it runs.
      cli_session_id: The id the session is known by across restarts.
      account: The person the session is attributed to; the key's owner by default.

    Returns:
      session_id: The new session.

    """
    started = await client.post(
        "/api/sessions/start",
        json={
            "cli": cli,
            "actor": actor,
            "cli_session_id": cli_session_id,
            "account": account,
        },
    )
    if started.status_code != 201:
        raise ValueError(f"session start answered {started.status_code}")
    session_id = from_plain(
        from_plain(loads(started.content), dict[str, object])["id"],
        str,
    )
    polled = await client.get(f"/api/sessions/{session_id}/inbound")
    if polled.status_code != 200:
        raise ValueError(f"inbound poll answered {polled.status_code}")
    return uuid.UUID(session_id)


async def open_science_chat(
    client: httpx2.AsyncClient,
    store: Store,
    *,
    conversation_id: uuid.UUID,
    account: str = TEST_USER_EMAIL,
    posters: tuple[str, ...] = (),
) -> uuid.UUID:
    """Open a conversation's session as the assistant does, then act as the browser.

    Args:
      client: The test client.
      store: The test database.
      conversation_id: The conversation.
      account: The person who started it.
      posters: Everyone who has posted in it besides the starter.

    Returns:
      session_id: The conversation's live session.

    """
    act_as_assistant("writer")
    session_id = await start_session(
        client,
        actor=f"chat-{conversation_id.hex[:12]}",
        cli_session_id=chat_session_id(conversation_id),
        account=account,
    )
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET labels = $2 WHERE id = $1",
            session_id,
            [SCIENCE_CHAT_LABEL, *(poster_label(email) for email in posters)],
        )
    browser()
    return session_id


async def open_workspace(
    client: httpx2.AsyncClient,
    *,
    user_id: uuid.UUID = TEST_USER_ID,
    email: str = TEST_USER_EMAIL,
) -> uuid.UUID:
    """Create the user's canvas as a browser.

    Args:
      client: The test client.
      user_id: The signed-in user.
      email: Their email.

    Returns:
      workspace_id: The canvas.

    """
    browser(user_id, email=email)
    created = await client.post("/api/workspaces")
    if created.status_code != 200:
        raise ValueError(f"workspace create answered {created.status_code}")
    return uuid.UUID(
        from_plain(from_plain(loads(created.content), dict[str, object])["id"], str),
    )


async def show_chat(
    client: httpx2.AsyncClient,
    *,
    workspace_id: uuid.UUID,
) -> uuid.UUID:
    """Show the Chat visual as the installed browser.

    Args:
      client: The test client.
      workspace_id: The canvas.

    Returns:
      chat_id: The Chat visual's instance id.

    """
    state = from_plain(
        loads((await client.get(f"/api/workspaces/{workspace_id}")).content),
        dict[str, object],
    )
    # pragma: no mutate start -- a header name's case is an equivalent mutant.
    headers = {"Idempotency-Key": str(uuid.uuid4())}
    # pragma: no mutate end
    shown = await client.post(
        f"/api/workspaces/{workspace_id}/operations",
        json={
            "revision": state["revision"],
            "operation": {"kind": "show", "visual_type": "trax.chat"},
        },
        headers=headers,
    )
    if shown.status_code != 200:
        raise ValueError(f"show answered {shown.status_code}")
    visuals = from_plain(
        from_plain(loads(shown.content), dict[str, object])["visuals"],
        list[dict[str, object]],
    )
    chat = next(v for v in visuals if v["type"] == "trax.chat")
    return uuid.UUID(from_plain(chat["id"], str))


async def send_chat(
    client: httpx2.AsyncClient,
    *,
    workspace_id: uuid.UUID,
    chat_id: uuid.UUID,
    text: str,
    conversation_id: uuid.UUID | None = None,
    key: uuid.UUID | None = None,
    screen: Mapping[str, object] | None = None,
    fork: Mapping[str, object] | None = None,
) -> httpx2.Response:
    """Post one browser line to a science chat.

    Args:
      client: The test client.
      workspace_id: The canvas.
      chat_id: Its Chat visual.
      text: The line.
      conversation_id: The conversation to post into; none starts one.
      key: The idempotency key; a fresh one by default.
      screen: The sender's ``page`` and ``trail``, when the line carries them.
      fork: The line to start a new conversation from, as the body names it.

    Returns:
      response: The raw response.

    """
    idempotency = str(key or uuid.uuid4())
    # pragma: no mutate start -- a header name's case is an equivalent mutant.
    headers = {"Idempotency-Key": idempotency}
    # pragma: no mutate end
    return await client.post(
        "/api/chats",
        json={
            "workspace_id": str(workspace_id),
            "text": text,
            "chat_instance_id": str(chat_id),
            "expected_record_id": None,
            "conversation_id": None
            if conversation_id is None
            else str(conversation_id),
            **(screen or {}),
            **({} if fork is None else {"fork": dict(fork)}),
        },
        headers=headers,
    )


def conversation_of(response: httpx2.Response) -> uuid.UUID:
    """Name the conversation a posted line's receipt carries.

    Args:
      response: A successful post response.

    Returns:
      conversation_id: The conversation.

    """
    if response.status_code != 200:
        raise ValueError(f"send answered {response.status_code}")
    receipt = from_plain(loads(response.content), dict[str, object])
    return uuid.UUID(from_plain(receipt["conversation_id"], str))


async def converse(
    client: httpx2.AsyncClient,
    store: Store,
    *,
    workspace_id: uuid.UUID,
    chat_id: uuid.UUID,
    text: str = "hello",
) -> uuid.UUID:
    """Post a line as the browser, and have the assistant open the conversation.

    Args:
      client: The test client.
      store: The test database.
      workspace_id: The canvas.
      chat_id: Its Chat visual.
      text: The line.

    Returns:
      session_id: The conversation's live session.

    """
    browser()
    conversation_id = conversation_of(
        await send_chat(client, workspace_id=workspace_id, chat_id=chat_id, text=text),
    )
    return await open_science_chat(client, store, conversation_id=conversation_id)


async def revision_of(client: httpx2.AsyncClient, *, workspace_id: uuid.UUID) -> int:
    """Read a canvas's revision as the installed browser.

    Args:
      client: The test client.
      workspace_id: The canvas.

    Returns:
      revision: Its current revision.

    """
    state = from_plain(
        loads((await client.get(f"/api/workspaces/{workspace_id}")).content),
        dict[str, object],
    )
    return from_plain(state["revision"], int)


async def drain(
    client: httpx2.AsyncClient,
    *,
    session_id: uuid.UUID,
) -> httpx2.Response:
    """Drain a session's inbound queue as the installed identity.

    Args:
      client: The test client.
      session_id: The session.

    Returns:
      response: The raw response.

    """
    return await client.get(f"/api/sessions/{session_id}/inbound")


def _agent(key_id: uuid.UUID, *, user_id: uuid.UUID, email: str, role: Role) -> None:
    """Act as an agent holding one API key."""
    install_identity(
        make_test_identity(
            user_id=user_id,
            api_key_id=key_id,
            email=email,
            role=role,
        ),
    )
