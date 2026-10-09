"""Atomic, per-user persistence for the visual canvas."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, cast

import hashlib
import json
import uuid

from trackinizer.lib.codec import from_plain
from trackinizer.server.inbound import Inbound, InboundQueue
from trackinizer.server.notify import tx
from trackinizer.server.visuals.chat_forks import (
    ConversationTakenError,
    ForeignChatError,
    may_continue,
)
from trackinizer.server.visuals.partners import (
    assistant_holds_chat,
    assistant_key_may_use,
    attach_partner,
    live_chat_session,
    resolve_partner,
)
from trackinizer.server.visuals.reports import read_artifact_content_on_conn
from trackinizer.server.visuals.science_chats import (
    read_starter,
    science_chat_exists_at,
)
from trackinizer.server.visuals.screens import read_screen
from trackinizer.server.visuals.workspaces import (
    ApplyWorkspaceOperation,
    ChoosePartner,
    Highlight,
    Navigate,
    ShowVisual,
    WorkspaceData,
    WorkspaceState,
    apply_operation,
    initial_data,
    refuse_record,
)
from trackinizer.wire.wire_science_chat import ChatSend, ChatSent
from trackinizer.wire.wire_sessions import (
    WorkspaceArtifactContent,
    WorkspaceMessageContext,
    WorkspaceRecordContext,
)


if TYPE_CHECKING:
    from collections.abc import Mapping

    from trackinizer.lib.postgres import Conn, DatabaseEngine
    from trackinizer.server.auth import Role
    from trackinizer.server.config import Assistant, ChatOrgs
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
    """The API key is not the canvas's Chat partner, which alone may use it."""


class WorkspaceSessionUnavailableError(Exception):
    """The canvas's partner has no session that can take this browser message."""


class PartnerBusyError(Exception):
    """The partner's inbound queue is full, so a message would evict an unread one."""


class WorkspaceContextChangedError(Exception):
    """The chat visual now targets a different record or report."""


@dataclass(frozen=True, slots=True, kw_only=True)
class AppliedOperation:
    """The canvas after an operation, and whether this call only replayed it."""

    state: WorkspaceState
    replayed: bool
    """The key had already been applied: nothing changed, so nothing is published."""


