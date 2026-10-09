import { ApiError, type CallOptions, client, send, TIMEOUT_MS } from "./client";
import type { components } from "./generated/schema";

/** One science chat in the signed-in user's History. */
export type ChatSummary = components["schemas"]["ChatSummary"];
/** A conversation's session, its starter and whether the assistant still has it open. */
export type ChatHead = components["schemas"]["ChatHead"];
/** What a posted line was queued for. */
export type ChatSent = components["schemas"]["ChatSent"];

/** A line of a science chat's session, which a fork starts from. */
export type ChatFork = {
  /** The original's session, whose records hold the line. */
  readonly sessionId: string;
  /** The line's record: its part and its position in it. */
  readonly part: number;
  readonly idx: number;
};

/** What one posted line carries besides its key. */
export type ChatLine = {
  readonly workspaceId: string;
  readonly text: string;
  /** The Chat visual it is typed in, when the canvas holds one. */
  readonly chatInstanceId: string | null;
  /** The record that Chat visual is about, which the server checks is still the one. */
  readonly expectedRecordId: string | null;
  /** The conversation it joins; null starts one, which the key then names. */
  readonly conversationId: string | null;
  /** Start a new conversation, named by the key, from this line of another instead of joining one. */
  readonly fork?: ChatFork;
  /** The `#/...` hash the sender is on, or null when it is not one the server takes. */
  readonly page: string | null;
  /** The hashes the sender came through before `page`, oldest first. */
  readonly trail: readonly string[];
};

/** The `cli_session_id` prefix and label of a science chat's session, as the server names them. */
export const CHAT_SESSION_PREFIX = "chat:";
export const SCIENCE_CHAT_LABEL = "science-chat";

/** Whether a session's routing name is the one the assistant opens a science chat under: `chat-` and twelve hex digits. */
export function isChatHandle(actor: string): boolean {
  return /^chat-[0-9a-f]{12}$/.test(actor);
}

/** The conversation id a science chat session's `cli_session_id` names, or null for any other session. */
export function conversationOf(cliSessionId: string | null | undefined): string | null {
  return cliSessionId?.startsWith(CHAT_SESSION_PREFIX) ? cliSessionId.slice(CHAT_SESSION_PREFIX.length) : null;
}

/** Read the science chats the signed-in user started or posted in, newest first. */
export async function listChats({ signal }: CallOptions = {}): Promise<ChatSummary[]> {
  const chats = await send(TIMEOUT_MS.read, signal, (signal) => client.GET("/api/chats", { signal }));
  if (!Array.isArray(chats) || !chats.every((chat) => chat && typeof chat.conversation_id === "string"
    && typeof chat.session_id === "string" && typeof chat.title === "string")) {
    throw new Error("Invalid chat list");
  }
  return chats;
}

/**
 * Find a conversation's session. Null until the assistant has opened it, which
 * it does when it first hears the conversation's line: the server answers 404 until then.
 */
export async function getChatHead(id: string, { signal }: CallOptions = {}): Promise<ChatHead | null> {
  let head: ChatHead;
  try {
    head = await send(TIMEOUT_MS.read, signal, (signal) => client.GET("/api/chats/{conversation_id}", {
      params: { path: { conversation_id: id } }, signal,
    }));
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
  if (!head || typeof head.session_id !== "string" || typeof head.conversation_id !== "string") {
    throw new Error("Invalid chat head");
  }
  return head;
}

/**
 * Post a line to a science chat, under idempotency `key`, which a retry reuses so the
 * line is queued once. The answer is the conversation id at once; its session appears
 * when the assistant opens it (`getChatHead`). A new conversation, and a fork, is named by the key.
 */
export async function sendChatLine(line: ChatLine, key: string): Promise<ChatSent> {
  return send(TIMEOUT_MS.write, undefined, (signal) => client.POST("/api/chats", {
    params: { header: { "Idempotency-Key": key } },
    body: {
      kind: "science", workspace_id: line.workspaceId, text: line.text, chat_instance_id: line.chatInstanceId,
      expected_record_id: line.expectedRecordId, conversation_id: line.conversationId,
      ...(line.fork && { fork: { session_id: line.fork.sessionId, part: line.fork.part, idx: line.fork.idx } }),
      page: line.page, trail: [...line.trail],
    },
    signal,
  }));
}
