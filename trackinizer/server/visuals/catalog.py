"""Trusted configgle visual definitions and their safe wire projection."""

from __future__ import annotations

from dataclasses import field
from typing import Literal, Protocol, Self

from configgle import Fig, Makeable
from pydantic import BaseModel, ConfigDict, Field, model_validator


__all__ = [
    "ContextGraphVisual",
    "FilesVisual",
    "ParameterDescription",
    "StaticVisual",
    "TimelineVisual",
    "Visual",
    "VisualCatalogBody",
    "VisualDescription",
    "Workspace",
    "default_catalog",
    "default_workspace",
]


class ParameterDescription(BaseModel):
    """One bounded parameter accepted by a visual provider."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["string", "integer", "boolean"]
    default: str | int | bool
    minimum: int | None = None
    maximum: int | None = None
    max_length: int | None = Field(default=None, ge=0, le=512)

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        """Keep every catalog default valid and accept only bounds of its type.

        Returns:
          schema: The validated parameter description.

        """
        if self.type == "string":
            if not isinstance(self.default, str) or self.max_length is None:
                raise ValueError("String parameters need a default and max_length.")
            if len(self.default) > self.max_length:
                raise ValueError("String default exceeds maximum length.")
            if self.minimum is not None or self.maximum is not None:
                raise ValueError("String parameters cannot have integer bounds.")
        elif self.type == "integer":
            if type(self.default) is not int:
                raise ValueError("Integer parameters need an integer default.")
            if self.minimum is None or self.maximum is None:
                raise ValueError("Integer parameters need minimum and maximum.")
            if self.minimum > self.maximum:
                raise ValueError("Integer minimum exceeds maximum.")
            if self.default < self.minimum or self.default > self.maximum:
                raise ValueError("Integer default is outside its bounds.")
            if self.max_length is not None:
                raise ValueError("Integer parameters cannot have a maximum length.")
        else:
            if type(self.default) is not bool:
                raise ValueError("Boolean parameters need a boolean default.")
            if (
                self.minimum is not None
                or self.maximum is not None
                or self.max_length is not None
            ):
                raise ValueError("Boolean parameters cannot have numeric bounds.")
        return self


class VisualDescription(BaseModel):
    """The inert catalog entry a browser can safely render."""

    type: str = Field(min_length=1)
    version: int = Field(ge=1)
    title: str = Field(min_length=1)
    description: str
    requires: list[Literal["record"]]
    default_size: Literal["compact", "wide"]
    parameter_schema: dict[str, ParameterDescription]
    record_kinds: list[str] | None = None
    """Kinds of record a show may target: none means any kind, empty means no record."""


class VisualCatalogBody(BaseModel):
    """Available visual types and the initial visual for a new canvas."""

    default_visual: str
    visuals: list[VisualDescription]


class Visual(Protocol):
    """A configured visual that projects one inert catalog descriptor."""

    def describe(self) -> VisualDescription:
        """Return the public description without fetching graph data."""
        ...


class StaticVisual:
    """A visual whose catalog entry is fully described by its config."""

    class Config(Fig["StaticVisual"]):
        type: str = ""
        """Namespaced visual type such as `trax.browse`; keep it stable."""

        version: int = 1
        """Renderer version; raise it when saved parameters cannot render."""

        title: str = ""
        """Name shown in the Configure panel."""

        description: str = ""
        """One sentence saying what the visual shows."""

        requires: list[Literal["record"]] = field(
            default_factory=list[Literal["record"]],
        )
        """Targets a show operation must supply for the visual."""

        default_size: Literal["compact", "wide"] = "wide"
        """Pane shape used when a show operation names no placement."""

        record_kinds: list[str] | None = None
        """Kinds of record a show may target; none means any, empty means no record."""

    def __init__(self, config: Config) -> None:
        """Keep the configured entry until projection."""
        self.config = config

    def describe(self) -> VisualDescription:
        """Describe the visual without loading its data.

        Returns:
          description: Inert catalog entry for this visual.

        """
        return VisualDescription(
            type=self.config.type,
            version=self.config.version,
            title=self.config.title,
            description=self.config.description,
            requires=self.config.requires,
            default_size=self.config.default_size,
            parameter_schema={},
            record_kinds=self.config.record_kinds,
        )


class TimelineVisual:
    """A bounded lineage and chronology of any record: leads, directions, evidence."""

    class Config(Fig["TimelineVisual"]):
        title: str = "Lineage and timeline"
        """Name shown in the Configure panel."""

        direction_limit: int = 12
        """Largest number of direct directions one timeline may request."""

        default_direction_limit: int = 8
        """Direct directions shown when the request names no limit."""

        results_per_direction: int = 5
        """Largest number of results one direction may request."""

        default_results_per_direction: int = 3
        """Results per direction shown when the request names no limit."""

    def __init__(self, config: Config) -> None:
        """Keep the configured title and limits until projection."""
        self.config = config

    def describe(self) -> VisualDescription:
        """Describe the timeline and the bounds every caller must respect.

        Returns:
          description: Inert catalog entry carrying the timeline's limits.

        """
        return VisualDescription(
            type="trax.timeline",
            version=1,
            title=self.config.title,
            description=(
                "Any record by date, under its lead issues, over its directions, "
                "results, and signed evidence."
            ),
            requires=["record"],
            default_size="wide",
            parameter_schema={
                "direction_limit": ParameterDescription(
                    type="integer",
                    default=self.config.default_direction_limit,
                    minimum=1,
                    maximum=self.config.direction_limit,
                ),
                "results_per_direction": ParameterDescription(
                    type="integer",
                    default=self.config.default_results_per_direction,
                    minimum=1,
                    maximum=self.config.results_per_direction,
                ),
            },
        )


class ContextGraphVisual:
    """A record in its graph: what lies near it lit, the ring past that dimmed."""

    class Config(Fig["ContextGraphVisual"]):
        title: str = "Context graph"
        """Name shown in the Configure panel."""

        default_hops: int = 2
        """Edges out from the record lit when the request names no reach."""

    def __init__(self, config: Config) -> None:
        """Keep the configured title and reach until projection."""
        self.config = config

    def describe(self) -> VisualDescription:
        """Describe the window and the reach and highlight a caller may set.

        Returns:
          description: Inert catalog entry carrying the window's bounds.

        """
        return VisualDescription(
            type="trax.subgraph",
            version=1,
            title=self.config.title,
            description="See a record in context: what lies near it, in the graph.",
            requires=["record"],
            default_size="wide",
            parameter_schema={
                # The window reads its ring through `/api/web/graph?focus=`,
                # which walks at most 3 hops.
                "hops": ParameterDescription(
                    type="integer",
                    default=self.config.default_hops,
                    minimum=1,
                    maximum=3,
                ),
                # Inquiry ids, comma-separated: 512 holds 13.
                "highlight": ParameterDescription(
                    type="string",
                    default="",
                    max_length=512,
                ),
            },
        )


class FilesVisual:
    """A record's files and what changed in them, each change with its diff."""

    class Config(Fig["FilesVisual"]):
        title: str = "Files"
        """Name shown in the Configure panel."""

    def __init__(self, config: Config) -> None:
        """Keep the configured title until projection."""
        self.config = config

    def describe(self) -> VisualDescription:
        """Describe the view and the narrowing a caller may set.

        Returns:
          description: Inert catalog entry carrying the narrowing's bounds.

        """
        return VisualDescription(
            type="trax.files",
            version=1,
            title=self.config.title,
            description=(
                "A record's files and what changed in them, each change with its diff."
            ),
            requires=["record"],
            default_size="wide",
            parameter_schema={
                # A path prefix (`loop/foo/`), or a glob when it holds `*` or `?`
                # (`**/*.py`), matched without case; empty shows every path.
                "path": ParameterDescription(type="string", default="", max_length=512),
                # True hides the file tree, leaving only the changed files.
                "changed": ParameterDescription(type="boolean", default=False),
                # A changed file's path, or a name every changed file ending in
                # `/<name>` matches, whose diff opens; it never narrows the list.
                "file": ParameterDescription(type="string", default="", max_length=512),
            },
        )


