"""Reading science chats: the sessions of a partner that carry a conversation.

A science chat is an AgentSession the assistant account opened, or the reader's own
``trax helper`` when their canvas chose it, whose ``cli_session_id`` is
``chat:<conversation id>`` and whose labels hold ``science-chat``, and
``poster:<email>`` for each person who posted. Nothing here writes: the partner
records lines, and these are the two reads that find a chat for a person (their
history) and for a conversation id (a link, or a line just posted). A session
carrying the same id under another account's key is not a chat.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Final

import uuid

from pydantic import BaseModel

from trackinizer.lib.codec import from_plain
from trackinizer.server.visuals.chat_forks import may_continue
from trackinizer.wire.wire_science_chat import (
    CHAT_SESSION_PREFIX,
    FORK_EDGE_LABEL,
    SCIENCE_CHAT_LABEL,
    ChatForkAt,
    ChatHead,
    ChatSummary,
    chat_session_id,
    poster_label,
)


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn, DatabaseEngine
    from trackinizer.server.config import Assistant, ChatOrgs


__all__ = [
    "MAX_CHATS",
    "chat_openers",
    "list_chats",
    "read_head",
    "read_starter",
    "science_chat_exists_at",
]


MAX_CHATS: Final = 50
"""Chats the history lists."""

# A conversation holds one session per assistant key that has spoken in it: a rotated
# key opens a new one, since a session is resumed only by the key that opened it. The
# reads name a conversation by its live session, else its newest.
_CHAT: Final = (
    "WITH chat AS (SELECT sess.id AS session_id, "
    "sess.agentsession_cli_session_id AS cli_session_id, "
    "sess.title, sess.account, sess.labels, sess.created, "
    "credential.user_id AS opener_id, "
    "GREATEST(sess.modified, COALESCE(liveness.last_seen, sess.modified)) "
    "AS modified, "
    "(sess.status = 'active' AND sess.agentsession_ended IS NULL) AS live "
    "FROM inquiries AS sess "
    "JOIN api_keys AS credential "
    "ON credential.id = sess.agentsession_opened_by_api_key_id "
    "JOIN users AS opener ON opener.id = credential.user_id "
    "LEFT JOIN session_liveness AS liveness ON liveness.session_id = sess.id "
    "WHERE sess.kind = 'AgentSession' AND opener.email = ANY($1::text[]) "
    "AND sess.agentsession_cli_session_id LIKE 'chat:%' AND $2 = ANY(sess.labels)) "
)

_LISTED: Final = (
    ", mine AS (SELECT DISTINCT cli_session_id FROM chat "
    "WHERE account = $3 OR $4 = ANY(labels)) "
    "SELECT session_id, cli_session_id, title, account, modified, live "
    "FROM (SELECT DISTINCT ON (chat.cli_session_id) chat.* FROM chat "
    "JOIN mine USING (cli_session_id) "
    "ORDER BY chat.cli_session_id, chat.live DESC, chat.created DESC, "
    "chat.session_id DESC) AS latest "
    "ORDER BY modified DESC, session_id DESC LIMIT $5"
)

_HEAD: Final = (
    "SELECT session_id, cli_session_id, title, account, modified, live FROM chat "
    "WHERE cli_session_id = $3 ORDER BY live DESC, created DESC, session_id DESC "
    "LIMIT 1"
)

# A fork edge counts when the one who opened the fork's session is who added it, as
# the audit of its creation records: the assistant links the forks it serves, a helper
# the forks it serves. Any writer may add an edge between any two sessions, or change
# an edge's labels, so the edge as it stands proves nothing; the audit names the key
# that added it and the labels it was added with. Both ends must be science chats an
# allowed key opened. A fork is counted once however many of its conversation's
# sessions hold the edge. A viewer sees the forks served by the sessions they may find:
# the assistant's and their own helper's.
_FORK_EDGES: Final = (
    ", fork_edge AS (SELECT link.from_id, link.to_id, MIN(audit.created) AS added "
    "FROM edges AS link "
    "JOIN change_log AS audit ON audit.kind = 'edge_added' "
    "AND audit.subject_id = link.from_id AND audit.new_peer_id = link.to_id "
    "AND audit.new_peer_edge_kind = 'produced_by' AND $3 = ANY(audit.new_edge_labels) "
    "JOIN api_keys AS author ON author.id = audit.api_key_id "
    "JOIN chat AS forked ON forked.session_id = link.from_id "
    "AND forked.opener_id = author.user_id "
    "WHERE link.edge_kind = 'produced_by' AND $3 = ANY(link.labels) "
    "GROUP BY link.from_id, link.to_id) "
)

_FORKS: Final = (
    "SELECT COUNT(DISTINCT forked.cli_session_id) FROM fork_edge AS link "
    "JOIN chat AS forked ON forked.session_id = link.from_id "
    "JOIN chat AS origin ON origin.session_id = link.to_id "
    "WHERE origin.cli_session_id = $4"
)

_FORKED_FROM: Final = (
    "SELECT origin.cli_session_id FROM fork_edge AS link "
    "JOIN chat AS origin ON origin.session_id = link.to_id "
    "WHERE link.from_id = $4 ORDER BY link.added, link.to_id LIMIT 1"
)

# A line is a person's, or an answer with words: the records the Chat panel shows.
_LINE: Final = (
    "SELECT EXISTS (SELECT 1 FROM session_records AS line "
    "WHERE line.session_id = $3 AND line.part = $4 AND line.idx = $5 "
    "AND (line.kind = 'AgentToAgentMessage' OR (line.kind = 'AssistantMessage' "
    "AND COALESCE(line.payload ->> 'content', '') <> ''))) "
    "AND EXISTS (SELECT 1 FROM chat WHERE session_id = $3)"
)


async def list_chats(
    engine: DatabaseEngine,
    *,
    assistant: Assistant | None,
    email: str,
) -> list[ChatSummary]:
    """List the science chats a person started or posted in, newest first.

    Args:
      engine: Database connection source.
      assistant: The configured assistant, if any; the chats of the person's own
        helper are listed without one.
      email: The person.

    Returns:
      chats: At most :data:`MAX_CHATS` chats, by when they were last heard from.

    """
    async with engine.acquire() as conn:
        rows = await conn.fetch(
            _CHAT + _LISTED,
            chat_openers(assistant, email=email),
            SCIENCE_CHAT_LABEL,
            email,
            poster_label(email),
            MAX_CHATS,
        )
    chats: list[ChatSummary] = []
    for row in rows:
        found = _Row.model_validate(dict(row))
        conversation = _conversation_named(found.cli_session_id)
        if conversation is not None:
            chats.append(
                ChatSummary(
                    conversation_id=conversation,
                    session_id=found.session_id,
                    title=found.title,
                    account=found.account,
                    modified=found.modified,
                ),
            )
    return chats


async def read_head(
    engine: DatabaseEngine,
    *,
    assistant: Assistant | None,
    email: str,
    conversation_id: uuid.UUID,
    orgs: ChatOrgs,
) -> ChatHead | None:
    """Find the session of a conversation.

    Args:
      engine: Database connection source.
      assistant: The configured assistant, if any.
      email: The person asking; the chat of their own helper is found too.
      conversation_id: The conversation.
      orgs: How the server groups its users, which says whether the person's typing
        forks the chat.

    Returns:
      head: The conversation's live session, else its newest, with its starter, how
        often it was forked and what it was forked from; None until its partner has
        opened it.

    """
    openers = chat_openers(assistant, email=email)
    cli_session_id = chat_session_id(conversation_id)
    async with engine.acquire() as conn:
        row = await conn.fetchrow(
            _CHAT + _HEAD,
            openers,
            SCIENCE_CHAT_LABEL,
            cli_session_id,
        )
        if row is None:
            return None
        found = _Row.model_validate(dict(row))
        forks = await conn.fetchval(
            _CHAT + _FORK_EDGES + _FORKS,
            openers,
            SCIENCE_CHAT_LABEL,
            FORK_EDGE_LABEL,
            cli_session_id,
        )
        origin = await conn.fetchval(
            _CHAT + _FORK_EDGES + _FORKED_FROM,
            openers,
            SCIENCE_CHAT_LABEL,
            FORK_EDGE_LABEL,
            found.session_id,
        )
    return ChatHead(
        conversation_id=conversation_id,
        session_id=found.session_id,
        title=found.title,
        account=found.account,
        live=found.live,
        forks=from_plain(forks, int, default=0),
        forked_from=None if origin is None else _conversation_named(str(origin)),
        forks_on_typing=not may_continue(email, starter=found.account, orgs=orgs),
    )


async def read_starter(
    conn: Conn,
    *,
    assistant: Assistant | None,
    conversation_id: uuid.UUID,
) -> str | None:
    """Find who started a conversation, by the oldest session that carries its id.

    Any session may carry a ``chat:<id>`` id, whoever opened it, and a later one proves
    nothing about who began the conversation; so the assistant's sessions come first,
    then the oldest of the rest.

    Args:
      conn: A database connection.
      assistant: The configured assistant, if any.
      conversation_id: The conversation.

    Returns:
      starter: The account of that session; None until some session carries the id.

    """
    starter = await conn.fetchval(
        "SELECT sess.account FROM inquiries AS sess "
        "LEFT JOIN api_keys AS credential "
        "ON credential.id = sess.agentsession_opened_by_api_key_id "
        "LEFT JOIN users AS opener ON opener.id = credential.user_id "
        "WHERE sess.kind = 'AgentSession' AND sess.agentsession_cli_session_id = $1 "
        "ORDER BY COALESCE(opener.email = $2, FALSE) DESC, sess.created, sess.id "
        "LIMIT 1",
        chat_session_id(conversation_id),
        None if assistant is None else assistant.email,
    )
    return from_plain(starter, str, default=None)


async def science_chat_exists_at(
    conn: Conn,
    *,
    assistant: Assistant | None,
    email: str,
    fork: ChatForkAt,
) -> bool:
    """Say whether ``fork`` names a line of a science chat's session.

    Args:
      conn: A database connection.
      assistant: The configured assistant, if any.
      email: The person asking; the chat of their own helper is found too.
      fork: The session, part and position.

    Returns:
      found: True when the session is a science chat the person may find and the
        record is a person's line or an answer with words.

    """
    return bool(
        await conn.fetchval(
            _CHAT + _LINE,
            chat_openers(assistant, email=email),
            SCIENCE_CHAT_LABEL,
            fork.session_id,
            fork.part,
            fork.idx,
        ),
    )


def chat_openers(assistant: Assistant | None, *, email: str) -> list[str]:
    """Name the accounts whose chats a person may find: the assistant's and their own.

    Args:
      assistant: The configured assistant, if any.
      email: The person.

    Returns:
      emails: The accounts a science chat's session may have been opened by.

    """
    return [email] if assistant is None else [assistant.email, email]


def _conversation_named(cli_session_id: str) -> uuid.UUID | None:
    """Read the conversation id out of a ``chat:<id>`` session id."""
    try:
        return uuid.UUID(cli_session_id.removeprefix(CHAT_SESSION_PREFIX))
    except ValueError:
        return None


class _Row(BaseModel):
    """A science chat's session row, as the reads select it."""

    session_id: uuid.UUID
    cli_session_id: str
    title: str
    account: str
    modified: datetime
    live: bool
