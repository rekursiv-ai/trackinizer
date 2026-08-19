"""Output rendering: row tables, detail views, audit feeds, and ``echo``."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Final, cast

import argparse
import json
import shutil
import sys

from trackinizer.trax.context import err_stream, out_stream
from trackinizer.types.edges import EDGE_POLICIES, Edge


SHOW_IDS: ContextVar[bool] = ContextVar("trax_show_ids", default=False)

TERMINAL_WIDTH: ContextVar[int | None] = ContextVar("trax_terminal_width", default=None)
"""Caller's terminal width, or ``None`` to detect it from ``sys.stdout``.

Set by the daemon, whose stdout is a socket: ``isatty()`` there is always
False, so autodetection would size every table as if piped and print
unbounded-width rows. ``0`` means "not a terminal" (no cap)."""


def show_ids() -> bool:
    """Whether UUIDs should appear in output (set by ``--show-ids``)."""
    return SHOW_IDS.get()


def echo(message: object = "", *, err: bool = False, nl: bool = True) -> None:
    """Write one message to stdout (or stderr when ``err``).

    The stream comes from :mod:`~trax.context`, not ``sys`` directly: the
    daemon serves concurrent invocations in one process, where rebinding
    ``sys.stdout`` around a request would route one caller's output into
    another caller's response.
    """
    stream = err_stream() if err else out_stream()
    stream.write(str(message))
    if nl:
        stream.write("\n")


def print_rows(
    rows: Sequence[dict[str, Any]],
    output: str,
    *,
    width: int | None = None,
) -> None:
    """Print rows as a table, JSON, or one id per line."""
    if output == "json":
        echo(format_json(list(rows)), nl=False)
    elif output == "ids":
        echo(format_ids(list(rows)), nl=False)
    else:
        echo(format_table(list(rows), width=width), nl=False)


def format_field_value(value: object) -> str:
    """Format one projected field; a list prints one item per line.

    A dict (the Experiment ``config`` JSON object) prints as indented
    JSON so the output round-trips through ``config to @file.json``.
    """
    if isinstance(value, list):
        return "\n".join(str(item) for item in cast(list[object], value))
    if isinstance(value, dict):
        return format_json(cast("dict[str, object]", value)).rstrip("\n")
    return str(value)


def add_write_flags(parser: argparse.ArgumentParser) -> None:
    """Add the ``--as`` (actor) and ``--reason`` flags shared by write commands."""
    parser.add_argument("--as", "--actor", dest="actor", default="")
    parser.add_argument("--reason", default="")


def resolve_labels(labels: Sequence[str] | None) -> list[str]:
    """Split comma-separated labels and trim whitespace, dropping empties."""
    out: list[str] = []
    for raw in labels or ():
        out.extend(label.strip() for label in raw.split(",") if label.strip())
    return out


_BAR: Final = "─"


type _RowFn = Callable[[dict[str, Any]], str]
"""Renders one row's cell for a given table column."""


def format_json(payload: object) -> str:
    """Pretty-print ``payload`` as indented JSON, stringifying anything unserializable."""
    return json.dumps(payload, indent=2, default=str) + "\n"


def format_ids(rows: Iterable[dict[str, Any]]) -> str:
    """One row id per line."""
    return "".join(f"{row['id']}\n" for row in rows)


