"""The inquiries nearest one inquiry: the neighbourhood ``/api/web/graph?focus=`` draws.

A breadth-first walk over ``edges`` in both directions, one query per hop, each
read through the edge indexes on ``from_id`` and ``to_id`` and the inquiries'
primary key. A hop that fills ``limit`` ends the walk, so every hop but the last
is taken whole, and a node's distance is its shortest.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import UUID

from trackinizer.lib.custom_json import convert


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn


__all__ = ["read_neighbourhood"]


# ``edges`` holds no ``created``, so a hop reads every edge of its frontier and looks
# up each neighbour's ``created`` by id before it can keep the newest. The frontier is
# the last hop, at most ``limit`` nodes, which is what bounds a hub's next hop.
#
# The lookup is a subquery, not a join, so the plan cannot change. Joined, a small
# ``LIMIT`` let the planner walk ``inquiries`` newest first instead, testing each row
# against every neighbour: on 58 hubs whose 17,400 neighbours were all old, one hop
# took 17 s, against 14 ms looked up by id.
_NEXT_HOP: Final = (
    "SELECT ARRAY(SELECT n.id FROM ("
    " SELECT to_id AS id FROM edges WHERE from_id = ANY($1::uuid[])"
    " UNION SELECT from_id FROM edges WHERE to_id = ANY($1::uuid[])"
    ") AS n WHERE n.id <> ALL($2::uuid[])"
    " ORDER BY (SELECT created FROM inquiries WHERE id = n.id) DESC, n.id DESC"
    " LIMIT $3)"
)


async def read_neighbourhood(
    conn: Conn,
    focus: UUID,
    *,
    hops: int,
    limit: int,
) -> dict[UUID, int]:
    """Return the inquiries nearest ``focus``, each with its hop distance.

    The focus comes first, at 0. Then every inquiry one edge away, in either
    direction, before any two away, and so on to ``hops``; within a hop the
    newest come first, a tie in ``created`` going to the larger id. The walk
    stops at ``limit`` inquiries, the focus included.

    Args:
      conn: The connection to read on.
      focus: The inquiry to walk from.
      hops: The most edges to walk, at least 1.
      limit: The most inquiries to return, at least 1.

    Returns:
      distances: Each inquiry's id and its hop distance, nearest first; empty
        when ``focus`` names no inquiry.

    """
    if not await conn.fetchval(
        "SELECT EXISTS (SELECT FROM inquiries WHERE id = $1)",
        focus,
    ):
        return {}
    distances: dict[UUID, int] = {focus: 0}
    frontier = [focus]
    for hop in range(1, hops + 1):
        room = limit - len(distances)
        if room < 1 or not frontier:
            break
        frontier = convert(
            await conn.fetchval(_NEXT_HOP, frontier, list(distances), room),
            list[UUID],
        )
        distances |= dict.fromkeys(frontier, hop)
    return distances
