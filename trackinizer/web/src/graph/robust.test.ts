// Every producer of the graph's shape, not only this build's server.
//
// The view read the server it ships with as a contract: a date is always there
// and parseable, an edge always names two records in the window. Fed a graph
// from another producer — a bench run's export, an older server, a window that
// moved between two queries — it did not degrade, it threw, and one missing
// field put a crash screen over the whole app.
//
// So each of these is a shape some producer can send, and the rule is the same
// for all of them: draw what can be drawn, leave out what cannot, never throw.
import { expect, test } from "vitest";
import type { Graph, GraphNode } from "../api/graph";
import { islandHomes } from "./islands";
import * as brief from "./briefing";
import { binsOf } from "./protoChrome";
import { findRoots } from "./roots";
import { GraphModel } from "./model";
import { PALETTE } from "./testing";

/** A record with every field present, to break one at a time. */
const whole = (over: Partial<GraphNode> = {}): GraphNode => ({
  id: "00000000-0000-4000-8000-000000000001", kind: "Belief", seq: 1, title: "A claim",
  status: "active", created: "2026-09-20T01:00:00+00:00", owner: "ada@example.com",
  judgement: "proven", confidence: 0.8, cost_agent_usd: 1, cost_resource_usd: 2,
} as GraphNode & typeof over);

const other = (id: string, over: Partial<GraphNode> = {}) => ({ ...whole(), id, seq: 2, ...over }) as GraphNode;
const ID2 = "00000000-0000-4000-8000-000000000002";

const CASES: [string, Graph][] = [
  ["empty", { nodes: [], edges: [] }],
  ["one node, no edges", { nodes: [whole()], edges: [] }],
  ["edges pointing at records that are not here", { nodes: [whole()], edges: [
    { from_id: "missing-a", to_id: "missing-b", edge_kind: "narrows", valence: 0.5, asserted: "2026-09-20T01:00:00+00:00", actor: "x" } as Graph["edges"][number]] }],
  ["an edge from a record to itself", { nodes: [whole()], edges: [
    { from_id: whole().id, to_id: whole().id, edge_kind: "narrows", valence: 0.5, asserted: "2026-09-20T01:00:00+00:00", actor: "x" } as Graph["edges"][number]] }],
  ["a cycle", { nodes: [whole(), other(ID2)], edges: [
    { from_id: whole().id, to_id: ID2, edge_kind: "narrows", valence: 0.5, asserted: "2026-09-20T01:00:00+00:00", actor: "x" },
    { from_id: ID2, to_id: whole().id, edge_kind: "narrows", valence: 0.5, asserted: "2026-09-20T01:00:00+00:00", actor: "x" }] as Graph["edges"] }],
  ["two records sharing one id", { nodes: [whole(), whole()], edges: [] }],
  ["no created", { nodes: [whole({ created: undefined as unknown as string })], edges: [] }],
  ["an unreadable created", { nodes: [whole({ created: "yesterday" as string })], edges: [] }],
  ["no title", { nodes: [whole({ title: undefined as unknown as string })], edges: [] }],
  ["an empty title", { nodes: [whole({ title: "" })], edges: [] }],
  ["no kind", { nodes: [whole({ kind: undefined as unknown as string })], edges: [] }],
  ["a kind this build has never heard of", { nodes: [whole({ kind: "Hypothesis" })], edges: [] }],
  ["no status", { nodes: [whole({ status: undefined as unknown as string })], edges: [] }],
  ["no owner", { nodes: [whole({ owner: undefined as unknown as string })], edges: [] }],
  ["no seq", { nodes: [whole({ seq: undefined as unknown as number })], edges: [] }],
  ["no judgement or confidence", { nodes: [whole({ judgement: undefined, confidence: undefined })], edges: [] }],
  ["a judgement this build has never heard of", { nodes: [whole({ judgement: "withdrawn" as never })], edges: [] }],
  ["no costs", { nodes: [whole({ cost_agent_usd: undefined, cost_resource_usd: undefined })], edges: [] }],
  ["negative costs", { nodes: [whole({ cost_agent_usd: -5, cost_resource_usd: -5 })], edges: [] }],
  ["an edge kind this build has never heard of", { nodes: [whole(), other(ID2)], edges: [
    { from_id: ID2, to_id: whole().id, edge_kind: "contradicts", valence: 0.5, asserted: "2026-09-20T01:00:00+00:00", actor: "x" }] as Graph["edges"] }],
  ["an edge with no valence or actor", { nodes: [whole(), other(ID2)], edges: [
    { from_id: ID2, to_id: whole().id, edge_kind: "proves" } as unknown as Graph["edges"][number]] }],
  ["a title longer than any panel", { nodes: [whole({ title: "x".repeat(20_000) })], edges: [] }],
  // Wired, and undated, so every path that ORDERS records runs: seven of them
  // compared the date strings directly and threw on the first one missing.
  ["a wired graph with no dates", undated()],
  ["a wired graph where only some records are dated", undated(true)],
];

