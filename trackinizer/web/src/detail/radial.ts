// Where the detail's graph preview draws each inquiry: a radial layout, worked out
// in one pass, so the preview needs no force simulation and no graph chunk.
import type { FocusGraph, FocusNode } from "../api/graph";

/** The outer ring's gap to the box's edge: room for a root's disc and its halo. */
export const PAD_PX = 14;

/**
 * Where each inquiry of a focus's neighbourhood sits in a box of `size`, by id.
 *
 * The focus sits in the middle; its neighbours (hop 1) on an inner ellipse, and
 * theirs (hop 2) on an outer one, `PAD_PX` inside the box. The inner ring goes
 * clockwise from the top by kind, then newest first, and each inner node stands
 * in the middle of an arc as wide as its share of the outer ring: the outer
 * nodes that hang from it, in the same order, at least one. An outer node hangs
 * from the first inner node it shares an edge with. The inner ring widens with
 * its share of the nodes, so a hub's many neighbours spread out, up to most of
 * the box with nothing on the outer ring. Nothing rests on the input's order.
 */
export function radialLayout(graph: FocusGraph, { width, height }: { width: number; height: number }): Map<string, { x: number; y: number }> {
  const [cx, cy] = [width / 2, height / 2];
  const inner = graph.nodes.filter((row) => row.hops === 1).toSorted(byKindThenNewest);
  const outer = graph.nodes.filter((row) => row.hops >= 2).toSorted(byKindThenNewest);
  const rank = new Map(inner.map((row, index) => [row.id, index]));
  // Each outer node's first inner neighbour; one the server's limit left without any hangs last, in an arc of its own.
  const from = new Map(outer.map((row) => [row.id, inner.length]));
  for (const { from_id, to_id } of graph.edges) {
    for (const [one, other] of [[from_id, to_id], [to_id, from_id]] as const) {
      const at = rank.get(other);
      if (at !== undefined && from.has(one)) from.set(one, Math.min(from.get(one)!, at));
    }
  }
  const hanging = [...inner, null].map((_, index) => outer.filter((row) => from.get(row.id) === index));
  const arcs = hanging.map((rows, index) => (index < inner.length ? Math.max(1, rows.length) : rows.length));
  const total = arcs.reduce((sum, arc) => sum + arc, 0);
  const placed = new Map(graph.nodes.filter((row) => row.hops === 0).map((row) => [row.id, { x: cx, y: cy }]));
  const put = (row: FocusNode, turns: number, scale: number) => {
    const angle = 2 * Math.PI * turns - Math.PI / 2;
    placed.set(row.id, { x: cx + scale * (cx - PAD_PX) * Math.cos(angle), y: cy + scale * (cy - PAD_PX) * Math.sin(angle) });
  };
  // In turns clockwise from the top; the first arc is centred on the top.
  let start = -arcs[0]! / total / 2;
  const inside = 0.5 + 0.3 * (inner.length / (inner.length + outer.length));
  for (const [index, rows] of hanging.entries()) {
    const arc = arcs[index]! / total;
    if (index < inner.length) put(inner[index]!, start + arc / 2, inside);
    for (const [at, row] of rows.entries()) put(row, start + (arc * (at + 0.5)) / rows.length, 1);
    start += arc;
  }
  return placed;
}

/** By kind, then newest first, then by seq and id, so the order never rests on the input's. */
function byKindThenNewest(a: FocusNode, b: FocusNode): number {
  return compare(a.kind, b.kind) || compare(b.created, a.created) || b.seq - a.seq || compare(a.id, b.id);
}

function compare(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}
