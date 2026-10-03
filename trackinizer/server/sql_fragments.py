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
    "CLAIM_NEXT_ISSUE_SQL",
    "COST_SUBTREE_SQL",
    "NARROWS_ANCESTORS_SQL",
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


# One predicate, two readers: the read-only preview (``NEXT_ISSUE_SQL``) and the
# atomic acquisition (``CLAIM_NEXT_ISSUE_SQL``) must agree on what "available"
# means, or an agent can be shown work it then cannot claim.
#
# ``owner IS NULL`` is what makes the queue a queue: ``trax/SKILL.md`` defines an
# active row with no owner as open and one with an owner as in progress, but the
# scheduler used to ignore ownership entirely and hand the same issue to every
# caller.
#
# A prerequisite blocks only while it is ``active``. Terminal is not the same as
# complete -- ``abandoned`` and ``invalid`` also unblock -- so this deliberately
# tests ``status = 'active'`` rather than ``status = 'complete'``.
_ELIGIBLE_ISSUE_PREDICATE: Final[str] = vetted_sql(
    "issue.kind = 'Issue' AND issue.status = 'active' "
    "  AND issue.owner IS NULL "
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
)
"""What makes an Issue available to work on. Shared by preview and acquisition."""

# Lowest ``issue_priority`` first, then oldest. A NULL priority sorts last under
# Postgres' default ASC NULL ordering, which keeps unprioritised work behind
# anything explicitly ranked.
_ELIGIBLE_ISSUE_ORDER: Final[str] = " ORDER BY issue.issue_priority, issue.created "


NEXT_ISSUE_SQL: Final[str] = vetted_sql(
    "SELECT issue.* FROM inquiries issue WHERE ",
    _ELIGIBLE_ISSUE_PREDICATE,
    _ELIGIBLE_ISSUE_ORDER,
    " LIMIT 1",
)
"""Preview the next available Issue WITHOUT reserving it.

Read-only: two callers racing this both see the same row, which is why
acquisition goes through :data:`CLAIM_NEXT_ISSUE_SQL` instead.
"""


CLAIM_NEXT_ISSUE_SQL: Final[str] = vetted_sql(
    # Select and claim in ONE statement. Splitting it into a read followed by an
    # owner write leaves a window -- seconds wide, with an agent deciding in
    # between -- where every caller sees the same unowned row and each overwrites
    # the last, so N agents duplicate one issue while the rest go untouched.
    #
    # ``SKIP LOCKED`` is what makes concurrent callers diverge rather than
    # collide: a row another transaction has locked is passed over, so the second
    # caller takes the next eligible issue instead of contending for this one.
    # ``OF issue`` scopes the lock to the inquiries row being claimed.
    "UPDATE inquiries SET owner = $1 WHERE id = ("
    "  SELECT issue.id FROM inquiries issue WHERE ",
    _ELIGIBLE_ISSUE_PREDICATE,
    _ELIGIBLE_ISSUE_ORDER,
    "  LIMIT 1 FOR UPDATE OF issue SKIP LOCKED) RETURNING *",
)
"""Atomically select and claim one available Issue for ``$1``.

Returns the claimed row, or no row when nothing is available -- which means
"nothing claimable right now", not "all work is finished": eligible rows may
simply be locked by another in-flight claim.
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


PROVING_EDGES_SQL: Final[str] = vetted_sql(
    # Same currency rule as PROVES_BELIEF_SQL, but returns the edge (citer id,
    # kind, and signed valence) rather than the full row -- all confidence_for
    # needs, one hop at a time. Signed valence: a disproof (< 0) lowers, a
    # proof (> 0) lifts.
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
"""Currently-true ``proves`` edges pointing at ``$1``, one hop, with valence.

``confidence_for`` folds each into a log-odds sum, recursing into any citer that
is itself a Belief/Experiment so a chain resolves bottom-up (the ``proves``
graph is a DAG). Drops citers an :class:`EdgeKindPolicy` flags currency-invalid.
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


NARROWS_ANCESTORS_SQL: Final[str] = (
    # The walk carries its depth because ``UNION`` alone cannot end it here: a
    # cycle keeps yielding new (child, parent, depth) rows, and the depth cap is
    # what stops them.
    "WITH RECURSIVE up(child, parent, depth) AS ("
    "    SELECT e.from_id, e.to_id, 1 FROM edges e "
    "    WHERE e.edge_kind = 'narrows' AND e.from_id = ANY($1::uuid[]) "
    "    UNION "
    "    SELECT e.from_id, e.to_id, up.depth + 1 FROM edges e "
    "    JOIN up ON e.from_id = up.parent "
    "    WHERE e.edge_kind = 'narrows' AND up.depth < $2"
    "), kept AS ("
    "    SELECT parent FROM up GROUP BY parent "
    "    ORDER BY min(depth), parent LIMIT $3"
    ") "
    "SELECT DISTINCT up.child, t.id, t.kind, t.seq, t.title, t.status "
    "FROM up JOIN kept ON kept.parent = up.parent "
    "JOIN inquiries t ON t.id = up.parent"
)
"""The ``narrows`` edges above the rows ``$1``, at most ``$2`` levels up.

Walks ``narrows`` upward (narrower -> broader, the stored from -> to), the
reverse of :data:`COST_SUBTREE_SQL`. Keeps at most ``$3`` ancestors, nearest
first, and returns every edge reaching one: its ``child`` and the ancestor's
light row. ``Store.narrows_ancestors`` assigns them to each row."""
