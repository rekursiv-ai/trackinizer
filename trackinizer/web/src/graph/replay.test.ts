import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { type Graph, type GraphNode } from "../api/graph";
import { replay } from "./replay";
import { edge, node } from "./testing";

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

/** A field of `count` records, a minute apart, oldest first. */
function field(count: number): Graph {
  const nodes: GraphNode[] = Array.from({ length: count }, (_, index) => node(index + 1, { created: madeAt(index) }));
  return { nodes, edges: count > 1 ? [edge(2, 1)] : [] };
}

const madeAt = (min: number) => new Date(Date.UTC(2026, 8, 20, 0, min)).toISOString().replace("Z", "+00:00");

/** Run a sweep, recording each frame's reach; returns what it saw and how to stop it. */
function sweep(graph: Graph) {
  const reached: number[] = [];
  let done = 0;
  const cancel = replay(graph, {
    step: (fraction) => reached.push(fraction),
    done: () => {
      done += 1;
    },
  });
  return { reached, done: () => done, cancel };
}

test("the sweep reaches the record made at the time it has crossed, and the whole field at its end", () => {
  const { reached, done } = sweep(field(3));
  // The design's own length for a small field: 14 s x 3/2500 + 4 s, so 4.017 s.
  vi.advanceTimersByTime(2008);
  expect(reached.at(-1)).toBeCloseTo(0.5, 1);
  expect(done()).toBe(0);
  vi.advanceTimersByTime(2100);
  expect(reached.at(-1)).toBe(1);
  expect(done()).toBe(1);
  // Never reached, and never stepping, after the end.
  vi.advanceTimersByTime(5000);
  expect(reached.at(-1)).toBe(1);
  expect(done()).toBe(1);
});

test("5,000 records cross in the design's 18 seconds, not one timer step apiece (F10)", () => {
  const { reached, done } = sweep(field(5000));
  vi.advanceTimersByTime(9000);
  expect(reached.at(-1)).toBeCloseTo(0.5, 1);
  expect(done()).toBe(0);
  vi.advanceTimersByTime(9100);
  expect(reached.at(-1)).toBe(1);
  expect(done()).toBe(1);
});

test("every frame speaks, so the frontier moves smoothly rather than in jumps", () => {
  const { reached } = sweep(field(5000));
  vi.advanceTimersByTime(1000);
  // A frame every 16 ms, and none of them saying less than the frame before.
  expect(reached.length).toBeGreaterThan(50);
  expect(reached.some((fraction, index) => index > 0 && fraction < reached[index - 1]!)).toBe(false);
});

test("cancelled, it stops where it is", () => {
  const { reached, done, cancel } = sweep(field(5000));
  vi.advanceTimersByTime(1000);
  const at = reached.length;
  cancel();
  vi.advanceTimersByTime(30_000);
  expect(reached).toHaveLength(at);
  expect(done()).toBe(0);
});
