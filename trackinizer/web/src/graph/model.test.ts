import { expect, test } from "vitest";
import { GraphModel, PLAIN_LENS } from "./model";
import { edge, node, PALETTE, uuid } from "./testing";

const LENS = PLAIN_LENS;
const byId = (model: GraphModel, n: number) => model.nodes.find((drawn) => drawn.id === uuid(n))!;
const keys = (model: GraphModel) => model.links.map((link) => `${link.from.slice(-1)}>${link.to.slice(-1)} ${link.kind}`);

test("an apply keeps one object per inquiry, where the layout put it, with the new fields", () => {
  const model = new GraphModel(PALETTE);
  expect(model.apply({ nodes: [node(1), node(2)], edges: [edge(2, 1)] })).toBe(true);
  const first = byId(model, 1);
  Object.assign(first, { x: 10, y: 20 });
  const changed = model.apply({ nodes: [node(1, { title: "Renamed", status: "complete" }), node(2)], edges: [edge(2, 1)] });
  expect(changed).toBe(false);
  expect(byId(model, 1)).toBe(first);
  expect(first).toMatchObject({ x: 10, y: 20, title: "Renamed", status: "complete" });
  expect(model.find(uuid(1))).toBe(first);
  expect(model.find(uuid(9))).toBeUndefined();
});

test("a Belief's judgement and confidence go when the answer no longer has them", () => {
  const model = new GraphModel(PALETTE);
  model.apply({ nodes: [node(1, { kind: "Belief", judgement: "proven", confidence: 0.9 })], edges: [] });
  model.apply({ nodes: [node(1, { kind: "Belief" })], edges: [] });
  expect(byId(model, 1).judgement).toBeUndefined();
  expect(byId(model, 1).confidence).toBeUndefined();
});

test("an inquiry the answer drops leaves, with its edges, and that changes the drawing's shape", () => {
  const model = new GraphModel(PALETTE);
  model.apply({ nodes: [node(1), node(2), node(3)], edges: [edge(2, 1), edge(3, 1)] });
  expect(model.apply({ nodes: [node(1), node(2)], edges: [edge(2, 1)] })).toBe(true);
  expect(model.nodes.map((drawn) => drawn.id)).toEqual([uuid(1), uuid(2)]);
  expect(keys(model)).toEqual(["2>1 narrows"]);
});

test("an edge added or removed between the same inquiries changes the shape", () => {
  const model = new GraphModel(PALETTE);
  model.apply({ nodes: [node(1), node(2)], edges: [edge(2, 1)] });
  expect(model.apply({ nodes: [node(1), node(2)], edges: [edge(2, 1), edge(2, 1, "requires")] })).toBe(true);
  expect(keys(model)).toEqual(["2>1 narrows", "2>1 requires"]);
  expect(model.apply({ nodes: [node(1), node(2)], edges: [edge(2, 1, "requires")] })).toBe(true);
  expect(keys(model)).toEqual(["2>1 requires"]);
});

test("a link keeps its object across applies, and a valence change restyles that object", () => {
  const model = new GraphModel(PALETTE);
  const nodes = [node(1, { kind: "Belief" }), node(2, { kind: "Paper" })];
  model.apply({ nodes, edges: [edge(2, 1, "proves", { valence: 0.8 })] });
  const link = model.links[0]!;
  // The layout swaps a link's ends for their nodes once it reads them.
  Object.assign(link, { source: byId(model, 2), target: byId(model, 1) });
  expect(model.apply({ nodes, edges: [edge(2, 1, "proves", { valence: -0.5 })] })).toBe(false);
  expect(model.links[0]).toBe(link);
  expect(link.source).toBe(byId(model, 2));
  expect(link.valence).toBe(-0.5);
  expect(link.look.color).toBe("against");
});

test("a node counts the distinct inquiries it shares an edge with", () => {
  const model = new GraphModel(PALETTE);
  model.apply({ nodes: [node(1), node(2), node(3)], edges: [edge(2, 1), edge(2, 1, "produced_by"), edge(3, 2, "requires")] });
  expect([1, 2, 3].map((n) => byId(model, n).degree)).toEqual([1, 2, 1]);
});

test("a root is an Issue that narrows nothing; requires makes no parent (S6-03)", () => {
  const model = new GraphModel(PALETTE);
  model.apply({
    nodes: [node(1), node(2), node(3), node(4, { kind: "Paper" })],
    edges: [edge(2, 1), edge(3, 1, "requires")],
  });
  expect([1, 2, 3, 4].map((n) => byId(model, n).root)).toEqual([true, false, true, false]);
});

test("the first answer places nothing: the layout does", () => {
  const model = new GraphModel(PALETTE);
  model.apply({ nodes: [node(1), node(2)], edges: [edge(2, 1)] });
  expect(model.nodes.map((drawn) => [drawn.x, drawn.y])).toEqual([
    [undefined, undefined],
    [undefined, undefined],
  ]);
});

test("a node that arrives later lands at rest beside a placed neighbour, and the placed ones slow down", () => {
  const model = new GraphModel(PALETTE);
  model.apply({ nodes: [node(1), node(2)], edges: [edge(2, 1)] });
  Object.assign(byId(model, 1), { x: 100, y: -50, vx: 4, vy: -8 });
  Object.assign(byId(model, 2), { x: -300, y: 300, vx: 0, vy: 0 });
  model.apply({ nodes: [node(1), node(2), node(3)], edges: [edge(2, 1), edge(3, 1)] });
  const added = byId(model, 3);
  expect(Math.hypot(added.x! - 100, added.y! + 50)).toBeLessThan(60);
  expect([added.vx, added.vy]).toEqual([0, 0]);
  expect([byId(model, 1).vx, byId(model, 1).vy]).toEqual([1, -2]);
});