/**
 * A claim, the papers for and against it, the experiment that produced it and
 * the issue it narrows: enough shape that the evidence, ledger, chain, impact,
 * turns and next-experiment paths all have something to sort. `some` dates half
 * of them, which is the shape a real export tends to have.
 */
function undated(some = false): Graph {
  const claim = whole();
  const rest: GraphNode[] = [];
  const edges: Graph["edges"][number][] = [];
  for (let n = 0; n < 12; n++) {
    const id = `00000000-0000-4000-8000-0000000001${String(n).padStart(2, "0")}`;
    rest.push(other(id, {
      kind: ["Paper", "Experiment", "Issue", "WebResult"][n % 4]!,
      seq: 100 + n,
      created: some && n % 2 === 0 ? "2026-09-20T01:00:00+00:00" : (undefined as unknown as string),
    }));
    edges.push({
      from_id: id, to_id: claim.id,
      edge_kind: ["proves", "favors", "produced_by", "narrows"][n % 4]!,
      valence: n % 3 === 0 ? 0.8 : 0.2,
      asserted: undefined as unknown as string, actor: "ada@example.com",
    } as Graph["edges"][number]);
  }
  return { nodes: [{ ...claim, created: undefined as unknown as string }, ...rest], edges };
}

test.each(CASES)("the graph survives %s", (_name, graph) => {
  // Nothing in the chain may throw, whatever a producer of this shape sends.
  const { homes } = islandHomes(graph);
  for (const node of graph.nodes) expect(homes.has(node.id)).toBe(true);
  expect(() => binsOf(graph)).not.toThrow();
  const view = brief.viewOf(graph);
  const roots = findRoots(graph);
  expect(() => new GraphModel(PALETTE).apply(graph)).not.toThrow();
  for (const node of graph.nodes) {
    expect(() => brief.costOf(node)).not.toThrow();
    expect(() => brief.shareOf(node, view)).not.toThrow();
    expect(() => brief.chainOf(node, view)).not.toThrow();
    expect(() => brief.impactOf(node, view)).not.toThrow();
    expect(() => brief.groupsOf(node, view)).not.toThrow();
    expect(() => brief.ledgerOf(node, view)).not.toThrow();
    expect(() => brief.evidenceOf(node, view)).not.toThrow();
    expect(() => brief.loadOf(node, view)).not.toThrow();
  }
  expect(() => brief.verdictsOf(graph.nodes)).not.toThrow();
  expect(() => brief.testPriceOf(graph.nodes, view)).not.toThrow();
  expect(() => brief.spendOf(graph.nodes, view)).not.toThrow();
  for (const group of roots.groups) {
    expect(() => brief.membersOf(group, view)).not.toThrow();
    expect(() => brief.turnsOf(group, view)).not.toThrow();
    expect(() => brief.nextExperiments(group, view)).not.toThrow();
  }
  expect(() => brief.reliedOn(graph.nodes, view)).not.toThrow();
});
