"""Visual operations preserve context and reject malformed module input."""

from __future__ import annotations

from datetime import UTC, datetime

import json
import re
import uuid

from pydantic import ValidationError

import pytest

from trackinizer.server.visuals.canvases import (
    MAX_MARKS,
    MAX_MARKS_BYTES,
    AddMark,
    ApplyCanvasOperation,
    CampaignSetup,
    CanvasData,
    CanvasLayout,
    CanvasMetadata,
    CanvasSummary,
    ClearMarks,
    HideVisual,
    Mark,
    NameChat,
    NewMark,
    Placement,
    PlaceVisual,
    RemoveMark,
    ShowVisual,
    TabVisual,
    WorkspaceSuggestion,
    apply_operation,
    initial_data,
    refuse_params,
    refuse_record,
    restore_page,
)
from trackinizer.server.visuals.catalog import (
    RECORD_VISUAL,
    Canvas,
    ParameterDescription,
    VisualCatalogBody,
    VisualDescription,
    default_canvas,
    default_catalog,
    offer_record_visual,
)


def _with_graph(catalog: VisualCatalogBody) -> CanvasData:
    """Return a new canvas with a context graph shown on it."""
    return apply_operation(
        initial_data(catalog),
        ShowVisual(kind="show", visual_type="trax.subgraph", record_id=uuid.uuid4()),
        catalog,
    )


def test_a_new_canvas_holds_only_its_default_visual() -> None:
    """Chat is a column of its own, so the canvas starts with the page alone."""
    data = initial_data(default_catalog())
    assert [(v.type, v.placement) for v in data.visuals] == [("trax.browse", "main")]
    assert data.focused_instance is None


def test_a_new_canvas_keeps_its_selected_provider() -> None:
    data = initial_data(default_catalog(), provider="codex")
    assert data.provider == "codex"


def test_chat_is_no_visual_so_showing_it_is_refused_like_any_unknown_type() -> None:
    """The catalog does not offer it, and a show of it is an unknown type."""
    catalog = default_catalog()
    assert "trax.chat" not in {visual.type for visual in catalog.visuals}
    with pytest.raises(ValueError, match=r"Unknown visual type 'trax\.chat'"):
        apply_operation(
            initial_data(catalog),
            ShowVisual(kind="show", visual_type="trax.chat"),
            catalog,
        )


def test_a_stored_canvas_that_holds_chat_reads_without_it() -> None:
    """A canvas saved when Chat was a visual drops it on read, and its focus with it."""
    page, chat, graph = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    def stored(focused: uuid.UUID) -> CanvasData:
        return CanvasData.model_validate(
            {
                "visuals": [
                    {"id": str(page), "type": "trax.browse", "version": 1},
                    {
                        "id": str(chat),
                        "type": "trax.chat",
                        "version": 1,
                        "placement": "side",
                        "record_id": str(uuid.uuid4()),
                    },
                    {
                        "id": str(graph),
                        "type": "trax.subgraph",
                        "version": 1,
                        "record_id": str(uuid.uuid4()),
                    },
                ],
                "focused_instance": str(focused),
            },
        )

    on_chat = stored(chat)
    assert [v.type for v in on_chat.visuals] == ["trax.browse", "trax.subgraph"]
    assert on_chat.focused_instance is None
    assert stored(graph).focused_instance == graph


def test_a_visual_is_placed_at_either_side_of_the_page_and_nowhere_unnamed() -> None:
    """A visual moves to the column left of the page and back; another name is refused."""
    catalog = default_catalog()
    data = _with_graph(catalog)
    graph = data.visuals[1].id
    sides: tuple[Placement, ...] = ("left", "side")
    for placement in sides:
        data = apply_operation(
            data,
            PlaceVisual(kind="place", instance_id=graph, placement=placement),
            catalog,
        )
        assert [(v.type, v.placement) for v in data.visuals] == [
            ("trax.browse", "main"),
            ("trax.subgraph", placement),
        ]
    with pytest.raises(ValidationError):
        PlaceVisual.model_validate(
            {"kind": "place", "instance_id": str(graph), "placement": "right"},
        )


