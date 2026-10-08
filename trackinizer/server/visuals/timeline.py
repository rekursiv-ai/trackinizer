"""Bounded SQL projection for a record's lineage and dated evidence timeline."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Final, cast
from uuid import UUID


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn


_EVIDENCE_LIMIT: Final = 6
"""Signed citations shown per Experiment; one extra row sets the truncation flag."""

# A fixed ceiling rather than a catalog parameter: a lead is context pinned beside the
# axis, so a deeper chain adds rows without adding information.
_LEAD_LEVELS: Final = 3
"""Most lead Issues returned: `narrows` ancestors climbed above the anchor."""

_ANCHORED: Final = frozenset({"Issue", "Experiment"})
"""Kinds whose own row is the anchor Issue's: they show its results."""


async def load_timeline(
    conn: Conn,
    target_id: UUID,
    *,
    direction_limit: int,
    results_per_direction: int,
) -> dict[str, object] | None:
    """Load one record's lineage and timeline with SQL-level row bounds.

    The record shown is the target for every kind except an Experiment, whose
    row is the Issue that produced it. The Issue supplying leads, results and
    directions is the *anchor*: an Issue is its own anchor, an Experiment's is
    its `produced_by` Issue, and any other kind takes the nearest Issue it was
    `produced_by` (oldest first). `narrows` is Issue-to-Issue only, so no other
    kind can reach an Issue by it. A record with no anchor is shown alone.

    Args:
      conn: Database connection.
      target_id: Record of any kind selected in the canvas.
      direction_limit: Direct directions to return. One extra row is fetched
        internally to set `directions_truncated`.
      results_per_direction: Results to return per Issue. One extra row is
        fetched internally to set `results_truncated`.

    Returns:
      timeline: Bounded timeline payload, or None when the record is missing.

    """
    target = await conn.fetchrow(_record_query(), target_id)
    if target is None:
        return None
    target_json = _record(target)
    kind = target["kind"]
    if kind == "Issue":
        issue_id: UUID | None = target_id
    else:
        producer = await conn.fetchrow(
            "SELECT i.id FROM edges e JOIN inquiries i ON i.id = e.to_id "
            "WHERE e.from_id = $1 AND e.edge_kind = 'produced_by' "
            "AND e.to_kind = 'Issue' ORDER BY i.created, i.id LIMIT 1",
            target_id,
        )
        issue_id = _as_uuid(producer["id"]) if producer is not None else None

    selected_result = (
        await _load_experiment(conn, target_id) if kind == "Experiment" else None
    )
    if issue_id is None:
        return {
            "target": target_json,
            "issue": None,
            "leads": [],
            "selected_result": selected_result,
            "root_results": [],
            "root_results_truncated": False,
            "directions": [],
            "directions_truncated": False,
            "unresolved_questions": [],
        }
    issue_row = await conn.fetchrow(_record_query(), issue_id)
    issue = _record(issue_row) if issue_row is not None else None
    # Only an Issue or an Experiment is a row of the anchor's own results.
    leads = await _load_leads(
        conn,
        issue_id=issue_id,
        include_anchor=kind not in _ANCHORED,
    )
    if kind in _ANCHORED:
        root_results, root_truncated = await _load_results(
            conn,
            issue_id,
            results_per_direction,
        )
    else:
        root_results, root_truncated = [], False

    direction_rows = await conn.fetch(
        "SELECT i.id, i.kind, i.seq, LEFT(i.title, 2000) AS title, i.status, "
        "i.created, i.modified, LEFT(i.description, 2000) AS description "
        "FROM edges e JOIN inquiries i ON i.id=e.from_id "
        "WHERE e.to_id=$1 AND e.from_id <> $1 AND e.edge_kind='narrows' "
        "AND e.from_kind='Issue' AND e.to_kind='Issue' "
        "ORDER BY i.created, i.id LIMIT $2",
        issue_id,
        direction_limit + 1,
    )
    directions_truncated = len(direction_rows) > direction_limit
    directions: list[dict[str, object]] = []
    unresolved: list[dict[str, object]] = []
    for direction in direction_rows[:direction_limit]:
        direction_id = _as_uuid(direction["id"])
        results, truncated = await _load_results(
            conn,
            direction_id,
            results_per_direction,
        )
        directions.append(
            {
                "issue": _record(direction),
                "results": results,
                "results_truncated": truncated,
            },
        )
        if direction["status"] == "active":
            question = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM inquiries WHERE id=$1 "
                "AND issue_kind @> ARRAY['question']::text[])",
                direction_id,
            )
            if question:
                unresolved.append(_record(direction))

    return {
        "target": target_json,
        "issue": issue,
        "leads": leads,
        "selected_result": selected_result,
        "root_results": root_results,
        "root_results_truncated": root_truncated,
        "directions": directions,
        "directions_truncated": directions_truncated,
        "unresolved_questions": unresolved,
    }


