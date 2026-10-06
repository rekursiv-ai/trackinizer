import { expect, test } from "vitest";
import type { SessionRecord } from "../../api/sessions";
import { recordView, type ToolKind, transcriptRows } from "./records";
import { groupRows, groupSummary, languageOf, toolSummary } from "./steps";

/** A record as the server sends one: the payload in its dataclass codec's shape. */
function record(kind: string, payload: { [field: string]: unknown } = {}, text = ""): SessionRecord {
  return {
    idx: 3,
    kind,
    context_id: null,
    timestamp: null,
    model: null,
    payload: { "py/object": `trackinizer.lib.agent.types.sessions.${kind}`, ...payload },
    text,
    ciphertext: null,
  };
}

test("a tool row is one line: what it did, to what, and how it went", () => {
  const view = (kind: string, payload: { [field: string]: unknown }, text = "") => recordView(record(kind, payload, text));
  const bash = view("ToolCall", { name: "Bash", arguments: { command: "pytest -x \\\n  --lf", description: "Run the tests" } });
  const ran = (stdout: string, stderr: string, exit: number) => view("ShellCommandResult", { stdout, stderr, exit_code: exit });
  expect(toolSummary(bash, ran("1 passed\n2 passed\n", "", 0))).toEqual({
    tool: "command",
    verb: "Ran",
    target: "pytest -x \\ …",
    outcome: "2 lines",
    failed: false,
    language: "bash",
  });
  expect(toolSummary(bash, ran("", "boom\n", 2))).toMatchObject({ outcome: "exit 2", failed: true });
  expect(toolSummary(bash, ran("", "", 0))).toMatchObject({ outcome: "no output", failed: false });
  // A call with no result says only what it asked; a result with no call, what it ran.
  expect(toolSummary(bash, null)).toMatchObject({ verb: "Ran", target: "pytest -x \\ …", outcome: "" });
  const codex = view("ShellCommandResult", { command: ["/bin/bash", "-lc", "date -u"], stdout: "now", stderr: "", exit_code: 0 });
  expect(toolSummary(null, codex)).toMatchObject({ verb: "Ran", target: "date -u", outcome: "1 line" });

  const read = view("FileReadResult", { path: "/work/loop/a.py", content: "import os\n\nprint(1)\n" });
  expect(toolSummary(view("ToolCall", { name: "Read", arguments: { file_path: "/work/loop/a.py" } }), read)).toEqual({
    tool: "read",
    verb: "Read",
    target: "/work/loop/a.py",
    outcome: "3 lines",
    failed: false,
    language: "py",
  });
  const splice = { before: "a\nb\n", after: "c\n", lead: null, trail: null, start: null, count: null };
  const edit = view("FileEditResult", { path: "src/b.ts", edits: [splice] });
  expect(toolSummary(null, edit)).toMatchObject({ tool: "edit", verb: "Edited", target: "src/b.ts", outcome: "+1 −2", language: "ts" });
  const write = view("FileWriteResult", { path: "/w/notes.md", content: "# Notes\n" });
  expect(toolSummary(null, write)).toMatchObject({ tool: "write", verb: "Wrote", target: "/w/notes.md", outcome: "1 line", language: "md" });
  const grep = view("ToolCall", { name: "Grep", arguments: { pattern: "x|y", path: "loop" } });
  expect(toolSummary(grep, view("UncategorizedToolResult", { content: "a.py\nb.py" }))).toMatchObject({ tool: "search", verb: "Searched", target: "x|y", outcome: "2 lines" });
  const found = [{ url: "https://a.b/", title: "A", snippet: "" }];
  expect(toolSummary(null, view("WebSearchResults", { query: "pglite", content: found }))).toMatchObject({ verb: "Searched the web", target: "pglite", outcome: "1 result" });
  const fetched = view("WebFetchResult", { url: "https://a.b/", content: "x", code: 200, size: 2048 });
  expect(toolSummary(null, fetched)).toMatchObject({ verb: "Fetched", target: "https://a.b/", outcome: "HTTP 200 · 2.0 KB" });
  // An offloaded result says so in place of a count.
  const offloaded = recordView({ ...record("ShellCommandResult", {}, "head"), payload: { $body: "offloaded" } });
  expect(toolSummary(null, offloaded)).toMatchObject({ tool: "command", outcome: "the first 4 characters; the rest is offloaded" });

  // Any other tool goes by its name, and its arguments in one line.
  const post = view("ToolCall", { name: "mcp__slack__post", arguments: { channel: "#eng", text: "Done.\nShipped." } });
  expect(toolSummary(post, view("UncategorizedToolResult", { content: "posted", extra: { is_error: true } }))).toMatchObject({
    tool: "call",
    verb: "mcp__slack__post",
    target: "channel: #eng, text: Done. …",
    outcome: "1 line",
    failed: true,
  });
  expect(toolSummary(null, view("UncategorizedToolResult", { content: "posted" }))).toMatchObject({ verb: "Tool result", target: "" });
});

test("a group of tool steps is counted by what they did, each verb once, in the order first done", () => {
  const step = (tool: ToolKind) => ({ tool, verb: "", target: "", outcome: "", failed: false, language: "" });
  const tools: ToolKind[] = ["command", "read", "command", "agent", "read", "edit", "call", "search", "search", "list", "web", "fetch", "write"];
  expect(groupSummary(tools.map(step))).toBe(
    "Ran 2 commands, 1 agent and 1 web search, read 2 files, edited 1 file, called 1 tool, searched for 2 patterns, listed 1 directory, fetched 1 page, wrote 1 file",
  );
  expect(groupSummary(["read", "read"].map((tool) => step(tool as ToolKind)))).toBe("Read 2 files");
});

test("a run of two or more tool steps between messages is one group, with the reasoning between them", () => {
  const at = (idx: number, kind: string, payload: { [field: string]: unknown } = {}) => ({ ...record(kind, payload), idx });
  const rows = transcriptRows([
    at(0, "UserMessage", { content: "go" }),
    at(1, "Thinking"),
    at(2, "ToolCall", { call_id: "a", name: "Bash" }),
    at(3, "ShellCommandResult", { call_id: "a", stdout: "" }),
    at(4, "Thinking"),
    at(5, "FileReadResult", { path: "x.py", content: "" }),
    at(6, "Thinking"),
    at(7, "AssistantMessage", { content: "done" }),
    // One step alone is a row of its own; anything but reasoning ends a run.
    at(8, "ToolCall", { name: "Bash" }),
    at(9, "AssistantMessage", { content: "again" }),
    at(10, "ToolCall", { name: "Bash" }),
    at(11, "TurnContext", { model: "b" }),
    at(12, "ToolCall", { name: "Bash" }),
  ]);
  const shown = groupRows(rows).map((item) => ("group" in item ? item.group.map((row) => row.record.idx) : item.record.idx));
  expect(shown).toEqual([0, 1, [2, 4, 5], 6, 7, 8, 9, 10, 11, 12]);
});

test("a file's language is its extension, or its name when it has none", () => {
  expect(["/w/a.PY", "src/x.test.tsx", "Makefile", "dir.d/README", ""].map(languageOf)).toEqual(["py", "tsx", "makefile", "readme", ""]);
});
