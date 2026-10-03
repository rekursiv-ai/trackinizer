import { expect, test } from "vitest";
import type { Detail, Peer } from "../api/detail";
import { loadSubgraph } from "./Subgraph";

function detail(id: string, kind: string, parents: readonly Peer[] = []): Detail {
  return {
    self: { id, kind, seq: Number(id.replace(/\D/g, "")) || 1, title: id,
      status: "active", created: "2026-01-01", modified: "2026-01-01" },
    edges: { narrows: parents }, backlinks: {}, changes: [],
  };
}

function issue(id: string): Peer {
  return { id, kind: "Issue", seq: Number(id.replace(/\D/g, "")), title: id, status: "active" };
}

test("loads an AgentSession and five Issue ancestor hops, without fetching the sixth", async () => {
  const records = new Map([
    ["session", { ...detail("session", "AgentSession"), edges: { produced_by: [issue("issue1")] } }],
    ...Array.from({ length: 6 }, (_, index) => {
      const id = `issue${index + 1}`;
      return [id, detail(id, "Issue", [issue(`issue${index + 2}`)])] as const;
    }),
  ]);
  const fetched: string[] = [];
  const graph = await loadSubgraph("session", new AbortController().signal, async (id) => {
    fetched.push(id);
    const row = records.get(id);
    if (!row) throw new Error(`Unexpected fetch: ${id}`);
    return row;
  });

  expect(graph.entries.map(({ row }) => row.id)).toEqual([
    "session", "issue1", "issue2", "issue3", "issue4", "issue5",
  ]);
  expect(graph.links).toHaveLength(5);
  expect(fetched).not.toContain("issue5");
  expect(graph.links[0]).toEqual({ source: "session", target: "issue1", kind: "produced_by" });
});

test("caps the graph at thirty records and fetches no more than four at once", async () => {
  const parents = Array.from({ length: 40 }, (_, index) => issue(`issue${index + 1}`));
  let active = 0;
  let peak = 0;
  let fetched = 0;
  const graph = await loadSubgraph("session", new AbortController().signal, async (id) => {
    active++;
    peak = Math.max(peak, active);
    fetched++;
    await Promise.resolve();
    active--;
    return id === "session"
      ? { ...detail("session", "AgentSession"), edges: { produced_by: parents } }
      : detail(id, "Issue");
  });

  expect(graph.entries).toHaveLength(30);
  expect(graph.links).toHaveLength(29);
  expect(fetched).toBe(30);
  expect(peak).toBe(4);
});

test("follows Issue narrows lineage without pulling every provenance branch", async () => {
  const records = new Map<string, Detail>([
    ["session", { ...detail("session", "AgentSession"), edges: { produced_by: [issue("issue21709")] } }],
    ["issue21709", { ...detail("issue21709", "Issue", [issue("issue21313")]),
      edges: { narrows: [issue("issue21313")], produced_by: [issue("issue21704"), issue("issue21700")] } }],
    ["issue21313", detail("issue21313", "Issue", [issue("issue20985")])],
    ["issue20985", detail("issue20985", "Issue", [issue("issue19688")])],
    ["issue19688", detail("issue19688", "Issue")],
  ]);
  const fetched: string[] = [];
  const graph = await loadSubgraph("session", new AbortController().signal, async (id) => {
    fetched.push(id);
    const row = records.get(id);
    if (!row) throw new Error(`Unexpected provenance fetch: ${id}`);
    return row;
  });

  expect(graph.entries.map(({ row }) => row.id)).toEqual([
    "session", "issue21709", "issue21313", "issue20985", "issue19688",
  ]);
  expect(fetched).not.toContain("issue21704");
  expect(fetched).not.toContain("issue21700");
});
