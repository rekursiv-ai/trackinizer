"""Tell a station's chat partner what its owner sent agents directly from the Console.

The Console lets a person steer any agent. The agents get the message from
``POST /api/messages``; the station's chat partner gets it here, so the chat
transcript records it and the partner's next prompt holds it, and the partner never
contradicts what the person told the agents.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from trackinizer.server.inbound import Inbound
from trackinizer.server.visuals.partners import live_chat_session
from trackinizer.server.visuals.workspace_store import read_workspace
from trackinizer.wire.wire_sessions import ConsoleRelay, WorkspaceMessageContext


if TYPE_CHECKING:
    import uuid

    from trackinizer.lib.postgres import DatabaseEngine
    from trackinizer.server.auth import Role
    from trackinizer.server.config import Assistant
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
) -> bool | None:
    """Queue one Console relay for the partner of the owner's station.

    It joins the queue a Chat line of that station joins: the chat's own session when
    the assistant has it open, else the assistant's service session, which opens the
    conversation's session before it records the relay. A retry under the same key
    queues nothing again. The partner answers a relay with nothing; it records the
    line and reads it with its next prompt.

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

    Returns:
      delivered: None when the station is not the owner's; False when it has no chat,
        its partner is not running, or the queue is full; else True.

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
    # A local helper drains its service session alone, so none of its chats is open.
    if assistant is not None and state.partner_choice != "local":
        async with engine.acquire() as conn:
            target = (
                await live_chat_session(
                    conn,
                    inbound=inbound,
                    opener=assistant.email,
                    conversation_id=state.conversation_id,
                )
                or target
            )
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
