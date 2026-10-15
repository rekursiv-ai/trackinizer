import { expect, test } from "vitest";
import { fileLines, gapsOf, hunksOf, patchFiles, sideTexts, splitRows, withShown } from "./splitDiff";
import { THIRTY, TWO_HUNKS } from "./testing";

const METER_PATCH = [
  "diff -ruN iter-00/x.py iter-02/x.py\n--- iter-00/x.py\n+++ iter-02/x.py\n@@ -1,2 +1,2 @@\n keep = 1\n-warps = 4\n+warps = 2\nOnly in iter-02: y.py\n",
  "diff -ruN iter-00/w.bin iter-02/w.bin\nFiles iter-00/w.bin and iter-02/w.bin differ; too large to diff\n… cut at 48,000 of 51,200 characters\n",
].join("");

test("a patch is cut into its files, each under its path, with the notes beside them as files of their own", () => {
  expect(patchFiles(METER_PATCH)).toEqual([
    {
      path: "iter-02/x.py",
      head: ["diff -ruN iter-00/x.py iter-02/x.py", "--- iter-00/x.py", "+++ iter-02/x.py"],
      body: ["@@ -1,2 +1,2 @@", " keep = 1", "-warps = 4", "+warps = 2"],
    },
    { path: "", head: ["Only in iter-02: y.py"], body: [] },
    {
      path: "iter-02/w.bin",
      head: ["diff -ruN iter-00/w.bin iter-02/w.bin", "Files iter-00/w.bin and iter-02/w.bin differ; too large to diff", "… cut at 48,000 of 51,200 characters"],
      body: [],
    },
  ]);
});

test("git's diff of one path is one file; a note before any file is a file of its own; nothing is no file", () => {
  const git = "diff --git a/src/m.py b/src/m.py\nindex 1..2 100644\n--- a/src/m.py\n+++ b/src/m.py\n@@ -3 +3 @@\n-a\n+b\n";
  expect(patchFiles(git).map(({ path, body }) => [path, body.length])).toEqual([["b/src/m.py", 3]]);
  const added = "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+x\n";
  expect(patchFiles(added).map(({ path }) => path)).toEqual(["b/new.py"]);
  const removed = "--- a/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n";
  expect(patchFiles(removed).map(({ path }) => path)).toEqual(["a/old.py"]);
  expect(patchFiles("… not compared: a snapshot holds more than 20,000 entries\ndiff -ruN a/x b/x\n").map(({ path }) => path)).toEqual(["", "b/x"]);
  expect(patchFiles("")).toEqual([]);
});

test("removed lines sit beside the added lines that follow them, the longer run's rest against nothing, each side numbered", () => {
  const rows = splitRows(["@@ -10,4 +20,3 @@", " a", "-b", "-c", "-d", "+B", " e", "+f", "\\ No newline at end of file"]);
  const cell = (side: { readonly kind: string; readonly n: number; readonly text: string } | null) => (side ? `${side.kind}:${side.n}:${side.text}` : "-");
  expect(rows.map((row) => (row.kind === "hunk" ? row.text : `${cell(row.left)} | ${cell(row.right)}`))).toEqual([
    "@@ -10,4 +20,3 @@",
    "ctx:10:a | ctx:20:a",
    "del:11:b | add:21:B",
    "del:12:c | -",
    "del:13:d | -",
    "ctx:14:e | ctx:22:e",
    "- | add:23:f",
    "\\ No newline at end of file",
  ]);
});

test("each line knows its place on its side, which the side's highlighting is read by", () => {
  const body = ["@@ -1,3 +1,3 @@", " a", "-b", "+B", " c"];
  const texts = sideTexts(body);
  expect(texts).toEqual({ old: "a\nb\nc", new: "a\nB\nc" });
  const pairs = splitRows(body).flatMap((row) => (row.kind === "pair" ? [row] : []));
  const old = texts.old.split("\n");
  const fresh = texts.new.split("\n");
  for (const { left, right } of pairs) {
    if (left) expect(old[left.at]).toBe(left.text);
    if (right) expect(fresh[right.at]).toBe(right.text);
  }
});

