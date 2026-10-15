import { ApiError, type CallOptions, client, send, TIMEOUT_MS } from "./client";
import type { components } from "./generated/schema";

/**
 * A conversation's session, its starter, whether the assistant still has it
 * open, who reads it (`audience`: `open` as the org mode says, `private` once its
 * starter hid it, `staff` once the assistant read a staff session into it), and
 * what the caller may do: `forks_on_typing` says they do not read it.
 */
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

/**
 * The 403 code of a post into a chat the poster does not read: not shared with
 * them, or staff only and they hold no staff. A fork would copy its lines, so it
 * is no way in either.
 */
export const CHAT_NOT_SHARED = "chat_not_shared";

/** The 409 code of a fork from a hidden or staff-only chat: the copy would not keep who may read it. */
export const CHAT_FORK_CLOSED = "chat_fork_closed";

/**
 * The 409 code of a fork whose readers the chat's are not: my organisation reads a
 * chat I start, so a chat shared with me from another one is posted in, not forked.
 */
export const CHAT_FORK_WIDER = "chat_fork_wider";

/** The 409 code of a share of a staff-only chat with someone who holds no staff. */
export const CHAT_STAFF_ONLY = "chat_staff_only";

/** The code (410) of a post, a head read or a station's for-chat switch that names a deleted chat; the chat is then let go and what was typed starts a new one. */
export const CHAT_DELETED = "chat_deleted";

/** The routing name the assistant opens a conversation's session under: `chat-` and the first twelve hex digits of its id. */
export function chatHandleOf(conversationId: string): string {
  return `chat-${conversationId.replaceAll("-", "").slice(0, 12)}`;
}

/** Whether a session's routing name is the one the assistant opens a science chat under: `chat-` and twelve hex digits. */
export function isChatHandle(actor: string): boolean {
  return /^chat-[0-9a-f]{12}$/.test(actor);
}

/** The conversation id a science chat session's `cli_session_id` names, or null for any other session. */
export function conversationOf(cliSessionId: string | null | undefined): string | null {
  return cliSessionId?.startsWith(CHAT_SESSION_PREFIX) ? cliSessionId.slice(CHAT_SESSION_PREFIX.length) : null;
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
      kind: "science", workspace_id: line.workspaceId, text: line.text,
      conversation_id: line.conversationId,
      ...(line.fork && { fork: { session_id: line.fork.sessionId, part: line.fork.part, idx: line.fork.idx } }),
      page: line.page, trail: [...line.trail],
    },
    signal,
  }));
}

/**
 * Let `email` read a conversation and post into it. The head says who now has it.
 * A staff-only chat takes only someone who holds staff (409 `chat_staff_only`).
 */
export async function shareChat(id: string, email: string): Promise<void> {
  await send(TIMEOUT_MS.write, undefined, (signal) => client.PUT("/api/chats/{conversation_id}/shares/{email}", {
    params: { path: { conversation_id: id, email } }, signal,
  }));
}

/** Stop `email` reading or posting into a conversation; their earlier lines stay. */
export async function unshareChat(id: string, email: string): Promise<void> {
  await send(TIMEOUT_MS.write, undefined, (signal) => client.DELETE("/api/chats/{conversation_id}/shares/{email}", {
    params: { path: { conversation_id: id, email } }, signal,
  }));
}

/** Delete a whole conversation: its sessions, records and shares, and the station binding. Its forks keep their copies. */
export async function deleteChat(id: string): Promise<void> {
  await send(TIMEOUT_MS.write, undefined, (signal) => client.DELETE("/api/chats/{conversation_id}", {
    params: { path: { conversation_id: id } }, signal,
  }));
}
