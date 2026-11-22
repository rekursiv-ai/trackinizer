"""The inquiries nearest some foci: the neighbourhood ``/api/web/graph?focus=`` draws.

A breadth-first walk over ``edges`` in both directions, each hop read through the
edge indexes on ``from_id`` and ``to_id`` and the inquiries' primary key. One
query reads the foci; each hop is one query, but hop 1 from several foci is one
per focus for its share and one more over them all for what is left. A hop that
fills ``limit`` ends the walk, so every hop but the last is taken whole, and a
node's distance is its shortest.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import UUID

from treekle import from_plain

from trackinizer.server.access_policy import inquiry_read_filter
from trackinizer.server.values import vetted_sql


if TYPE_CHECKING:
    from collections.abc import Sequence

    from trackinizer.lib.postgres import Conn
    from trackinizer.server.access_policy import AccessPolicy, Principal


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
    ") AS n WHERE n.id <> ALL($2::uuid[]){readable}"
    " ORDER BY (SELECT created FROM inquiries WHERE id = n.id) DESC, n.id DESC"
    " LIMIT $3)"
)


async def read_neighbourhood(
    conn: Conn,
    *,
    focus: Sequence[UUID],
    hops: int,
    limit: int,
    policy: AccessPolicy | None = None,
    principal: Principal | None = None,
) -> dict[UUID, int]:
    """Return the inquiries nearest each of ``focus``, each with its hop distance.

    Every focus the reader may read comes first, at 0, in the order given. Then
    every inquiry one edge away, in either direction, before any two away, and so
    on to ``hops``; within a hop the newest come first, a tie in ``created`` going
    to the larger id. The walk stops at ``limit`` inquiries, the foci included.

    With several foci, hop 1 first gives each in turn at most an even share of the
    room still left, so a focus with many newer neighbours cannot crowd out the
    others and what one leaves unused passes to those after it; the newest of the
    rest then fill what is left, as every later hop does.

    Args:
      conn: The connection to read on.
      focus: The inquiries to walk from.
      hops: The most edges to walk, at least 1.
      limit: The most inquiries to return, at least 1.
      policy: Deployment policy for each traversal hop.
      principal: Request reader; None preserves trusted reads.

    Returns:
      distances: Each inquiry's id and its hop distance, nearest first; empty
        when no focus names an inquiry the reader may read.

    """
    foci = await _readable(conn, focus=focus, policy=policy, principal=principal)
    distances = dict.fromkeys(foci[:limit], 0)
    frontier = list(distances)
    for hop in range(1, hops + 1):
        if not frontier:
            break
        found = (
            await _shares(
                conn,
                foci=frontier,
                distances=distances,
                limit=limit,
                policy=policy,
                principal=principal,
            )
            if hop == 1 and len(frontier) > 1
            else []
        )
        if (room := limit - len(distances)) > 0:
            found += await _next_hop(
                conn,
                frontier=frontier,
                seen=distances,
                room=room,
                policy=policy,
                principal=principal,
            )
            distances |= dict.fromkeys(found, hop)
        frontier = found
    return distances


async def _readable(
    conn: Conn,
    *,
    focus: Sequence[UUID],
    policy: AccessPolicy | None,
    principal: Principal | None,
) -> list[UUID]:
    """Return each of ``focus`` the reader may read, once, in the order given."""
    params: list[object] = [list(focus)]
    predicate = inquiry_read_filter(
        "inquiries.id",
        policy=policy,
        principal=principal,
        params=params,
    )
    readable = from_plain(
        await conn.fetchval(
            vetted_sql(
                "SELECT ARRAY(SELECT id FROM inquiries WHERE id = ANY($1::uuid[])",
                "" if predicate == "TRUE" else f" AND ({predicate})",
                ")",
            ),
            *params,
        ),
        list[UUID],
    )
    return [each for each in dict.fromkeys(focus) if each in readable]


async def _shares(
    conn: Conn,
    *,
    foci: list[UUID],
    distances: dict[UUID, int],
    limit: int,
    policy: AccessPolicy | None,
    principal: Principal | None,
) -> list[UUID]:
    """Take each focus's newest neighbours into hop 1, an even share of what is left."""
    taken: list[UUID] = []
    for at, each in enumerate(foci):
        share = -(-(limit - len(distances)) // (len(foci) - at))
        if share > 0:
            found = await _next_hop(
                conn,
                frontier=[each],
                seen=distances,
                room=share,
                policy=policy,
                principal=principal,
            )
            # The caller's ``distances`` gains each as it is taken, so no later focus
            # takes it again, each share is of what is truly left, and so is the room
            # ``read_neighbourhood`` tops up after.
            distances |= dict.fromkeys(found, 1)
            taken += found
    return taken


async def _next_hop(
    conn: Conn,
    *,
    frontier: list[UUID],
    seen: dict[UUID, int],
    room: int,
    policy: AccessPolicy | None,
    principal: Principal | None,
) -> list[UUID]:
    """Return the newest ``room`` neighbours of ``frontier`` not in ``seen``."""
    params: list[object] = [frontier, list(seen), room]
    predicate = inquiry_read_filter(
        "n.id",
        policy=policy,
        principal=principal,
        params=params,
    )
    query = _NEXT_HOP.format(
        readable="" if predicate == "TRUE" else f" AND ({predicate})",
    )
    return from_plain(await conn.fetchval(vetted_sql(query), *params), list[UUID])
