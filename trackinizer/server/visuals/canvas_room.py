"""How a canvas keeps at most two columns of at most two rows as visuals are shown.

A canvas stands its docked visuals in columns: each tile of the main strip is a column
of one row, and the ``left`` and ``side`` columns count as one each while they hold a
tile, and stack the tiles they hold as rows. Floating windows and Chat count for
nothing.

:func:`make_room` is pure: it takes the canvas as it stands and one show, and returns
the operations that leave the requested visual shown and the canvas within that bound,
changing as little as it can, in this order:

1. A visual already on the canvas moves; it opens no column of its own.
2. The page (``trax.browse``) closes, unless the page is what was asked for.
3. A show that would open a column goes into a ``left`` or ``side`` column that holds
   one tile, as its second row.
4. The visual focused least recently closes, never the one being shown. The canvas
   keeps one focus, so the tile in focus goes last and the others go oldest first.
   When no single visual makes room, the oldest closes and the show may take its column.

A record tile (``trax.record``) has one instance per record and stands where its kind
puts it, not where it was asked to (:func:`record_room`):

1. A record already on the canvas is shown where it stands.
2. The page closes when it holds nothing: on Home, which shows nothing, or docked alone,
   whatever it shows, so the first record takes its place.
3. Its column is the ``left`` or ``side`` one that holds a record of its kind; else
   one that holds nothing, if the canvas has room for another column, closing the page
   for it if that is what it takes; else one with a free row; else the visual focused
   least recently closes and the column is chosen again.
4. A full column closes its oldest tile, in focus or not, so a column shows its two
   newest. A web app draws a column's record tiles newest first, the newest over two
   thirds of the column, so the new one stands on top.

The server makes the same room for a record an agent's key shows
(``canvases.apply_operation``): with no placement the rule places it; a placement the
agent names is kept, and only the page that holds nothing closes. A web app that opens
records in tiles plans its links by the same rule; its tests and this module's read one
table of cases, ``canvas_room_cases.json``, so a copy that drifts fails a test.

The page is never a show of its own: a navigation brings it back, in the main strip, so
a request for it returns only the operations that make room for it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from treekle import from_plain

from trackinizer.server.visuals.catalog import RECORD_VISUAL


if TYPE_CHECKING:
    from collections.abc import Sequence

    from treekle import PlainTree


__all__ = [
    "PAGE",
    "Canvas",
    "Column",
    "Plan",
    "RecordRoom",
    "Show",
    "Tile",
    "make_room",
    "record_room",
    "returned_canvas",
]

PAGE: Final = "trax.browse"
"""The page: the catalog's default visual, which navigation moves."""

type Column = Literal["left", "side"]
"""A column beside the main strip: left of it, or right of it (``side``)."""

