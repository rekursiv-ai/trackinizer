"""Atomic, per-user persistence for the visual canvas."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import hashlib
import json
import uuid

from trackinizer.lib.codec import from_plain
from trackinizer.server.inbound import Inbound, InboundQueue
from trackinizer.server.notify import tx
from trackinizer.server.visuals.reports import read_artifact_content_on_conn
from trackinizer.server.visuals.workspaces import (
    ApplyWorkspaceOperation,
    ConnectableSession,
    ShowVisual,
    WorkspaceConnection,
    WorkspaceConnectionStatus,
    WorkspaceData,
    WorkspaceMessageReceipt,
    WorkspaceMessageRequest,
    WorkspaceState,
    apply_operation,
    initial_data,
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


class WorkspacePairingError(Exception):
    """The key has no active session connected to this canvas."""


class WorkspaceSessionUnavailableError(Exception):
    """The paired session cannot receive a browser message."""


class WorkspaceContextChangedError(Exception):
    """The chat visual now targets a different record or report."""


async def workspace_connection_status(
    engine: DatabaseEngine,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    *,
    inbound: InboundQueue,
) -> WorkspaceConnectionStatus | None:
    """Read the stored pairing directly, including sessions beyond picker cap.

    Args:
      engine: Database connection source.
      user_id: Authenticated browser account.
      workspace_id: Canvas whose pairing is checked.
      inbound: In-process poller leases.

    Returns:
      status: Direct pairing status, or None for a foreign canvas.

    """
    state = await read_workspace(engine, user_id, workspace_id, inbound=inbound)
    if state is None:
        return None
    session_id = state.connected_session_id
    if session_id is None:
        return WorkspaceConnectionStatus(status="unavailable")
    async with engine.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT sess.owner AS actor, sess.agentsession_cli AS cli, "
            "sess.status, sess.agentsession_ended AS ended "
            "FROM inquiries AS sess JOIN api_keys AS credential "
            "ON credential.id = sess.agentsession_opened_by_api_key_id "
            "WHERE sess.id = $1 AND sess.kind = 'AgentSession' "
            "AND credential.user_id = $2 AND credential.revoked_at IS NULL "
            "AND sess.owner IS NOT NULL",
            session_id,
            user_id,
        )
    if row is None:
        return WorkspaceConnectionStatus(status="unavailable", session_id=session_id)
    if row["status"] != "active" or row["ended"] is not None:
        return WorkspaceConnectionStatus(status="ended", session_id=session_id)
    if not inbound.has_poller(session_id):
        return WorkspaceConnectionStatus(status="unavailable", session_id=session_id)
    return WorkspaceConnectionStatus(
        status="live",
        session_id=session_id,
        actor=cast(str, row["actor"]),
        cli=cast(str | None, row["cli"]),
    )


async def send_workspace_message(
    engine: DatabaseEngine,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    body: WorkspaceMessageRequest,
    key: uuid.UUID,
    *,
    source: str,
    inbound: InboundQueue,
) -> WorkspaceMessageReceipt | None:
    """Validate live pairing and canvas context before queueing one message.

    Args:
      engine: Database connection source.
      user_id: Authenticated browser account.
      workspace_id: Canvas carrying the paired session and visual context.
      body: Browser message and optional chat visual identity.
      key: Required idempotency key.
      source: Attested browser principal email.
      inbound: Session message queue.

    Returns:
      receipt: Queued session and pending depth, or None for a foreign canvas.

    """
    async with engine.acquire() as conn, tx(conn):
        row = await conn.fetchrow(
            "SELECT id, revision, state, session_id FROM visual_workspaces "
            "WHERE id = $1 AND user_id = $2 FOR SHARE",
            workspace_id,
            user_id,
        )
        if row is None:
            return None
        state = state_from_row(cast("Mapping[str, object]", row))
        session_id = state.connected_session_id
        if (
            session_id is None
            or not inbound.has_poller(session_id)
            or not await _live_session_owned_by_key(
                conn,
                session_id,
                user_id,
            )
        ):
            raise WorkspaceSessionUnavailableError("No live paired session")
        if body.chat_instance_id is None and state.visuals:
            raise ValueError("Chat visual instance required.")
        record_id = None
        if body.chat_instance_id is not None:
            chat = next(
                (v for v in state.visuals if v.id == body.chat_instance_id),
                None,
            )
            if chat is None or chat.type != "trax.chat":
                raise ValueError("Chat visual instance not found.")
            record_id = chat.record_id
        if record_id != body.expected_record_id:
            raise WorkspaceContextChangedError(
                "Chat target changed; refresh before sending.",
            )
        record = None
        if record_id is not None:
            record_row = await conn.fetchrow(
                "SELECT id, kind, seq, title FROM inquiries WHERE id = $1",
                record_id,
            )
            if record_row is not None:
                record_fields = dict(record_row)
                record_fields["title"] = from_plain(record_fields.get("title"), str)[
                    :512
                ]
                record = WorkspaceRecordContext.model_validate(record_fields)
        artifact_content = None
        if record_id is not None and (record is None or record.kind == "Artifact"):
            revision = await read_artifact_content_on_conn(
                conn,
                record_id,
                include_html=False,
            )
            if revision is not None:
                artifact_content = WorkspaceArtifactContent.model_validate(
                    revision.model_dump(),
                )
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
        )
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "workspace_id": str(workspace_id),
                    "session_id": str(session_id),
                    "source": source,
                    "body": body.model_dump(mode="json"),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
        ).hexdigest()
        queued = inbound.send_scoped_once(
            key,
            session_id,
            Inbound(text=body.text, source=source, context=context),
            fingerprint=fingerprint,
        )
        return WorkspaceMessageReceipt(session_id=session_id, queued=queued)


async def list_connectable_sessions(
    engine: DatabaseEngine,
    user_id: uuid.UUID,
    *,
    inbound: InboundQueue,
) -> list[ConnectableSession]:
    """List recent live sessions opened by this user's unrevoked keys.

    Args:
      engine: Database connection source.
      user_id: Authenticated browser account.
      inbound: In-process poller leases.

    Returns:
      sessions: Up to 100 pairable sessions, newest first.

    """
    async with engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT sess.id, sess.title, sess.owner AS actor, "
            "sess.agentsession_cli AS cli FROM inquiries AS sess "
            "JOIN api_keys AS credential "
            "ON credential.id = sess.agentsession_opened_by_api_key_id "
            "WHERE credential.user_id = $1 AND credential.revoked_at IS NULL "
            "AND sess.kind = 'AgentSession' AND sess.status = 'active' "
            "AND sess.agentsession_ended IS NULL AND sess.owner IS NOT NULL "
            "AND sess.id = ANY($2::uuid[]) "
            "ORDER BY sess.created DESC, sess.id DESC LIMIT 100",
            user_id,
            inbound.active_poller_ids(),
        )
    return [
        ConnectableSession.model_validate(dict(row))
        for row in rows
        if inbound.has_poller(cast(uuid.UUID, row["id"]))
    ]


async def create_default_workspace(
    engine: DatabaseEngine,
    user_id: uuid.UUID,
    *,
    catalog: VisualCatalogBody,
) -> WorkspaceState:
    """Return the user's default canvas, creating it once across races.

    Args:
      engine: Database connection source.
      user_id: Owner of the default canvas.
      catalog: Trusted visual definitions and the initial selection.

    Returns:
      state: The existing or newly created workspace.

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
            "SELECT id, revision, state, session_id FROM visual_workspaces "
            "WHERE user_id = $1 AND is_default",
            user_id,
        )
    if row is None:
        raise RuntimeError("default workspace insert was not visible")
    return state_from_row(cast("Mapping[str, object]", row))


