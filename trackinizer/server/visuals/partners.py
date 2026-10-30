"""Who a canvas's Chat talks to: the shared assistant, or the owner's claude or codex.

The canvas's provider says which. The assistant is configured by actor and account,
and a session is its own only when both match: an actor handle is first come, first
served, so a name alone proves nothing. A CLI's partner is the chat's own session,
while a run drains it. Every function here runs on the caller's connection, and the
assistant's reads take a share lock on the session they name, so a canvas transaction
holds the partner stable until it commits.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast

import uuid

from treekle import from_plain

from trackinizer.server.asks import open_ask_ids, waiting_sessions
from trackinizer.server.inbound import PollerRun
from trackinizer.server.preferences import ASSISTANT_KEYS, stored_preferences
from trackinizer.server.visuals.canvases import (
    NO_MACHINE_REASON,
    CanvasPartner,
    ProviderFlags,
    ProviderReasons,
)
from trackinizer.server.visuals.science_chats import read_starter
from trackinizer.wire.wire_machine_host import OFFLINE_AFTER_SEC
from trackinizer.wire.wire_science_chat import (
    CHAT_SESSION_PREFIX,
    POSTER_LABEL_PREFIX,
    SCIENCE_CHAT_LABEL,
    chat_conversation_id,
    chat_session_id,
)
from trackinizer.wire.wire_sessions import CANVAS_LABEL_PREFIX, RunState


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from asyncpg import Record

    from trackinizer.lib.postgres import Conn
    from trackinizer.server.config import Assistant
    from trackinizer.server.inbound import InboundQueue
    from trackinizer.server.visuals.canvases import CanvasState
    from trackinizer.wire.wire_science_chat import ChatProvider


_NEEDS: Final[Mapping[RunState, int]] = {
    "idle": 0,
    "running": 1,
    "requires_action": 2,
    "taken_over": 3,
}
"""How much of the person each state needs, for choosing among a chat's runs."""


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
    provider: ChatProvider,
    conversation_id: uuid.UUID | None,
) -> CanvasPartner | None:
    """Name a canvas's partner: the provider's live session, or say it has none.

    Args:
      conn: Connection the answer must be consistent with.
      inbound: In-process poller leases; a session nobody polls is not live.
      assistant: The configured assistant, if any.
      provider: The canvas's provider.
      conversation_id: The chat the canvas holds, if any.

    Returns:
      partner: For ``assistant``, the assistant, live or unavailable, or None with
        no assistant; for a CLI, its run on the chat, live or offline.

    """
    if provider == "assistant":
        partner = await shared_partner(conn, inbound=inbound, assistant=assistant)
        if partner is None or assistant is None or conversation_id is None:
            return partner
        # The assistant reports no state, so its chat's open ask is the whole of it.
        chat = await live_chat_session(
            conn,
            inbound=inbound,
            opener=assistant.email,
            conversation_id=conversation_id,
        )
        if chat is None or not await open_ask_ids(conn, session_id=chat):
            return partner
        return partner.model_copy(update={"state": "requires_action"})
    return await cli_partner(
        conn,
        inbound=inbound,
        assistant=assistant,
        provider=provider,
        conversation_id=conversation_id,
    )


