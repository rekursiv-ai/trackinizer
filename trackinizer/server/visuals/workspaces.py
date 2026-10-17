"""Validated canvas state and the operations shared by people and agents."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Final, Literal, Self

import math
import uuid

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)


if TYPE_CHECKING:
    from collections.abc import Mapping

    from trackinizer.server.visuals.catalog import (
        ParameterDescription,
        VisualCatalogBody,
        VisualDescription,
    )


ROUTE_PATTERN: Final = r"^#/[^\s\x00-\x1f\x7f]*$"
"""A `#/...` hash with no space or control character."""


type Placement = Literal["main", "left", "side", "floating"]
"""Where a visual stands: the main strip, the column left of it, the column right
of it (``side``), or floating over them."""


type PartnerChoice = Literal["shared", "local"]
"""Chat's partner: the server's shared assistant, or the owner's own helper."""


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


RETIRED_VISUAL_TYPES: frozenset[str] = frozenset({"trax.chat"})
"""Visual types a canvas once held and no catalog offers now. Chat is a column of its
own beside the canvas, so a stored canvas that still holds it reads without it."""


class WorkspaceData(BaseModel):
    """The portion of workspace state stored as one JSONB value."""

    visuals: list[VisualInstance]
    focused_instance: uuid.UUID | None = None
    agent_instructions: str | None = Field(default=None, max_length=8_192)
    continuation_record_id: uuid.UUID | None = None
    partner_choice: PartnerChoice = "shared"
    """Whose session Chat talks to: the shared assistant or the owner's helper."""

    @model_validator(mode="after")
    def _drop_retired_visuals(self) -> WorkspaceData:
        retired = {v.id for v in self.visuals if v.type in RETIRED_VISUAL_TYPES}
        if retired:
            self.visuals = [v for v in self.visuals if v.id not in retired]
            if self.focused_instance in retired:
                self.focused_instance = None
        return self


type _Fraction = Annotated[float, Field(ge=0, le=1)]
"""A share of the stage, so the layout fits any screen."""


class _TilePlace(BaseModel):
    """Where a floating window stands.

    Its top left corner is ``left`` of the stage's width and ``top`` of its height
    from the stage's own.
    """

    model_config = ConfigDict(extra="forbid")

    left: _Fraction
    top: _Fraction


class _TileSize(BaseModel):
    """How large a floating window is, as fractions of the stage's width and height."""

    model_config = ConfigDict(extra="forbid")

    width: Annotated[float, Field(gt=0, le=1)]
    height: Annotated[float, Field(gt=0, le=1)]


class _TileMemory(BaseModel):
    """How a floating window of one visual type was left."""

    model_config = ConfigDict(extra="forbid")

    collapsed: bool = False
    place: _TilePlace | None = None


type _TypeKey = Annotated[str, Field(min_length=1, max_length=64)]
"""A visual type's name."""


_MAX_TYPES: Final = 64
"""Most visual types a by-type map holds."""


class _Columns(BaseModel):
    """How wide the columns left and right of the page are, as fractions of the stage."""

    model_config = ConfigDict(extra="forbid")

    left: Annotated[float, Field(gt=0, le=1)] | None = None
    side: Annotated[float, Field(gt=0, le=1)] | None = None


class _CanvasSizes(BaseModel):
    """How a canvas's columns and windows were left, as fractions of the stage."""

    model_config = ConfigDict(extra="forbid")

    columns: _Columns | None = None
    floating: dict[_TypeKey, _TileSize] | None = None
    """Floating window sizes, by visual type."""

    tiles: dict[_TypeKey, _TileMemory] | None = None
    """Floating windows' places and folds, by visual type."""

    share: dict[_TypeKey, Annotated[float, Field(gt=0, le=100)]] | None = None
    """Each docked tile's share of its strip, by visual type (1 is an even share)."""

    @field_validator("floating", "tiles", "share")
    @classmethod
    def _bounded(cls, value: dict[str, object] | None) -> dict[str, object] | None:
        """Hold a by-type map to a count no canvas reaches."""
        if value is not None and len(value) > _MAX_TYPES:
            raise ValueError(f"At most {_MAX_TYPES} visual types.")
        return value


