"""PageRank over a citation-relation graph: derived load-bearing authority.

A node is load-bearing to the degree that important nodes depend on it. That is
PageRank: authority flows backward along "depends on" edges and settles at a
fixed point, so a node cited by strongly-cited nodes ranks high. Unlike
:mod:`trackinizer.types.belief_confidence` (a one-pass DAG fold), PageRank is a
global mutual fixed point and must be iterated to convergence.

The compute here is pure Python over id-keyed dicts: one authority graph is at
most every edge of one relation, iterated a few dozen times -- linear in edges,
seconds at millions, and run off the request path by the authority sweep. Each
relation (``proves``, ``favors``, ``cited_by``, ``issue``) is ranked
separately, so "load-bearing as proof" never mixes with "as bibliography".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


__all__ = ["Edge", "pagerank", "relation_edges"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Edge:
    """One weighted dependency edge ``dependent -> dependency``.

    Authority flows from ``dependent`` to ``dependency`` (the dependency is
    load-bearing FOR the dependent), so PageRank ranks a node by the weighted
    authority of everything that depends on it.

    Attributes:
      dependent: The node that leans on ``dependency``.
      dependency: The load-bearing node receiving authority.
      weight: Non-negative edge weight (``abs(valence)``; 1.0 when unweighted).

    """

    dependent: UUID
    dependency: UUID
    weight: float


def pagerank(
    nodes: Sequence[UUID],
    edges: Sequence[Edge],
    *,
    damping: float = 0.85,
    max_iterations: int = 100,
    tolerance: float = 1e-9,
) -> dict[UUID, float]:
    """Return each node's PageRank authority, summing to 1 over ``nodes``.

    Power iteration on the weighted graph until the L1 change falls below
    ``tolerance`` or ``max_iterations`` passes elapse. A node with no outgoing
    dependency (a sink) redistributes its mass uniformly, so the vector stays a
    probability distribution and rank is comparable across nodes.

    Args:
      nodes: Every node to rank. Determines the vector's support and the
        uniform teleport / sink-redistribution mass.
      edges: Weighted ``dependent -> dependency`` edges; endpoints outside
        ``nodes`` are ignored.
      damping: Follow-edge probability; the complement teleports uniformly.
        Defaults to Bring & Page's original 0.85.
      max_iterations: Hard cap on power-iteration passes; convergence normally
        stops the loop far sooner.
      tolerance: L1 change between successive score vectors below which
        iteration stops.

    Returns:
      authority: ``node -> score`` for every id in ``nodes``, summing to 1.
        Empty when ``nodes`` is empty.

    """
    count = len(nodes)
    if count == 0:
        return {}
    node_set = set(nodes)
    out_weight: dict[UUID, float] = dict.fromkeys(nodes, 0.0)
    inbound: dict[UUID, list[tuple[UUID, float]]] = {n: [] for n in nodes}
    for edge in edges:
        if (
            edge.dependent not in node_set
            or edge.dependency not in node_set
            or edge.weight <= 0.0
        ):
            continue
        out_weight[edge.dependent] += edge.weight
        inbound[edge.dependency].append((edge.dependent, edge.weight))
    teleport = (1.0 - damping) / count
    scores: dict[UUID, float] = dict.fromkeys(nodes, 1.0 / count)
    for _ in range(max_iterations):
        # A sink (no outgoing weight) would leak probability; gather its mass
        # and redistribute it uniformly so the vector stays normalized.
        sink_mass = sum(scores[n] for n in nodes if out_weight[n] == 0.0)
        shared_sink = damping * sink_mass / count
        nxt: dict[UUID, float] = {}
        for node in nodes:
            flowed = sum(
                scores[src] * weight / out_weight[src] for src, weight in inbound[node]
            )
            nxt[node] = teleport + shared_sink + damping * flowed
        delta = sum(abs(nxt[n] - scores[n]) for n in nodes)
        scores = nxt
        if delta < tolerance:
            break
    return scores


def relation_edges(
    rows: Sequence[Mapping[str, object]],
    *,
    weighted: bool,
) -> list[Edge]:
    """Build authority edges from ``(from_id, to_id, valence)`` rows.

    An edge means ``from_id`` (the younger citing/dependent node) leans on
    ``to_id`` (the older cited/dependency node), so authority flows
    ``from_id -> to_id``. Direction and load-bearing sign are magnitude-only:
    a disproof (valence < 0) is as load-bearing as a proof.

    Args:
      rows: Edge rows, each with ``from_id``, ``to_id``, and (when
        ``weighted``) a ``valence``.
      weighted: Use ``abs(valence)`` as the edge weight; ``False`` weights every
        edge 1.0 (the unweighted ``cites_paper`` graph, which carries no
        valence).

    Returns:
      edges: One :class:`Edge` per row, ``dependent -> dependency``.

    """
    built: list[Edge] = []
    for row in rows:
        weight = abs(_as_float(row["valence"])) if weighted else 1.0
        built.append(
            Edge(
                dependent=_as_uuid(row["from_id"]),
                dependency=_as_uuid(row["to_id"]),
                weight=weight,
            ),
        )
    return built


def _as_float(value: object) -> float:
    """Coerce a DB-decoded numeric to float."""
    if isinstance(value, (int, float)):
        return float(value)
    raise TypeError(f"expected a numeric valence, got {type(value).__name__}")


def _as_uuid(value: object) -> UUID:
    """Narrow a DB-decoded id to UUID."""
    if isinstance(value, UUID):
        return value
    raise TypeError(f"expected a UUID id, got {type(value).__name__}")