# Audience: gated -- the session's id and liveness go to a canvas that holds the
# chat, which its owner bound as a reader.
async def cli_partner(
    conn: Conn,
    *,
    inbound: InboundQueue,
    assistant: Assistant | None,
    provider: ChatProvider,
    conversation_id: uuid.UUID | None,
) -> CanvasPartner:
    """Name a chat's run of ``provider``: its session while a run drains it.

    The chat's sessions are those under its ``chat:`` id that the assistant's key or
    its starter's opened (:func:`~.science_chats.read_starter`); one a stranger's key
    opened under the id is not the chat's. One is live when it is open, its opening
    key is not revoked, and a poller that named ``provider`` as its CLI drains it.
    The drain names its CLI, never a part's format: a run just attached has written
    no part yet, and the runner's own part after ``/compact`` has none.

    Args:
      conn: Connection the answer must be consistent with.
      inbound: In-process poller leases; a session nobody polls is not live.
      assistant: The configured assistant, if any.
      provider: ``claude`` or ``codex``.
      conversation_id: The chat the canvas holds; None for none yet.

    Returns:
      partner: The live session, with the machine, detached host and folder its
        run's last drain named (:meth:`~.inbound.InboundQueue.poller_run`) and whether it
        takes Run's commands, or ``offline`` naming the session that began the chat
        (None for no session yet), which a run attaches to, and why its run's last
        drain was refused (:meth:`~.inbound.InboundQueue.refusal`).

    """
    offline = CanvasPartner(
        session_id=None,
        actor=provider,
        cli=provider,
        status="offline",
        provider=provider,
        label=f"{provider}: no run",
    )
    if conversation_id is None:
        return offline
    starter = await read_starter(
        conn,
        assistant=assistant,
        conversation_id=conversation_id,
    )
    rows = await conn.fetch(
        "SELECT sess.id, (sess.status = 'active' AND sess.agentsession_ended IS NULL "
        "AND credential.revoked_at IS NULL AND sess.id = ANY($4::uuid[])) AS live "
        "FROM inquiries AS sess JOIN api_keys AS credential "
        "ON credential.id = sess.agentsession_opened_by_api_key_id "
        "JOIN users AS member ON member.id = credential.user_id "
        "WHERE sess.kind = 'AgentSession' AND sess.agentsession_cli_session_id = $1 "
        "AND (member.email = $2 OR lower(member.email) = lower($3)) "
        "ORDER BY sess.created, sess.id",
        chat_session_id(conversation_id),
        None if assistant is None else assistant.email,
        starter or "",
        inbound.active_poller_ids(cli=provider),
    )
    live = next((row for row in rows if from_plain(row["live"], bool)), None)
    if live is not None:
        session_id = from_plain(live["id"], uuid.UUID)
        # A lease that lapsed since the query above leaves the run unnamed, not gone.
        where = inbound.poller_run(session_id) or PollerRun()
        return offline.model_copy(
            update={
                "session_id": session_id,
                "status": "live",
                "state": await _run_state(conn, session_id=session_id, run=where),
                "machine": where.machine,
                "run_name": where.run,
                "cwd": where.cwd,
                "runs": where.runs,
                "label": provider
                if where.machine is None
                else f"{provider} · {where.machine}",
            },
        )
    if not rows:
        return offline
    session_id = from_plain(rows[0]["id"], uuid.UUID)
    refusal = inbound.refusal(session_id)
    return offline.model_copy(
        update={
            "session_id": session_id,
            "refused": "" if refusal is None else refusal.reason,
            "refused_at": None if refusal is None else refusal.at,
        },
    )


async def live_run_states(
    conn: Conn,
    *,
    inbound: InboundQueue,
    session_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, RunState]:
    """Say what each live run among some sessions is doing.

    Args:
      conn: A database connection.
      inbound: In-process poller leases, which hold what a run reported.
      session_ids: The sessions to ask about.

    Returns:
      states: By session, for those a run polls now; a session no run polls is left out.

    """
    runs = {
        session_id: run
        for session_id in session_ids
        if (run := inbound.poller_run(session_id)) is not None
    }
    waiting = await waiting_sessions(conn, session_ids=list(runs))
    return {
        session_id: _settled(run, waiting=session_id in waiting)
        for session_id, run in runs.items()
    }


async def chat_run_states(
    conn: Conn,
    *,
    inbound: InboundQueue,
    openers: Sequence[str],
    conversation_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, RunState]:
    """Say what each chat's live runs are doing, for the badges of a canvas list.

    A chat's runs are the sessions under its id that one of ``openers`` opened and some
    poller drains, as :func:`cli_partner` finds them; one a stranger's key opened is
    not the chat's. When a chat has several, the state that needs the most from the
    person wins: taken over, then waiting, then running.

    Args:
      conn: A database connection.
      inbound: In-process poller leases.
      openers: The emails whose keys open a chat's sessions, in any case: the
        canvas's owner and the assistant.
      conversation_ids: The chats to ask about.

    Returns:
      states: By chat, for those with a live run.

    """
    if not conversation_ids:
        return {}
    rows = await conn.fetch(
        "SELECT sess.id, sess.agentsession_cli_session_id AS chat "
        "FROM inquiries AS sess JOIN api_keys AS credential "
        "ON credential.id = sess.agentsession_opened_by_api_key_id "
        "JOIN users AS member ON member.id = credential.user_id "
        "WHERE sess.kind = 'AgentSession' AND sess.status = 'active' "
        "AND sess.agentsession_ended IS NULL AND credential.revoked_at IS NULL "
        "AND sess.agentsession_cli_session_id = ANY($1::text[]) "
        "AND lower(member.email) = ANY($2::text[]) AND sess.id = ANY($3::uuid[])",
        [chat_session_id(each) for each in conversation_ids],
        [each.lower() for each in openers],
        inbound.active_poller_ids(),
    )
    states = await live_run_states(
        conn,
        inbound=inbound,
        session_ids=[from_plain(row["id"], uuid.UUID) for row in rows],
    )
    by_chat: dict[uuid.UUID, RunState] = {}
    for row in rows:
        state = states.get(from_plain(row["id"], uuid.UUID))
        chat = chat_conversation_id(from_plain(row["chat"], str))
        if state is not None and chat is not None:
            held = by_chat.get(chat, "idle")
            by_chat[chat] = max(held, state, key=_NEEDS.__getitem__)
    return by_chat


