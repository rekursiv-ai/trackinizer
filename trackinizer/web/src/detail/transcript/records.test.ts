import { expect, test } from "vitest";
import type { SessionRecord } from "../../api/sessions";
import { ansiRuns, clipLines, clipText, harnessBlocks, lineNumbers, markBookkeeping, partLabel, recordView, transcriptRows, unreadable } from "./records";

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

test("messages show their prose under who wrote them", () => {
  expect(recordView(record("UserMessage", { content: "Why?" }))).toEqual({
    shape: "message",
    label: "User",
    text: "Why?",
    context: null,
    long: false,
    attachments: "",
    tool: null,
    failed: false,
  });
  expect(recordView(record("AssistantMessage", { content: "Because." }))).toMatchObject({ label: "Assistant" });
  expect(recordView(record("AgentToAgentMessage", { sender: "craftax-arm", content: "done" }))).toMatchObject({
    label: "From craftax-arm",
  });
  expect(recordView(record("SystemMessage", { subtype: "init", content: "hi" }))).toMatchObject({ label: "System · init" });
});

test("reasoning folds away; a sealed block has nothing to show", () => {
  expect(recordView(record("Thinking", { content: "Hmm." }))).toEqual({ shape: "thinking", label: "Thinking", text: "Hmm.", attachments: "", tool: null, failed: false });
  expect(recordView(record("Thinking", { content: null, summary: "Planned." }))).toMatchObject({ text: "Planned." });
  expect(recordView(record("Thinking", { content: null, encrypted: "" }))).toMatchObject({ text: "" });
});

test("a tool call shows its main argument as written, and its other arguments apart (B6)", () => {
  expect(recordView(record("ToolCall", { name: "Bash", arguments: { command: "ls -la", description: "List" } }))).toEqual({
    shape: "tool",
    label: "Tool call",
    name: "Bash",
    primary: "ls -la",
    args: [["description", "List"]],
    attachments: "",
    tool: "command",
    failed: false,
  });
  // Codex's shell names its argv, which the codec writes as a tuple; the shell
  // that runs a script goes, as Codex's own TUI drops it.
  const argv = { command: { "py/tuple": ["bash", "-lc", "ls -la"] }, workdir: "/w" };
  expect(recordView(record("ToolCall", { name: "shell", arguments: argv }))).toMatchObject({
    primary: "ls -la",
    args: [["workdir", "/w"]],
  });
  const opaque = { channel: "#eng", blocks: { "py/tuple": [{ type: "section" }] } };
  expect(recordView(record("ToolCall", { name: "mcp__slack__post", arguments: opaque }))).toMatchObject({
    primary: "",
    args: [
      ["channel", "#eng"],
      ["blocks", '[\n  {\n    "type": "section"\n  }\n]'],
    ],
  });
});

test("a shell result names its command as a shell would read it, apart from its output and exit (B8)", () => {
  const argv = { "py/tuple": ["echo", "it's", "a b", "--x=1"] };
  expect(recordView(record("ShellCommandResult", { command: argv, stdout: "it's\n", stderr: "", exit_code: 0 }))).toEqual({
    shape: "shell",
    label: "Shell command",
    command: `echo 'it'"'"'s' 'a b' --x=1`,
    stdout: "it's\n",
    stderr: "",
    exit: 0,
    attachments: "",
    tool: "command",
    failed: false,
  });
  const script = { "py/tuple": ["/bin/bash", "-lc", "cd /w && date -u"] };
  expect(recordView(record("ShellCommandResult", { command: script, stdout: "", stderr: "", exit_code: 0 }))).toMatchObject({ command: "cd /w && date -u" });
  // Sagent's argv is the whole command line as one word.
  const line = { "py/tuple": ["date -u +%s; ls 'a b'"] };
  expect(recordView(record("ShellCommandResult", { command: line, stdout: "", stderr: "", exit_code: 0 }))).toMatchObject({ command: "date -u +%s; ls 'a b'" });
});

