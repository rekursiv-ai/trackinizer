import { expect, test } from "vitest";
import type { SessionRecord } from "../api/sessions";
import { type ChatPart, goalOf, linesThrough, PENDING, readTranscript, RELAY_MARK, sentBefore, withPending } from "./chatLines";

function record(idx: number, kind: string, payload: { [field: string]: unknown }): SessionRecord {
  return { idx, kind, payload, text: "", context_id: null, timestamp: null, model: null, ciphertext: null } as unknown as SessionRecord;
}
const said = (idx: number, sender: string, content: string) => record(idx, "AgentToAgentMessage", { sender, content });
const answer = (idx: number, content: string) => record(idx, "AssistantMessage", { content });
const call = (idx: number, name: string) => record(idx, "ToolCall", { name });
const result = (idx: number) => record(idx, "ShellCommandResult", { stdout: "x" });
const parts = (...records: SessionRecord[]): ChatPart[] => [{ part: 0, format: "sagent", records }];

test("a person's line is a message from its poster, an answer an assistant message, and the rest are not lines", () => {
  const { lines } = readTranscript(parts(said(0, "ada@example.com", "what is X?"), call(1, "SearchRecords"), result(2), answer(3, "X is Y."),
    record(4, "TurnContext", {}), answer(5, "")));
  expect(lines).toEqual([
    { key: "0:0", at: { part: 0, idx: 0 }, role: "user", author: "ada@example.com", text: "what is X?", sentAt: null, pending: false },
    { key: "0:3", at: { part: 0, idx: 3 }, role: "assistant", author: null, text: "X is Y.", sentAt: null, pending: false, agent: "assistant" },
  ]);
});

test("a stored line carries when its record was written; a line still being sent carries none", () => {
  const at = record(0, "AgentToAgentMessage", { sender: "ada", content: "hi" });
  const stored = readTranscript(parts({ ...at, timestamp: "2026-10-13T11:50:00Z" } as SessionRecord));
  expect(stored.lines.map((line) => line.sentAt)).toEqual(["2026-10-13T11:50:00Z"]);
  const sending = withPending(stored, [{ key: "k", text: "more", conversationId: "c", baseline: 1 }], { me: "ada" });
  expect(sending.at(-1)?.sentAt).toBeNull();
});

test("an answer is pointed when its turn called the Highlight tool, whatever prefix a host gives it", () => {
  const { pointed } = readTranscript(parts(
    said(0, "ada", "q"), call(1, "Highlight"), answer(2, "a"),
    said(3, "ada", "q2"), call(4, "SearchRecords"), answer(5, "b"),
    said(6, "ada", "q3"), call(7, "mcp__canvas__Highlight"), answer(8, "c"),
  ));
  expect([...pointed]).toEqual(["0:2", "0:8"]);
});

test("lines of every part come in part order, each keyed by its part and place", () => {
  const { lines } = readTranscript([{ part: 0, format: "sagent", records: [said(0, "ada", "one"), answer(1, "a")] }, { part: 2, format: "sagent", records: [said(0, "grace", "two")] }]);
  expect(lines.map((line) => [line.key, line.author ?? "assistant", line.text])).toEqual([
    ["0:0", "ada", "one"], ["0:1", "assistant", "a"], ["2:0", "grace", "two"],
  ]);
});

test("the agent is working on the last line when it is a person's: on its last tool call since, else on nothing named", () => {
  expect(readTranscript(parts(said(0, "ada", "q"))).working).toBe("");
  expect(readTranscript(parts(said(0, "ada", "q"), call(1, "SearchRecords"), result(2), call(3, "ReadRecord"))).working).toBe("ReadRecord");
  expect(readTranscript(parts(said(0, "ada", "q"), call(1, "SearchRecords"), answer(2, "done"))).working).toBeNull();
  expect(readTranscript(parts()).working).toBeNull();
  expect(readTranscript(parts(call(0, "Orphan"))).working).toBeNull();
  // A second person's line starts the working over.
  expect(readTranscript(parts(said(0, "ada", "q"), call(1, "SearchRecords"), said(2, "grace", "and?"))).working).toBe("");
});

