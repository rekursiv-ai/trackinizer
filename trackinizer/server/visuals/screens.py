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

from trackinizer.types.inquiries import KIND_TO_CLASS
from trackinizer.wire.wire_sessions import (
    WorkspacePage,
    WorkspaceRecordContext,
    WorkspaceVisibleVisual,
)


if TYPE_CHECKING:
    from collections.abc import Sequence

    from trackinizer.lib.postgres import Conn
    from trackinizer.server.visuals.workspaces import VisualInstance


type Seq = tuple[str, int]
"""A record named by its kind and per-kind number."""

type Named = uuid.UUID | Seq


@dataclass(frozen=True, slots=True, kw_only=True)
class Screen:
    """The sender's pages and visuals, each with the record it shows."""

    page: WorkspacePage | None
    trail: list[WorkspacePage]
    visuals: list[WorkspaceVisibleVisual]


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
    found: dict[Named, WorkspaceRecordContext] = {}
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
        record = WorkspaceRecordContext.model_validate(
            {**dict(row), "title": str(row["title"])[:512]},
        )
        found[record.id] = found[record.kind, record.seq] = record
    pages = {
        route: WorkspacePage(
            route=route,
            record=None if ref is None else found.get(ref),
        )
        for route, ref in named.items()
    }
    return Screen(
        page=None if page is None else pages[page],
        trail=[pages[route] for route in trail],
        visuals=[
            WorkspaceVisibleVisual(
                id=v.id,
                type=v.type,
                record=None if v.record_id is None else found.get(v.record_id),
            )
            for v in visuals
        ],
    )


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
