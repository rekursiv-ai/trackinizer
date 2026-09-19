"""SQL fragments derived from the EdgeKindPolicy registry.

Every edge-walking query (cascade, next_issue, proves_belief) used to
hardcode specific edge kinds (the supersession / refutation / decomposition
branches) directly. Those branches now derive from
:data:`~trackinizer.types.edges.EDGE_POLICIES` via
the helpers below -- one table change adds a new edge kind everywhere
consistently.
"""

from __future__ import annotations

from typing import Final, Literal

from trackinizer.server.values import vetted_sql
from trackinizer.types.edges import EDGE_POLICIES


__all__ = [
    "COST_SUBTREE_SQL",
    "NEXT_ISSUE_SQL",
    "PROVES_BELIEF_SQL",
    "PROVING_EDGES_SQL",
]


# Only consumed by trusted-internal SQL builders below; the strings come from
# :data:`Edge.Kind`, a closed set.
# Used by the ``next_issue`` and ``proves_belief`` queries to drop endpoints that an
# edge marks as scheduler-excluded or currency-invalidated. ``subject_alias`` is the
# inquiry alias the NOT EXISTS subquery joins against (``issue.id`` for next_issue,
# ``t.id`` for proves_belief).
def _policy_exclude_clauses(
    *,
    subject_alias: str,
    policy_attr: Literal["skips_scheduler_on", "invalidates_currency_on"],
) -> str:
    """Render an ``AND NOT EXISTS`` clause per policy that sets ``policy_attr``."""
    sides: dict[Literal["from", "to"], list[str]] = {"from": [], "to": []}
    for kind, policy in EDGE_POLICIES.items():
        side = (
            policy.skips_scheduler_on
            if policy_attr == "skips_scheduler_on"
            else policy.invalidates_currency_on
        )
        if side is None:
            continue
        sides[side].append(kind)
    clauses: list[str] = []
    for side, kinds in sides.items():
        if not kinds:
            continue
        column = "from_id" if side == "from" else "to_id"
        kindset = ", ".join(f"'{kind}'" for kind in sorted(kinds))
        clauses.append(
            vetted_sql(
                "AND NOT EXISTS (SELECT 1 FROM edges p WHERE p.",
                column,
                " = ",
                subject_alias,
                " AND p.edge_kind IN (",
                kindset,
                "))",
            ),
        )
    return " ".join(clauses)


NEXT_ISSUE_SQL: Final[str] = vetted_sql(
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


PROVES_BELIEF_SQL: Final[str] = vetted_sql(
    # Proves is stored Artifact(from) -> Belief(to), so the artifacts proving
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
        subject_alias="t.id",
        policy_attr="invalidates_currency_on",
    ),
    " ORDER BY t.created",
)
"""Currently-true ``proves``-citations a Belief depends on.

Drops any Artifact that an :class:`EdgeKindPolicy` flags as
currency-invalidated (superseded predecessor); the policy table is the single
declaration site.
"""


COST_SUBTREE_SQL: Final[str] = (
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


PROVING_EDGES_SQL: Final[str] = vetted_sql(
    # Same shape and currency rule as PROVES_BELIEF_SQL, but returns the edge
    # (citer id/kind + valence) instead of the full citing row -- all
    # strength_for needs, one hop at a time.
    "SELECT e.from_id, t.kind AS from_kind, e.valence FROM edges e "
    "JOIN inquiries t ON t.id = e.from_id "
    "WHERE e.edge_kind = 'proves' AND e.to_id = $1 "
    "  AND ("
    "    (t.kind = 'Belief' AND t.belief_judgement = 'proven') "
    "    OR (t.kind = 'Experiment' AND t.status = 'complete') "
    "    OR (t.kind NOT IN ('Belief', 'Experiment') AND t.status = 'active')"
    "  ) ",
    _policy_exclude_clauses(
        subject_alias="t.id",
        policy_attr="invalidates_currency_on",
    ),
)
"""Currently-true ``proves`` edges pointing at ``$1``, one hop.

``strength_for`` walks this recursively for each citer that is itself
claimable (Belief/Experiment, since only those kinds can carry inbound
``proves``), so a chain of Beliefs citing Beliefs resolves bottom-up.
"""