test("an edit is a unified diff: the provider's own context and header, else the line it named (B9)", () => {
  const edits = {
    "py/tuple": [
      { before: "old\n", after: "new\n", lead: "@@ -3,3 +3,3 @@\n ctx a\n", trail: " ctx b\n", start: 4, count: 1, bare: { "py/set": [] } },
      { before: "x\n", after: "", lead: null, trail: null, start: 10, count: 1, bare: { "py/set": [] } },
    ],
  };
  const view = recordView(record("FileEditResult", { path: "a.py", edits }));
  expect(view).toMatchObject({ shape: "edit", label: "File edit", source: "a.py" });
  expect(view.shape === "edit" && view.lines.map((line) => `${line.kind}:${line.text}`)).toEqual([
    "hunk:@@ -3,3 +3,3 @@",
    "ctx: ctx a",
    "del:-old",
    "add:+new",
    "ctx: ctx b",
    "hunk:@@ -10,1 @@",
    "del:-x",
  ]);
  // Each line's number in the file it is in: the new file's for context and
  // added lines, the old one's for removed lines; none where the provider gave none.
  expect(view.shape === "edit" && view.lines.map((line) => line.line)).toEqual([null, 3, 4, 4, 5, null, 10]);
  const bare = recordView(record("FileEditResult", { path: "a.py", edits: { "py/tuple": [{ before: "a\n", after: "b\n", lead: null, trail: null }] } }));
  expect(bare.shape === "edit" && bare.lines.map((line) => line.line)).toEqual([null, null]);
});

test("an injected canvas context splits from what was said; a malformed one still folds, unlinked (B12)", () => {
  const marker = "\nTrackinizer context (verify with trax): ";
  expect(recordView(record("UserMessage", { content: "plain" }))).toMatchObject({ text: "plain", context: null });
  expect(recordView(record("UserMessage", { content: `hi${marker}{not json\nCanvas commands: x` }))).toMatchObject({
    text: "hi",
    context: { text: "{not json\nCanvas commands: x", recordId: "", title: "" },
  });
});

test("bookkeeping is the harness's records; a legacy turn and a model change are not (B5)", () => {
  const records = [
    record("TurnContext", { model: "a" }),
    record("UserMessage", { content: "hi" }),
    record("TurnContext", { model: "a" }),
    record("TurnContext", { model: null }),
    record("TurnContext", { model: "b" }),
    record("UncategorizedRecord", { kind: "legacy/UserMessage", payload: {} }),
    record("UncategorizedRecord", { kind: "queue-operation", payload: {} }),
    record("TokenUsage"),
    record("ContextState"),
    record("SystemMessage"),
    record("Thinking", { content: null, summary: "Planned." }),
  ];
  expect(markBookkeeping(records)).toEqual([true, false, true, true, false, false, true, true, true, true, false]);
  expect(recordView(records[4]!)).toEqual({ shape: "note", label: "Model: b", meta: "", attachments: "", tool: null, failed: false });
});

test("a tool result shows its text, where it acted, and whether it failed", () => {
  expect(recordView(record("ShellCommandResult", { exit_code: 2 }, "no such file"))).toEqual({
    shape: "output",
    label: "Shell command",
    source: "",
    meta: "",
    text: "no such file",
    failed: true,
    attachments: "",
    tool: "command",
  });
  expect(recordView(record("FileReadResult", { path: "/work/a.py" }, "print(1)"))).toMatchObject({
    label: "File read",
    source: "/work/a.py",
    failed: false,
  });
  expect(recordView(record("WebSearchResults", { query: "pglite" }, "…"))).toMatchObject({ label: "Web search", source: "pglite" });
});

test("bookkeeping is one line, and a kind this view does not know still shows its text", () => {
  expect(recordView(record("TokenUsage", { info: { input_tokens: 3 } }))).toEqual({ shape: "note", label: "Token usage", meta: "", attachments: "", tool: null, failed: false });
  expect(recordView(record("TurnContext"))).toEqual({ shape: "note", label: "Turn context", meta: "", attachments: "", tool: null, failed: false });
  expect(recordView(record("ContextSnapshot", {}, "what a newer capture wrote"))).toMatchObject({
    shape: "output",
    label: "Context snapshot",
    text: "what a newer capture wrote",
  });
});

