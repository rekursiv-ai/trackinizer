"""Tests for the canvas room policy: at most two columns of two rows, whatever is shown."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from treekle import json_loads

import pytest

from trackinizer.server.visuals.canvas_room import (
    PAGE,
    Canvas,
    Show,
    Tile,
    canvas_of,
    make_room,
)
from trackinizer.server.visuals.catalog import RECORD_VISUAL


if TYPE_CHECKING:
    from collections.abc import Sequence

    from treekle import PlainTree

_CWD: Final = Path(__file__).resolve().parent

BROWSE = PAGE
GRAPH = "trax.subgraph"
TIMELINE = "trax.timeline"
CAMPAIGN = "trax.campaign"
CAMPAIGNS = "trax.campaigns"
FILES = "trax.files"


def _canvas(*held: tuple[str, str], focused: str | None = None) -> Canvas:
    """Build a canvas of tiles in the order given, each (type, placement); ids are types."""
    return Canvas(
        tiles=tuple(Tile(id=name, type=name, placement=where) for name, where in held),
        focused=focused,
    )


def _steps(canvas: Canvas, request: Show) -> list[tuple[str, ...]]:
    """Return what the policy does for ``request``, each operation as a tuple."""
    steps: list[tuple[str, ...]] = []
    for operation in make_room(canvas, request=request).operations:
        if operation["kind"] == "hide":
            steps.append(("hide", str(operation["instance_id"])))
        else:
            steps.append(
                ("show", str(operation["visual_type"]), str(operation["placement"])),
            )
    return steps


def _held(canvas: Canvas, steps: Sequence[tuple[str, ...]]) -> list[tuple[str, str]]:
    """Return the tiles (type, placement) held once ``steps`` are applied."""
    held = {tile.type: tile.placement for tile in canvas.tiles}
    for step in steps:
        if step[0] == "hide":
            del held[step[1]]
        else:
            held[step[1]] = step[2]
    return list(held.items())


def _within_two_by_two(held: Sequence[tuple[str, str]]) -> bool:
    docked = [where for _, where in held if where != "floating"]
    columns = docked.count("main") + len({where for where in docked if where != "main"})
    return columns <= 2 and docked.count("left") <= 2 and docked.count("side") <= 2


_SHOWS = [
    pytest.param(
        _canvas((BROWSE, "main")),
        Show(type=GRAPH, record_id="r1", placement="side"),
        [("show", GRAPH, "side")],
        id="fresh canvas takes a graph at the side: two columns",
    ),
    pytest.param(
        _canvas((BROWSE, "main")),
        Show(type=TIMELINE, record_id="r1", placement="main"),
        [("show", TIMELINE, "main")],
        id="fresh canvas takes a main visual beside the page: two columns",
    ),
    pytest.param(
        _canvas((BROWSE, "main"), (TIMELINE, "main")),
        Show(type=GRAPH, record_id="r1", placement="side"),
        [("hide", BROWSE), ("show", GRAPH, "side")],
        id="page and a main visual, then a graph: the page closes",
    ),
    pytest.param(
        _canvas((BROWSE, "main"), (TIMELINE, "main")),
        Show(type=CAMPAIGN, record_id="r1", placement="main"),
        [("hide", BROWSE), ("show", CAMPAIGN, "main")],
        id="page and a main visual, then a main visual: the page closes",
    ),
    pytest.param(
        _canvas((BROWSE, "main"), (GRAPH, "side")),
        Show(type=GRAPH, record_id="r2", placement="side"),
        [("show", GRAPH, "side")],
        id="a visual already on the canvas is shown where it is",
    ),
    pytest.param(
        _canvas((BROWSE, "main"), (GRAPH, "side")),
        Show(type=GRAPH, record_id="r2", placement="main"),
        [("show", GRAPH, "main")],
        id="a visual already on the canvas moves; it opens no column",
    ),
    pytest.param(
        _canvas((CAMPAIGN, "main"), (GRAPH, "side")),
        Show(type=TIMELINE, record_id="r1", placement="main"),
        [("show", TIMELINE, "side")],
        id="no page: a main show goes to the side column with a free row",
    ),
    pytest.param(
        _canvas((GRAPH, "left"), (CAMPAIGNS, "side"), (TIMELINE, "side")),
        Show(type=CAMPAIGN, record_id="r1", placement="side"),
        [("show", CAMPAIGN, "left")],
        id="a full side column sends the show to the other column with room",
    ),
    pytest.param(
        _canvas((BROWSE, "main"), (CAMPAIGN, "main")),
        Show(type=CAMPAIGNS, record_id="r1", placement="side"),
        [("hide", BROWSE), ("show", CAMPAIGNS, "side")],
        id="page closes before anything is moved",
    ),
    pytest.param(
        _canvas((GRAPH, "side"), (TIMELINE, "side"), (BROWSE, "main")),
        Show(type=CAMPAIGNS, record_id="r1", placement="side"),
        [("hide", GRAPH), ("show", CAMPAIGNS, "side")],
        id="a full column with the page closed would not help: the oldest goes",
    ),
    pytest.param(
        _canvas(
            (GRAPH, "left"),
            (TIMELINE, "left"),
            (CAMPAIGNS, "side"),
            (CAMPAIGN, "side"),
        ),
        Show(type="trax.artifact", record_id="r1", placement="main"),
        [("hide", GRAPH), ("show", "trax.artifact", "left")],
        id="two full columns: the oldest goes and the show takes its column",
    ),
    pytest.param(
        _canvas((BROWSE, "main"), (TIMELINE, "main"), ("x.float", "floating")),
        Show(type=GRAPH, record_id="r1", placement="side"),
        [("hide", BROWSE), ("show", GRAPH, "side")],
        id="a floating window is neither counted nor closed",
    ),
    pytest.param(
        _canvas((BROWSE, "main"), (TIMELINE, "main")),
        Show(type=GRAPH, record_id="r1", placement="floating"),
        [("show", GRAPH, "floating")],
        id="a floating show needs no room",
    ),
]


@pytest.mark.parametrize(("canvas", "request_", "expected"), _SHOWS)
def test_a_show_leaves_two_columns_of_two_at_most(
    canvas: Canvas,
    request_: Show,
    expected: list[tuple[str, ...]],
) -> None:
    steps = _steps(canvas, request_)

    assert steps == expected
    assert _within_two_by_two(_held(canvas, steps))


def test_a_show_with_no_record_sends_none() -> None:
    plan = make_room(
        _canvas((BROWSE, "main")),
        request=Show(type=CAMPAIGNS, placement="side"),
    )

    assert plan.operations == (
        {"kind": "show", "visual_type": CAMPAIGNS, "placement": "side"},
    )


def test_a_show_carries_its_params_into_the_operation() -> None:
    params = {"path": "loop/foo/", "changed": True, "file": ""}

    plan = make_room(
        _canvas((BROWSE, "main")),
        request=Show(type=FILES, record_id="r", placement="main", params=params),
    )

    assert plan.operations == (
        {
            "kind": "show",
            "visual_type": FILES,
            "placement": "main",
            "record_id": "r",
            "params": params,
        },
    )


def test_a_show_with_no_params_sends_none() -> None:
    plan = make_room(
        _canvas((BROWSE, "main")),
        request=Show(type=FILES, record_id="r", placement="main"),
    )

    assert plan.operations == (
        {"kind": "show", "visual_type": FILES, "placement": "main", "record_id": "r"},
    )


def test_a_launch_opens_the_campaigns_then_the_campaign_and_later_shows_keep_two_by_two() -> (
    None
):
    """The sequence of a launch, then a graph, then a timeline, each planned from the last."""
    canvas = _canvas((BROWSE, "main"))
    outcomes: list[list[tuple[str, ...]]] = []
    for request in (
        Show(type=CAMPAIGNS, record_id="b", placement="side"),
        Show(type=CAMPAIGN, record_id="b", placement="main"),
        Show(type=GRAPH, record_id="i", placement="side"),
        Show(type=TIMELINE, record_id="i", placement="main"),
    ):
        steps = _steps(canvas, request)
        outcomes.append(steps)
        canvas = _canvas(*_held(canvas, steps), focused=request.type)

    assert outcomes == [
        [("show", CAMPAIGNS, "side")],
        [("hide", BROWSE), ("show", CAMPAIGN, "main")],
        [("show", GRAPH, "side")],
        [("hide", CAMPAIGN), ("show", TIMELINE, "main")],
    ]
    assert sorted(_held(canvas, [])) == sorted(
        [(CAMPAIGNS, "side"), (GRAPH, "side"), (TIMELINE, "main")],
    )


_NAVIGATIONS = [
    pytest.param(
        _canvas((BROWSE, "main")),
        [],
        id="page present on a fresh canvas: nothing to do",
    ),
    pytest.param(
        _canvas((BROWSE, "side"), (TIMELINE, "main")),
        [],
        id="page present wherever it stands: it is not moved",
    ),
    pytest.param(
        _canvas((TIMELINE, "main")),
        [],
        id="page absent with room: the page returns by itself",
    ),
    pytest.param(
        _canvas((TIMELINE, "main"), (GRAPH, "side")),
        [("hide", TIMELINE)],
        id="page absent on a full canvas: the oldest closes",
    ),
    pytest.param(
        _canvas((TIMELINE, "main"), (GRAPH, "side"), focused=TIMELINE),
        [("hide", GRAPH)],
        id="page absent on a full canvas: the one in focus stays",
    ),
    pytest.param(
        _canvas((GRAPH, "left"), (CAMPAIGNS, "side"), (TIMELINE, "side")),
        [("hide", GRAPH)],
        id="page absent on two columns of rows: the page cannot join a column",
    ),
]


@pytest.mark.parametrize(("canvas", "expected"), _NAVIGATIONS)
def test_a_navigation_brings_the_page_back_within_two_by_two(
    canvas: Canvas,
    expected: list[tuple[str, ...]],
) -> None:
    steps = _steps(canvas, Show(type=BROWSE, placement="main"))

    assert steps == expected
    after = _held(canvas, steps)
    if not any(kind == BROWSE for kind, _ in after):
        after = [*after, (BROWSE, "main")]
    assert _within_two_by_two(after)


def test_the_page_is_not_a_show_of_its_own_because_the_navigation_brings_it() -> None:
    plan = make_room(
        _canvas((TIMELINE, "main")),
        request=Show(type=BROWSE, placement="main"),
    )

    assert plan.operations == ()
    assert plan.placement == "main"


def test_what_the_plan_did_is_named_for_the_line_the_model_reads() -> None:
    closed = make_room(
        _canvas((BROWSE, "main"), (TIMELINE, "main")),
        request=Show(type=GRAPH, record_id="r", placement="side"),
    )
    moved = make_room(
        _canvas((CAMPAIGN, "main"), (GRAPH, "side")),
        request=Show(type=TIMELINE, record_id="r", placement="main"),
    )
    plain = make_room(
        _canvas((BROWSE, "main")),
        request=Show(type=GRAPH, placement="side"),
    )

    assert [each.type for each in closed.hidden] == [BROWSE]
    assert closed.placement == "side"
    assert moved.hidden == ()
    assert moved.placement == "side"
    assert (plain.hidden, plain.placement) == ((), "side")


def test_the_focused_visual_goes_last_and_the_one_shown_never() -> None:
    held = _canvas(
        (TIMELINE, "main"),
        (GRAPH, "main"),
        focused=TIMELINE,
    )

    moved = _steps(held, Show(type=GRAPH, record_id="r", placement="side"))
    other = _steps(held, Show(type=CAMPAIGN, record_id="r", placement="side"))

    assert moved == [("show", GRAPH, "side")]
    assert other == [("hide", GRAPH), ("show", CAMPAIGN, "side")]


def test_cross_column_eviction_never_closes_an_older_floating_window() -> None:
    held = _canvas(
        ("x.float", "floating"),
        (GRAPH, "left"),
        (TIMELINE, "left"),
        (CAMPAIGNS, "side"),
        (CAMPAIGN, "side"),
    )

    assert _steps(held, Show(type=FILES, placement="main")) == [
        ("hide", GRAPH),
        ("show", FILES, "left"),
    ]


def test_focus_closes_when_it_is_the_only_tile_that_frees_a_column() -> None:
    held = _canvas(
        (GRAPH, "left"),
        (CAMPAIGNS, "side"),
        (TIMELINE, "side"),
        focused=GRAPH,
    )

    assert _steps(held, Show(type=BROWSE, placement="main")) == [("hide", GRAPH)]


@dataclass(frozen=True, slots=True, kw_only=True)
class _Open:
    """One case of the shared table: a canvas, the record opened on it, the steps."""

    case: str
    tiles: list[list[str]]
    page: str | None
    focused: str | None
    record: str
    steps: list[list[str]]


@dataclass(frozen=True, slots=True, kw_only=True)
class _Sequence:
    """Records opened one after another, each planned on the canvas the last left."""

    case: str
    tiles: list[list[str]]
    page: str | None
    records: list[str]
    steps: list[list[list[str]]]
    after: list[list[str]]


@dataclass(frozen=True, slots=True, kw_only=True)
class _Cases:
    """The table this test and a web app's copy of the rule both read."""

    about: str
    kinds: dict[str, str]
    opens: list[_Open]
    sequence: _Sequence