def format_table(rows: Sequence[dict[str, Any]], *, width: int | None = None) -> str:
    """Render rows as an aligned table, dropping empty optional columns to fit width."""
    if not rows:
        return "(no rows)\n"
    columns = _visible_table_columns(
        rows,
        (
            ("ref", lambda r: f"{r.get('kind', '?')}#{r.get('seq', '?')}"),
            ("status", lambda r: cast(str, r.get("status", ""))),
            ("title", lambda r: cast(str, r.get("title", ""))),
            ("priority", lambda r: _row_value(r.get("priority"))),
            ("owner", lambda r: _row_value(r.get("owner"))),
            ("kind", lambda r: _row_value(r.get("issue_kind"))),
            ("labels", lambda r: _row_value(r.get("labels"))),
            ("description", lambda r: _row_value(r.get("description"))),
            ("validation", lambda r: _row_value(r.get("validation"))),
            ("subscribers", lambda r: _row_value(r.get("subscribers"))),
            ("judgement", lambda r: _row_value(r.get("judgement"))),
            ("confidence", lambda r: _row_value(r.get("confidence"))),
            ("outcome", lambda r: _row_value(r.get("outcome"))),
            ("abstract", lambda r: _row_value(r.get("abstract"))),
            ("authors", lambda r: _row_value(r.get("authors"))),
            ("publication_type", lambda r: _row_value(r.get("publication_type"))),
            ("venue", lambda r: _row_value(r.get("venue"))),
            ("subvenue", lambda r: _row_value(r.get("subvenue"))),
            ("publish_date", lambda r: _row_value(r.get("publish_date"))),
            ("source", lambda r: _row_value(r.get("source"))),
            ("sha", lambda r: _row_value(r.get("sha"))),
            ("url", lambda r: _row_value(r.get("url"))),
            ("query", lambda r: _row_value(r.get("query"))),
            ("provider", lambda r: _row_value(r.get("provider"))),
            ("cli", lambda r: _row_value(r.get("cli"))),
            ("codechanges", lambda r: _row_value(r.get("codechanges"))),
            ("edge-priority", lambda r: _row_value(r.get("edge_priority"))),
            ("valence", lambda r: _row_value(r.get("edge_valence"))),
            ("edge-labels", lambda r: _row_value(r.get("edge_labels"))),
            ("note", lambda r: _row_value(r.get("edge_note"))),
            ("agent-cost", lambda r: _row_cost(r, "agent_usd")),
            ("resource-cost", lambda r: _row_cost(r, "resource_usd")),
        ),
    )
    columns = _columns_for_width(columns, width=_resolved_table_width(width))
    cells = [[_table_cell(fn(row)) for _, fn in columns] for row in rows]
    widths = _table_widths(columns, cells, width=_resolved_table_width(width))
    lines: list[str] = [
        "  ".join(
            _truncate_cell(_column_heading(name), w).ljust(w)
            for (name, _), w in zip(columns, widths, strict=True)
        ),
        "  ".join(_BAR * w for w in widths),
    ]
    lines.extend(
        "  ".join(
            _truncate_cell(cell, w).ljust(w)
            for cell, w in zip(row, widths, strict=True)
        )
        for row in cells
    )
    return "\n".join(lines) + "\n"


def format_edge(view: Mapping[str, object], *, changes: bool = False) -> str:
    """Render one edge: its two endpoints and its metadata.

    ``changes`` adds the recent-changes block; off by default to keep
    mutation echoes compact (the CLI opts in with ``--changes``).
    """
    edge = cast(Mapping[str, object], view["edge"])
    lines = [f"edge: {cast(str, view['title'])}"]
    for endpoint in cast(Sequence[Mapping[str, object]], view["endpoints"]):
        lines.append("")
        lines.append(f"{endpoint['label']}:")
        lines.append(
            f"  {endpoint.get('kind')}#{endpoint.get('seq')}  "
            f"{endpoint.get('title') or ''}"
        )
    lines.append("")
    lines.append("edge:")
    lines.extend(_format_selected_edge(edge))
    change_rows = cast(list[dict[str, Any]], view.get("changes") or [])
    if changes and change_rows:
        lines.append("")
        lines.append("Recent changes:")
        lines.extend(_format_change_lines(change_rows[:10]))
    return "\n".join(lines) + "\n"


