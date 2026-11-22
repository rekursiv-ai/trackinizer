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
its titles in turn, and every record's summary from its prose, so a panel opens on
text someone has written rather than on a blank — it stands in for theirs, and they
overwrite it there. Then each record that plausibly spent money is charged its cost
from the same file's ranges, an Experiment for its GPU-hours and an AgentSession for
its tokens, through the client's own cost writes; a Belief or an Issue is left at
zero, because a claim does not spend, testing one does. Every choice follows from a
node's index, and every cost from a hash of its title, so every seed writes the same.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

import bisect
import functools
import hashlib
import itertools
import math
import string
import uuid

from treekle import from_plain, json_loads

from trackinizer.web.scripts.graph_structure import Link, Node, Structure
from trackinizer.wire.bodies import BATCH_MAX_ITEMS


_CWD: Final[Path] = Path(__file__).resolve().parent

if TYPE_CHECKING:
    from collections.abc import Sequence

    from trackinizer.types.inquiries import Inquiry
    from trackinizer.wire.wire_sessions import SessionEnd


GRAPH_CONTENTS: Final = _CWD / "graph_contents.json"
"""The hand-written text: roots' titles, newest first, the showcase, word lists."""


@functools.lru_cache(maxsize=1)
def _contents() -> Mapping[str, object]:
    """``GRAPH_CONTENTS``, read once: a seed gives every node its summary."""
    return json_loads(GRAPH_CONTENTS.read_text(), dict[str, object])


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

    def add_cost(
        self,
        target_id: uuid.UUID,
        field: str,
        value: float,
        *,
        actor: str,
        reason: str = "",
    ) -> None:
        """Add ``value`` USD to one ``marginal_cost`` axis of ``target_id``."""
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

    The island is one the newest window holds -- the demo opens on the 1k view
    -- whose edges carry the most valences, one of them against, so its
    evidence draws in both colours; the Belief is the one in it cited most.

    Args:
      structure: The nodes and edges.
      found: Its islands, as ``islands`` finds them.

    Returns:
      showcase: The island's root and the Belief, by node index.

    Raises:
      ValueError: No island has evidence against a Belief in it.

    """
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
    # The newest fifth of the stream: an island there is a ball in the window
    # the demo opens, whatever the structure's full size.
    members: dict[int, list[int]] = {}
    for n, home in found.home.items():
        members.setdefault(home, []).append(n)
    fresh = 0.6 * len(structure.nodes)
    candidates = [
        r for r in against if sum(members[r]) / len(members[r]) > fresh
    ] or list(against)
    if not candidates:
        raise ValueError("no island has evidence against a Belief in it")
    root = max(candidates, key=lambda r: (weight[r], r))
    beliefs = [
        (count, n)
        for (home, n), count in cited.items()
        if home == root and structure.nodes[n].kind == "Belief"
    ]
    # The oldest of the most-cited, so a seed that adds claims to the island
    # keeps showing the one it showed before.
    return Showcase(
        root=root, belief=max(beliefs, key=lambda row: (row[0], -row[1]))[1]
    )


# The exported graph settled each thread on its own evidence: hardly a
# ``proves`` or ``favors`` crosses islands, while a living graph cites an
# experiment across neighbouring threads. The mesh between the grouped
# graph's clusters is those edges, so each Belief gains one citation from a
# nearby island, its sign turning on its index.
_SPREAD_LEAD: Final = 500
"""The widest gap, in creation order, between a Belief and evidence spread finds it."""


def spread(structure: Structure) -> Structure:
    """Return ``structure`` with one cross-island citation added per Belief.

    Each Belief takes one Experiment, Paper or WebResult of another island
    created within ``_SPREAD_LEAD`` of it: the pair already cited, or whose
    addition would close a cycle the server would reject, passes to the next
    candidate. Only cited edges are added, so ``islands`` groups the result
    the same and ``showcase`` picks the same island.

    Args:
      structure: The nodes and edges, as ``load`` read them.

    Returns:
      structure: Its nodes, and its edges with the spread's added.

    """
    found = islands(structure)
    home = {n: found.home.get(n, n) for n in range(len(structure.nodes))}
    cited: set[tuple[int, int]] = set()
    walks: dict[int, list[int]] = {}
    for link in structure.edges:
        if link.kind in {"proves", "favors", "supersedes"}:
            cited.add((link.from_index, link.to_index))
            walks.setdefault(link.from_index, []).append(link.to_index)

    def reaches(start: int, stop: int) -> bool:
        """Whether ``start`` already follows cited edges to ``stop``."""
        walk = [start]
        seen = {start}
        for node in walk:
            if node == stop:
                return True
            for next_ in walks.get(node, ()):
                if next_ not in seen:
                    seen.add(next_)
                    walk.append(next_)
        return False

    evidence = [
        n
        for n, node in enumerate(structure.nodes)
        if node.kind in {"Experiment", "Paper", "WebResult"}
    ]
    added: list[Link] = []
    for n, node in enumerate(structure.nodes):
        if node.kind != "Belief":
            continue
        near = [
            m
            for m in evidence[
                bisect.bisect_left(evidence, n - _SPREAD_LEAD) : bisect.bisect_right(
                    evidence,
                    n + _SPREAD_LEAD,
                )
            ]
            if home[m] != home[n] and (m, n) not in cited and not reaches(n, m)
        ]
        if not near:
            continue
        # Which candidate takes each Belief follows from its index alone, so
        # every seed spreads the same way.
        m = near[(n * 31 + 7) % len(near)]
        added.append(
            Link(
                from_index=m,
                to_index=n,
                kind="favors" if n % 3 == 0 else "proves",
                sign=-1 if n % 4 == 0 else 1,
            ),
        )
        cited.add((m, n))
        walks.setdefault(m, []).append(n)
    return Structure(nodes=structure.nodes, edges=(*structure.edges, *added))


# The exported graph's newest work is thin on claims: a 286-node thread of
# experiments and code changes holds no Belief at all, so the overview's
# blue-on-lavender reads it as a thread that concluded nothing. Every thread
# of a working size should hold a few claims, so each island under
# ``_CLAIMS_MIN`` Beliefs gains them between its own nodes, in time order:
# produced by an older Issue or session of the thread, and proven by a
# younger Experiment or favored by a younger Paper of it.
_CLAIMS_MIN: Final = 10
"""The fewest Beliefs a rooted thread of ``claims`` holds."""

_CLAIMS_ISLAND: Final = 24
"""The fewest nodes an island must have for ``claims`` to fill it."""


def claims(structure: Structure) -> Structure:
    """Return ``structure`` with each thin island's Beliefs added.

    Args:
      structure: The nodes and edges, as ``spread`` left them.

    Returns:
      structure: Its islands' nodes with Beliefs interleaved in creation
        order, and the ``produced_by``, ``proves`` and ``favors`` that place
        and settle each claim. Only positive votes are added, so
        ``showcase``'s candidates and ``islands``' roots hold.

    """
    found = islands(structure)
    # A childless Issue turns into a root the moment a claim names it its
    # producer, splitting the island: only a node that already has children,
    # or a session (which no root is), can take one.
    parents = {
        link.to_index
        for link in structure.edges
        if link.kind in {"narrows", "produced_by"}
    }
    members: dict[int, list[int]] = {}
    held: Counter[int] = Counter()
    for n, node in enumerate(structure.nodes):
        home = found.home.get(n)
        if home is not None:
            members.setdefault(home, []).append(n)
            if node.kind == "Belief":
                held[home] += 1

    # How many Beliefs to insert after each of the structure's nodes.
    after: Counter[int] = Counter()
    for home, row in sorted(members.items()):
        if len(row) < _CLAIMS_ISLAND:
            continue
        need = _CLAIMS_MIN - held[home]
        if need <= 0:
            continue
        # Half the claims go into the island's last quarter: the demo's
        # windows show each thread's newest members, and a claim that lands
        # before the window is a cluster without any blue.
        head = (need + 1) // 2
        for j in range(need):
            if j < head:
                d = (j + 1) * (len(row) // 4) // (head + 1)
                after[row[len(row) - 1 - max(d, 1)]] += 1
            else:
                after[row[(j - head + 1) * len(row) // (need - head + 1)]] += 1

    nodes: list[Node] = []
    mapping: dict[int, int] = {}
    # The new Beliefs' slots: their index once placed, and the thread they join.
    placed: list[tuple[int, int]] = []
    for n, node in enumerate(structure.nodes):
        mapping[n] = len(nodes)
        nodes.append(node)
        for _ in range(after[n]):
            placed.append((len(nodes), n))
            nodes.append(Node(kind="Belief", status="active"))
    edges = [
        Link(
            from_index=mapping[link.from_index],
            to_index=mapping[link.to_index],
            kind=link.kind,
            sign=link.sign,
        )
        for link in structure.edges
    ]
    for at, parent in placed:
        # The Belief's producer: the nearest Issue or session at or before
        # the node it follows, which stands older by placement and already
        # has children of its own or is no Issue at all.
        home = found.home[parent]
        row = members[home]
        producer = next(
            (
                m
                for m in reversed(row[: row.index(parent) + 1])
                if structure.nodes[m].kind == "AgentSession"
                or (structure.nodes[m].kind == "Issue" and m in parents)
            ),
            home,
        )
        edges.append(
            Link(
                from_index=at,
                to_index=mapping[producer],
                kind="produced_by",
                sign=0,
            ),
        )
        # And its evidence: an Experiment, else a Paper, of the same thread
        # made after it, which settles the claim or favors it. Which one each
        # claim takes turns on its slot, so the thread's claims rest on
        # different runs.
        later = [m for m in row if mapping[m] > at]
        evidence = [m for m in later if structure.nodes[m].kind == "Experiment"] or [
            m for m in later if structure.nodes[m].kind == "Paper"
        ]
        if evidence:
            m = evidence[at % len(evidence)]
            edges.append(
                Link(
                    from_index=mapping[m],
                    to_index=at,
                    kind="proves"
                    if structure.nodes[m].kind == "Experiment"
                    else "favors",
                    sign=1,
                ),
            )
    return Structure(nodes=tuple(nodes), edges=tuple(edges))


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
    bodies = _bodies(structure, found, picked)
    ids = _write(client, structure, bodies, actor=actor)
    _charge(client, structure, bodies, ids, actor=actor)
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
    contents = _contents()
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
        # The showcase's own text replaces the built title, so it takes no
        # turn of the cycle: a skipped turn would shift every later title
        # and repeat one where the cycle wraps.
        if title is None and n not in {picked.root, picked.belief}:
            title = built[node.kind][made[node.kind] % len(built[node.kind])]
            made[node.kind] += 1
        body: dict[str, object] = {
            "title": title,
            "status": "active" if ending else node.status,
            # A session's owner is its agent's name, which no two live sessions share.
            "owner": (
                f"{_AGENTS[n % len(_AGENTS)]}-{n}"
                if node.kind == "AgentSession"
                else node.owner or f"{_PEOPLE[n % len(_PEOPLE)]}@example.com"
            ),
        }
        if node.kind == "Belief":
            judgement = verdicts.get(n, "unproven")
            body["judgement"] = judgement
            body["confidence"] = {"proven": (7 + n % 3) / 10, "disproven": 0.2}.get(
                judgement,
                0.5,
            )
        stated = body.get("confidence")
        # Every record is opened with its summary written: a panel that says "add
        # a summary" on nine hundred rows reads as an empty tool, and the demo is
        # of a graph people have been keeping. The text stands in for what a
        # person would have typed, and they overwrite it there.
        body["description"] = summary_for(
            node.kind,
            str(body["title"]),
            owner=str(body["owner"]),
            verdict=str(body.get("judgement", "")),
            confidence=float(stated) if isinstance(stated, float) else None,
        )
        if n in {picked.root, picked.belief}:
            body |= from_plain(
                shown.get("root" if n == picked.root else "belief"),
                dict[str, object],
            )
        bodies.append(body)
    return bodies


# A record's summary, which the panels show under its title.
#
# The variants in ``graph_contents.json`` are the hand-written text: each stands
# in for what a person would have typed, and picks up the record's own words so it
# reads about the thing its title names. ``{w0}`` and friends are the kind's word
# lists found inside the title, so a Belief titled "Label smoothing hurts recall"
# gets "Label smoothing" and "hurts recall" back as slots.
def summary_for(
    kind: str,
    title: str,
    *,
    owner: str = "",
    verdict: str = "",
    confidence: float | None = None,
) -> str:
    """Return the summary ``seed_graph`` gives a record of ``kind`` titled ``title``.

    Args:
      kind: The record's kind, a key in the contents' ``prose``.
      title: Its title, which the variant is read against.
      owner: Who the record is assigned to.
      verdict: A Belief's judgement.
      confidence: A Belief's confidence.

    Returns:
      summary: The variant's text, with the record's own words in it; empty when
        every variant for the kind names something this record does not have.

    """
    contents = _contents()
    prose = from_plain(contents.get("prose"), dict[str, object])
    variants = from_plain(prose.get(kind), list[str])
    if not variants:
        raise ValueError(f"No prose written for kind {kind!r}.")
    # A variant that names what the record lacks would read as a hole where the
    # word should be, so it is not offered: a record with no owner gets a summary
    # that does not name one.
    missing = [
        name
        for name, stated in (
            ("owner", owner),
            ("verdict", verdict),
            ("conf", "" if confidence is None else "0"),
        )
        if not stated
    ]
    usable = [
        text
        for text in variants
        if not any(f"{{{name}}}" in text for name in missing)
    ]
    if not usable:
        return ""
    vocabulary = from_plain(
        from_plain(contents.get("kinds"), dict[str, object]).get(kind),
        dict[str, object],
    )
    words = [
        from_plain(row, list[str])
        for row in from_plain(vocabulary.get("words"), list[object], default=[])
    ]
    found: dict[str, str] = {}
    for slot, pool in enumerate(words):
        hit = next((word for word in pool if word.lower() in title.lower()), "")
        found[f"w{slot}"] = hit
        found[f"W{slot}"] = hit[:1].upper() + hit[1:]
    # Counted over every variant, so a title keeps its summary whether or not the
    # record has the words some of them name: short of one, it takes the nearest
    # variant that can be said.
    return usable[
        min(
            int(hashlib.blake2b(title.encode(), digest_size=8).hexdigest(), 16)
            % len(variants),
            len(usable) - 1,
        )
    ].format(
        Title=title,
        title=title[:1].lower() + title[1:],
        owner=owner,
        verdict=verdict,
        conf="" if confidence is None else f"{confidence:.2f}",
        **found,
    )


# A record's cost, which the ledger keeps beside its summary.
#
# The ranges in ``graph_contents.json`` are the lab's own: an Experiment's GPU-hours,
# an AgentSession's tokens. The two axes -- ``agent`` and ``resource`` -- are charged
# through the client's ``add_cost``, one write each, because the running totals on an
# inquiry row are not settable fields but the sum of its change log. A kind absent
# from the block, a Belief or an Issue, is left at zero: stating a claim costs
# nothing, and the honest story and the cheap one agree that the spend is on the runs
# that tested it, not the record that holds the verdict.
_AGENT_AXIS: Final = "marginal_cost_agent_usd"
_RESOURCE_AXIS: Final = "marginal_cost_resource_usd"


def cost_for(kind: str, title: str) -> dict[str, float]:
    """Return the axes ``seed_graph`` charges a record of ``kind`` titled ``title``.

    Args:
      kind: The record's kind, a key in the contents' ``cost``, or absent for a kind
        that spends nothing.
      title: Its title, which the value is drawn from, so two seeds charge the same.

    Returns:
      cost: Each axis name the record pays, mapped to its USD amount; empty when the
        kind spends nothing.

    """
    block = from_plain(_contents().get("cost"), dict[str, object])
    spans = block.get(kind)
    if not isinstance(spans, Mapping):
        # A kind absent from the block -- a Belief, an Issue -- spends nothing.
        return {}
    charged: dict[str, float] = {}
    for axis, field in (("agent", _AGENT_AXIS), ("resource", _RESOURCE_AXIS)):
        row = from_plain(spans.get(axis), list[float])
        if len(row) != 2:
            continue
        low, high = row
        # The same title, the same figure: a value between the bounds at the position
        # the title's hash lands on, drawn to a cent, exactly as ``summary_for`` picks
        # a variant from the hash rather than the roll of a die.
        span = int(hashlib.blake2b(title.encode(), digest_size=8).hexdigest(), 16)
        amount = round(low + span / 2**64 * (high - low), 2)
        if amount > 0:
            charged[field] = amount
    return charged


def _charge(
    client: GraphClient,
    structure: Structure,
    bodies: Sequence[Mapping[str, object]],
    ids: Sequence[uuid.UUID],
    *,
    actor: str,
) -> None:
    """Accrue each created record's cost through ``client``, in node order.

    Every spend-bearing kind gets one ``add_cost`` write per axis it pays; a Belief or
    an Issue, whose cost is empty, is written nothing at all. Node order fixes the
    sequence, so two seeds make the same writes in the same order.
    """
    for n, node in enumerate(structure.nodes):
        for field, amount in cost_for(node.kind, str(bodies[n]["title"])).items():
            client.add_cost(ids[n], field, amount, actor=actor)


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
        for row in from_plain(
            vocabulary.get("words"),
            list[object],
            default=list[object](),
        )
    ]
    titles: set[str] = set()
    for template in from_plain(
        vocabulary.get("templates"),
        list[str],
        default=list[str](),
    ):
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
