import { expect, test } from "vitest";
import type { Graph, GraphNode } from "../api/graph";
import {
  FALLBACK_TEST_USD,
  type GraphView,
  nextExperiments,
  shareOf,
  spendOf,
  testPriceOf,
  viewOf,
} from "./briefing";
import { type RootGroup } from "./roots";
import { edge, node } from "./testing";

/** The whole graph as one group, which is how the home briefing ranks it. */
function groupOf(nodes: readonly GraphNode[]): RootGroup {
  return {
    key: "*",
    root: null,
    members: nodes.map((row) => row.id),
    newest: nodes.at(-1)?.created ?? "",
    kinds: new Map(),
  };
}

/** A thread that ends on an open, contested claim whose experiment paid `price`. */
function thread(price: number): { graph: Graph; view: GraphView } {
  const nodes = [
    node(1),
    node(2, { kind: "Experiment", cost_agent_usd: price }),
    node(3, { kind: "Belief", judgement: "unproven", confidence: 0.5 }),
    node(4, { kind: "Paper" }),
    node(5, { kind: "Paper" }),
  ];
  const edges = [edge(2, 1, "produced_by"), edge(4, 3, "proves", { valence: 1 }), edge(5, 3, "proves", { valence: -1 })];
  const graph = { nodes, edges };
  return { graph, view: viewOf(graph) };
}

test("a producer's cost is shared among everything it produced", () => {
  // The Issue spent 6 and produced two records; each carries 3 of it on top of its own.
  const nodes = [
    node(1, { cost_agent_usd: 4, cost_resource_usd: 2 }),
    node(2, { kind: "Experiment", cost_agent_usd: 2 }),
    node(3, { kind: "Artifact" }),
  ];
  const view = viewOf({ nodes, edges: [edge(2, 1, "produced_by"), edge(3, 1, "produced_by")] });
  expect(shareOf(nodes[1]!, view)).toBeCloseTo(5);
  expect(shareOf(nodes[2]!, view)).toBeCloseTo(3);
  // A record with nothing above it owes only its own line.
  expect(shareOf(nodes[0]!, view)).toBeCloseTo(6);
});

test("a thread's test price is its experiments' median, and an unpriced thread says so", () => {
  const nodes = [
    node(1, { kind: "Experiment", cost_agent_usd: 1 }),
    node(2, { kind: "Experiment", cost_agent_usd: 2 }),
    node(3, { kind: "Experiment", cost_agent_usd: 9 }),
    node(4, { kind: "Issue" }),
  ];
  const view = viewOf({ nodes, edges: [] });
  expect(testPriceOf(nodes, view)).toEqual({ usd: 2, priced: true });
  // Nothing spent, or no experiment at all: the design's standing figure, marked as assumed.
  expect(testPriceOf([node(4)], view)).toEqual({ usd: FALLBACK_TEST_USD, priced: false });
  expect(testPriceOf([node(1)], viewOf({ nodes: [node(1)], edges: [] }))).toEqual({
    usd: FALLBACK_TEST_USD,
    priced: false,
  });
});

test("the same claim in a cheaper thread is worth settling first", () => {
  const cheap = thread(1);
  const dear = thread(100);
  const [cheapRow] = nextExperiments(groupOf(cheap.graph.nodes), cheap.view);
  const [dearRow] = nextExperiments(groupOf(dear.graph.nodes), dear.view);
  // Identical stakes: undecided claim, two citations, nothing leaning on it.
  expect(cheapRow?.evidence).toBe(2);
  expect(dearRow?.evidence).toBe(2);
  // One dollar's test is worth ten of them, so the cheap thread's score is ten times higher.
  expect(cheapRow!.score / dearRow!.score).toBeCloseTo(10);
  expect(cheapRow!.price).toEqual({ usd: 1, priced: true });
  expect(dearRow!.price).toEqual({ usd: 100, priced: true });
});

test("an unpriced thread ranks on its stakes and says the price is assumed", () => {
  const { graph, view } = thread(0);
  const [row] = nextExperiments(groupOf(graph.nodes), view);
  expect(row!.price).toEqual({ usd: FALLBACK_TEST_USD, priced: false });
  // The fallback still divides the score, so every unpriced thread ranks the same way.
  expect(row!.score).toBeCloseTo((1 * 1.5 * Math.log2(2 + 2)) / Math.sqrt(FALLBACK_TEST_USD));
});

test("spend totals both axes and prices only the verdicts its own proofs settled", () => {
  const nodes = [
    node(1, { kind: "Experiment", cost_agent_usd: 0.4, cost_resource_usd: 0.1 }),
    node(2, { kind: "Experiment", cost_resource_usd: 0.5 }),
    node(3, { kind: "Belief", judgement: "proven" }),
    // Settled, but by a record the window does not hold, so its cost is not this bill's.
    node(4, { kind: "Belief", judgement: "disproven" }),
    node(5, { kind: "Belief", judgement: "unproven" }),
    node(6, { kind: "Paper" }),
  ];
  const edges = [
    edge(1, 3, "proves", { valence: 1 }),
    edge(2, 5, "favors"),
    edge(6, 4, "proves", { valence: -1 }),
  ];
  const window = [nodes[0]!, nodes[1]!, nodes[2]!, nodes[4]!];
  const spend = spendOf(window, viewOf({ nodes, edges }));
  expect(spend.agent).toBeCloseTo(0.4);
  expect(spend.resource).toBeCloseTo(0.6);
  expect(spend.total).toBeCloseTo(1);
  expect(spend.settled).toBe(1);
  expect(spend.perSettled).toBeCloseTo(1);
});

test("a bill with nothing settled claims no cost per verdict", () => {
  const nodes = [node(1, { kind: "Experiment", cost_agent_usd: 2 }), node(2, { kind: "Belief", judgement: "proven" })];
  const spend = spendOf(nodes, viewOf({ nodes, edges: [] }));
  expect(spend.total).toBe(2);
  expect(spend.settled).toBe(0);
  expect(spend.perSettled).toBeNull();
});

test("a graph with no cost at all reads as free, not as unmeasured", () => {
  const nodes = [node(1, { kind: "Experiment" }), node(2, { kind: "Belief", judgement: "proven" })];
  const edges = [edge(1, 2, "proves", { valence: 1 })];
  const spend = spendOf(nodes, viewOf({ nodes, edges }));
  expect(spend).toMatchObject({ agent: 0, resource: 0, total: 0, perSettled: 0 });
  // A settled verdict counts even against an empty bill: the panel reads $0 per
  // verdict, which is what was spent, rather than the dash a void window gets.
  expect(spend.settled).toBe(1);
});