async def read_workspace(
    engine: DatabaseEngine,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    *,
    inbound: InboundQueue,
    agent_api_key_id: uuid.UUID | None = None,
) -> WorkspaceState | None:
    """Read one canvas only when it belongs to the principal.

    Args:
      engine: Database connection source.
      user_id: Authenticated owner.
      workspace_id: Requested workspace.
      inbound: In-process poller leases.
      agent_api_key_id: Agent key requiring a live workspace pairing, if any.

    Returns:
      state: Workspace, or None when it is not owned by this user.

    """
    async with engine.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, revision, state, session_id FROM visual_workspaces "
            "WHERE id = $1 AND user_id = $2",
            workspace_id,
            user_id,
        )
        if row is None:
            return None
        state = state_from_row(cast("Mapping[str, object]", row))
        if agent_api_key_id is not None and (
            state.connected_session_id is None
            or not inbound.has_poller(state.connected_session_id)
            or not await _live_session_owned_by_key(
                conn,
                state.connected_session_id,
                user_id,
                agent_api_key_id,
            )
        ):
            raise WorkspacePairingError("No live paired session for this API key")
    return state


async def apply_workspace_operation(
    engine: DatabaseEngine,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    key: uuid.UUID,
    body: ApplyWorkspaceOperation,
    *,
    catalog: VisualCatalogBody,
    inbound: InboundQueue,
    agent_api_key_id: uuid.UUID | None = None,
) -> WorkspaceState | None:
    """Apply one compare-and-swap operation and persist its replay receipt.

    Args:
      engine: Database connection source.
      user_id: Authenticated owner.
      workspace_id: Target workspace.
      key: Idempotency key for this operation.
      body: Expected revision and validated operation.
      catalog: Trusted descriptor set the operation is validated against.
      inbound: In-process poller leases.
      agent_api_key_id: Calling API key, or None for browser cookie auth.

    Returns:
      state: Updated or replayed state, or None when not owned.

    """
    canonical = json.dumps(
        body.model_dump(mode="json", exclude_unset=True),
        sort_keys=True,
        separators=(",", ":"),
    )
    request_hash = hashlib.sha256(canonical.encode()).hexdigest()
    async with engine.acquire() as conn, tx(conn):
        row = await conn.fetchrow(
            "SELECT id, revision, state, session_id FROM visual_workspaces "
            "WHERE id = $1 AND user_id = $2 FOR UPDATE",
            workspace_id,
            user_id,
        )
        if row is None:
            return None
        current = state_from_row(cast("Mapping[str, object]", row))
        if agent_api_key_id is not None and (
            current.connected_session_id is None
            or not inbound.has_poller(current.connected_session_id)
            or not await _live_session_owned_by_key(
                conn,
                current.connected_session_id,
                user_id,
                agent_api_key_id,
            )
        ):
            raise WorkspacePairingError("No live paired session for this API key")
        receipt = await conn.fetchrow(
            "SELECT request_hash, response FROM visual_workspace_operations "
            "WHERE workspace_id = $1 AND key = $2",
            workspace_id,
            key,
        )
        if receipt is not None:
            if receipt["request_hash"] != request_hash:
                raise ReplayConflictError(current)
            return WorkspaceState.model_validate(receipt["response"])
        if body.revision != current.revision:
            raise RevisionConflictError(current)
        if (
            isinstance(body.operation, ShowVisual)
            and body.operation.visual_type == "trax.artifact"
        ):
            if body.operation.record_id is None:
                raise ValueError("Artifact visual requires a record target.")
            if (
                await conn.fetchval(
                    "SELECT 1 FROM visual_report_revisions WHERE artifact_id = $1",
                    body.operation.record_id,
                )
                is None
            ):
                raise ValueError("Artifact content not found.")
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
            connected_session_id=current.connected_session_id,
        )
        await conn.execute(
            "UPDATE visual_workspaces SET revision = $2, state = $3, "
            "modified_at = clock_timestamp() WHERE id = $1",
            workspace_id,
            updated.revision,
            updated_data.model_dump(mode="json"),
        )
        await conn.execute(
            "INSERT INTO visual_workspace_operations "
            "(workspace_id, key, request_hash, response) VALUES ($1, $2, $3, $4)",
            workspace_id,
            key,
            request_hash,
            updated.model_dump(mode="json"),
        )
        if updated.revision % 16 == 0:
            # Old retries still carry a stale revision and receive 409 after
            # their receipt expires. Keep the recent 64 exact replay results.
            await conn.execute(
                "DELETE FROM visual_workspace_operations WHERE workspace_id = $1 "
                "AND key IN (SELECT key FROM visual_workspace_operations "
                "WHERE workspace_id = $1 ORDER BY created_at DESC, key DESC "
                "OFFSET 64)",
                workspace_id,
            )
        return updated


