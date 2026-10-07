"""Atomic, per-user persistence for the visual canvas."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, cast

import hashlib
import json
import uuid

from trackinizer.lib.codec import from_plain
from trackinizer.server.chat_hub import MessageFrame
from trackinizer.server.inbound import Inbound, InboundQueue
from trackinizer.server.notify import tx
from trackinizer.server.visuals.chats import (
    ChatRequestConflictError,
    SentMessage,
    add_message,
    replay_of,
    start_or_continue,
)
from trackinizer.server.visuals.partners import (
    assistant_key_may_use,
    attach_partner,
    resolve_partner,
)
from trackinizer.server.visuals.reports import read_artifact_content_on_conn
from trackinizer.server.visuals.workspaces import (
    ApplyWorkspaceOperation,
    Highlight,
    Navigate,
    ShowVisual,
    WorkspaceData,
    WorkspaceMessageReceipt,
    WorkspaceMessageRequest,
    WorkspaceState,
    apply_operation,
    initial_data,
    refuse_record,
)
from trackinizer.wire.wire_sessions import (
    WorkspaceArtifactContent,
    WorkspaceMessageContext,
    WorkspaceRecordContext,
    WorkspaceVisibleVisual,
)


if TYPE_CHECKING:
    from collections.abc import Mapping

    from trackinizer.lib.postgres import Conn, DatabaseEngine
    from trackinizer.server.chat_hub import ChatHub
    from trackinizer.server.config import Assistant
    from trackinizer.server.visuals.catalog import VisualCatalogBody


class RevisionConflictError(Exception):
    """The caller based its operation on an older workspace revision."""

    def __init__(self, current: WorkspaceState) -> None:
        super().__init__("stale workspace revision")
        self.current = current


class ReplayConflictError(Exception):
    """An idempotency key was reused with a different operation."""

    def __init__(self, current: WorkspaceState) -> None:
        super().__init__("Idempotency-Key already used for another operation")
        self.current = current


class WorkspaceDisabledError(Exception):
    """The account cannot access a visual canvas."""


class WorkspaceKeyRefusedError(Exception):
    """The API key is not the canvas's Chat assistant, which alone may use it."""


class WorkspaceSessionUnavailableError(Exception):
    """The assistant has no live session to take a browser message."""


class PartnerBusyError(Exception):
    """The partner's inbound queue is full, so a message would evict an unread one."""


class WorkspaceContextChangedError(Exception):
    """The chat visual now targets a different record or report."""


class DuplicateSendError(Exception):
    """A concurrent send with the same idempotency key stored its message first."""


@dataclass(frozen=True, slots=True, kw_only=True)
class AppliedOperation:
    """The canvas after an operation, and whether this call only replayed it."""

    state: WorkspaceState
    replayed: bool
    """The key had already been applied: nothing changed, so nothing is published."""


