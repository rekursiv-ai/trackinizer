import { expect, test } from "vitest";
import type { FocusGraph, FocusNode } from "../api/graph";
import { edge, node, uuid } from "../graph/testing";
import { PAD_PX, radialLayout } from "./radial";

const SIZE = { width: 280, height: 190 };
const [CX, CY] = [SIZE.width / 2, SIZE.height / 2];

/** Inquiry `n`, `hops` from the focus; higher `n` is newer. */
function at(n: number, hops: number, fields: Partial<FocusNode> = {}): FocusNode {
  return { ...node(n), hops, ...fields };
}

// Focus 1; on the inner ring 2 (with children 4, 5, 6) and 3 (with child 7).
const GRAPH: FocusGraph = {
  nodes: [at(1, 0), at(2, 1), at(3, 1), at(4, 2), at(5, 2), at(6, 2), at(7, 2)],
  edges: [edge(2, 1), edge(1, 3), edge(4, 2), edge(5, 2), edge(2, 6, "produced_by"), edge(7, 3)],
};

/** Where `n` sits as a fraction of the outer ring, an ellipse `PAD_PX` inside the box: 0 in the middle, 1 on it. */
function ring(placed: ReadonlyMap<string, { x: number; y: number }>, n: number) {
  const { x, y } = placed.get(uuid(n))!;
  return Math.hypot((x - CX) / (CX - PAD_PX), (y - CY) / (CY - PAD_PX));
}

/** The angle of `n` about the middle, clockwise from the top, in degrees 0 to 360. */
function angle(placed: ReadonlyMap<string, { x: number; y: number }>, n: number) {
  const { x, y } = placed.get(uuid(n))!;
  return ((Math.atan2(x - CX, -(y - CY)) * 180) / Math.PI + 360) % 360;
}

test("the focus sits in the middle, its neighbours on an inner ring and theirs on an outer one, all in the box", () => {
  const placed = radialLayout(GRAPH, SIZE);
  expect(placed.get(uuid(1))).toEqual({ x: CX, y: CY });
  const inner = [2, 3].map((n) => ring(placed, n));
  const outer = [4, 5, 6, 7].map((n) => ring(placed, n));
  for (const r of inner) expect(r).toBeCloseTo(inner[0]!, 6);
  for (const r of outer) expect(r).toBeCloseTo(1, 6);
  expect(inner[0]).toBeGreaterThan(0.3);
  expect(inner[0]).toBeLessThan(0.7);
  for (const { x, y } of placed.values()) {
    expect(x).toBeGreaterThan(0);
    expect(x).toBeLessThan(SIZE.width);
    expect(y).toBeGreaterThan(0);
    expect(y).toBeLessThan(SIZE.height);
  }
});

test("the layout is deterministic: the same neighbourhood in any order lands in the same places", () => {
  const placed = radialLayout(GRAPH, SIZE);
  const shuffled = radialLayout({ nodes: [...GRAPH.nodes].reverse(), edges: [...GRAPH.edges].reverse() }, SIZE);
  expect(shuffled).toEqual(placed);
});

test("each inner node stands in the middle of an arc as wide as its share of the outer ring, and the outer nodes hanging from it fill the arc", () => {
  const placed = radialLayout(GRAPH, SIZE);
  const apart = (a: number, b: number) => {
    const turn = Math.abs(angle(placed, a) - angle(placed, b));
    return Math.min(turn, 360 - turn);
  };
  // 3 is newer, so first, at the top; it has one child and 2 three, so 3's arc is 90 degrees and 2's 270.
  expect(angle(placed, 3)).toBeCloseTo(0, 6);
  expect(angle(placed, 2)).toBeCloseTo(180, 6);
  expect(apart(7, 3)).toBeLessThan(45);
  for (const child of [4, 5, 6]) expect(apart(child, 2)).toBeLessThan(135);
  // Within its arc, newest first, as on the inner ring.
  expect([6, 5, 4].map((n) => angle(placed, n))).toEqual([90, 180, 270].map((degrees) => expect.closeTo(degrees, 6)));
});

test("the inner ring goes clockwise from the top by kind, then newest first", () => {
  const graph: FocusGraph = {
    nodes: [at(1, 0), at(2, 1), at(3, 1), at(4, 1, { kind: "Belief" })],
    edges: [edge(2, 1), edge(3, 1), edge(4, 1, "proves")],
  };
  const placed = radialLayout(graph, SIZE);
  expect(angle(placed, 4)).toBeCloseTo(0, 6);
  expect([4, 3, 2].map((n) => angle(placed, n))).toEqual([...[4, 3, 2].map((n) => angle(placed, n))].sort((a, b) => a - b));
});

test("the more of the neighbourhood the neighbours are, the wider their ring: widest with nothing two hops out", () => {
  const alone: FocusGraph = { nodes: [at(1, 0), at(2, 1), at(3, 1)], edges: [edge(2, 1), edge(3, 1)] };
  // A hub: five neighbours, one node two hops out.
  const hub: FocusGraph = {
    nodes: [at(1, 0), ...[2, 3, 4, 5, 6].map((n) => at(n, 1)), at(7, 2)],
    edges: [...[2, 3, 4, 5, 6].map((n) => edge(n, 1)), edge(7, 2)],
  };
  const [some, most, all] = [GRAPH, hub, alone].map((graph) => ring(radialLayout(graph, SIZE), 2));
  expect(some).toBeLessThan(most!);
  expect(most).toBeLessThan(all!);
  expect(all).toBeLessThan(1);
});