async def send_chat_line(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    body: ChatSend,
    key: uuid.UUID,
    source: str,
    source_role: Role,
    inbound: InboundQueue,
    assistant: Assistant | None,
    orgs: ChatOrgs,
) -> ChatSent | None:
    """Queue a browser line for a science chat's assistant, with its canvas context.

    Nothing is stored here: the assistant records the line in the conversation's
    session. A conversation whose session the assistant has open takes the line on
    that session's own queue; any other, and a new one, goes to the assistant's
    service session, which opens or resumes the conversation's session before it
    answers. A new conversation is named by ``key``, so a retry under the same key
    queues nothing again and answers with the same conversation.

    Args:
      engine: Database connection source.
      user_id: Authenticated browser account.
      body: The line, its canvas, and optional conversation and chat visual identity.
      key: Required idempotency key.
      source: Attested browser principal email.
      source_role: That principal's role.
      inbound: Session message queue.
      assistant: The configured assistant, if any.
      orgs: How the server groups its users into organisations.

    Returns:
      sent: The conversation, and its session when the assistant has it open; None
        for a foreign canvas.

    Raises:
      WorkspaceSessionUnavailableError: The canvas has no live partner, or its local
        helper was asked into a conversation the shared assistant holds.
      PartnerBusyError: The queue the line would join is full.
      WorkspaceContextChangedError: The chat visual targets another record now.
      IdempotencyReuseError: ``key`` already posted another line.
      ForeignChatError: The conversation was started outside the poster's organisation.
      ConversationTakenError: The fork's key names a conversation someone else began.
      ValueError: The fork names no line of a science chat.

    """
    conversation_id = key if body.fork else (body.conversation_id or key)
    async with engine.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, revision, state FROM visual_workspaces "
            "WHERE id = $1 AND user_id = $2",
            body.workspace_id,
            user_id,
        )
        if row is None:
            return None
        await _check_fork_or_membership(
            conn,
            body=body,
            conversation_id=conversation_id,
            email=source,
            assistant=assistant,
            orgs=orgs,
            inbound=inbound,
        )
        state = state_from_row(cast("Mapping[str, object]", row))
        partner = await resolve_partner(
            conn,
            inbound=inbound,
            assistant=assistant,
            choice=state.partner_choice,
            owner_id=user_id,
        )
        if partner is None or partner.session_id is None or partner.status != "live":
            raise WorkspaceSessionUnavailableError(
                "Your local helper is not running; start it with `trax helper claude`"
                if state.partner_choice == "local"
                else "The assistant is not running",
            )
        # Only the assistant drains a chat's own session. A local helper opens its
        # chats but drains its service session alone, so a line sent to one of its
        # chats would be read by nobody.
        chat = None
        if state.partner_choice == "local":
            if await assistant_holds_chat(
                conn,
                assistant=assistant,
                conversation_id=conversation_id,
            ):
                raise WorkspaceSessionUnavailableError(
                    "The shared assistant holds this conversation; start a new one "
                    "to use your local helper",
                )
        elif assistant is not None:
            chat = await live_chat_session(
                conn,
                inbound=inbound,
                opener=assistant.email,
                conversation_id=conversation_id,
            )
        target = chat or partner.session_id
        if inbound.is_full(target):
            raise PartnerBusyError("Partner busy: too many unread messages; try again.")
        record_id, record, artifact_content = await _chat_target(
            conn,
            state=state,
            body=body,
        )
        screen = await read_screen(
            conn,
            page=body.page,
            trail=body.trail,
            visuals=state.visuals,
        )
    _ = inbound.send_once(
        key,
        [
            (
                target,
                Inbound(
                    text=body.text,
                    source=source,
                    source_role=source_role,
                    context=WorkspaceMessageContext(
                        workspace_id=body.workspace_id,
                        record_id=record_id,
                        record=record,
                        artifact_content=artifact_content,
                        visible_visuals=screen.visuals,
                        agent_instructions=state.agent_instructions,
                        continuation_record_id=state.continuation_record_id,
                        conversation_id=conversation_id,
                        fork=body.fork,
                        page=screen.page,
                        trail=screen.trail,
                    ),
                ),
            ),
        ],
        fingerprint=hashlib.sha256(
            "\0".join(
                (
                    str(conversation_id),
                    str(body.workspace_id),
                    body.text,
                    str(body.fork),
                ),
            ).encode(),
        ).hexdigest(),
    )
    return ChatSent(conversation_id=conversation_id, session_id=chat)


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
            owner_id=user_id,
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
        found = await _workspace_for_principal(
            conn,
            workspace_id=workspace_id,
            user_id=user_id,
            agent_api_key_id=agent_api_key_id,
            inbound=inbound,
            assistant=assistant,
        )
        if found is None:
            return None
        state, owner_id = found
        return await attach_partner(
            conn,
            state=state,
            owner_id=owner_id,
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
        found = await _workspace_for_principal(
            conn,
            workspace_id=workspace_id,
            user_id=user_id,
            agent_api_key_id=agent_api_key_id,
            inbound=inbound,
            assistant=assistant,
            for_update=True,
        )
        if found is None:
            return None
        current, owner_id = found
        with_partner = partial(
            attach_partner,
            conn,
            owner_id=owner_id,
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
            # The receipt keeps the choice it was applied under; the partner is
            # computed from the choice now.
            replay = (
                current
                if isinstance(body.operation, Navigate | Highlight)
                else WorkspaceState.model_validate(receipt["response"]).model_copy(
                    update={"partner_choice": current.partner_choice},
                )
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
        if isinstance(body.operation, ChoosePartner) and agent_api_key_id is not None:
            raise ValueError("Only the canvas's owner chooses its Chat partner.")
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
                partner_choice=current.partner_choice,
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
            partner_choice=updated_data.partner_choice,
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
        partner_choice=data.partner_choice,
    )


async def _chat_target(
    conn: Conn,
    *,
    state: WorkspaceState,
    body: ChatSend,
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
# partner: the key opened the partner's live session and a live science chat in
# which the owner is a poster. With a local partner that is the owner's own key;
# with the shared assistant, the owner's own key is refused by name, so a terminal
# `trax workspace` learns why. Any other is told nothing.
async def _workspace_for_principal(
    conn: Conn,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    agent_api_key_id: uuid.UUID | None,
    inbound: InboundQueue,
    assistant: Assistant | None,
    for_update: bool = False,
) -> tuple[WorkspaceState, uuid.UUID] | None:
    """Find a canvas the principal may read and operate, with its owner."""
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
        return state, owner_id
    partner = await resolve_partner(
        conn,
        inbound=inbound,
        assistant=assistant,
        choice=state.partner_choice,
        owner_id=owner_id,
    )
    if await assistant_key_may_use(
        conn,
        owner_id=owner_id,
        partner=partner,
        api_key_id=agent_api_key_id,
    ):
        return state, owner_id
    if owner_id == user_id:
        raise WorkspaceKeyRefusedError(
            "Only the canvas's Chat partner may use it with an API key",
        )
    return None


# The starter asked about is the conversation the line will be queued for, which the
# idempotency key can name as well as the body can. The first line of a conversation is
# queued before any session carries its id, so the sender of the send that used the id
# as its key is its starter until a session says otherwise.
async def _check_fork_or_membership(
    conn: Conn,
    *,
    body: ChatSend,
    conversation_id: uuid.UUID,
    email: str,
    assistant: Assistant | None,
    orgs: ChatOrgs,
    inbound: InboundQueue,
) -> None:
    """Refuse a fork of what is no chat's line, and a post into another's chat."""
    if body.fork is not None and not await science_chat_exists_at(
        conn,
        assistant=assistant,
        email=email,
        fork=body.fork,
    ):
        raise ValueError("The fork point is not a line of a science chat")
    starter = inbound.sender_of(conversation_id) or await read_starter(
        conn,
        assistant=assistant,
        conversation_id=conversation_id,
    )
    if starter is None:
        return
    if body.fork is not None:
        if starter.lower() != email.lower():
            raise ConversationTakenError(
                "This conversation already exists; fork under a new key",
            )
    elif not may_continue(email, starter=starter, orgs=orgs):
        raise ForeignChatError(
            "This chat was started outside your organisation; fork it to continue",
        )
