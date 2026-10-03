import { expect, test } from "vitest";
import { hopCounts, isWithin, reach } from "./hops";
import { edge, node, uuid } from "./testing";

// 1 <- 2 <- 3 <- 4 by narrows, 5 produced by 2, and 6 on its own.
const GRAPH = {
  nodes: [1, 2, 3, 4, 5, 6].map((n) => node(n)),
  edges: [edge(2, 1), edge(3, 2), edge(4, 3), edge(5, 2, "produced_by")],
};
const ALL = { skipEdges: new Set<string>(), keep: () => true };
/** Each reached node's number, with its hops from the start. */
const hops = (reached: ReadonlyMap<string, number>) => Object.fromEntries([...reached].map(([id, far]) => [Number(id.slice(-3)), far]));

test("reach walks edges both ways and counts each node's hops from the start, the start at 0", () => {
  expect(hops(reach(GRAPH, uuid(2), ALL))).toEqual({ 2: 0, 1: 1, 3: 1, 5: 1, 4: 2 });
});

test("reach walks only the edge kinds not skipped", () => {
  expect(hops(reach(GRAPH, uuid(3), { ...ALL, skipEdges: new Set(["produced_by"]) }))).toEqual({ 3: 0, 2: 1, 4: 1, 1: 2 });
});

test("reach neither counts nor walks through a node it does not keep", () => {
  const keep = (row: { seq: number }) => row.seq !== 2;
  expect(hops(reach(GRAPH, uuid(3), { ...ALL, keep }))).toEqual({ 3: 0, 4: 1 });
  // A start it does not keep, or one the graph lacks, reaches nothing.
  expect(reach(GRAPH, uuid(2), { ...ALL, keep }).size).toBe(0);
  expect(reach(GRAPH, uuid(9), ALL).size).toBe(0);
});

test("hop counts are how many nodes lie within 1, 2 and 3 hops, the start included, and in the whole connected part", () => {
  expect(hopCounts(reach(GRAPH, uuid(1), ALL))).toEqual({ 1: 2, 2: 4, 3: 5, all: 5 });
  expect(hopCounts(reach(GRAPH, uuid(6), ALL))).toEqual({ 1: 1, 2: 1, 3: 1, all: 1 });
});

test("a node lies within a focus's hops when it was reached that near, or reached at all for All", () => {
  expect([0, 1, 2].map((far) => isWithin(far, 1))).toEqual([true, true, false]);
  expect(isWithin(40, "all")).toBe(true);
  expect(isWithin(undefined, "all")).toBe(false);
});