def test_timeline_is_off_in_a_new_canvas_and_recentres_when_shown_again() -> None:
    """The merged view opens only on request; a second show moves it, not duplicates."""
    catalog = default_catalog()
    fresh = initial_data(catalog)
    assert [visual.type for visual in fresh.visuals] == ["trax.browse"]
    first, second = uuid.uuid4(), uuid.uuid4()
    opened = apply_operation(
        fresh,
        ShowVisual(kind="show", visual_type="trax.timeline", record_id=first),
        catalog,
    )
    moved = apply_operation(
        opened,
        ShowVisual(kind="show", visual_type="trax.timeline", record_id=second),
        catalog,
    )
    timelines = [visual for visual in moved.visuals if visual.type == "trax.timeline"]
    assert [visual.record_id for visual in timelines] == [second]
    assert timelines[0].id == opened.visuals[-1].id


def test_show_reuses_visual_type_and_updates_its_context() -> None:
    """Reopening a visual updates its target without orphaning a duplicate tile."""
    catalog = default_catalog()
    original = _with_graph(catalog)
    graph_id = original.visuals[1].id
    record_id = uuid.uuid4()
    reopened = apply_operation(
        original,
        ShowVisual(kind="show", visual_type="trax.subgraph", record_id=record_id),
        catalog,
    )
    assert len(reopened.visuals) == 2
    assert reopened.visuals[1].id == graph_id
    assert reopened.visuals[1].record_id == record_id
    assert reopened.focused_instance == graph_id


def test_a_canvas_holds_one_record_tile_per_record_and_reshowing_one_focuses_it() -> (
    None
):
    """Record tiles stand side by side; a show of a record on show focuses its tile."""
    visuals = Canvas.Config()
    offer_record_visual(visuals)
    catalog = visuals.make().catalog()
    first, second = uuid.uuid4(), uuid.uuid4()
    data = initial_data(catalog)
    for record in (first, second, first):
        data = apply_operation(
            data,
            ShowVisual(
                kind="show",
                visual_type=RECORD_VISUAL,
                placement="left",
                record_id=record,
                params={"kind": "Issue"},
            ),
            catalog,
        )
    tiles = [visual for visual in data.visuals if visual.type == RECORD_VISUAL]
    assert [tile.record_id for tile in tiles] == [first, second]
    assert data.focused_instance == tiles[0].id


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


_NARROWED: dict[str, str | int | bool] = {
    "path": "loop/foo/",
    "changed": True,
    "file": "bar.py",
}


def _files(data: CanvasData) -> tuple[uuid.UUID | None, dict[str, str | int | bool]]:
    [files] = [visual for visual in data.visuals if visual.type == "trax.files"]
    return files.record_id, files.params


def test_a_visual_records_who_placed_it_only_when_told_and_only_when_it_is_new() -> (
    None
):
    """An agent's show names its key's user on the new instance; a re-show keeps it."""
    catalog = default_catalog()
    show = ShowVisual(
        kind="show",
        visual_type="trax.terminal",
        params={"machine": "gx10", "start": True},
    )

    by_agent = apply_operation(
        initial_data(catalog),
        show,
        catalog,
        placed_by="kb@x.y",
    )
    again = apply_operation(by_agent, show, catalog, placed_by="other@x.y")
    by_person = apply_operation(initial_data(catalog), show, catalog)

    def placed(data: CanvasData) -> list[str | None]:
        return [v.placed_by for v in data.visuals if v.type == "trax.terminal"]

    assert placed(by_agent) == ["kb@x.y"]
    assert placed(again) == ["kb@x.y"]
    assert placed(by_person) == [None]
    [terminal] = [v for v in by_agent.visuals if v.type == "trax.terminal"]
    assert terminal.params == {"machine": "gx10", "start": True}


def test_showing_files_on_a_new_record_keeps_its_narrowing() -> None:
    """A show naming only a new record moves the files view; its params stay."""
    catalog = default_catalog()
    first, second = uuid.uuid4(), uuid.uuid4()
    shown = apply_operation(
        initial_data(catalog),
        ShowVisual(
            kind="show",
            visual_type="trax.files",
            record_id=first,
            params=_NARROWED,
        ),
        catalog,
    )
    moved = apply_operation(
        shown,
        ShowVisual(kind="show", visual_type="trax.files", record_id=second),
        catalog,
    )
    assert _files(shown) == (first, _NARROWED)
    assert _files(moved) == (second, _NARROWED)


