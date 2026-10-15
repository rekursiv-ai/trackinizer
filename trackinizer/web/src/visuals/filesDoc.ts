// A files document (`wire/files_doc.py`): what one record's tree holds and how it differs from a
// base, as plain JSON in an Artifact's description. Any source may file one; this reads its shape alone.
import type { DiffLine } from "../detail/transcript/records";

export type FileChange = "added" | "modified" | "deleted";

/** Why a changed file's diff is cut or missing; empty when it is whole. */
export type DiffNote = "" | "clipped" | "withheld" | "omitted";

export type ChangedFile = {
  readonly path: string;
  readonly change: FileChange;
  /** Git's unified diff of this one path, maybe cut at a line end; `""` when not shown. */
  readonly diff: string;
  readonly note: DiffNote;
};

/** A record's tree, read-only: the paths it holds, and the files that differ from `base`. */
export type FilesDoc = {
  readonly kind: "files";
  readonly version: 1;
  /** What `changed` is measured against, as the source names it (`origin/main`, a sha); `""` when unknown. */
  readonly base: string;
  readonly files: readonly string[];
  /** More files exist than are listed, or some were not read. */
  readonly more_files: boolean;
  readonly changed: readonly ChangedFile[];
  readonly more_changes: boolean;
  /** Paths the source withheld from both lists by its own rule. */
  readonly hidden: number;
};

/** The label on every files document. */
export const FILES_LABEL = "files";

/** The prefix of the label that links a files document to each record it describes: `files:of:<record id>`. */
export const FILES_OF = "files:of:";

/** A file or a directory of `fileTree`; a directory's `name` and `path` end in `/`. */
export type FileNode =
  | { readonly kind: "file"; readonly name: string; readonly path: string }
  | { readonly kind: "dir"; readonly name: string; readonly path: string; readonly children: readonly FileNode[] };

/** A files document from an Artifact's description, or null when it is not one this build reads. */
export function parseFilesDoc(text: string | null): FilesDoc | null {
  let value: unknown = null;
  try {
    value = text ? JSON.parse(text) : null;
  } catch {
    return null;
  }
  if (!isObj(value) || value.kind !== "files" || value.version !== 1) return null;
  const ok =
    str(value.base) &&
    list(value.files, str) &&
    bool(value.more_files) &&
    list(value.changed, isChangedFile) &&
    bool(value.more_changes) &&
    count(value.hidden);
  return ok ? (value as unknown as FilesDoc) : null;
}

/**
 * Whether a path matches `path`, case ignored: every path when it is empty; when it holds `*` or
 * `?`, a glob over the whole path (`**` then `/` is zero or more whole directories, `**` any run
 * of characters, `*` any run without `/`, `?` one character but `/`, any other character itself);
 * else a prefix, so `src/` is that directory and `src` also matches `srcs.py`.
 *
 * A glob is matched by every place in it the path can have reached at once, one character at a
 * time, so it costs the path's length times the glob's whatever they hold. A regular expression
 * would backtrack: `**a**a**a**a**a**b` against a long run of `a` takes minutes, and the glob is a
 * param anyone who may show the visual sets, read on every render.
 */
export function pathMatcher(path: string): (candidate: string) => boolean {
  if (!/[*?]/.test(path)) {
    const prefix = path.toLowerCase();
    return (candidate) => candidate.toLowerCase().startsWith(prefix);
  }
  const steps = globSteps(path.toLowerCase());
  return (candidate) => globMatches(steps, candidate.toLowerCase());
}

/**
 * The changed files `file` opens: the one whose path is `file`; else every one whose path is
 * `file` or ends in `/` and `file`, case ignored. None when `file` is empty or names none.
 */
export function openedPaths(changed: readonly ChangedFile[], file: string): string[] {
  if (!file) return [];
  if (changed.some(({ path }) => path === file)) return [file];
  const name = file.toLowerCase();
  return changed
    .map(({ path }) => path)
    .filter((path) => {
      const lower = path.toLowerCase();
      return lower === name || lower.endsWith(`/${name}`);
    });
}

/**
 * `paths` as a tree: in each directory its directories first, then its files, each by name. A
 * directory whose only child is a directory joins it (`a/b/`), as GitHub's file tree shows one.
 */