class Workspace:
    """The trusted composition of visual configs and its initial selection."""

    class Config(Fig["Workspace"]):
        visuals: list[Makeable[Visual]] = field(
            default_factory=lambda: [
                StaticVisual.Config(
                    type="trax.browse",
                    title="Browse",
                    description="Browse trax records by kind and query.",
                    record_kinds=[],
                ),
                ContextGraphVisual.Config(),
                TimelineVisual.Config(),
                StaticVisual.Config(
                    type="trax.artifact",
                    title="Artifact",
                    description="Read immutable shared Artifact content.",
                    requires=["record"],
                    record_kinds=["Artifact"],
                ),
                StaticVisual.Config(
                    type="trax.summary",
                    title="Campaign summary",
                    description=(
                        "What a campaign did, for readers who may not read its "
                        "transcripts: its sessions, steps, phases and beliefs."
                    ),
                    requires=["record"],
                    record_kinds=["Issue", "AgentSession"],
                ),
                FilesVisual.Config(),
            ],
        )
        """Configured visual modules available to a workspace."""

        default_visual: str = "trax.browse"
        """Visual type shown when a new workspace opens; it must need no record."""

    def __init__(self, config: Config) -> None:
        """Build the lightweight definitions and validate their identity."""
        self.visuals = [visual.make().describe() for visual in config.visuals]
        types = [visual.type for visual in self.visuals]
        if len(types) != len(set(types)):
            raise ValueError("Visual types must be unique.")
        default = self.visual(config.default_visual)
        if default is None:
            raise ValueError("Default visual must be registered.")
        if "record" in default.requires:
            raise ValueError("Default visual cannot require a record.")
        self.default_visual = config.default_visual

    def visual(self, visual_type: str) -> VisualDescription | None:
        """Return one registered description, or None when it is not registered."""
        return next(
            (visual for visual in self.visuals if visual.type == visual_type),
            None,
        )

    def catalog(self) -> VisualCatalogBody:
        """Project the trusted configuration as plain validated JSON fields."""
        return VisualCatalogBody(
            default_visual=self.default_visual,
            visuals=self.visuals,
        )


def default_workspace() -> Workspace:
    """Build the default workspace without instantiating a data provider."""
    return Workspace.Config().make()


def default_catalog() -> VisualCatalogBody:
    """Project the default workspace's catalog."""
    return default_workspace().catalog()