async def set_workspace_connection(
    engine: DatabaseEngine,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    body: WorkspaceConnection,
    *,
    inbound: InboundQueue,
) -> WorkspaceState | None:
    """Pair a live owned session, or disconnect it, with revision control.

    Args:
      engine: Database connection source.
      user_id: Authenticated browser account.
      workspace_id: Canvas to connect.
      body: Target session and expected canvas revision.
      inbound: In-process poller leases.

    Returns:
      state: Updated canvas, or None when the account does not own it.

    """
    async with engine.acquire() as conn, tx(conn):
        row = await conn.fetchrow(
            "SELECT id, revision, state, session_id FROM visual_workspaces "
            "WHERE id = $1 AND user_id = $2 FOR UPDATE",
            workspace_id,
            user_id,
        )
        if row is None:
            return None
        current = state_from_row(cast("Mapping[str, object]", row))
        if body.revision != current.revision:
            raise RevisionConflictError(current)
        if body.session_id is not None and (
            not inbound.has_poller(body.session_id)
            or not await _live_session_owned_by_key(
                conn,
                body.session_id,
                user_id,
            )
        ):
            raise ValueError("Session is not live or does not belong to this account.")
        updated = current.model_copy(
            update={
                "revision": current.revision + 1,
                "connected_session_id": body.session_id,
            },
        )
        await conn.execute(
            "UPDATE visual_workspaces SET revision = $2, session_id = $3, "
            "modified_at = clock_timestamp() WHERE id = $1",
            workspace_id,
            updated.revision,
            body.session_id,
        )
        return updated