test("a legacy turn names its old kind, and shows its old payload when it has no text", () => {
  const legacy = record("UncategorizedRecord", { kind: "legacy/UserMessage", payload: { text: "turn 0" } });
  expect(recordView(legacy)).toMatchObject({ shape: "output", label: "legacy/UserMessage", text: "text: turn 0" });
  expect(recordView({ ...legacy, text: "turn 0 as indexed" })).toMatchObject({ text: "turn 0 as indexed" });
});

test("a provider's own record shows its fields, a message in one as written, not as escaped JSON", () => {
  // Claude Code's queue, trimmed from a captured line.
  const queued = record("UncategorizedRecord", {
    kind: "queue-operation",
    payload: { type: "queue-operation", operation: "enqueue", content: "[room-a] someone: Report read.\nGo ahead." },
  });
  expect(recordView(queued)).toMatchObject({
    shape: "output",
    label: "queue-operation",
    text: "type: queue-operation\noperation: enqueue\ncontent: [room-a] someone: Report read.\nGo ahead.",
  });
  // Codex's echo of a user turn nests the message, which reads as written too.
  const echo = record("UncategorizedRecord", {
    kind: "event_msg/item_completed/UserMessage",
    payload: {
      type: "item_completed",
      item: { type: "UserMessage", content: { "py/tuple": [{ type: "text", text: "Line one.\nLine two." }, "x"] }, meta: {} },
    },
  });
  expect(recordView(echo)).toMatchObject({
    text: "type: item_completed\nitem:\n  type: UserMessage\n  content:\n    - type: text\n      text: Line one.\nLine two.\n    - x\n  meta: {}",
  });
  // A tool that answers with one JSON object, as Claude's SendMessage does.
  const sent = record("UncategorizedToolResult", {
    content: `{"success":false,"message":"No agent named 'arm-b' is reachable.\\nUse ListAgents to see everyone you can message."}`,
  });
  expect(recordView(sent)).toMatchObject({
    text: "success: false\nmessage: No agent named 'arm-b' is reachable.\nUse ListAgents to see everyone you can message.",
  });
});

test("a codex script's output shows each command's output as written, under its exit, not as JSON lines", () => {
  // Codex's `exec` tool, trimmed from captured results: each `text(...)` prints
  // a JSON line, and a command's output is a string inside it.
  const content = [
    "Script completed",
    "Wall time 1.4 seconds",
    "Output:",
    "",
    `{"chunk_id":"688ddd","wall_time_seconds":0.0000076,"exit_code":0,"original_token_count":188,"output":"ls: cannot access '.venv': No such file or directory\\ncpython-3.12-linux-aarch64-gnu\\n"}`,
    `{"status":"fulfilled","value":{"name":"arms_1","chunk_id":"8dd3ba","wall_time_seconds":0.0000094,"exit_code":2,"original_token_count":3,"output":"\\"\\"\\"N, R and H.\\"\\"\\""}}`,
    `{"chunk_id":"be772c","wall_time_seconds":1.0008,"session_id":76816,"original_token_count":0,"output":""}`,
    `{"current_time":"2026-10-01T18:38:15Z"}`,
    "plain line",
  ].join("\n");
  expect(recordView(record("UncategorizedToolResult", { content }))).toMatchObject({
    shape: "output",
    label: "Tool result",
    meta: "Script completed · Wall time 1.4 seconds",
    text: [
      "[exit 0]",
      "ls: cannot access '.venv': No such file or directory",
      "cpython-3.12-linux-aarch64-gnu",
      "[arms_1 · exit 2]",
      `"""N, R and H."""`,
      "[session 76816]",
      `{"current_time":"2026-10-01T18:38:15Z"}`,
      "plain line",
    ].join("\n"),
  });
  // An older script writes no blank line after `Output:`; one still running says so.
  const running = "Script running with cell ID 15\nWall time 1.0 seconds\nOutput:\nfirst line";
  expect(recordView(record("UncategorizedToolResult", { content: running }))).toMatchObject({
    meta: "Script running with cell ID 15 · Wall time 1.0 seconds",
    text: "first line",
  });
});

