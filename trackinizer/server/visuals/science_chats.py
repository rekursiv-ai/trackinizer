"""Reading science chats: the sessions of a partner that carry a conversation.

A science chat is an AgentSession the assistant account opened, whose
``cli_session_id`` is ``chat:<conversation id>`` and whose labels hold
``science-chat``, and ``poster:<email>`` for each person who posted. Nothing here
writes: the partner records lines, and these reads find a chat for a conversation id
(a link, or a line just posted), who it is shared with, what the reader may do to it,
and which of its records a fork of it copies. A session carrying the same id under
another account's key is not a chat.
"""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import groupby
from typing import TYPE_CHECKING, Final

import uuid

from pydantic import BaseModel
from treekle import from_plain

from trackinizer.server.audience import (
    chat_beginnings_sql,
    may_hide,
    members_reader,
    reader_for,
    reads_session,
    visible_sql,
)
from trackinizer.server.store.deleted_chats import chat_was_deleted
from trackinizer.server.store.session_ir import PartPrefix
from trackinizer.server.values import vetted_sql
from trackinizer.server.visuals.chat_forks import (
    delete_refusal,
    may_continue,
    may_share,
)
from trackinizer.types.errors import ChatDeletedError
from trackinizer.wire.audience import OPEN, PRIVATE
from trackinizer.wire.wire_science_chat import (
    FORK_EDGE_LABEL,
    SCIENCE_CHAT_LABEL,
    ChatForkAt,
    ChatHead,
    chat_conversation_id,
    chat_session_id,
)


if TYPE_CHECKING:
    from collections.abc import Sequence

    from trackinizer.lib.postgres import Conn, DatabaseEngine
    from trackinizer.server.audience import SessionReader
    from trackinizer.server.auth import AuthIdentity
    from trackinizer.server.config import Assistant, ChatOrgs
    from trackinizer.server.inbound import InboundQueue


__all__ = [
    "Conversation",
    "chat_openers",
    "chat_readers",
    "conversation_of_session",
    "counts_in_chat",
    "may_post",
    "read_chat_sessions",
    "read_conversation",
    "read_first_session",
    "read_fork_prefixes",
    "read_head",
    "read_line_starter",
    "read_shares",
    "read_starter",
    "science_chat_exists_at",
]