async def send_workspace_message(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    body: WorkspaceMessageRequest,
    key: uuid.UUID,
    source: str,
    inbound: InboundQueue,
    assistant: Assistant | None,
    hub: ChatHub,
) -> WorkspaceMessageReceipt | None:
    """Store a message, publish it, then queue it for the partner.

    The message is stored as the next line of its conversation, which a null
    ``conversation_id`` starts, together with the key and a hash of the request.
    After the commit it is published to the owner's open browsers, then queued for
    the assistant with its conversation in the context. A send whose key
    is already stored is a replay: the same request returns the original receipt,
    queues nothing, and answers even when the partner has since gone away; a
    different request under that key raises. Two concurrent sends with one key
    store one message and replay the other.

    Args:
      engine: Database connection source.
      user_id: Authenticated browser account.
      workspace_id: Canvas carrying the partner and visual context.
      body: Browser message, optional conversation and chat visual identity.
      key: Required idempotency key.
      source: Attested browser principal email.
      inbound: Session message queue.
      assistant: The configured assistant, if any.
      hub: Where the committed line is published.

    Returns:
      receipt: Partner session, conversation and stored line, or None for a
        foreign canvas.

    Raises:
      ChatConversationNotFoundError: The conversation is not this user's on this canvas.
      ChatRequestConflictError: The key was used for another send.
      WorkspaceSessionUnavailableError: The canvas has no live partner.

    """
    request_hash = hashlib.sha256(
        json.dumps(
            {"workspace_id": str(workspace_id), "body": body.model_dump(mode="json")},
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    ).hexdigest()
    async with engine.acquire() as conn:
        replay = await replay_of(
            conn,
            user_id=user_id,
            request_key=key,
            request_hash=request_hash,
        )
    if replay is not None:
        return _receipt(replay)
    try:
        async with engine.acquire() as conn, tx(conn):
            queued = await _store_send(
                conn,
                user_id=user_id,
                workspace_id=workspace_id,
                body=body,
                key=key,
                request_hash=request_hash,
                source=source,
                inbound=inbound,
                assistant=assistant,
            )
    except DuplicateSendError:
        async with engine.acquire() as conn:
            replay = await replay_of(
                conn,
                user_id=user_id,
                request_key=key,
                request_hash=request_hash,
            )
        if replay is None:
            raise ChatRequestConflictError(
                "Idempotency-Key already used for another message",
            ) from None
        return _receipt(replay)
    if queued is None:
        return None
    sent, session_id, inbound_message = queued
    hub.publish(
        workspace_id,
        frame=MessageFrame(conversation_id=sent.conversation_id, message=sent.message),
    )
    inbound.enqueue(session_id, inbound_message)
    return _receipt(sent)


async def create_default_workspace(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    catalog: VisualCatalogBody,
    inbound: InboundQueue,
    assistant: Assistant | None,
) -> WorkspaceState:
    """Return the user's default canvas, creating it once across races.

    Args:
      engine: Database connection source.
      user_id: Owner of the default canvas.
      catalog: Trusted visual definitions and the initial selection.
      inbound: In-process poller leases.
      assistant: The configured assistant, if any.

    Returns:
      state: The existing or newly created workspace, with its partner.

    """
    workspace_id = uuid.uuid4()
    state = initial_data(catalog)
    async with engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO visual_workspaces (id, user_id, state) VALUES ($1, $2, $3) "
            "ON CONFLICT DO NOTHING",
            workspace_id,
            user_id,
            state.model_dump(mode="json"),
        )
        row = await conn.fetchrow(
            "SELECT id, revision, state FROM visual_workspaces "
            "WHERE user_id = $1 AND is_default",
            user_id,
        )
        if row is None:
            raise RuntimeError("default workspace insert was not visible")
        return await attach_partner(
            conn,
            state=state_from_row(cast("Mapping[str, object]", row)),
            inbound=inbound,
            assistant=assistant,
        )


async def read_workspace(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    inbound: InboundQueue,
    assistant: Assistant | None,
    agent_api_key_id: uuid.UUID | None = None,
) -> WorkspaceState | None:
    """Read one canvas for its owner, or for an agent key allowed to use it.

    Args:
      engine: Database connection source.
      user_id: Authenticated principal.
      workspace_id: Requested workspace.
      inbound: In-process poller leases.
      assistant: The configured assistant, if any.
      agent_api_key_id: The calling agent key, if any; see
        :func:`_workspace_for_principal` for what it may read.

    Returns:
      state: Workspace with its partner, or None when the principal may not see it.

    """
    async with engine.acquire() as conn:
        state = await _workspace_for_principal(
            conn,
            workspace_id=workspace_id,
            user_id=user_id,
            agent_api_key_id=agent_api_key_id,
            inbound=inbound,
            assistant=assistant,
        )
        if state is None:
            return None
        return await attach_partner(
            conn,
            state=state,
            inbound=inbound,
            assistant=assistant,
        )


