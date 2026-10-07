import type { QueryClient } from "@tanstack/react-query";
import type { ChatMessage, ChatThread } from "../api/chats";

/**
 * A conversation's lines, in one cache entry: the thread read, then every line
 * the events stream and a send's receipt appended. `read` says whether the
 * thread read has landed; before it, the entry holds only pushed lines, which
 * the read then merges with.
 */
export type ChatLines = {
  readonly messages: readonly ChatMessage[];
  /** The server holds older messages than the read returned. */
  readonly earlier: boolean;
  readonly read: boolean;
};

/** At most this many lines are kept for a conversation. */
export const KEPT = 500;

export const chatKey = (conversationId: string | null) => ["chat", conversationId] as const;

/** `held` and `fresh` merged: each message once by id, in `seq` order, the newest `KEPT`. */
export function mergeMessages(held: readonly ChatMessage[], fresh: readonly ChatMessage[]): ChatMessage[] {
  const byId = new Map<string, ChatMessage>();
  for (const message of [...held, ...fresh]) byId.set(message.id, message);
  return [...byId.values()].sort((left, right) => left.seq - right.seq).slice(-KEPT);
}

/** The thread read `thread`, merged with the lines already pushed into `held`. */
export function readLines(thread: ChatThread, held: ChatLines | undefined): ChatLines {
  const all = mergeMessages(thread.messages, held?.messages ?? []);
  // Pushed lines older than the read's window are not "earlier" ones lost.
  return { messages: all, earlier: thread.earlier === true, read: true };
}

/** Append lines to a conversation's entry, whether or not its thread has been read. */
export function appendLines(client: QueryClient, conversationId: string, messages: readonly ChatMessage[]): void {
  if (messages.length === 0) return;
  client.setQueryData<ChatLines>(chatKey(conversationId), (held) => ({
    messages: mergeMessages(held?.messages ?? [], messages),
    earlier: held?.earlier ?? false,
    read: held?.read ?? false,
  }));
}

/** The highest `seq` held for a conversation, 0 for none. */
export function lastSeq(lines: ChatLines | undefined): number {
  return Math.max(0, ...(lines?.messages ?? []).map((message) => message.seq));
}
