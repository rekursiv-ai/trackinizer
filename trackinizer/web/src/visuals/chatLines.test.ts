import { expect, test } from "vitest";
import type { ChatMessage } from "../api/chats";
import { transcript } from "./chatLines";

const message = (seq: number): ChatMessage =>
  ({ id: `m${seq}`, seq, role: seq % 2 ? "user" : "assistant", author: "a", text: `t${seq}`, created: "2026-10-03T10:00:00Z" });

test("stored messages show in order with their seq, then what is pending, which has none", () => {
  const lines = transcript([message(1), message(2)], [{ key: "k", text: "later", conversationId: null }]);
  expect(lines.map((line) => [line.key, line.seq])).toEqual([["m1", 1], ["m2", 2], ["pending:k", null]]);
  expect(lines[2]).toMatchObject({ role: "user", text: "later" });
});