_MAX_ROWS: Final = 2
_COLUMNS: Final[tuple[Column, Column]] = ("left", "side")
"""The two columns beside the main strip, in the order they stand."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Tile:
    """One visual on a canvas: its instance id, type and placement."""

    id: str
    type: str
    placement: str
    record_id: str | None = None
    """The record it shows, if any."""

    kind: str = ""
    """A record tile's kind, from its ``kind`` param: what it stacks with."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Canvas:
    """A canvas as the policy reads it: its tiles in order, and the focused one."""

    tiles: tuple[Tile, ...]
    focused: str | None = None
    """The focused tile's id, if any."""

    page: str | None = None
    """The route the page last stood on, as the canvas keeps it; None while it never
    moved, which is Home."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Show:
    """One requested show: a visual type, its record if any, and its placement."""

    type: str
    placement: str
    """Where it is asked to stand; a record tile stands where its kind puts it."""

    record_id: str | None = None
    params: Mapping[str, str | int | bool] | None = None
    """The visual's params, all set anew; None sends none, so the shown visual keeps
    the ones it has. A record tile's ``kind`` picks its column."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Plan:
    """What to do for a show: the operations in order, and what they did."""

    operations: tuple[dict[str, object], ...]
    """``hide`` operations, then the ``show`` (none for the page)."""

    hidden: tuple[Tile, ...]
    """The tiles the plan closes to make room."""

    placement: str
    """Where the requested visual ends up, which is not the requested one when the
    plan put it in a column with a free row."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordRoom:
    """Where a new record tile stands, and what closes for it."""

    column: Column
    idle: tuple[Tile, ...]
    """The page, when it closes for holding nothing rather than for room."""

    hidden: tuple[Tile, ...]
    """The tiles that close to make room."""


def returned_canvas(state: dict[str, PlainTree]) -> Canvas:
    """Read the canvas a ``read_canvas`` call returned.

    Args:
      state: The canvas state as the server returns it.

    Returns:
      canvas: Its visuals in order, its focused instance and its page's route.

    """
    visuals = from_plain(state.get("visuals"), list[dict[str, object]], default=[])
    focused = from_plain(state.get("focused_instance"), str, default="") or None
    return Canvas(
        tiles=tuple(
            Tile(
                id=from_plain(each.get("id"), str),
                type=from_plain(each.get("type"), str),
                placement=from_plain(each.get("placement"), str, default="main"),
                record_id=from_plain(each.get("record_id"), str, default=None),
                kind=from_plain(
                    from_plain(each.get("params"), dict[str, object], default={}).get(
                        "kind",
                    ),
                    str,
                    default="",
                ),
            )
            for each in visuals
        ),
        focused=focused,
        page=from_plain(state.get("page"), str, default=None),
    )


def make_room(canvas: Canvas, *, request: Show) -> Plan:
    """Plan the operations that show ``request`` within two columns of two rows.

    Args:
      canvas: The canvas as it stands.
      request: The visual to show. A request for the page names the navigation that
        will bring it back; the page is left where it stands when it is there.

    Returns:
      plan: The operations to apply in order, the tiles they close and where the
        visual lands.

    """
    if request.type == RECORD_VISUAL:
        return _room_for_record(canvas, request=request)
    page = request.type == PAGE
    present = next((tile for tile in canvas.tiles if tile.type == request.type), None)
    if page and present is not None:
        return Plan(operations=(), hidden=(), placement=present.placement)
    base = [tile for tile in canvas.tiles if tile.type != request.type]
    hidden: list[Tile] = []
    placement = request.placement
    # The page returns in the main strip whatever else is true, so it is never moved.
    movable = not page
    if not _fits(base, placement=placement):
        page_tile = next((t for t in base if t.type == PAGE), None)
        if page_tile is not None and _fits(
            _without(base, gone=page_tile),
            placement=placement,
        ):
            hidden.append(page_tile)
        elif movable and (column := _column_with_room(base)) is not None:
            placement = column
        else:
            hidden, placement = _close_until_fits(
                base,
                focused=canvas.focused,
                placement=placement,
                movable=movable,
            )
    return Plan(
        operations=(
            *(_hide(tile) for tile in hidden),
            *(() if page else (_show(request, placement=placement),)),
        ),
        hidden=tuple(hidden),
        placement=placement,
    )


def record_room(canvas: Canvas, *, kind: str) -> RecordRoom:
    """Choose the column a new record tile stands in, and what closes for it.

    Args:
      canvas: The canvas as it stands, which does not hold the record yet.
      kind: The record's kind, which picks its column.

    Returns:
      room: Its column, the page if it holds nothing, and the tiles that close for
        room, the oldest of its column among them when the column is full.

    """
    idle = tuple(
        tile for tile in canvas.tiles if tile.type == PAGE and _holds_nothing(canvas)
    )
    held = [tile for tile in canvas.tiles if tile not in idle]
    column, hidden = _record_column(held, kind=kind, focused=canvas.focused)
    held = [tile for tile in held if tile not in hidden]
    if _rows(held, column=column) >= _MAX_ROWS:
        hidden.append(next(tile for tile in held if tile.placement == column))
    return RecordRoom(column=column, idle=idle, hidden=tuple(hidden))


def _room_for_record(canvas: Canvas, *, request: Show) -> Plan:
    """Plan a record tile: the column its kind puts it in, with room made there."""
    present = next(
        (
            tile
            for tile in canvas.tiles
            if tile.type == RECORD_VISUAL and tile.record_id == request.record_id
        ),
        None,
    )
    if present is not None:
        return Plan(
            operations=(_show(request, placement=present.placement),),
            hidden=(),
            placement=present.placement,
        )
    room = record_room(canvas, kind=str((request.params or {}).get("kind", "")))
    # The page that holds nothing closes for that, not for room, so the plan does not
    # name it among the tiles it closed.
    return Plan(
        operations=(
            *(_hide(tile) for tile in (*room.idle, *room.hidden)),
            _show(request, placement=room.column),
        ),
        hidden=room.hidden,
        placement=room.column,
    )


def _record_column(
    tiles: Sequence[Tile],
    *,
    kind: str,
    focused: str | None,
) -> tuple[Column, list[Tile]]:
    """Choose a record tile's column, and the tiles that close so it has one."""
    held = list(tiles)
    hidden: list[Tile] = []
    of_kind = {
        tile.placement
        for tile in held
        if tile.type == RECORD_VISUAL and tile.kind == kind
    }
    column = next((each for each in _COLUMNS if each in of_kind), _open_column(held))
    page = next((tile for tile in held if tile.type == PAGE), None)
    if column is None and page is not None:
        column = _open_column(_without(held, gone=page))
        if column is not None:
            hidden.append(page)
    while column is None:
        column = _column_with_room(held)
        if column is None:
            oldest = _by_recency(held, focused=focused)[0]
            held = _without(held, gone=oldest)
            hidden.append(oldest)
            # A tile closing never makes a column of the kind, looked for first.
            column = _open_column(held)
    return column, hidden


