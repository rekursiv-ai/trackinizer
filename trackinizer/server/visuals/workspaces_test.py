"""Visual operations preserve context and reject malformed module input."""

from __future__ import annotations

import uuid

from pydantic import ValidationError

import pytest

from trackinizer.server.visuals.catalog import (
    ParameterDescription,
    VisualCatalogBody,
    VisualDescription,
    default_catalog,
    default_workspace,
)
from trackinizer.server.visuals.workspaces import (
    ApplyWorkspaceOperation,
    HideVisual,
    ShowVisual,
    WorkspaceData,
    WorkspaceMessageRequest,
    apply_operation,
    initial_data,
    refuse_record,
)


def test_a_new_canvas_has_chat_floating_over_its_default_visual() -> None:
    """Chat is there with no setup: the default visual in main, Chat floating."""
    data = initial_data(default_catalog())
    assert [(v.type, v.placement) for v in data.visuals] == [
        ("trax.browse", "main"),
        ("trax.chat", "floating"),
    ]
    assert data.focused_instance is None


def test_show_reuses_visual_type_and_updates_its_context() -> None:
    """Reopening Chat updates its target without orphaning a duplicate tile."""
    catalog = default_catalog()
    original = apply_operation(
        initial_data(catalog),
        ShowVisual(kind="show", visual_type="trax.chat"),
        catalog,
    )
    chat_id = original.visuals[1].id
    record_id = uuid.uuid4()
    reopened = apply_operation(
        original,
        ShowVisual(kind="show", visual_type="trax.chat", record_id=record_id),
        catalog,
    )
    assert len(reopened.visuals) == 2
    assert reopened.visuals[1].id == chat_id
    assert reopened.visuals[1].record_id == record_id
    assert reopened.focused_instance == chat_id


def test_recentring_the_context_graph_keeps_its_reach_and_highlight() -> None:
    """A show naming only a new record moves the window; its params stay."""
    catalog = default_catalog()
    first, second = uuid.uuid4(), uuid.uuid4()
    shown = apply_operation(
        initial_data(catalog),
        ShowVisual(
            kind="show",
            visual_type="trax.subgraph",
            record_id=first,
            params={"hops": 3, "highlight": str(second)},
        ),
        catalog,
    )
    moved = apply_operation(
        shown,
        ShowVisual(kind="show", visual_type="trax.subgraph", record_id=second),
        catalog,
    )
    [graph] = [visual for visual in moved.visuals if visual.type == "trax.subgraph"]
    assert (graph.record_id, graph.params) == (
        second,
        {"hops": 3, "highlight": str(second)},
    )


def test_show_uses_bounded_catalog_defaults_and_requires_record() -> None:
    """A contributed module can be enabled with its safe default parameters."""
    catalog = VisualCatalogBody(
        default_visual="trax.subgraph",
        visuals=[
            VisualDescription(
                type="trax.subgraph",
                version=1,
                title="Subgraph",
                description="Bounded lineage.",
                requires=["record"],
                default_size="wide",
                parameter_schema={
                    "depth": ParameterDescription(
                        type="integer",
                        default=2,
                        minimum=1,
                        maximum=3,
                    ),
                },
            ),
        ],
    )
    with pytest.raises(ValueError, match="record"):
        apply_operation(
            WorkspaceData(visuals=[]),
            ShowVisual(kind="show", visual_type="trax.subgraph"),
            catalog,
        )
    record_id = uuid.uuid4()
    shown = apply_operation(
        WorkspaceData(visuals=[]),
        ShowVisual(kind="show", visual_type="trax.subgraph", record_id=record_id),
        catalog,
    )
    assert shown.visuals[0].params == {"depth": 2}
    assert shown.visuals[0].record_id == record_id


def test_show_rejects_unbounded_string_parameter() -> None:
    """A large agent parameter cannot be copied into every receipt."""
    catalog = VisualCatalogBody(
        default_visual="trax.search",
        visuals=[
            VisualDescription(
                type="trax.search",
                version=1,
                title="Search",
                description="Search records.",
                requires=[],
                default_size="wide",
                parameter_schema={
                    "query": ParameterDescription(
                        type="string",
                        default="",
                        max_length=8,
                    ),
                },
            ),
        ],
    )
    with pytest.raises(ValueError, match="maximum length"):
        apply_operation(
            WorkspaceData(visuals=[]),
            ShowVisual(
                kind="show",
                visual_type="trax.search",
                params={"query": "ninechars"},
            ),
            catalog,
        )


