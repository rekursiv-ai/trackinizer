import { QueryClient } from "@tanstack/react-query";
import { expect, test } from "vitest";
import type { ChatMessage, ChatThread } from "../api/chats";
import { appendLines, type ChatLines, chatKey, KEPT, lastSeq, mergeMessages, readLines } from "./chatCache";

const message = (seq: number): ChatMessage =>
  ({ id: `m${seq}`, seq, role: "user", author: "a", text: `t${seq}`, created: "2026-10-03T10:00:00Z" });
const thread = (messages: ChatMessage[], earlier = false): ChatThread =>
  ({ id: "c", title: "t", partner_actor: null, partner_session_id: null, earlier, messages });

test("merging keeps each message once by id, in seq order, and the newest KEPT", () => {
  expect(mergeMessages([message(2), message(1)], [message(2), message(3)]).map((m) => m.seq)).toEqual([1, 2, 3]);
  expect(mergeMessages([], Array.from({ length: KEPT + 3 }, (_, n) => message(n + 1)))[0]!.seq).toBe(4);
});

test("lines pushed before the read are held unread, and the read merges with them", () => {
  const client = new QueryClient();
  appendLines(client, "c", [message(3)]);
  const held = client.getQueryData<ChatLines>(chatKey("c"));
  expect(held).toMatchObject({ read: false, earlier: false });
  const read = readLines(thread([message(1), message(2)], true), held);
  expect(read).toMatchObject({ read: true, earlier: true });
  expect(read.messages.map((m) => m.seq)).toEqual([1, 2, 3]);
  client.setQueryData(chatKey("c"), read);
  appendLines(client, "c", [message(4), message(4)]);
  expect(client.getQueryData<ChatLines>(chatKey("c"))).toMatchObject({ read: true, earlier: true });
  expect(lastSeq(client.getQueryData<ChatLines>(chatKey("c")))).toBe(4);
  expect(lastSeq(undefined)).toBe(0);
});

test("appending nothing creates no entry", () => {
  const client = new QueryClient();
  appendLines(client, "c", []);
  expect(client.getQueryData(chatKey("c"))).toBeUndefined();
});
