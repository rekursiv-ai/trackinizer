import type { Graph, GraphNode } from "../api/graph";
import { micros } from "../live/rows";

/** The key of the group of nodes under no root; no inquiry's id is this. */
export const UNROOTED = "unrooted";

/** A root and every node under it, or the nodes under no root. */
export type RootGroup = {
  /** The root's id, or `UNROOTED`. */
  readonly key: string;
  /** Null for the nodes under no root. */
  readonly root: GraphNode | null;
  /** The ids of the nodes it holds: the root, then the rest by hops below it; with no root, in the graph's order. */
  readonly members: readonly string[];
  /** When its newest node was created. */
  readonly newest: string;
  /** How many of its nodes are of each kind. */
  readonly kinds: ReadonlyMap<string, number>;
};

/** A graph's roots and where each node sits among them. */
export type Roots = {
  /** Each root's group, the one with the newest node first, then the nodes under no root, if any. */
  readonly groups: readonly RootGroup[];
  /**
   * Each node's group for the layout, by node id: the root nearest above it,
   * the newest root of those as near; `UNROOTED` under none.
   */
  readonly home: ReadonlyMap<string, string>;
  /** The roots above each node, by node id, nearest first, then newest first; a root is under itself. */
  readonly under: ReadonlyMap<string, readonly string[]>;
};

/**
 * The roots of `graph`'s subgraphs and each node's place under them.
 *
 * A root is an Issue that narrows nothing in the graph, as a root goal tops a
 * stream in the Issue list, with something under it: what narrows it, what
 * narrows those, and so on down, and everything any of them produced
 * (`produced_by`). Other edges (`requires`, `proves`, ...) put nothing under a
 * root. A node can be under several roots; a cycle no root is above leaves its
 * nodes under none.
 */
export function findRoots({ nodes, edges }: Graph): Roots {
  const byId = new Map(nodes.map((found) => [found.id, found]));
  /** What narrows each node or was produced by it. */
  const below = new Map<string, string[]>();
  const narrowing = new Set<string>();
  for (const { from_id: from, to_id: to, edge_kind: kind } of edges) {
    if ((kind !== "narrows" && kind !== "produced_by") || !byId.has(from) || !byId.has(to)) continue;
    if (kind === "narrows") narrowing.add(from);
    const children = below.get(to) ?? [];
    children.push(from);
    below.set(to, children);
  }
  const roots = nodes.filter((found) => found.kind === "Issue" && !narrowing.has(found.id) && below.has(found.id)).toSorted(newestFirst);
  const above = new Map(nodes.map((found) => [found.id, [] as { root: string; hops: number }[]]));
  const groups = roots.map((root) => {
    // Breadth first, so each node's first count is its fewest hops; a node met
    // again, as in a cycle, is not walked again.
    const hops = new Map([[root.id, 0]]);
    for (const [id, depth] of hops) {
      for (const child of below.get(id) ?? []) if (!hops.has(child)) hops.set(child, depth + 1);
    }
    for (const [id, depth] of hops) above.get(id)!.push({ root: root.id, hops: depth });
    return group(root.id, root, [...hops.keys()].map((id) => byId.get(id)!));
  });
  // Each node's roots came newest first; a stable sort by hops keeps that among the as near.
  const under = new Map([...above].map(([id, tops]) => [id, tops.toSorted((a, b) => a.hops - b.hops).map(({ root }) => root)]));
  const unrooted = nodes.filter((found) => under.get(found.id)!.length === 0);
  return {
    groups: [
      // Stable: of two groups whose newest node is the same, the newer root first.
      ...groups.toSorted((a, b) => micros(b.newest) - micros(a.newest)),
      ...(unrooted.length > 0 ? [group(UNROOTED, null, unrooted)] : []),
    ],
    home: new Map([...under].map(([id, tops]) => [id, tops[0] ?? UNROOTED])),
    under,
  };
}

function group(key: string, root: GraphNode | null, members: readonly GraphNode[]): RootGroup {
  const kinds = new Map<string, number>();
  let newest = members[0]!.created;
  for (const { kind, created } of members) {
    kinds.set(kind, (kinds.get(kind) ?? 0) + 1);
    if (micros(created) > micros(newest)) newest = created;
  }
  return { key, root, members: members.map(({ id }) => id), newest, kinds };
}

/** By `created`, to the microsecond the server keeps: `Date.parse` keeps only milliseconds. */
function newestFirst(a: GraphNode, b: GraphNode): number {
  return micros(b.created) - micros(a.created);
}