_CASES: Final = json_loads((_CWD / "canvas_room_cases.json").read_bytes(), _Cases)


def _tile(name: str, where: str) -> Tile:
    """Return a record tile for ``I2`` and the like, else a visual of type ``name``."""
    if name[:1] in _CASES.kinds and name[1:].isdigit():
        return Tile(
            id=name,
            type=RECORD_VISUAL,
            placement=where,
            record_id=name,
            kind=_CASES.kinds[name[0]],
        )
    return Tile(id=name, type=name, placement=where)


def _board(
    *held: Sequence[str],
    focused: str | None = None,
    page: str | None = "#/",
) -> Canvas:
    """Build a canvas of tiles (name, placement) in the order given, its page at ``page``."""
    return Canvas(
        tiles=tuple(_tile(name, where) for name, where in held),
        focused=focused,
        page=page,
    )


def _opening(name: str) -> Show:
    """Return the request to open record ``name`` in a tile."""
    return Show(
        type=RECORD_VISUAL,
        placement="left",
        record_id=name,
        params={"kind": _CASES.kinds[name[0]]},
    )


def _opened(canvas: Canvas, name: str) -> list[list[str]]:
    """Return what opening record ``name`` does, a tile named by its id."""
    return [
        ["hide", str(operation["instance_id"])]
        if operation["kind"] == "hide"
        else ["show", str(operation["record_id"]), str(operation["placement"])]
        for operation in make_room(canvas, request=_opening(name)).operations
    ]


