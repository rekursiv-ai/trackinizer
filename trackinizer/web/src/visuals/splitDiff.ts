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

/** The lines one side of a hunk covers: `count` of them from line `start`. A side with none stands before `start`. */
export type Span = { readonly start: number; readonly count: number };

/** One hunk of a file's diff: its header, each side's span, and the lines under the header. */
export type Hunk = { readonly header: string; readonly old: Span; readonly new: Span; readonly lines: readonly string[] };

/** A hunk header's numbers: each side's first line, and its count when it is not 1. */
const HUNK_HEAD = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/;

/**
 * A file's diff lines cut into hunks, each side's span from its `@@ -a,b +c,d @@`, when
 * every hunk holds the lines its header counts and as many unchanged lines come before
 * each on both sides; null otherwise, as for a diff cut short inside a hunk, whose
 * gaps cannot be told. A side with no lines names the line before it, as git and
 * difflib write it, so its span starts one further on.
 */
export function hunksOf(body: readonly string[]): Hunk[] | null {
  const starts = body.flatMap((line, index) => (line.startsWith("@@") ? [index] : []));
  if (starts[0] !== 0) return null;
  const hunks: Hunk[] = [];
  for (const [k, start] of starts.entries()) {
    const head = HUNK_HEAD.exec(body[start]!);
    if (!head) return null;
    const lines = body.slice(start + 1, starts[k + 1] ?? body.length);
    const hunk = { header: body[start]!, old: spanOf(head[1]!, head[2]), new: spanOf(head[3]!, head[4]), lines };
    const counted = sideCounts(lines);
    const before = hunks.at(-1);
    if (counted.old !== hunk.old.count || counted.new !== hunk.new.count) return null;
    if (hunk.old.start - endOf(before?.old) !== hunk.new.start - endOf(before?.new)) return null;
    hunks.push(hunk);
  }
  return hunks;
}

/** The unchanged lines a diff leaves out before a hunk, or after the last: each side's first, and how many (null: unknown). */
export type Gap = { readonly old: number; readonly new: number; readonly count: number | null };

/**
 * The gaps around `hunks`: one before each, then one after the last, which is
 * counted only once the file's `total` lines are known.
 */
export function gapsOf(hunks: readonly Hunk[], total: number | null): Gap[] {
  const gaps = hunks.map((hunk, k) => {
    const before = hunks[k - 1];
    return { old: endOf(before?.old), new: endOf(before?.new), count: hunk.new.start - endOf(before?.new) };
  });
  const last = hunks.at(-1);
  const tail = endOf(last?.new);
  return [...gaps, { old: endOf(last?.old), new: tail, count: total === null ? null : Math.max(0, total - tail + 1) }];
}

/** How much of a gap the reader has shown: lines under the hunk above it (`down`) and over the hunk below it (`up`). */
export type Shown = { readonly down: number; readonly up: number };

/**
 * A gap that still hides lines: its place among the gaps, where it stands, and how many
 * it hides, null while the file's length is unknown.
 */
export type OpenGap = { readonly index: number; readonly place: "top" | "between" | "bottom"; readonly hidden: number | null };

/** A hunk as drawn once gaps are shown: its lines, header first, and the gap above it that still hides lines. */
export type ShownHunk = { readonly lines: readonly string[]; readonly above: OpenGap | null };

/**
 * `hunks` with the lines `shown` asks of each gap joined to them as context, read from
 * the file's `lines` (its new side, which holds the same unchanged lines as the old;
 * null before it is read, which shows none). A hunk that takes lines gets a header
 * rebuilt from its spans, and a gap shown whole joins its two hunks into one, as a
 * diff with that much context would have drawn them.
 */
export function withShown(
  hunks: readonly Hunk[],
  shown: readonly Shown[],
  lines: readonly string[] | null,
): { readonly hunks: readonly ShownHunk[]; readonly below: OpenGap | null } {
  const context = (from: number, count: number) => (lines ?? []).slice(from - 1, from - 1 + count).map((line) => ` ${line}`);
  const drawn: Drawn[] = [];
  let below: OpenGap | null = null;
  for (const [index, gap] of gapsOf(hunks, lines ? lines.length : null).entries()) {
    const place: OpenGap["place"] = index === 0 ? "top" : index === hunks.length ? "bottom" : "between";
    const ask = (lines && shown[index]) || NOTHING_SHOWN;
    const count = gap.count ?? 0;
    const down = place === "top" ? 0 : Math.min(ask.down, count);
    const up = place === "bottom" ? 0 : Math.min(ask.up, count - down);
    const hidden = gap.count === null ? null : count - down - up;
    const open = drawn.at(-1);
    const take = (more: readonly string[]) => {
      if (!open || more.length === 0) return;
      open.lines.push(...more);
      open.written = null;
    };
    take(context(gap.new, down));
    const hunk = hunks[index];
    if (!hunk) below = hidden === 0 ? null : { index, place, hidden };
    else if (open && hidden === 0) take([...context(gap.new + down, up), ...hunk.lines]);
    else {
      const above = hidden === 0 ? null : { index, place, hidden };
      drawn.push({ old: hunk.old.start - up, new: hunk.new.start - up, lines: [...context(gap.new + count - up, up), ...hunk.lines], written: up > 0 ? null : hunk.header, above });
    }
  }
  return { hunks: drawn.map(shownOf), below };
}

/**
 * A file's text as its lines, each without its newline, as the code store splits it:
 * at `\n` alone, and a last newline ends the last line rather than starting one.
 */
export function fileLines(text: string): string[] {
  const lines = text.split("\n");
  if (lines.at(-1) === "") lines.pop();
  return lines;
}

const NOTHING_SHOWN: Shown = { down: 0, up: 0 };

/** The first line after a span: where the next gap starts on that side; 1 before the first hunk. */
function endOf(span: Span | undefined): number {
  return span ? span.start + span.count : 1;
}

function spanOf(start: string, count: string | undefined): Span {
  const lines = count === undefined ? 1 : Number(count);
  return { start: lines === 0 ? Number(start) + 1 : Number(start), count: lines };
}

/** How many of a hunk's lines are on each side: context on both, a removed line on the old, an added one on the new. */
function sideCounts(lines: readonly string[]): { readonly old: number; readonly new: number } {
  const marked = (mark: string) => lines.filter((line) => line.startsWith(mark)).length;
  const context = lines.length - marked("+") - marked("-") - marked("\\");
  return { old: context + marked("-"), new: context + marked("+") };
}

/** One side's span in a hunk header: `start,count`, the count left out when it is 1 and the line before named when it is 0. */
function range(start: number, count: number): string {
  return count === 1 ? `${start}` : `${count === 0 ? start - 1 : start},${count}`;
}

/** A hunk being drawn: where each side starts, its lines, and its header as written until it takes lines. */
type Drawn = { readonly old: number; readonly new: number; readonly lines: string[]; written: string | null; readonly above: OpenGap | null };

function shownOf({ old, new: fresh, lines, written, above }: Drawn): ShownHunk {
  const counted = sideCounts(lines);
  return { lines: [written ?? `@@ -${range(old, counted.old)} +${range(fresh, counted.new)} @@`, ...lines], above };
}
