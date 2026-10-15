import { expect, test } from "vitest";
import { patchFiles, sideTexts, splitRows } from "./splitDiff";

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