test("a message queued while the agent worked is conversation, read from where Claude put it", () => {
  // Claude Code's `queued_command` attachment, trimmed: the prompt sits in the
  // attachment, and the capture kept no `content`.
  const queued = (origin: string, prompt: string) =>
    record("ContextState", {
      kind: "queued_command",
      content: null,
      extra: { attachment: { prompt, commandMode: "prompt", origin: { kind: origin } } },
    });
  const human = queued("human", "[room-a] someone: Report read. Source freeze accepted.");
  expect(recordView(human)).toEqual({
    shape: "message",
    label: "User · queued",
    text: "[room-a] someone: Report read. Source freeze accepted.",
    context: null,
    long: false,
    attachments: "",
    tool: null,
    failed: false,
  });
  const notice = queued("task-notification", "<task-notification>\n<task-id>b03k6kngg</task-id>\n</task-notification>");
  // A task's notice reads as one, by its status and summary (`taskNotice`).
  expect(recordView(notice)).toMatchObject({ shape: "message", label: "Task notification" });
  // Once the capture states the prompt as the content, that is read.
  expect(recordView(record("ContextState", { kind: "queued_command", content: "stated", extra: {} }))).toMatchObject({ text: "stated" });
  expect(markBookkeeping([human, notice, record("ContextState", { kind: "date", content: null })])).toEqual([false, false, true]);
});

test("injected state with no prose folds the state its provider stated", () => {
  // Codex's `world_state` and Claude's `date` attachment, trimmed.
  const codex = record("ContextState", { kind: "world_state", content: null, extra: { state: { model: "m", agents_md: { text: "# Global\nRules." } } } });
  expect(recordView(codex)).toMatchObject({ shape: "folded", summary: "world_state", text: "model: m\nagents_md:\n  text: # Global\nRules." });
  const claude = record("ContextState", { kind: "date", content: null, extra: { attachment: { date: "2026-10-01" } } });
  expect(recordView(claude)).toMatchObject({ text: "date: 2026-10-01" });
});

test("a message the harness wrote on the user's turn says so, and folds when long under its first line", () => {
  const meta = (content: string) => record("UserMessage", { content, extra: { isMeta: true } });
  expect(recordView(meta("Stop hook feedback:\n[Make the signal robust]"))).toMatchObject({
    shape: "message",
    label: "Harness",
    text: "Stop hook feedback:\n[Make the signal robust]",
  });
  const skill = `Base directory for this skill: /skills/trax\n\n${"x".repeat(1200)}`;
  expect(recordView(meta(skill))).toMatchObject({ shape: "folded", label: "Harness", summary: "Base directory for this skill: /skills/trax", text: skill });
  const hook = `A session-scoped Stop hook is now active with condition: "${"y".repeat(1200)}"`;
  expect(recordView(meta(hook))).toMatchObject({ summary: `${hook.slice(0, 119)}…` });
});

test("a codex peer message shows its payload without the envelope codex wraps it in; a sealed one has nothing to read", () => {
  const peer = (content: string) => record("AgentToAgentMessage", { content, sender: "/root/prototype", recipient: "/root" });
  const envelope = (type: string) => `Message Type: ${type}\nTask name: /root\nSender: /root/prototype\nPayload:\n`;
  const answer = peer(`${envelope("FINAL_ANSWER")}Implemented **it**.\n\n- one`);
  expect(recordView(answer)).toMatchObject({ shape: "message", label: "From /root/prototype", text: "Implemented **it**.\n\n- one" });
  // A MESSAGE whose payload codex keeps only as ciphertext (`extra.$templates`) has nothing to read.
  const sealed = peer(envelope("MESSAGE"));
  expect(recordView(sealed)).toMatchObject({ shape: "message", text: "" });
  // Prose that only names the envelope's fields is not one.
  const prose = peer("Message Type: MESSAGE is what codex writes first.");
  expect(recordView(prose)).toMatchObject({ text: "Message Type: MESSAGE is what codex writes first." });
  expect([answer, sealed, prose].map(unreadable)).toEqual([false, true, false]);
});

