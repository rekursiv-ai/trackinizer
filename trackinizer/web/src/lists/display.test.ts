import { describe, expect, test } from "vitest";
import type { InquiryRow } from "../api/inquiries";
import type { Meta } from "../app/boot";
import { defaultDisplay, groupings, groupRows, orderings, sortRows } from "./display";

const META: Meta = {
  enums: {
    status: ["active", "complete", "abandoned", "invalid"],
    judgement: ["proven", "disproven", "unproven", "undecidable"],
  },
  fieldOwners: { priority: "issue", judgement: "belief", confidence: "belief" },
  edges: {},
  kinds: ["Issue", "Artifact", "Paper", "Belief"],
};

let nextSeq = 1;
function row(fields: Partial<InquiryRow>): InquiryRow {
  const seq = nextSeq++;
  return {
    id: `id-${seq}`,
    kind: "Issue",
    seq,
    title: `Row ${seq}`,
    status: "active",
    owner: null,
    labels: null,
    marginal_cost: { agent_usd: 0, resource_usd: 0 },
    created: "2026-09-20T00:00:00+00:00",
    modified: "2026-09-20T00:00:00+00:00",
    ...fields,
  };
}

const values = (rows: readonly InquiryRow[], grouping: Parameters<typeof groupRows>[1]) =>
  groupRows(rows, grouping, META).map((group) => group.value);

describe("groups come from the loaded rows", () => {
  test("COLD-09: an owner nobody has seen before gets its own group", () => {
    const rows = [row({ owner: "dan" }), row({ owner: null }), row({ owner: "Agent" })];
    expect(values(rows, "owner")).toEqual(["Agent", "dan", null]);

    const reassigned = [...rows.slice(0, 2), { ...rows[2]!, owner: "Jane Example" }];
    const groups = groupRows(reassigned, "owner", META);
    expect(groups.map((group) => group.value)).toEqual(["dan", "Jane Example", null]);
    expect(groups.find((group) => group.value === "Jane Example")?.rows).toEqual([reassigned[2]]);
  });

  test("every row lands in exactly one group, whatever the grouping", () => {
    const rows = [
      row({ status: "complete", priority: 40 }),
      row({ kind: "Belief", judgement: null, owner: "" }),
      row({ status: "brand-new", priority: 5 }),
      row({ kind: "Paper", owner: "josh" }),
    ];
    for (const grouping of ["none", "kind", "status", "owner", "priority", "judgement"] as const) {
      const grouped = groupRows(rows, grouping, META).flatMap((group) => group.rows);
      expect(grouped.toSorted((a, b) => a.seq - b.seq)).toEqual(rows);
    }
  });

  test("known values keep the server's order; others follow, then the rows with none", () => {
    const rows = [row({ status: "invalid" }), row({ status: "brand-new" }), row({ status: "active" })];
    expect(values(rows, "status")).toEqual(["active", "invalid", "brand-new"]);
    const beliefs = [row({ judgement: null }), row({ judgement: "undecidable" }), row({ judgement: "proven" })];
    expect(values(beliefs, "judgement")).toEqual(["proven", "undecidable", null]);
    const kinds = [row({ kind: "Belief" }), row({ kind: "Issue" })];
    expect(values(kinds, "kind")).toEqual(["Issue", "Belief"]);
  });

  test("priority groups are bands, with backlog under P3 and no priority last", () => {
    const rows = [row({ priority: null }), row({ priority: 40 }), row({ priority: 0 }), row({ priority: 15 })];
    expect(values(rows, "priority")).toEqual(["0", "1", "3", null]);
  });

  test("no grouping is one group of every row, in order", () => {
    const rows = [row({}), row({})];
    expect(groupRows(rows, "none", META)).toEqual([{ value: null, rows }]);
  });
});

describe("sorting reorders loaded rows only", () => {
  test("priority: most urgent first, rows with none last, ties in server order", () => {
    const rows = [row({ priority: null }), row({ priority: 20 }), row({ priority: 0 }), row({ priority: 20 })];
    expect(sortRows(rows, "priority").map((r) => r.priority)).toEqual([0, 20, 20, null]);
    expect(sortRows(rows, "priority")[1]).toBe(rows[1]);
  });

  test("last updated and created: newest first; number: highest first", () => {
    const old = row({ modified: "2026-09-01T00:00:00+00:00", created: "2026-09-03T00:00:00+00:00" });
    const recent = row({ modified: "2026-09-25T00:00:00+00:00", created: "2026-09-02T00:00:00+00:00" });
    expect(sortRows([old, recent], "modified")).toEqual([recent, old]);
    expect(sortRows([recent, old], "created")).toEqual([old, recent]);
    expect(sortRows([old, recent], "seq")).toEqual([recent, old]);
  });

  test("confidence: highest first, rows with none last", () => {
    const rows = [row({ confidence: null }), row({ confidence: 0.2 }), row({ confidence: 0.9 })];
    expect(sortRows(rows, "confidence").map((r) => r.confidence)).toEqual([0.9, 0.2, null]);
  });

  test("the input array is left as it was", () => {
    const rows = [row({ priority: 30 }), row({ priority: 0 })];
    sortRows(rows, "priority");
    expect(rows.map((r) => r.priority)).toEqual([30, 0]);
  });
});

describe("what each list offers", () => {
  test("a field's grouping and sorting need the field on every kind in the list", () => {
    expect(groupings(["Issue"], META.fieldOwners)).toEqual(["none", "status", "owner", "priority"]);
    expect(groupings(["Belief"], META.fieldOwners)).toEqual(["none", "status", "owner", "judgement"]);
    expect(groupings(["Issue", "Belief"], META.fieldOwners)).toEqual(["none", "kind", "status", "owner"]);
    expect(orderings(["Issue"], META.fieldOwners)).toEqual(["priority", "modified", "created", "seq"]);
    expect(orderings(["Belief"], META.fieldOwners)).toEqual(["confidence", "modified", "created", "seq"]);
    expect(orderings(["Paper"], META.fieldOwners)).toEqual(["modified", "created", "seq"]);
  });

  test("defaults follow the mock: Issues by priority, Beliefs by judgement, mixed by kind", () => {
    expect(defaultDisplay(["Issue"], META.fieldOwners)).toEqual({ grouping: "priority", ordering: "priority" });
    expect(defaultDisplay(["Belief"], META.fieldOwners)).toEqual({ grouping: "judgement", ordering: "modified" });
    expect(defaultDisplay(["Paper"], META.fieldOwners)).toEqual({ grouping: "status", ordering: "modified" });
    expect(defaultDisplay(["Issue", "Paper"], META.fieldOwners)).toEqual({ grouping: "kind", ordering: "modified" });
  });
});
