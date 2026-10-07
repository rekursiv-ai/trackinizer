import { afterEach, expect, test, vi } from "vitest";
import { getChat, listChats } from "./chats";
import { stubFetch } from "./testing";

afterEach(() => vi.unstubAllGlobals());

const summary = { id: "c1", title: "Hello", partner_actor: "scout", workspace_id: "w", created: "2026-10-03T10:00:00Z", modified: "2026-10-03T10:00:00Z" };
const message = { id: "m1", seq: 3, role: "assistant", author: "scout", text: "hi", created: "2026-10-03T10:00:00Z" };

test("lists the conversations, and refuses an answer that is not a list of them", async () => {
  const sent = stubFetch(() => Response.json([summary]));
  await expect(listChats()).resolves.toEqual([summary]);
  expect(sent).toMatchObject([{ method: "GET", path: "/api/chats" }]);
  stubFetch(() => Response.json({}));
  await expect(listChats()).rejects.toThrow("Invalid chat list");
});

test("reads a conversation after a seq", async () => {
  const thread = { id: "c1", title: "Hello", partner_actor: "scout", partner_session_id: "s", messages: [message] };
  const sent = stubFetch(() => Response.json(thread));
  await expect(getChat("c1", 2)).resolves.toEqual(thread);
  expect(sent).toMatchObject([{ method: "GET", path: "/api/chats/c1", query: "?after_seq=2" }]);
  stubFetch(() => Response.json({ id: "c1", messages: [{}] }));
  await expect(getChat("c1", 0)).rejects.toThrow("Invalid chat thread");
});