def format_show(
    view: dict[str, Any],
    *,
    changes: bool = False,
    include_id: bool = False,
) -> str:
    """Render one inquiry: its own fields, then its relations.

    The seq-ref header is always shown; ``include_id`` adds the UUID line.
    """
    self_view = cast(dict[str, Any], view["self"])
    lines: list[str] = []
    ref = f"{self_view.get('kind')}#{self_view.get('seq')}"
    lines.append(f"{ref}  [{self_view.get('status')}]")
    if include_id:
        lines.append(f"  id:          {self_view.get('id')}")
    lines.append(f"  owner:       {self_view.get('owner') or '(unassigned)'}")
    lines.append(f"  account:     {self_view.get('account') or ''}")
    lines.append(f"  title:       {self_view.get('title') or ''}")
    if self_view.get("description"):
        lines.append(f"  description: {self_view['description']}")
    if labels := self_view.get("labels"):
        lines.append(f"  labels:      {','.join(cast(list[str], labels))}")
    if subs := self_view.get("subscribers"):
        lines.append(f"  subscribers: {','.join(cast(list[str], subs))}")
    lines.extend(
        f"  {extra:11}: {value}"
        for extra in (
            "judgement",
            "confidence",
            "kind",
            "validation",
            "priority",
            "outcome",
            "abstract",
            "authors",
            "publication_type",
            "venue",
            "subvenue",
            "publish_date",
            "source",
            "sha",
            "url",
            "query",
            "provider",
            "cli",
            "cli_session_id",
            "started",
            "ended",
        )
        if (
            value := _row_value(
                self_view.get("issue_kind" if extra == "kind" else extra)
            )
        )
    )
    if "codechanges" in self_view:
        ids = cast(list[str], self_view["codechanges"])
        lines.append(f"  codechanges: {len(ids)} entries")
        lines.extend(f"    - {cid}" for cid in ids)
    cost = cast(dict[str, float], self_view.get("marginal_cost") or {})
    agent_cost = float(cost.get("agent_usd", 0))
    resource_cost = float(cost.get("resource_usd", 0))
    if agent_cost:
        lines.append(f"  agent-cost:  ${agent_cost:.4f}")
    if resource_cost:
        lines.append(f"  resource-cost: ${resource_cost:.4f}")
    if created := _format_local_time(self_view.get("created")):
        lines.append(f"  created:     {created}")
    if modified := _format_local_time(self_view.get("modified")):
        lines.append(f"  modified:    {modified}")
    if selected_edge := cast(dict[str, Any], view.get("selected_edge") or {}):
        lines.append("")
        lines.append("Selected edge:")
        lines.extend(_format_selected_edge(selected_edge))
    edges = cast(dict[str, list[dict[str, Any]]], view.get("edges") or {})
    backlinks = cast(dict[str, list[dict[str, Any]]], view.get("backlinks") or {})
    if edges or backlinks:
        lines.append("")
        lines.extend(_format_relations(edges, inbound=False))
        lines.extend(_format_relations(backlinks, inbound=True))
    rows = cast(list[dict[str, Any]], view.get("changes") or [])
    if changes and rows:
        lines.append("")
        lines.append("Recent changes:")
        lines.extend(_format_change_lines(rows[:10]))
    return "\n".join(lines) + "\n"


def format_changes(rows: Sequence[dict[str, Any]]) -> str:
    """Render the audit feed, one entry plus its field deltas."""
    if not rows:
        return "(no changes)\n"
    lines: list[str] = []
    for change in rows:
        lines.append(
            f"{_format_local_time(change.get('created'))}  "
            f"{change.get('kind'):25}  "
            f"{change.get('subject_kind')}#{str(change.get('subject_id', ''))[:8]}  "
            f"{_format_actor(change)}"
        )
        lines.extend(f"  {line}" for line in _format_change_delta(change))
    return "\n".join(lines) + "\n"


def _visible_table_columns(
    rows: Sequence[dict[str, Any]],
    columns: Sequence[tuple[str, _RowFn]],
) -> tuple[tuple[str, _RowFn], ...]:
    return tuple(
        column
        for index, column in enumerate(columns)
        if index < 3 or any(column[1](row) for row in rows)
    )


def _resolved_table_width(width: int | None) -> int:
    if width is not None:
        return width
    if (injected := TERMINAL_WIDTH.get()) is not None:
        return injected
    if not sys.stdout.isatty():
        return 0
    return shutil.get_terminal_size(fallback=(120, 24)).columns


