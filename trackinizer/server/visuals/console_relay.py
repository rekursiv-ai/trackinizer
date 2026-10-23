"""Tell a canvas's chat partner what its owner sent agents directly from the Console.

The Console lets a person steer any agent. The agents get the message from
``POST /api/messages``; the canvas's chat partner gets it here, so the chat
transcript records it and the partner's next prompt holds it, and the partner never
contradicts what the person told the agents.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from trackinizer.server.inbound import Inbound
from trackinizer.server.notify import tx
from trackinizer.server.store.deleted_chats import chat_was_deleted, lock_chat_id
from trackinizer.server.visuals.canvas_store import (
    CanvasSessionUnavailableError,
    chat_answerer,
    chat_line_target,
    read_canvas,
)
from trackinizer.server.visuals.science_chats import may_post
from trackinizer.wire.wire_sessions import CanvasMessageContext, ConsoleRelay


if TYPE_CHECKING:
    import uuid

    from trackinizer.lib.postgres import DatabaseEngine
    from trackinizer.server.audience import SessionReader
    from trackinizer.server.auth import Role
    from trackinizer.server.config import Assistant
    from trackinizer.server.inbound import InboundQueue


async def relay_console_line(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    canvas_id: uuid.UUID,
    relay: ConsoleRelay,
    key: uuid.UUID,
    source: str,
    source_role: Role,
    inbound: InboundQueue,
    assistant: Assistant | None,
    reader: SessionReader,
) -> bool | None:
    """Queue one Console relay for the partner of the owner's canvas.

    It joins the queue a Chat line of that canvas joins, as :func:`chat_line_target`
    says, for the provider that answers the chat (:func:`chat_answerer`): the chat's
    own session when the assistant has it open, else the partner's service session,
    which opens the conversation's session before it records the relay; for
    ``claude`` or ``codex``, the chat's session its run drains. It is refused where
    that line would be. A retry under the same key
    queues nothing again. The partner answers a relay with nothing; it records the
    line and reads it with its next prompt. A relay is a line of its sender's in the
    canvas's chat, so only a sender who may post into that chat relays into it: any
    canvas may be bound to a chat to read it, and that is no leave to write there.

    Args:
      engine: Database connection source.
      user_id: The signed-in owner.
      canvas_id: The canvas the Console is in.
      relay: The agents the owner sent to, and what they sent.
      key: Idempotency key of the relay.
      source: Attested principal email.
      source_role: That principal's role.
      inbound: Session message queue.
      assistant: The configured assistant, if any.
      reader: The sender, as the transcript test reads them: they post into a chat
        they read.

    Returns:
      delivered: None when the canvas is not the owner's; False when it has no chat,
        the chat was deleted, the sender may not post into it, its partner is not
        running or would not read it there, or the queue is full; else True.

    """
    state = await read_canvas(
        engine,
        user_id=user_id,
        canvas_id=canvas_id,
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
            reader=reader,
            conversation_id=state.conversation_id,
            assistant=assistant,
            inbound=inbound,
        ):
            return False
        provider = await chat_answerer(
            conn,
            state=state,
            conversation_id=state.conversation_id,
            poster=source,
            assistant=assistant,
        )
        try:
            chat = await chat_line_target(
                conn,
                inbound=inbound,
                assistant=assistant,
                provider=provider,
                conversation_id=state.conversation_id,
            )
        except CanvasSessionUnavailableError:
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
                        context=CanvasMessageContext(
                            canvas_id=canvas_id,
                            visible_visuals=[],
                            conversation_id=state.conversation_id,
                            relay=relay,
                        ),
                    ),
                ),
            ],
        )
    return True
