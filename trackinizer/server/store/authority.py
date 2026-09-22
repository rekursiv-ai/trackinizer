""":class:`_AuthorityMixin` -- the load-bearing (PageRank) authority sweep.

:meth:`recompute_authority` ranks each citation-relation graph
(:data:`AUTHORITY_RELATIONS`) with :func:`trackinizer.types.authority.pagerank`
and writes the score back to that relation's column on ``inquiries``. Global by
nature -- one edge changing any relation can shift every score in it -- so it is
a full recompute run off the request path by a periodic sweep, not an
incremental per-write update.

A read-only-until-the-final-write leaf like :class:`_ExportMixin`: it reads
edges and inquiry ids through ``self.engine`` and writes only the derived
authority columns, calling no other mixin.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from trackinizer.server.notify import tx
from trackinizer.server.store.shared import _StoreShared
from trackinizer.server.values import vetted_sql
from trackinizer.types.authority import Edge, pagerank, relation_edges


if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from trackinizer.lib.postgres import Conn


__all__ = ["AUTHORITY_RELATIONS", "AuthorityRelation", "_AuthorityMixin"]


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthorityRelation:
    """One citation-relation authority graph and where its score is stored.

    Attributes:
      column: The ``inquiries`` column the score is written to.
      edge_kind: The stored ``edges.edge_kind`` whose graph is ranked.
      weighted: Rank by ``abs(valence)`` (citations) rather than unit weight
        (the valence-free ``cites_paper`` bibliography).

    """

    column: str
    edge_kind: str
    weighted: bool


AUTHORITY_RELATIONS: Final[tuple[AuthorityRelation, ...]] = (
    AuthorityRelation(column="proves_authority", edge_kind="proves", weighted=True),
    AuthorityRelation(column="favors_authority", edge_kind="favors", weighted=True),
    AuthorityRelation(
        column="cited_by_authority",
        edge_kind="cites_paper",
        weighted=False,
    ),
    # ``issue`` load-bearing spans BOTH Issue-to-Issue relations: a prerequisite
    # (requires) and a parent decomposition (narrows) are each depended upon.
    AuthorityRelation(column="issue_authority", edge_kind="requires", weighted=False),
    AuthorityRelation(column="issue_authority", edge_kind="narrows", weighted=False),
)
"""Every authority graph, ``(column, edge_kind, weighted)``. Two rows share
``issue_authority`` -- its graph is the union of ``requires`` and ``narrows``
edges -- so the sweep accumulates them before ranking."""


class _AuthorityMixin(_StoreShared):
    """The load-bearing authority sweep for :class:`Store`."""

    async def recompute_authority(self, *, conn: Conn | None = None) -> int:
        """Recompute every authority column from the current edge graph.

        Ranks each relation in :data:`AUTHORITY_RELATIONS` and writes the score
        to its column, in one transaction so a reader never sees a half-written
        ranking. Nodes with no edge in a relation are reset to NULL, so a score
        exists only where the graph actually reaches.

        Args:
          conn: Existing connection to reuse, or None to acquire one. PGlite's
            single connection deadlocks on a re-entrant acquire.

        Returns:
          written: Total (node, column) scores written across all relations.

        """
        if conn is not None:
            return await self._recompute(conn)
        async with self.engine.acquire() as new_conn:
            return await self._recompute(new_conn)

    async def _recompute(self, conn: Conn) -> int:
        """Rank each relation and write its column inside one transaction."""
        # Accumulate edge rows per TARGET column first, so the two Issue
        # relations union into one ``issue_authority`` graph before ranking.
        rows_by_column: dict[str, list[dict[str, object]]] = {}
        weighted_by_column: dict[str, bool] = {}
        for relation in AUTHORITY_RELATIONS:
            rows = await conn.fetch(
                "SELECT from_id, to_id, valence FROM edges WHERE edge_kind = $1",
                relation.edge_kind,
            )
            rows_by_column.setdefault(relation.column, []).extend(
                {"from_id": r["from_id"], "to_id": r["to_id"], "valence": r["valence"]}
                for r in rows
            )
            weighted_by_column[relation.column] = relation.weighted
        written = 0
        async with tx(conn):
            for column, rows in rows_by_column.items():
                edges = relation_edges(rows, weighted=weighted_by_column[column])
                # Rank only the relation's OWN participating nodes, not every
                # inquiry: teleport mass spread over the whole table drove a
                # cited node's score toward zero as unrelated rows accrued.
                nodes = list(
                    {edge.dependent for edge in edges}
                    | {edge.dependency for edge in edges},
                )
                scores = pagerank(nodes, edges)
                written += await _write_scores(conn, column, edges, scores)
        return written


# Only nodes that are the dependency (target) of at least one edge carry a meaningful
# score; every other node has no authority in this relation and its column reads NULL.
#
# The reset is scoped to rows that currently hold a score (``column IS NOT NULL``)
# rather than the whole table: a node that lost its last inbound edge since the previous
# sweep still had a score to clear, so this catches it without rewriting every unrelated
# row each sweep.
async def _write_scores(
    conn: Conn,
    column: str,
    edges: Sequence[Edge],
    scores: dict[UUID, float],
) -> int:
    """Write one relation's scores; NULL every node its graph never reaches."""
    reached = {edge.dependency for edge in edges}
    await conn.execute(
        vetted_sql(
            "UPDATE inquiries SET ",
            column,
            " = NULL WHERE ",
            column,
            " IS NOT NULL",
        ),
    )
    if not reached:
        return 0
    ids = list(reached)
    values = [scores[n] for n in ids]
    await conn.execute(
        vetted_sql(
            "UPDATE inquiries AS i SET ",
            column,
            " = v.score FROM (SELECT unnest($1::uuid[]) AS id, "
            "unnest($2::double precision[]) AS score) AS v WHERE i.id = v.id",
        ),
        ids,
        values,
    )
    return len(ids)