def _columns_for_width(
    columns: tuple[tuple[str, _RowFn], ...],
    *,
    width: int,
) -> tuple[tuple[str, _RowFn], ...]:
    if width <= 0:
        return columns
    out = list(columns)
    for name in (
        "validation",
        "description",
        "subscribers",
        "codechanges",
        "resource-cost",
        "agent-cost",
        "edge-labels",
        "query",
        "url",
        "source",
        "sha",
        "labels",
        "kind",
        "owner",
        "note",
    ):
        if _minimum_table_width(out) <= width:
            break
        out = [column for column in out if column[0] != name]
    return tuple(out)


def _minimum_table_width(columns: Sequence[tuple[str, _RowFn]]) -> int:
    return sum(_minimum_column_width(name) for name, _ in columns) + 2 * (
        len(columns) - 1
    )


def table_width(width: int | None = None) -> int:
    """Table width for this invocation: explicit, terminal, or unbounded."""
    return _resolved_table_width(width)


def table_cell(value: str, width: int) -> str:
    """Collapse whitespace in ``value`` and truncate it to ``width``."""
    return _truncate_cell(_table_cell(value), width)


def _column_heading(name: str) -> str:
    return {
        "priority": "PRI",
        "description": "DESC",
        "validation": "VALID",
        "subscribers": "SUBS",
        "publish_date": "PUBLISHED",
        "edge-priority": "EDGE-PRI",
        "edge-labels": "EDGE-LABEL",
        "agent-cost": "$AGENT",
        "resource-cost": "$RESOURCE",
    }.get(name, name.upper())


def _minimum_column_width(name: str) -> int:
    return {
        "ref": 8,
        "status": 8,
        "title": 20,
        "priority": 3,
        "owner": 8,
        "description": 16,
        "validation": 16,
    }.get(name, len(name))


def _capped_column_width(name: str, natural_width: int) -> int:
    return min(natural_width, _max_column_width(name))


def _max_column_width(name: str) -> int:
    return {
        "description": 40,
        "validation": 40,
        "note": 80,
    }.get(name, natural_unbounded_width())


def natural_unbounded_width() -> int:
    """Sentinel column width standing in for "no cap"."""
    return 1_000_000


def _table_widths(
    columns: Sequence[tuple[str, _RowFn]],
    cells: Sequence[Sequence[str]],
    *,
    width: int,
) -> list[int]:
    natural = [
        _capped_column_width(
            name,
            max(
                len(_column_heading(name)),
                max((len(row[index]) for row in cells), default=0),
            ),
        )
        for index, (name, _) in enumerate(columns)
    ]
    if width <= 0 or sum(natural) + 2 * (len(natural) - 1) <= width:
        return natural
    widths = [_minimum_column_width(name) for name, _ in columns]
    for index, (name, _) in enumerate(columns):
        if name == "ref":
            widths[index] = natural[index]
    separators = 2 * (len(widths) - 1)
    budget = max(width - separators, sum(widths))
    while sum(widths) < budget:
        candidates = [
            index
            for index, natural_width in enumerate(natural)
            if widths[index] < natural_width
        ]
        if not candidates:
            break
        index = max(
            candidates, key=lambda candidate: natural[candidate] - widths[candidate]
        )
        widths[index] += 1
    return widths


def _truncate_cell(value: str, width: int) -> str:
    if width <= 0 or len(value) <= width:
        return value
    if width == 1:
        return "…"
    return value[: width - 1] + "…"


def _table_cell(value: str) -> str:
    return " ".join(value.split())


def _row_value(value: object, _depth: int = 0) -> str:
    if _depth > 8:
        return "..."
    if value is None or value == "":
        return ""
    if isinstance(value, list | tuple):
        return ",".join(
            _row_value(item, _depth + 1) for item in cast(Sequence[object], value)
        )
    return str(value)


