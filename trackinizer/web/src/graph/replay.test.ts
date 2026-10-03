import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { Graph } from "../api/graph";
import { replay } from "./replay";
import { edge, node } from "./testing";

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

// Sent 3, 1, 2 but made 1, 2, 3: replay goes by `created`, not the answer's order.
const GRAPH: Graph = {
  nodes: [node(3, { created: "2026-09-20T00:05:00+00:00" }), node(1, { kind: "Belief", judgement: "proven", confidence: 0.9 }), node(2)],
  edges: [edge(2, 1, "proves", { valence: 0.5 }), edge(3, 1)],
};

function record(speed = () => 1) {
  const steps: { seqs: number[]; edges: string[] }[] = [];
  let done = false;
  const cancel = replay(GRAPH, {
    speed,
    step: (grown) => steps.push({ seqs: grown.nodes.map((row) => row.seq), edges: grown.edges.map((row) => row.edge_kind) }),
    done: () => (done = true),
  });
  return { steps, isDone: () => done, cancel };
}

test("replay starts empty, then grows the graph oldest first at 70 ms a node, each edge once both its ends show", () => {
  const { steps, isDone } = record();
  expect(steps).toEqual([{ seqs: [], edges: [] }]);
  vi.advanceTimersByTime(69);
  expect(steps).toHaveLength(1);
  vi.advanceTimersByTime(1);
  expect(steps.at(-1)).toEqual({ seqs: [1], edges: [] });
  vi.advanceTimersByTime(70);
  expect(steps.at(-1)).toEqual({ seqs: [1, 2], edges: ["proves"] });
  expect(isDone()).toBe(false);
  vi.advanceTimersByTime(70);
  expect(steps.at(-1)).toEqual({ seqs: [1, 2, 3], edges: ["proves", "narrows"] });
  expect(isDone()).toBe(true);
});

test("each node keeps every field the graph sent, a Belief's judgement and confidence too (CR-08)", () => {
  let last: Graph | undefined;
  replay(GRAPH, { speed: () => 1, step: (grown) => (last = grown), done: () => {} });
  vi.advanceTimersByTime(70 * 3);
  expect(last!.nodes.find((row) => row.seq === 1)).toEqual(GRAPH.nodes[1]);
});

test("the speed is read before each node, so a change takes effect at the next; 10x waits 16 ms, a frame", () => {
  let speed = 0.25;
  const { steps } = record(() => speed);
  vi.advanceTimersByTime(280);
  expect(steps).toHaveLength(2);
  speed = 10;
  vi.advanceTimersByTime(280);
  expect(steps).toHaveLength(3);
  vi.advanceTimersByTime(16);
  expect(steps).toHaveLength(4);
});

test("cancelled, it stops where it is", () => {
  const { steps, isDone, cancel } = record();
  vi.advanceTimersByTime(70);
  cancel();
  vi.advanceTimersByTime(1_000);
  expect(steps).toHaveLength(2);
  expect(isDone()).toBe(false);
});

test("replay orders `created` as instants: the server drops a zero fraction, so precision varies (GR-04)", () => {
  const grown: number[][] = [];
  const graph: Graph = {
    nodes: [node(1, { created: "2026-01-01T00:00:00+00:00" }), node(2, { created: "2026-01-01T00:00:00.000001+00:00" })],
    edges: [],
  };
  replay(graph, { speed: () => 1, step: (step) => grown.push(step.nodes.map((row) => row.seq)), done: () => {} });
  vi.advanceTimersByTime(70 * 2);
  expect(grown).toEqual([[], [1], [1, 2]]);
});