test("a line sent from here shows until the session holds one more line by the sender", () => {
  const stored = readTranscript(parts(said(0, "ada", "hi"), answer(1, "hello")));
  const me = { me: "ada" };
  const pending = [{ key: "k1", text: "hi", conversationId: "c", baseline: 1 }];
  expect(withPending(stored, pending, me).map((line) => [line.key, line.pending])).toEqual([
    ["0:0", false], ["0:1", false], [`${PENDING}k1`, true],
  ]);
  const after = readTranscript(parts(said(0, "ada", "hi"), answer(1, "hello"), said(2, "ada", "hi")));
  expect(withPending(after, pending, me).map((line) => line.pending)).toEqual([false, false, false]);
  // Someone else's line is not the sender's.
  const other = readTranscript(parts(said(0, "ada", "hi"), said(1, "grace", "hi")));
  expect(withPending(other, pending, me).filter((line) => line.pending)).toHaveLength(1);
});

test("a line the assistant changed before recording it stops showing as sent", () => {
  const pending = [{ key: "k1", text: "is sk-secret the key?", conversationId: "c", baseline: 0 }];
  const recorded = readTranscript(parts(said(0, "ada", "is [redacted] the key?")));
  const shown = withPending(recorded, pending, { me: "ada" });
  expect(shown.map((line) => [line.text, line.pending])).toEqual([["is [redacted] the key?", false]]);
});

test("two sends are two lines until two records arrive, whatever their words", () => {
  const pending = [
    { key: "k1", text: "hi", conversationId: "c", baseline: 0 },
    { key: "k2", text: "hi again", conversationId: "c", baseline: 1 },
  ];
  const me = { me: "ada" };
  const none = readTranscript(parts());
  const one = readTranscript(parts(said(0, "ada", "hi")));
  const two = readTranscript(parts(said(0, "ada", "hi"), said(1, "ada", "changed")));
  expect(withPending(none, pending, me).filter((line) => line.pending)).toHaveLength(2);
  expect(withPending(one, pending, me).filter((line) => line.pending).map((line) => line.key)).toEqual([`${PENDING}k2`]);
  expect(withPending(two, pending, me).filter((line) => line.pending)).toHaveLength(0);
});

test("a stored line says where it is, and a line still being sent says nothing", () => {
  const stored = readTranscript([{ part: 0, format: "sagent", records: [said(0, "ada", "one")] }, { part: 2, format: "sagent", records: [call(0, "T"), answer(1, "two")] }]);
  const shown = withPending(stored, [{ key: "k", text: "three", conversationId: "c", baseline: 0 }], { me: "grace" });
  expect(shown.map((line) => line.at)).toEqual([{ part: 0, idx: 0 }, { part: 2, idx: 1 }, null]);
});

test("a fork from a line opens with the lines up to and including it, whichever part they are in", () => {
  const stored = readTranscript([{ part: 0, format: "sagent", records: [said(0, "ada", "one"), answer(1, "a")] }, { part: 2, format: "sagent", records: [said(0, "ada", "two"), answer(1, "b")] }]);
  expect(linesThrough(stored.lines, { part: 0, idx: 1 }).map((line) => line.text)).toEqual(["one", "a"]);
  expect(linesThrough(stored.lines, { part: 2, idx: 0 }).map((line) => line.text)).toEqual(["one", "a", "two"]);
  expect(linesThrough(stored.lines, { part: 1, idx: 0 })).toEqual([]);
});

test("a send counts the lines the sender already has, sent or sending", () => {
  const stored = readTranscript(parts(said(0, "ada", "hi"), said(1, "grace", "hi"), answer(2, "hi")));
  expect(sentBefore(withPending(stored, [], { me: "ada" }), { me: "ada" })).toBe(1);
  const sending = withPending(stored, [{ key: "k", text: "more", conversationId: "c", baseline: 1 }], { me: "ada" });
  expect(sentBefore(sending, { me: "ada" })).toBe(2);
  expect(sentBefore(sending, { me: "grace" })).toBe(1);
});