async def provider_availability(
    conn: Conn,
    *,
    owner_id: uuid.UUID,
) -> tuple[ProviderFlags, ProviderReasons]:
    """Say which providers a canvas's owner may choose, and why not the others.

    ``claude`` or ``codex`` is available when the owner has a confirmed machine that
    is online, holds a credential that is not revoked, and lists the CLI in its
    facts: what ``GET /api/terminals/machines`` offers to start a run on.
    ``assistant`` is always available here: the credit ledger that may stop it is the
    site's billing addon, which the browser reads.

    Args:
      conn: A database connection.
      owner_id: The canvas's owner.

    Returns:
      available: Which providers they may choose.
      reasons: Why each one they may not is unavailable.

    """
    rows = await conn.fetch(
        "SELECT DISTINCT jsonb_array_elements_text(m.facts -> 'clis') AS cli "
        "FROM machines AS m JOIN users AS member ON member.email = m.owner "
        "WHERE member.id = $1 AND m.confirmed_at IS NOT NULL "
        "AND m.host_instance IS NOT NULL "
        "AND m.last_heartbeat > now() - make_interval(secs => $2) "
        "AND jsonb_typeof(m.facts -> 'clis') = 'array' "
        "AND NOT (EXISTS (SELECT 1 FROM machine_credentials AS c "
        "WHERE c.machine_id = m.id) AND NOT EXISTS (SELECT 1 "
        "FROM machine_credentials AS c WHERE c.machine_id = m.id "
        "AND c.revoked_at IS NULL))",
        owner_id,
        OFFLINE_AFTER_SEC,
    )
    clis = {from_plain(row["cli"], str) for row in rows}
    claude = "claude" in clis
    codex = "codex" in clis
    return (
        ProviderFlags(claude=claude, codex=codex),
        ProviderReasons(
            claude="" if claude else NO_MACHINE_REASON.format(cli="claude"),
            codex="" if codex else NO_MACHINE_REASON.format(cli="codex"),
        ),
    )


async def shared_partner(
    conn: Conn,
    *,
    inbound: InboundQueue,
    assistant: Assistant | None,
) -> CanvasPartner | None:
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
    rows = await _live_sessions(conn, inbound=inbound, email=assistant.email)
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
    return CanvasPartner(
        session_id=None if row is None else cast(uuid.UUID, row["id"]),
        actor=assistant.actor,
        cli=None if row is None else cast(str | None, row["cli"]),
        status="unavailable" if row is None else "live",
        label=assistant.actor,
    )


