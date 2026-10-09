import { expect, test } from "vitest";
import type { SessionRecord } from "../api/sessions";
import { type ChatPart, linesThrough, PENDING, readTranscript, sentBefore, withPending } from "./chatLines";

function record(idx: number, kind: string, payload: { [field: string]: unknown }): SessionRecord {
  return { idx, kind, payload, text: "", context_id: null, timestamp: null, model: null, ciphertext: null } as unknown as SessionRecord;
}
const said = (idx: number, sender: string, content: string) => record(idx, "AgentToAgentMessage", { sender, content });
const answer = (idx: number, content: string) => record(idx, "AssistantMessage", { content });
const call = (idx: number, name: string) => record(idx, "ToolCall", { name });
const result = (idx: number) => record(idx, "ShellCommandResult", { stdout: "x" });
const parts = (...records: SessionRecord[]): ChatPart[] => [{ part: 0, records }];

test("a person's line is a message from its poster, an answer an assistant message, and the rest are not lines", () => {
  const { lines } = readTranscript(parts(said(0, "ada@example.com", "what is X?"), call(1, "SearchRecords"), result(2), answer(3, "X is Y."),
    record(4, "TurnContext", {}), answer(5, "")));
  expect(lines).toEqual([
    { key: "0:0", at: { part: 0, idx: 0 }, role: "user", author: "ada@example.com", text: "what is X?", pending: false },
    { key: "0:3", at: { part: 0, idx: 3 }, role: "assistant", author: null, text: "X is Y.", pending: false },
  ]);
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
  const { lines } = readTranscript([{ part: 0, records: [said(0, "ada", "one"), answer(1, "a")] }, { part: 2, records: [said(0, "grace", "two")] }]);
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
  const stored = readTranscript([{ part: 0, records: [said(0, "ada", "one")] }, { part: 2, records: [call(0, "T"), answer(1, "two")] }]);
  const shown = withPending(stored, [{ key: "k", text: "three", conversationId: "c", baseline: 0 }], { me: "grace" });
  expect(shown.map((line) => line.at)).toEqual([{ part: 0, idx: 0 }, { part: 2, idx: 1 }, null]);
});

test("a fork from a line opens with the lines up to and including it, whichever part they are in", () => {
  const stored = readTranscript([{ part: 0, records: [said(0, "ada", "one"), answer(1, "a")] }, { part: 2, records: [said(0, "ada", "two"), answer(1, "b")] }]);
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