export function fileTree(paths: readonly string[]): FileNode[] {
  const root: Folder = { folders: new Map(), files: [] };
  for (const path of paths) {
    const parts = path.split("/");
    let folder = root;
    for (const part of parts.slice(0, -1)) {
      const next = folder.folders.get(part) ?? { folders: new Map(), files: [] };
      folder.folders.set(part, next);
      folder = next;
    }
    folder.files.push(parts.at(-1)!);
  }
  return nodesOf(root, "");
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

type Obj = Readonly<Record<string, unknown>>;

const isObj = (value: unknown): value is Obj => typeof value === "object" && value !== null && !Array.isArray(value);
const str = (value: unknown): value is string => typeof value === "string";
const bool = (value: unknown): value is boolean => typeof value === "boolean";
const count = (value: unknown): value is number => typeof value === "number" && Number.isInteger(value) && value >= 0;
const list = <T>(value: unknown, ok: (item: unknown) => item is T): value is readonly T[] => Array.isArray(value) && value.every(ok);

function isChangedFile(value: unknown): value is ChangedFile {
  return (
    isObj(value) &&
    str(value.path) &&
    str(value.diff) &&
    (value.change === "added" || value.change === "modified" || value.change === "deleted") &&
    (value.note === "" || value.note === "clipped" || value.note === "withheld" || value.note === "omitted")
  );
}

/**
 * One step of a glob, per `pathMatcher`: `dirs` is `**` then `/`, zero or more whole directories;
 * `any` is `**`; `star` is `*`; `one` is `?`; any other character is itself.
 */
type GlobStep = "dirs" | "any" | "star" | "one" | { readonly char: string };

/**
 * Where a glob's match can stand after some of the path: at the start of each step (`at`, one
 * past the last step when the glob is done), and inside each `dirs` step, past its zero
 * directories but short of the `/` that ends one (`within`).
 */
type Places = { readonly at: boolean[]; readonly within: boolean[] };

function globSteps(glob: string): GlobStep[] {
  const steps: GlobStep[] = [];
  for (let k = 0; k < glob.length; ) {
    const [step, size]: [GlobStep, number] = glob.startsWith("**/", k)
      ? ["dirs", 3]
      : glob.startsWith("**", k)
        ? ["any", 2]
        : [glob[k] === "*" ? "star" : glob[k] === "?" ? "one" : { char: glob[k]! }, 1];
    steps.push(step);
    k += size;
  }
  return steps;
}

function globMatches(steps: readonly GlobStep[], candidate: string): boolean {
  let places = settle(steps, { at: [true], within: [] });
  for (let k = 0; k < candidate.length; k += 1) places = settle(steps, advance(steps, places, candidate[k]!));
  return places.at[steps.length] === true;
}

/** The places `char` moves each of `places` to. */
function advance(steps: readonly GlobStep[], { at, within }: Places, char: string): Places {
  const next: Places = { at: [], within: [] };
  steps.forEach((step, k) => {
    if (within[k]) {
      next.within[k] = true;
      if (char === "/") next.at[k + 1] = true;
    }
    if (!at[k]) return;
    if (step === "any" || (step === "star" && char !== "/")) next.at[k] = true;
    else if ((step === "one" && char !== "/") || (typeof step === "object" && step.char === char)) next.at[k + 1] = true;
  });
  return next;
}

/** `places` with every place a step that may match nothing reaches from them, in one pass, since steps only move on. */
function settle(steps: readonly GlobStep[], places: Places): Places {
  steps.forEach((step, k) => {
    if (!places.at[k] || step === "one" || typeof step === "object") return;
    places.at[k + 1] = true;
    if (step === "dirs") places.within[k] = true;
  });
  return places;
}

/** One directory as `fileTree` gathers it: its directories by name, and its files' names. */
type Folder = { readonly folders: Map<string, Folder>; readonly files: string[] };

function nodesOf(folder: Folder, prefix: string): FileNode[] {
  const folders = [...folder.folders].sort(([a], [b]) => byName(a, b)).map(([name, inner]) => dirNode(`${name}/`, inner, prefix));
  const files = [...folder.files].sort(byName).map((name): FileNode => ({ kind: "file", name, path: prefix + name }));
  return [...folders, ...files];
}

/** The directory `name` (ending in `/`), joined with its one child while that is all it holds. */
function dirNode(name: string, folder: Folder, prefix: string): FileNode {
  const [only] = folder.folders;
  if (folder.files.length === 0 && folder.folders.size === 1 && only) return dirNode(`${name}${only[0]}/`, only[1], prefix);
  return { kind: "dir", name, path: prefix + name, children: nodesOf(folder, prefix + name) };
}

/** Names in code-point order, as `git ls-files` sorts paths. */
function byName(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}
