"""Validated canvas state and the operations shared by people and agents."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal

import uuid

from pydantic import BaseModel, ConfigDict, Field


if TYPE_CHECKING:
    from trackinizer.server.visuals.catalog import VisualCatalogBody


type Placement = Literal["main", "side", "floating"]


class FloatingRect(BaseModel):
    """Bounded desktop coordinates for a floating canvas pane."""

    model_config = ConfigDict(extra="forbid")

    left: int = Field(ge=0, le=10_000)
    top: int = Field(ge=0, le=10_000)
    width: int = Field(ge=240, le=2_000)
    height: int = Field(ge=180, le=2_000)


class VisualInstance(BaseModel):
    """One configured module on a canvas."""

    id: uuid.UUID
    type: str
    version: int = Field(ge=1)
    placement: Placement = "main"
    record_id: uuid.UUID | None = None
    params: dict[str, str | int | bool] = Field(default_factory=dict)
    floating_rect: FloatingRect | None = None


class WorkspaceData(BaseModel):
    """The portion of workspace state stored as one JSONB value."""

    visuals: list[VisualInstance]
    focused_instance: uuid.UUID | None = None
    agent_instructions: str | None = Field(default=None, max_length=8_192)
    continuation_record_id: uuid.UUID | None = None


class WorkspaceState(WorkspaceData):
    """A revisioned canvas state returned to browser and agent clients."""

    id: uuid.UUID
    revision: int = Field(ge=0)
    connected_session_id: uuid.UUID | None = None


class WorkspaceConnection(BaseModel):
    """Browser request to connect or disconnect a live agent session."""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0)
    session_id: uuid.UUID | None


class ConnectableSession(BaseModel):
    """One live trax session the signed-in user may pair with a canvas."""

    id: uuid.UUID
    title: str
    actor: str
    cli: str | None = None


class WorkspaceMessageRequest(BaseModel):
    """A browser message scoped to a persisted chat visual."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=16_384)
    chat_instance_id: uuid.UUID | None = None
    expected_record_id: uuid.UUID | None = None


class WorkspaceMessageReceipt(BaseModel):
    """The paired session and queue depth recorded by the original send.

    An idempotent replay returns that original depth, even after a drain.
    """

    session_id: uuid.UUID
    queued: int


class WorkspaceConnectionStatus(BaseModel):
    """Direct status of a canvas's stored session pairing."""

    status: Literal["live", "ended", "unavailable"]
    session_id: uuid.UUID | None = None
    actor: str | None = None
    cli: str | None = None


class OperationModel(BaseModel):
    """Reject misspelled fields at the agent and browser write boundary."""

    model_config = ConfigDict(extra="forbid")


class ShowVisual(OperationModel):
    """Show one catalog visual, or focus its existing instance."""

    kind: Literal["show"]
    visual_type: str
    placement: Placement | None = None
    record_id: uuid.UUID | None = None
    params: dict[str, str | int | bool] = Field(default_factory=dict)


class HideVisual(OperationModel):
    """Dismiss one visual instance without deleting a saved preset."""

    kind: Literal["hide"]
    instance_id: uuid.UUID


class FocusVisual(OperationModel):
    """Bring one instance into focus."""

    kind: Literal["focus"]
    instance_id: uuid.UUID


class PlaceVisual(OperationModel):
    """Move one instance between the primary and secondary pane."""

    kind: Literal["place"]
    instance_id: uuid.UUID
    placement: Placement


type Operation = Annotated[
    ShowVisual | HideVisual | FocusVisual | PlaceVisual,
    Field(discriminator="kind"),
]


class ApplyWorkspaceOperation(OperationModel):
    """A compare-and-swap operation with its expected revision."""

    revision: int = Field(ge=0)
    operation: Operation


class WorkspaceConflict(BaseModel):
    """A stale operation's conflict response, including live state."""

    detail: str
    current: WorkspaceState


