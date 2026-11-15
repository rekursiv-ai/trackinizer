import { expect, test } from "vitest";
import type { GraphEdge, GraphNode } from "../api/graph";
import { islandHomes } from "./islands";
import { edge, node, uuid } from "./testing";

/** A family of `size` records: a root and everything else narrowing it, numbered from `at`. */
function ball(at: number, size: number): { nodes: GraphNode[]; edges: GraphEdge[]; root: string } {
  const nodes = [node(at)];
  const edges: GraphEdge[] = [];
  for (let n = 1; n < size; n++) {
    nodes.push(node(at + n));
    edges.push(edge(at + n, at));
  }
  return { nodes, edges, root: uuid(at) };
}

/** A family of `size` as a chain, so its depths are a spread the split can band. */
function chain(at: number, size: number): { nodes: GraphNode[]; edges: GraphEdge[]; root: string } {
  const nodes = [node(at)];
  const edges: GraphEdge[] = [];
  for (let n = 1; n < size; n++) {
    nodes.push(node(at + n));
    edges.push(edge(at + n, at + n - 1));
  }
  return { nodes, edges, root: uuid(at) };
}

/** The islands the grouping drew, biggest first, as `[key, size]`. */
function drawn(homes: Map<string, string>): [string, number][] {
  const size = new Map<string, number>();
  for (const key of homes.values()) size.set(key, (size.get(key) ?? 0) + 1);
  return [...size].sort((a, b) => b[1] - a[1]);
}

test("two lines of inquiry big enough to read stay two islands, each named by its own head", () => {
  const a = ball(100, 30);
  const b = ball(200, 30);
  const { homes, headless } = islandHomes({ nodes: [...a.nodes, ...b.nodes], edges: [...a.edges, ...b.edges] });
  expect(drawn(homes)).toEqual([
    [a.root, 30],
    [b.root, 30],
  ]);
  // Both are headed, so both may wear their head's title.
  expect([...headless]).toEqual([]);
});

test("a family too small to read as an island joins the one its records touch most", () => {
  const near = ball(100, 30);
  const far = ball(200, 30);
  const speck = ball(300, 5);
  // Citations, not kinship: they leave the families apart but make the speck touch.
  const touches = [
    edge(300, 100, "proves"),
    edge(301, 200, "proves"),
    edge(302, 200, "proves"),
    edge(303, 200, "proves"),
  ];
  const { homes } = islandHomes({
    nodes: [...near.nodes, ...far.nodes, ...speck.nodes],
    edges: [...near.edges, ...far.edges, ...speck.edges, ...touches],
  });
  // Three touches to `far` against one to `near`, so the speck goes to `far`.
  expect(homes.get(uuid(300))).toBe(far.root);
  expect(drawn(homes)).toEqual([
    [far.root, 35],
    [near.root, 30],
  ]);
});

test("a speck that touches nothing joins the island whose records were alive when it happened", () => {
  // No edge leaves it, so there is no neighbour to count -- only when it happened.
  const early = ball(100, 30);
  const late = ball(500, 30);
  const speck = ball(300, 5);
  const { homes } = islandHomes({
    nodes: [...early.nodes, ...late.nodes, ...speck.nodes],
    edges: [...early.edges, ...late.edges, ...speck.edges],
  });
  // `early` ends at minute 129 and the speck starts at 300; `late` starts at 500.
  expect(homes.get(uuid(300))).toBe(early.root);
});

test("a produced_by the server hung beside a requires does not sew two inquiries into one", () => {
  // The trap this rule exists for: the server writes a produced_by next to every
  // requires between two Issues, and a requires across lines of inquiry is normal.
  // Counting the companion as kinship would make the two families one.
  const a = ball(100, 30);
  const b = ball(200, 30);
  const across = [edge(200, 100, "requires"), edge(200, 100, "produced_by")];
  const { homes } = islandHomes({
    nodes: [...a.nodes, ...b.nodes],
    edges: [...a.edges, ...b.edges, ...across],
  });
  expect(drawn(homes)).toEqual([
    [a.root, 30],
    [b.root, 30],
  ]);
});

test("a produced_by standing on its own is kinship, and makes the two one family", () => {
  const a = ball(100, 30);
  const b = ball(200, 30);
  const { homes } = islandHomes({
    nodes: [...a.nodes, ...b.nodes],
    edges: [...a.edges, ...b.edges, edge(200, 100, "produced_by")],
  });
  // One family now, crowned by the elder head: b hangs off a.
  expect(drawn(homes)).toEqual([[a.root, 60]]);
});

test("a ball of records nobody built on may not wear one of their titles as the inquiry's", () => {
  // Loose satellites: parentless, but heading nothing. They are gathered into an
  // island because they happened, not because they are a line of inquiry.
  const nodes = Array.from({ length: 30 }, (_, n) => node(100 + n, { kind: "Paper" }));
  const { homes, headless } = islandHomes({ nodes, edges: [] });
  const [[key]] = drawn(homes);
  expect(headless.has(key!)).toBe(true);
});

test("a family that would be most of the window is cut into bands of its own spread", () => {
  // 300 records, all one chain: the share leaves a cap of 240, so it is cut in two.
  const one = chain(100, 300);
  const { homes } = islandHomes(one);
  const islands = drawn(homes);
  expect(islands).toHaveLength(2);
  expect(islands.map(([, size]) => size)).toEqual([150, 150]);
  // Each band is a run of the chain, not a scatter: the first holds the head.
  expect(homes.get(one.root)).toBe(islands.find(([key]) => key === one.root)?.[0]);
});

test("the same family in a wider window is left whole, because the cap is a share of it", () => {
  // The point of the share: 300 was too much of a 300-record window and is a
  // comfortable part of a 1,000-record one, so widening the window stops the cut
  // rather than a constant deciding it against some other graph's biggest thread.
  const one = chain(100, 300);
  // Three more threads, each comfortably under the cap the four of them set.
  const rest = [ball(1_000, 150), ball(2_000, 150), ball(3_000, 150)];
  const { homes } = islandHomes({
    nodes: [...one.nodes, ...rest.flatMap((each) => each.nodes)],
    edges: [...one.edges, ...rest.flatMap((each) => each.edges)],
  });
  // A 750-record window caps an island at 300, so the chain fits exactly whole
  // where the same 300 was cut in two when it was the only thread in view.
  expect(drawn(homes).map(([, size]) => size)).toEqual([300, 150, 150, 150]);
  expect([...homes.values()].filter((key) => key === one.root)).toHaveLength(300);
});

test("below the floor nothing is cut, so a narrow window of one inquiry stays one island", () => {
  // A share alone would band a window that is a single thread and nothing else,
  // inventing sub-inquiries that are not there.
  const only = chain(100, 240);
  const { homes } = islandHomes(only);
  expect(drawn(homes)).toEqual([[only.root, 240]]);
});
