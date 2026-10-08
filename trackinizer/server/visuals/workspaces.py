"""Validated canvas state and the operations shared by people and agents."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Final, Literal

import uuid

from pydantic import BaseModel, ConfigDict, Field

from trackinizer.wire.wire_chats import ChatMessage


if TYPE_CHECKING:
    from trackinizer.server.visuals.catalog import (
        VisualCatalogBody,
        VisualDescription,
    )


type Placement = Literal["main", "side", "floating"]


type PartnerChoice = Literal["shared", "local"]
"""Chat's partner: the server's shared assistant, or the owner's own helper."""


_ROUTE: Final = r"^#/[^\s\x00-\x1f\x7f]*$"
"""A `#/...` hash with no space or control character."""


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
    partner_choice: PartnerChoice = "shared"
    """Whose session Chat talks to: the shared assistant or the owner's helper."""


class WorkspacePartner(BaseModel):
    """Who a canvas's Chat talks to now, computed on every read and never stored.

    With the shared choice, the partner is the assistant's newest live session; an
    assistant with no live session is still named, as unavailable. With the local
    choice, it is the owner's newest live `trax helper` session, or unavailable.
    """

    session_id: uuid.UUID | None
    actor: str | None
    """The configured name of the assistant, or the local helper's actor."""

    cli: str | None
    status: Literal["live", "unavailable"]
    kind: PartnerChoice = "shared"
    """Which choice it answers for."""


class WorkspaceState(WorkspaceData):
    """A revisioned canvas state returned to browser and agent clients."""

    id: uuid.UUID
    revision: int = Field(ge=0)
    assistant: str | None = None
    """The configured assistant's actor, or None when the server has none."""

    partner: WorkspacePartner | None = None


class WorkspaceMessageRequest(BaseModel):
    """A browser message scoped to a persisted chat visual."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(pattern=r"\S", max_length=16_384)
    chat_instance_id: uuid.UUID | None = None
    expected_record_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    """The conversation to continue; none starts one."""

    page: str | None = Field(default=None, max_length=512, pattern=_ROUTE)
    """The `#/...` address the sender is on as they send."""

    trail: list[Annotated[str, Field(max_length=512, pattern=_ROUTE)]] = Field(
        default_factory=list,
        max_length=8,
    )
    """The addresses the sender came through before it, oldest first."""


class WorkspaceMessageReceipt(BaseModel):
    """The partner session and conversation of the original send.

    An idempotent replay returns the original receipt.
    """

    session_id: uuid.UUID | None
    """None when the partner session record was deleted since."""

    conversation_id: uuid.UUID
    message: ChatMessage
    """The user's line as stored."""


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


class Navigate(OperationModel):
    """Move the browser's page. An event: no visual and no revision changes."""

    kind: Literal["navigate"]
    route: str = Field(max_length=512, pattern=r"^#/[^\s\x00-\x1f\x7f]*$")
    """A `#/...` hash with no space or control character."""


class Highlight(OperationModel):
    """Mark inquiries on the browser's page. An event, as navigate is."""

    kind: Literal["highlight"]
    ids: list[uuid.UUID] = Field(max_length=50)
    """The inquiries to mark; the newest event wins and an empty list clears."""


class ChoosePartner(OperationModel):
    """Choose whose session the canvas's Chat talks to."""

    kind: Literal["partner"]
    choice: PartnerChoice


type Operation = Annotated[
    ShowVisual
    | HideVisual
    | FocusVisual
    | PlaceVisual
    | Navigate
    | Highlight
    | ChoosePartner,
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
    """Start a new canvas with the default visual, and Chat floating over it if offered.

    Args:
      catalog: Trusted visual definitions and initial selection.

    Returns:
      state: Initial canvas data.

    """
    default = next(
        visual for visual in catalog.visuals if visual.type == catalog.default_visual
    )
    chat = next(
        (
            visual
            for visual in catalog.visuals
            if visual.type == "trax.chat" and visual is not default
        ),
        None,
    )
    visuals = [
        VisualInstance(
            id=uuid.uuid4(),
            type=default.type,
            version=default.version,
            placement="main",
        ),
    ]
    if chat is not None:
        visuals.append(
            VisualInstance(
                id=uuid.uuid4(),
                type=chat.type,
                version=chat.version,
                placement="floating",
            ),
        )
    return WorkspaceData(visuals=visuals)


def refuse_record(
    operation: ShowVisual,
    *,
    descriptor: VisualDescription,
    kind: str | None,
) -> str | None:
    """Say why a visual cannot show the record a show names, or None if it can.

    One rule for every caller, the server and a stand-in for it alike: a visual
    whose catalog takes no record refuses any, and one that lists kinds refuses a
    record of another kind, or one that does not exist.

    Args:
      operation: The show being applied.
      descriptor: The catalog entry of the visual it names.
      kind: The record's kind, or None when it does not exist. Not read for a
        visual that takes no record or any kind.

    Returns:
      reason: A sentence for the refused caller, or None.

    """
    if operation.record_id is None:
        return None
    if descriptor.record_kinds == []:
        return "Visual takes no record target."
    if descriptor.record_kinds is not None and kind not in descriptor.record_kinds:
        return (
            f"{descriptor.title} shows {' or '.join(descriptor.record_kinds)} "
            f"records, not {kind or 'an unknown record'}."
        )
    return None


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
        if descriptor.record_kinds == [] and (
            reason := refuse_record(operation, descriptor=descriptor, kind=None) or ""
        ):
            raise ValueError(reason)
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
    elif isinstance(operation, Navigate | Highlight):
        pass
    elif isinstance(operation, ChoosePartner):
        updated.partner_choice = operation.choice
    elif isinstance(operation, HideVisual):
        if not any(visual.id == operation.instance_id for visual in updated.visuals):
            raise ValueError("Visual instance not found.")
        if any(
            visual.id == operation.instance_id and visual.type == catalog.default_visual
            for visual in updated.visuals
        ):
            raise ValueError("The default visual is the page and cannot be hidden.")
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