type _TabId = Annotated[str, Field(min_length=1, max_length=64)]
"""A canvas tab's id, which the browser makes."""


class TabVisual(BaseModel):
    """A visual a canvas tab shows, kept while another tab is open."""

    model_config = ConfigDict(extra="forbid")

    type: _TypeKey
    placement: Placement = "main"
    record_id: uuid.UUID | None = None
    params: Annotated[
        dict[
            Annotated[str, Field(max_length=64)],
            Annotated[str, Field(max_length=2_000)] | int | bool,
        ],
        Field(max_length=32),
    ] = Field(default_factory=dict)


class CanvasTab(_CanvasSizes):
    """One of a station's canvas tabs.

    The open tab is the station's canvas itself; the others keep here what they
    show (the page, the visuals and their sizes) until they are opened again,
    when the browser shows it through the canvas's own operations.
    """

    id: _TabId
    name: Annotated[str, Field(min_length=1, max_length=60)]
    page: Annotated[str, Field(max_length=512, pattern=ROUTE_PATTERN)] | None = None
    """The `#/...` page the tab was on, or None when it showed no page."""

    visuals: Annotated[list[TabVisual], Field(max_length=12)] = Field(
        default_factory=list,
    )


class StationLayout(_CanvasSizes):
    """A station's layout: resolution-free, fitted to each screen by the browser.

    Every key is optional, and a write replaces only the keys it names. Widths,
    sizes and places are fractions of the stage, never pixels; Chat's width is in
    ``ch`` (a Terminal Chat is 80 columns). The sizes are the open canvas tab's.
    """

    chat_ch: Annotated[float, Field(ge=10, le=400)] | None = None
    console: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    """The id of the Console view that is open."""

    tabs: Annotated[list[CanvasTab], Field(max_length=12)] | None = None
    """The canvas tabs, in the order they stand; none until a second one is made."""

    tab: _TabId | None = None
    """The id of the canvas tab that is open."""

    @field_validator("tabs")
    @classmethod
    def _distinct(cls, tabs: list[CanvasTab] | None) -> list[CanvasTab] | None:
        """Refuse two tabs of one id, which no switch could tell apart."""
        if tabs is not None and len({tab.id for tab in tabs}) < len(tabs):
            raise ValueError("Each canvas tab needs its own id.")
        return tabs


class CampaignLead(BaseModel):
    """The lead's model and effort in a workspace's campaign setup; either may be unset."""

    model_config = ConfigDict(extra="forbid")

    model: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    effort: Annotated[str, Field(min_length=1, max_length=20)] | None = None


class CampaignSetup(BaseModel):
    """A workspace's default campaign settings, any subset of them.

    A launch from a chat in the workspace takes each field the asker's line leaves
    unset, and the site's terms under both. Only the shape is validated here: the
    terms bound and default every field at launch, where the model catalog and the
    staffing grammar are known, so a value over a ceiling, or a model the site cannot
    run, is refused there with a plain line, never at the write.
    """

    model_config = ConfigDict(extra="forbid")

    hours: Annotated[float, Field(gt=0)] | None = None
    budget_usd: Annotated[float, Field(gt=0)] | None = None
    scientists: Annotated[int, Field(ge=1, le=8)] | None = None
    lead: CampaignLead | None = None
    staffing: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    repo: Annotated[str, Field(min_length=1, max_length=140)] | None = None

    @field_validator("hours", "budget_usd")
    @classmethod
    def _finite(cls, value: float | None) -> float | None:
        """Reject non-finite hours or budget: only a real number bounds a launch."""
        if value is not None and not math.isfinite(value):
            raise ValueError("Hours and budget must be finite numbers.")
        return value

    @field_validator("staffing")
    @classmethod
    def _staffing_shape(cls, value: str) -> str:
        """Hold staffing to MODEL:EFFORT:COUNT parts; their models bind at launch."""
        parts = [part.strip() for part in value.split(",") if part.strip()]
        if not parts or not all(_is_staffing_part(part) for part in parts):
            raise ValueError(
                "Staffing is MODEL:EFFORT:COUNT parts joined by commas, such as "
                "gpt-6.1-sol:high:2.",
            )
        return value

    @field_validator("repo")
    @classmethod
    def _repo_shape(cls, value: str) -> str:
        """Hold a repository to ``owner/name``; the asker's link to it binds at launch."""
        owner, slash, name = value.partition("/")
        if not owner.strip() or not slash or not name.strip() or "/" in name:
            raise ValueError("A repository is owner/name, such as rekursiv-ai/loop.")
        return value


