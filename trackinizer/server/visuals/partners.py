"""Who a canvas's Chat talks to: the shared assistant, or its owner's own helper.

The assistant is configured by actor and account, and a session is its own only
when both match: an actor handle is first come, first served, so a name alone
proves nothing. A local helper is the owner's own: a session that a key of the
canvas's owner opened. Every function here runs on the caller's connection, and takes a
share lock on the session it names, so a canvas transaction holds the partner
stable until it commits.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import uuid

from trackinizer.server.visuals.workspaces import WorkspacePartner
from trackinizer.wire.wire_science_chat import (
    CHAT_HELPER_CLI,
    CHAT_SESSION_PREFIX,
    POSTER_LABEL_PREFIX,
    SCIENCE_CHAT_LABEL,
    chat_session_id,
)


if TYPE_CHECKING:
    from asyncpg import Record

    from trackinizer.lib.postgres import Conn
    from trackinizer.server.config import Assistant
    from trackinizer.server.inbound import InboundQueue
    from trackinizer.server.visuals.workspaces import (
        PartnerChoice,
        WorkspaceState,
    )


def is_assistant_session(
    assistant: Assistant | None,
    *,
    actor: str,
    email: str,
) -> bool:
    """Say whether a session is the assistant's.

    The session's granted actor is the configured one or that name with the
    ``#N`` suffix the server appends once any session, live or ended, has held the
    name, so an assistant that restarts stays recognised. The account that opened
    the session must be the configured one.

    Args:
      assistant: The configured assistant, if any.
      actor: The session's granted actor.
      email: The email of the account that owns the key that opened it.

    Returns:
      matches: Whether the session is the assistant's.

    """
    if assistant is None or email != assistant.email:
        return False
    suffix = actor.removeprefix(f"{assistant.actor}#")
    return actor == assistant.actor or (suffix != actor and suffix.isdecimal())


async def resolve_partner(
    conn: Conn,
    *,
    inbound: InboundQueue,
    assistant: Assistant | None,
    choice: PartnerChoice,
    owner_id: uuid.UUID,
) -> WorkspacePartner | None:
    """Name a canvas's partner: the chosen session, or say it has none.

    The shared choice is the assistant's newest live session. The local choice is
    the newest live ``trax helper`` session a key of the canvas's owner opened, so
    another user's helper never answers this owner's Chat.

    Args:
      conn: Connection the answer must be consistent with.
      inbound: In-process poller leases; a session nobody polls is unavailable.
      assistant: The configured assistant, if any.
      choice: The canvas's chosen partner.
      owner_id: The canvas's owner.

    Returns:
      partner: The partner, live or unavailable, or None for the shared choice
        with no assistant configured.

    """
    if choice == "local":
        rows = await _live_sessions(
            conn,
            inbound=inbound,
            email=None,
            owner_id=owner_id,
            cli=CHAT_HELPER_CLI,
        )
        row = next(iter(rows), None)
        return WorkspacePartner(
            session_id=None if row is None else cast(uuid.UUID, row["id"]),
            actor=None if row is None else cast(str, row["actor"]),
            cli=None if row is None else cast(str | None, row["cli"]),
            status="unavailable" if row is None else "live",
            kind="local",
        )
    return await shared_partner(conn, inbound=inbound, assistant=assistant)


async def shared_partner(
    conn: Conn,
    *,
    inbound: InboundQueue,
    assistant: Assistant | None,
) -> WorkspacePartner | None:
    """Name the assistant's newest live session, or say it has none.

    Args:
      conn: Connection the answer must be consistent with.
      inbound: In-process poller leases; a session nobody polls is unavailable.
      assistant: The configured assistant, if any.

    Returns:
      partner: The assistant, live or unavailable, or None with no assistant.

    """
    if assistant is None:
        return None
    rows = await _live_sessions(
        conn,
        inbound=inbound,
        email=assistant.email,
        owner_id=None,
        cli=None,
    )
    row = next(
        (
            row
            for row in rows
            if is_assistant_session(
                assistant,
                actor=cast(str, row["actor"]),
                email=assistant.email,
            )
        ),
        None,
    )
    return WorkspacePartner(
        session_id=None if row is None else cast(uuid.UUID, row["id"]),
        actor=assistant.actor,
        cli=None if row is None else cast(str | None, row["cli"]),
        status="unavailable" if row is None else "live",
    )


async def attach_partner(
    conn: Conn,
    *,
    state: WorkspaceState,
    owner_id: uuid.UUID,
    inbound: InboundQueue,
    assistant: Assistant | None,
) -> WorkspaceState:
    """Return ``state`` with its assistant and partner.

    Both are computed, never stored: the canvas JSONB and its replay receipts
    carry neither. Only the choice between them, ``state.partner_choice``, is.

    Args:
      conn: Connection the answer must be consistent with.
      state: The canvas as stored.
      owner_id: The canvas's owner.
      inbound: In-process poller leases.
      assistant: The configured assistant, if any.

    Returns:
      state: A copy carrying the assistant's name and the partner.

    """
    partner = await resolve_partner(
        conn,
        inbound=inbound,
        assistant=assistant,
        choice=state.partner_choice,
        owner_id=owner_id,
    )
    return state.model_copy(
        update={
            "partner": partner,
            "assistant": None if assistant is None else assistant.actor,
        },
    )


async def live_chat_session(
    conn: Conn,
    *,
    inbound: InboundQueue,
    opener: str,
    conversation_id: uuid.UUID,
) -> uuid.UUID | None:
    """Return the session a partner has open for a science chat, if it has one.

    A conversation's session is the live session of the partner's account whose
    ``cli_session_id`` is ``chat:<conversation id>`` and that some poller is
    draining; any other session that carries the id is not it.

    Args:
      conn: Database connection.
      inbound: In-process poller leases; a session nobody drains is not open.
      opener: The email of the partner's account: the assistant's. A local helper
        drains only its service session, so none of its chats is ever open here.
      conversation_id: The conversation.

    Returns:
      session_id: The conversation's live session, or None.

    """
    found = await conn.fetchval(
        "SELECT sess.id FROM inquiries AS sess JOIN api_keys AS credential "
        "ON credential.id = sess.agentsession_opened_by_api_key_id "
        "JOIN users AS member ON member.id = credential.user_id "
        "WHERE sess.kind = 'AgentSession' AND sess.status = 'active' "
        "AND sess.agentsession_ended IS NULL AND credential.revoked_at IS NULL "
        "AND sess.agentsession_cli_session_id = $1 AND member.email = $2 "
        "AND sess.id = ANY($3::uuid[]) "
        "ORDER BY sess.created DESC, sess.id DESC LIMIT 1",
        chat_session_id(conversation_id),
        opener,
        inbound.active_poller_ids(),
    )
    return None if found is None else cast(uuid.UUID, found)


async def assistant_holds_chat(
    conn: Conn,
    *,
    assistant: Assistant | None,
    conversation_id: uuid.UUID,
) -> bool:
    """Say whether the assistant has a session, live or closed, for a conversation.

    A local helper that joined such a conversation would open a second session under
    the same id, and the reads that name a conversation by its live session would
    show that private one in place of the shared history.

    Args:
      conn: Database connection.
      assistant: The configured assistant, if any.
      conversation_id: The conversation.

    Returns:
      held: Whether the assistant's account opened a session under its id.

    """
    if assistant is None:
        return False
    return bool(
        await conn.fetchval(
            "SELECT 1 FROM inquiries AS sess JOIN api_keys AS credential "
            "ON credential.id = sess.agentsession_opened_by_api_key_id "
            "JOIN users AS member ON member.id = credential.user_id "
            "WHERE sess.kind = 'AgentSession' "
            "AND sess.agentsession_cli_session_id = $1 AND member.email = $2 "
            "LIMIT 1",
            chat_session_id(conversation_id),
            assistant.email,
        ),
    )


def is_chat_session(
    assistant: Assistant | None,
    *,
    cli_session_id: str | None,
    email: str,
) -> bool:
    """Say whether a session is one of the assistant's science chats.

    Args:
      assistant: The configured assistant, if any.
      cli_session_id: The session's ``cli_session_id``.
      email: The email of the account that owns the key that opened it.

    Returns:
      matches: Whether the assistant account opened a ``chat:`` session.

    """
    return (
        assistant is not None
        and email == assistant.email
        and cli_session_id is not None
        and cli_session_id.startswith(CHAT_SESSION_PREFIX)
    )


async def assistant_key_may_use(
    conn: Conn,
    *,
    owner_id: uuid.UUID,
    partner: WorkspacePartner | None,
    api_key_id: uuid.UUID,
) -> bool:
    """Say whether a partner's key may read and operate the canvas.

    The key must have opened the canvas's live partner session, and also a live
    science chat the canvas's owner started or posted in: a user who never talked
    to the partner gives it nothing. With a local partner the key is the owner's
    own, so it passes by the same rule.

    Args:
      conn: Connection the answer must be consistent with.
      owner_id: The canvas's owner.
      partner: The canvas's partner.
      api_key_id: The calling key.

    Returns:
      allowed: Whether the key may use the canvas.

    """
    if partner is None or partner.session_id is None or partner.status != "live":
        return False
    return bool(
        await conn.fetchval(
            "SELECT 1 FROM inquiries AS sess JOIN api_keys AS credential "
            "ON credential.id = sess.agentsession_opened_by_api_key_id "
            "WHERE sess.id = $1 AND credential.id = $2 "
            "AND credential.revoked_at IS NULL "
            "AND EXISTS (SELECT 1 FROM inquiries AS chat "
            "JOIN users AS poster ON poster.id = $3 "
            "WHERE chat.kind = 'AgentSession' AND chat.status = 'active' "
            "AND chat.agentsession_ended IS NULL "
            "AND chat.agentsession_opened_by_api_key_id = $2 "
            "AND $4 = ANY(chat.labels) "
            "AND (chat.account = poster.email "
            "OR $5 || poster.email = ANY(chat.labels)))",
            partner.session_id,
            api_key_id,
            owner_id,
            SCIENCE_CHAT_LABEL,
            POSTER_LABEL_PREFIX,
        ),
    )


async def opener_email(conn: Conn, *, api_key_id: uuid.UUID | None) -> str | None:
    """Return the email of the account that owns an API key.

    Args:
      conn: Database connection.
      api_key_id: The key that opened a session, if known.

    Returns:
      email: The owning account's email, or None for no or an unknown key.

    """
    if api_key_id is None:
        return None
    return cast(
        str | None,
        await conn.fetchval(
            "SELECT member.email FROM api_keys AS credential "
            "JOIN users AS member ON member.id = credential.user_id "
            "WHERE credential.id = $1",
            api_key_id,
        ),
    )


# A ``None`` filter matches every session: ``email`` is the account that owns the
# opening key, ``owner_id`` the same account by id, ``cli`` the session's CLI.
async def _live_sessions(
    conn: Conn,
    *,
    inbound: InboundQueue,
    email: str | None,
    owner_id: uuid.UUID | None,
    cli: str | None,
) -> list[Record]:
    """Return polled, unrevoked live sessions, newest first, and share-lock them."""
    return await conn.fetch(
        "SELECT sess.id, sess.owner AS actor, sess.agentsession_cli AS cli "
        "FROM inquiries AS sess JOIN api_keys AS credential "
        "ON credential.id = sess.agentsession_opened_by_api_key_id "
        "JOIN users AS member ON member.id = credential.user_id "
        "WHERE sess.kind = 'AgentSession' AND sess.status = 'active' "
        "AND sess.agentsession_ended IS NULL AND credential.revoked_at IS NULL "
        "AND sess.owner IS NOT NULL AND ($1::text IS NULL OR member.email = $1) "
        "AND ($2::uuid IS NULL OR member.id = $2) "
        "AND ($3::text IS NULL OR sess.agentsession_cli = $3) "
        "AND sess.id = ANY($4::uuid[]) "
        "ORDER BY sess.created DESC, sess.id DESC "
        "FOR SHARE OF sess, credential",
        email,
        owner_id,
        cli,
        inbound.active_poller_ids(),
    )