def test_any_param_sets_the_files_narrowing_anew() -> None:
    """Each param a show leaves out goes back to its default."""
    catalog = default_catalog()
    record = uuid.uuid4()
    shown = apply_operation(
        initial_data(catalog),
        ShowVisual(
            kind="show",
            visual_type="trax.files",
            record_id=record,
            params=_NARROWED,
        ),
        catalog,
    )
    again = apply_operation(
        shown,
        ShowVisual(
            kind="show",
            visual_type="trax.files",
            record_id=record,
            params={"file": "baz.py"},
        ),
        catalog,
    )
    assert _files(again) == (record, {"path": "", "changed": False, "file": "baz.py"})


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
            CanvasData(visuals=[]),
            ShowVisual(kind="show", visual_type="trax.subgraph"),
            catalog,
        )
    record_id = uuid.uuid4()
    shown = apply_operation(
        CanvasData(visuals=[]),
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
            CanvasData(visuals=[]),
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
        ApplyCanvasOperation.model_validate(
            {
                "revision": 0,
                "operation": {
                    "kind": "show",
                    "visual_type": "trax.subgraph",
                    "recordId": str(uuid.uuid4()),
                },
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
    operation = ApplyCanvasOperation.model_validate(
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
        ApplyCanvasOperation.model_validate(
            {"revision": 0, "operation": {"kind": "navigate", "route": route}},
        )


def test_navigate_route_may_be_512_characters_and_no_more() -> None:
    """The bound is exactly 512."""
    ok = "#/" + "x" * 510
    ApplyCanvasOperation.model_validate(
        {"revision": 0, "operation": {"kind": "navigate", "route": ok}},
    )
    with pytest.raises(ValidationError):
        ApplyCanvasOperation.model_validate(
            {"revision": 0, "operation": {"kind": "navigate", "route": ok + "x"}},
        )


@pytest.mark.parametrize("hidden", [True, False])
def test_chat_hides_or_shows_the_window_and_changes_nothing(hidden: bool) -> None:
    """A chat operation names the flag, and applying it is a no-op."""
    catalog = default_catalog()
    data = initial_data(catalog)
    operation = ApplyCanvasOperation.model_validate(
        {"revision": 0, "operation": {"kind": "chat", "hidden": hidden}},
    )
    assert apply_operation(data, operation.operation, catalog) == data


@pytest.mark.parametrize(
    "operation",
    [
        {"kind": "chat"},
        {"kind": "chat", "hidden": "maybe"},
        {"kind": "chat", "hidden": True, "x": 1},
    ],
)
def test_chat_needs_a_boolean_flag_and_nothing_else(
    operation: dict[str, object],
) -> None:
    """A missing or non-boolean flag, or an extra field, is refused."""
    with pytest.raises(ValidationError):
        ApplyCanvasOperation.model_validate({"revision": 0, "operation": operation})


def test_highlight_takes_up_to_fifty_uuids_and_changes_nothing() -> None:
    """An empty list clears, 50 ids is the bound, and applying it is a no-op."""
    catalog = default_catalog()
    data = initial_data(catalog)
    for count in (0, 1, 50):
        operation = ApplyCanvasOperation.model_validate(
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
        ApplyCanvasOperation.model_validate(
            {"revision": 0, "operation": {"kind": "highlight", "ids": ids}},
        )


def test_the_page_can_be_hidden_like_any_other_visual() -> None:
    """Browse is the visual a canvas opens with, not one it must keep."""
    catalog = default_catalog()
    data = _with_graph(catalog)
    browse, graph = data.visuals
    hidden = apply_operation(
        data,
        HideVisual(kind="hide", instance_id=browse.id),
        catalog,
    )
    assert [visual.type for visual in hidden.visuals] == ["trax.subgraph"]
    hidden = apply_operation(
        data,
        HideVisual(kind="hide", instance_id=graph.id),
        catalog,
    )
    assert [visual.type for visual in hidden.visuals] == ["trax.browse"]


def test_restoring_the_page_shows_it_in_main_and_focuses_it() -> None:
    """A canvas that holds no page gets it back; one that holds it is unchanged."""
    catalog = default_catalog()
    data = _with_graph(catalog)
    browse, graph = data.visuals
    assert restore_page(data, catalog=catalog) == data
    without = apply_operation(
        data,
        HideVisual(kind="hide", instance_id=browse.id),
        catalog,
    )
    restored = restore_page(without, catalog=catalog)
    assert [(v.type, v.placement) for v in restored.visuals] == [
        ("trax.subgraph", "main"),
        ("trax.browse", "main"),
    ]
    assert restored.focused_instance == restored.visuals[-1].id
    assert restored.visuals[0].id == graph.id


def test_a_visual_that_takes_no_record_refuses_one() -> None:
    """Browse is the page, so a record target is a mistake, not a hint."""
    catalog = default_catalog()
    with pytest.raises(ValueError, match="takes no record"):
        apply_operation(
            initial_data(catalog),
            ShowVisual(kind="show", visual_type="trax.browse", record_id=uuid.uuid4()),
            catalog,
        )


def _entry(visual: str) -> VisualDescription:
    entry = default_canvas().visual(visual)
    assert entry is not None
    return entry


@pytest.mark.parametrize(
    ("visual", "kind", "refused"),
    [
        ("trax.timeline", "Issue", False),
        ("trax.timeline", "Belief", False),
        ("trax.timeline", None, False),
        ("trax.artifact", "Artifact", False),
        ("trax.artifact", "Issue", True),
        ("trax.subgraph", "Paper", False),
        ("trax.subgraph", None, False),
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
        ShowVisual(kind="show", visual_type=visual, record_id=uuid.uuid4()),
        descriptor=_entry(visual),
        kind=kind,
    )
    assert (reason is not None) is refused


def test_refuse_record_names_the_kinds_and_what_it_found() -> None:
    """The sentence a refused caller reads says what the visual shows."""
    reason = refuse_record(
        ShowVisual(kind="show", visual_type="trax.artifact", record_id=uuid.uuid4()),
        descriptor=_entry("trax.artifact"),
        kind="Belief",
    )
    assert reason == "Artifact shows Artifact records, not Belief."
    two = _entry("trax.artifact").model_copy(
        update={"record_kinds": ["Paper", "Issue"]},
    )
    assert (
        refuse_record(
            ShowVisual(
                kind="show",
                visual_type="trax.artifact",
                record_id=uuid.uuid4(),
            ),
            descriptor=two,
            kind="Belief",
        )
        == "Artifact shows Paper or Issue records, not Belief."
    )
    unknown = refuse_record(
        ShowVisual(kind="show", visual_type="trax.artifact", record_id=uuid.uuid4()),
        descriptor=_entry("trax.artifact"),
        kind=None,
    )
    assert unknown is not None
    assert unknown.endswith("not an unknown record.")
    assert (
        refuse_record(
            ShowVisual(kind="show", visual_type="trax.browse", record_id=uuid.uuid4()),
            descriptor=_entry("trax.browse"),
            kind=None,
        )
        == "Visual takes no record target."
    )


@pytest.mark.parametrize(
    ("visual", "params", "reason"),
    [
        (
            "trax.files",
            {"path": "", "depth": 1},
            "Visual parameters do not match the catalog schema.",
        ),
        ("trax.files", {"path": 1}, "Parameter 'path' must be a string."),
        ("trax.files", {"file": True}, "Parameter 'file' must be a string."),
        (
            "trax.files",
            {"path": "x" * 513},
            "Parameter 'path' exceeds maximum length of 512.",
        ),
        (
            "trax.files",
            {"file": "x" * 513},
            "Parameter 'file' exceeds maximum length of 512.",
        ),
        ("trax.files", {"changed": "true"}, "Parameter 'changed' must be a boolean."),
        ("trax.files", {"changed": 1}, "Parameter 'changed' must be a boolean."),
        ("trax.subgraph", {"hops": "2"}, "Parameter 'hops' must be an integer."),
        ("trax.subgraph", {"hops": True}, "Parameter 'hops' must be an integer."),
        ("trax.subgraph", {"hops": 0}, "Parameter 'hops' is below its minimum of 1."),
        ("trax.subgraph", {"hops": 4}, "Parameter 'hops' is above its maximum of 3."),
    ],
)
def test_refuse_params_names_the_rule_a_show_breaks(
    visual: str,
    params: dict[str, str | int | bool],
    reason: str,
) -> None:
    """The sentence names the param and the bound; the server raises the same."""
    assert refuse_params(params, descriptor=_entry(visual)) == reason
    catalog = default_catalog()
    with pytest.raises(ValueError, match=re.escape(reason)):
        apply_operation(
            initial_data(catalog),
            ShowVisual(
                kind="show",
                visual_type=visual,
                record_id=uuid.uuid4(),
                params=params,
            ),
            catalog,
        )


@pytest.mark.parametrize(
    ("visual", "params"),
    [
        ("trax.files", {}),
        ("trax.files", {"path": "x" * 512, "changed": True, "file": "x" * 512}),
        ("trax.files", {"path": "**/*.py", "changed": False, "file": ""}),
        ("trax.subgraph", {"hops": 1, "highlight": ""}),
        ("trax.subgraph", {"hops": 3}),
    ],
)
def test_refuse_params_accepts_params_within_bounds(
    visual: str,
    params: dict[str, str | int | bool],
) -> None:
    """A param at its bound is taken; one left out takes its default."""
    assert refuse_params(params, descriptor=_entry(visual)) is None


def test_a_show_without_a_record_is_never_refused_for_one() -> None:
    """Whether a record is required is another rule."""
    assert (
        refuse_record(
            ShowVisual(kind="show", visual_type="trax.browse", record_id=None),
            descriptor=_entry("trax.browse"),
            kind=None,
        )
        is None
    )
    assert (
        refuse_record(
            ShowVisual(kind="show", visual_type="trax.timeline", record_id=None),
            descriptor=_entry("trax.timeline"),
            kind=None,
        )
        is None
    )


def test_a_campaign_setup_validates_a_partial_subset_with_a_nested_lead() -> None:
    setup = CampaignSetup.model_validate(
        {"hours": 2, "scientists": 3, "lead": {"model": "gpt-6.1-sol"}},
    )

    assert setup.hours == 2.0
    assert setup.scientists == 3
    assert setup.lead is not None
    assert setup.lead.model == "gpt-6.1-sol"
    assert (setup.budget_usd, setup.staffing, setup.repo) == (None, None, None)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"hours": 0}, "greater than 0"),
        ({"hours": float("inf")}, "finite"),
        ({"budget_usd": -5}, "greater than 0"),
        ({"scientists": 9}, "less than or equal to 8"),
        ({"scientists": 0}, "greater than or equal to 1"),
        ({"staffing": "gpt-6.1-sol:high"}, "MODEL:EFFORT:COUNT"),
        ({"staffing": ""}, "at least 1"),
        ({"repo": "loop"}, "owner/name"),
        ({"extra": 1}, "Extra inputs"),
    ],
)
def test_a_campaign_setup_refuses_a_misshapen_field_with_a_plain_line(
    payload: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError) as caught:
        CampaignSetup.model_validate(payload)

    assert message in str(caught.value)


def test_a_canvas_tab_keeps_its_page_visuals_and_sizes() -> None:
    """A tab not open holds what it shows, so opening it again can show it."""
    record_id = uuid.uuid4()
    layout = CanvasLayout.model_validate(
        {
            "tab": "b",
            "tabs": [
                {"id": "a", "name": "Tab 1"},
                {
                    "id": "b",
                    "name": "Review",
                    "page": "#/ref/Issue/7",
                    "visuals": [
                        {"type": "trax.browse"},
                        {
                            "type": "trax.subgraph",
                            "placement": "side",
                            "record_id": str(record_id),
                            "params": {"hops": 2},
                        },
                    ],
                    "columns": {"side": 0.3},
                    "share": {"trax.browse": 1.5},
                },
            ],
        },
    )
    assert layout.tabs is not None
    first, second = layout.tabs
    assert (first.page, first.visuals) == (None, [])
    assert second.visuals[1] == TabVisual(
        type="trax.subgraph",
        placement="side",
        record_id=record_id,
        params={"hops": 2},
    )
    assert layout.model_dump(mode="json", exclude_none=True)["tab"] == "b"


@pytest.mark.parametrize(
    "tabs",
    [
        [{"id": "a", "name": "One"}, {"id": "a", "name": "Two"}],
        [{"id": "a", "name": ""}],
        [{"id": "a", "name": "x" * 61}],
        [{"id": "a", "name": "One", "page": "#/has space"}],
        [{"id": "a", "name": "One", "visuals": [{"type": "trax.browse"}] * 13}],
        [{"id": "a", "name": "One", "visuals": [{"type": "x", "where": "main"}]}],
        [{"id": "a", "name": "One", "columns": {"side": 420}}],
        [{"id": str(n), "name": "Tab"} for n in range(13)],
    ],
)
def test_canvas_tabs_out_of_bounds_are_refused(tabs: list[dict[str, object]]) -> None:
    """Tabs keep distinct ids, short names, clean pages, and bounded visuals."""
    with pytest.raises(ValidationError):
        CanvasLayout.model_validate({"tabs": tabs})


def test_a_summary_names_each_tab_and_the_open_one_and_a_canvas_without_none() -> None:
    """The canvas list carries flags, row time, and tab names, never tab contents."""
    made = datetime(2026, 10, 15, tzinfo=UTC)
    modified = datetime(2026, 10, 16, 9, 30, tzinfo=UTC)
    canvas = uuid.uuid4()
    chat = uuid.uuid4()
    workspace_id = uuid.uuid4()
    suggestion = WorkspaceSuggestion(
        workspace_id=uuid.uuid4(),
        title="Possible workspace",
    )
    layout = CanvasLayout.model_validate(
        {
            "tabs": [
                {"id": "a", "name": "Graph", "page": "#/graph"},
                {"id": "b", "name": "Issue#7"},
            ],
            "tab": "b",
        },
    )
    tabbed = CanvasSummary.of(
        CanvasMetadata(
            name="beta",
            conversation_id=chat,
            page="#/graph",
            layout=layout,
            hidden=True,
            pinned=True,
            workspace_id=workspace_id,
            suggestion=suggestion,
        ),
        canvas_id=canvas,
        created_at=made,
        modified_at=modified,
    )
    plain = CanvasSummary.of(
        CanvasMetadata(name="alpha"),
        canvas_id=canvas,
        created_at=made,
        modified_at=modified,
    )
    assert tabbed.model_dump(mode="json") == {
        "id": str(canvas),
        "name": "beta",
        "conversation_id": str(chat),
        "page": "#/graph",
        "created_at": "2026-10-15T00:00:00Z",
        "modified_at": "2026-10-16T09:30:00Z",
        "hidden": True,
        "pinned": True,
        "workspace_id": str(workspace_id),
        "suggestion": {
            "workspace_id": str(suggestion.workspace_id),
            "title": "Possible workspace",
        },
        "tabs": [{"id": "a", "name": "Graph"}, {"id": "b", "name": "Issue#7"}],
        "tab": "b",
        "state": "idle",
    }
    waiting = CanvasSummary.of(
        CanvasMetadata(name="alpha"),
        canvas_id=canvas,
        created_at=made,
        modified_at=modified,
        state="requires_action",
    )
    assert (plain.state, waiting.state) == ("idle", "requires_action")
    assert (plain.tabs, plain.tab, plain.name, plain.page) == ([], None, "alpha", None)
    assert (plain.hidden, plain.pinned, plain.modified_at) == (False, False, modified)


def test_a_workspace_suggestion_has_a_bounded_title_and_optional_target() -> None:
    suggestion = WorkspaceSuggestion(title="A possible stream")
    assert suggestion.workspace_id is None
    assert (
        WorkspaceSuggestion.model_validate(
            {"workspace_id": str(uuid.uuid4()), "title": "A"},
        ).title
        == "A"
    )
    with pytest.raises(ValidationError):
        WorkspaceSuggestion(title=" ")
    with pytest.raises(ValidationError):
        WorkspaceSuggestion(title="x" * 121)


def test_a_canvas_in_a_workspace_reads_without_a_suggestion_of_that_workspace() -> None:
    workspace_id = uuid.uuid4()
    canvas = CanvasMetadata.model_validate(
        {
            "name": "alpha",
            "workspace_id": str(workspace_id),
            "suggestion": {"workspace_id": str(workspace_id), "title": "Charm"},
        },
    )
    assert canvas.suggestion is None
    assert canvas.workspace_id == workspace_id


@pytest.mark.parametrize("in_workspace", [True, False])
def test_a_canvas_outside_the_suggested_workspace_keeps_the_suggestion(
    *,
    in_workspace: bool,
) -> None:
    suggested = uuid.uuid4()
    canvas = CanvasMetadata(
        name="alpha",
        workspace_id=uuid.uuid4() if in_workspace else None,
        suggestion=WorkspaceSuggestion(workspace_id=suggested, title="Charm"),
    )
    assert canvas.suggestion == WorkspaceSuggestion(
        workspace_id=suggested,
        title="Charm",
    )


def test_a_title_only_suggestion_is_kept_in_a_workspace() -> None:
    canvas = CanvasMetadata(
        name="alpha",
        workspace_id=uuid.uuid4(),
        suggestion=WorkspaceSuggestion(title="A new stream"),
    )
    assert canvas.suggestion == WorkspaceSuggestion(title="A new stream")


def test_a_chat_name_is_trimmed_and_bounded() -> None:
    assert NameChat(name="  Project title  ").name == "Project title"
    with pytest.raises(ValidationError):
        NameChat(name=" ")
    with pytest.raises(ValidationError):
        NameChat(name="x" * 121)


def _box(**changes: object) -> NewMark:
    """Make a valid box from two corners, with ``changes`` applied."""
    return NewMark.model_validate(
        {"points": [{"x": 0.1, "y": 0.2}, {"x": 0.4, "y": 0.5}]} | changes,
    )


def _draw(
    data: CanvasData,
    *,
    new: NewMark,
    author: str = "jp@example.com",
    page: str | None = "#/graph",
) -> CanvasData:
    return apply_operation(
        data,
        AddMark(kind="mark", mark=new),
        default_catalog(),
        author=author,
        page=page,
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"points": [{"x": 0.1, "y": 0.1}]},
        {"points": [{"x": 0, "y": 0}] * 3},
        {"points": [{"x": 1.5, "y": 0}] * 2},
        {"points": [{"x": 0, "y": -0.1}] * 2},
        {"points": [{"x": 0, "y": 0}] * 2, "kind": "arrow"},
        {"points": [{"x": 0, "y": 0}] * 2, "color": "blue"},
        {"points": [{"x": 0, "y": 0}] * 2, "page": "no hash"},
    ],
)
def test_a_box_is_two_corners_on_the_stage_and_nothing_else(
    bad: dict[str, object],
) -> None:
    """Corner count, fractions, extra fields and a bad page are refused."""
    with pytest.raises(ValidationError):
        NewMark.model_validate(bad)


