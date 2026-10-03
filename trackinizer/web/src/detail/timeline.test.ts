import { expect, test } from "vitest";
import { kindFields } from "./fields";
import { change, detail, META, peer, row } from "./testing";
import { activityItems, describeChange, type Phrase } from "./timeline";

const ISSUE = row("Issue", 1);
const PARENT = peer("Issue", 2);
const CHILD = peer("Issue", 3);
const DETAIL = detail(ISSUE, { edges: { narrows: [PARENT] }, backlinks: { narrows: [CHILD] } });
const CONTEXT = { detail: DETAIL, topology: META.edges, fields: kindFields(ISSUE, META.fieldOwners) };

/** A phrase as text, each named inquiry as its ref or `Kind <id prefix>`. */
function text(phrase: Phrase): string {
  return phrase
    .map((part) => (typeof part === "string" ? part : part.peer ? `${part.kind}#${part.peer.seq}` : `${part.kind} ${part.id.slice(0, 8)}`))
    .join("");
}

function describe(fields: Parameters<typeof change>[2]): string {
  return text(describeChange(change(ISSUE, 1, fields), CONTEXT));
}

test("activity reads oldest first and folds each run of alerts into one item", () => {
  const alert = (n: number) => change(ISSUE, n, { kind: "dependency_changed" });
  const items = activityItems([alert(5), alert(4), change(ISSUE, 3, { kind: "status" }), alert(2), alert(1)]);
  expect(items.map((item) => (item.type === "change" ? item.change.kind : `${item.changes.length} alerts`))).toEqual([
    "2 alerts",
    "status",
    "2 alerts",
  ]);
  const last = items[2];
  expect(last.type === "alerts" && last.changes.map((c) => c.created)).toEqual([
    change(ISSUE, 4, {}).created,
    change(ISSUE, 5, {}).created,
  ]);
});

test("a field edit names the field as the detail labels it", () => {
  expect(describe({ kind: "issue_priority", old: { issue_priority: 20 }, new: { issue_priority: 0 } })).toBe(
    "changed Priority from 20 to 0",
  );
  expect(describe({ kind: "status", old: { status: "active" }, new: { status: "invalid" } })).toBe(
    "changed Status from active to invalid",
  );
  expect(describe({ kind: "owner", old: {}, new: { owner: "ada" } })).toBe("set Owner to ada");
  expect(describe({ kind: "owner", old: { owner: "ada" }, new: {} })).toBe("cleared Owner");
  expect(describe({ kind: "issue_kind", old: { issue_kind: ["bug"] }, new: { issue_kind: ["task"] } })).toBe(
    "added task to Type; removed bug from Type",
  );
  expect(describe({ kind: "issue_validation", old: { issue_validation: "a" }, new: { issue_validation: "b" } })).toBe(
    "edited Done when",
  );
  expect(describe({ kind: "title", old: { title: "x".repeat(80) }, new: { title: "Short" } })).toBe(
    `changed Title from ${"x".repeat(59)}… to Short`,
  );
});

test("events name what they did", () => {
  expect(describe({ kind: "created" })).toBe("created this");
  expect(describe({ kind: "purged" })).toBe("purged this");
  expect(
    describe({
      kind: "marginal_cost",
      old: { marginal_cost: { agent_usd: 1, resource_usd: 0 } },
      new: { marginal_cost: { agent_usd: 1.5, resource_usd: 0 } },
    }),
  ).toBe("recorded cost agent +$0.50");
  // Under a cent, every digit the server keeps (parity bug B4).
  expect(
    describe({
      kind: "marginal_cost",
      old: { marginal_cost: { agent_usd: 0.0001, resource_usd: 0 } },
      new: { marginal_cost: { agent_usd: 0.0043, resource_usd: 0 } },
    }),
  ).toBe("recorded cost agent +$0.0042");
  expect(describe({ kind: "implicit_subs_opened" })).toBe("Implicit subs opened");
});

test("an edge event names the relation as read from this inquiry", () => {
  const edge = (id: string) => ({ peer_id: id, peer_kind: "Issue", peer_edge_kind: "narrows" });
  expect(describe({ kind: "edge_added", new: edge(PARENT.id) })).toBe("added relation Narrows Issue#2");
  expect(describe({ kind: "edge_added", new: edge(CHILD.id) })).toBe("added relation Narrowed by Issue#3");
  const gone = "5f3a9c1e-0000-4000-8000-000000000099";
  // The edge is gone. Nothing causes the child's half of a user's edge, so an
  // uncaused half is this inquiry's as the child; a caused one may be either
  // half, and Issue to Issue runs both ways, so it names no direction (DRV-EDGE-01).
  expect(describe({ kind: "edge_removed", old: edge(gone) })).toBe("removed relation Narrows Issue 5f3a9c1e");
  expect(describe({ kind: "edge_removed", old: edge(gone), caused_by: gone })).toBe(
    "removed a narrows relation with Issue 5f3a9c1e",
  );
  expect(describe({ kind: "dependency_changed", new: edge(CHILD.id) })).toBe("upstream change in Issue#3");
  expect(
    describe({
      kind: "edge_annotation_changed",
      old: { ...edge(CHILD.id), edge_priority: 20, edge_note: "" },
      new: { ...edge(CHILD.id), edge_priority: 0, edge_note: "" },
    }),
  ).toBe("annotated relation Narrowed by Issue#3: priority 20 → 0");
});

test("a removed edge's direction comes from the kinds it can join, when they decide it (DRV-EDGE-01)", () => {
  const belief = row("Belief", 4);
  const context = { detail: detail(belief), topology: META.edges, fields: kindFields(belief, META.fieldOwners) };
  const gone = { peer_id: "5f3a9c1e-0000-4000-8000-000000000099", peer_kind: "Paper", peer_edge_kind: "proves" };
  const cause = "5f3a9c1e-0000-4000-8000-000000000098";
  // A proves edge runs from an artifact to a Belief, so this Belief is its parent.
  expect(text(describeChange(change(belief, 1, { kind: "edge_removed", old: gone, caused_by: cause }), context))).toBe(
    "removed relation Proved by Paper 5f3a9c1e",
  );
  const unknown = { ...gone, peer_edge_kind: "linked_to" };
  expect(text(describeChange(change(belief, 1, { kind: "edge_added", new: unknown, caused_by: cause }), context))).toBe(
    "added a linked to relation with Paper 5f3a9c1e",
  );
});
