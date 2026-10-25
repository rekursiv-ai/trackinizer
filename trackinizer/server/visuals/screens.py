"""What a Chat sender sees: the records behind their pages and visuals.

The browser sends routes and a canvas; this reads which records they name, in one
query, so the assistant never has to guess what "this" is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, unquote

import re
import uuid

from trackinizer.server.audience import visible_sql
from trackinizer.server.values import vetted_sql
from trackinizer.types.inquiries import KIND_TO_CLASS
from trackinizer.wire.wire_sessions import (
    CanvasMark,
    CanvasPage,
    CanvasRecordContext,
    CanvasVisibleVisual,
)


if TYPE_CHECKING:
    from collections.abc import Sequence

    from trackinizer.lib.postgres import Conn
    from trackinizer.server.audience import SessionReader
    from trackinizer.server.visuals.canvases import Mark, VisualInstance


type Seq = tuple[str, int]
"""A record named by its kind and per-kind number."""

type Named = uuid.UUID | Seq


@dataclass(frozen=True, slots=True, kw_only=True)
class Screen:
    """The sender's pages and visuals, each with the record it shows."""

    page: CanvasPage | None
    trail: list[CanvasPage]
    visuals: list[CanvasVisibleVisual]


async def read_screen(
    conn: Conn,
    *,
    page: str | None,
    trail: Sequence[str],
    visuals: Sequence[VisualInstance],
) -> Screen:
    """Resolve the routes and visuals of a send to their records, in one query.

    A route that names no record, or a record that does not exist, keeps its
    route and has no record.

    Args:
      conn: Connection to read with.
      page: The route the sender is on.
      trail: The routes before it, oldest first.
      visuals: The canvas's visuals.

    Returns:
      screen: The page, the trail and the visuals with the records they show.

    """
    routes = [*trail] if page is None else [page, *trail]
    named = {route: _named(route) for route in routes}
    wanted = [
        *(ref for ref in named.values() if ref is not None),
        *(v.record_id for v in visuals if v.record_id is not None),
    ]
    ids = [ref for ref in wanted if isinstance(ref, uuid.UUID)]
    seqs = [ref for ref in wanted if not isinstance(ref, uuid.UUID)]
    found: dict[Named, CanvasRecordContext] = {}
    for row in (
        await conn.fetch(
            "SELECT id, kind, seq, title FROM inquiries WHERE id = ANY($1::uuid[]) "
            "OR (kind, seq) IN (SELECT * FROM unnest($2::text[], $3::bigint[]))",
            ids,
            [kind for kind, _seq in seqs],
            [seq for _kind, seq in seqs],
        )
        if wanted
        else []
    ):
        record = CanvasRecordContext.model_validate(
            {**dict(row), "title": str(row["title"])[:512]},
        )
        found[record.id] = found[record.kind, record.seq] = record
    pages = {
        route: CanvasPage(
            route=route,
            record=None if ref is None else found.get(ref),
        )
        for route, ref in named.items()
    }
    return Screen(
        page=None if page is None else pages[page],
        trail=[pages[route] for route in trail],
        visuals=[
            CanvasVisibleVisual(
                id=v.id,
                type=v.type,
                record=None if v.record_id is None else found.get(v.record_id),
                params=dict(v.params),
            )
            for v in visuals
        ],
    )


async def read_covered_records(
    conn: Conn,
    *,
    marks: Sequence[Mark],
    reader: SessionReader,
) -> dict[uuid.UUID, CanvasRecordContext]:
    """Resolve the records ``marks`` cover, in one query.

    A mark's covers are what its drawer's browser said, so a session among them is
    named only when ``reader`` reads its transcript.

    Args:
      conn: Connection to read with.
      marks: The marks whose covered records are wanted.
      reader: Who the names are for, as the transcript test reads them.

    Returns:
      records: Each record that exists and may be named, by id; any other is left out.

    """
    ids = sorted({record for mark in marks for record in mark.covers.records})
    if not ids:
        return {}
    params: list[object] = [ids]
    # Audience: gated -- a session is named only to a reader of its transcript.
    readable = visible_sql("i", params=params, reader=reader)
    rows = await conn.fetch(
        vetted_sql(
            "SELECT i.id, i.kind, i.seq, i.title FROM inquiries AS i "
            "WHERE i.id = ANY($1::uuid[]) AND (i.kind <> 'AgentSession' OR ",
            readable,
            ")",
        ),
        *params,
    )
    records = (
        CanvasRecordContext.model_validate(
            {**dict(row), "title": str(row["title"])[:512]},
        )
        for row in rows
    )
    return {record.id: record for record in records}


def wire_marks(
    marks: Sequence[Mark],
    *,
    records: dict[uuid.UUID, CanvasRecordContext],
    visuals: Sequence[CanvasVisibleVisual],
) -> list[CanvasMark]:
    """Say what ``marks`` cover in the form an agent's line carries.

    Args:
      marks: The marks drawn since the sender's last line.
      records: The records they cover that exist, as :func:`read_covered_records` read.
      visuals: The visuals on the canvas, which a mark's instances are matched to.

    Returns:
      marks: Each mark with the visuals and records under it that are still there.
        A mark whose tile left the canvas is left out: the person no longer sees it.

    """
    on_canvas = {visual.id: visual for visual in visuals}
    return [
        CanvasMark(
            id=mark.id,
            author=mark.author,
            page=mark.page,
            tile=None if mark.tile is None else on_canvas[mark.tile],
            window=mark.window,
            left=min(p.x for p in mark.points),
            top=min(p.y for p in mark.points),
            right=max(p.x for p in mark.points),
            bottom=max(p.y for p in mark.points),
            instances=[
                on_canvas[each] for each in mark.covers.instances if each in on_canvas
            ],
            records=[records[each] for each in mark.covers.records if each in records],
        )
        for mark in marks
        if mark.tile is None or mark.tile in on_canvas
    ]


def _named(route: str) -> Named | None:
    """Return the record a route names, or None when it names none."""
    path, _, query = route.removeprefix("#/").partition("?")
    head, *rest = (unquote(part) for part in path.removesuffix("/").split("/"))
    match head, rest:
        case "lookup" | "inquiry", [text]:
            return _uuid(text)
        case "ref", [kind, digits]:
            return _seq(kind, digits=digits)
        case "graph", []:
            focus = parse_qs(query).get("focus", [""])[0]
            kind, _, digits = focus.partition("/")
            return _uuid(focus) or _seq(kind, digits=digits)
        case _:
            return None


def _uuid(text: str) -> uuid.UUID | None:
    """Return the id ``text`` spells, or None."""
    if re.fullmatch(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", text):
        return uuid.UUID(text)
    return None


def _seq(kind: str, *, digits: str) -> Seq | None:
    """Return a kind spelled as the server spells it, with its number, or None."""
    spelled = {known.lower(): known for known in KIND_TO_CLASS}.get(kind.lower())
    if spelled is None or not re.fullmatch(r"[0-9]{1,15}", digits):
        return None
    return spelled, int(digits)
