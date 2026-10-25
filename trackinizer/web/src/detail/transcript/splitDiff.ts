// A unified diff read for the transcript's views: one file's lines paired across the old and new sides
// as GitHub's split view pairs them, or numbered in one column.
import type { DiffLine } from "./records";

/** One file of a diff: its header lines (`diff `, `---`, `+++`, a note), then its diff lines. */
export type PatchFile = {
  /** The path the header names on the new side, else the old side; empty when it names none. */
  readonly path: string;
  readonly head: readonly string[];
  /** The hunk headers and the lines under them, each as written, mark first. */
  readonly body: readonly string[];
};

/** One side of a split row: a line of the old file or the new one, numbered in it. */
export type Side = {
  readonly kind: "ctx" | "del" | "add";
  /** Its number in the file it is in. */
  readonly n: number;
  /** The line without its mark. */
  readonly text: string;
  /** Its place among the lines of its side, context included, for that side's highlighting. */
  readonly at: number;
};

/** A split row: a hunk header across both sides, or a line of each side, either one absent. */
export type SplitRow =
  | { readonly kind: "hunk"; readonly text: string }
  | { readonly kind: "pair"; readonly left: Side | null; readonly right: Side | null };

/**
 * A file's diff lines paired across the two sides: a context line on both, a run of
 * removed lines beside the run of added lines that follows it, the longer run's rest
 * against nothing. Numbers come from each hunk's `@@ -a,b +c,d @@`; a `\ No newline`
 * line is a header row of its own.
 */
export function splitRows(body: readonly string[]): SplitRow[] {
  const rows: SplitRow[] = [];
  let [old, next, oldAt, newAt] = [1, 1, 0, 0];
  let dels: Side[] = [];
  let adds: Side[] = [];
  const flush = () => {
    for (let k = 0; k < Math.max(dels.length, adds.length); k++) rows.push({ kind: "pair", left: dels[k] ?? null, right: adds[k] ?? null });
    [dels, adds] = [[], []];
  };
  for (const line of body) {
    const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
    if (hunk || line.startsWith("@@") || line.startsWith("\\")) {
      flush();
      if (hunk) [old, next] = [Number(hunk[1]), Number(hunk[2])];
      rows.push({ kind: "hunk", text: line });
    } else if (line.startsWith("-")) {
      dels.push({ kind: "del", n: old++, text: line.slice(1), at: oldAt++ });
    } else if (line.startsWith("+")) {
      adds.push({ kind: "add", n: next++, text: line.slice(1), at: newAt++ });
    } else {
      flush();
      // A context line is a space and its text; a tool that trims lines leaves an empty one.
      const text = line.slice(1);
      rows.push({ kind: "pair", left: { kind: "ctx", n: old++, text, at: oldAt++ }, right: { kind: "ctx", n: next++, text, at: newAt++ } });
    }
  }
  flush();
  return rows;
}

/** Each side's whole text, context included, in order: what that side's highlighting reads. */
export function sideTexts(body: readonly string[]): { readonly old: string; readonly new: string } {
  const side = (skip: string) =>
    body
      .filter((line) => !line.startsWith("@@") && !line.startsWith("\\") && !line.startsWith(skip))
      .map((line) => line.slice(1))
      .join("\n");
  return { old: side("+"), new: side("-") };
}

/**
 * A unified diff of one path in two parts: its header lines (`diff --git`, `---`, `+++` and the
 * like), then its hunks a row a line, each numbered from its `@@ -a,b +c,d @@`: a removed line in
 * the old file, an added or context line in the new one. `\ No newline at end of file` is a row
 * of its own, as a hunk header is, with no number.
 */
export function diffParts(diff: string): { readonly head: readonly string[]; readonly lines: readonly DiffLine[] } {
  const all = diff ? diff.replace(/\n$/, "").split("\n") : [];
  const start = all.findIndex((text) => text.startsWith("@@"));
  let [old, next] = [0, 0];
  const lines = (start < 0 ? [] : all.slice(start)).map((text): DiffLine => {
    const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(text);
    if (hunk) [old, next] = [Number(hunk[1]), Number(hunk[2])];
    if (text.startsWith("@@") || text.startsWith("\\")) return { kind: "hunk", text, line: null };
    if (text.startsWith("-")) return { kind: "del", text, line: old++ };
    if (text.startsWith("+")) return { kind: "add", text, line: next++ };
    old++;
    // A context line is a space and its text; a tool that trims lines leaves an empty one.
    return { kind: "ctx", text: text || " ", line: next++ };
  });
  return { head: start < 0 ? all : all.slice(0, start), lines };
}