def test_a_box_takes_its_author_page_and_a_rising_seq_from_the_server() -> None:
    """A drawer sends no id, author or seq; the page it names wins over the canvas's."""
    data = _draw(initial_data(default_catalog()), new=_box(), page="#/list/Issue")
    data = _draw(data, new=_box(page="#/own"), author="agent", page="#/list/Issue")
    data = _draw(data, new=_box(), author="agent", page=None)
    assert [(m.seq, m.author, m.page) for m in data.marks] == [
        (1, "jp@example.com", "#/list/Issue"),
        (2, "agent", "#/own"),
        (3, "agent", None),
    ]
    assert data.marks_seq == 3


def test_the_two_hundred_and_first_box_drops_the_oldest() -> None:
    """The count cap evicts from the front and seq keeps counting."""
    full = _draw(initial_data(default_catalog()), new=_box())
    # A full canvas, seeded in place: drawing all 200 re-copies the canvas each time.
    full.marks = [
        full.marks[0].model_copy(update={"seq": seq}) for seq in range(1, 201)
    ]
    full.marks_seq = MAX_MARKS
    data = _draw(full, new=_box())
    assert [m.seq for m in data.marks] == list(range(2, MAX_MARKS + 2))


def test_boxes_past_sixty_four_kib_drop_the_oldest_until_they_fit() -> None:
    """The byte cap binds before the count when the boxes are large."""
    covers = {"records": [str(uuid.uuid4()) for _ in range(20)]}
    data = _draw(initial_data(default_catalog()), new=_box(covers=covers))
    one = len(json.dumps([data.marks[0].model_dump(mode="json")]))
    # Seeded one box short of the byte cap: well under the count cap of 200.
    count = MAX_MARKS_BYTES // one
    data.marks = [
        data.marks[0].model_copy(update={"seq": seq}) for seq in range(1, count + 1)
    ]
    data.marks_seq = count
    assert count < MAX_MARKS
    data = _draw(_draw(data, new=_box(covers=covers)), new=_box(covers=covers))
    stored = json.dumps([mark.model_dump(mode="json") for mark in data.marks])
    assert len(stored) <= MAX_MARKS_BYTES
    # The oldest went: what is left is the newest run, ending at the last drawn.
    seqs = [mark.seq for mark in data.marks]
    assert seqs[0] > 1
    assert seqs == list(range(seqs[0], count + 3))


