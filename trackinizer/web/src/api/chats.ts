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
/**
 * The reply the assistant is writing in a chat, held in the server's memory only:
 * its turn (one per line it answers), its text so far, and `done` once that text is
 * the answer now being stored.
 */
export type ChatPartial = components["schemas"]["ChatPartial"];
/** A chat's partial as the server holds it now, under `seq`, new with every change and compared for equality only (0: none held). */
export type ChatPartialState = components["schemas"]["ChatPartialState"];

/** How long the server holds a read of a chat's partial that has nothing new: its cap, `CHAT_PARTIAL_WAIT_SEC`. */
const PARTIAL_WAIT_SEC = 25;

/** A line of a science chat's session, which a fork starts from. */
export type ChatFork = {
  /** The original's session, whose records hold the line. */
  readonly sessionId: string;
  /** The line's record: its part and its position in it. */
  readonly part: number;
  readonly idx: number;
};

/**
 * An answer to an ask an agent made, which the run takes from the chat's `answer` line.
 * It names the agent's own `option` by id; a card from before the options names a
 * `decision` instead, which the server reads as the option of that kind. `updated_input`
 * carries a question's answers under their headers (or the agent's ids for them), and is
 * null for a permission or a plan; `message` is the person's words with the choice. The
 * wire types a post's `payload` as any object, so its shape for an `answer`
 * (`docs/api.md`) is spelled here.
 */
export type AnswerPayload = {
  readonly prompt_id: string;
  readonly updated_input: { readonly answers: { readonly [header: string]: string } } | null;
  readonly message: string;
} & ({ readonly option: string } | { readonly decision: "allow" | "deny" });

/** What one posted line carries besides its key. */
export type ChatLine = {
  readonly canvasId: string;
  readonly text: string;
  /** The conversation it joins; null starts one, which the key then names. */
  readonly conversationId: string | null;
  /** Start a new conversation, named by the key, from this line of another instead of joining one. */
  readonly fork?: ChatFork;
  /** The `#/...` hash the sender is on, or null when it is not one the server takes. */
  readonly page: string | null;
  /** The hashes the sender came through before `page`, oldest first. */
  readonly trail: readonly string[];
  /**
   * `answer` posts a decision on a prompt, with its `payload`; `interrupt` stops the turn a claude or codex run is on,
   * with none; absent for a line a person typed.
   */
  readonly kind?: "answer" | "interrupt";
  readonly payload?: AnswerPayload;
};

type ChatSend = components["schemas"]["ChatSend"];

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

/** The code (410) of a post, a head read or a canvas's for-chat switch that names a deleted chat; the chat is then let go and what was typed starts a new one. */
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
 * Read the reply the assistant is writing in a chat's session once its `seq` is not
 * `after`, the one last read (0 at first). The server holds the read until it is, or
 * for 25 s, so the timeout is 10 s past that: a read's 15 s would end every held read.
 * Only the chat's readers may read it; 404 when the server streams no replies.
 */
export async function readChatPartial(sessionId: string, after: number, { signal }: CallOptions = {}): Promise<ChatPartialState> {
  const state = await send((PARTIAL_WAIT_SEC + 10) * 1000, signal, (signal) => client.GET("/api/sessions/{session_id}/partial", {
    params: { path: { session_id: sessionId }, query: { after, wait_sec: PARTIAL_WAIT_SEC } }, signal,
  }));
  if (!state || typeof state.seq !== "number" || (state.partial !== null && typeof state.partial?.text !== "string")) {
    throw new Error("Invalid chat partial");
  }
  return state;
}

/**
 * Post a line to a science chat, under idempotency `key`, which a retry reuses so the
 * line is queued once. The answer is the conversation id at once; its session appears
 * when the assistant opens it (`getChatHead`). A new conversation, and a fork, is named by the key.
 */
export async function sendChatLine(line: ChatLine, key: string): Promise<ChatSent> {
  const body: ChatSend = {
    canvas_id: line.canvasId, text: line.text,
    conversation_id: line.conversationId,
    ...(line.kind && { kind: line.kind }),
    ...(line.payload && { payload: line.payload }),
    ...(line.fork && { fork: { session_id: line.fork.sessionId, part: line.fork.part, idx: line.fork.idx } }),
    page: line.page, trail: [...line.trail],
  };
  return send(TIMEOUT_MS.write, undefined, (signal) => client.POST("/api/chats", {
    params: { header: { "Idempotency-Key": key } },
    body,
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

/** Delete a whole conversation: its sessions, records and shares, and the canvas binding. Its forks keep their copies. */
export async function deleteChat(id: string): Promise<void> {
  await send(TIMEOUT_MS.write, undefined, (signal) => client.DELETE("/api/chats/{conversation_id}", {
    params: { path: { conversation_id: id } }, signal,
  }));
}

/**
 * Take a chat's run back from the terminal that took it over: the server reopens the run
 * headless, on the same machine and in the same folder, resuming the CLI's own session, so
 * Chat can answer it again. The owner only. A run that is live and not taken over is a 409
 * (`chat_run_live`), a person who is not the owner a 403. A retry under the same `key`
 * reopens it once.
 */
export async function reattachChat(sessionId: string, key: string): Promise<void> {
  await send(TIMEOUT_MS.write, undefined, (signal) => client.POST("/api/sessions/{session_id}/reattach", {
    params: { path: { session_id: sessionId }, header: { "Idempotency-Key": key } }, signal,
  }));
}
