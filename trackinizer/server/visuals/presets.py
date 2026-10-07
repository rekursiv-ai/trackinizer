"""Owned, durable canvas snapshots that never capture an agent connection."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, cast

import hashlib
import json
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

from trackinizer.lib.codec import from_plain
from trackinizer.server.notify import tx
from trackinizer.server.visuals.workspace_store import (
    ReplayConflictError,
    RevisionConflictError,
    WorkspaceDisabledError,
    state_from_row,
)
from trackinizer.server.visuals.workspaces import (
    FloatingRect,
    WorkspaceData,
    WorkspaceState,
)


if TYPE_CHECKING:
    from collections.abc import Mapping

    from trackinizer.lib.postgres import Conn, DatabaseEngine


class PresetRequest(BaseModel):
    """Reject unknown client fields at the durable snapshot boundary."""

    model_config = ConfigDict(extra="forbid")


class CreatePreset(PresetRequest):
    """Save one revision of an owned workspace as a named workflow."""

    workspace_id: uuid.UUID
    revision: int = Field(ge=0)
    name: str = Field(min_length=1, max_length=120, pattern=r"\S")
    agent_instructions: str | None = Field(default=None, max_length=8_192)
    continuation_record_id: uuid.UUID | None = None
    floating_rects: dict[uuid.UUID, FloatingRect] = Field(default_factory=dict)


class OpenPreset(PresetRequest):
    """Restore a snapshot only against the caller's expected canvas revision."""

    workspace_id: uuid.UUID
    revision: int = Field(ge=0)


class UpdatePreset(PresetRequest):
    """Modify saved workflow metadata without replacing its visual snapshot."""

    name: str | None = Field(default=None, min_length=1, max_length=120, pattern=r"\S")
    agent_instructions: str | None = Field(default=None, max_length=8_192)
    continuation_record_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def check_fields(self) -> UpdatePreset:
        """Require at least one mutable field and a non-null name if supplied."""
        if not self.model_fields_set or (
            "name" in self.model_fields_set and self.name is None
        ):
            raise ValueError("A valid preset field is required.")
        return self


class WorkspacePreset(BaseModel):
    """A named snapshot safe to render or reopen on another device."""

    id: uuid.UUID
    name: str
    agent_instructions: str | None
    continuation_record_id: uuid.UUID | None
    state: WorkspaceData
    created_at: datetime
    modified_at: datetime


async def list_presets(
    engine: DatabaseEngine,
    user_id: uuid.UUID,
) -> list[WorkspacePreset]:
    """List only snapshots owned by this account, newest first.

    Args:
      engine: Database connection source.
      user_id: Signed-in account.

    Returns:
      presets: At most 100 owned snapshots.

    """
    async with engine.acquire() as conn:
        if not await conn.fetchval(
            "SELECT visual_workspace_enabled FROM users WHERE id = $1",
            user_id,
        ):
            raise WorkspaceDisabledError("Visual workspace is disabled")
        rows = await conn.fetch(
            "SELECT id, name, state, created_at, modified_at "
            "FROM visual_workspace_presets WHERE user_id = $1 "
            "ORDER BY modified_at DESC, id DESC LIMIT 100",
            user_id,
        )
    return [_preset_from_row(cast("Mapping[str, object]", row)) for row in rows]


async def read_preset(
    engine: DatabaseEngine,
    user_id: uuid.UUID,
    preset_id: uuid.UUID,
) -> WorkspacePreset | None:
    """Read an owned snapshot; foreign ids are indistinguishable from missing ids.

    Args:
      engine: Database connection source.
      user_id: Signed-in account.
      preset_id: Requested snapshot.

    Returns:
      preset: Owned snapshot or None.

    """
    async with engine.acquire() as conn:
        if not await conn.fetchval(
            "SELECT visual_workspace_enabled FROM users WHERE id = $1",
            user_id,
        ):
            raise WorkspaceDisabledError("Visual workspace is disabled")
        row = await conn.fetchrow(
            "SELECT id, name, state, created_at, modified_at "
            "FROM visual_workspace_presets WHERE id = $1 AND user_id = $2",
            preset_id,
            user_id,
        )
    return _preset_from_row(cast("Mapping[str, object]", row)) if row else None