def initial_data(catalog: VisualCatalogBody) -> WorkspaceData:
    """Start a new canvas with the backend's configured default visual.

    Args:
      catalog: Trusted visual definitions and initial selection.

    Returns:
      state: Initial canvas data.

    """
    default = next(
        visual for visual in catalog.visuals if visual.type == catalog.default_visual
    )
    instance = VisualInstance(
        id=uuid.uuid4(),
        type=default.type,
        version=default.version,
        placement="main",
    )
    return WorkspaceData(visuals=[instance])


def apply_operation(
    data: WorkspaceData,
    operation: Operation,
    catalog: VisualCatalogBody,
) -> WorkspaceData:
    """Validate and apply one operation to a copy of the current state.

    Args:
      data: Current canvas data.
      operation: Requested visual change.
      catalog: Trusted descriptor set.

    Returns:
      state: New canvas data.

    """
    updated = data.model_copy(deep=True)
    if isinstance(operation, ShowVisual):
        descriptor = next(
            (
                visual
                for visual in catalog.visuals
                if visual.type == operation.visual_type
            ),
            None,
        )
        if descriptor is None:
            raise ValueError(f"Unknown visual type {operation.visual_type!r}.")
        if "record" in descriptor.requires and operation.record_id is None:
            raise ValueError("Visual requires a record target.")
        if set(operation.params) - set(descriptor.parameter_schema):
            raise ValueError("Visual parameters do not match the catalog schema.")
        params = {
            name: schema.default for name, schema in descriptor.parameter_schema.items()
        }
        params.update(operation.params)
        for name, schema in descriptor.parameter_schema.items():
            value = params[name]
            if schema.type == "string":
                if not isinstance(value, str):
                    raise ValueError(f"Parameter {name!r} must be a string.")
                if len(value) > (schema.max_length or 0):
                    raise ValueError(f"Parameter {name!r} exceeds maximum length.")
            if schema.type == "boolean" and not isinstance(value, bool):
                raise ValueError(f"Parameter {name!r} must be a boolean.")
            if schema.type == "integer":
                if not isinstance(value, int) or isinstance(value, bool):
                    raise ValueError(f"Parameter {name!r} must be an integer.")
                if schema.minimum is not None and value < schema.minimum:
                    raise ValueError(f"Parameter {name!r} is below its minimum.")
                if schema.maximum is not None and value > schema.maximum:
                    raise ValueError(f"Parameter {name!r} is above its maximum.")
        existing = next(
            (visual for visual in updated.visuals if visual.type == descriptor.type),
            None,
        )
        if existing is not None:
            existing.version = descriptor.version
            if operation.placement is not None:
                existing.placement = operation.placement
                if operation.placement != "floating":
                    existing.floating_rect = None
            if "record_id" in operation.model_fields_set:
                existing.record_id = operation.record_id
            if "params" in operation.model_fields_set:
                existing.params = params
            updated.focused_instance = existing.id
            return updated
        if len(updated.visuals) >= 12:
            raise ValueError("A workspace can show at most 12 visuals.")
        instance = VisualInstance(
            id=uuid.uuid4(),
            type=descriptor.type,
            version=descriptor.version,
            placement=operation.placement
            or ("main" if descriptor.default_size == "wide" else "side"),
            record_id=operation.record_id,
            params=params,
        )
        updated.visuals.append(instance)
        updated.focused_instance = instance.id
    elif isinstance(operation, HideVisual):
        if not any(visual.id == operation.instance_id for visual in updated.visuals):
            raise ValueError("Visual instance not found.")
        updated.visuals = [
            visual for visual in updated.visuals if visual.id != operation.instance_id
        ]
        if updated.focused_instance == operation.instance_id:
            updated.focused_instance = None
    elif isinstance(operation, FocusVisual):
        if not any(visual.id == operation.instance_id for visual in updated.visuals):
            raise ValueError("Visual instance not found.")
        updated.focused_instance = operation.instance_id
    else:
        instance = next(
            (
                visual
                for visual in updated.visuals
                if visual.id == operation.instance_id
            ),
            None,
        )
        if instance is None:
            raise ValueError("Visual instance not found.")
        instance.placement = operation.placement
        if operation.placement != "floating":
            instance.floating_rect = None
    return updated