def _after(canvas: Canvas, steps: Sequence[Sequence[str]]) -> list[list[str]]:
    """Return the tiles [id, placement] on ``canvas`` once ``steps`` are applied."""
    held = {tile.id: tile.placement for tile in canvas.tiles}
    for step in steps:
        if step[0] == "hide":
            del held[step[1]]
        else:
            held[step[1]] = step[2]
    return [[name, where] for name, where in held.items()]


@pytest.mark.parametrize("case", _CASES.opens, ids=[each.case for each in _CASES.opens])
def test_a_record_opens_in_a_tile_within_two_columns_of_two(case: _Open) -> None:
    canvas = _board(*case.tiles, focused=case.focused, page=case.page)

    steps = _opened(canvas, case.record)

    assert steps == case.steps
    assert _within_two_by_two([(name, where) for name, where in _after(canvas, steps)])


def test_records_opened_one_after_another_keep_to_the_rule_at_each_step() -> None:
    sequence = _CASES.sequence
    canvas = _board(*sequence.tiles, page=sequence.page)
    outcomes: list[list[list[str]]] = []
    for name in sequence.records:
        steps = _opened(canvas, name)
        outcomes.append(steps)
        canvas = _board(*_after(canvas, steps), focused=name, page=sequence.page)

    assert outcomes == sequence.steps
    assert _after(canvas, []) == sequence.after


