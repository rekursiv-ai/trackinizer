"""Tell a station's chat partner what its owner sent agents directly from the Console.

The Console lets a person steer any agent. The agents get the message from
``POST /api/messages``; the station's chat partner gets it here, so the chat
transcript records it and the partner's next prompt holds it, and the partner never
contradicts what the person told the agents.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from trackinizer.server.inbound import Inbound
from trackinizer.server.notify import tx
from trackinizer.server.store.deleted_chats import chat_was_deleted, lock_chat_id
from trackinizer.server.visuals.science_chats import may_post
from trackinizer.server.visuals.workspace_store import (
    WorkspaceSessionUnavailableError,
    chat_line_target,
    read_workspace,
)
from trackinizer.wire.wire_sessions import ConsoleRelay, WorkspaceMessageContext


if TYPE_CHECKING:
    import uuid

    from trackinizer.lib.postgres import DatabaseEngine
    from trackinizer.server.auth import Role
    from trackinizer.server.config import Assistant, ChatOrgs
    from trackinizer.server.inbound import InboundQueue


async def relay_console_line(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    relay: ConsoleRelay,
    key: uuid.UUID,
    source: str,
    source_role: Role,
    inbound: InboundQueue,
    assistant: Assistant | None,
    orgs: ChatOrgs,
) -> bool | None:
    """Queue one Console relay for the partner of the owner's station.

    It joins the queue a Chat line of that station joins, as :func:`chat_line_target`
    says: the chat's own session when the assistant has it open, else the partner's
    service session, which opens the conversation's session before it records the
    relay; and it is refused where that line would be. A retry under the same key
    queues nothing again. The partner answers a relay with nothing; it records the
    line and reads it with its next prompt. A relay is a line of its sender's in the
    station's chat, so only a sender who may post into that chat relays into it: any
    station may be bound to a chat to read it, and that is no leave to write there.

    Args:
      engine: Database connection source.
      user_id: The signed-in owner.
      workspace_id: The station the Console is in.
      relay: The agents the owner sent to, and what they sent.
      key: Idempotency key of the relay.
      source: Attested principal email.
      source_role: That principal's role.
      inbound: Session message queue.
      assistant: The configured assistant, if any.
      orgs: Who besides a chat's starter and the people it is shared with continues
        it.

    Returns:
      delivered: None when the station is not the owner's; False when it has no chat,
        the chat was deleted, the sender may not post into it, its partner is not
        running or would not read it there, or the queue is full; else True.

    """
    state = await read_workspace(
        engine,
        user_id=user_id,
        workspace_id=workspace_id,
        inbound=inbound,
        assistant=assistant,
    )
    if state is None:
        return None
    partner = state.partner
    if (
        state.conversation_id is None
        or partner is None
        or partner.session_id is None
        or partner.status != "live"
    ):
        return False
    target = partner.session_id
    # Queued under the chat's lock, so a delete either finds this relay queued, or
    # this relay finds the delete's tombstone.
    async with engine.acquire() as conn, tx(conn):
        await lock_chat_id(conn, conversation_id=state.conversation_id)
        if await chat_was_deleted(
            conn,
            conversation_id=state.conversation_id,
        ) or not await may_post(
            conn,
            poster=source,
            conversation_id=state.conversation_id,
            assistant=assistant,
            orgs=orgs,
            inbound=inbound,
        ):
            return False
        try:
            chat = await chat_line_target(
                conn,
                inbound=inbound,
                assistant=assistant,
                choice=state.partner_choice,
                conversation_id=state.conversation_id,
            )
        except WorkspaceSessionUnavailableError:
            return False
        target = chat or target
        if inbound.is_full(target):
            return False
        _ = inbound.send_once(
            key,
            [
                (
                    target,
                    Inbound(
                        text=relay.text,
                        source=source,
                        source_role=source_role,
                        context=WorkspaceMessageContext(
                            workspace_id=workspace_id,
                            visible_visuals=[],
                            conversation_id=state.conversation_id,
                            relay=relay,
                        ),
                    ),
                ),
            ],
        )
    return True
