"""Sharing a science chat with a person, and deleting a whole chat.

A share names one person by email, who then reads and posts into the chat as its
starter does. It follows the conversation, whatever sessions it holds, and lives in
``science_chat_shares``. The starter adds and removes shares; an admin only removes
them. A ``staff`` chat is shared only with a person who holds ``staff``: its
assistant read a staff session into it.

Deleting a conversation takes every session under its id, whoever opened it, with
their records, its shares, and its binding to any station, whose next line then
starts a new chat. Each such station moves to a new revision, as any station write
does: a browser orders canvas frames by revision, and an open tab would otherwise keep
the deleted chat. Its forks keep their copies of its lines; their edge to it goes with
its sessions, so their ``forked_from`` reads as none. A tombstone keeps its id, under
which no chat starts again. The change log keeps a row for each change its sessions
had, but no title or description any held: nothing enforces that log as append-only,
and a deleted chat's words should not outlive it there. :mod:`chat_forks` says who
may share and who may delete.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import uuid

from treekle import from_plain

from trackinizer.server.audience import audiences_for
from trackinizer.server.notify import notify_after_commit, tx
from trackinizer.server.store.deleted_chats import lock_chat_id
from trackinizer.server.visuals.chat_forks import (
    ChatClosedError,
    ChatRefusedError,
    delete_refusal,
    may_share,
)
from trackinizer.server.visuals.science_chats import (
    read_conversation,
    read_shares,
)
from trackinizer.wire.audience import STAFF
from trackinizer.wire.wire_science_chat import chat_session_id


if TYPE_CHECKING:
    from collections.abc import Sequence

    from trackinizer.lib.postgres import Conn, DatabaseEngine
    from trackinizer.server.auth import AuthIdentity
    from trackinizer.server.config import Assistant
    from trackinizer.server.inbound import InboundQueue
    from trackinizer.server.store.core import Store


__all__ = [
    "UnboundStation",
    "delete_chat",
    "set_share",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class UnboundStation:
    """A station that held a deleted chat and now holds none, and its owner."""

    workspace_id: uuid.UUID
    user_id: uuid.UUID


async def set_share(
    engine: DatabaseEngine,
    *,
    assistant: Assistant | None,
    caller: AuthIdentity,
    conversation_id: uuid.UUID,
    email: str,
    shared: bool,
    staff: Sequence[str],
) -> list[str] | None:
    """Share a chat with one person, or stop sharing it; either again changes nothing.

    Args:
      engine: Database connection source.
      assistant: The configured assistant, if any.
      caller: Who asks: the chat's starter, or an admin stopping a share.
      conversation_id: The chat.
      email: The person, stored lowercase.
      shared: Share it with them; False stops sharing it.
      staff: The configured staff patterns, which say whether the person holds
        ``staff``.

    Returns:
      shared_with: The chat's shares after, oldest first; None when the caller finds
        no such chat.

    Raises:
      ChatRefusedError: The caller is not the chat's starter, and is not an admin
        stopping a share.
      ChatClosedError: The chat is ``staff`` and the person, an active user or not,
        does not hold ``staff`` now.

    """
    async with engine.acquire() as conn, tx(conn):
        found = await read_conversation(
            conn,
            assistant=assistant,
            caller=caller,
            conversation_id=conversation_id,
        )
        if found is None:
            return None
        if not may_share(
            caller.email,
            starter=found.account,
            admin=caller.role == "admin",
            shared=shared,
        ):
            raise ChatRefusedError("chat_not_starter")
        if (
            shared
            and found.audience == STAFF
            and STAFF not in await _holds(conn, email=email, staff=staff)
        ):
            raise ChatClosedError("chat_staff_only")
        if shared:
            await conn.execute(
                "INSERT INTO science_chat_shares (conversation_id, email, shared_by) "
                "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                conversation_id,
                email.lower(),
                caller.email,
            )
        else:
            await conn.execute(
                "DELETE FROM science_chat_shares "
                "WHERE conversation_id = $1 AND email = $2",
                conversation_id,
                email.lower(),
            )
        return await read_shares(conn, conversation_id=conversation_id)


async def delete_chat(
    store: Store,
    *,
    assistant: Assistant | None,
    caller: AuthIdentity,
    conversation_id: uuid.UUID,
    inbound: InboundQueue,
) -> list[UnboundStation] | None:
    """Delete a chat: its sessions and their records, its shares, its stations' hold.

    Each session under its id is purged as ``DELETE /api/inquiries/<uuid>`` purges a
    row, with its audit and its edges' audits, in one transaction with the rest.
    Their change log rows lose the titles and descriptions they held, and a tombstone
    keeps its id from starting a chat again. A line on its way in, queued or taken by
    the assistant and not yet recorded, counts as posted, so its starter may not
    delete a chat another person has a line on the way into; whatever line is queued
    goes with it, so none reaches the assistant.

    Args:
      store: The store, whose purge deletes each session.
      assistant: The configured assistant, if any.
      caller: Who asks.
      conversation_id: The chat.
      inbound: Session message queue, which holds the chat's lines not yet read.

    Returns:
      stations: The stations that held the chat, none of which holds it now; None
        when the caller finds no such chat.

    Raises:
      ChatRefusedError: The caller may not delete it.

    """
    async with notify_after_commit(), store.engine.acquire() as conn, tx(conn):
        # The id is held first: a session start, a line or a station bound under it
        # finishes before this reads what it takes, or waits and finds the tombstone.
        await lock_chat_id(conn, conversation_id=conversation_id)
        # Locked before they are read: a line recorded meanwhile, which locks its
        # session too, would otherwise land after the check that no one else posted.
        await conn.execute(
            "SELECT id FROM inquiries WHERE kind = 'AgentSession' "
            "AND agentsession_cli_session_id = $1 ORDER BY id FOR UPDATE",
            chat_session_id(conversation_id),
        )
        found = await read_conversation(
            conn,
            assistant=assistant,
            caller=caller,
            conversation_id=conversation_id,
        )
        if found is None:
            return None
        starter = found.account.lower()
        queued = inbound.conversation_senders(conversation_id)
        refusal = delete_refusal(
            caller.email,
            starter=found.account,
            admin=caller.role == "admin",
            opened=found.opened,
            others_posted=found.others_posted
            or any((sender or "").lower() != starter for sender in queued),
        )
        if refusal is not None:
            raise ChatRefusedError(refusal)
        for session_id in found.sessions:
            await store.purge_held(
                conn,
                session_id,
                kind="AgentSession",
                api_key_id=caller.api_key_id,
                actor=caller.email,
                reason="Deleted with its science chat",
            )
        await conn.execute(
            "UPDATE change_log SET old_title = CASE WHEN kind = 'title' THEN '' END, "
            "new_title = CASE WHEN kind = 'title' THEN '' END, "
            "old_description = NULL, new_description = NULL "
            "WHERE subject_id = ANY($1::uuid[]) AND kind IN ('title', 'description')",
            found.sessions,
        )
        await conn.execute(
            "DELETE FROM science_chat_shares WHERE conversation_id = $1",
            conversation_id,
        )
        await conn.execute(
            "INSERT INTO science_chat_tombstones (conversation_id, deleted_by) "
            "VALUES ($1, $2) ON CONFLICT DO NOTHING",
            conversation_id,
            caller.email,
        )
        rows = await conn.fetch(
            "UPDATE visual_workspaces SET station = station - 'conversation_id', "
            "revision = revision + 1 "
            "WHERE station->>'conversation_id' = $1 RETURNING id, user_id",
            str(conversation_id),
        )
        # Under the lock still, so no line can be queued for it after this. A drain
        # held on one of its sessions is let go at once, so its poller learns of the
        # delete from the next one rather than after its wait.
        _ = inbound.drop_conversation(conversation_id)
        for session_id in found.sessions:
            inbound.forget_poller(session_id)
    return [
        UnboundStation(
            workspace_id=from_plain(row["id"], uuid.UUID),
            user_id=from_plain(row["user_id"], uuid.UUID),
        )
        for row in rows
    ]


async def _holds(conn: Conn, *, email: str, staff: Sequence[str]) -> frozenset[str]:
    """Return the audiences the active user ``email`` holds; none for anyone else."""
    row = await conn.fetchrow(
        "SELECT email, hosted_domain FROM users WHERE email = $1 AND status = 'active'",
        email.lower(),
    )
    if row is None:
        return frozenset[str]()
    return audiences_for(
        from_plain(row["email"], str),
        hosted_domain=from_plain(row["hosted_domain"], str, default=None),
        staff=staff,
    )
