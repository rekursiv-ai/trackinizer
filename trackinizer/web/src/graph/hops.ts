// A focus's neighbourhood: how many edges each inquiry is from the one focused,
// as Gephi's Ego Network and Neo4j Bloom's "expand" count them.
import type { Graph, GraphNode } from "../api/graph";
import type { Hops } from "../router/route";

/**
 * How many edges each inquiry is from `from`, by a breadth-first walk over
 * `graph`'s edges in either direction: `from` itself at 0, and only what the
 * walk reaches. It walks no edge of a kind in `skipEdges`, and neither counts
 * nor walks through an inquiry `keep` refuses (one the view hides). A start the
 * graph lacks or `keep` refuses reaches nothing.
 */
export function reach(
  graph: Graph,
  from: string,
  { skipEdges, keep }: { skipEdges: ReadonlySet<string>; keep: (node: GraphNode) => boolean },
): Map<string, number> {
  const kept = new Set(graph.nodes.filter(keep).map((row) => row.id));
  const neighbours = new Map<string, string[]>();
  const join = (one: string, other: string) => {
    const peers = neighbours.get(one);
    if (peers) peers.push(other);
    else neighbours.set(one, [other]);
  };
  for (const { from_id, to_id, edge_kind } of graph.edges) {
    if (skipEdges.has(edge_kind) || !kept.has(from_id) || !kept.has(to_id)) continue;
    join(from_id, to_id);
    join(to_id, from_id);
  }
  const hops = new Map<string, number>();
  if (!kept.has(from)) return hops;
  hops.set(from, 0);
  // A queue as an array read from the front: each node joins it once.
  const queue = [from];
  for (let next = 0; next < queue.length; next++) {
    const at = queue[next]!;
    for (const peer of neighbours.get(at) ?? []) {
      if (hops.has(peer)) continue;
      hops.set(peer, hops.get(at)! + 1);
      queue.push(peer);
    }
  }
  return hops;
}

/** How many inquiries lie within 1, 2 and 3 hops of the start of `reached`, the start included, and in its whole connected part. */
export function hopCounts(reached: ReadonlyMap<string, number>): { readonly [hops in Hops]: number } {
  const within = (most: number) => [...reached.values()].filter((far) => far <= most).length;
  return { 1: within(1), 2: within(2), 3: within(3), all: reached.size };
}

/** Whether an inquiry `far` hops from a focus lies within `hops` of it. */
export function isWithin(far: number | undefined, hops: Hops): boolean {
  return far !== undefined && (hops === "all" || far <= hops);
}