async def create_preset(
    engine: DatabaseEngine,
    user_id: uuid.UUID,
    body: CreatePreset,
    key: uuid.UUID,
) -> WorkspacePreset | None:
    """Snapshot a stable source revision, rects, and reusable guidance atomically.

    Args:
      engine: Database connection source.
      user_id: Signed-in account.
      body: Expected source revision and durable workflow fields.
      key: Idempotency key retained with the source workspace.

    Returns:
      preset: Created snapshot or None for a foreign workspace.

    """
    async with engine.acquire() as conn, tx(conn):
        enabled = await conn.fetchval(
            "SELECT visual_workspace_enabled FROM users WHERE id = $1 FOR UPDATE",
            user_id,
        )
        if not enabled:
            raise WorkspaceDisabledError("Visual workspace is disabled")
        row = await conn.fetchrow(
            "SELECT id, revision, state, session_id FROM visual_workspaces "
            "WHERE id = $1 AND user_id = $2 FOR UPDATE",
            body.workspace_id,
            user_id,
        )
        if row is None:
            return None
        source = state_from_row(cast("Mapping[str, object]", row))
        request_hash = _request_hash("create", body)
        receipt = await conn.fetchrow(
            "SELECT request_hash, response FROM visual_workspace_operations "
            "WHERE workspace_id = $1 AND key = $2",
            body.workspace_id,
            key,
        )
        if receipt is not None:
            if receipt["request_hash"] != request_hash:
                raise ReplayConflictError(source)
            return WorkspacePreset.model_validate(receipt["response"])
        if source.revision != body.revision:
            raise RevisionConflictError(source)
        count = from_plain(
            await conn.fetchval(
                "SELECT count(*) FROM visual_workspace_presets WHERE user_id = $1",
                user_id,
            ),
            int,
        )
        if count >= 100:
            raise ValueError("An account can save at most 100 presets.")
        snapshot = WorkspaceData(
            visuals=source.visuals,
            focused_instance=source.focused_instance,
            agent_instructions=(
                body.agent_instructions
                if "agent_instructions" in body.model_fields_set
                else source.agent_instructions
            ),
            continuation_record_id=(
                body.continuation_record_id
                if "continuation_record_id" in body.model_fields_set
                else source.continuation_record_id
            ),
        ).model_copy(deep=True)
        for instance_id, rect in body.floating_rects.items():
            visual = next(
                (item for item in snapshot.visuals if item.id == instance_id),
                None,
            )
            if visual is None or visual.placement != "floating":
                raise ValueError("Floating rectangle must target a floating visual.")
            visual.floating_rect = rect
        created = await conn.fetchrow(
            "INSERT INTO visual_workspace_presets (id, user_id, name, state) "
            "VALUES ($1, $2, $3, $4) "
            "RETURNING id, name, state, created_at, modified_at",
            uuid.uuid4(),
            user_id,
            body.name,
            snapshot.model_dump(mode="json"),
        )
        if created is None:
            raise RuntimeError("Preset insert returned no row.")
        preset = _preset_from_row(cast("Mapping[str, object]", created))
        await conn.execute(
            "INSERT INTO visual_workspace_operations "
            "(workspace_id, key, request_hash, response) VALUES ($1, $2, $3, $4)",
            body.workspace_id,
            key,
            request_hash,
            preset.model_dump(mode="json"),
        )
        await _trim_receipts(conn, body.workspace_id)
        return preset


async def open_preset(
    engine: DatabaseEngine,
    user_id: uuid.UUID,
    preset_id: uuid.UUID,
    body: OpenPreset,
    key: uuid.UUID,
) -> WorkspaceState | None:
    """Restore a visual snapshot and disconnect any previous session atomically.

    Args:
      engine: Database connection source.
      user_id: Signed-in account.
      preset_id: Snapshot to restore.
      body: Owned target workspace and expected revision.
      key: Idempotency key retained with the target workspace.

    Returns:
      state: Restored workspace or None for a foreign resource.

    """
    async with engine.acquire() as conn, tx(conn):
        if not await conn.fetchval(
            "SELECT visual_workspace_enabled FROM users WHERE id = $1",
            user_id,
        ):
            raise WorkspaceDisabledError("Visual workspace is disabled")
        row = await conn.fetchrow(
            "SELECT id, revision, state, session_id FROM visual_workspaces "
            "WHERE id = $1 AND user_id = $2 FOR UPDATE",
            body.workspace_id,
            user_id,
        )
        if row is None:
            return None
        current = state_from_row(cast("Mapping[str, object]", row))
        request_hash = _request_hash("open", body, preset_id=preset_id)
        receipt = await conn.fetchrow(
            "SELECT request_hash, response FROM visual_workspace_operations "
            "WHERE workspace_id = $1 AND key = $2",
            body.workspace_id,
            key,
        )
        if receipt is not None:
            if receipt["request_hash"] != request_hash:
                raise ReplayConflictError(current)
            return WorkspaceState.model_validate(receipt["response"])
        preset = await conn.fetchrow(
            "SELECT state FROM visual_workspace_presets WHERE id = $1 AND user_id = $2",
            preset_id,
            user_id,
        )
        if preset is None:
            return None
        if current.revision != body.revision:
            raise RevisionConflictError(current)
        data = WorkspaceData.model_validate(preset["state"])
        revision = current.revision + 1
        await conn.execute(
            "UPDATE visual_workspaces SET revision = $2, state = $3, "
            "session_id = NULL, modified_at = clock_timestamp() WHERE id = $1",
            body.workspace_id,
            revision,
            data.model_dump(mode="json"),
        )
        state = WorkspaceState(
            id=body.workspace_id,
            revision=revision,
            connected_session_id=None,
            visuals=data.visuals,
            focused_instance=data.focused_instance,
            agent_instructions=data.agent_instructions,
            continuation_record_id=data.continuation_record_id,
        )
        await conn.execute(
            "INSERT INTO visual_workspace_operations "
            "(workspace_id, key, request_hash, response) VALUES ($1, $2, $3, $4)",
            body.workspace_id,
            key,
            request_hash,
            state.model_dump(mode="json"),
        )
        await _trim_receipts(conn, body.workspace_id)
        return state