def _record_query() -> str:
    """Return the fixed projection shared by timeline record fetches."""
    return (
        "SELECT id, kind, seq, LEFT(title, 2000) AS title, status, created, "
        "modified, LEFT(description, 2000) AS description, "
        "LEFT(experiment_outcome, 2000) AS outcome FROM inquiries WHERE id=$1"
    )


def _record(row: object) -> dict[str, object]:
    values = cast(Mapping[str, object], row)
    return {
        "id": str(values["id"]),
        "kind": values["kind"],
        "seq": values["seq"],
        "title": values["title"],
        "status": values["status"],
        "created": values["created"],
        "modified": values["modified"],
        "description": values["description"],
        "outcome": values.get("outcome"),
    }


def _as_uuid(value: object) -> UUID:
    """Narrow a database identifier to the domain UUID type."""
    return UUID(str(value))


# One indexed lookup a level keeps the read bounded however deep a lineage runs. The
# oldest parent is the one an Issue was first filed under, and a repeated Issue means a
# `narrows` cycle, which would otherwise fill every level with the same rows. A record
# that is not an Issue shows its anchor as its nearest lead, so the anchor takes a level.
async def _load_leads(
    conn: Conn,
    *,
    issue_id: UUID,
    include_anchor: bool,
) -> list[dict[str, object]]:
    """Climb `narrows` from the anchor Issue, farthest lead first."""
    seen = {issue_id}
    ancestors: list[dict[str, object]] = []
    current = issue_id
    for _ in range(_LEAD_LEVELS - (1 if include_anchor else 0)):
        parent = await conn.fetchrow(
            "SELECT i.id, i.kind, i.seq, LEFT(i.title, 2000) AS title, i.status, "
            "i.created, i.modified, LEFT(i.description, 2000) AS description "
            "FROM edges e JOIN inquiries i ON i.id=e.to_id WHERE e.from_id=$1 "
            "AND e.edge_kind='narrows' AND e.from_kind='Issue' "
            "AND e.to_kind='Issue' ORDER BY i.created, i.id LIMIT 1",
            current,
        )
        if parent is None:
            break
        current = _as_uuid(parent["id"])
        if current in seen:
            break
        seen.add(current)
        ancestors.append(_record(parent))
    ancestors.reverse()
    if include_anchor:
        anchor = await conn.fetchrow(_record_query(), issue_id)
        if anchor is not None:
            ancestors.append(_record(anchor))
    return ancestors


async def _load_results(
    conn: Conn,
    issue_id: UUID,
    limit: int,
) -> tuple[list[dict[str, object]], bool]:
    rows = await conn.fetch(
        "SELECT i.id, i.kind, i.seq, LEFT(i.title, 2000) AS title, i.status, "
        "i.created, i.modified, LEFT(i.description, 2000) AS description, "
        "LEFT(i.experiment_outcome, 2000) AS outcome FROM edges e "
        "JOIN inquiries i ON i.id=e.from_id WHERE e.to_id=$1 "
        "AND e.edge_kind='produced_by' AND e.from_kind='Experiment' "
        "AND e.to_kind='Issue' ORDER BY i.created DESC, i.id DESC LIMIT $2",
        issue_id,
        limit + 1,
    )
    truncated = len(rows) > limit
    selected = list(reversed(rows[:limit]))
    return await _load_record_results(conn, selected), truncated


async def _load_experiment(conn: Conn, experiment_id: UUID) -> dict[str, object] | None:
    """Load one selected Experiment and its bounded signed citations."""
    row = await conn.fetchrow(_record_query(), experiment_id)
    if row is None:
        return None
    return (await _load_record_results(conn, [row]))[0]


async def _load_record_results(
    conn: Conn,
    rows: Sequence[object],
) -> list[dict[str, object]]:
    """Project selected Experiment rows and their bounded evidence edges."""
    result: list[dict[str, object]] = []
    for row in rows:
        values = cast(Mapping[str, object], row)
        evidence_rows = await conn.fetch(
            "SELECT c.id, c.kind, c.seq, LEFT(c.title, 2000) AS title, c.status, "
            "c.created, c.modified, LEFT(c.description, 2000) AS description, "
            "e.edge_kind, e.valence, LEFT(e.note, 2000) AS note FROM edges e "
            "JOIN inquiries c ON c.id=e.to_id WHERE e.from_id=$1 "
            "AND e.edge_kind IN ('proves','favors') ORDER BY c.created, c.id LIMIT $2",
            values["id"],
            _EVIDENCE_LIMIT + 1,
        )
        evidence = [
            {
                "claim": _record(item),
                "edge_kind": item["edge_kind"],
                "valence": item["valence"],
                "note": item["note"],
            }
            for item in evidence_rows[:_EVIDENCE_LIMIT]
        ]
        result.append(
            {
                "record": _record(row),
                "evidence": evidence,
                "evidence_truncated": len(evidence_rows) > _EVIDENCE_LIMIT,
            },
        )
    return result
