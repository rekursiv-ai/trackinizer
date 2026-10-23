"""The visual catalog validates its own contract and has one source of limits."""

from __future__ import annotations

from pydantic import ValidationError

import pytest

from trackinizer.server.visuals.catalog import (
    RECORD_VISUAL,
    ContextGraphVisual,
    FilesVisual,
    ParameterDescription,
    StaticVisual,
    TerminalVisual,
    TimelineVisual,
    Workspace,
    default_catalog,
    default_workspace,
    offer_record_visual,
)


def test_default_workspace_builds_the_default_catalog() -> None:
    """The built workspace is the one place the default catalog comes from."""
    assert default_workspace().catalog() == default_catalog()
    assert [visual.type for visual in default_catalog().visuals] == [
        "trax.browse",
        "trax.subgraph",
        "trax.timeline",
        "trax.artifact",
        "trax.summary",
        "trax.files",
        "trax.terminal",
    ]


def test_a_type_that_is_not_registered_has_no_description() -> None:
    """A show of an unknown type is refused by the lookup, not by an exception."""
    workspace = default_workspace()
    assert workspace.visual("x.unknown") is None
    summary = workspace.visual("trax.summary")
    assert summary is not None
    assert (summary.requires, summary.record_kinds) == (
        ["record"],
        ["Issue", "AgentSession"],
    )


def test_static_visual_is_configured_by_data() -> None:
    """One class serves every visual that has no parameters."""
    visual = StaticVisual.Config(
        type="x.notes",
        title="Notes",
        description="Free-form notes.",
        requires=["record"],
        default_size="compact",
    ).make()
    described = visual.describe()
    assert (described.type, described.title, described.requires) == (
        "x.notes",
        "Notes",
        ["record"],
    )
    assert described.default_size == "compact"
    assert described.parameter_schema == {}


def test_browse_is_the_page_and_takes_no_record_or_parameter() -> None:
    """Browse draws the page: no record, no parameters, version unchanged."""
    browse = default_workspace().visual("trax.browse")
    assert browse is not None
    assert browse.version == 1
    assert browse.record_kinds == []
    assert browse.parameter_schema == {}


def test_record_visuals_name_the_kinds_they_accept() -> None:
    """The catalog says what each visual can draw, so a show can be refused early."""
    kinds = {visual.type: visual.record_kinds for visual in default_catalog().visuals}
    assert kinds == {
        "trax.browse": [],
        "trax.subgraph": None,
        "trax.timeline": None,
        "trax.artifact": ["Artifact"],
        "trax.summary": ["Issue", "AgentSession"],
        "trax.files": None,
        "trax.terminal": None,
    }


def test_the_record_tile_is_offered_once_and_is_no_default_visual() -> None:
    """A site whose app draws record tiles offers them; the default catalog does not."""
    visuals = Workspace.Config()
    offer_record_visual(visuals)
    offer_record_visual(visuals)
    offered = [
        visual
        for visual in visuals.make().catalog().visuals
        if visual.type == RECORD_VISUAL
    ]
    assert RECORD_VISUAL not in {visual.type for visual in default_catalog().visuals}
    assert len(offered) == 1
    record = offered[0]
    assert (record.requires, record.record_kinds, record.default_size) == (
        ["record"],
        None,
        "compact",
    )
    assert sorted(record.parameter_schema) == ["kind"]
    assert record.parameter_schema["kind"].max_length == 64


def test_default_visual_requiring_a_record_is_rejected() -> None:
    """A new canvas cannot open on a tile that has no record to render."""
    with pytest.raises(ValueError, match="record"):
        Workspace.Config(default_visual="trax.subgraph").make()


def test_context_graph_bounds_its_reach_and_its_highlight() -> None:
    """The window lights 1 to 3 hops, 2 unless asked, and marks ids it lists."""
    described = ContextGraphVisual.Config().make().describe()
    assert (described.type, described.requires, described.default_size) == (
        "trax.subgraph",
        ["record"],
        "wide",
    )
    hops = described.parameter_schema["hops"]
    assert (hops.type, hops.default, hops.minimum, hops.maximum) == (
        "integer",
        2,
        1,
        3,
    )
    highlight = described.parameter_schema["highlight"]
    assert (highlight.type, highlight.default, highlight.max_length) == (
        "string",
        "",
        512,
    )


def test_terminal_needs_no_record_and_bounds_its_machine_and_has_no_terminal_id() -> (
    None
):
    """The tile starts nothing itself, caps its machine and keeps no terminal id."""
    described = TerminalVisual.Config().make().describe()
    assert (described.type, described.requires, described.default_size) == (
        "trax.terminal",
        [],
        "wide",
    )
    machine = described.parameter_schema["machine"]
    assert (machine.type, machine.default, machine.max_length) == ("string", "", 63)
    assert set(described.parameter_schema) == {"machine", "start"}
    start = described.parameter_schema["start"]
    assert (start.type, start.default) == ("boolean", False)


def test_files_takes_any_record_and_bounds_its_narrowing() -> None:
    """The files view draws any record; its path and file are capped at 512."""
    described = FilesVisual.Config().make().describe()
    assert described.model_dump() == {
        "type": "trax.files",
        "version": 1,
        "title": "Files",
        "description": (
            "A record's files and what changed in them, each change with its diff."
        ),
        "requires": ["record"],
        "default_size": "wide",
        "parameter_schema": {
            "path": {
                "type": "string",
                "default": "",
                "minimum": None,
                "maximum": None,
                "max_length": 512,
            },
            "changed": {
                "type": "boolean",
                "default": False,
                "minimum": None,
                "maximum": None,
                "max_length": None,
            },
            "file": {
                "type": "string",
                "default": "",
                "minimum": None,
                "maximum": None,
                "max_length": 512,
            },
        },
        "record_kinds": None,
    }
    assert default_workspace().visual("trax.files") == described


def test_context_graph_default_reach_past_three_hops_is_a_config_error() -> None:
    """The neighbourhood read walks at most 3 hops, so no default may ask more."""
    config = ContextGraphVisual.Config()
    config.default_hops = 4
    with pytest.raises(ValueError, match="outside its bounds"):
        config.make().describe()


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
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
