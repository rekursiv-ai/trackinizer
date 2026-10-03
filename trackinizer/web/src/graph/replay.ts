import type { Graph, GraphEdge } from "../api/graph";

/**
 * Grow `graph` again as it was made, as v1's Replay did: from nothing, one node
 * at a time in `created` order, 70 ms apart at 1x and never under a frame, each
 * edge as soon as both its ends show. `step` gets each graph as it grows, the
 * empty one first; `done` follows the whole. `speed` is read before each wait,
 * so a change applies from the next node. Returns the function that stops it.
 */
export function replay(
  graph: Graph,
  { speed, step, done }: { speed: () => number; step: (grown: Graph) => void; done: () => void },
): () => void {
  // As instants: the server writes no fraction when it is zero, so the text's
  // precision varies. Ties within a millisecond keep the server's order, oldest first.
  const nodes = graph.nodes.toSorted((a, b) => Date.parse(a.created) - Date.parse(b.created));
  const order = new Map(nodes.map((row, index) => [row.id, index]));
  // An edge shows once its later end does: after `shownAt` nodes.
  const shownAt = (row: GraphEdge) => Math.max(order.get(row.from_id)!, order.get(row.to_id)!) + 1;
  const edges = graph.edges.toSorted((a, b) => shownAt(a) - shownAt(b));
  let count = 0;
  let edgeCount = 0;
  let timer: ReturnType<typeof setTimeout>;
  const next = () => {
    while (edgeCount < edges.length && shownAt(edges[edgeCount]!) <= count) edgeCount += 1;
    step({ nodes: nodes.slice(0, count), edges: edges.slice(0, edgeCount) });
    if (count === nodes.length) return done();
    count += 1;
    timer = setTimeout(next, Math.max(FRAME_MS, STEP_MS / speed()));
  };
  next();
  return () => clearTimeout(timer);
}

/** v1's pace at 1x. */
const STEP_MS = 70;
const FRAME_MS = 16;