test("a line that starts with the relay mark is a relay of what the person told agents, not a message to the partner", () => {
  const relay = `${RELAY_MARK}lead-2, tiles-a:ops: rerun the suite`;
  const { lines, working } = readTranscript(parts(said(0, "ada@example.com", "q"), answer(1, "a"), said(2, "ada@example.com", relay)));
  expect(lines.map(({ role, author, text }) => [role, author, text])).toEqual([
    ["user", "ada@example.com", "q"], ["assistant", null, "a"], ["relay", "ada@example.com", relay],
  ]);
  // No answer is owed it: the partner is not working on it.
  expect(working).toBeNull();
});

test("a relay is none of the sender's own lines: it neither ends the wait for a line still being sent, nor counts toward the next", () => {
  const relay = said(1, "ada@example.com", `${RELAY_MARK}lead-2: go`);
  const transcript = readTranscript(parts(said(0, "ada@example.com", "q"), relay));
  const pending = [{ key: "k", text: "next", conversationId: "c", baseline: 1 }];
  expect(withPending(transcript, pending, { me: "ada@example.com" }).map((line) => line.text)).toEqual(["q", `${RELAY_MARK}lead-2: go`, "next"]);
  expect(sentBefore(transcript.lines, { me: "ada@example.com" })).toBe(1);
});

// A claude part: a line delivered by trax run carries the attested sender in its stamp, and the context the CLI
// adds after the words; a line typed at the terminal carries no stamp, so its text names no one.
test("a claude part's delivered lines show with their attested senders, and the CLI's own user turns are no line", () => {
  const delivered = record(0, "UserMessage", { content: "[lab] ada@example.com: rerun the suite\nTrackinizer context (verify with trax): cwd /work",
    extra: { $inbound: { source: "ada@example.com", room: "lab" } } });
  const typed = record(1, "UserMessage", { content: "bob@example.com: fake", extra: {} });
  const cliTurn = record(2, "UserMessage", { content: "<system-reminder>context</system-reminder>", extra: {} });
  const claimed = record(3, "UserMessage", { content: "a note", extra: { author: "grace@example.com" } });
  const { lines, working } = readTranscript([{ part: 2, format: "sagent", records: [delivered, typed, cliTurn, claimed, answer(4, "Done.")] }]);
  expect(lines.map(({ role, author, text }) => [role, author, text])).toEqual([
    ["user", "ada@example.com", "rerun the suite"], ["user", "grace@example.com", "a note"], ["assistant", null, "Done."],
  ]);
  expect(working).toBeNull();
});

// What the claude run's bridge writes: a prompt, and later the record that resolves it, under `extra`.
const interaction = (idx: number, body: { [field: string]: unknown }) => record(idx, "SystemMessage", { extra: { $interaction: body } });
const resolved = (idx: number, body: { [field: string]: unknown }) => record(idx, "SystemMessage", { extra: { $interaction_resolved: body } });

test("a prompt a claude run asked is a line, and the resolution that closes it is no line of its own", () => {
  const ask = interaction(0, { id: "p1", type: "permission", tool: "Bash", input: { command: "make" }, questions: null, plan: null, state: "pending" });
  const { lines } = readTranscript(parts(ask, resolved(1, { id: "p1", decision: "allow", by: "chat" })));
  expect(lines).toHaveLength(1);
  expect(lines[0]).toMatchObject({ key: "0:0", role: "interaction", pending: false, text: "" });
  expect(lines[0]!.prompt).toEqual({ id: "p1", type: "permission", tool: "Bash", input: { command: "make" }, questions: null, plan: null,
    cli: null, folder: null, machine: null, remembered: null, resolution: { decision: "allow", by: "chat" } });
});

