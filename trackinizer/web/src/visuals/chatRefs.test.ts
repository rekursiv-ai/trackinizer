import { expect, test } from "vitest";
import { namedRows } from "./chatRefs";

const KINDS = ["Issue", "Belief", "Experiment", "AgentSession"];
const ID = "6f1c2d3e-0000-4000-8000-0123456789ab";

test("every ref an answer names, any kind the app knows, in the order it names them, once each", () => {
  const text = "Start with Belief#7, then Issue#12 and AgentSession#4. Issue#12 again, and Experiment#3.";
  expect(namedRows(text, KINDS)).toEqual([
    { kind: "Belief", seq: 7 }, { kind: "Issue", seq: 12 }, { kind: "AgentSession", seq: 4 }, { kind: "Experiment", seq: 3 },
  ]);
});

test("a kind the app does not know, a lowercase kind, and a ref glued to a word or a hash are not refs", () => {
  // A seq past the safe integers is built here, not written out: the export forbids long literal refs.
  const huge = `Issue#${"9".repeat(20)}`;
  expect(namedRows(`Widget#1 issue#2 xIssue#3 ##Issue#4 Issue#5x Issue# ${huge}`, KINDS)).toEqual([]);
});

test("a ref in prose next to punctuation is one", () => {
  expect(namedRows("(Issue#1), Issue#2; “Issue#3” — Issue#4.", KINDS).map((row) => "seq" in row && row.seq)).toEqual([1, 2, 3, 4]);
});

test("a row id is named by its UUID, lowercased", () => {
  expect(namedRows(`See ${ID.toUpperCase()} and Issue#1.`, KINDS)).toEqual([{ id: ID }, { kind: "Issue", seq: 1 }]);
});

test("a ref in a code span or a code block is quoted, not cited: the page does not link it either", () => {
  const text = [
    "Use `Issue#1` as the syntax, ``Issue#2`` too.",
    "```",
    "Issue#3",
    "```",
    "~~~sh",
    "echo Issue#4",
    "~~~",
    "Issue#5 is real.",
  ].join("\n");
  expect(namedRows(text, KINDS)).toEqual([{ kind: "Issue", seq: 5 }]);
});

test("an unclosed code block hides the rest of the answer, as the page renders it", () => {
  expect(namedRows("Issue#1\n```\nIssue#2\nIssue#3", KINDS)).toEqual([{ kind: "Issue", seq: 1 }]);
});

test("a link's text is cited, its target and a bare address are not", () => {
  const text = "[Issue#1](https://example.com/Issue#9) and [the second one, Belief#2](#/ref/Belief/2) at https://x.dev/#Issue#8 or <https://x.dev/#Issue#7>.";
  expect(namedRows(text, KINDS)).toEqual([{ kind: "Issue", seq: 1 }, { kind: "Belief", seq: 2 }]);
});

test("a link written as a ref to the app's own page cites that ref once", () => {
  expect(namedRows("[Issue#4](https://alpha.example/app/#/ref/Issue/4)", KINDS)).toEqual([{ kind: "Issue", seq: 4 }]);
});

test("at most 50 rows: as many as one highlight takes", () => {
  const text = Array.from({ length: 60 }, (_, n) => `Issue#${n + 1}`).join(" ");
  const rows = namedRows(text, KINDS);
  expect(rows).toHaveLength(50);
  expect(rows.at(-1)).toEqual({ kind: "Issue", seq: 50 });
});

test("no kinds known yet: only UUIDs", () => {
  expect(namedRows(`Issue#1 ${ID}`, [])).toEqual([{ id: ID }]);
});
