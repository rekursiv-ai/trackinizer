import { type DiffLine, type RecordView, type ToolKind, toolKind, type TranscriptRow } from "./records";

/**
 * A tool step's one line, as the Claude and Codex CLIs summarize one: a verb
 * (or, for any other tool, its name), what it acted on, and how it went.
 */
export type ToolSummary = {
  readonly tool: ToolKind;
  readonly verb: string;
  /** Its command, path, pattern, URL or query, or its arguments, in one line: `…` ends one cut short. */
  readonly target: string;
  /** How it went: `exit 1`, `45 lines`, `+3 −1`, `2 results`, its result's own note; `""` with no result. */
  readonly outcome: string;
  readonly failed: boolean;
  /** The highlight.js language its command or file is in: `bash`, a file's extension, or `""`. */
  readonly language: string;
};

/**
 * The one line a tool step shows: its call, its result, or both. What it did
 * comes from the result's kind unless that is the opaque `call`; what it acted on
 * from the result (a path, a query, a URL, a command) else the call's arguments.
 */
export function toolSummary(call: RecordView | null, result: RecordView | null): ToolSummary {
  const tool = result?.tool && (result.tool !== "call" || !call) ? result.tool : (call?.tool ?? "call");
  const asked = call?.shape === "tool" ? call.primary || call.args.map(([name, value]) => `${name}: ${value}`).join(", ") : "";
  const where = result?.shape === "shell" ? result.command : result && "source" in result ? result.source : "";
  const target = where || asked;
  const named = call?.shape === "tool" ? call.name : result?.label;
  return {
    tool,
    verb: tool === "call" ? named || "Tool" : TOOL_VERBS[tool],
    target: oneLine(target),
    outcome: result ? outcomeOf(result) : "",
    failed: result?.failed ?? false,
    language: tool === "command" ? "bash" : tool === "read" || tool === "edit" || tool === "write" ? languageOf(target) : "",
  };
}

/**
 * What a group of tool steps did, as one sentence: each kind counted, the kinds
 * that share a verb under it once, in the order first done (`Ran 2 commands and
 * 1 agent, read 3 files`).
 */
export function groupSummary(steps: readonly ToolSummary[]): string {
  const counts = new Map<ToolKind, number>();
  for (const { tool } of steps) counts.set(tool, (counts.get(tool) ?? 0) + 1);
  const byVerb = new Map<string, string[]>();
  for (const [tool, count] of counts) {
    const [verb, one, many] = GROUP_PHRASES[tool];
    byVerb.set(verb, [...(byVerb.get(verb) ?? []), `${count.toLocaleString("en")} ${count === 1 ? one : many}`]);
  }
  const text = [...byVerb]
    .map(([verb, counted]) => `${verb} ${counted.length > 1 ? `${counted.slice(0, -1).join(", ")} and ${counted.at(-1)}` : counted[0]}`)
    .join(", ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** A run of two or more tool steps, with the reasoning between them (`groupRows`). */
export type ToolGroup = { readonly group: readonly TranscriptRow[] };

/**
 * `rows` with each run of two or more tool steps between other lines folded into
 * one group, as the Claude CLI folds reads and searches and its desktop app and
 * focus view fold a turn's tool calls. Reasoning between two steps joins their
 * group, as Codex keeps it in an exploring cell; reasoning before the first or
 * after the last stays a line of its own. Any other line ends the run.
 */
export function groupRows(rows: readonly TranscriptRow[]): (TranscriptRow | ToolGroup)[] {
  const items: (TranscriptRow | ToolGroup)[] = [];
  let k = 0;
  while (k < rows.length) {
    let end = k;
    while (end < rows.length && (isTool(rows[end]!) || rows[end]!.record.kind === "Thinking")) end++;
    let first = k;
    while (first < end && !isTool(rows[first]!)) first++;
    let last = end;
    while (last > first && !isTool(rows[last - 1]!)) last--;
    if (rows.slice(first, last).filter(isTool).length < 2) {
      end = Math.max(end, k + 1);
      items.push(...rows.slice(k, end));
    } else {
      items.push(...rows.slice(k, first), { group: rows.slice(first, last) }, ...rows.slice(last, end));
    }
    k = end;
  }
  return items;
}

/** The highlight.js language a file at `path` is in: its extension, or its name when it has none. */
export function languageOf(path: string): string {
  const name = path.slice(path.lastIndexOf("/") + 1);
  return (/\.([^.]+)$/.exec(name)?.[1] ?? name).toLowerCase();
}

/** Whether `row` is a tool step: a call, with its result or not, or a result alone. */
function isTool(row: TranscriptRow): boolean {
  return !row.stream && toolKind(row.record) !== null;
}

/** How a tool step went, from its result: its exit, what it changed or found, or how much it said. */
function outcomeOf(result: RecordView): string {
  switch (result.shape) {
    case "shell":
      return result.exit !== null && result.exit !== 0 ? `exit ${result.exit}` : linesOf(result.stdout, result.stderr);
    case "edit": {
      const count = (kind: DiffLine["kind"]) => result.lines.filter((line) => line.kind === kind).length;
      return `+${count("add")} −${count("del")}`;
    }
    case "search":
      return `${result.results.length} ${result.results.length === 1 ? "result" : "results"}`;
    case "output":
      return result.meta || linesOf(result.text);
    default:
      return "";
  }
}

/** How many lines `texts` hold together, the newline that ends one left out: `3 lines`, or `no output`. */
function linesOf(...texts: string[]): string {
  const count = texts.reduce((sum, text) => sum + (text ? text.replace(/\n$/, "").split("\n").length : 0), 0);
  return count ? `${count.toLocaleString("en")} ${count === 1 ? "line" : "lines"}` : "no output";
}

/** `text`'s first line, with `…` when more follow. */
function oneLine(text: string): string {
  const [first, ...rest] = text.trim().split("\n");
  return rest.length ? `${first} …` : first!;
}

/** A step's verb on its row, as the CLIs word theirs (`Ran`, `Read`, `Edited`); a `call` takes its tool's name. */
const TOOL_VERBS: { readonly [tool in Exclude<ToolKind, "call">]: string } = {
  command: "Ran",
  read: "Read",
  edit: "Edited",
  write: "Wrote",
  search: "Searched",
  list: "Listed",
  web: "Searched the web",
  fetch: "Fetched",
  agent: "Ran agent",
};

/** How a group counts each kind of step: its verb, and its noun for one and for more. */
const GROUP_PHRASES: { readonly [tool in ToolKind]: readonly [verb: string, one: string, many: string] } = {
  command: ["ran", "command", "commands"],
  agent: ["ran", "agent", "agents"],
  web: ["ran", "web search", "web searches"],
  read: ["read", "file", "files"],
  edit: ["edited", "file", "files"],
  write: ["wrote", "file", "files"],
  search: ["searched for", "pattern", "patterns"],
  list: ["listed", "directory", "directories"],
  fetch: ["fetched", "page", "pages"],
  call: ["called", "tool", "tools"],
};
