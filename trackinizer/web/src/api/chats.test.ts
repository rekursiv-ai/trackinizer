import { afterEach, expect, test, vi } from "vitest";
import { ApiError } from "./client";
import { conversationOf, deleteChat, getChatHead, isChatHandle, readChatPartial, reattachChat, sendChatLine, shareChat, unshareChat } from "./chats";
import { stubFetch } from "./testing";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const head = {
  conversation_id: "c1", session_id: "s1", title: "Hello", account: "ada@example.com", live: true,
  forks: 2, forked_from: null, forks_on_typing: false, audience: "open", may_hide: true, shared_with: [], may_share: true, may_delete: false,
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
    body: { workspace_id: "w1", text: "hi", conversation_id: null,
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

test("an answer to a prompt goes as the chat's answer line, with its decision and its answers, and a person's line carries neither", async () => {
  const sent = stubFetch(() => Response.json({ conversation_id: "c1", session_id: "s1", workspace_id: "w1" }));
  const payload = { prompt_id: "p1", decision: "allow", updated_input: { answers: { Color: "red, blue" } }, message: "" } as const;
  await sendChatLine({ workspaceId: "w1", text: "Color: red, blue", conversationId: "c1", kind: "answer", payload, page: null, trail: [] }, "k5");
  await sendChatLine({ workspaceId: "w1", text: "hi", conversationId: "c1", page: null, trail: [] }, "k6");
  expect(sent[0]!.body).toMatchObject({ kind: "answer", conversation_id: "c1", text: "Color: red, blue", payload });
  // The server reads a post that names no kind as a person's line.
  expect(sent[1]!.body).not.toHaveProperty("kind");
  expect(sent[1]!.body).not.toHaveProperty("payload");
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

test("a share and an unshare name the conversation and the address, which is encoded; a delete names the conversation", async () => {
  const sent = stubFetch(() => Response.json({}));
  await shareChat("c1", "a+b@example.com");
  await unshareChat("c1", "a+b@example.com");
  await deleteChat("c1");
  expect(sent).toMatchObject([
    { method: "PUT", path: "/api/chats/c1/shares/a%2Bb%40example.com" },
    { method: "DELETE", path: "/api/chats/c1/shares/a%2Bb%40example.com" },
    { method: "DELETE", path: "/api/chats/c1" },
  ]);
  stubFetch(() => Response.json({ detail: "Only the starter shares" }, { status: 403 }));
  await expect(shareChat("c1", "x@example.com")).rejects.toMatchObject({ status: 403 });
});

test("the reply being written is read after the seq last read, held by the server, and timed out only past its hold", async () => {
  const state = { seq: 8, partial: { turn: "t1", text: "So far", done: false } };
  const sent = stubFetch(() => Response.json(state));
  await expect(readChatPartial("s1", 7)).resolves.toEqual(state);
  expect(sent).toMatchObject([{ method: "GET", path: "/api/sessions/s1/partial", query: "?after=7&wait_sec=25" }]);

  // A read's 15 s would end every read the server holds for its 25 s.
  vi.useFakeTimers();
  vi.stubGlobal("fetch", (request: Request) => new Promise((_, reject) => {
    request.signal.addEventListener("abort", () => reject(request.signal.reason));
  }));
  let settled = false;
  const held = readChatPartial("s1", 8).catch((error: unknown) => error).finally(() => { settled = true; });
  await vi.advanceTimersByTimeAsync(34_999);
  expect(settled).toBe(false);
  await vi.advanceTimersByTimeAsync(1);
  expect(await held).toMatchObject({ status: 0, code: "timeout", detail: "No response within 35 s." });
});

test("a reply being written that is none and an answer that is no partial are told apart", async () => {
  stubFetch(() => Response.json({ seq: 0, partial: null }));
  await expect(readChatPartial("s1", 3)).resolves.toEqual({ seq: 0, partial: null });
  for (const odd of [{}, { seq: 2 }, { seq: 2, partial: { turn: "t1" } }]) {
    stubFetch(() => Response.json(odd));
    await expect(readChatPartial("s1", 0)).rejects.toThrow("Invalid chat partial");
  }
});

test("an answer names the agent's own option, and the older form names a decision", async () => {
  const sent = stubFetch(() => Response.json({ conversation_id: "c1", session_id: "s1", workspace_id: "w1" }));
  const line = { workspaceId: "w1", text: "Allow", conversationId: "c1", kind: "answer", page: null, trail: [] } as const;
  await sendChatLine({ ...line, payload: { prompt_id: "p1", option: "allow_session", updated_input: null, message: "" } }, "k7");
  await sendChatLine({ ...line, payload: { prompt_id: "p1", decision: "deny", updated_input: null, message: "no" } }, "k8");
  expect(sent[0]!.body).toMatchObject({ payload: { prompt_id: "p1", option: "allow_session" } });
  expect(sent[0]!.body).not.toHaveProperty("payload.decision");
  expect(sent[1]!.body).toMatchObject({ payload: { prompt_id: "p1", decision: "deny", message: "no" } });
});

test("reattaching posts to the chat's session under its key, and a refusal throws the server's reason", async () => {
  const sent = stubFetch(() => new Response(null, { status: 204 }));
  await reattachChat("s1", "k9");
  expect(sent).toMatchObject([{ method: "POST", path: "/api/sessions/s1/reattach", headers: { "idempotency-key": "k9" } }]);

  stubFetch(() => Response.json({ detail: "This chat's run is live.", code: "chat_run_live" }, { status: 409 }));
  await expect(reattachChat("s1", "k10")).rejects.toMatchObject({ status: 409, detail: "This chat's run is live.", code: "chat_run_live" });
});