def test_operation_rejects_misspelled_record_context() -> None:
    """Unknown fields fail instead of silently discarding agent context."""
    with pytest.raises(ValidationError, match="recordId"):
        ApplyWorkspaceOperation.model_validate(
            {
                "revision": 0,
                "operation": {
                    "kind": "show",
                    "visual_type": "trax.chat",
                    "recordId": str(uuid.uuid4()),
                },
            },
        )


def test_chat_artifact_uses_the_same_record_target() -> None:
    """Chat uses the Artifact ID as its sole target and rejects a second shape."""
    artifact_id = uuid.uuid4()
    shown = apply_operation(
        initial_data(default_catalog()),
        ShowVisual(kind="show", visual_type="trax.chat", record_id=artifact_id),
        default_catalog(),
    )
    assert shown.visuals[-1].record_id == artifact_id
    with pytest.raises(ValidationError):
        ShowVisual.model_validate(
            {
                "kind": "show",
                "visual_type": "trax.chat",
                "record_id": str(artifact_id),
                "report_target": {"artifact_id": str(artifact_id)},
            },
        )
    with pytest.raises(ValidationError):
        WorkspaceMessageRequest.model_validate(
            {
                "text": "hello",
                "expected_report_target": {"artifact_id": str(artifact_id)},
            },
        )


def test_artifact_visual_requires_record_target() -> None:
    """An Artifact tile uses the same record target as other visuals."""
    catalog = default_catalog()
    with pytest.raises(ValueError, match="record target"):
        apply_operation(
            initial_data(catalog),
            ShowVisual(kind="show", visual_type="trax.artifact"),
            catalog,
        )
    artifact_id = uuid.uuid4()
    shown = apply_operation(
        initial_data(catalog),
        ShowVisual(kind="show", visual_type="trax.artifact", record_id=artifact_id),
        catalog,
    )
    assert shown.visuals[-1].record_id == artifact_id


@pytest.mark.parametrize(
    "route",
    ["#/list/Issue", "#/", "#/search/some-text", "#/ref/Issue/12"],
)
def test_navigate_takes_a_hash_route(route: str) -> None:
    """A navigate names a `#/...` hash, and applying it changes nothing."""
    catalog = default_catalog()
    data = initial_data(catalog)
    operation = ApplyWorkspaceOperation.model_validate(
        {"revision": 0, "operation": {"kind": "navigate", "route": route}},
    )
    assert apply_operation(data, operation.operation, catalog) == data


@pytest.mark.parametrize(
    "route",
    ["", "list/Issue", "#list", "#/a b", "#/a\tb", "#/a\nb", "#/" + "x" * 511],
)
def test_navigate_refuses_a_route_that_is_not_a_clean_hash(route: str) -> None:
    """No space or control character, `#/` first, at most 512 characters."""
    with pytest.raises(ValidationError):
        ApplyWorkspaceOperation.model_validate(
            {"revision": 0, "operation": {"kind": "navigate", "route": route}},
        )


def test_navigate_route_may_be_512_characters_and_no_more() -> None:
    """The bound is exactly 512."""
    ok = "#/" + "x" * 510
    ApplyWorkspaceOperation.model_validate(
        {"revision": 0, "operation": {"kind": "navigate", "route": ok}},
    )
    with pytest.raises(ValidationError):
        ApplyWorkspaceOperation.model_validate(
            {"revision": 0, "operation": {"kind": "navigate", "route": ok + "x"}},
        )


def test_highlight_takes_up_to_fifty_uuids_and_changes_nothing() -> None:
    """An empty list clears, 50 ids is the bound, and applying it is a no-op."""
    catalog = default_catalog()
    data = initial_data(catalog)
    for count in (0, 1, 50):
        operation = ApplyWorkspaceOperation.model_validate(
            {
                "revision": 0,
                "operation": {
                    "kind": "highlight",
                    "ids": [str(uuid.uuid4()) for _ in range(count)],
                },
            },
        )
        assert apply_operation(data, operation.operation, catalog) == data


