"""Trusted configgle visual definitions and their safe wire projection."""

from __future__ import annotations

from dataclasses import field
from typing import Literal, Protocol, Self

from configgle import Fig, Makeable
from pydantic import BaseModel, ConfigDict, Field, model_validator


__all__ = [
    "ArtifactVisual",
    "BrowseVisual",
    "ChatVisual",
    "SubgraphVisual",
    "Visual",
    "VisualCatalogBody",
    "VisualDescription",
    "Workspace",
    "default_catalog",
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
        """Keep every catalog default valid and every string bounded.

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
        elif type(self.default) is not bool:
            raise ValueError("Boolean parameters need a boolean default.")
        elif (
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
    requires: list[Literal["record", "session"]]
    default_size: Literal["compact", "wide"]
    parameter_schema: dict[str, ParameterDescription]


class VisualCatalogBody(BaseModel):
    """Available visual types and the initial visual for a new canvas."""

    default_visual: str
    visuals: list[VisualDescription]


class Visual(Protocol):
    """A configured visual that projects one inert catalog descriptor."""

    def describe(self) -> VisualDescription:
        """Return the public description without fetching graph data."""
        ...


class BrowseVisual:
    """The existing kind list as a canvas visual."""

    class Config(Fig["BrowseVisual"]):
        title: str = "Browse"
        """Name shown in the Configure panel."""

    def __init__(self, config: Config) -> None:
        """Keep the configured title until projection."""
        self.title = config.title

    def describe(self) -> VisualDescription:
        """Describe the existing list without loading its rows.

        Returns:
          description: Inert catalog entry for Browse.

        """
        return VisualDescription(
            type="trax.browse",
            version=1,
            title=self.title,
            description="Browse trax records by kind and query.",
            requires=[],
            default_size="wide",
            parameter_schema={},
        )


class ChatVisual:
    """A session-connected conversation visual."""

    class Config(Fig["ChatVisual"]):
        title: str = "Chat"
        """Name shown in the Configure panel."""

    def __init__(self, config: Config) -> None:
        """Keep the configured title until projection."""
        self.title = config.title

    def describe(self) -> VisualDescription:
        """Describe chat without opening a session.

        Returns:
          description: Inert catalog entry for Chat.

        """
        return VisualDescription(
            type="trax.chat",
            version=1,
            title=self.title,
            description="Talk with a connected trax run session.",
            requires=["session"],
            default_size="compact",
            parameter_schema={},
        )


class SubgraphVisual:
    """A bounded context graph centered on one inquiry record."""

    class Config(Fig["SubgraphVisual"]):
        title: str = "Context graph"
        """Name shown in the Configure panel."""

    def __init__(self, config: Config) -> None:
        """Keep the configured title until projection."""
        self.title = config.title

    def describe(self) -> VisualDescription:
        """Describe the graph without fetching its records.

        Returns:
          description: Inert catalog entry for the context graph.

        """
        return VisualDescription(
            type="trax.subgraph",
            version=1,
            title=self.title,
            description="Explore a selected record and its issue lineage.",
            requires=["record"],
            default_size="wide",
            parameter_schema={},
        )


class ArtifactVisual:
    """An exact immutable published Artifact."""

    class Config(Fig["ArtifactVisual"]):
        title: str = "Artifact"
        """Name shown in the Configure panel."""

    def __init__(self, config: Config) -> None:
        self.title = config.title

    def describe(self) -> VisualDescription:
        """Describe the Artifact renderer without loading its content.

        Returns:
          description: Inert catalog entry for published Artifacts.

        """
        return VisualDescription(
            type="trax.artifact",
            version=1,
            title=self.title,
            description="Read immutable shared Artifact content.",
            requires=["record"],
            default_size="wide",
            parameter_schema={},
        )


class TimelineVisual:
    """A bounded chronology of an Issue's directions and evidence."""

    class Config(Fig["TimelineVisual"]):
        title: str = "Evidence timeline"
        """Name shown in the Configure panel."""

        direction_limit: int = 8
        """Maximum direct directions shown in one timeline."""

        results_per_direction: int = 3
        """Maximum results attached to each direction."""

    def __init__(self, config: Config) -> None:
        self.title = config.title
        self.direction_limit = config.direction_limit
        self.results_per_direction = config.results_per_direction

    def describe(self) -> VisualDescription:
        return VisualDescription(
            type="trax.timeline",
            version=1,
            title=self.title,
            description="Follow dated directions, results, and signed evidence.",
            requires=["record"],
            default_size="wide",
            parameter_schema={
                "direction_limit": ParameterDescription(
                    type="integer",
                    default=self.direction_limit,
                    minimum=1,
                    maximum=12,
                ),
                "results_per_direction": ParameterDescription(
                    type="integer",
                    default=self.results_per_direction,
                    minimum=1,
                    maximum=5,
                ),
            },
        )


class Workspace:
    """The trusted composition of visual configs and its initial selection."""

    class Config(Fig["Workspace"]):
        visuals: list[Makeable[Visual]] = field(
            default_factory=lambda: [
                BrowseVisual.Config(),
                ChatVisual.Config(),
                SubgraphVisual.Config(),
                TimelineVisual.Config(),
                ArtifactVisual.Config(),
            ],
        )
        """Configured visual modules available to a workspace."""

        default_visual: str = "trax.browse"
        """Visual type shown when a new workspace opens."""

    def __init__(self, config: Config) -> None:
        """Build the lightweight definitions and validate their identity."""
        self.visuals = [visual.make().describe() for visual in config.visuals]
        types = [visual.type for visual in self.visuals]
        if len(types) != len(set(types)):
            raise ValueError("Visual types must be unique.")
        if config.default_visual not in types:
            raise ValueError("Default visual must be registered.")
        self.default_visual = config.default_visual

    def catalog(self) -> VisualCatalogBody:
        """Project the trusted configuration as plain validated JSON fields."""
        return VisualCatalogBody(
            default_visual=self.default_visual,
            visuals=self.visuals,
        )


def default_catalog() -> VisualCatalogBody:
    """Build the default catalog without instantiating a data provider."""
    return Workspace.Config().make().catalog()