test("a resolution closes the open prompts of its id before it: a prompt of that id asked later is open until its own resolution", () => {
  const { lines } = readTranscript(parts(
    interaction(0, { id: "p1", type: "permission", tool: "Bash", input: {} }),
    resolved(1, { id: "p1", decision: "deny", by: "terminal" }),
    interaction(2, { id: "p1", type: "plan", tool: "ExitPlanMode", input: null, plan: "Rename the module" }),
    resolved(3, { id: "p9", decision: "allow", by: "chat" }),
  ));
  expect(lines.map((line) => line.prompt?.resolution)).toEqual([{ decision: "deny", by: "terminal" }, null]);
  expect(lines[1]!.prompt).toMatchObject({ type: "plan", plan: "Rename the module" });

  // Its own resolution closes the second; the first keeps the decision that closed it.
  const closed = readTranscript(parts(
    interaction(0, { id: "p1", type: "permission", tool: "Bash", input: {} }),
    resolved(1, { id: "p1", decision: "deny", by: "terminal" }),
    interaction(2, { id: "p1", type: "plan", tool: "ExitPlanMode", input: null, plan: "Rename the module" }),
    resolved(3, { id: "p1", decision: "allow", by: "chat" }),
  ));
  expect(closed.lines.map((line) => line.prompt?.resolution)).toEqual([{ decision: "deny", by: "terminal" }, { decision: "allow", by: "chat" }]);
});

test("a malformed prompt is no line, a malformed resolution closes nothing, and a question without questions is none", () => {
  const { lines } = readTranscript(parts(
    interaction(0, { id: "p1", type: "dance" }),
    interaction(1, { id: "p2", type: "question", tool: "AskUserQuestion", questions: [{ header: "no question text" }] }),
    interaction(2, { type: "permission", tool: "Bash" }),
    interaction(3, { id: "p3", type: "permission", tool: "Bash", input: {} }),
    resolved(4, { id: "p3", decision: "yes", by: "chat" }),
  ));
  expect(lines.map((line) => [line.prompt?.id, line.prompt?.resolution])).toEqual([["p3", null]]);
});

test("a question keeps its options, and is answered under its header, or its question when it has none", () => {
  const { lines } = readTranscript(parts(interaction(0, { id: "q1", type: "question", tool: "AskUserQuestion", input: {}, questions: [
    { question: "Pick a color", header: "Color", multiSelect: true, options: [{ label: "red", description: "warm" }, { label: "blue" }] },
    { question: "Why?" },
  ] })));
  expect(lines[0]!.prompt!.questions).toEqual([
    { question: "Pick a color", header: "Color", multiSelect: true, options: [{ label: "red", description: "warm" }, { label: "blue", description: "" }] },
    { question: "Why?", header: "Why?", multiSelect: false, options: [] },
  ]);
});

// A record stamped by its CLI or trax run, and the goal, command and reply records they write.
const ME = "ada@example.com";
const stamped = (at: SessionRecord, timestamp: string): SessionRecord => ({ ...at, timestamp }) as SessionRecord;
const typedCommand = (idx: number, text: string, verb: string, args: string, source = ME): SessionRecord =>
  record(idx, "SystemMessage", { subtype: "command", content: text, extra: { $command: { text, verb, args, source, room: "lab", state: "typed", reason: null } } });
const refusedCommand = (idx: number, text: string, reason: string): SessionRecord =>
  record(idx, "SystemMessage", { subtype: "command", content: `refused: ${reason}`, extra: { $command: { text, verb: text.slice(1).split(" ")[0], args: "", source: ME, room: "lab", state: "refused", reason } } });
const claudeGoal = (idx: number, attachment: { [field: string]: unknown }) => record(idx, "ContextState", { kind: "goal_status", content: null, extra: { attachment } });
const codexGoal = (idx: number, goal: { [field: string]: unknown }) =>
  record(idx, "UncategorizedRecord", { kind: "event_msg/thread_goal_updated", payload: { type: "thread_goal_updated", threadId: "t1", goal } });

test("records of every part come in the order they happened, a stamp with a zone or a fraction and no zone all reading as UTC", () => {
  const main = [stamped(said(0, ME, "first"), "2026-10-20T10:00:00Z"), answer(1, "no stamp, so the moment before it"), stamped(said(2, ME, "last"), "2026-10-20T10:02:00.500000Z")];
  const run = [stamped(typedCommand(0, "/goal ship it", "goal", "ship it"), "2026-10-20T10:01:00"), stamped(answer(1, "late"), "2026-10-20T10:03:00+00:00")];
  const { lines } = readTranscript([{ part: 0, format: "sagent", records: main }, { part: 1, format: "sagent", records: run }]);
  expect(lines.map((line) => [line.role, line.text])).toEqual([
    ["user", "first"], ["assistant", "no stamp, so the moment before it"], ["command", "/goal ship it"], ["user", "last"], ["assistant", "late"],
  ]);
});

