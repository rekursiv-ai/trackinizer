"""Seeding helpers shared by the session route tests.

Three principals share one PGlite database: the browser user, a second user, and
the account the assistant runs under. Each acts through a browser
identity (no API key) or an agent identity (an API key that opened sessions), with
the ``hd`` claim Google gives its login: its email's domain, unless a consumer one.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, override

import asyncio
import uuid

from treekle import from_plain, json_loads

from trackinizer.server.api.conftest import (
    TEST_API_KEY_ID,
    TEST_USER_EMAIL,
    TEST_USER_ID,
    install_identity,
    make_test_identity,
)
from trackinizer.server.audience import CONSUMER_DOMAINS
from trackinizer.server.config import Assistant, Config
from trackinizer.server.primitives import insert_inquiry
from trackinizer.server.session_relay import SessionRelay
from trackinizer.server.values import canonical_strs
from trackinizer.wire.wire_session_relay import AgentProcess, Hello, ProcessLine


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    import httpx2

    from trackinizer.server.auth import AuthIdentity, Role
    from trackinizer.server.store.core import Store
    from trackinizer.wire.json_types import JSON
    from trackinizer.wire.wire_session_relay import NewInput
    from trackinizer.wire.wire_sessions import MessageKind


OTHER_USER_ID: Final = uuid.UUID("33333333-3333-3333-3333-333333333333")
OTHER_KEY_ID: Final = uuid.UUID("44444444-4444-4444-4444-444444444444")
OTHER_EMAIL: Final = "other-user@example.com"

KB_USER_ID: Final = uuid.UUID("55555555-5555-5555-5555-555555555555")
KB_KEY_ID: Final = uuid.UUID("66666666-6666-6666-6666-666666666666")
KB_EMAIL: Final = "scout@example.com"
KB_ACTOR: Final = "scout"


@dataclass(frozen=True, slots=True, kw_only=True)
class Agent:
    """An account's API key, as it opens sessions."""

    key_id: uuid.UUID
    email: str


USER_AGENT: Final = Agent(key_id=TEST_API_KEY_ID, email=TEST_USER_EMAIL)
OTHER_AGENT: Final = Agent(key_id=OTHER_KEY_ID, email=OTHER_EMAIL)
ASSISTANT: Final = Agent(key_id=KB_KEY_ID, email=KB_EMAIL)

ASSISTANT_CONFIG: Final[Config] = replace(
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
    install_identity(
        _signed_in(make_test_identity(user_id=user_id, api_key_id=None, email=email)),
    )


def act_as_user_agent(role: Role = "viewer") -> None:
    """Act as the browser user's own agent run's key, as a viewer or a writer."""
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
                "visual_canvas_enabled) "
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
    """Open a session as the installed agent identity.

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
    return uuid.UUID(
        from_plain(json_loads(started.content, dict[str, object])["id"], str),
    )


async def seed_session(
    store: Store,
    agent: Agent,
    *,
    actor: str | None = None,
    cli: str = "codex",
    cli_session_id: str | None = None,
    account: str | None = None,
    labels: Sequence[str] = (),
    polled: bool = True,
) -> uuid.UUID:
    """Open a session for ``agent`` straight in the store, watched for silence if asked.

    What ``start_session`` does through the routes, for a test whose subject is not
    the session start: the same row, in one statement instead of a dozen requests'
    worth, and the reaper's watch an attached process starts. The routing name is taken as given, so it
    must be free among the live sessions.

    Args:
      store: The test database.
      agent: The key that opens the session.
      actor: The routing name; the key owner's email by default.
      cli: The CLI the session says it runs.
      cli_session_id: The id the session is known by across restarts.
      account: The person the session is attributed to; the key's owner by default.
      labels: The labels the row carries.
      polled: Whether the reaper watches the session, as it does one a process
        attached to.

    Returns:
      session_id: The new session.

    """
    session_id = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await insert_inquiry(
            conn,
            session_id,
            "AgentSession",
            values={
                "title": f"{cli} session",
                "owner": actor or agent.email,
                "account": account or agent.email,
                "labels": canonical_strs(labels),
                "subscribers": (),
                "agentsession_audience": "open",
                "agentsession_cli": cli,
                "agentsession_cli_session_id": cli_session_id,
                "agentsession_rooms": (),
                "agentsession_opened_by_api_key_id": agent.key_id,
            },
        )
    if polled:
        await store.record_session_seen(
            session_id,
            at=datetime.now(UTC),
            polled=True,
        )
    return session_id


class ServingRelay(SessionRelay):
    """A relay whose every session has an attached agent process, noting each send.

    Attributes:
      sent: Each input sent, as its session, its payload and its author, in order.

    """

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[tuple[uuid.UUID, JSON, str]] = []

    @override
    def process(self, session_id: uuid.UUID) -> AgentProcess | None:
        return AgentProcess(
            session_id=session_id,
            process_id=uuid.UUID(int=1),
            host="lab",
            state="attached",
            since=datetime(2026, 11, 12, tzinfo=UTC),
        )

    @override
    def send_input(
        self,
        session_id: uuid.UUID,
        new: NewInput,
        *,
        author: str,
        recipient: str = "",
        booked: MessageKind | None = None,
    ) -> uuid.UUID:
        del recipient, booked
        self.sent.append((session_id, new.payload, author))
        return new.id


@asynccontextmanager
async def attached_process(
    relay: SessionRelay,
    session_id: uuid.UUID,
    *,
    host: str = "gx10",
    key: uuid.UUID | None = None,
) -> AsyncGenerator[AgentProcess]:
    """Hold an agent process attached to a session, through the relay's own socket.

    A process a person started at a terminal says ``Hello`` on its socket and is
    attached; the block ends with the process closing its socket, which detaches it.

    Args:
      relay: The relay to attach to.
      session_id: The session the process serves.
      host: The host the process names in its ``Hello``.
      key: The key it holds its socket with; ``None`` for none.

    Yields:
      process: The process, ``attached``.

    """
    lines = relay.lines(session_id, allowed=_always)
    _ = await anext(lines)
    socket = _OpenSocket()
    served = asyncio.ensure_future(
        relay.serve_process(
            session_id,
            socket,
            Hello(process_id=uuid.uuid4(), version="test", host=host),
            host_id=None,
            allowed=_always,
            key=key,
        ),
    )
    attached = await anext(lines)
    assert isinstance(attached, ProcessLine)
    try:
        yield attached.process
    finally:
        await socket.close()
        await served
        await lines.aclose()


def _agent(key_id: uuid.UUID, *, user_id: uuid.UUID, email: str, role: Role) -> None:
    """Act as an agent holding one API key."""
    install_identity(
        _signed_in(
            make_test_identity(
                user_id=user_id,
                api_key_id=key_id,
                email=email,
                role=role,
            ),
        ),
    )


def _signed_in(identity: AuthIdentity) -> AuthIdentity:
    """Give ``identity`` the hd claim Google gives its login."""
    domain = identity.email.lower().rpartition("@")[2]
    return replace(
        identity,
        hosted_domain=None if domain in CONSUMER_DOMAINS else domain,
    )


async def _always() -> bool:
    """Answer yes: the rule that let the process in always holds."""
    return True


class _OpenSocket:
    """A process socket that sends nothing and stays open until it is closed."""

    def __init__(self) -> None:
        self._closed: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    async def receive(self) -> str | bytes | int:
        return await asyncio.shield(self._closed)

    async def send_text(self, text: str) -> None:
        del text

    async def close(self, code: int = 1000, reason: str = "") -> None:
        del reason
        if not self._closed.done():
            self._closed.set_result(code)