async def update_preset(
    engine: DatabaseEngine,
    user_id: uuid.UUID,
    preset_id: uuid.UUID,
    body: UpdatePreset,
) -> WorkspacePreset | None:
    """Edit an owned preset's name and optional guidance, preserving visuals.

    Args:
      engine: Database connection source.
      user_id: Signed-in account.
      preset_id: Snapshot to update.
      body: Supplied metadata fields.

    Returns:
      preset: Updated snapshot or None for a foreign id.

    """
    async with engine.acquire() as conn, tx(conn):
        if not await conn.fetchval(
            "SELECT visual_workspace_enabled FROM users WHERE id = $1",
            user_id,
        ):
            raise WorkspaceDisabledError("Visual workspace is disabled")
        row = await conn.fetchrow(
            "SELECT id, name, state, created_at, modified_at "
            "FROM visual_workspace_presets WHERE id = $1 AND user_id = $2 FOR UPDATE",
            preset_id,
            user_id,
        )
        if row is None:
            return None
        current = _preset_from_row(cast("Mapping[str, object]", row))
        data = current.state.model_copy(deep=True)
        if "agent_instructions" in body.model_fields_set:
            data.agent_instructions = body.agent_instructions
        if "continuation_record_id" in body.model_fields_set:
            data.continuation_record_id = body.continuation_record_id
        updated = await conn.fetchrow(
            "UPDATE visual_workspace_presets SET name = $3, state = $4, "
            "modified_at = clock_timestamp() WHERE id = $1 AND user_id = $2 "
            "RETURNING id, name, state, created_at, modified_at",
            preset_id,
            user_id,
            body.name if "name" in body.model_fields_set else current.name,
            data.model_dump(mode="json"),
        )
    if updated is None:
        raise RuntimeError("Preset update returned no row.")
    return _preset_from_row(cast("Mapping[str, object]", updated))


async def delete_preset(
    engine: DatabaseEngine,
    user_id: uuid.UUID,
    preset_id: uuid.UUID,
) -> bool:
    """Delete an owned preset without changing any live workspace.

    Args:
      engine: Database connection source.
      user_id: Signed-in account.
      preset_id: Snapshot to delete.

    Returns:
      removed: Whether an owned row was deleted.

    """
    async with engine.acquire() as conn:
        if not await conn.fetchval(
            "SELECT visual_workspace_enabled FROM users WHERE id = $1",
            user_id,
        ):
            raise WorkspaceDisabledError("Visual workspace is disabled")
        result = await conn.execute(
            "DELETE FROM visual_workspace_presets WHERE id = $1 AND user_id = $2",
            preset_id,
            user_id,
        )
    return int(result.rsplit(" ", 1)[-1]) == 1


def _preset_from_row(row: Mapping[str, object]) -> WorkspacePreset:
    """Decode a stored preset without treating database JSON as trusted input."""
    data = WorkspaceData.model_validate(row["state"])
    return WorkspacePreset(
        id=cast(uuid.UUID, row["id"]),
        name=cast(str, row["name"]),
        agent_instructions=data.agent_instructions,
        continuation_record_id=data.continuation_record_id,
        state=data,
        created_at=cast(datetime, row["created_at"]),
        modified_at=cast(datetime, row["modified_at"]),
    )


def _request_hash(
    kind: str,
    body: CreatePreset | OpenPreset,
    *,
    preset_id: uuid.UUID | None = None,
) -> str:
    """Bind a replay key to the exact request and mutation kind."""
    canonical = json.dumps(
        {
            "kind": kind,
            "preset_id": str(preset_id) if preset_id is not None else None,
            "body": body.model_dump(mode="json", exclude_unset=True),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


async def _trim_receipts(conn: Conn, workspace_id: uuid.UUID) -> None:
    """Keep retries bounded even when presets change without canvas operations."""
    await conn.execute(
        "DELETE FROM visual_workspace_operations WHERE workspace_id = $1 "
        "AND key IN (SELECT key FROM visual_workspace_operations "
        "WHERE workspace_id = $1 ORDER BY created_at DESC, key DESC OFFSET 64)",
        workspace_id,
    )
