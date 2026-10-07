"""Seed a server with a graph's structure, giving each node invented contents.

``graph_structure.json`` holds the shape of a real research graph alone
(``graph_structure.py`` writes it): each node's kind and status in creation
order, and each edge's ends, kind and valence sign. This gives every node a
title of its kind and an ``example.com`` owner, a Belief the judgement and
confidence its evidence implies, and a valenced edge a weight, then writes them
through the batch route oldest first, so the server's creation order is the
structure's.

Only the text the screenshots and the demo show is written by hand, in
``graph_contents.json``: the roots' titles, which label the grouped graph's
islands and fill its roots list, and the showcase, the island the demo opens
and the Belief in it that it selects, with descriptions and labels. Every other
title is built from that file's word lists for its kind, the nodes of a kind taking
its titles in turn. Every choice follows from a node's index, so every seed writes
the same.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

import functools
import hashlib
import itertools
import math
import string
import uuid

from trackinizer.lib.codec import from_plain, loads
from trackinizer.wire.bodies import BATCH_MAX_ITEMS


_CWD: Final = Path(__file__).resolve().parent

if TYPE_CHECKING:
    from collections.abc import Sequence

    from trackinizer.types.inquiries import Inquiry
    from trackinizer.web.scripts.graph_structure import Link, Structure
    from trackinizer.wire.wire_sessions import SessionEnd


GRAPH_CONTENTS: Final = _CWD / "graph_contents.json"
"""The hand-written text: roots' titles, newest first, the showcase, word lists."""