test("a slash command a person typed shows as they typed it, and what it printed as its output, not as tags (claude)", () => {
  const typed = "<command-name>/goal</command-name>\n            <command-message>goal</command-message>\n            <command-args>Deliver a memo per NEXT-W<k>.md.</command-args>";
  expect(recordView(record("UserMessage", { content: typed }))).toMatchObject({ shape: "message", label: "User", text: "/goal Deliver a memo per NEXT-W<k>.md." });
  const bare = "<command-name>/clear</command-name>\n<command-message>clear</command-message>\n<command-args></command-args>";
  expect(recordView(record("UserMessage", { content: bare }))).toMatchObject({ label: "User", text: "/clear" });
  const printed = "<local-command-stdout>Goal set: Deliver a memo.</local-command-stdout>";
  expect(recordView(record("UserMessage", { content: printed }))).toMatchObject({ shape: "message", label: "Command output", text: "Goal set: Deliver a memo." });
  // A person's message that holds a tag among other text keeps it, folded as before.
  expect(recordView(record("UserMessage", { content: `Look: ${printed}` }))).toMatchObject({ label: "User", text: `Look: ${printed}` });
});

test("context codex writes on the user's turn is the harness's, folded under its source, and bookkeeping (codex)", () => {
  const context = `<codex_internal_context source="goal">\n${"Continue working toward the active thread goal.\n".repeat(30)}</codex_internal_context>`;
  const wrote = record("UserMessage", { content: context });
  expect(recordView(wrote)).toMatchObject({ shape: "folded", label: "Harness", summary: "codex_internal_context · goal" });
  // A person's text beside a block is theirs, the block folded in it.
  const typed = record("UserMessage", { content: `Look at this.\n${context}` });
  expect(recordView(typed)).toMatchObject({ shape: "message", label: "User" });
  expect(markBookkeeping([wrote, typed])).toEqual([true, false]);
});

test("a background task's notice shows its status, summary and result, not its tags (claude)", () => {
  const notice = (inner: string) => record("UserMessage", { content: `<task-notification>\n<task-id>b1</task-id>\n<tool-use-id>toolu_1</tool-use-id>\n<output-file>/tmp/b1.output</output-file>\n${inner}\n</task-notification>` });
  const ran = notice('<status>completed</status>\n<summary>Background command "Run P5" completed (exit code 0)</summary>');
  expect(recordView(ran)).toMatchObject({ shape: "message", label: "Task notification · completed", text: 'Background command "Run P5" completed (exit code 0)' });
  const answered = notice("<status>completed</status>\n<summary>Agent \"Analyst\" finished</summary>\n<note>It may notify again.</note>\n<result>All **seven** constraints hold.</result>");
  expect(recordView(answered)).toMatchObject({ label: "Task notification · completed", text: 'Agent "Analyst" finished\n\nAll **seven** constraints hold.' });
  expect(markBookkeeping([ran])).toEqual([false]);
  // Queued while the agent worked, it reads the same.
  const queued = record("ContextState", { kind: "queued_command", content: null, extra: { attachment: { prompt: ran.payload!.content, origin: { kind: "task-notification" } } } });
  expect(recordView(queued)).toMatchObject({ label: "Task notification · completed", text: 'Background command "Run P5" completed (exit code 0)' });
});

test("a record with nothing to read, which no view draws or counts: sealed reasoning with no summary, and a sealed codex peer message", () => {
  const envelope = "Message Type: MESSAGE\nTask name: /root\nSender: /root/prototype\nPayload:\n";
  const peer = (content: string, attachments: unknown[] = []) => record("AgentToAgentMessage", { content, sender: "/root/prototype", attachments: { "py/tuple": attachments } });
  const records = [
    record("Thinking", { content: null, encrypted: "gAAAA", summary: null }),
    record("Thinking", { content: "", encrypted: "", summary: null }),
    peer(envelope),
    record("Thinking", { content: null, encrypted: "gAAAA", summary: "**Planned** the run." }),
    record("Thinking", { content: "Hmm." }),
    peer(`${envelope}Done.`),
    peer(envelope, [{ mime_descriptor: "image/png" }]),
    record("AssistantMessage", { content: "" }),
    // An empty one is bookkeeping, as an empty message of any kind is; only codex's seal hides it.
    peer(" \n"),
  ];
  expect(records.map(unreadable)).toEqual([true, true, true, false, false, false, false, false, false]);
  // A summary is what reasoning shows when its text is sealed.
  expect(recordView(records[3]!)).toMatchObject({ shape: "thinking", text: "**Planned** the run." });
});