test("a command a run typed is its sender's line, and a refused one says why", () => {
  const { lines, working } = readTranscript(parts(typedCommand(0, "/goal ship it", "goal", "ship it"), refusedCommand(1, "/model opus", "Chat cannot run /model")));
  expect(lines.map(({ role, author, text }) => [role, author, text])).toEqual([
    ["command", ME, "/goal ship it"], ["command", ME, "refused: Chat cannot run /model"],
  ]);
  // A goal set from Chat is work owed, as a person's line is.
  expect(working).toBe("");
});

test("a command that sets no goal is no work owed, and a goal control is not a condition", () => {
  expect(readTranscript(parts(typedCommand(0, "/compact", "compact", ""))).working).toBeNull();
  expect(readTranscript(parts(typedCommand(0, "/goal clear", "goal", "clear"))).working).toBeNull();
  expect(readTranscript(parts(typedCommand(0, "/goal pause", "goal", "pause"))).working).toBeNull();
});

test("what claude prints for a command Chat typed shows as its reply, and a print after no command is no line", () => {
  const printed = (idx: number, content: string) => record(idx, "SystemMessage", { subtype: "local_command", content });
  const { lines } = readTranscript(parts(typedCommand(0, "/goal ship it", "goal", "ship it"), printed(1, "<local-command-stdout>Goal set: ship it</local-command-stdout>"),
    printed(2, "<local-command-stdout>Goal set: twice</local-command-stdout>"), printed(3, "<local-command-stdout></local-command-stdout>")));
  expect(lines.map(({ role, text }) => [role, text])).toEqual([["command", "/goal ship it"], ["reply", "Goal set: ship it"]]);
});

test("a claude goal reads as set, checked and met: its goal lines, the last check while it stands, and no goal once met", () => {
  const set = claudeGoal(0, { type: "goal_status", met: false, sentinel: true, condition: "ship it" });
  const check = (idx: number, reason: string) => claudeGoal(idx, { type: "goal_status", met: false, condition: "ship it", reason });
  const midway = readTranscript(parts(set, check(1, "tests not run yet"), check(2, "the PR is open")));
  expect(midway.lines.map((line) => [line.role, line.text])).toEqual([["goal", "Goal set: ship it"]]);
  expect(goalOf(midway)).toEqual({ condition: "ship it", state: "active", lastReason: "the PR is open" });
  const met = readTranscript(parts(set, check(1, "tests not run yet"), check(2, "the PR is open"),
    claudeGoal(3, { type: "goal_status", met: true, condition: "ship it", reason: "PR merged", iterations: 3, durationMs: 83_000 })));
  expect(met.lines.map((line) => [line.role, line.text])).toEqual([["goal", "Goal set: ship it"], ["goal", "Goal met: PR merged · 3 turns · 1 min 23 s"]]);
  expect(goalOf(met)).toBeNull();
});

test("a claude goal cleared from the prompt reads as cleared, and a stopped one as stopped", () => {
  const cleared = readTranscript(parts(claudeGoal(0, { type: "goal_status", met: false, sentinel: true, condition: "ship it" }),
    claudeGoal(1, { type: "goal_status", met: true, sentinel: true, condition: "ship it" })));
  expect(cleared.lines.map((line) => line.text)).toEqual(["Goal set: ship it", "Goal cleared: ship it"]);
  expect(goalOf(cleared)).toBeNull();
  const stopped = readTranscript(parts(claudeGoal(0, { type: "goal_status", met: false, sentinel: true, condition: "ship it" }),
    claudeGoal(1, { type: "goal_status", met: false, failed: true, condition: "ship it", reason: "impossible here" })));
  expect(stopped.lines.map((line) => line.text)).toEqual(["Goal set: ship it", "Goal stopped: impossible here"]);
  expect(goalOf(stopped)).toBeNull();
});