class GraphClient(Protocol):
    """The writes ``seed_graph`` makes, as :class:`Client` makes them."""

    def submit_batch(
        self,
        items: Sequence[tuple[Inquiry.InquiryKind, Mapping[str, object]]],
        *,
        edges: Sequence[Mapping[str, object]] = (),
        actor: str,
    ) -> list[uuid.UUID]:
        """Create ``items`` and ``edges`` in one batch; return their ids."""
        ...

    def session_end(
        self,
        session_id: uuid.UUID,
        body: SessionEnd | None = None,
    ) -> object:
        """End the session ``session_id``."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class Islands:
    """A structure's roots and the island each node sits in, as the graph finds them.

    A root is an Issue that narrows nothing, with something under it: what
    narrows it or was produced by it, and so on down. A node's island is the
    nearest root above it, the newest of those as near; a node under no root has
    none. ``src/graph/roots.ts`` is the rule this mirrors.
    """

    roots: tuple[int, ...]
    """The roots' node indexes, newest first."""

    home: Mapping[int, int]
    """Each rooted node's island, by node index."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Showcase:
    """The island the demo opens and the Belief in it that it selects, by node index."""

    root: int
    belief: int


@dataclass(frozen=True, slots=True, kw_only=True)
class SeededGraph:
    """What ``seed_graph`` wrote: each node's id by index, and the showcase's ids."""

    ids: tuple[uuid.UUID, ...]
    root: uuid.UUID
    belief: uuid.UUID


def islands(structure: Structure) -> Islands:
    """Find ``structure``'s roots and each node's island.

    Args:
      structure: The nodes and edges to group.

    Returns:
      islands: The roots, newest first, and the island each rooted node sits in.

    """
    below: dict[int, list[int]] = {}
    narrowing: set[int] = set()
    for link in structure.edges:
        if link.kind in {"narrows", "produced_by"}:
            below.setdefault(link.to_index, []).append(link.from_index)
        if link.kind == "narrows":
            narrowing.add(link.from_index)
    roots = tuple(
        n
        for n in reversed(range(len(structure.nodes)))
        if structure.nodes[n].kind == "Issue" and n not in narrowing and n in below
    )
    nearest: dict[int, tuple[int, int]] = {}
    for root in roots:
        # Breadth first, so each node's first count is its fewest hops.
        hops = {root: 0}
        walk = [root]
        for node in walk:
            for child in below.get(node, ()):
                if child not in hops:
                    hops[child] = hops[node] + 1
                    walk.append(child)
        for node, depth in hops.items():
            if node not in nearest or depth < nearest[node][0]:
                nearest[node] = (depth, root)
    return Islands(
        roots=roots,
        home={node: root for node, (_, root) in nearest.items()},
    )


def showcase(structure: Structure, found: Islands) -> Showcase:
    """Pick the island the demo opens, and the Belief in it that it selects.

    The island is the one of at most 100 nodes whose Beliefs its own edges cite
    with the most valences, one of them against, so its evidence draws in both
    colours and it frames large; the Belief is the one in it cited most.

    Args:
      structure: The nodes and edges.
      found: Its islands, as ``islands`` finds them.

    Returns:
      showcase: The island's root and the Belief, by node index.

    Raises:
      ValueError: No island has evidence against a Belief in it.

    """
    sizes = Counter(found.home.values())
    cited: Counter[tuple[int, int]] = Counter()
    against: set[int] = set()
    for link in structure.edges:
        home = found.home.get(link.to_index)
        if (
            link.sign
            and structure.nodes[link.to_index].kind == "Belief"
            and home is not None
            and home == found.home.get(link.from_index)
        ):
            cited[home, link.to_index] += 1
            if link.sign < 0:
                against.add(home)
    weight = Counter[int]()
    for (home, _), count in cited.items():
        weight[home] += count
    candidates = [r for r in against if sizes[r] <= 100]
    if not candidates:
        raise ValueError("no island has evidence against a Belief in it")
    root = max(candidates, key=lambda r: (weight[r], r))
    belief = max((count, n) for (home, n), count in cited.items() if home == root)
    return Showcase(root=root, belief=belief[1])


def seed_graph(
    client: GraphClient,
    structure: Structure,
    *,
    actor: str,
) -> SeededGraph:
    """Write ``structure``'s nodes oldest first, and its edges, through ``client``.

    Args:
      client: A client of the server to seed.
      structure: The nodes and edges to write.
      actor: Who the change log says wrote them.

    Returns:
      graph: Each node's id, by its index, and the showcase's.

    """
    found = islands(structure)
    picked = showcase(structure, found)
    ids = _write(client, structure, _bodies(structure, found, picked), actor=actor)
    return SeededGraph(ids=tuple(ids), root=ids[picked.root], belief=ids[picked.belief])


def cascade_order(structure: Structure) -> list[Link]:
    """Order ``structure``'s edges so that adding them walks the change cascade little.

    The server walks the cascade from each new edge's ``from`` end: along every
    edge stored, a change alerts the end its kind names dependent (its
    ``EdgeKindPolicy.cascade_dependent``), then that end's, and so on, a
    change-log row a step. Between Issues the alerts run both ways: a
    ``narrows`` alerts the parent, and the ``produced_by`` beside it, which the
    server adds if absent, alerts the child; so a walk from an Issue reaches its
    whole tree as built so far. In creation order the committed structure walks
    over ten times as far as in this order, which takes:

    1. An Issue's edges to other kinds, while its tree is only itself.
    2. The edges between Issues, a parent's at a time, the youngest parent first,
       so a parent gathers its children before it joins its own parent; and of
       its children the smallest subtree first, so each new child's walk passes
       the parent's small branches, not its large ones.
    3. Every other edge: Experiments, Beliefs and the rest walk only to what
       they cite.

    Args:
      structure: The nodes and edges to order.

    Returns:
      edges: ``structure``'s edges, each once, in the order to add them.

    """
    issue = [node.kind == "Issue" for node in structure.nodes]
    children: dict[int, set[int]] = {}
    for link in structure.edges:
        if issue[link.from_index] and issue[link.to_index]:
            children.setdefault(link.to_index, set()).add(link.from_index)
    # Children are younger than their parent, so the youngest first counts each
    # child's subtree before its parent's.
    size = [1] * len(structure.nodes)
    for n in reversed(range(len(structure.nodes))):
        size[n] += sum(size[child] for child in children.get(n, ()) if child > n)
    return sorted(
        structure.edges,
        key=functools.partial(_cascade_key, issue=issue, size=size),
    )


_PEOPLE: Final = ("ada", "ben", "cleo", "dee", "eli", "fay", "gus")
_AGENTS: Final = (
    "sweep-runner",
    "eval-bot",
    "lit-review",
    "profiler",
    "repro",
    "triage",
)


# The batch route adds edges only beside a node it creates. One transaction per batch
# keeps a PGlite server quick, where one per edge took 0.2 s each, so the newest nodes
# are held back to carry the edges, a batch each.
def _write(
    client: GraphClient,
    structure: Structure,
    bodies: Sequence[Mapping[str, object]],
    *,
    actor: str,
) -> list[uuid.UUID]:
    """Create the nodes oldest first and the edges in ``cascade_order``."""
    pending = cascade_order(structure)
    first = _first_carrier(len(structure.nodes), pending)
    ids: list[uuid.UUID] = []
    for chunk in itertools.batched(range(first), BATCH_MAX_ITEMS):
        ids.extend(
            client.submit_batch(
                [(structure.nodes[n].kind, bodies[n]) for n in chunk],
                actor=actor,
            ),
        )
    _end_sessions(client, structure, ids)
    for carrier in range(first, len(structure.nodes)):
        batch = [
            link for link in pending if max(link.from_index, link.to_index) <= carrier
        ][:BATCH_MAX_ITEMS]
        taken = set(batch)
        pending = [link for link in pending if link not in taken]
        ids.extend(
            client.submit_batch(
                [(structure.nodes[carrier].kind, bodies[carrier])],
                edges=[_edge(link, carrier=carrier, ids=ids) for link in batch],
                actor=actor,
            ),
        )
        _end_sessions(client, structure, ids, start=carrier)
    if pending:
        raise ValueError(f"{len(pending)} edges found no batch")
    return ids


# A carrier takes up to ``BATCH_MAX_ITEMS`` edges whose ends are no newer than it.
# Filling carriers oldest first with any edges they can take then lands every edge
# exactly when, for each node, the edges whose newer end is that node or newer fit the
# carriers from there on.
def _first_carrier(nodes: int, edges: Sequence[Link]) -> int:
    """Return the oldest of the fewest newest nodes that can carry ``edges``."""
    first = nodes - math.ceil(len(edges) / BATCH_MAX_ITEMS)
    newest = Counter(max(link.from_index, link.to_index) for link in edges)
    later = 0
    for n in reversed(range(nodes)):
        later += newest[n]
        if later > BATCH_MAX_ITEMS * (nodes - n):
            raise ValueError(
                f"{len(edges)} edges found no batch: {later} end at node {n} or "
                f"newer, past what {nodes - n} carriers of {BATCH_MAX_ITEMS} hold",
            )
    return first


# A complete session is created live, as every session is, and its end completes it.
# Ended before its edges are added, its end alerts nothing.
def _end_sessions(
    client: GraphClient,
    structure: Structure,
    ids: Sequence[uuid.UUID],
    *,
    start: int = 0,
) -> None:
    """End each complete session among the nodes ``ids`` holds from ``start`` on."""
    for n in range(start, len(ids)):
        node = structure.nodes[n]
        if node.kind == "AgentSession" and node.status == "complete":
            client.session_end(ids[n])


def _bodies(
    structure: Structure,
    found: Islands,
    picked: Showcase,
) -> list[dict[str, object]]:
    """Each node's submit body: title, status and owner, a Belief's verdict, the showcase's text."""
    contents = from_plain(loads(GRAPH_CONTENTS.read_text()), dict[str, object])
    written = from_plain(contents["roots"], list[str])
    named = {root: written[k % len(written)] for k, root in enumerate(found.roots)}
    kinds = from_plain(contents.get("kinds"), dict[str, object])
    built = {
        kind: _titles(from_plain(kinds.get(kind), dict[str, object]))
        for kind in {node.kind for node in structure.nodes}
    }
    made = Counter[str]()
    shown = from_plain(contents.get("showcase"), dict[str, object])
    verdicts = _verdicts(structure)
    bodies: list[dict[str, object]] = []
    for n, node in enumerate(structure.nodes):
        # A complete session is created live and ended after its batch.
        ending = node.kind == "AgentSession" and node.status == "complete"
        title = named.get(n)
        if title is None:
            title = built[node.kind][made[node.kind] % len(built[node.kind])]
            made[node.kind] += 1
        body: dict[str, object] = {
            "title": title,
            "status": "active" if ending else node.status,
            # A session's owner is its agent's name, which no two live sessions share.
            "owner": (
                f"{_AGENTS[n % len(_AGENTS)]}-{n}"
                if node.kind == "AgentSession"
                else f"{_PEOPLE[n % len(_PEOPLE)]}@example.com"
            ),
        }
        if node.kind == "Belief":
            judgement = verdicts.get(n, "unproven")
            body["judgement"] = judgement
            body["confidence"] = {"proven": (7 + n % 3) / 10, "disproven": 0.2}.get(
                judgement,
                0.5,
            )
        if n in {picked.root, picked.belief}:
            body |= from_plain(
                shown.get("root" if n == picked.root else "belief"),
                dict[str, object],
            )
        bodies.append(body)
    return bodies