test("a tool result whose fields are empty shows what the model read instead (claude)", () => {
  // Claude's failed Read and backgrounded Bash, trimmed: the typed fields are
  // empty and the block's text, what the model read, is kept beside them.
  const failed = record("FileReadResult", {
    path: null,
    content: null,
    extra: { $result: { block: { is_error: true }, tool_name: "Read", text: "File does not exist." } },
  });
  expect(recordView(failed)).toMatchObject({ shape: "output", label: "File read", text: "File does not exist.", failed: true });
  const background = record("ShellCommandResult", {
    command: null,
    stdout: "",
    stderr: "",
    exit_code: null,
    extra: { $result: { block: { is_error: false }, tool_name: "Bash", text: "Command running in background with ID: b44ic5nb0." } },
  });
  expect(recordView(background)).toMatchObject({ shape: "shell", stdout: "Command running in background with ID: b44ic5nb0." });
  const refused = record("UncategorizedToolResult", {
    content: "No agent named 'arm-b'.",
    extra: { $result: { block: { is_error: true }, tool_name: "SendMessage" } },
  });
  expect(recordView(refused)).toMatchObject({ failed: true });
});

test("parts are named by number, file, format and size; part -1 as the legacy backfill", () => {
  const part = { part: 0, name: "s.jsonl", format: "claude", records: 250, metadata: {}, ir_id: null };
  expect(partLabel(part)).toBe("Part 0 · s.jsonl · claude · 250 records");
  expect(partLabel({ ...part, part: -1, name: "legacy", format: "", records: 1 })).toBe("Legacy turns, backfilled · 1 record");
});

test("long text is clipped, saying how much was left out", () => {
  expect(clipText("abcdef", 4)).toEqual({ shown: "abcd", hidden: 2 });
  expect(clipText("abc", 4)).toEqual({ shown: "abc", hidden: 0 });
});

test("long output keeps its first and last lines, and says how many it left out between (B7)", () => {
  const text = Array.from({ length: 7 }, (_, k) => `line ${k}`).join("\n");
  expect(clipLines(text, 2, 3)).toEqual({ head: "line 0\nline 1", hidden: 2, tail: "line 4\nline 5\nline 6" });
  expect(clipLines(text, 2, 5)).toEqual({ head: text, hidden: 0, tail: "" });
});

test("only a harness's tags fold: a person's HTML and an unclosed tag stay prose (B13)", () => {
  expect(harnessBlocks("a <b>bold</b> word <system-reminder>\nnote\n</system-reminder> end")).toEqual([
    { tag: "", text: "a <b>bold</b> word " },
    { tag: "system-reminder", text: "note" },
    { tag: "", text: " end" },
  ]);
  expect(harnessBlocks("<command-name>/clear</command-name>")).toEqual([{ tag: "command-name", text: "/clear" }]);
  expect(harnessBlocks("<system-reminder> never closed")).toEqual([{ tag: "", text: "<system-reminder> never closed" }]);
  // Codex names the source of the context it writes on a user turn in the tag.
  expect(harnessBlocks('<codex_internal_context source="goal">\nKeep going.\n<objective>\nX\n</objective>\n</codex_internal_context>')).toEqual([
    { tag: "codex_internal_context · goal", text: "Keep going.\n<objective>\nX\n</objective>" },
  ]);
  // Codex wraps AGENTS.md in a capitalized tag on a user turn.
  expect(harnessBlocks("# AGENTS.md instructions for /w\n\n<INSTRUCTIONS>\n# Global\n</INSTRUCTIONS>")).toEqual([
    { tag: "", text: "# AGENTS.md instructions for /w\n\n" },
    { tag: "INSTRUCTIONS", text: "# Global" },
  ]);
});