async def apply_workspace_operation(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    key: uuid.UUID,
    body: ApplyWorkspaceOperation,
    catalog: VisualCatalogBody,
    inbound: InboundQueue,
    assistant: Assistant | None,
    agent_api_key_id: uuid.UUID | None = None,
) -> AppliedOperation | None:
    """Apply one compare-and-swap operation and persist its replay receipt.

    A ``navigate`` or ``highlight`` operation moves no visual and no revision: it
    is only recorded, for replay, and an agent key alone may send it. A show names
    a record the visual accepts: its catalog lists the kinds, and one that takes no
    record refuses any.

    Args:
      engine: Database connection source.
      user_id: Authenticated principal.
      workspace_id: Target workspace.
      key: Idempotency key for this operation.
      body: Expected revision and validated operation.
      catalog: Trusted descriptor set the operation is validated against.
      inbound: In-process poller leases.
      assistant: The configured assistant, if any.
      agent_api_key_id: Calling API key, or None for browser cookie auth; see
        :func:`_workspace_for_principal` for what it may operate.

    Returns:
      applied: The updated or replayed state with its partner, or None when the
        principal may not see the canvas.

    """
    canonical = json.dumps(
        body.model_dump(mode="json", exclude_unset=True),
        sort_keys=True,
        separators=(",", ":"),
    )
    request_hash = hashlib.sha256(canonical.encode()).hexdigest()
    async with engine.acquire() as conn, tx(conn):
        current = await _workspace_for_principal(
            conn,
            workspace_id=workspace_id,
            user_id=user_id,
            agent_api_key_id=agent_api_key_id,
            inbound=inbound,
            assistant=assistant,
            for_update=True,
        )
        if current is None:
            return None
        with_partner = partial(
            attach_partner,
            conn,
            inbound=inbound,
            assistant=assistant,
        )
        receipt = await conn.fetchrow(
            "SELECT request_hash, response FROM visual_workspace_operations "
            "WHERE workspace_id = $1 AND key = $2",
            workspace_id,
            key,
        )
        if receipt is not None:
            if receipt["request_hash"] != request_hash:
                raise ReplayConflictError(await with_partner(state=current))
            replay = (
                current
                if isinstance(body.operation, Navigate | Highlight)
                else WorkspaceState.model_validate(receipt["response"])
            )
            return AppliedOperation(
                state=await with_partner(state=replay),
                replayed=True,
            )
        if isinstance(body.operation, Navigate | Highlight):
            # Nothing changes, so only the key is kept: a retry publishes nothing.
            if agent_api_key_id is None:
                raise ValueError("Only an agent navigates or highlights.")
            await _remember(
                conn,
                workspace_id=workspace_id,
                key=key,
                request_hash=request_hash,
                response={},
            )
            await trim_operation_receipts(conn, workspace_id=workspace_id)
            return AppliedOperation(
                state=await with_partner(state=current),
                replayed=False,
            )
        if body.revision != current.revision:
            raise RevisionConflictError(await with_partner(state=current))
        if isinstance(body.operation, ShowVisual):
            await _check_record(conn, operation=body.operation, catalog=catalog)
        updated_data = apply_operation(
            WorkspaceData(
                visuals=current.visuals,
                focused_instance=current.focused_instance,
                agent_instructions=current.agent_instructions,
                continuation_record_id=current.continuation_record_id,
            ),
            body.operation,
            catalog,
        )
        updated = WorkspaceState(
            id=current.id,
            revision=current.revision + 1,
            visuals=updated_data.visuals,
            focused_instance=updated_data.focused_instance,
            agent_instructions=updated_data.agent_instructions,
            continuation_record_id=updated_data.continuation_record_id,
        )
        await conn.execute(
            "UPDATE visual_workspaces SET revision = $2, state = $3, "
            "modified_at = clock_timestamp() WHERE id = $1",
            workspace_id,
            updated.revision,
            updated_data.model_dump(mode="json"),
        )
        await _remember(
            conn,
            workspace_id=workspace_id,
            key=key,
            request_hash=request_hash,
            response=updated.model_dump(mode="json"),
        )
        if updated.revision % 16 == 0:
            # Old retries still carry a stale revision and receive 409 after
            # their receipt expires. Keep the recent 64 exact replay results.
            await trim_operation_receipts(conn, workspace_id=workspace_id)
        return AppliedOperation(state=await with_partner(state=updated), replayed=False)