def state_from_row(row: Mapping[str, object]) -> WorkspaceState:
    """Validate stored JSON before returning it as API state.

    Args:
      row: A `visual_workspaces` row with id, revision, state and session_id.

    Returns:
      state: The validated workspace state.

    """
    data = WorkspaceData.model_validate(row["state"])
    return WorkspaceState(
        id=cast(uuid.UUID, row["id"]),
        revision=from_plain(row.get("revision"), int),
        connected_session_id=cast(uuid.UUID | None, row["session_id"]),
        visuals=data.visuals,
        focused_instance=data.focused_instance,
        agent_instructions=data.agent_instructions,
        continuation_record_id=data.continuation_record_id,
    )


async def _live_session_owned_by_key(
    conn: Conn,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    api_key_id: uuid.UUID | None = None,
) -> bool:
    """Hold the live session stable until the canvas transaction commits."""
    return bool(
        await conn.fetchval(
            "SELECT 1 FROM inquiries AS sess "
            "JOIN api_keys AS credential "
            "ON credential.id = sess.agentsession_opened_by_api_key_id "
            "WHERE sess.id = $1 AND credential.user_id = $2 "
            "AND ($3::uuid IS NULL OR credential.id = $3) "
            "AND credential.revoked_at IS NULL "
            "AND sess.kind = 'AgentSession' AND sess.status = 'active' "
            "AND sess.agentsession_ended IS NULL AND sess.owner IS NOT NULL "
            "FOR SHARE OF sess, credential",
            session_id,
            user_id,
            api_key_id,
        ),
    )
