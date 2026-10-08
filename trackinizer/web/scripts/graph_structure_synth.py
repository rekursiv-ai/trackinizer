#!/bin/sh
# ruff: noqa: EXE003, D300, D205 -- Polyglot shell/Python script.
# fmt: off
'''' 2>/dev/null #
exec uv --quiet --project "$(dirname "$0")" run --frozen --no-sync python3 "$0" "$@"
Write a synthetic research graph as structure alone, in the design's own image.

The committed ``graph_structure.json`` is the shape of a real research server,
which is experiment-heavy: experiments and issues crowd out the papers and web
results, so the graph reads as two colours. The design prototype draws its demo
from a generator of its own (``generate`` in the graph HTML) whose kind weights
and link patterns make every island a mesh of all nine kinds. This is that
generator, ported: the same weights, the same per-kind edges, the same
preferential attachment, the same mulberry32 stream, and the same share of
records left unattached -- a third of the field hangs by one edge or none, which
is what keeps an island a loose natural ball instead of a packed disc. Three
deliberate changes: a segment's root Issue narrows its thread's earlier root,
every segment carries its own tree, and cross-thread evidence stays inside its
time band, so the app's kinship islands come out as the prototype's threads --
six balls in the newest thousand records, fifteen in the newest 2.5k, sixteen
in all of it -- instead of fragmenting or drifting into whatever absorbs them.

The file is graph_structure.json beside this script, which seed_graph.py reads;
--out writes elsewhere.

Examples:
  ./graph_structure_synth.py
  ./graph_structure_synth.py --nodes 4970 --out /tmp/graph_structure.json

'''
# fmt: on

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

import argparse
import math

from collections import Counter

