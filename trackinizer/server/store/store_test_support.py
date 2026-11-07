"""Helpers the Postgres-backed store tests share, imported by tests, never collected."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Final, Protocol
from uuid import NAMESPACE_URL, uuid4, uuid5

from treekle import immutable

from trackinizer.sagent.session.types import UserMessage
from trackinizer.server.audience import SessionReader
from trackinizer.server.store.session_feed import FeedScope
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.audience import AUDIENCES
from trackinizer.wire.bodies import SubmitAgentSession


if TYPE_CHECKING:
    from uuid import UUID

    from trackinizer.server.store.core import Store


__all__ = [
    "EVERY_SESSION",
    "append_user_message",
    "fetch_value",
    "last_seen",
    "open_agent_session",
    "polled_session",
    "seed_active_user",
]


EVERY_SESSION: Final = FeedScope(
    reader=SessionReader(audiences=AUDIENCES, every_session=True, oversight=True),
)
"""Reads every session but a chat: the feed tests are of what it keeps, not who reads."""


# A record's key is its POSITION in its source file, so the caller states the ``idx``
# rather than relying on a server-side counter. The MANIFEST is written too, because a
# read is bounded by it: a part without one reads as empty, rows with no manifest being
# a torn write.
async def append_user_message(
    store: Store,
    session_id: UUID,
    *,
    idx: int,
    text: str,
) -> None:
    """Append one user message to a session's part 0, bounded by its manifest.

    Args:
      store: The store the session lives in.
      session_id: The session to append to.
      idx: The message's position in the part; the manifest then bounds ``idx + 1``.
      text: The message.

    """
    _ = await store.upsert_session_manifest(
        session_id,
        name="part0.jsonl",
        metadata=immutable({}),
        ir_id=uuid5(NAMESPACE_URL, f"{session_id}:0"),
        format="claude",
        records=idx + 1,
    )
    await store.append_session_records(
        session_id,
        [
            SessionRecordRow.of(
                session_id=session_id,
                part=0,
                idx=idx,
                record=UserMessage(content=text),
            ),
        ],
    )


# The session-start route resolves the request's ``account`` from the authenticated
# identity's email and rejects (422) any email that is not an active user. Route tests
# inject an identity but truncate the ``users`` table per test, so each must seed its
# own principal first.
async def seed_active_user(store: Store, email: str) -> None:
    """Seed ``email`` as an ACTIVE ``users`` row so session routes accept it.

    Args:
      store: The store whose ``users`` table takes the row.
      email: The account to seed; an existing row of that email is left as it is.

    """
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, $2, 'u', 'writer', 'active') "
            "ON CONFLICT (email) DO NOTHING",
            uuid4(),
            email,
        )


async def open_agent_session(
    store: Store,
    *,
    cli_session_id: str | None = None,
) -> UUID:
    """Open an open-audience session, or resume the one ``cli_session_id`` names.

    Args:
      store: The store to open it in.
      cli_session_id: The CLI's own id; one a session already holds resumes it.

    Returns:
      session_id: The session opened or resumed.

    """
    session_id, _, _ = await store.start_session(
        SubmitAgentSession(
            title="reaper",
            cli="claude",
            account="t@e",
            cli_session_id=cli_session_id,
        ),
        requested_actor="agent",
        audience="open",
    )
    return session_id


async def polled_session(
    store: Store,
    *,
    at: datetime,
    cli_session_id: str | None = None,
) -> UUID:
    """Open a session that polled its inbound queue at ``at``.

    Args:
      store: The store to open it in.
      at: When it polled.
      cli_session_id: The CLI's own id, as :func:`open_agent_session` takes it.

    Returns:
      session_id: The session, watched for silence from ``at``.

    """
    session_id = await open_agent_session(store, cli_session_id=cli_session_id)
    await store.record_session_seen(session_id, at=at, polled=True)
    return session_id


async def last_seen(store: Store, session_id: UUID) -> datetime:
    """Return when ``session_id`` was last recorded as heard from.

    Args:
      store: The store the session lives in.
      session_id: The session whose liveness row is read.

    Returns:
      seen: Its ``session_liveness.last_seen``.

    """
    async with store.engine.acquire() as conn:
        seen = await conn.fetchval(
            "SELECT last_seen FROM session_liveness WHERE session_id = $1",
            session_id,
        )
    assert isinstance(seen, datetime)
    return seen


async def fetch_value(conn: _FetchValConnection, query: str, *args: object) -> object:
    """Name the scalar as an object at asyncpg's untyped boundary."""
    return await conn.fetchval(query, *args)


class _FetchValConnection(Protocol):
    """The asyncpg connection slice used by scalar assertions."""

    async def fetchval(self, query: str, *args: object) -> object: ...