class WorkstreamSuggestion(BaseModel):
    """A workstream the assistant suggests for a station, without creating it."""

    model_config = ConfigDict(extra="forbid")

    workstream_id: uuid.UUID | None = None
    title: str = Field(min_length=1, max_length=120)

    @field_validator("title")
    @classmethod
    def _trimmed_title(cls, title: str) -> str:
        """Reject blank suggestions and trim the displayed title."""
        title = title.strip()
        if not title:
            raise ValueError("A suggested workstream title is 1 to 120 characters.")
        return title


class StationData(BaseModel):
    """A canvas's station fields: the optional ``station`` JSON only the workbench reads.

    A key is stored only once it is set, so a station without a chat or a page has
    neither key.
    """

    name: str
    """The station's name, unique among its owner's: ``unnamed-workstream#N`` until
    renamed."""

    conversation_id: uuid.UUID | None = None
    page: str | None = None
    layout: StationLayout | None = None

    hidden: bool = False
    pinned: bool = False
    workstream_id: uuid.UUID | None = None
    suggestion: WorkstreamSuggestion | None = None
    session_id: uuid.UUID | None = None
    """The session this station was made for (``trax run`` placement), if any. A
    station holds many sessions, each labelled ``station:<id>``; this one only names
    why the station exists and never changes who Chat talks to."""

    clear_key: uuid.UUID | None = None
    """Internal idempotency key for a cleared station; not exposed in workspace state."""


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
    """A revisioned station state returned to browser and agent clients."""

    id: uuid.UUID
    revision: int = Field(ge=0)
    name: str
    """The station's name, unique among its owner's: ``unnamed-workstream#N`` until
    renamed."""

    conversation_id: uuid.UUID | None
    """The science chat the station holds, once a line has been sent from it."""

    page: str | None
    """The `#/...` page the station was last on, for coming back to it."""

    layout: StationLayout | None = None
    """How the station's columns and windows were left, once they were."""

    hidden: bool = False
    pinned: bool = False
    workstream_id: uuid.UUID | None = None
    suggestion: WorkstreamSuggestion | None = None
    session_id: uuid.UUID | None = None
    """The session the station was made for, if it was made for one."""

    assistant: str | None = None
    """The configured assistant's actor, or None when the server has none."""

    partner: WorkspacePartner | None = None

    preferences: dict[str, JsonValue] | None = None
    """The owner's preferences a station's assistant may see (``ASSISTANT_KEYS``), so
    it can say what is set; computed on each read, never stored with the canvas."""


class TabName(BaseModel):
    """A canvas tab as the station list names it: enough to show and open it."""

    id: str
    name: str