test("a harness's tags in code stay code, fenced or inline, and fold again after it (DR-09)", () => {
  const fenced = "Example:\n\n```xml\n<system-reminder>sample</system-reminder>\n```\n";
  expect(harnessBlocks(fenced)).toEqual([{ tag: "", text: fenced }]);
  const inline = "Wrap it as `<system-reminder>x</system-reminder>` here.";
  expect(harnessBlocks(inline)).toEqual([{ tag: "", text: inline }]);
  // An opening tag in code does not pair with a closing one after it.
  const after = "~~~\n<system-reminder>\n~~~\n<system-reminder>real</system-reminder>";
  expect(harnessBlocks(after)).toEqual([
    { tag: "", text: "~~~\n<system-reminder>\n~~~\n" },
    { tag: "system-reminder", text: "real" },
  ]);
  // A block's own text is not Markdown: a fence in it neither ends nor hides it.
  expect(harnessBlocks("<system-reminder>\n```\n</system-reminder> `a`")).toEqual([
    { tag: "system-reminder", text: "```" },
    { tag: "", text: " `a`" },
  ]);
  const open = "```\n<system-reminder>never closed</system-reminder>";
  expect(harnessBlocks(open)).toEqual([{ tag: "", text: open }]);
  // A backtick with no partner before its paragraph ends is text, not code.
  expect(harnessBlocks("a ` b\n\n<system-reminder>x</system-reminder>")).toEqual([
    { tag: "", text: "a ` b\n\n" },
    { tag: "system-reminder", text: "x" },
  ]);
});

test("an agent's state, kind and time show whatever its content, and its text when it has none (DR-10)", () => {
  const status = (content: string | null) =>
    recordView(
      record("AgentStatusResult", { agent_kind: "Explore", prompt: "Find the uses.", content, state: "async_launched", duration_sec: 2.4 }, "Find the uses."),
    );
  for (const content of [null, ""]) {
    expect(status(content)).toMatchObject({ shape: "output", label: "Agent status", meta: "async_launched · Explore · 2 s", text: "Find the uses." });
  }
});

test("a call takes the first result after it with its call_id, however far on, and a run of terminal records is one line (B18, B22, TX-07)", () => {
  const at = (idx: number, kind: string, payload: { [field: string]: unknown } = {}) => ({ ...record(kind, payload), idx });
  const call = (idx: number, id: string) => at(idx, "ToolCall", { call_id: id, name: "Bash" });
  const answer = (idx: number, id: string) => at(idx, "ShellCommandResult", { call_id: id, stdout: "" });
  const rows = transcriptRows([call(0, "a"), answer(9, "a"), call(10, "b"), answer(20, "b"), answer(21, "c"), at(22, "Stdout"), at(23, "Stderr"), at(24, "Stdin")]);
  expect(rows.map((row) => [row.record.idx, row.result?.idx ?? null, row.stream?.length ?? 0])).toEqual([
    [0, 9, 0],
    [10, 20, 0],
    [21, null, 0],
    [22, null, 3],
  ]);
  // Ten calls in a batch, then their ten results: ten steps, each with its own.
  const batch = transcriptRows([
    ...Array.from({ length: 10 }, (_, k) => call(k, `t${k}`)),
    ...Array.from({ length: 10 }, (_, k) => answer(10 + k, `t${k}`)),
  ]);
  expect(batch.map((row) => [row.record.idx, row.result?.idx])).toEqual(Array.from({ length: 10 }, (_, k) => [k, 10 + k]));
});

