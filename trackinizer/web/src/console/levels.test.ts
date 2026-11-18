import { expect, test } from "vitest";
import type { FeedEvent } from "../api/sessions";
import { levelCounts, levelKinds, levelOf } from "./levels";

/** A feed record of `kind` with `message` as its payload. */
function record(kind: string, message: { [field: string]: unknown } = {}): FeedEvent {
  return {
    session_id: "00000000-0000-4000-8000-000000000001",
    actor: "codex",
    rooms: [],
    part: 0,
    seq: 0,
    kind,
    created: "2026-10-01T10:00:00Z",
    message: { "py/object": `trackinizer.lib.agent.types.sessions.${kind}`, ...message },
    text: "",
  };
}

test("Messages is the conversation: what a person, the agent or another agent said, and a person's queued message", () => {
  expect(levelOf(record("UserMessage", { content: "Fix the flake." }))).toBe(1);
  expect(levelOf(record("AssistantMessage", { content: "Done." }))).toBe(1);
  expect(levelOf(record("AgentToAgentMessage", { content: "FINAL_ANSWER", sender: "/root/reviewer" }))).toBe(1);
  const queued = (origin: string) =>
    record("ContextState", { kind: "queued_command", content: null, extra: { attachment: { prompt: "stop", origin: { kind: origin } } } });
  expect(levelOf(queued("human"))).toBe(1);
  // What the harness typed on the user's turn, a task's notice, and injected state are not.
  expect(levelOf(record("UserMessage", { content: "<command-name>", extra: { isMeta: true } }))).toBe(4);
  expect(levelOf(queued("task-notification"))).toBe(4);
  expect(levelOf(record("ContextState", { kind: "world_state", content: "" }))).toBe(4);
});

test("a record with nothing to read shows at no level, All included: a sealed codex peer message, and sealed reasoning with no summary", () => {
  const peer = (type: string, payload: string) =>
    record("AgentToAgentMessage", { content: `Message Type: ${type}\nTask name: /root\nSender: /root/prototype\nPayload:\n${payload}`, sender: "/root/prototype" });
  expect(levelOf(peer("FINAL_ANSWER", "Implemented and frozen."))).toBe(1);
  expect(levelOf(peer("MESSAGE", ""))).toBeGreaterThan(4);
  expect(levelOf(record("Thinking", { content: null, encrypted: "", summary: null }))).toBeGreaterThan(4);
  expect(levelOf(record("Thinking", { content: null, encrypted: "", summary: "Planned." }))).toBe(3);
});

test("what a harness writes on the user's turn is not Messages: codex's context, and a background task's notice", () => {
  const context = '<codex_internal_context source="goal">\nKeep going.\n</codex_internal_context>';
  expect(levelOf(record("UserMessage", { content: context }))).toBe(4);
  expect(levelOf(record("UserMessage", { content: `Look at this.\n${context}` }))).toBe(1);
  const notice = "<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>\n<summary>Done</summary>\n</task-notification>";
  expect(levelOf(record("UserMessage", { content: notice }))).toBe(4);
});

test("+ Calls adds tool calls, + Output their results and thinking, and All the bookkeeping", () => {
  expect(levelOf(record("ToolCall", { call_id: "c1", name: "Bash", arguments: { command: "ls" } }))).toBe(2);
  for (const kind of ["ShellCommandResult", "FileEditResult", "UncategorizedToolResult", "Thinking", "ContextClear", "Stdout"]) {
    expect([kind, levelOf(record(kind, { content: "x" }))]).toEqual([kind, 3]);
  }
  for (const kind of ["TokenUsage", "TurnContext", "SystemMessage", "UncategorizedRecord"]) {
    expect([kind, levelOf(record(kind))]).toEqual([kind, 4]);
  }
  // A message with nothing in it is bookkeeping, as the transcript folds it.
  expect(levelOf(record("AssistantMessage", { content: "  " }))).toBe(4);
  const image = { "py/object": "trackinizer.lib.agent.types.sessions.Attachment", mime_descriptor: "image/png", data: "AA==" };
  expect(levelOf(record("UserMessage", { content: "", attachments: [image] }))).toBe(1);
});

test("the server reads only a low level's kinds; + Output and All read every kind", () => {
  const messages = ["AgentToAgentMessage", "AssistantMessage", "ContextState", "UserMessage"];
  expect(levelKinds(1)).toEqual(messages);
  expect(levelKinds(2)).toEqual([...messages, "ToolCall"]);
  expect([levelKinds(3), levelKinds(4)]).toEqual([[], []]);
});

test("each level counts what it shows: the agents' conversation, then each kind it adds", () => {
  const kinds = {
    UserMessage: 30,
    AssistantMessage: 50,
    AgentToAgentMessage: 10,
    ContextState: 20,
    ToolCall: 200,
    ShellCommandResult: 150,
    Thinking: 100,
    TokenUsage: 300,
    TurnContext: 90,
    SystemMessage: 5,
    UncategorizedRecord: 45,
  };
  // 82 of the 110 records of conversation kinds are conversation; the other 28 count with the bookkeeping.
  expect(levelCounts({ conversation: 82, count: 1000, kinds })).toEqual([82, 282, 532, 1000]);
});

test("a message's attachments count whether plain or a tagged tuple", () => {
  const plain = { "py/object": "trackinizer.lib.agent.types.sessions.Attachment", mime_descriptor: "image/png", data: "AA==" };
  const tagged = { ...plain, data: { "py/b64": "AA==" } };
  expect(levelOf(record("UserMessage", { content: "", attachments: [plain] }))).toBe(1);
  expect(levelOf(record("UserMessage", { content: "", attachments: { "py/tuple": [tagged] } }))).toBe(1);
  expect(levelOf(record("UserMessage", { content: "", attachments: [] }))).toBe(4);
  expect(levelOf(record("UserMessage", { content: "", attachments: { "py/tuple": [] } }))).toBe(4);
});