# A conversation holds one session per assistant key that has spoken in it: a rotated
# key opens a new one, since a session is resumed only by the key that opened it. The
# reads name a conversation by its live session, else its newest.
_CHAT: Final = (
    "WITH chat AS (SELECT sess.id AS session_id, "
    "sess.agentsession_cli_session_id AS cli_session_id, "
    "sess.agentsession_audience AS audience, "
    "sess.title, sess.account, sess.labels, sess.created, "
    "sess.agentsession_opened_by_api_key_id AS opener_key, "
    "credential.user_id AS opener_id, opener.email AS opener_email, "
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

# The conversation is the sessions the assistant ($5) opened under its id, whenever it
# has any; without one, those its starter's ($6) own key opened. Any
# writer's key may open a session under a ``chat:`` id, and one that did gains nothing
# from it. Beside the head, of every session under the id, which a delete takes: their
# ids; whether the caller's key ($4) opened each; and whether a person's line in one
# the assistant or the starter's key opened names a sender other than the starter, so
# lines a stranger writes into a session of their own neither count nor block. A
# fork's copies of its original's lines keep their senders, so they count as their
# original posters'. Lines are read only off a session whose audience the caller reads
# ($7); one they do not read counts as someone else's, so its starter cannot delete it
# unseen.
# Audience: gated -- a session's lines are read only when its audience is one of $7.
_HEAD: Final = (
    ", conversation AS (SELECT * FROM chat WHERE cli_session_id = $3 "
    "AND (opener_email = $5::text OR (lower(opener_email) = lower($6::text) "
    "AND NOT EXISTS (SELECT 1 FROM chat AS assistant "
    "WHERE assistant.cli_session_id = $3 AND assistant.opener_email = $5::text)))) "
    ", taken AS (SELECT sess.id, sess.agentsession_audience AS audience, "
    "COALESCE(sess.agentsession_opened_by_api_key_id = $4::uuid, FALSE) AS by_caller, "
    "COALESCE(opener.email = $5::text OR lower(opener.email) = lower($6::text), "
    "FALSE) AS counted "
    "FROM inquiries AS sess LEFT JOIN api_keys AS credential "
    "ON credential.id = sess.agentsession_opened_by_api_key_id "
    "LEFT JOIN users AS opener ON opener.id = credential.user_id "
    "WHERE sess.kind = 'AgentSession' AND sess.agentsession_cli_session_id = $3) "
    "SELECT head.session_id, head.cli_session_id, head.title, "
    "head.modified, head.live, head.audience, "
    "ARRAY(SELECT taken.id FROM taken ORDER BY taken.id) AS sessions, "
    "(SELECT bool_and(taken.by_caller) FROM taken) AS opened, "
    "(EXISTS (SELECT 1 FROM taken AS hidden WHERE hidden.counted "
    "AND NOT COALESCE(hidden.audience = ANY($7::text[]), FALSE)) "
    "OR EXISTS (SELECT 1 FROM taken AS held "
    "JOIN session_records AS line ON line.session_id = held.id "
    "WHERE held.counted AND held.audience = ANY($7::text[]) "
    "AND (line.kind = 'AgentToAgentMessage' OR (line.kind = 'UserMessage' "
    "AND (line.payload -> 'extra' -> '$inbound' IS NOT NULL "
    "OR line.payload -> 'extra' ->> 'author' IS NOT NULL))) "
    "AND lower(COALESCE(line.payload ->> 'sender', "
    "line.payload -> 'extra' -> '$inbound' ->> 'source', "
    "line.payload -> 'extra' ->> 'author', '')) <> lower($6::text))) "
    "AS others_posted "
    "FROM conversation AS head "
    "ORDER BY head.live DESC, head.created DESC, head.session_id DESC LIMIT 1"
)

# The session that began the conversation $1, by the one rule for it, the assistant
# being $2. Only a session start names a session's ``chat:`` id, which is what the rule
# rests on. The science-chat label is no test of it: the assistant labels a chat's
# session after it starts it, so the first session may be unlabelled while a later
# squat already carries the label.
_FIRST_SESSION: Final = vetted_sql(
    chat_beginnings_sql(assistant="$2"),
    " AND began.agentsession_cli_session_id = $1 "
    "ORDER BY began.created, began.id LIMIT 1",
)

# A fork edge counts when the one who opened the fork's session is who added it, as
# the audit of its creation records: the assistant links the forks it serves. Any writer
# may add an edge between any two sessions, or change
# an edge's labels, so the edge as it stands proves nothing; the audit names the key
# that added it and the labels it was added with. Both ends must be science chats an
# allowed key opened. A fork is counted once however many of its conversation's
# sessions hold the edge. A viewer sees the forks served by the sessions they may find:
# the assistant's and their own.
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


class Conversation(BaseModel):
    """A science chat's head session, its other sessions, and what the caller did."""

    session_id: uuid.UUID
    """The live session, else the newest."""

    cli_session_id: str
    title: str
    account: str
    """Who started the conversation, as :func:`read_starter` finds them."""

    modified: datetime
    live: bool
    audience: str
    """The head session's audience, which every session under its id shares."""

    sessions: list[uuid.UUID]
    """Every session under the conversation's id, whoever opened it."""

    opened: bool
    """Whether the caller's key opened every session under its id."""

    others_posted: bool
    """Whether a person's line in a session under its id that the assistant or the
    starter's key opened is not the starter's, or such a session's audience is one the
    caller does not read."""


async def read_head(
    engine: DatabaseEngine,
    *,
    assistant: Assistant | None,
    caller: AuthIdentity,
    conversation_id: uuid.UUID,
    reader: SessionReader,
) -> ChatHead | None:
    """Find the session of a conversation.

    Args:
      engine: Database connection source.
      assistant: The configured assistant, if any.
      caller: The person asking; their own chats are found too, and what they may do
        to the chat is said for them.
      conversation_id: The conversation.
      reader: The caller, as the transcript test reads them, which says whether they
        read the chat.

    Returns:
      head: The conversation's live session, else its newest, with its starter, how
        often it was forked, what it was forked from, its audience, who it is shared
        with, and whether the caller reads it, may post into, hide, share or delete
        it; None until its partner has opened it.

    Raises:
      ChatDeletedError: The conversation was deleted.

    """
    openers = chat_openers(assistant, email=caller.email)
    async with engine.acquire() as conn:
        found = await read_conversation(
            conn,
            assistant=assistant,
            caller=caller,
            conversation_id=conversation_id,
        )
        if found is None:
            if await chat_was_deleted(conn, conversation_id=conversation_id):
                raise ChatDeletedError("This chat was deleted")
            return None
        forks = await conn.fetchval(
            _CHAT + _FORK_EDGES + _FORKS,
            openers,
            SCIENCE_CHAT_LABEL,
            FORK_EDGE_LABEL,
            found.cli_session_id,
        )
        origin = await conn.fetchval(
            _CHAT + _FORK_EDGES + _FORKED_FROM,
            openers,
            SCIENCE_CHAT_LABEL,
            FORK_EDGE_LABEL,
            found.session_id,
        )
        shares = await read_shares(conn, conversation_id=conversation_id)
        reads = await reads_session(conn, reader=reader, session_id=found.session_id)
    admin = caller.role == "admin"
    sharer = may_share(caller.email, starter=found.account, admin=admin, shared=True)
    unsharer = may_share(caller.email, starter=found.account, admin=admin, shared=False)
    return ChatHead(
        conversation_id=conversation_id,
        session_id=found.session_id,
        title=found.title,
        account=found.account,
        live=found.live,
        forks=from_plain(forks, int, default=0),
        forked_from=None if origin is None else chat_conversation_id(str(origin)),
        forks_on_typing=not reads,
        may_post=reads
        and may_continue(
            caller.email,
            starter=found.account,
            shared_with=shares,
            org=reader.org,
        ),
        audience=found.audience,
        may_hide=may_hide(
            caller.email,
            owners=(found.account,),
            audience=found.audience,
        ),
        shared_with=shares if unsharer else [],
        may_share=sharer,
        may_delete=delete_refusal(
            caller.email,
            starter=found.account,
            admin=admin,
            opened=found.opened,
            others_posted=found.others_posted,
        )
        is None,
    )


async def read_conversation(
    conn: Conn,
    *,
    assistant: Assistant | None,
    caller: AuthIdentity,
    conversation_id: uuid.UUID,
) -> Conversation | None:
    """Find a conversation's sessions, its starter, and who posted in it.

    Args:
      conn: A database connection.
      assistant: The configured assistant, if any.
      caller: The person asking; their own chats are found too.
      conversation_id: The conversation.

    Returns:
      conversation: Its head and sessions; None until its partner has opened it.

    """
    starter = await read_starter(
        conn,
        assistant=assistant,
        conversation_id=conversation_id,
    )
    if starter is None:
        return None
    row = await conn.fetchrow(
        _CHAT + _HEAD,
        chat_openers(assistant, email=caller.email),
        SCIENCE_CHAT_LABEL,
        chat_session_id(conversation_id),
        caller.api_key_id,
        None if assistant is None else assistant.email,
        starter,
        sorted(caller.read_audiences),
    )
    if row is None:
        return None
    return Conversation.model_validate(dict(row) | {"account": starter})


async def read_shares(conn: Conn, *, conversation_id: uuid.UUID) -> list[str]:
    """Return the emails a conversation is shared with, oldest share first.

    Args:
      conn: A database connection.
      conversation_id: The conversation.

    Returns:
      shared_with: Lowercase emails; empty when it is shared with nobody.

    """
    rows = await conn.fetch(
        "SELECT email FROM science_chat_shares WHERE conversation_id = $1 "
        "ORDER BY shared_at, email",
        conversation_id,
    )
    return [from_plain(row["email"], str) for row in rows]


async def conversation_of_session(
    conn: Conn,
    *,
    session_id: uuid.UUID,
) -> uuid.UUID | None:
    """Name the science chat a session holds, as a fork point names its session.

    Args:
      conn: A database connection.
      session_id: The session.

    Returns:
      conversation_id: The ``<id>`` of its ``chat:<id>``; None for a session that is
        gone or holds no chat.

    """
    return chat_conversation_id(
        from_plain(
            await conn.fetchval(
                "SELECT agentsession_cli_session_id FROM inquiries "
                "WHERE id = $1 AND kind = 'AgentSession'",
                session_id,
            ),
            str,
            default=None,
        ),
    )


async def read_starter(
    conn: Conn,
    *,
    assistant: Assistant | None,
    conversation_id: uuid.UUID,
) -> str | None:
    """Find who started a conversation, by the oldest session that began it.

    That is the oldest session under its ``chat:<id>`` id that the assistant opened, or
    that its own account's key opened. This is the one rule
    for a chat's starter: a post, the head, a share and a delete all ask it, so a
    session a writer's own key opened under the id later, or one the assistant opened
    later for a poster it was shared with, never makes anyone else the starter.

    Args:
      conn: A database connection.
      assistant: The configured assistant, if any.
      conversation_id: The conversation.

    Returns:
      starter: The account of that session; None until some session carries the id.

    """
    starter = await conn.fetchval(
        "SELECT began.account " + _FIRST_SESSION,
        chat_session_id(conversation_id),
        None if assistant is None else assistant.email,
    )
    return from_plain(starter, str, default=None)


async def read_first_session(
    conn: Conn,
    *,
    assistant: Assistant | None,
    conversation_id: uuid.UUID,
) -> uuid.UUID | None:
    """Find the session that began a conversation, as :func:`read_starter` finds it.

    Args:
      conn: A database connection.
      assistant: The configured assistant, if any.
      conversation_id: The conversation.

    Returns:
      session_id: That session; None until some session carries the id.

    """
    return from_plain(
        await conn.fetchval(
            "SELECT began.id " + _FIRST_SESSION,
            chat_session_id(conversation_id),
            None if assistant is None else assistant.email,
        ),
        uuid.UUID,
        default=None,
    )


async def chat_readers(
    conn: Conn,
    *,
    assistant: Assistant | None,
    conversation_id: uuid.UUID,
    orgs: ChatOrgs,
) -> list[SessionReader]:
    """Describe everyone who reads a chat's lines, for what may be copied into it.

    Its starter and each person it is shared with, as the assistant reads for them
    (``delegated``) and holding what every person holds: a copy into the chat reaches
    them whatever they oversee, so it may hold only what each of them reads as
    anyone. And, while the chat is not ``private``, everyone it is open to by the
    organisation mode, as one reader
    (:func:`~trackinizer.server.audience.members_reader`).

    Args:
      conn: A database connection.
      assistant: The configured assistant, if any.
      conversation_id: The chat.
      orgs: The server's organisation mode.

    Returns:
      readers: One per person, then the one for those it is open to; empty for a
        conversation no session carries.

    """
    began = await conn.fetchrow(
        "SELECT began.account, began.agentsession_audience AS audience "
        + _FIRST_SESSION,
        chat_session_id(conversation_id),
        None if assistant is None else assistant.email,
    )
    if began is None:
        return []
    starter = from_plain(began["account"], str).lower()
    people = [starter, *await read_shares(conn, conversation_id=conversation_id)]
    rows = await conn.fetch(
        "SELECT lower(email) AS email, hosted_domain FROM users "
        "WHERE lower(email) = ANY($1::text[])",
        people,
    )
    claims = {
        from_plain(row["email"], str): from_plain(
            row["hosted_domain"],
            str,
            default=None,
        )
        for row in rows
    }
    named = "" if assistant is None else assistant.email
    readers = [
        reader_for(
            person,
            audiences=frozenset({OPEN, PRIVATE}),
            admin=False,
            orgs=orgs,
            assistant=named,
            hosted_domain=claims.get(person),
            delegated=True,
        )
        for person in dict.fromkeys(people)
    ]
    members = members_reader(starter, orgs=orgs, assistant=named)
    if members is None or began["audience"] == PRIVATE:
        return readers
    return [*readers, members]


async def read_chat_sessions(
    conn: Conn,
    *,
    conversation_id: uuid.UUID,
) -> list[uuid.UUID]:
    """Return every session under a conversation's id, whoever opened it.

    Args:
      conn: A database connection.
      conversation_id: The conversation.

    Returns:
      session_ids: In id order; empty for a conversation no session carries.

    """
    rows = await conn.fetch(
        "SELECT id FROM inquiries WHERE kind = 'AgentSession' "
        "AND agentsession_cli_session_id = $1 ORDER BY id",
        chat_session_id(conversation_id),
    )
    return [from_plain(row["id"], uuid.UUID) for row in rows]


# The head's rule for whose lines count: a stranger's key that writes a person's line
# into a session of its own under the id neither posts it nor clears one on its way in.
async def counts_in_chat(
    conn: Conn,
    *,
    assistant: Assistant | None,
    session_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> bool:
    """Say whether the person's lines a session records count as posted in its chat.

    Args:
      conn: A database connection.
      assistant: The configured assistant, if any.
      session_id: A session under the conversation's ``chat:<id>``.
      conversation_id: The conversation.

    Returns:
      counted: True when the assistant, or a key of the chat's starter, opened it.

    """
    opener = from_plain(
        await conn.fetchval(
            "SELECT opener.email FROM inquiries AS sess JOIN api_keys AS credential "
            "ON credential.id = sess.agentsession_opened_by_api_key_id "
            "JOIN users AS opener ON opener.id = credential.user_id "
            "WHERE sess.id = $1 AND sess.kind = 'AgentSession'",
            session_id,
        ),
        str,
        default=None,
    )
    if opener is None:
        return False
    if assistant is not None and opener == assistant.email:
        return True
    starter = await read_starter(
        conn,
        assistant=assistant,
        conversation_id=conversation_id,
    )
    return starter is not None and opener.lower() == starter.lower()


async def read_line_starter(
    conn: Conn,
    *,
    assistant: Assistant | None,
    conversation_id: uuid.UUID,
    inbound: InboundQueue,
) -> str | None:
    """Find who started the conversation a new line names, its session opened or not.

    The database answers first. The first line of a conversation is queued before any
    session carries its id, so until one does, the sender of the send that used the id
    as its key started it. Only then: that key map is in memory and global, so after a
    restart, or once an entry is evicted, any writer can plant a chat's id there as the
    key of a send of their own.

    Args:
      conn: A database connection.
      assistant: The configured assistant, if any.
      conversation_id: The conversation.
      inbound: Session message queue, which remembers each send's key and sender.

    Returns:
      starter: Their email; None for a conversation nobody has begun.

    """
    return await read_starter(
        conn,
        assistant=assistant,
        conversation_id=conversation_id,
    ) or inbound.sender_of(conversation_id)


async def may_post(
    conn: Conn,
    *,
    poster: str,
    reader: SessionReader,
    conversation_id: uuid.UUID,
    assistant: Assistant | None,
    inbound: InboundQueue,
) -> bool:
    """Say whether ``poster`` may add a line to a conversation, by Chat or by a relay.

    A chat's starter, the people they share it with and, on a ``domain`` server, the
    starter's organisation post into it while they read the session that began it
    (:func:`~trackinizer.server.visuals.chat_forks.may_continue`); any other
    reader forks it. A conversation no session carries yet is its first line's
    sender's.

    Args:
      conn: A database connection.
      poster: The attested email of the person whose line it is.
      reader: The poster, as the transcript test reads them, whose organisation
        is theirs.
      conversation_id: The conversation.
      assistant: The configured assistant, if any.
      inbound: Session message queue, which knows the sender of a first line.

    Returns:
      allowed: True when the poster continues the conversation by
        :func:`~trackinizer.server.visuals.chat_forks.may_continue` and reads
        the session that began it; or when none has and no one else sent its first
        line.

    """
    began = await conn.fetchrow(
        "SELECT began.id, began.account " + _FIRST_SESSION,
        chat_session_id(conversation_id),
        None if assistant is None else assistant.email,
    )
    if began is None:
        sender = inbound.sender_of(conversation_id)
        return sender is None or sender.lower() == poster.lower()
    return may_continue(
        poster,
        starter=from_plain(began["account"], str),
        shared_with=await read_shares(conn, conversation_id=conversation_id),
        org=reader.org,
    ) and await reads_session(
        conn,
        reader=reader,
        session_id=from_plain(began["id"], uuid.UUID),
    )


async def science_chat_exists_at(
    conn: Conn,
    *,
    assistant: Assistant | None,
    email: str,
    fork: ChatForkAt,
    reader: SessionReader,
) -> bool:
    """Say whether ``fork`` names a line of a science chat's session.

    A line is a person's, or an answer with words: the records the Chat panel shows.
    A person's line is an agent-to-agent message, or a user turn that carries its
    sender: the ``$inbound`` stamp the agent runner puts on a line it delivered, or an
    ``author``. The assistant copies the lines up to a fork point into the fork, so the
    point must be in a transcript the person reads. For any other the answer is the no
    that a position past the end gets, and a caller cannot tell a hidden line from a
    missing one.

    Args:
      conn: A database connection.
      assistant: The configured assistant, if any.
      email: The person asking; their own chats are found too.
      fork: The session, part and position.
      reader: The person, as the transcript test reads them.

    Returns:
      found: True when the session is a science chat the person may find and read
        and the record is a person's line or an answer with words.

    """
    params: list[object] = [
        chat_openers(assistant, email=email),
        SCIENCE_CHAT_LABEL,
        fork.session_id,
        fork.part,
        fork.idx,
    ]
    # Audience: gated -- the line is looked for only in a transcript ``reader`` reads.
    readable = visible_sql("target", params=params, reader=reader)
    return bool(
        await conn.fetchval(
            vetted_sql(
                _CHAT,
                "SELECT EXISTS (SELECT 1 FROM session_records AS line "
                "WHERE line.session_id = $3 AND line.part = $4 AND line.idx = $5 "
                "AND (line.kind = 'AgentToAgentMessage' OR (line.kind = "
                "'UserMessage' AND (line.payload -> 'extra' -> '$inbound' IS NOT NULL "
                "OR line.payload -> 'extra' ->> 'author' IS NOT NULL)) "
                "OR (line.kind = 'AssistantMessage' AND COALESCE("
                "line.payload ->> 'content', '') <> ''))) "
                "AND EXISTS (SELECT 1 FROM chat WHERE session_id = $3) "
                "AND EXISTS (SELECT 1 FROM inquiries AS target WHERE target.id = $3 "
                "AND ",
                readable,
                ")",
            ),
            *params,
        ),
    )


# Audience: gated -- a fork's route checks its forker reads the session
# (``science_chat_exists_at``) before it asks which records the fork copies.
async def read_fork_prefixes(conn: Conn, *, fork: ChatForkAt) -> list[PartPrefix]:
    """Say how much of each part of a chat's session a fork from one of its lines copies.

    A fork holds what the conversation held up to and including the fork point, in
    the order :func:`~trackinizer.client.conversation.placed` orders a
    session's parts: by when each record happened, ties going to the lower part. The
    fork point's part is copied through the fork point; each other part is copied up
    to its first record that comes after it, so every part copied is a prefix that
    keeps its records' positions. A part with no format is skipped, as the reader
    skips it: it is a terminal scrape, not a conversation.

    Args:
      conn: A database connection.
      fork: The session, part and position of the last line copied.

    Returns:
      prefixes: The parts copied, in part order, each with how many of its first
        records; empty when the fork point is no record of the session.

    """
    rows = await conn.fetch(
        "SELECT r.part, r.idx, r.timestamp FROM session_records AS r "
        "JOIN session_manifests AS m ON m.session_id = r.session_id "
        "AND m.part = r.part WHERE r.session_id = $1 AND r.idx < m.records "
        "AND (m.format <> '' OR m.part = $2) ORDER BY r.part, r.idx",
        fork.session_id,
        fork.part,
    )
    return _fork_prefixes(
        [
            (
                from_plain(row["part"], int),
                from_plain(row["idx"], int),
                from_plain(row["timestamp"], datetime, default=None),
            )
            for row in rows
        ],
        fork=fork,
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


def _fork_prefixes(
    rows: Sequence[tuple[int, int, datetime | None]],
    *,
    fork: ChatForkAt,
) -> list[PartPrefix]:
    """Return each part's prefix that the reader orders at or before the fork point."""
    parts = {
        part: _moments([(idx, stamp) for _, idx, stamp in held])
        for part, held in groupby(rows, key=lambda row: row[0])
    }
    point = next(
        (moment for idx, moment in parts.get(fork.part, []) if idx == fork.idx),
        None,
    )
    if point is None:
        return []
    prefixes: list[PartPrefix] = []
    for part, held in parts.items():
        after = (
            fork.idx + 1
            if part == fork.part
            else next(
                (idx for idx, moment in held if (moment, part) > (point, fork.part)),
                held[-1][0] + 1,
            )
        )
        if after:
            prefixes.append(PartPrefix(part=part, records=after))
    return prefixes


# The reader's rule (``client/conversation.py::_moments``): a record with no stamp takes
# the one before it in its part, and a leading one the part's first stamp, so a part's
# own order holds whatever the other parts did.
def _moments(
    stamps: Sequence[tuple[int, datetime | None]],
) -> list[tuple[int, datetime]]:
    """Return each record of one part with when the reader takes it to have happened."""
    last = next(
        (stamp for _, stamp in stamps if stamp is not None),
        datetime.min.replace(tzinfo=UTC),
    )
    out: list[tuple[int, datetime]] = []
    for idx, stamp in stamps:
        last = last if stamp is None else stamp
        out.append((idx, last))
    return out
