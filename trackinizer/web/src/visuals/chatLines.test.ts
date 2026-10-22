import { expect, test } from "vitest";
import type { SessionRecord } from "../api/sessions";
import { type ChatPart, goalOf, linesThrough, openPrompts, PENDING, readTranscript, RELAY_MARK, sentBefore, withPending } from "./chatLines";

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

// What each CLI logs when its turn is interrupted: claude a user turn of its own, codex a developer message.
test("an interrupt the CLI logs ends the turn: it is one note, and no answer is owed", () => {
  const stops = [
    record(2, "UserMessage", { content: "[Request interrupted by user]", extra: {} }),
    record(2, "UserMessage", { content: "[Request interrupted by user for tool use]", extra: {} }),
    record(2, "SystemMessage", { subtype: "developer", content: "<turn_aborted>\nThe previous turn was interrupted on purpose." }),
  ];
  for (const stop of stops) {
    const { lines, working } = readTranscript(parts(said(0, "ada", "run it"), call(1, "Bash"), stop));
    expect(lines.map(({ role, text }) => [role, text])).toEqual([["user", "run it"], ["interrupt", "Interrupted"]]);
    expect(working).toBeNull();
  }
  // A person's delivered line in the same words is still their line, and still owed an answer.
  const typed = record(1, "UserMessage", { content: "ada: [Request interrupted by user]", extra: { $inbound: { source: "ada" } } });
  const { lines, working } = readTranscript(parts(said(0, "ada", "run it"), typed));
  expect(lines.map(({ role, text }) => [role, text])).toEqual([["user", "run it"], ["user", "[Request interrupted by user]"]]);
  expect(working).toBe("");
  // Codex's other developer messages are no line.
  expect(readTranscript(parts(record(0, "SystemMessage", { subtype: "developer", content: "<permissions instructions>" }))).lines).toEqual([]);
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

// A line sent while claude is mid-turn is queued: claude logs it as a `queued_command` ContextState, not a UserMessage.
// trax run stamps the one it typed; claude's own queued notices (task notifications) carry no stamp.
test("a stamped queued_command is the person's line and clears a pending post; an unstamped one is no line", () => {
  const me = { me: "ada@example.com" };
  const first = record(0, "UserMessage", { content: "go", extra: { author: "ada@example.com" } });
  const queued = record(1, "ContextState", { kind: "queued_command", content: "[lab] ada@example.com: also check CI\nTrackinizer context (verify with trax): cwd /work",
    extra: { $inbound: { source: "ada@example.com", room: "lab" } } });
  const notice = record(2, "ContextState", { kind: "queued_command", content: "<task-notification>done</task-notification>", extra: {} });
  const sent = [{ key: "k", text: "also check CI", conversationId: "c", baseline: 1 }];
  const before = readTranscript([{ part: 0, format: "claude", records: [first, notice] }]);
  const after = readTranscript([{ part: 0, format: "claude", records: [first, queued, notice] }]);
  expect(after.lines.map(({ role, author, text }) => [role, author, text])).toEqual([
    ["user", "ada@example.com", "go"], ["user", "ada@example.com", "also check CI"],
  ]);
  expect(sentBefore(before.lines, me)).toBe(1);
  expect(sentBefore(after.lines, me)).toBe(2);
  expect(withPending(before, sent, me).filter((line) => line.pending)).toHaveLength(1);
  expect(withPending(after, sent, me).filter((line) => line.pending)).toHaveLength(0);
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

test("a refused delivery settles its pending line and reports why without starting work", () => {
  const stored = readTranscript(parts(record(0, "SystemMessage", { extra: { $delivery: {
    text: "continue", source: "ada", kind: "line", refused: true, reason: "run ended",
  } } })));
  const shown = withPending(stored, [{ key: "k", text: "continue", conversationId: "c", baseline: 0 }], { me: "ada" });
  expect(shown).toHaveLength(1);
  expect(shown[0]).toMatchObject({ text: "continue", author: "ada", pending: false, delivery: { refused: true, reason: "run ended" } });
  expect(stored.working).toBeNull();
});

test("an unconfirmed delivery is distinct from a refusal", () => {
  const stored = readTranscript(parts(record(0, "SystemMessage", { extra: { $delivery: {
    text: "continue", source: "ada", kind: "line", refused: false, reason: "connection lost",
  } } })));
  expect(stored.lines[0]).toMatchObject({ delivery: { refused: false, reason: "connection lost" } });
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

// Tool calls as lines: a claude part writes the call and its result with the CLI's own
// ids, a sagent part with sagent's.
const toolCall = (idx: number, id: string, name: string, args: { [field: string]: unknown } = {}) => record(idx, "ToolCall", { call_id: id, name, arguments: args });
const toolResult = (idx: number, id: string, stdout: string) => record(idx, "ShellCommandResult", { call_id: id, command: ["ls"], stdout, stderr: "", exit_code: 0 });
const withTools = { tools: true };

test("with tool calls on, each call is a line of its own in time order, its result the one answering its call id", () => {
  const records = [said(0, ME, "list home"), toolCall(1, "a", "Bash", { command: "ls" }), toolCall(2, "b", "Read", { file_path: "/x" }),
    toolResult(3, "b", "text"), toolResult(4, "a", "ada\n"), answer(5, "done")];
  const { lines } = readTranscript(parts(...records), withTools);
  expect(lines.map((line) => [line.key, line.role])).toEqual([["0:0", "user"], ["0:1", "tool"], ["0:2", "tool"], ["0:5", "assistant"]]);
  expect(lines[1]!.tool).toEqual({ call: records[1], result: records[4] });
  expect(lines[2]!.tool).toEqual({ call: records[2], result: records[3] });
  expect(lines[1]).toMatchObject({ at: { part: 0, idx: 1 }, author: null, text: "Bash", pending: false });
});

test("a call still running has no result, and the result that arrives is attached to it", () => {
  const running = readTranscript(parts(said(0, ME, "q"), toolCall(1, "a", "Bash")), withTools);
  expect(running.lines[1]!.tool).toEqual({ call: expect.anything(), result: null });
  const answered = readTranscript(parts(said(0, ME, "q"), toolCall(1, "a", "Bash"), toolResult(2, "a", "x")), withTools);
  expect(answered.lines).toHaveLength(2);
  expect(answered.lines[1]!.tool!.result).not.toBeNull();
});

test("a result of no call here, or of a call whose id is empty, is no line and is attached to nothing", () => {
  const { lines } = readTranscript(parts(toolResult(0, "ghost", "x"), toolCall(1, "", "Bash"), toolResult(2, "", "y")), withTools);
  expect(lines.map((line) => [line.role, line.tool?.result ?? null])).toEqual([["tool", null]]);
});

test("a call and its result in different parts, or out of stamp order, still meet; cards sit between the lines by time", () => {
  const main = [stamped(said(0, ME, "q"), "2026-10-20T10:00:00Z"), stamped(answer(1, "a"), "2026-10-20T10:03:00Z")];
  const run = [stamped(toolCall(0, "a", "Bash"), "2026-10-20T10:01:00Z"), stamped(toolResult(1, "a", "x"), "2026-10-20T10:02:00Z")];
  const { lines } = readTranscript([{ part: 0, format: "claude", records: main }, { part: 1, format: "claude", records: run }], withTools);
  expect(lines.map((line) => [line.key, line.role, line.tool?.result?.idx ?? null])).toEqual([["0:0", "user", null], ["1:0", "tool", 1], ["0:1", "assistant", null]]);
});

test("with tool calls off, the lines are exactly what they were, and the other fields do not change with the setting", () => {
  const records = [said(0, ME, "list home"), toolCall(1, "a", "Highlight"), toolResult(2, "a", "x"), toolCall(3, "b", "Bash"), answer(4, "done")];
  const off = readTranscript(parts(...records));
  expect(readTranscript(parts(...records), { tools: false })).toEqual(off);
  expect(off.lines.map((line) => line.role)).toEqual(["user", "assistant"]);
  const on = readTranscript(parts(...records), withTools);
  expect([on.working, [...on.pointed], on.goal]).toEqual([off.working, [...off.pointed], off.goal]);
  expect(on.lines.filter((line) => line.role !== "tool")).toEqual(off.lines);
});

test("a tool line is no message of a person: it does not count toward what a sender has sent", () => {
  const stored = readTranscript(parts(said(0, ME, "q"), toolCall(1, "a", "Bash")), withTools);
  expect(sentBefore(stored.lines, { me: ME })).toBe(1);
  expect(withPending(stored, [], { me: ME }).map((line) => line.role)).toEqual(["user", "tool"]);
});

// The unified ask path: every agent writes the same card, with its provider, kind and the agent's own options.
const OPTIONS = [{ id: "allow_once", label: "Allow", kind: "allow_once" }, { id: "allow_session", label: "Allow for the session", kind: "allow_session" },
  { id: "deny", label: "Deny", kind: "deny" }];
const asked = (idx: number, body: { [field: string]: unknown } = {}) => interaction(idx, { id: "a1", type: "permission", tool: "Bash", input: { command: "ls" },
  questions: null, plan: null, state: "pending", provider: "codex", kind: "command", title: "Run a command", tool_call_id: "", command: "ls", cwd: "/work",
  reason: "to list", diff: "", options: OPTIONS, answerable_in_chat: true, ...body });

test("an ask with options keeps its provider, kind, words and the agent's own options for the card", () => {
  const { lines } = readTranscript(parts(asked(0)));
  expect(lines[0]!.prompt!.ask).toEqual({ provider: "codex", kind: "command", title: "Run a command", toolCallId: "", command: "ls", cwd: "/work",
    reason: "to list", diff: "", options: OPTIONS, answerable: true });
  expect(lines[0]!.prompt).toMatchObject({ id: "a1", type: "permission", tool: "Bash", resolution: null });
});

test("a record from before the options has no ask, and one whose options are all malformed is read as before", () => {
  const old = readTranscript(parts(interaction(0, { id: "p1", type: "permission", tool: "Bash", input: {} })));
  expect(old.lines[0]!.prompt!.ask).toBeUndefined();
  const broken = readTranscript(parts(asked(0, { options: [{ id: "x", label: "X", kind: "launch" }, { label: "no id", kind: "deny" }, 7] })));
  expect(broken.lines[0]!.prompt!.ask).toBeUndefined();
  // A malformed option is dropped from a card that has others.
  const some = readTranscript(parts(asked(0, { options: [...OPTIONS, { id: "x", label: "X", kind: "launch" }] })));
  expect(some.lines[0]!.prompt!.ask!.options).toEqual(OPTIONS);
});

test("an ask only the terminal can answer says so, and a missing flag reads as answerable", () => {
  expect(readTranscript(parts(asked(0, { answerable_in_chat: false }))).lines[0]!.prompt!.ask!.answerable).toBe(false);
  expect(readTranscript(parts(asked(0, { answerable_in_chat: undefined }))).lines[0]!.prompt!.ask!.answerable).toBe(true);
});

test("a trust ask with no folder is a permission card of its own options, and a trust prompt without options is still no line", () => {
  const ask = readTranscript(parts(asked(0, { type: "trust", kind: "trust", title: "Trust this folder?" })));
  expect(ask.lines[0]!.prompt).toMatchObject({ type: "permission", ask: { kind: "trust", title: "Trust this folder?" } });
  const bare = readTranscript(parts(interaction(0, { id: "t9", type: "trust", tool: "claude", input: {} })));
  expect(bare.lines).toHaveLength(0);
});

test("a resolution by timeout or release closes its card, with the option taken and who took it when they are named", () => {
  const { lines } = readTranscript(parts(asked(0), asked(1, { id: "a2" }), asked(2, { id: "a3" }),
    resolved(3, { id: "a1", option: "", by: "released", who: "", decision: "released" }),
    resolved(4, { id: "a2", option: "allow_session", by: "chat", who: ME, decision: "allow" }),
    resolved(5, { id: "a3", option: "", by: "timeout", who: "" })));
  expect(lines.map((line) => line.prompt!.resolution)).toEqual([
    { decision: "released", by: "released" },
    { decision: "allow", by: "chat", option: "allow_session", who: ME },
    { decision: "released", by: "timeout" },
  ]);
});

test("a resolution that names no decision reads one from who ended it: released for the run, answered for a person", () => {
  const { lines } = readTranscript(parts(asked(0), asked(1, { id: "a2" }),
    resolved(2, { id: "a1", option: "1", by: "chat", who: ME }), resolved(3, { id: "a2", option: "", by: "run" })));
  expect(lines.map((line) => line.prompt!.resolution?.decision)).toEqual(["answered", "released"]);
});

test("open prompts are the cards no resolution has closed", () => {
  expect(openPrompts(readTranscript(parts(asked(0), asked(1, { id: "a2" }), resolved(2, { id: "a1", by: "chat", decision: "allow" }))))).toBe(1);
  expect(openPrompts(readTranscript(parts(asked(0), resolved(1, { id: "a1", by: "released", decision: "released" }))))).toBe(0);
  expect(openPrompts(readTranscript(parts(said(0, ME, "hi"))))).toBe(0);
});

test("an ask that names its tool call shows right under that call's card, whenever it was written", () => {
  const records = [said(0, ME, "list"), toolCall(1, "toolu_1", "Bash", { command: "ls" }), toolCall(2, "toolu_2", "Read"), asked(3, { tool_call_id: "toolu_1" }),
    toolResult(4, "toolu_2", "x")];
  const { lines } = readTranscript(parts(...records), withTools);
  expect(lines.map((line) => [line.key, line.role, line.attached ?? false])).toEqual([
    ["0:0", "user", false], ["0:1", "tool", false], ["0:3", "interaction", true], ["0:2", "tool", false],
  ]);
  // The result still finds its call across the moved card.
  expect(lines[3]!.tool!.result).toBe(records[4]);
  // The call may come after the ask, as a log written by another process can.
  const late = readTranscript(parts(asked(0, { tool_call_id: "toolu_1" }), toolCall(1, "toolu_1", "Bash")), withTools);
  expect(late.lines.map((line) => [line.role, line.attached ?? false])).toEqual([["tool", false], ["interaction", true]]);
});

test("an ask shows once, under the first card, when two cards carry its call's id", () => {
  const { lines } = readTranscript(parts(toolCall(0, "toolu_1", "Bash"), toolCall(1, "toolu_1", "Bash"), asked(2, { tool_call_id: "toolu_1" })), withTools);
  expect(lines.map((line) => [line.key, line.role, line.attached ?? false])).toEqual([["0:0", "tool", false], ["0:2", "interaction", true], ["0:1", "tool", false]]);
});

test("an ask stays where it happened when tool calls are off, when it names no call, or when no card has that call", () => {
  const records = [said(0, ME, "list"), asked(1, { tool_call_id: "toolu_1" }), toolCall(2, "toolu_1", "Bash"), asked(3, { id: "a2", tool_call_id: "" }),
    asked(4, { id: "a3", tool_call_id: "toolu_9" })];
  expect(readTranscript(parts(...records)).lines.map((line) => [line.role, line.attached ?? false])).toEqual([
    ["user", false], ["interaction", false], ["interaction", false], ["interaction", false]]);
  expect(readTranscript(parts(...records), withTools).lines.map((line) => [line.role, line.attached ?? false])).toEqual([
    ["user", false], ["tool", false], ["interaction", true], ["interaction", false], ["interaction", false]]);
});

test("a question that wants words has no options, and keeps the id its agent gave it", () => {
  const { lines } = readTranscript(parts(interaction(0, { id: "q2", type: "question", tool: "request_user_input", input: {}, questions: [
    { id: "dir", header: "", question: "Which folder?", options: null, isOther: true },
  ] })));
  expect(lines[0]!.prompt!.questions).toEqual([{ id: "dir", question: "Which folder?", header: "Which folder?", multiSelect: false, options: [] }]);
});

test("a question its agent marks secret is read as secret", () => {
  const { lines } = readTranscript(parts(interaction(0, { id: "q3", type: "question", tool: "request_user_input", input: {}, questions: [
    { id: "tok", header: "Token", question: "API token?", options: null, isSecret: true },
  ] })));
  expect(lines[0]!.prompt!.questions).toEqual([{ id: "tok", question: "API token?", header: "Token", multiSelect: false, options: [], secret: true }]);
});

// The run names a sender only when it says something: the run's own person is typed bare while nobody else speaks.
test("a delivered line shows its words with or without a room, a sender or a context trailer", () => {
  const stamp = { source: "ada@example.com", room: "lab" };
  const context = "\nTrackinizer context (verify with trax): {}";
  const delivered = (idx: number, content: string, extra: { [field: string]: unknown }) =>
    record(idx, "UserMessage", { content, extra: { $inbound: extra } });
  const { lines } = readTranscript([{ part: 2, format: "sagent", records: [
    delivered(0, "[lab] ada@example.com: one", stamp),
    delivered(1, "[lab] two", stamp),
    delivered(2, `[lab] three${context}`, stamp),
    delivered(3, "four", { source: "ada@example.com", room: "" }),
    delivered(4, `ada@example.com: five${context}`, { source: "ada@example.com", room: "" }),
  ] }]);
  expect(lines.map(({ author, text }) => [author, text])).toEqual([
    ["ada@example.com", "one"], ["ada@example.com", "two"], ["ada@example.com", "three"],
    ["ada@example.com", "four"], ["ada@example.com", "five"],
  ]);
});
