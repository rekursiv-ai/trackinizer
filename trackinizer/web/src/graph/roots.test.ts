import { expect, test } from "vitest";
import type { GraphEdge, GraphNode } from "../api/graph";
import { findRoots, UNROOTED } from "./roots";
import { edge, node, uuid } from "./testing";

/** Each group as `<root seq or unrooted>: <size>`, in the order `findRoots` gives them. */
function groups(nodes: readonly GraphNode[], edges: readonly GraphEdge[]): string[] {
  return findRoots({ nodes, edges }).groups.map(({ root, members }) => `${root ? root.seq : "unrooted"}: ${members.length}`);
}

/** Node `n`'s layout group, as a seq or `unrooted`. */
function homeOf(nodes: readonly GraphNode[], edges: readonly GraphEdge[], n: number): string {
  const key = findRoots({ nodes, edges }).home.get(uuid(n));
  return key === UNROOTED ? "unrooted" : String(nodes.find((found) => found.id === key)?.seq);
}

test("a root is an Issue that narrows nothing; what narrows it, down the chain, and what those produced are its subgraph", () => {
  const nodes = [node(1), node(2), node(3), node(4, { kind: "Experiment" }), node(5, { kind: "Artifact" })];
  // 3 narrows 2 narrows 1; the Experiment came of 3, the Artifact of the Experiment.
  const edges = [edge(2, 1), edge(3, 2), edge(4, 3, "produced_by"), edge(5, 4, "produced_by")];
  const [group, ...rest] = findRoots({ nodes, edges }).groups;
  expect(rest).toEqual([]);
  expect(group!.root).toEqual(nodes[0]);
  expect(group!.key).toBe(uuid(1));
  expect(group!.members).toEqual([1, 2, 3, 4, 5].map(uuid));
  expect(group!.newest).toBe(nodes[4]!.created);
  expect(Object.fromEntries(group!.kinds)).toEqual({ Issue: 3, Experiment: 1, Artifact: 1 });
  for (const n of [1, 2, 3, 4, 5]) expect(homeOf(nodes, edges, n)).toBe("1");
});

test("an Issue with only products under it is a root; one with nothing under it is not", () => {
  const nodes = [node(1), node(2, { kind: "Experiment" }), node(3)];
  expect(groups(nodes, [edge(2, 1, "produced_by")])).toEqual(["1: 2", "unrooted: 1"]);
});

test("a node under two roots is listed under both and laid out with the nearer, or the newer when they are as near", () => {
  // Roots 1 and 2 (the newer). 5 narrows 1 and, through 6, 2: nearer 1.
  // 7 narrows both straight: as near, so the newer, 2.
  const nodes = [node(1), node(2), node(5), node(6), node(7)];
  const edges = [edge(5, 1), edge(5, 6), edge(6, 2), edge(7, 1), edge(7, 2)];
  const { under, home } = findRoots({ nodes, edges });
  expect(home.get(uuid(5))).toBe(uuid(1));
  expect(under.get(uuid(5))).toEqual([uuid(1), uuid(2)]);
  expect(home.get(uuid(7))).toBe(uuid(2));
  expect(under.get(uuid(7))).toEqual([uuid(2), uuid(1)]);
  // A root is under itself, first.
  expect(under.get(uuid(1))).toEqual([uuid(1)]);
  expect(groups(nodes, edges)).toEqual(["2: 4", "1: 3"]);
});

test("a root's products that are Issues bring what is under them, and the nearer root lays them out", () => {
  // 3 is a root of its own, made by 2 under root 1: under both, laid out with itself.
  const nodes = [node(1), node(2), node(3), node(4)];
  const edges = [edge(2, 1), edge(3, 2, "produced_by"), edge(4, 3)];
  const { under, home } = findRoots({ nodes, edges });
  expect(groups(nodes, edges)).toEqual(["3: 2", "1: 4"]);
  expect(under.get(uuid(4))).toEqual([uuid(3), uuid(1)]);
  expect(home.get(uuid(4))).toBe(uuid(3));
});

test("a cycle ends the walk; a cycle no root is above is unrooted", () => {
  // 2 and 3 narrow each other under root 1; 8 and 9 narrow each other alone.
  const nodes = [node(1), node(2), node(3), node(8), node(9)];
  const edges = [edge(2, 1), edge(3, 2), edge(2, 3), edge(8, 9), edge(9, 8)];
  expect(groups(nodes, edges)).toEqual(["1: 3", "unrooted: 2"]);
  // Two Issues that made each other are each a root, each under the other too.
  const made = [node(1), node(2)];
  const both = [edge(1, 2, "produced_by"), edge(2, 1, "produced_by")];
  expect(groups(made, both)).toEqual(["2: 2", "1: 2"]);
  expect(findRoots({ nodes: made, edges: both }).under.get(uuid(1))).toEqual([uuid(1), uuid(2)]);
});

test("other edges make no subgraph: what requires, proves, favors or supersedes a root's node stays out", () => {
  const nodes = [node(1), node(2), node(3), node(4, { kind: "Belief" }), node(5, { kind: "Experiment" }), node(6, { kind: "Paper" })];
  const edges = [edge(2, 1), edge(3, 2, "requires"), edge(5, 4, "proves"), edge(5, 2, "favors"), edge(6, 2, "supersedes")];
  expect(groups(nodes, edges)).toEqual(["1: 2", "unrooted: 4"]);
  expect(homeOf(nodes, edges, 5)).toBe("unrooted");
  expect(findRoots({ nodes, edges }).under.get(uuid(5))).toEqual([]);
});

test("only an Issue roots a subgraph: a Paper's results with no Issue above are unrooted", () => {
  const nodes = [node(1, { kind: "Paper" }), node(2, { kind: "WebResult" })];
  const [unrooted] = findRoots({ nodes, edges: [edge(2, 1, "produced_by")] }).groups;
  expect(unrooted!.key).toBe(UNROOTED);
  expect(unrooted!.root).toBeNull();
  expect(unrooted!.members).toEqual([uuid(1), uuid(2)]);
  expect(Object.fromEntries(unrooted!.kinds)).toEqual({ Paper: 1, WebResult: 1 });
});

test("roots come by their newest node, newest first, Unrooted last; an empty graph has none", () => {
  // Root 1 is older than root 2, but 9, under 1, is the newest node.
  const nodes = [node(1), node(2), node(3), node(9), node(10)];
  const edges = [edge(9, 1), edge(3, 2)];
  expect(groups(nodes, edges)).toEqual(["1: 2", "2: 2", "unrooted: 1"]);
  expect(findRoots({ nodes: [], edges: [] }).groups).toEqual([]);
});

test("times within one millisecond order to the microsecond the server keeps: the newer root comes first and lays out what both hold", () => {
  const at = (micros: string) => `2026-09-20T12:00:00.${micros}+00:00`;
  // The server sends nodes oldest first. 3 narrows both roots, and is the newest node under 1.
  const nodes = [node(1, { created: at("001100") }), node(3, { created: at("001500") }), node(2, { created: at("001900") })];
  const edges = [edge(3, 1), edge(3, 2)];
  const { groups: found, home } = findRoots({ nodes, edges });
  expect(found.map(({ root, newest }) => `${root!.seq}: ${newest}`)).toEqual([`2: ${at("001900")}`, `1: ${at("001500")}`]);
  expect(home.get(uuid(3))).toBe(uuid(2));
});