def _row_cost(row: dict[str, Any], key: str) -> str:
    value = float(cast(dict[str, float], row.get("marginal_cost") or {}).get(key, 0))
    return f"${value:.2f}" if value else ""


def _format_selected_edge(edge: Mapping[str, object]) -> list[str]:
    lines: list[str] = []
    for label, key in (
        ("priority", "edge_priority"),
        ("valence", "edge_valence"),
        ("labels", "edge_labels"),
        ("note", "edge_note"),
    ):
        if value := _row_value(edge.get(key)):
            lines.append(f"  {label:10}: {value}")
    return lines or ["  (no metadata)"]


def _format_change_lines(changes: Sequence[Mapping[str, object]]) -> list[str]:
    lines: list[str] = []
    for change in changes:
        lines.append(
            f"  {_format_local_time(change.get('created'))}  "
            f"{change.get('kind'):25}  {_format_actor(change)}"
        )
        lines.extend(f"    {line}" for line in _format_change_delta(change))
    return lines


def _format_actor(change: Mapping[str, object]) -> str:
    actor = change.get("actor") or "system"
    principal = change.get("principal")
    if principal and principal != actor:
        return f"actor={actor} principal={principal}"
    return f"actor={actor}"


_CHANGE_DELTA_HIDDEN_KEYS: frozenset[str] = frozenset(
    {"marginal_cost", "updated_at", "modified", "revision"}
)


def _format_change_delta(change: Mapping[str, object]) -> list[str]:
    old = cast(Mapping[str, object], change.get("old") or {})
    new = cast(Mapping[str, object], change.get("new") or {})
    return [
        f"{key}: {old_text or '∅'} -> {new_text or '∅'}"
        for key in tuple(dict.fromkeys((*old.keys(), *new.keys())))
        if key not in _CHANGE_DELTA_HIDDEN_KEYS
        for old_text, new_text in (
            (_row_value(old.get(key)), _row_value(new.get(key))),
        )
        if old_text != new_text
    ]


def _format_local_time(value: object) -> str:
    """Format a server timestamp in local time, or ``""`` if absent or unparseable.

    Returning empty rather than raising lets the surrounding render decide
    whether to drop the line or show a placeholder.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        raw = value.isoformat()
    else:
        raw = str(value).replace("Z", "+00:00")
    if len(raw) < 6 or raw[-6:-5] not in ("+", "-"):
        raw = f"{raw}+00:00"
    try:
        return datetime.fromisoformat(raw).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ""


def _format_relations(
    edges: Mapping[str, list[dict[str, Any]]], *, inbound: bool
) -> list[str]:
    lines: list[str] = []
    for edge_kind, refs in edges.items():
        lines.append(f"{_relation_title(edge_kind, inbound=inbound)}:")
        lines.extend(
            f"  {peer.get('kind')}#{peer.get('seq')}  {peer.get('title')}"
            f"{_edge_annotation(peer)}"
            for peer in refs
        )
    return lines


def _relation_title(edge_kind: str, *, inbound: bool) -> str:
    # ``inbound=False`` is the outbound (forward) view from the subject vertex;
    # ``inbound=True`` reads the edge from the opposite vertex. Every edge is
    # stored child -> parent. Labels come from the single EDGE_POLICIES source
    # (forward/inverse_label), never a parallel table here.
    policy = EDGE_POLICIES.get(cast("Edge.Kind", edge_kind))
    if policy is None:
        return edge_kind.replace("_", " ").title()
    return policy.inverse_label if inbound else policy.forward_label


def _edge_annotation(peer: dict[str, Any]) -> str:
    """Compact ``[rel=...; labels=...; note]`` suffix for a related row, if any."""
    parts: list[str] = []
    if "priority" in peer:
        parts.append(f"prio={peer['priority']}")
    if "valence" in peer:
        parts.append(f"val={peer['valence']}")
    if labels := peer.get("labels"):
        parts.append("labels=" + ",".join(cast(list[str], labels)))
    if note := peer.get("note"):
        parts.append(cast(str, note))
    return f"  [{'; '.join(parts)}]" if parts else ""
