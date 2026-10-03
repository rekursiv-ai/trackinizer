import { expect, test } from "vitest";
import type { Ancestor, InquiryRow } from "../api/inquiries";
import { issue, uuid } from "../live/testing";
import { buildOutline, type Outline } from "./tree";

/** An older ancestor `seq` (outside the page), with the ids of what narrows it. */
function up(seq: number, ...children: number[]): Ancestor {
  return { id: uuid(seq), kind: "Issue", seq, title: `Issue ${seq}`, status: "active", child_ids: children.map(uuid) };
}

/** `rows` in list order, each with the ancestry `of` gives it; a row `of` leaves out has none read yet. */
function outline(rows: readonly InquiryRow[], of: { [seq: number]: Ancestor[] }, collapsed: number[] = []): Outline {
  const ancestry = new Map(rows.filter((row) => of[row.seq]).map((row) => [row.id, of[row.seq]!]));
  return buildOutline(rows, ancestry, new Set(collapsed.map(uuid)));
}

/** Each line as `<indent>#seq` (or `#a › #b` compressed), marked when older, collapsed or under another parent too. */
function lines({ lines }: Outline): string[] {
  return lines.map((line) => {
    const refs = line.nodes.map((node) => `#${node.seq}`).join(" › ");
    const older = line.nodes[0]!.row ? "" : " older";
    const folded = line.parent && !line.expanded ? ` (${line.below} below)` : "";
    const also = line.alsoUnder.map((node) => ` also under #${node.seq}`).join("");
    return `${"  ".repeat(line.depth)}${refs}${older}${folded}${also}`;
  });
}

test("rows nest under their parents, and parents outside the page show as older lines", () => {
  // #1 is outside the page; #10 narrows it, #11 narrows #10, #12 narrows #1.
  const result = outline([issue(11), issue(10), issue(12)], {
    11: [up(10, 11), up(1, 10)],
    10: [up(1, 10)],
    12: [up(1, 12)],
  });
  expect(lines(result)).toEqual(["#1 older", "  #10", "    #11", "  #12"]);
  expect(result.orphans).toEqual([]);
});

test("trees and siblings come in the order of their first row in the list", () => {
  const result = outline([issue(21), issue(30), issue(20)], {
    21: [up(2, 21)],
    30: [up(3, 30)],
    20: [up(2, 20)],
  });
  expect(lines(result)).toEqual(["#2 older", "  #21", "  #20", "#3 older", "  #30"]);
});

test("a chain of older parents, each the only child of the one above, compresses into one line", () => {
  const result = outline([issue(21), issue(22), issue(23)], {
    21: [up(2, 21), up(1, 2)],
    22: [up(2, 22), up(1, 2)],
    // An older parent over one row is not compressed: the row is a line of its own.
    23: [up(3, 23)],
  });
  expect(lines(result)).toEqual(["#1 › #2 older", "  #21", "  #22", "#3 older", "  #23"]);
});

test("rows with no parent and nothing under them go in the No parent group, in list order", () => {
  // #40 has no parent but #41 narrows it, so #40 heads a tree of its own.
  const result = outline([issue(43), issue(41), issue(42), issue(40)], {
    43: [],
    41: [up(40, 41)],
    42: [],
    40: [],
  });
  expect(lines(result)).toEqual(["#40", "  #41"]);
  expect(result.orphans.map((row) => row.seq)).toEqual([43, 42]);
});

test("a row with two parents shows once, under the first, and names the other", () => {
  const result = outline([issue(30)], { 30: [up(1, 30), up(2, 30)] });
  expect(lines(result)).toEqual(["#1 older", "  #30 also under #2"]);
});

test("a collapsed line hides what is under it and counts the rows there", () => {
  const result = outline(
    [issue(11), issue(10), issue(12)],
    { 11: [up(10, 11), up(1, 10)], 10: [up(1, 10)], 12: [up(1, 12)] },
    [1],
  );
  expect(lines(result)).toEqual(["#1 older (3 below)"]);
});

test("a cycle among the parents still ends, and shows the row", () => {
  // #1 and #2 narrow each other, and #30 narrows #1: #2 heads the line, and is under #1 too.
  const result = outline([issue(30)], { 30: [up(1, 30, 2), up(2, 1)] });
  expect(lines(result)).toEqual(["#2 › #1 older also under #1", "  #30"]);
});

test("a row whose parents are not read yet is held back, not put under No parent", () => {
  const result = outline([issue(11), issue(12)], { 11: [up(1, 11)] });
  expect(lines(result)).toEqual(["#1 older", "  #11"]);
  expect(result.orphans).toEqual([]);
});
