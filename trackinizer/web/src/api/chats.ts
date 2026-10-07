import { type CallOptions, client, send, TIMEOUT_MS } from "./client";
import type { components } from "./generated/schema";

export type ChatMessage = components["schemas"]["ChatMessage"];
export type ChatSummary = components["schemas"]["ChatSummary"];
export type ChatThread = components["schemas"]["ChatThread"];

/** Read the signed-in user's conversations, newest change first. */
export async function listChats({ signal }: CallOptions = {}): Promise<ChatSummary[]> {
  const chats = await send(TIMEOUT_MS.read, signal, (signal) => client.GET("/api/chats", { signal }));
  if (!Array.isArray(chats) || !chats.every((chat) => chat && typeof chat.id === "string" && typeof chat.title === "string")) {
    throw new Error("Invalid chat list");
  }
  return chats;
}

/** Read one conversation's messages after `afterSeq` (0 reads from the start). */
export async function getChat(id: string, afterSeq: number, { signal }: CallOptions = {}): Promise<ChatThread> {
  const thread = await send(TIMEOUT_MS.read, signal, (signal) => client.GET("/api/chats/{conversation_id}", {
    params: { path: { conversation_id: id }, query: { after_seq: afterSeq } }, signal,
  }));
  if (!thread || typeof thread.id !== "string" || !Array.isArray(thread.messages)
    || !thread.messages.every((message) => message && typeof message.id === "string" && Number.isInteger(message.seq))) {
    throw new Error("Invalid chat thread");
  }
  return thread;
}