class WorkspaceSummary(BaseModel):
    """One of a user's stations, as the station list shows it."""

    id: uuid.UUID
    name: str
    conversation_id: uuid.UUID | None
    page: str | None
    created_at: datetime
    modified_at: datetime
    hidden: bool = False
    pinned: bool = False
    workstream_id: uuid.UUID | None = None
    suggestion: WorkstreamSuggestion | None = None
    tabs: list[TabName] = Field(default_factory=list)
    """The station's canvas tabs, in order; none until a second one is made."""

    tab: str | None = None
    """The id of the station's open canvas tab, once its layout names one."""

    @classmethod
    def of(
        cls,
        data: StationData,
        *,
        workspace_id: uuid.UUID,
        created_at: datetime,
        modified_at: datetime,
    ) -> Self:
        """Summarize station ``data``, its layout's tabs by id and name.

        Args:
          data: The station's stored fields.
          workspace_id: The station's id.
          created_at: When the station was made.
          modified_at: When the station was last changed.

        Returns:
          summary: The station as the list shows it; a layout with no tabs lists
            none, and the browser shows its first.

        """
        layout = data.layout or StationLayout()
        return cls(
            id=workspace_id,
            name=data.name,
            conversation_id=data.conversation_id,
            page=data.page,
            created_at=created_at,
            modified_at=modified_at,
            hidden=data.hidden,
            pinned=data.pinned,
            workstream_id=data.workstream_id,
            suggestion=data.suggestion,
            tabs=[TabName(id=tab.id, name=tab.name) for tab in layout.tabs or []],
            tab=layout.tab,
        )


class UpdateWorkspace(BaseModel):
    """A station's name or visibility and pin flags."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=1_000)
    hidden: bool | None = None
    pinned: bool | None = None

    @field_validator("name")
    @classmethod
    def _trimmed_and_bounded(cls, name: str | None) -> str | None:
        """Trim a supplied name and hold it to 1 to 120 characters."""
        if name is None:
            return None
        name = name.strip()
        if not name or len(name) > 120:
            raise ValueError("A station name is 1 to 120 characters.")
        return name

    @model_validator(mode="after")
    def _requires_update(self) -> Self:
        """Require at least one name or flag value."""
        if self.name is None and self.hidden is None and self.pinned is None:
            raise ValueError("At least one of name, hidden, or pinned is required.")
        return self


class NameChat(BaseModel):
    """A name the assistant suggests for an unnamed chat station."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def _trimmed_and_bounded(cls, name: str) -> str:
        name = name.strip()
        if not name or len(name) > 120:
            raise ValueError("A station name is 1 to 120 characters.")
        return name


class SetPage(BaseModel):
    """The page a station is on, as the browser reports it."""

    model_config = ConfigDict(extra="forbid")

    route: str = Field(max_length=512, pattern=ROUTE_PATTERN)
    """A `#/...` hash with no space or control character, as a navigation's."""


class SessionCampaign(BaseModel):
    """Campaign settings stored on a chat's head AgentSession.

    The whole setup, or ``None`` to clear it.
    """

    model_config = ConfigDict(extra="forbid")

    campaign: CampaignSetup | None = None


class SettingsArtifact(BaseModel):
    """The id of the principal's pinned personal settings Artifact."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: uuid.UUID


class WorkspaceSettingsRefs(BaseModel):
    """Settings rows and station metadata the assistant uses for a chat."""

    model_config = ConfigDict(extra="forbid")

    workstream_issue_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    personal_artifact_id: uuid.UUID | None = None
    name: str | None = None
    suggestion: WorkstreamSuggestion | None = None


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
    """Dismiss one visual instance."""

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
    route: str = Field(max_length=512, pattern=ROUTE_PATTERN)
    """A `#/...` hash with no space or control character."""


class Highlight(OperationModel):
    """Mark inquiries on the browser's page. An event, as navigate is."""

    kind: Literal["highlight"]
    ids: list[uuid.UUID] = Field(max_length=50)
    """The inquiries to mark; the newest event wins and an empty list clears."""


class ChatWindow(OperationModel):
    """Hide or show the browser tab's Chat window. An event, as navigate is."""

    kind: Literal["chat"]
    hidden: bool
    """True to hide Chat, false to show it again; the tab keeps no memory of it."""