def test_removing_a_box_takes_that_one_whoever_drew_it_and_a_gone_one_changes_nothing() -> (
    None
):
    """Any box goes by its id; removing it again, or another id, leaves the rest."""
    catalog = default_catalog()
    data = _draw(initial_data(catalog), new=_box(), author="jp")
    data = _draw(data, new=_box(), author="agent")
    first, second = data.marks
    removed = apply_operation(
        data,
        RemoveMark(kind="remove_mark", id=first.id),
        catalog,
    )
    assert [m.id for m in removed.marks] == [second.id]
    again = apply_operation(
        removed,
        RemoveMark(kind="remove_mark", id=first.id),
        catalog,
    )
    assert again == removed
    stranger = apply_operation(
        removed,
        RemoveMark(kind="remove_mark", id=uuid.uuid4()),
        catalog,
    )
    assert stranger == removed
    # Later edits of the returned state must not mutate the previous revision.
    removed.marks[0].points[0].x = 0.8
    removed.visuals[0].params["changed"] = True
    assert data.marks[1].points[0].x == 0.1
    assert data.visuals[0].params == {}


def test_clearing_takes_every_box_and_seq_keeps_counting() -> None:
    """Clear is all: the author does not matter."""
    data = _draw(initial_data(default_catalog()), new=_box(), author="jp")
    data = _draw(data, new=_box(), author="agent")
    cleared = apply_operation(
        data,
        ClearMarks(kind="clear_marks"),
        default_catalog(),
    )
    assert cleared.marks == []
    assert cleared.marks_seq == 2