test("a codex goal reads as set, paused and met, one line for each change of status", () => {
  const active = codexGoal(0, { objective: "ship it", status: "active" });
  const paused = readTranscript(parts(active, codexGoal(1, { objective: "ship it", status: "paused" }), codexGoal(2, { objective: "ship it", status: "paused" })));
  expect(paused.lines.map((line) => line.text)).toEqual(["Goal set: ship it", "Goal paused"]);
  expect(goalOf(paused)).toEqual({ condition: "ship it", state: "paused", lastReason: null });
  const met = readTranscript(parts(active, codexGoal(1, { objective: "ship it", status: "paused" }), codexGoal(2, { objective: "ship it", status: "active" }),
    codexGoal(3, { objective: "ship it", status: "complete" })));
  expect(met.lines.map((line) => line.text)).toEqual(["Goal set: ship it", "Goal paused", "Goal met"]);
  expect(goalOf(met)).toBeNull();
});

test("a codex goal blocked or stopped by a limit says so, and a new objective is a new goal", () => {
  const blocked = readTranscript(parts(codexGoal(0, { objective: "a", status: "active" }), codexGoal(1, { objective: "a", status: "blocked" })));
  expect(blocked.lines.map((line) => line.text)).toEqual(["Goal set: a", "Goal blocked"]);
  expect(goalOf(blocked)).toEqual({ condition: "a", state: "blocked", lastReason: null });
  const limited = readTranscript(parts(codexGoal(0, { objective: "a", status: "active" }), codexGoal(1, { objective: "a", status: "usage_limited" })));
  expect(limited.lines.map((line) => line.text)).toEqual(["Goal set: a", "Goal stopped: usage limit"]);
  expect(goalOf(limited)).toBeNull();
  const replaced = readTranscript(parts(codexGoal(0, { objective: "a", status: "active" }), codexGoal(1, { objective: "b", status: "active" })));
  expect(replaced.lines.map((line) => line.text)).toEqual(["Goal set: a", "Goal set: b"]);
});

test("a /goal clear typed in Chat closes the goal, since codex logs no clear of its own", () => {
  const { lines, goal } = readTranscript(parts(codexGoal(0, { objective: "ship it", status: "active" }), typedCommand(1, "/goal clear", "goal", "clear")));
  expect(goal).toBeNull();
  expect(lines.map((line) => [line.role, line.text])).toEqual([["goal", "Goal set: ship it"], ["command", "/goal clear"]]);
});

test("a command a run recorded settles the line sent for it, and counts as the sender's", () => {
  const stored = readTranscript(parts(typedCommand(0, "/goal ship it", "goal", "ship it")));
  const pending = [{ key: "k", text: "/goal ship it", conversationId: "c", baseline: 0 }];
  expect(withPending(stored, pending, { me: ME }).filter((line) => line.pending)).toHaveLength(0);
  expect(sentBefore(stored.lines, { me: ME })).toBe(1);
});

test("a trust prompt a CLI waits on is a line with its folder, machine and remembered flag, and its resolution closes it", () => {
  const trust = (idx: number, body: { [field: string]: unknown }) => interaction(idx, { id: "t1", type: "trust", tool: "claude", input: { folder: "~/proj" },
    questions: null, plan: null, state: "pending", cli: "claude", folder: "~/proj", machine: "gx10", remembered: false, ...body });
  const { lines } = readTranscript(parts(trust(0, {}), resolved(1, { id: "t1", decision: "allow", by: "chat" })));
  expect(lines).toHaveLength(1);
  expect(lines[0]!.prompt).toEqual({ id: "t1", type: "trust", tool: "claude", input: { folder: "~/proj" }, questions: null, plan: null,
    cli: "claude", folder: "~/proj", machine: "gx10", remembered: false, resolution: { decision: "allow", by: "chat" } });
  // A trust prompt names its CLI and folder, or it is no line; a home folder's machine is none.
  const malformed = readTranscript(parts(trust(0, { cli: "gemini" }), trust(1, { id: "t2", folder: "" })));
  expect(malformed.lines).toHaveLength(0);
  const home = readTranscript(parts(trust(0, { id: "t3", machine: null, remembered: true })));
  expect(home.lines[0]!.prompt).toMatchObject({ machine: null, remembered: true, resolution: null });
});