@pytest.mark.parametrize(
    "ids",
    [[str(uuid.uuid4()) for _ in range(51)], ["Issue#12"], ["nope"], [1], "x"],
)
def test_highlight_refuses_too_many_ids_and_non_uuids(ids: object) -> None:
    """The bound is 50 and an id is a UUID."""
    with pytest.raises(ValidationError):
        ApplyWorkspaceOperation.model_validate(
            {"revision": 0, "operation": {"kind": "highlight", "ids": ids}},
        )


def test_the_default_visual_is_the_page_and_cannot_be_hidden() -> None:
    """Chat can be hidden; browse, the page itself, cannot."""
    catalog = default_catalog()
    data = initial_data(catalog)
    browse, chat = data.visuals
    with pytest.raises(ValueError, match="cannot be hidden"):
        apply_operation(
            data,
            HideVisual(kind="hide", instance_id=browse.id),
            catalog,
        )
    hidden = apply_operation(
        data,
        HideVisual(kind="hide", instance_id=chat.id),
        catalog,
    )
    assert [visual.type for visual in hidden.visuals] == ["trax.browse"]


def test_a_visual_that_takes_no_record_refuses_one() -> None:
    """Browse is the page, so a record target is a mistake, not a hint."""
    catalog = default_catalog()
    with pytest.raises(ValueError, match="takes no record"):
        apply_operation(
            initial_data(catalog),
            ShowVisual(kind="show", visual_type="trax.browse", record_id=uuid.uuid4()),
            catalog,
        )


@pytest.mark.parametrize("text", ["", " ", "\n\t "])
def test_a_message_must_hold_a_non_space_character(text: str) -> None:
    """Whitespace alone is nothing to send."""
    with pytest.raises(ValidationError):
        WorkspaceMessageRequest(text=text)


def test_a_message_is_at_most_16384_characters() -> None:
    """The bound is exactly 16,384."""
    assert WorkspaceMessageRequest(text="x" * 16_384).text
    with pytest.raises(ValidationError):
        WorkspaceMessageRequest(text="x" * 16_385)


def _show(visual: str, *, record: uuid.UUID | None) -> ShowVisual:
    return ShowVisual(kind="show", visual_type=visual, record_id=record)


def _entry(visual: str) -> VisualDescription:
    entry = default_workspace().visual(visual)
    assert entry is not None
    return entry


@pytest.mark.parametrize(
    ("visual", "kind", "refused"),
    [
        ("trax.timeline", "Issue", False),
        ("trax.timeline", "Experiment", False),
        ("trax.timeline", "Belief", True),
        ("trax.timeline", None, True),
        ("trax.artifact", "Artifact", False),
        ("trax.artifact", "Issue", True),
        ("trax.subgraph", "Paper", False),
        ("trax.subgraph", None, False),
        ("trax.chat", "Belief", False),
        ("trax.browse", "Issue", True),
        ("trax.browse", None, True),
    ],
)
def test_refuse_record_applies_the_catalogs_kinds(
    visual: str,
    kind: str | None,
    refused: bool,
) -> None:
    """One rule, callable without a database: a reason, or None."""
    reason = refuse_record(
        _show(visual, record=uuid.uuid4()),
        descriptor=_entry(visual),
        kind=kind,
    )
    assert (reason is not None) is refused


def test_refuse_record_names_the_kinds_and_what_it_found() -> None:
    """The sentence a refused caller reads says what the visual shows."""
    reason = refuse_record(
        _show("trax.timeline", record=uuid.uuid4()),
        descriptor=_entry("trax.timeline"),
        kind="Belief",
    )
    assert reason == "Evidence timeline shows Issue or Experiment records, not Belief."
    unknown = refuse_record(
        _show("trax.timeline", record=uuid.uuid4()),
        descriptor=_entry("trax.timeline"),
        kind=None,
    )
    assert unknown is not None
    assert unknown.endswith("not an unknown record.")
    assert (
        refuse_record(
            _show("trax.browse", record=uuid.uuid4()),
            descriptor=_entry("trax.browse"),
            kind=None,
        )
        == "Visual takes no record target."
    )


def test_a_show_without_a_record_is_never_refused_for_one() -> None:
    """Whether a record is required is another rule."""
    assert (
        refuse_record(
            _show("trax.browse", record=None),
            descriptor=_entry("trax.browse"),
            kind=None,
        )
        is None
    )
    assert (
        refuse_record(
            _show("trax.timeline", record=None),
            descriptor=_entry("trax.timeline"),
            kind=None,
        )
        is None
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
