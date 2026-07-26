"""SQL fragments derived from the EdgeKindPolicy registry.

Every edge-walking query (cascade, next_issue, proves_belief) used to
hardcode specific edge kinds (the supersession / refutation / decomposition
branches) directly. Those branches now derive from
:data:`~trackinizer.types.edges.EDGE_POLICIES` via
the helpers below -- one table change adds a new edge kind everywhere
consistently.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final, Literal, cast

from trackinizer.server.values import vetted_sql
from trackinizer.types.edges import EDGE_POLICIES


def _quote_kinds(kinds: Iterable[str]) -> str:
    """Render an iterable of edge-kind names as a SQL IN-list body.

    Only consumed by trusted-internal SQL builders below; the strings
    come from :data:`Edge.Kind`, a closed set.
    """
    return ", ".join(f"'{k}'" for k in sorted(kinds))


def _policy_exclude_clauses(
    *,
    subject_alias: str,
    policy_attr: Literal["skips_scheduler_on", "invalidates_currency_on"],
) -> str:
    """Render ``AND NOT EXISTS (...)`` clauses for every policy that
    sets ``policy_attr``.

    Used by the ``next_issue`` and ``proves_belief`` queries to
    drop endpoints that an edge marks as scheduler-excluded or
    currency-invalidated. ``subject_alias`` is the inquiry alias the
    NOT EXISTS subquery joins against (``issue.id`` for next_issue,
    ``t.id`` for proves_belief).
    """
    sides: dict[Literal["from", "to"], list[str]] = {"from": [], "to": []}
    for kind, policy in EDGE_POLICIES.items():
        side = getattr(policy, policy_attr)
        if side is None:
            continue
        sides[cast(Literal["from", "to"], side)].append(kind)
    clauses: list[str] = []
    for side, kinds in sides.items():
        if not kinds:
            continue
        column = "from_id" if side == "from" else "to_id"
        kindset = _quote_kinds(kinds)
        clauses.append(
            vetted_sql(
                "AND NOT EXISTS (SELECT 1 FROM edges p WHERE p.",
                column,
                " = ",
                subject_alias,
                " AND p.edge_kind IN (",
                kindset,
                "))",
            )
        )
    return " ".join(clauses)


_NEXT_ISSUE_SQL: str = vetted_sql(
    "SELECT issue.* FROM inquiries issue "
    "WHERE issue.kind = 'Issue' AND issue.status = 'active' "
    "  AND NOT EXISTS ("
    # ``requires`` is stored requirer -> prerequisite, so an issue with an
    # active prerequisite (its to-side) is not yet schedulable.
    "    SELECT 1 FROM edges e "
    "    JOIN inquiries prerequisite ON prerequisite.id = e.to_id "
    "    WHERE e.from_id = issue.id "
    "      AND e.edge_kind = 'requires' "
    "      AND prerequisite.status = 'active'"
    "  ) ",
    _policy_exclude_clauses(subject_alias="issue.id", policy_attr="skips_scheduler_on"),
    " ORDER BY issue.issue_priority, issue.created LIMIT 1",
)
"""Next active Issue whose prerequisites are terminal and which no
:data:`EdgeKindPolicy` excludes from the scheduler.

Built from the policy registry: any edge kind with
``skips_scheduler_on`` contributes a ``NOT EXISTS`` clause here.
"""


_PROVES_BELIEF_SQL: str = vetted_sql(
    # proves is stored Artifact(from) -> Belief(to), so the artifacts proving
    # belief $1 are the from-side of edges pointing at it.
    "SELECT t.* FROM inquiries t "
    "JOIN edges e ON e.from_id = t.id AND e.edge_kind = 'proves' "
    "WHERE e.to_id = $1 "
    "  AND ("
    "    (t.kind = 'Belief' AND t.belief_judgement = 'proven') "
    "    OR (t.kind = 'Experiment' AND t.status = 'complete') "
    "    OR (t.kind NOT IN ('Belief', 'Experiment') AND t.status = 'active')"
    "  ) ",
    _policy_exclude_clauses(
        subject_alias="t.id", policy_attr="invalidates_currency_on"
    ),
    " ORDER BY t.created",
)
"""Currently-true ``proves``-citations a Belief depends on.

Drops any Artifact that an :class:`EdgeKindPolicy` flags as
currency-invalidated (superseded predecessor); the policy table is the single
declaration site.
"""


_COST_SUBTREE_SQL: Final[str] = (
    "WITH RECURSIVE subtree(id) AS ("
    "    SELECT $1::uuid "
    "    UNION "
    "    SELECT e.from_id FROM edges e "
    "    JOIN subtree s ON s.id = e.to_id "
    "    WHERE e.edge_kind = 'narrows'"
    ") "
    "SELECT "
    "    COALESCE(SUM(t.marginal_cost_agent_usd), 0)    AS agent_usd, "
    "    COALESCE(SUM(t.marginal_cost_resource_usd), 0) AS resource_usd "
    "FROM inquiries t WHERE t.id IN (SELECT id FROM subtree)"
)
"""Decomposition-rollup of ``marginal_cost_*_usd`` from the subtree
rooted at ``$1``. Walks ``narrows`` edges downward (broader -> narrower via the
stored narrower -> broader edge's from-side)."""