test("a file's hunks are read from their headers, each side's span and lines, when every hunk holds what its header counts", () => {
  const hunks = hunksOf(TWO_HUNKS)!;
  expect(hunks.map(({ header, old, new: fresh, lines }) => [header, old, fresh, lines.length])).toEqual([
    ["@@ -6,6 +6,7 @@", { start: 6, count: 6 }, { start: 6, count: 7 }, 7],
    ["@@ -21,7 +22,7 @@", { start: 21, count: 7 }, { start: 22, count: 7 }, 8],
  ]);
  // A side with no lines names the line before it, as git and difflib write it; one line has no count.
  const inserted = hunksOf(["@@ -0,0 +1,2 @@", "+a", "+b", "@@ -5 +7 @@", "-x", "\\ No newline at end of file", "+y"])!;
  expect(inserted.map(({ old, new: fresh }) => [old, fresh])).toEqual([
    [{ start: 1, count: 0 }, { start: 1, count: 2 }],
    [{ start: 5, count: 1 }, { start: 7, count: 1 }],
  ]);
});

test("a diff whose hunks do not hold what their headers count, or whose sides disagree on what lies between, has no hunks to fill", () => {
  // Cut short at a whole line, as the store cuts the file that passes its bound.
  expect(hunksOf(TWO_HUNKS.slice(0, -2))).toBeNull();
  expect(hunksOf(["@@ -1,2 +1,2 @@", " a", "-b", "+B", "+C"])).toBeNull();
  expect(hunksOf(["@@ malformed @@", " a"])).toBeNull();
  expect(hunksOf([" a"])).toBeNull();
  expect(hunksOf([])).toBeNull();
  // Six unchanged lines before the first hunk on one side, five on the other.
  expect(hunksOf(["@@ -7 +6 @@", "-a", "+b"])).toBeNull();
  expect(hunksOf(["@@ -1 +1 @@", "-a", "+b", "@@ -9 +10 @@", "-c", "+d"])).toBeNull();
});

test("the gaps are the unchanged lines before each hunk and after the last, numbered on both sides; the last counts once the file is read", () => {
  const hunks = hunksOf(TWO_HUNKS)!;
  expect(gapsOf(hunks, null)).toEqual([
    { old: 1, new: 1, count: 5 },
    { old: 12, new: 13, count: 9 },
    { old: 28, new: 29, count: null },
  ]);
  expect(gapsOf(hunks, 30).at(-1)).toEqual({ old: 28, new: 29, count: 2 });
  expect(gapsOf(hunks, 28).at(-1)).toEqual({ old: 28, new: 29, count: 0 });
  // An insertion at the top leaves nothing above it.
  expect(gapsOf(hunksOf(["@@ -0,0 +1,2 @@", "+a", "+b", "@@ -5 +7 @@", "-x", "+y"])!, 7)).toEqual([
    { old: 1, new: 1, count: 0 },
    { old: 1, new: 3, count: 4 },
    { old: 6, new: 8, count: 0 },
  ]);
});

/** Each hunk as drawn: its header, how many lines it holds, and the gap still hidden above it. */
function drawn(shown: ReturnType<typeof withShown>) {
  return {
    hunks: shown.hunks.map(({ lines, above }) => [lines[0], lines.length - 1, above]),
    below: shown.below,
  };
}

test("nothing shown, the hunks are drawn as written, each gap that hides lines above its hunk and the file's end below", () => {
  const hunks = hunksOf(TWO_HUNKS)!;
  const none = [{ down: 0, up: 0 }, { down: 0, up: 0 }, { down: 0, up: 0 }];
  expect(drawn(withShown(hunks, none, null))).toEqual({
    hunks: [
      ["@@ -6,6 +6,7 @@", 7, { index: 0, place: "top", hidden: 5 }],
      ["@@ -21,7 +22,7 @@", 8, { index: 1, place: "between", hidden: 9 }],
    ],
    below: { index: 2, place: "bottom", hidden: null },
  });
  expect(withShown(hunks, none, THIRTY).hunks.flatMap(({ lines }) => lines)).toEqual(TWO_HUNKS);
  // Lines asked for before the file is read wait for it.
  expect(withShown(hunks, [{ down: 0, up: 20 }, { down: 20, up: 0 }, { down: 20, up: 0 }], null)).toEqual(withShown(hunks, none, null));
});

