import { expect, test } from "vitest";
import type { Ancestor, InquiryRow } from "../api/inquiries";
import { issue, uuid } from "../live/testing";
import { buildStreams, type Stream } from "./streams";

/** An ancestor `seq` outside the page, with the ids of what narrows it. */
function up(seq: number, ...children: number[]): Ancestor {
  return { id: uuid(seq), kind: "Issue", seq, title: `Issue ${seq}`, status: "active", child_ids: children.map(uuid) };
}

/** `rows`, newest first, each with the ancestry `of` gives it; a row `of` leaves out has none read yet. */
function streams(rows: readonly InquiryRow[], of: { [seq: number]: Ancestor[] }): Stream[] {
  return buildStreams(rows, new Map(rows.filter((row) => of[row.seq]).map((row) => [row.id, of[row.seq]!])));
}

/** Each stream as `#root: rows`, a row as `#seq`, `(in #parent)` when that is not the root, `(also under #r)`. */
function summary(found: readonly Stream[]): string[] {
  return found.map(({ root, rows }) => {
    const listed = rows.map(({ row, parent, alsoUnder }) => {
      const notes = [...(parent ? [`in #${parent.seq}`] : []), ...alsoUnder.map((other) => `also under #${other.seq}`)];
      return notes.length ? `#${row.seq} (${notes.join(", ")})` : `#${row.seq}`;
    });
    return `${root ? `#${root.seq}` : "No parent"}: ${listed.join(" ")}`;
  });
}

test("rows group by the top of their narrows ancestry, groups ordered by their newest row, No parent last", () => {
  const found = streams([issue(25), issue(24), issue(23), issue(22), issue(21)], {
    25: [up(2, 25)],
    24: [],
    23: [up(11, 23), up(1, 11)],
    22: [up(1, 22)],
    21: [up(2, 21)],
  });
  expect(summary(found)).toEqual(["#2: #25 #21", "#1: #23 (in #11) #22", "No parent: #24"]);
});

test("a stream counts its active and done rows, and knows its newest", () => {
  const found = streams([issue(32), issue(31, { status: "complete" }), issue(30, { status: "abandoned" })], {
    32: [up(1, 32)],
    31: [up(1, 31)],
    30: [up(1, 30)],
  });
  expect(found).toHaveLength(1);
  const [{ active, done, newest, rows }] = found as [Stream];
  expect({ active, done, rows: rows.length }).toEqual({ active: 1, done: 1, rows: 3 });
  expect(newest).toBe(issue(32).created);
});

test("a row under two roots is listed under each, marked, and a row reaching one root twice is listed once", () => {
  const found = streams([issue(41), issue(40)], {
    // #41 narrows #1 and #2, two roots.
    41: [up(1, 41), up(2, 41)],
    // #40 reaches #3 through #11 and through #12.
    40: [up(11, 40), up(12, 40), up(3, 11, 12)],
  });
  expect(summary(found)).toEqual(["#1: #41 (also under #2)", "#2: #41 (also under #1)", "#3: #40 (in #11)"]);
});

test("a row with no parent heads a stream of its own when rows are under it; otherwise it goes under No parent", () => {
  const found = streams([issue(52), issue(51), issue(50)], {
    52: [],
    51: [up(50, 51)],
    50: [],
  });
  expect(summary(found)).toEqual(["#50: #51 #50", "No parent: #52"]);
  expect(found[0]!.root).toMatchObject({ seq: 50, title: "Issue 50" });
});

test("a cycle with no top still makes a stream, under the farthest ancestor read", () => {
  const found = streams([issue(60)], { 60: [up(1, 60, 2), up(2, 1)] });
  expect(summary(found)).toEqual(["#2: #60 (in #1)"]);
});

test("a row whose ancestry is not read yet is held back", () => {
  expect(summary(streams([issue(71), issue(70)], { 70: [up(1, 70)] }))).toEqual(["#1: #70"]);
});