test("a tool step is named by what it did: its result's kind, else its call's name", () => {
  const call = (name: string) => recordView(record("ToolCall", { name, arguments: {} })).tool;
  const names = ["Bash", "exec_command", "Read", "ReadFile", "Edit", "apply_patch", "Write", "Grep", "Glob", "SearchCode", "List", "WebSearch", "WebFetch", "Agent", "Task"];
  expect(names.map(call)).toEqual(["command", "command", "read", "read", "edit", "edit", "write", "search", "search", "search", "list", "web", "fetch", "agent", "agent"]);
  // Codex's script tool, an MCP tool: what they did is not known from the name.
  expect(["exec", "mcp__slack__post"].map(call)).toEqual(["call", "call"]);
  const result = (kind: string) => recordView(record(kind)).tool;
  const results = ["ShellCommandResult", "FileReadResult", "FileEditResult", "FileWriteResult", "WebSearchResults", "WebFetchResult", "AgentStatusResult", "UncategorizedToolResult"];
  expect(results.map(result)).toEqual(["command", "read", "edit", "write", "web", "fetch", "agent", "call"]);
  expect(["UserMessage", "Thinking", "TokenUsage", "UncategorizedRecord", "Stdout"].map(result)).toEqual([null, null, null, null, null]);
});

test("ANSI colours become styled runs of text, and other escape codes go", () => {
  expect(ansiRuns("\u001b[32m412 passed\u001b[0m in \u001b[1;31m9.01s\u001b[0m\n")).toEqual([
    { text: "412 passed", style: "a-green" },
    { text: " in ", style: "" },
    { text: "9.01s", style: "a-bold a-red" },
    { text: "\n", style: "" },
  ]);
  // Cursor moves and 256-colour or true colour picks have no colour here; bright
  // colours are their plain ones; dim lasts until normal intensity.
  expect(ansiRuns("\u001b[2K\u001b[1Aplain \u001b[38;5;208mx\u001b[39m \u001b[92my\u001b[2mz\u001b[22m!")).toEqual([
    { text: "plain x ", style: "" },
    { text: "y", style: "a-green" },
    { text: "z", style: "a-dim a-green" },
    { text: "!", style: "a-green" },
  ]);
  expect(ansiRuns("no codes")).toEqual([{ text: "no codes", style: "" }]);
});

test("an extended foreground colour this view has none for takes the text's own colour; an extended background leaves the foreground (TX-11)", () => {
  expect(ansiRuns("\u001b[31mred \u001b[38;5;208morange \u001b[38;2;1;2;3mrgb")).toEqual([
    { text: "red ", style: "a-red" },
    { text: "orange rgb", style: "" },
  ]);
  expect(ansiRuns("\u001b[31mred \u001b[48;5;208mon orange \u001b[48;2;1;2;3mon rgb")).toEqual([{ text: "red on orange on rgb", style: "a-red" }]);
});

test("a read's line numbers come apart from its text, when every line has one", () => {
  expect(lineNumbers("     1\tdef f():\n     2\t    return 1\n")).toEqual({ numbers: [1, 2], text: "def f():\n    return 1\n" });
  expect(lineNumbers("12→a\n13→b")).toEqual({ numbers: [12, 13], text: "a\nb" });
  expect(lineNumbers("1\tx\nplain")).toEqual({ numbers: null, text: "1\tx\nplain" });
  // A table whose first column is a number is not numbered lines.
  expect(lineNumbers("7\ta\n3\tb")).toEqual({ numbers: null, text: "7\ta\n3\tb" });
});

test("a person's long message folds to its first lines; the assistant's never does", () => {
  const long = "x\n".repeat(30);
  expect(recordView(record("UserMessage", { content: long }))).toMatchObject({ long: true });
  expect(recordView(record("AgentToAgentMessage", { sender: "a", content: "y".repeat(2000) }))).toMatchObject({ long: true });
  expect(recordView(record("AssistantMessage", { content: long }))).toMatchObject({ long: false });
  expect(recordView(record("UserMessage", { content: "short" }))).toMatchObject({ long: false });
});

test("a preview's lines leave the newline that ends the text out of the count", () => {
  expect(clipLines("a\nb\nc\nd\n", 3, 0)).toEqual({ head: "a\nb\nc", hidden: 1, tail: "" });
  expect(clipLines("a\nb\n", 3, 0)).toEqual({ head: "a\nb\n", hidden: 0, tail: "" });
});