def _verdicts(structure: Structure) -> dict[int, str]:
    """Judge each Belief its ``proves`` edges vote on: proven by any for, else disproven."""
    votes: dict[int, set[int]] = {}
    for link in structure.edges:
        if link.kind == "proves" and link.sign:
            votes.setdefault(link.to_index, set()).add(link.sign)
    return {n: "proven" if 1 in signs else "disproven" for n, signs in votes.items()}


def _cascade_key(
    link: Link,
    *,
    issue: Sequence[bool],
    size: Sequence[int],
) -> tuple[int, int, int]:
    """Where ``cascade_order`` puts ``link``: its step, then its parent, youngest first, then its subtree."""
    if not issue[link.from_index]:
        return 3, 0, 0
    if not issue[link.to_index]:
        return 1, 0, 0
    return 2, -link.to_index, size[link.from_index]


# A valenced edge weighs 0.3 to 0.9, cycling by its ends, so the graph's citations draw
# at several widths.
def _edge(
    link: Link,
    *,
    carrier: int,
    ids: Sequence[uuid.UUID],
) -> dict[str, object]:
    """One edge of a batch that creates node ``carrier``: by its index there, others by id."""
    edge: dict[str, object] = {"edge_kind": link.kind}
    for end, index in (("from", link.from_index), ("to", link.to_index)):
        if index == carrier:
            edge[f"{end}_index"] = 0
        else:
            edge[f"{end}_id"] = str(ids[index])
    if link.sign:
        weight = 0.3 + (link.from_index + link.to_index) % 4 / 5
        edge["valence"] = round(link.sign * weight, 1)
    return edge


# A kind's nodes take its titles in turn, so lists, which show a kind's newest rows
# together, repeat none until the kind has used them all. Picking each word by a
# checksum of the node's index repeated titles within a dozen rows: CRC-32 is linear,
# so for indexes of one length the words of two lists of eight always paired alike.
def _titles(vocabulary: Mapping[str, object]) -> list[str]:
    """Every title a kind's templates and word lists make, in the order of their hashes."""
    words = [
        from_plain(row, list[str])
        for row in from_plain(vocabulary.get("words"), list[object], default=[])
    ]
    titles: set[str] = set()
    for template in from_plain(vocabulary.get("templates"), list[str], default=[]):
        slots = sorted(
            {
                int(f)
                for f in (
                    field or "" for _, field, _, _ in string.Formatter().parse(template)
                )
                if f
            },
        )
        for picked in itertools.product(*(words[slot] for slot in slots)):
            parts = dict(zip(slots, picked, strict=True))
            title = template.format(*(parts.get(k, "") for k in range(len(words))))
            titles.add(title[0].upper() + title[1:])
    return sorted(titles, key=lambda title: hashlib.blake2b(title.encode()).digest())