def test_clearing_a_page_takes_the_boxes_it_shows_and_leaves_the_other_pages() -> None:
    """A page's boxes and the page-less ones go; a box of another page stays."""
    data = initial_data(default_catalog())
    for page in ("#/graph", "#/list/Issue", None, "#/graph"):
        data = _draw(data, new=_box(), page=page)
    here, other, anywhere, again = data.marks

    cleared = apply_operation(
        data,
        ClearMarks(kind="clear_marks", page="#/graph"),
        default_catalog(),
    )

    assert [m.id for m in cleared.marks] == [other.id]
    assert {here.id, anywhere.id, again.id}.isdisjoint(m.id for m in cleared.marks)


_TILE = "cccccccc-cccc-cccc-cccc-cccccccccccc"


def test_a_box_keeps_the_tile_it_was_drawn_on_and_an_old_box_has_none() -> None:
    """The tile rides through `with_mark` and a stored box without one still loads."""
    data = _draw(initial_data(default_catalog()), new=_box(tile=_TILE))
    data = _draw(data, new=_box())
    pinned, loose = data.marks
    assert pinned.tile == uuid.UUID(_TILE)
    assert loose.tile is None
    old = loose.model_dump(mode="json")
    del old["tile"]
    assert Mark.model_validate(old).tile is None


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