test("a node that arrives with no placed neighbour lands near the middle of the placed ones", () => {
  const model = new GraphModel(PALETTE);
  model.apply({ nodes: [node(1), node(2)], edges: [] });
  Object.assign(byId(model, 1), { x: 0, y: 0 });
  Object.assign(byId(model, 2), { x: 200, y: 100 });
  model.apply({ nodes: [node(1), node(2), node(3)], edges: [] });
  expect(Math.hypot(byId(model, 3).x! - 100, byId(model, 3).y! - 50)).toBeLessThan(60);
});

test("showing hides the filtered kinds' and statuses' nodes, and every link with a hidden end", () => {
  const model = new GraphModel(PALETTE);
  model.apply({
    nodes: [node(1), node(2, { kind: "Paper" }), node(3), node(4, { status: "invalid" })],
    edges: [edge(2, 1, "produced_by"), edge(3, 1), edge(4, 3)],
  });
  model.show(PALETTE, { ...LENS, hiddenKinds: new Set(["Paper"]), hiddenStatuses: new Set(["invalid"]) });
  expect([1, 2, 3, 4].map((n) => byId(model, n).hidden)).toEqual([false, true, false, true]);
  expect(model.links.map((link) => link.hidden)).toEqual([true, false, true]);
  model.show({ ...PALETTE, kinds: { Issue: "new-issue-hue" } }, LENS);
  expect(byId(model, 2).hidden).toBe(false);
  expect(byId(model, 1).look.fill).toBe("new-issue-hue");
});

// 1 <- 2 <- 3 <- 4 by narrows, and 5 produced by 3.
const CHAIN = { nodes: [1, 2, 3, 4, 5].map((n) => node(n)), edges: [edge(2, 1), edge(3, 2), edge(4, 3), edge(5, 3, "produced_by")] };
const ringAlphas = (model: GraphModel) => model.nodes.map((drawn) => drawn.look.ringAlpha);
const linkColors = (model: GraphModel) => model.links.map((link) => link.look.color);

test("a light dims what it leaves out, fades what is two or more hops out, and lights links between lit nodes, across applies", () => {
  const model = new GraphModel(PALETTE);
  model.apply(CHAIN);
  model.show(PALETTE, { ...LENS, lit: new Map([[uuid(2), 0], [uuid(1), 1], [uuid(3), 1], [uuid(4), 2]]) });
  expect(ringAlphas(model)).toEqual([1, 1, 1, 0.7, 0.14]);
  expect(linkColors(model)).toEqual(["narrows-hue", "narrows-hue", "narrows-hue", "faint"]);
  model.apply({ ...CHAIN, nodes: CHAIN.nodes.map((row) => ({ ...row, title: "Renamed" })) });
  expect(ringAlphas(model)).toEqual([1, 1, 1, 0.7, 0.14]);
  model.show(PALETTE, LENS);
  expect(ringAlphas(model)).toEqual([1, 1, 1, 1, 1]);
});

test("a light dims a link of a kind it does not walk, even between lit nodes", () => {
  const model = new GraphModel(PALETTE);
  model.apply(CHAIN);
  model.show(PALETTE, { ...LENS, lit: new Map([[uuid(3), 0], [uuid(5), 1], [uuid(2), 1]]), skipEdges: new Set(["narrows"]) });
  expect(linkColors(model)).toEqual(["faint", "faint", "faint", "fallback"]);
});

test("Only these hides what the light leaves out, with its links, rather than dimming it", () => {
  const model = new GraphModel(PALETTE);
  model.apply(CHAIN);
  model.show(PALETTE, { ...LENS, lit: new Map([[uuid(1), 0], [uuid(2), 1]]), only: true });
  expect(model.nodes.map((drawn) => drawn.hidden)).toEqual([false, false, true, true, true]);
  expect(model.links.map((link) => link.hidden)).toEqual([false, true, true, true]);
  expect(ringAlphas(model).slice(0, 2)).toEqual([1, 1]);
});

test("the focus and the selection get the strong halo, search matches the match halo", () => {
  const model = new GraphModel(PALETTE);
  model.apply(CHAIN);
  model.show(PALETTE, { ...LENS, strong: new Set([uuid(1)]), matches: new Set([uuid(1), uuid(4)]) });
  expect(model.nodes.map((drawn) => drawn.look.halo)).toEqual(["halo", null, null, "match", null]);
});

test("the nodes the lens labels carry their titles as labels; the rest none", () => {
  const model = new GraphModel(PALETTE);
  model.apply({ nodes: [node(1), node(2, { title: "" })], edges: [] });
  model.show(PALETTE, { ...LENS, labelled: new Set([uuid(1), uuid(2)]) });
  expect(model.nodes.map((drawn) => drawn.look.label?.text)).toEqual(["Issue 1", "(untitled)"]);
  model.show(PALETTE, LENS);
  expect(model.nodes.map((drawn) => drawn.look.label)).toEqual([null, null]);
});