class SetPreferences(OperationModel):
    """Change the station owner's preferences, as the station's agent. An event.

    It moves no visual and no revision. The keys and values are the owner's
    preferences' (``server/preferences.py``); ``None`` puts a key back to its default.
    """

    kind: Literal["preferences"]
    change: dict[str, JsonValue] = Field(min_length=1, max_length=64)
    """Keys of ``ASSISTANT_KEYS`` only: the UI's, never the user's own data."""


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
    | ChatWindow
    | SetPreferences
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
    """Start a new canvas with the default visual.

    Args:
      catalog: Trusted visual definitions and initial selection.

    Returns:
      state: Initial canvas data.

    """
    default = next(
        visual for visual in catalog.visuals if visual.type == catalog.default_visual
    )
    return WorkspaceData(
        visuals=[
            VisualInstance(
                id=uuid.uuid4(),
                type=default.type,
                version=default.version,
                placement="main",
            ),
        ],
    )


def restore_page(data: WorkspaceData, *, catalog: VisualCatalogBody) -> WorkspaceData:
    """Show the page again, in the main strip, on a canvas that has closed it.

    A navigation moves the page, so it must find one to move.

    Args:
      data: Current canvas data.
      catalog: Trusted descriptor set, whose default visual is the page.

    Returns:
      state: ``data`` itself when the page is there, else a copy with the page
        shown and focused.

    """
    if any(visual.type == catalog.default_visual for visual in data.visuals):
        return data
    return apply_operation(
        data,
        ShowVisual(
            kind="show",
            visual_type=catalog.default_visual,
            placement="main",
        ),
        catalog,
    )


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


def refuse_params(
    params: Mapping[str, str | int | bool],
    *,
    descriptor: VisualDescription,
) -> str | None:
    """Say why a visual cannot take the params a show names, or None if it can.

    One rule for every caller, the server and an agent checking before it asks: a
    name the catalog does not list, a value of another type, a string past its
    length, or an integer outside its bounds is refused. A name left out takes its
    default, which the catalog keeps valid.

    Args:
      params: The params the show sets.
      descriptor: The catalog entry of the visual it names.

    Returns:
      reason: A sentence for the refused caller, naming the bound it breaks, or None.

    """
    if set(params) - set(descriptor.parameter_schema):
        return "Visual parameters do not match the catalog schema."
    for name, value in params.items():
        reason = _refuse_value(name, value, schema=descriptor.parameter_schema[name])
        if reason is not None:
            return reason
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
        reason = refuse_params(operation.params, descriptor=descriptor)
        if reason is not None:
            raise ValueError(reason)
        params = {
            name: schema.default for name, schema in descriptor.parameter_schema.items()
        }
        params.update(operation.params)
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
    elif isinstance(operation, Navigate | Highlight | ChatWindow | SetPreferences):
        pass
    elif isinstance(operation, ChoosePartner):
        updated.partner_choice = operation.choice
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


def _refuse_value(
    name: str,
    value: str | int | bool,
    *,
    schema: ParameterDescription,
) -> str | None:
    """Say why one param's value breaks its catalog schema, or None."""
    if schema.type == "string":
        if not isinstance(value, str):
            return f"Parameter {name!r} must be a string."
        if len(value) > (schema.max_length or 0):
            return f"Parameter {name!r} exceeds maximum length of {schema.max_length}."
    elif schema.type == "boolean":
        if not isinstance(value, bool):
            return f"Parameter {name!r} must be a boolean."
    elif not isinstance(value, int) or isinstance(value, bool):
        return f"Parameter {name!r} must be an integer."
    elif schema.minimum is not None and value < schema.minimum:
        return f"Parameter {name!r} is below its minimum of {schema.minimum}."
    elif schema.maximum is not None and value > schema.maximum:
        return f"Parameter {name!r} is above its maximum of {schema.maximum}."
    return None


def _is_staffing_part(part: str) -> bool:
    """Say whether ``part`` is a ``MODEL:EFFORT:COUNT`` triple with a numeric count."""
    fields = part.split(":")
    return len(fields) == 3 and fields[2].strip().isdigit()
