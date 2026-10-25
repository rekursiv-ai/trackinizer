import { expect, test } from "vitest";
import { diffParts, sideTexts, splitRows } from "./splitDiff";

/** The diff git gives of the one file the fixture tree changed, with a line that ends the file unterminated. */
const TREE_DIFF = [
  "diff --git a/attn.py b/attn.py",
  "index 1111111..2222222 100644",
  "--- a/attn.py",
  "+++ b/attn.py",
  "@@ -3,3 +3,3 @@ def tile():",
  " def tile():",
  "-    warps = 4",
  "+    warps = 2",
  "     return warps",
  "\\ No newline at end of file",
  "",
].join("\n");

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

test("a diff splits into its header lines and its hunks, each line numbered in the file it is in", () => {
  const { head, lines } = diffParts(TREE_DIFF);
  expect(head).toEqual(["diff --git a/attn.py b/attn.py", "index 1111111..2222222 100644", "--- a/attn.py", "+++ b/attn.py"]);
  expect(lines.map(({ kind, line }) => [kind, line])).toEqual([
    ["hunk", null],
    ["ctx", 3],
    ["del", 4],
    ["add", 4],
    ["ctx", 5],
    ["hunk", null],
  ]);
  expect(lines.at(-1)!.text).toBe("\\ No newline at end of file");
});

test("each hunk numbers from its own header; a context line left empty is a space", () => {
  const { lines } = diffParts(["@@ -1,2 +1,2 @@", "-a", "+b", "@@ -10 +11,2 @@", "", "+c"].join("\n"));
  expect(lines.map(({ kind, text, line }) => [kind, text, line])).toEqual([
    ["hunk", "@@ -1,2 +1,2 @@", null],
    ["del", "-a", 1],
    ["add", "+b", 1],
    ["hunk", "@@ -10 +11,2 @@", null],
    ["ctx", " ", 11],
    ["add", "+c", 12],
  ]);
});

test("a diff with no hunk is all header, and no diff is nothing", () => {
  expect(diffParts("diff --git a/x.bin b/x.bin\nBinary files a/x.bin and b/x.bin differ\n")).toEqual({
    head: ["diff --git a/x.bin b/x.bin", "Binary files a/x.bin and b/x.bin differ"],
    lines: [],
  });
  expect(diffParts("")).toEqual({ head: [], lines: [] });
});
