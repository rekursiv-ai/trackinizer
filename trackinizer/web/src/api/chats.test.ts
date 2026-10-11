import { afterEach, expect, test, vi } from "vitest";
import { ApiError } from "./client";
import { conversationOf, getChatHead, isChatHandle, sendChatLine } from "./chats";
import { stubFetch } from "./testing";

afterEach(() => vi.unstubAllGlobals());

const head = {
  conversation_id: "c1", session_id: "s1", title: "Hello", account: "ada@example.com", live: true,
  forks: 2, forked_from: null, forks_on_typing: false,
};

test("finds a conversation's session, and says nothing until the assistant has opened it", async () => {
  const sent = stubFetch(() => Response.json(head));
  await expect(getChatHead("c1")).resolves.toEqual(head);
  expect(sent).toMatchObject([{ method: "GET", path: "/api/chats/c1" }]);

  stubFetch(() => Response.json({ detail: "Conversation not found" }, { status: 404 }));
  await expect(getChatHead("c1")).resolves.toBeNull();

  stubFetch(() => Response.json({ detail: "boom" }, { status: 500 }));
  await expect(getChatHead("c1")).rejects.toBeInstanceOf(ApiError);

  stubFetch(() => Response.json({ conversation_id: "c1" }));
  await expect(getChatHead("c1")).rejects.toThrow("Invalid chat head");
});

test("a line goes to /api/chats under its key, with its canvas, and the answer is the conversation", async () => {
  const sent = stubFetch(() => Response.json({ conversation_id: "k1", session_id: null, workspace_id: "w1" }));
  await expect(sendChatLine(
    { workspaceId: "w1", text: "hi", conversationId: null,
      page: "#/ref/Issue/9", trail: ["#/graph", "#/lookup/x"] },
    "k1",
  )).resolves.toEqual({ conversation_id: "k1", session_id: null, workspace_id: "w1" });
  expect(sent).toMatchObject([{ method: "POST", path: "/api/chats", headers: { "idempotency-key": "k1" },
    body: { kind: "science", workspace_id: "w1", text: "hi", conversation_id: null,
      page: "#/ref/Issue/9", trail: ["#/graph", "#/lookup/x"] } }]);
});

test("a line that forks names the line it starts from, and a line that does not names none", async () => {
  const sent = stubFetch(() => Response.json({ conversation_id: "k3", session_id: null, workspace_id: "w1" }));
  await sendChatLine({ workspaceId: "w", text: "hi", conversationId: null, page: null, trail: [],
    fork: { sessionId: "s1", part: 1, idx: 4 } }, "k3");
  await sendChatLine({ workspaceId: "w", text: "hi", conversationId: "c1", page: null, trail: [] }, "k4");
  expect(sent[0]!.body).toMatchObject({ conversation_id: null, fork: { session_id: "s1", part: 1, idx: 4 } });
  expect(sent[1]!.body).not.toHaveProperty("fork");
});

test("a line sent from no page carries a null page and an empty trail", async () => {
  const sent = stubFetch(() => Response.json({ conversation_id: "k2", session_id: null, workspace_id: "w1" }));
  await sendChatLine({ workspaceId: "w", text: "hi", conversationId: null, page: null, trail: [] }, "k2");
  expect(sent).toMatchObject([{ body: { page: null, trail: [] } }]);
});

test("a science chat's session names its conversation, and no other session does", () => {
  expect(conversationOf("chat:3d0e9f1a-1b2c-4d5e-8f60-7a8b9c0d1e2f")).toBe("3d0e9f1a-1b2c-4d5e-8f60-7a8b9c0d1e2f");
  for (const other of ["slack:C1:123", "trax:lab:ada", "", null, undefined]) expect(conversationOf(other)).toBeNull();
});

test("a session whose routing name is chat- and twelve hex digits is a chat's", () => {
  expect(isChatHandle("chat-3d0e9f1a1b2c")).toBe(true);
  for (const other of ["chat-3d0e9f1a1b2", "chat-3d0e9f1a1b2cc", "chat-3D0E9F1A1B2C", "scout", "chat-xyz", "my-chat-3d0e9f1a1b2c"]) {
    expect(isChatHandle(other)).toBe(false);
  }
});