test("an answer names the agent whose part wrote it, so each reply of a chat that switched keeps its own", () => {
  const { lines } = readTranscript([
    { part: 0, format: "sagent", records: [said(0, ME, "q"), answer(1, "from the assistant")] },
    { part: 1, format: "claude", records: [stamped(answer(0, "from claude"), "2030-01-01T00:00:00Z")] },
    { part: 2, format: "codex", records: [stamped(answer(0, "from codex"), "2030-01-02T00:00:00Z")] },
    { part: 3, format: "", records: [stamped(answer(0, "from a part of no known format"), "2030-01-03T00:00:00Z")] },
  ]);
  expect(lines.map(({ role, agent, text }) => [role, agent, text])).toEqual([
    ["user", undefined, "q"], ["assistant", "assistant", "from the assistant"], ["assistant", "claude", "from claude"],
    ["assistant", "codex", "from codex"], ["assistant", undefined, "from a part of no known format"],
  ]);
});

// What AgentSession#82 on alpha holds for a /compact typed in Chat: trax run's `$command` on a part of its own, and on
// claude's part the bare command, the boundary, the compaction and clear, the summary and the CLI's own print.
test("a /compact typed in Chat is its command line, then one compaction line, and claude's own records of it are no lines", () => {
  const run = [stamped(typedCommand(0, "/compact", "compact", ""), "2026-10-20T22:46:54.590037Z")];
  const claude = [
    stamped(record(70, "UserMessage", { content: "/compact", extra: { $bare: true } }), "2026-10-20T22:46:54.597Z"),
    stamped(record(77, "SystemMessage", { subtype: "compact_boundary", content: "Conversation compacted", extra: { compactMetadata: { trigger: "manual" } } }), "2026-10-20T22:47:08.865Z"),
    stamped(record(78, "ContextCompaction", { summary: null, extra: { $in_place: true } }), "2026-10-20T22:47:08.711Z"),
    stamped(record(79, "ContextClear", { summary: "This session is being continued", extra: { $in_place: true } }), "2026-10-20T22:47:08.711Z"),
    stamped(record(80, "UserMessage", { content: "This session is being continued", extra: { $bare: true } }), "2026-10-20T22:47:08.711Z"),
    stamped(record(84, "UserMessage", { content: "<local-command-stdout>\u001b[2mCompacted (ctrl+o to see full summary)\u001b[22m</local-command-stdout>", extra: { $bare: true } }),
      "2026-10-20T22:47:08.965Z"),
    stamped(answer(153, "Yes, /compact ran."), "2026-10-20T22:51:08.051Z"),
  ];
  const { lines, working } = readTranscript([{ part: 0, format: "claude", records: claude }, { part: 1, format: "", records: run }]);
  expect(lines.map(({ key, role, text }) => [key, role, text])).toEqual([
    ["1:0", "command", "/compact"], ["0:78", "compaction", "Context compacted"], ["0:153", "assistant", "Yes, /compact ran."],
  ]);
  expect(working).toBeNull();
});

test("one compaction is one line, however many records name it, and a compaction on its own shows too", () => {
  // Codex writes its `context_compacted` marker beside the `compacted` line, which both read as a compaction.
  const twice = readTranscript(parts(answer(0, "a"), record(1, "ContextCompaction", {}), record(2, "ContextClear", {}), record(3, "ContextCompaction", {}), answer(4, "b")));
  expect(twice.lines.map(({ role, text }) => [role, text])).toEqual([["assistant", "a"], ["compaction", "Context compacted"], ["assistant", "b"]]);
  const again = readTranscript(parts(record(0, "ContextCompaction", {}), answer(1, "a"), record(2, "ContextCompaction", {})));
  expect(again.lines.map(({ role }) => role)).toEqual(["compaction", "assistant", "compaction"]);
});
