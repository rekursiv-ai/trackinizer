"""The visual catalog validates its own contract and has one source of limits."""

from __future__ import annotations

from pydantic import ValidationError

import pytest

from trackinizer.server.visuals.catalog import (
    ParameterDescription,
    StaticVisual,
    TimelineVisual,
    Workspace,
    default_catalog,
    default_workspace,
)


def test_default_workspace_builds_the_default_catalog() -> None:
    """The built workspace is the one place the default catalog comes from."""
    assert default_workspace().catalog() == default_catalog()
    assert [visual.type for visual in default_catalog().visuals] == [
        "trax.browse",
        "trax.chat",
        "trax.subgraph",
        "trax.timeline",
        "trax.artifact",
    ]


def test_static_visual_is_configured_by_data() -> None:
    """One class serves every visual that has no parameters."""
    visual = StaticVisual.Config(
        type="x.notes",
        title="Notes",
        description="Free-form notes.",
        requires=["session"],
        default_size="compact",
    ).make()
    described = visual.describe()
    assert (described.type, described.title, described.requires) == (
        "x.notes",
        "Notes",
        ["session"],
    )
    assert described.default_size == "compact"
    assert described.parameter_schema == {}


def test_default_visual_requiring_a_record_is_rejected() -> None:
    """A new canvas cannot open on a tile that has no record to render."""
    with pytest.raises(ValueError, match="record"):
        Workspace.Config(default_visual="trax.subgraph").make()


def test_timeline_config_raises_the_descriptor_bound_instead_of_failing() -> None:
    """A configured ceiling above the old literal flows into the descriptor."""
    timeline = TimelineVisual.Config(direction_limit=20, results_per_direction=9)
    schema = timeline.make().describe().parameter_schema
    assert (schema["direction_limit"].default, schema["direction_limit"].maximum) == (
        8,
        20,
    )
    assert (
        schema["results_per_direction"].default,
        schema["results_per_direction"].maximum,
    ) == (3, 9)
    workspace = Workspace.Config(
        visuals=[StaticVisual.Config(type="x.notes", title="Notes"), timeline],
        default_visual="x.notes",
    ).make()
    registered = workspace.visual("trax.timeline")
    assert registered is not None
    assert registered.parameter_schema["direction_limit"].maximum == 20


def test_timeline_default_above_its_ceiling_is_a_config_error() -> None:
    """A default the ceiling forbids is rejected when the catalog is built."""
    timeline = TimelineVisual.Config(default_direction_limit=13)
    with pytest.raises(ValueError, match="outside its bounds"):
        timeline.make().describe()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"type": "integer", "default": 1, "minimum": 0, "maximum": 2, "max_length": 5},
        {"type": "boolean", "default": True, "max_length": 5},
        {"type": "boolean", "default": True, "minimum": 0},
        {"type": "string", "default": "", "max_length": 5, "maximum": 3},
    ],
)
def test_parameter_bounds_that_do_not_apply_to_the_type_are_rejected(
    kwargs: dict[str, object],
) -> None:
    """Every type rejects the bounds that belong to the other types."""
    with pytest.raises(ValidationError, match="cannot have"):
        ParameterDescription.model_validate(kwargs)


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