def test_a_record_show_names_its_record_and_kind() -> None:
    plan = make_room(_board(), request=_opening("E4"))

    assert plan.operations == (
        {
            "kind": "show",
            "visual_type": RECORD_VISUAL,
            "placement": "left",
            "record_id": "E4",
            "params": {"kind": "Experiment"},
        },
    )
    assert (plan.hidden, plan.placement) == ((), "left")


def test_the_page_that_holds_nothing_closes_unnamed_since_it_made_no_room() -> None:
    on_home = make_room(_board((BROWSE, "main")), request=_opening("I1"))
    alone = make_room(
        _board((BROWSE, "main"), page="#/console"),
        request=_opening("I1"),
    )
    for_room = make_room(
        _board((BROWSE, "main"), ("I1", "left"), page="#/console"),
        request=_opening("E1"),
    )

    for plan in (on_home, alone):
        assert [operation["kind"] for operation in plan.operations] == ["hide", "show"]
        assert plan.hidden == ()
    assert [tile.id for tile in for_room.hidden] == [BROWSE]


def test_canvas_of_reads_each_tiles_record_and_kind_and_the_page() -> None:
    state: dict[str, PlainTree] = {
        "visuals": [
            {"id": "p", "type": PAGE, "placement": "main", "record_id": None},
            {
                "id": "t",
                "type": RECORD_VISUAL,
                "placement": "left",
                "record_id": "r",
                "params": {"kind": "Issue"},
            },
        ],
        "focused_instance": "t",
        "page": "#/console",
    }

    assert canvas_of(state) == Canvas(
        tiles=(
            Tile(id="p", type=PAGE, placement="main"),
            Tile(
                id="t",
                type=RECORD_VISUAL,
                placement="left",
                record_id="r",
                kind="Issue",
            ),
        ),
        focused="t",
        page="#/console",
    )
    assert canvas_of({"visuals": []}).page is None


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
