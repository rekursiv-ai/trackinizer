// A unified diff read for a side-by-side view: cut into files, and each file's lines paired across
// the old and new sides as GitHub's split view pairs them.

/** One file of a patch: its header lines (`diff `, `---`, `+++`, `Binary files`, a note), then its diff lines. */
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
 * A patch cut into its files, at each `diff ` line. A diff line opens with a space, `+`,
 * `-`, `@` or `\`, so a `diff ` line is always a header. Lines before the first `diff `
 * line are a file of their own when the patch has no `diff ` line at all (git's diff of
 * one path opens at `---`), else a file with a header only (a note such as `… cut at`).
 * Notes after a file's lines (`Only in`, `… not compared`) are a header-only file of their own.
 */
export function patchFiles(patch: string): PatchFile[] {
  const all = patch ? patch.replace(/\n$/, "").split("\n") : [];
  const starts = all.flatMap((line, index) => (line.startsWith("diff ") ? [index] : []));
  const cuts = starts.length === 0 ? [0] : [...(starts[0]! > 0 ? [0] : []), ...starts];
  return cuts.flatMap((start, index) => fileOf(all.slice(start, cuts[index + 1] ?? all.length)));
}

/** A line after a file's hunks that is no diff line: what `diff -r` and the meter say beside the files. */
const TRAILING = /^(Only in |Binary files |Files |… )/;

/** One block of a patch: a file, and any notes that trail its lines as a file of their own. */
function fileOf(lines: readonly string[]): PatchFile[] {
  const first = lines.findIndex((line) => line.startsWith("@@"));
  if (first < 0) return lines.length > 0 ? [{ path: pathOf(lines), head: lines, body: [] }] : [];
  const head = lines.slice(0, first);
  const rest = lines.slice(first);
  const note = rest.findIndex((line) => TRAILING.test(line));
  const body = note < 0 ? rest : rest.slice(0, note);
  const trailing = note < 0 ? [] : [{ path: "", head: rest.slice(note), body: [] }];
  return [{ path: pathOf(head), head, body }, ...trailing];
}

/** The path a file's header names: the new side's `+++`, else the old side's `---`, else the `diff` line's last. */
function pathOf(head: readonly string[]): string {
  const named = (prefix: string) => {
    const line = head.find((each) => each.startsWith(prefix));
    const path = line?.slice(prefix.length).split("\t")[0]?.trim() ?? "";
    return path === "/dev/null" ? "" : path;
  };
  return named("+++ ") || named("--- ") || (head.find((line) => line.startsWith("diff "))?.split(" ").at(-1) ?? "");
}

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