test("lines shown under a hunk and over the next join each as context, numbered on both sides, and a gap shown whole joins the two hunks", () => {
  const hunks = hunksOf(TWO_HUNKS)!;
  const some = withShown(hunks, [{ down: 0, up: 2 }, { down: 2, up: 3 }, { down: 1, up: 0 }], THIRTY);
  expect(drawn(some)).toEqual({
    hunks: [
      ["@@ -4,10 +4,11 @@", 11, { index: 0, place: "top", hidden: 3 }],
      ["@@ -18,11 +19,11 @@", 12, { index: 1, place: "between", hidden: 4 }],
    ],
    below: { index: 2, place: "bottom", hidden: 1 },
  });
  expect(some.hunks[0]!.lines.slice(1, 3)).toEqual([" line 4", " line 5"]);
  expect(some.hunks[0]!.lines.slice(-2)).toEqual([" line 13", " line 14"]);
  expect(some.hunks[1]!.lines.slice(1, 4)).toEqual([" line 19", " line 20", " line 21"]);
  expect(some.hunks[1]!.lines.at(-1)).toBe(" line 29");
  // Each side numbers the shown lines from the rebuilt header: old 4 is new 4, old 18 is new 19.
  const rows = splitRows(some.hunks.flatMap(({ lines }) => lines)).flatMap((row) => (row.kind === "pair" && row.left?.kind === "ctx" ? [[row.left.n, row.right!.n, row.left.text]] : []));
  expect(rows).toContainEqual([4, 4, "line 4"]);
  expect(rows).toContainEqual([18, 19, "line 19"]);
  expect(rows).toContainEqual([28, 29, "line 29"]);
  // Asking for more than a gap holds shows what it holds: the top from line 1, the middle joined, the end whole.
  const all = withShown(hunks, [{ down: 0, up: 20 }, { down: 20, up: 0 }, { down: 20, up: 0 }], THIRTY);
  expect(drawn(all)).toEqual({ hunks: [["@@ -1,29 +1,30 @@", 31, null]], below: null });
  const context = (lines: readonly string[]) => lines.map((line) => ` ${line}`);
  expect(all.hunks[0]!.lines.slice(1)).toEqual([...context(THIRTY.slice(0, 8)), "+line 9", ...context(THIRTY.slice(9, 24)), "-old 25", "+line 25", ...context(THIRTY.slice(25))]);
});

test("a side that held no lines takes a count once context joins it, written as git writes one line", () => {
  const hunks = hunksOf(["@@ -0,0 +1,2 @@", "+a", "+b", "@@ -5 +7 @@", "-x", "+y"])!;
  const lines = ["a", "b", "c", "d", "e", "f", "y"];
  const shown = withShown(hunks, [{ down: 0, up: 0 }, { down: 1, up: 0 }, { down: 0, up: 0 }], lines);
  expect(shown.hunks.map(({ lines: each }) => each[0])).toEqual(["@@ -1 +1,3 @@", "@@ -5 +7 @@"]);
  expect(drawn(shown).hunks.map(([, , above]) => above)).toEqual([null, { index: 1, place: "between", hidden: 3 }]);
  expect(shown.below).toBeNull();
});

test("a file's lines are split at each newline alone, as the store splits them, a last newline ending the last line", () => {
  expect(fileLines("a\r\nb\n")).toEqual(["a\r", "b"]);
  expect(fileLines("a\n\nb")).toEqual(["a", "", "b"]);
  expect(fileLines("")).toEqual([]);
});