def _holds_nothing(canvas: Canvas) -> bool:
    """Whether the canvas's page holds nothing: it stands on Home, or docked alone."""
    return _at_home(canvas.page) or all(
        tile.type == PAGE for tile in canvas.tiles if tile.placement != "floating"
    )


def _at_home(route: str | None) -> bool:
    """Whether ``route`` is Home as the web app reads it: ``#/``, or the old ``#/graph``."""
    if route is None:
        return True
    path = route.removeprefix("#").removeprefix("/").partition("?")[0].rstrip("/")
    return path in {"", "graph"}


def _open_column(tiles: Sequence[Tile]) -> Column | None:
    """Return a ``left`` or ``side`` column that holds nothing, if the canvas has room."""
    return next(
        (
            column
            for column in _COLUMNS
            if not _rows(tiles, column=column) and _fits(tiles, placement=column)
        ),
        None,
    )


def _fits(tiles: Sequence[Tile], *, placement: str) -> bool:
    """Whether a new tile at ``placement`` leaves ``tiles`` within the bound."""
    if placement == "floating":
        return True
    main = sum(1 for tile in tiles if tile.placement == "main")
    rows: dict[str, int] = {column: _rows(tiles, column=column) for column in _COLUMNS}
    if placement == "main":
        main += 1
    else:
        rows[placement] += 1
    columns = main + sum(1 for held in rows.values() if held)
    return columns <= 2 and all(held <= _MAX_ROWS for held in rows.values())


def _rows(tiles: Sequence[Tile], *, column: str) -> int:
    return sum(1 for tile in tiles if tile.placement == column)


def _without(tiles: Sequence[Tile], *, gone: Tile) -> list[Tile]:
    return [tile for tile in tiles if tile.id != gone.id]


def _column_with_room(tiles: Sequence[Tile]) -> Column | None:
    """Return a ``left`` or ``side`` column that holds a tile and has a free row."""
    return next(
        (
            column
            for column in _COLUMNS
            if 0 < _rows(tiles, column=column) < _MAX_ROWS
            and _fits(tiles, placement=column)
        ),
        None,
    )


def _close_until_fits(
    base: Sequence[Tile],
    *,
    focused: str | None,
    placement: str,
    movable: bool,
) -> tuple[list[Tile], str]:
    """Close the visual focused least recently until a show at ``placement`` fits."""
    held = list(base)
    hidden: list[Tile] = []
    while True:
        order = _by_recency(held, focused=focused)
        single = next(
            (
                tile
                for tile in order
                if _fits(_without(held, gone=tile), placement=placement)
            ),
            None,
        )
        if single is not None:
            return [*hidden, single], placement
        if not order:
            return hidden, placement
        held = _without(held, gone=order[0])
        hidden.append(order[0])
        if movable and (column := _column_with_room(held)) is not None:
            return hidden, column


def _by_recency(tiles: Sequence[Tile], *, focused: str | None) -> list[Tile]:
    """Order the docked tiles as they close: oldest first, the focused one last."""
    docked = [tile for tile in tiles if tile.placement != "floating"]
    return [t for t in docked if t.id != focused] + [
        t for t in docked if t.id == focused
    ]


def _hide(tile: Tile) -> dict[str, object]:
    """Return the ``hide`` operation for ``tile``."""
    return {"kind": "hide", "instance_id": tile.id}


def _show(request: Show, *, placement: str) -> dict[str, object]:
    """Return the ``show`` operation for ``request`` at ``placement``."""
    operation: dict[str, object] = {
        "kind": "show",
        "visual_type": request.type,
        "placement": placement,
    }
    if request.record_id is not None:
        operation["record_id"] = request.record_id
    if request.params is not None:
        operation["params"] = dict(request.params)
    return operation
