// Grouping and sorting over rows already loaded. Neither reaches the server: the
// list route has no sort param and returns rows newest-created first.
import type { InquiryRow } from "../api/inquiries";
import type { FieldOwners } from "../api/meta";
import type { Meta } from "../app/boot";
import { appliesTo } from "../query/query";
import { priorityBand } from "../ui/glyphs";

/** What rows are grouped by; `none` is one list with no headings. */
export type Grouping = "none" | "kind" | "status" | "owner" | "priority" | "judgement";

/** What rows are sorted by, within each group. */
export type Ordering = "modified" | "created" | "seq" | "priority" | "confidence";

/** Rows under one heading. `value` is `null` for rows without one, and for `none`. */
export type Group = { readonly value: string | null; readonly rows: readonly InquiryRow[] };

/** The groupings a list of `kinds` offers: a field's only when every kind has it. */
export function groupings(kinds: readonly string[], fieldOwners: FieldOwners): Grouping[] {
  const all = everyKindHas(kinds, fieldOwners);
  return [
    "none",
    ...(kinds.length > 1 ? (["kind"] as const) : []),
    "status",
    "owner",
    ...(all("priority") ? (["priority"] as const) : []),
    ...(all("judgement") ? (["judgement"] as const) : []),
  ];
}

/** The orderings a list of `kinds` offers, its kind's own field first. */
export function orderings(kinds: readonly string[], fieldOwners: FieldOwners): Ordering[] {
  const all = everyKindHas(kinds, fieldOwners);
  return [
    ...(all("priority") ? (["priority"] as const) : []),
    ...(all("confidence") ? (["confidence"] as const) : []),
    "modified",
    "created",
    "seq",
  ];
}

/** How a list of `kinds` starts out, as the mock does. */
export function defaultDisplay(
  kinds: readonly string[],
  fieldOwners: FieldOwners,
): { grouping: Grouping; ordering: Ordering } {
  const all = everyKindHas(kinds, fieldOwners);
  const grouping: Grouping =
    kinds.length > 1 ? "kind" : all("priority") ? "priority" : all("judgement") ? "judgement" : "status";
  return { grouping, ordering: all("priority") ? "priority" : "modified" };
}

/** `rows` in `ordering`; ties keep the server's order. Returns a new array. */
export function sortRows(rows: readonly InquiryRow[], ordering: Ordering): InquiryRow[] {
  return rows.toSorted(COMPARE[ordering]);
}

/**
 * `rows` under headings, keeping their order within each.
 *
 * Headings come from the rows themselves, never from a fixed list, so a value
 * nobody has seen before, such as a new owner, gets its own group (COLD-09).
 * Known values (statuses, judgements, kinds, priority bands) come first in the
 * server's order, then any others alphabetically, then rows without a value.
 */
export function groupRows(rows: readonly InquiryRow[], grouping: Grouping, meta: Meta): Group[] {
  if (grouping === "none") return [{ value: null, rows }];
  const byValue = new Map<string | null, InquiryRow[]>();
  for (const row of rows) {
    const value = valueOf(row, grouping) || null;
    byValue.set(value, [...(byValue.get(value) ?? []), row]);
  }
  const known = knownOrder(grouping, meta);
  const others = [...byValue.keys()]
    .filter((value): value is string => value !== null && !known.includes(value))
    .sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
  return [...known.filter((value) => byValue.has(value)), ...others, null]
    .filter((value) => byValue.has(value))
    .map((value) => ({ value, rows: byValue.get(value)! }));
}

function valueOf(row: InquiryRow, grouping: Exclude<Grouping, "none">): string | null | undefined {
  switch (grouping) {
    case "kind":
      return row.kind;
    case "status":
      return row.status;
    case "owner":
      return row.owner;
    case "judgement":
      return row.judgement;
    case "priority": {
      const band = priorityBand(row.priority);
      return band === null ? null : String(band);
    }
  }
}

function knownOrder(grouping: Exclude<Grouping, "none">, meta: Meta): readonly string[] {
  switch (grouping) {
    case "kind":
      return meta.kinds;
    case "status":
    case "judgement":
      return meta.enums[grouping] ?? [];
    case "priority":
      return ["0", "1", "2", "3"];
    case "owner":
      return [];
  }
}

function everyKindHas(kinds: readonly string[], fieldOwners: FieldOwners) {
  return (field: string) => kinds.every((kind) => appliesTo(field, kind, fieldOwners));
}

type Compare = (a: InquiryRow, b: InquiryRow) => number;

const newestFirst = (a: string, b: string) => Date.parse(b) - Date.parse(a);

/** Ascending by `key`, rows without a value last. */
const nullsLast =
  (key: (row: InquiryRow) => number | null | undefined, sign: 1 | -1): Compare =>
  (a, b) => {
    const x = key(a);
    const y = key(b);
    if (x == null || y == null) return (x == null ? 1 : 0) - (y == null ? 1 : 0);
    return sign * (x - y);
  };

const COMPARE: { readonly [ordering in Ordering]: Compare } = {
  modified: (a, b) => newestFirst(a.modified, b.modified),
  created: (a, b) => newestFirst(a.created, b.created),
  seq: (a, b) => b.seq - a.seq,
  priority: nullsLast((row) => row.priority, 1),
  confidence: nullsLast((row) => row.confidence, -1),
};