async def trim_operation_receipts(conn: Conn, *, workspace_id: uuid.UUID) -> None:
    """Keep a canvas's 64 newest operation receipts.

    Args:
      conn: Connection inside the operation's transaction.
      workspace_id: The canvas.

    """
    await conn.execute(
        "DELETE FROM visual_workspace_operations WHERE workspace_id = $1 "
        "AND key IN (SELECT key FROM visual_workspace_operations "
        "WHERE workspace_id = $1 ORDER BY created_at DESC, key DESC "
        "OFFSET 64)",
        workspace_id,
    )


def state_from_row(row: Mapping[str, object]) -> WorkspaceState:
    """Validate stored JSON before returning it as API state.

    Args:
      row: A `visual_workspaces` row with id, revision and state.

    Returns:
      state: The validated workspace state.

    """
    data = WorkspaceData.model_validate(row["state"])
    return WorkspaceState(
        id=cast(uuid.UUID, row["id"]),
        revision=from_plain(row.get("revision"), int),
        visuals=data.visuals,
        focused_instance=data.focused_instance,
        agent_instructions=data.agent_instructions,
        continuation_record_id=data.continuation_record_id,
    )


async def _store_send(
    conn: Conn,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    body: WorkspaceMessageRequest,
    key: uuid.UUID,
    request_hash: str,
    source: str,
    inbound: InboundQueue,
    assistant: Assistant | None,
) -> tuple[SentMessage, uuid.UUID, Inbound] | None:
    """Validate a send and store its message; return it with what to queue."""
    row = await conn.fetchrow(
        "SELECT id, revision, state FROM visual_workspaces "
        "WHERE id = $1 AND user_id = $2 FOR SHARE",
        workspace_id,
        user_id,
    )
    if row is None:
        return None
    state = state_from_row(cast("Mapping[str, object]", row))
    partner = await resolve_partner(conn, inbound=inbound, assistant=assistant)
    if partner is None or partner.session_id is None or partner.status != "live":
        raise WorkspaceSessionUnavailableError("The assistant is not running")
    if inbound.is_full(partner.session_id):
        raise PartnerBusyError("Partner busy: too many unread messages; try again.")
    record_id, record, artifact_content = await _chat_target(
        conn,
        state=state,
        body=body,
    )
    conversation_id = await start_or_continue(
        conn,
        user_id=user_id,
        workspace_id=workspace_id,
        conversation_id=body.conversation_id,
        text=body.text,
        partner_session_id=partner.session_id,
        partner_actor=partner.actor,
    )
    message = await add_message(
        conn,
        conversation_id=conversation_id,
        role="user",
        author=source,
        text=body.text,
        request_key=key,
        request_hash=request_hash,
    )
    if message is None:
        raise DuplicateSendError
    context = WorkspaceMessageContext(
        workspace_id=workspace_id,
        record_id=record_id,
        record=record,
        artifact_content=artifact_content,
        visible_visuals=[
            WorkspaceVisibleVisual(id=visual.id, type=visual.type)
            for visual in state.visuals
        ],
        agent_instructions=state.agent_instructions,
        continuation_record_id=state.continuation_record_id,
        conversation_id=conversation_id,
    )
    return (
        SentMessage(
            conversation_id=conversation_id,
            session_id=partner.session_id,
            message=message,
        ),
        partner.session_id,
        Inbound(text=body.text, source=source, context=context, seq=message.seq),
    )


async def _chat_target(
    conn: Conn,
    *,
    state: WorkspaceState,
    body: WorkspaceMessageRequest,
) -> tuple[
    uuid.UUID | None,
    WorkspaceRecordContext | None,
    WorkspaceArtifactContent | None,
]:
    """Check the chat visual the message came from, and read the record it shows."""
    if body.chat_instance_id is None and state.visuals:
        raise ValueError("Chat visual instance required.")
    record_id = None
    if body.chat_instance_id is not None:
        chat = next((v for v in state.visuals if v.id == body.chat_instance_id), None)
        if chat is None or chat.type != "trax.chat":
            raise ValueError("Chat visual instance not found.")
        record_id = chat.record_id
    if record_id != body.expected_record_id:
        raise WorkspaceContextChangedError(
            "Chat target changed; refresh before sending.",
        )
    if record_id is None:
        return None, None, None
    record = None
    record_row = await conn.fetchrow(
        "SELECT id, kind, seq, title FROM inquiries WHERE id = $1",
        record_id,
    )
    if record_row is not None:
        record_fields = dict(record_row)
        record_fields["title"] = from_plain(record_fields.get("title"), str)[:512]
        record = WorkspaceRecordContext.model_validate(record_fields)
    artifact_content = None
    if record is None or record.kind == "Artifact":
        revision = await read_artifact_content_on_conn(
            conn,
            record_id,
            include_html=False,
        )
        if revision is not None:
            artifact_content = WorkspaceArtifactContent.model_validate(
                revision.model_dump(),
            )
    return record_id, record, artifact_content