async def attach_partner(
    conn: Conn,
    *,
    state: CanvasState,
    owner_id: uuid.UUID,
    inbound: InboundQueue,
    assistant: Assistant | None,
) -> CanvasState:
    """Return ``state`` with its assistant, partner, providers and owner's preferences.

    All are computed, never stored: the canvas JSONB and its replay receipts
    carry none. Only the choice of provider, ``state.provider``, is.

    Args:
      conn: Connection the answer must be consistent with.
      state: The canvas as stored.
      owner_id: The canvas's owner.
      inbound: In-process poller leases.
      assistant: The configured assistant, if any.

    Returns:
      state: A copy carrying the assistant's name, the partner, and which providers
        the owner may choose.

    """
    partner = await resolve_partner(
        conn,
        inbound=inbound,
        assistant=assistant,
        provider=state.provider,
        conversation_id=state.conversation_id,
    )
    available, reasons = await provider_availability(conn, owner_id=owner_id)
    preferences = await conn.fetchval(
        "SELECT preferences FROM users WHERE id = $1",
        owner_id,
    )
    return state.model_copy(
        update={
            "partner": partner,
            "available": available,
            "reasons": reasons,
            "assistant": None if assistant is None else assistant.actor,
            "preferences": stored_preferences(preferences).model_dump(
                mode="json",
                include=set(ASSISTANT_KEYS),
            ),
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
      opener: The email of the assistant's account.
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
    partner: CanvasPartner | None,
    conversation_id: uuid.UUID | None,
    api_key_id: uuid.UUID,
) -> bool:
    """Say whether a partner's key may read and operate a canvas.

    The key must have opened the canvas's live partner session, and also the live
    science chat the canvas holds, in which the canvas's owner is the account or a
    poster. A canvas without a chat takes no key, and a chat the key did not open, or
    one of another conversation, gives it nothing.

    Args:
      conn: Connection the answer must be consistent with.
      owner_id: The canvas's owner.
      partner: The canvas's partner.
      conversation_id: The science chat the canvas holds, if any.
      api_key_id: The calling key.

    Returns:
      allowed: Whether the key may use the canvas.

    """
    if (
        partner is None
        or partner.session_id is None
        or partner.status != "live"
        or conversation_id is None
    ):
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
            "AND chat.agentsession_cli_session_id = $6 "
            "AND $4 = ANY(chat.labels) "
            "AND (chat.account = poster.email "
            "OR $5 || poster.email = ANY(chat.labels)))",
            partner.session_id,
            api_key_id,
            owner_id,
            SCIENCE_CHAT_LABEL,
            POSTER_LABEL_PREFIX,
            chat_session_id(conversation_id),
        ),
    )


async def canvas_session_key_may_use(
    conn: Conn,
    *,
    owner_id: uuid.UUID,
    canvas_id: uuid.UUID,
    api_key_id: uuid.UUID,
) -> bool:
    """Say whether a key opened a live session placed on a canvas of its own user.

    An agent run's session is labelled ``canvas:<id>`` when it is placed. The label
    is the session's own claim, so the key must also belong to the canvas's owner:
    a label naming another user's canvas gives its key nothing.

    Args:
      conn: Connection the answer must be consistent with.
      owner_id: The canvas's owner.
      canvas_id: The canvas.
      api_key_id: The calling key.

    Returns:
      allowed: Whether the key may read and operate the canvas.

    """
    return bool(
        await conn.fetchval(
            "SELECT 1 FROM inquiries AS sess JOIN api_keys AS credential "
            "ON credential.id = sess.agentsession_opened_by_api_key_id "
            "WHERE sess.kind = 'AgentSession' AND sess.status = 'active' "
            "AND sess.agentsession_ended IS NULL AND credential.revoked_at IS NULL "
            "AND credential.id = $1 AND credential.user_id = $2 "
            "AND $3 = ANY(sess.labels) LIMIT 1",
            api_key_id,
            owner_id,
            f"{CANVAS_LABEL_PREFIX}{canvas_id}",
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


# ``email`` is the account that owns the opening key.
async def _live_sessions(
    conn: Conn,
    *,
    inbound: InboundQueue,
    email: str,
) -> list[Record]:
    """Return polled, unrevoked live sessions, newest first, and share-lock them."""
    return await conn.fetch(
        "SELECT sess.id, sess.owner AS actor, sess.agentsession_cli AS cli "
        "FROM inquiries AS sess JOIN api_keys AS credential "
        "ON credential.id = sess.agentsession_opened_by_api_key_id "
        "JOIN users AS member ON member.id = credential.user_id "
        "WHERE sess.kind = 'AgentSession' AND sess.status = 'active' "
        "AND sess.agentsession_ended IS NULL AND credential.revoked_at IS NULL "
        "AND sess.owner IS NOT NULL AND member.email = $1 "
        "AND sess.id = ANY($2::uuid[]) "
        "ORDER BY sess.created DESC, sess.id DESC "
        "FOR SHARE OF sess, credential",
        email,
        inbound.active_poller_ids(),
    )


# A run in a terminal is not asking anything of Chat, whatever an old ask says.
def _settled(run: PollerRun, *, waiting: bool) -> RunState:
    """Say what a live run is doing: its own word, or waiting on an ask open in the chat."""
    if run.state == "taken_over":
        return run.state
    return "requires_action" if waiting else run.state


async def _run_state(conn: Conn, *, session_id: uuid.UUID, run: PollerRun) -> RunState:
    """Say what a live run is doing."""
    return _settled(run, waiting=bool(await open_ask_ids(conn, session_id=session_id)))
