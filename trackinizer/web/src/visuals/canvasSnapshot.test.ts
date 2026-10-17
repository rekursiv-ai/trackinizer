import { expect, test } from "vitest";
import type { WorkspaceState } from "../api/workspaces";
import { HOME_ROUTE, homeSnapshot, PAGE, planRestore, type Snapshot, snapshotKey, snapshotOf } from "./canvasSnapshot";

type Visual = WorkspaceState["visuals"][number];

const page: Visual = { id: "p", type: PAGE, version: 1, placement: "main", record_id: null, params: {} };
const graph: Visual = { id: "g", type: "trax.subgraph", version: 1, placement: "side", record_id: "r1", params: { hops: 2 } };

test("a snapshot names the page's route while the page is shown, and every visual's place, record and params", () => {
  expect(snapshotOf([page, graph], "#/list/Issue")).toEqual({
    page: "#/list/Issue",
    visuals: [
      { type: PAGE, placement: "main", record_id: null, params: {} },
      { type: "trax.subgraph", placement: "side", record_id: "r1", params: { hops: 2 } },
    ],
  });
  expect(snapshotOf([graph], "#/list/Issue")?.page).toBeNull();
});

test.each(["#/settings", "#/admin/users", "#/new/Issue", "", "#/has space"])("no snapshot is taken while %j lies over the page", (hash) => {
  expect(snapshotOf([page], hash)).toBeNull();
});

test("the page's address does not matter while the page is closed", () => {
  expect(snapshotOf([graph], "#/settings")).toEqual({ page: null, visuals: [expect.objectContaining({ type: "trax.subgraph" })] });
});

test("Home is the page on the graph, or nothing", () => {
  expect(homeSnapshot("graph")).toEqual({ page: HOME_ROUTE, visuals: [{ type: PAGE, placement: "main", record_id: null, params: {} }] });
  expect(homeSnapshot("empty")).toEqual({ page: null, visuals: [] });
});

test("two snapshots of the same canvas have one key, whatever order their visuals and params came in", () => {
  const a = snapshotOf([page, { ...graph, params: { hops: 2, highlight: "x" } }], "#/graph")!;
  const b = snapshotOf([{ ...graph, params: { highlight: "x", hops: 2 } }, page], "#/graph")!;
  expect(snapshotKey(a)).toBe(snapshotKey(b));
  expect(snapshotKey(a)).not.toBe(snapshotKey({ ...a, page: "#/activity" }));
  expect(snapshotKey(a)).not.toBe(snapshotKey(snapshotOf([page, { ...graph, placement: "left" }], "#/graph")!));
});

test("a restore hides what the target lacks and shows what it misses, with its place, record and params", () => {
  const target: Snapshot = { page: "#/graph", visuals: [
    { type: PAGE, placement: "main", record_id: null, params: {} },
    { type: "trax.timeline", placement: "left", record_id: "r2", params: {} },
  ] };
  expect(planRestore([page, graph], target)).toEqual([
    { kind: "hide", instance_id: "g" },
    { kind: "show", visual_type: "trax.timeline", placement: "left", record_id: "r2", params: {} },
  ]);
});

test("a visual the target shows elsewhere is placed; on another record or with other params it is shown anew", () => {
  const moved = snapshotOf([page, { ...graph, placement: "floating" }], "#/graph")!;
  expect(planRestore([page, graph], moved)).toEqual([{ kind: "place", instance_id: "g", placement: "floating" }]);
  const recentred = snapshotOf([page, { ...graph, record_id: "r9" }], "#/graph")!;
  expect(planRestore([page, graph], recentred)).toEqual([
    { kind: "show", visual_type: "trax.subgraph", placement: "side", record_id: "r9", params: { hops: 2 } },
  ]);
  const reached = snapshotOf([page, { ...graph, params: { hops: 3 } }], "#/graph")!;
  expect(planRestore([page, graph], reached)).toEqual([
    { kind: "show", visual_type: "trax.subgraph", placement: "side", record_id: "r1", params: { hops: 3 } },
  ]);
});

test("a canvas that already shows the target needs no operation, and an empty target hides everything", () => {
  expect(planRestore([page, graph], snapshotOf([graph, page], "#/x")!)).toEqual([]);
  expect(planRestore([page, graph], homeSnapshot("empty"))).toEqual([
    { kind: "hide", instance_id: "p" },
    { kind: "hide", instance_id: "g" },
  ]);
});