def _receipt(sent: SentMessage) -> WorkspaceMessageReceipt:
    """Build the receipt for a stored send."""
    return WorkspaceMessageReceipt(
        session_id=sent.session_id,
        conversation_id=sent.conversation_id,
        message=sent.message,
    )


async def _remember(
    conn: Conn,
    *,
    workspace_id: uuid.UUID,
    key: uuid.UUID,
    request_hash: str,
    response: Mapping[str, object],
) -> None:
    """Record an applied operation's result, so a retry replays it."""
    await conn.execute(
        "INSERT INTO visual_workspace_operations "
        "(workspace_id, key, request_hash, response) VALUES ($1, $2, $3, $4)",
        workspace_id,
        key,
        request_hash,
        response,
    )


async def _check_record(
    conn: Conn,
    *,
    operation: ShowVisual,
    catalog: VisualCatalogBody,
) -> None:
    """Refuse a show whose record its visual cannot draw, or has no content."""
    descriptor = next(
        (v for v in catalog.visuals if v.type == operation.visual_type),
        None,
    )
    if descriptor is None or operation.record_id is None:
        return
    kind = None
    if descriptor.record_kinds is not None:
        kind = from_plain(
            await conn.fetchval(
                "SELECT kind FROM inquiries WHERE id = $1",
                operation.record_id,
            ),
            str,
            default=None,
        )
    if reason := refuse_record(operation, descriptor=descriptor, kind=kind) or "":
        raise ValueError(reason)
    if operation.visual_type == "trax.artifact" and (
        await conn.fetchval(
            "SELECT 1 FROM visual_report_revisions WHERE artifact_id = $1",
            operation.record_id,
        )
        is None
    ):
        raise ValueError("Artifact content not found.")


# A browser sees only its own canvas. An agent key may use a canvas only as its Chat
# assistant: the key opened the assistant's live session and the owner has a
# conversation on this canvas with that session. The owner's own key is refused by
# name, so a terminal `trax workspace` learns why; any other is told nothing.
async def _workspace_for_principal(
    conn: Conn,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    agent_api_key_id: uuid.UUID | None,
    inbound: InboundQueue,
    assistant: Assistant | None,
    for_update: bool = False,
) -> WorkspaceState | None:
    """Find a canvas the principal may read and operate."""
    row = await conn.fetchrow(
        "SELECT id, user_id, revision, state FROM visual_workspaces "
        "WHERE id = $1 AND ($3::boolean OR user_id = $2) FOR UPDATE"
        if for_update
        else "SELECT id, user_id, revision, state FROM visual_workspaces "
        "WHERE id = $1 AND ($3::boolean OR user_id = $2)",
        workspace_id,
        user_id,
        agent_api_key_id is not None,
    )
    if row is None:
        return None
    owner_id = cast(uuid.UUID, row["user_id"])
    state = state_from_row(cast("Mapping[str, object]", row))
    if agent_api_key_id is None:
        return state
    partner = await resolve_partner(conn, inbound=inbound, assistant=assistant)
    if await assistant_key_may_use(
        conn,
        owner_id=owner_id,
        workspace_id=workspace_id,
        partner=partner,
        api_key_id=agent_api_key_id,
    ):
        return state
    if owner_id == user_id:
        raise WorkspaceKeyRefusedError(
            "Only the canvas's Chat assistant may use it with an API key",
        )
    return None