from trackinizer.web.scripts.graph_structure import (
    GRAPH_STRUCTURE,
    Link,
    Node,
    Structure,
    dump,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_KW: Final = {
    "Issue": 0.16,
    "Belief": 0.09,
    "Experiment": 0.06,
    "Paper": 0.29,
    "CodeChange": 0.025,
    "WebSearch": 0.05,
    "WebResult": 0.14,
    "AgentSession": 0.014,
    "Artifact": 0.018,
}
"""The design's kind weights: papers and web results carry the colours."""

_THREADS: Final = 16
"""The design's topic list's length: the most threads any graph gets."""

_XBRIDGE_CAP: Final = 8
"""How many links two threads may share. The design's busiest pair of clusters
shares 22 of 434 bridges, and its clusters are its threads; ours are coarser
than a thread, so the cap runs tighter than the design's count. Without one a
pair can run past a hundred, and a hundred nearly parallel hairlines blend
additively into a cone across the field."""

_PARENTED: Final = 0.78
"""How many of the design's leaf records actually carry a produced_by.

The rest hang free, and that is the whole secret of its balls: over a third of
the field has one edge or none, so the rim of an island is a fringe of loose
satellites rather than a packed circle. Attach everything and the physics has
nothing to do but make a disc.

Only records nothing else hangs off go loose -- papers, results, experiments,
code changes, artifacts, beliefs. The design drops its ``narrows`` at the same
rate, 71% of its Issues carrying one, and that is the one rate not worth
matching: its layout tags every node with a community at birth, so a loose Issue
sits in its ball whatever it links to, while the app's islands *are* the
kinship, so a loose Issue -- or a loose session, with its searches and their
results behind it -- would take everything hung off it into a second ball."""

_OWNERS: Final = (
    "ada@example.com",
    "ben@example.com",
    "cleo@example.com",
    "dee@example.com",
    "eli@example.com",
    "fay@example.com",
    "gus@example.com",
)
"""The seed's people, in seed_graph's ``_PEOPLE`` order: a thread is one
person's line of work, so the graph's Owner filter reads as grouping by
person -- three inquiries each once the bands put 21 threads in the field."""


def _mulberry(seed: int) -> Callable[[], float]:
    """Return the design's mulberry32 stream, seeded with ``seed``."""
    state = seed & 0xFFFFFFFF

    def rand() -> float:
        nonlocal state
        state = (state + 0x6D2B79F5) & 0xFFFFFFFF
        t = state ^ (state >> 15)
        t = (t * (state | 1)) & 0xFFFFFFFF
        u = t ^ (t >> 7)
        u = (u * (t | 61)) & 0xFFFFFFFF
        t = (t + u) & 0xFFFFFFFF
        t ^= u
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 0x100000000

    return rand


def _split(budget: int, weights: list[float]) -> list[int]:
    """`budget` records across the weights, whole, none under a dozen."""
    raw = [budget * w / sum(weights) for w in weights]
    sizes = [max(12, math.floor(r)) for r in raw]
    left = budget - sum(sizes)
    for k in sorted(range(len(raw)), key=lambda k: raw[k] - sizes[k], reverse=True):
        if left <= 0:
            break
        sizes[k] += 1
        left -= 1
    while left < 0 and min(sizes) > 12:
        sizes[max(range(len(sizes)), key=lambda k: sizes[k])] -= 1
        left += 1
    return sizes


def generate(nodes: int, *, seed: int = 1234) -> Structure:
    """Return a structure of ``nodes`` nodes, as the design's ``generate`` makes one.

    Args:
      nodes: How many records to draw, oldest first as they come out.
      seed: The stream's seed; the design adds the node count to its base.

    Returns:
      structure: The nodes' kinds and statuses, and the edges child to parent.

    """
    rand = _mulberry(seed + nodes)

    def gauss() -> float:
        u = 0.0
        v = 0.0
        while not u:
            u = rand()
        while not v:
            v = rand()
        return math.sqrt(-2.0 * math.log(u)) * math.cos(2.0 * math.pi * v)

    def pick(items: list[int]) -> int:
        return items[math.floor(rand() * len(items))]

    threads = _THREADS
    # The app reads the newest 1k/2.5k/5k of one stream, where the design
    # regenerates a whole structure per size: its 1k holds six threads, its
    # 2.5k fifteen, its 5k sixteen, every ball a different size and none a
    # giant. So a thread here lives a lifespan across three time bands -- the
    # oldest band is the fifteen older threads' early work, the middle their
    # inquiry, the newest the six young threads' whole life -- and a window
    # holds exactly the threads that wrote in it: six balls at 1k, fifteen at
    # 2.5k, sixteen at 5k, as spread in size as the design's.
    segments: list[tuple[int, int, int]] = []
    budgets: list[int] = []
    if nodes >= 2500:
        budgets = [nodes - 2500, 1500, 1000]
        plan = (
            [32, 48, 66, 58, 67, 63, 178, 252, 228, 236, 217, 260, 210, 220, 205, 160],
            [0, 0, 0, 0, 0, 0, 308, 221, 213, 197, 185, 140, 120, 60, 56, 0],
            [219, 189, 161, 159, 139, 133, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        )
        for b, weights in enumerate(plan):
            live = [t for t in range(threads) if weights[t]]
            if budgets[b] < 12 * len(live):
                continue
            segments.extend(zip(live, [b] * len(live), _split(budgets[b], [weights[t] for t in live])))
        n_threads = threads
    else:
        budgets = [nodes]
        n_threads = min(max(math.floor(nodes / 170 + 0.5), 6), threads)
        spread = 0.5 if nodes / n_threads <= 200 else 0.3
        weights = [math.exp(gauss() * spread) for _ in range(n_threads)]
        segments = [(t, 0, size) for t, size in enumerate(_split(nodes, weights))]

    slots: list[tuple[int, float, int]] = []
    for t, b, size in segments:
        # A segment's records fall where the design drops them: a first record
        # at the band's start, the rest pow-skewed across it.
        lo = sum(budgets[:b]) / nodes
        hi = sum(budgets[: b + 1]) / nodes
        for i in range(size):
            f = lo if i == 0 else lo + (hi - lo) * rand() ** 0.85
            slots.append((t, f, b))
    slots.sort(key=lambda slot: slot[1])

    kinds = list(_KW)
    cumulative: list[float] = []
    acc = 0.0
    for kind in kinds:
        acc += _KW[kind]
        cumulative.append(acc)

    def draw_kind() -> str:
        r = rand() * acc
        for i, cap in enumerate(cumulative):
            if r < cap:
                return kinds[i]
        return "Paper"

    def status(kind: str) -> str:
        r = rand()
        if kind == "Issue":
            return "complete" if r < 0.42 else ("active" if r < 0.86 else ("abandoned" if r < 0.95 else "invalid"))
        if kind == "Belief":
            return "active" if r < 0.8 else ("complete" if r < 0.92 else ("invalid" if r < 0.97 else "abandoned"))
        return "complete" if r < 0.62 else ("active" if r < 0.94 else ("abandoned" if r < 0.98 else "invalid"))

    rows: list[Node] = []
    edges: set[tuple[int, int, str, int]] = set()
    node_thread = [0] * len(slots)
    xpairs: Counter[tuple[int, int]] = Counter()
    # A thread's records, per band it wrote in. Evidence and paper citations
    # reach for these and never for the thread whole: a link to the same
    # thread's work in another band is invisible to a window holding only one
    # of them, and where both show it is a hair drawn clean across the field,
    # which is the one thing the design's field never shows.
    thread_pools: dict[tuple[int, int], dict[str, list[int]]] = {}
    # The design's cross pool spans its whole community set, so a shared target
    # is one of ~a thousand beliefs spread over every community: its busiest pair
    # of clusters ends up sharing two or three hairlines. A pool scoped to one
    # band is a tenth that size, and preferential attachment then crowns a few
    # beliefs inside it; every thread's stray evidence lands on the same crowned
    # few, and the two islands that hold them share eighty lines -- a searchlight,
    # not a hairline.
    cross_pools: dict[str, list[int]] = {"Belief": [], "Paper": [], "Issue": []}
    cited: list[int] = [0] * len(slots)

    def link(a: int, b: int, kind: str, sign: int = 0) -> None:
        # Two lines of inquiry borrow from each other a handful of times, not a
        # hundred -- the design's busiest pair shares 22 links. Uncapped, the two
        # threads that happen to cite the same band draw a cone across the field.
        if a == b:
            return
        [lo, hi] = sorted((node_thread[a], node_thread[b]))
        if lo != hi:
            if xpairs[(lo, hi)] >= _XBRIDGE_CAP:
                return
            xpairs[(lo, hi)] += 1
        edges.add((a, b, kind, sign))

    def pick_belief(pool: list[int]) -> int:
        if not pool:
            return -1
        total_weight = sum(1 + cited[b] * 1.6 for b in pool)
        r = rand() * total_weight
        for b in pool:
            r -= 1 + cited[b] * 1.6
            if r <= 0:
                return b
        return pool[-1]

    # A window holds a thread only from the segments that fall in it, and the
    # app's islands are kinship -- narrows and produced_by -- so each segment
    # carries its own tree: its own root Issue, sessions, papers. A segment's
    # root narrows the thread's earlier root, so the full stream shows one
    # island per thread while every window shows one per segment set: six balls
    # at 1k, fifteen at 2.5k, sixteen at 5k.
    seg_pools: dict[tuple[int, int], dict[str, list[int]]] = {}
    prev_root: dict[int, int] = {}
    for position, (c, _f, b) in enumerate(slots):
        pool = thread_pools.setdefault((c, b), {kind: [] for kind in kinds})
        seg = seg_pools.setdefault((c, b), {kind: [] for kind in kinds})
        starting = not any(seg.values())
        cross_pool = cross_pools
        if starting:
            kind = "Issue"
        elif not seg["AgentSession"] and seg["Issue"]:
            kind = "AgentSession"
        else:
            kind = draw_kind()
        if kind == "WebResult" and not seg["WebSearch"]:
            kind = "WebSearch"
        if kind == "WebSearch" and not seg["AgentSession"]:
            kind = "AgentSession"
        if kind == "CodeChange" and not seg["Experiment"] and not seg["AgentSession"]:
            kind = "Issue"
        if kind == "Belief" and not seg["Issue"]:
            kind = "Issue"
        node = position
        node_thread[node] = c
        rows.append(
            Node(
                kind=cast("Node.kind", kind),
                status=cast("Node.status", status(kind)),
                owner=_OWNERS[c % len(_OWNERS)],
            )
        )
        tied = len(edges)

        def evidence() -> None:
            cross = rand() < 0.07
            belief_pool = cross_pool["Belief"] if cross and cross_pool["Belief"] else pool["Belief"]
            b = pick_belief(belief_pool)
            if b < 0:
                return
            cites = rand() < (0.35 if kind == "WebResult" else 0.45)
            sign = (1 if rand() < (0.76 if cites else 0.6) else -1)
            link(node, b, "proves" if cites else "favors", sign)
            cited[b] += 1

        if kind == "Issue":
            # Every Issue but a segment's first narrows, so a window's balls are
            # exactly its segments' trees; a segment's root narrows the thread's
            # earlier root, which joins the segments into one island once the
            # whole stream shows.
            if starting:
                if prev_root.get(c) is not None:
                    link(node, prev_root[c], "narrows")
                prev_root[c] = node
            elif seg["Issue"]:
                link(node, pick(seg["Issue"]), "narrows")
                if rand() < 0.24:
                    link(node, pick(seg["Issue"]), "requires")
            if rand() < 0.07 and cross_pool["Issue"]:
                link(node, pick(cross_pool["Issue"]), "requires")
        elif kind == "AgentSession":
            if seg["Issue"]:
                link(node, pick(seg["Issue"]), "produced_by")
        elif kind == "WebSearch":
            link(node, pick(seg["AgentSession"]), "produced_by")
        elif kind == "WebResult":
            if rand() < _PARENTED:
                link(node, pick(seg["WebSearch"]), "produced_by")
            if rand() < 0.42:
                evidence()
        elif kind == "Paper":
            # The design's generator tags every paper with its community from
            # birth; the app groups by kinship, so a paper hangs on its segment
            # -- the search that found it, else a session, else an issue of it
            # -- at the design's rate. Left unattached, papers drift into
            # whatever island absorbs them and the balls come out mis-sized;
            # attached at the design's rate, the loose ones stay near home and
            # give the rim its fringe.
            parent_pool = seg["WebSearch"] or seg["AgentSession"] or seg["Issue"]
            if parent_pool and rand() < _PARENTED:
                link(node, pick(parent_pool), "produced_by")
            for _ in range(math.floor(rand() * 2.9)):
                paper_pool = pool["Paper"] if rand() < 0.88 and pool["Paper"] else cross_pool["Paper"]
                if paper_pool:
                    link(node, pick(paper_pool), "cites_paper")
            if rand() < 0.44:
                evidence()
        elif kind == "Experiment":
            parent = pick(seg["AgentSession"]) if rand() < 0.7 and seg["AgentSession"] else pick(seg["Issue"])
            if rand() < _PARENTED:
                link(node, parent, "produced_by")
            for _ in range(1 + math.floor(rand() * 2.4) if rand() < 0.85 else 0):
                cross = rand() < 0.07
                belief_pool = cross_pool["Belief"] if cross and cross_pool["Belief"] else pool["Belief"]
                b = pick_belief(belief_pool)
                if b >= 0:
                    link(node, b, "proves", 1 if rand() < 0.76 else -1)
                    cited[b] += 1
        elif kind == "CodeChange":
            if seg["Experiment"] and rand() < 0.5:
                parent = pick(seg["Experiment"])
            else:
                parent = pick(seg["AgentSession"] or seg["Issue"])
            if rand() < _PARENTED:
                link(node, parent, "produced_by")
        elif kind == "Artifact":
            if rand() < _PARENTED:
                link(node, pick(seg["Issue"]), "produced_by")
        elif kind == "Belief":
            parent = pick(seg["Issue"]) if rand() < 0.6 else pick(seg["AgentSession"] or seg["Issue"])
            if rand() < _PARENTED:
                link(node, parent, "produced_by")
            if pool["Belief"] and rand() < 0.07:
                link(node, pick(pool["Belief"]), "supersedes")
            if pool["Belief"] and rand() < 0.08:
                b = pick_belief(pool["Belief"])
                link(node, b, "proves", 1 if rand() < 0.76 else -1)
                cited[b] += 1
        if len(edges) == tied:
            # A record with nothing at all is noise, not a satellite: the
            # design's loose ones still cite or prove something. That one tie is
            # what keeps a loose record in its own ball when the islands absorb
            # it -- with none it joins whichever island was alive nearest, and a
            # clump of those reads as a ball with no spine.
            if kind == "Paper" and pool["Paper"]:
                link(node, pick(pool["Paper"]), "cites_paper")
            else:
                evidence()
        seg[kind].append(node)
        pool[kind].append(node)
        if kind in cross_pool:
            cross_pool[kind].append(node)

    kept = tuple(rows)
    ordered = sorted(
        (Link(from_index=a, to_index=b, kind=cast("Link.kind", kind), sign=sign) for a, b, kind, sign in edges),
        key=lambda row: (row.to_index, row.from_index, row.kind),
    )
    return Structure(nodes=kept, edges=tuple(ordered))


def main() -> int:
    """Generate the structure and write it.

    Returns:
      result: Process exit code (0 on success).

    """
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").split("\n", 2)[2],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--nodes", type=int, default=4970, help="Records to draw.")
    parser.add_argument("--out", type=Path, default=GRAPH_STRUCTURE)
    flags = parser.parse_args()
    structure = generate(flags.nodes)
    flags.out.write_text(dump(structure))
    kinds = Counter(node.kind for node in structure.nodes)
    print(
        f"{flags.out}: {len(structure.nodes)} nodes {dict(kinds.most_common())}, "
        f"{len(structure.edges)} edges",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
# vim: ft=python
