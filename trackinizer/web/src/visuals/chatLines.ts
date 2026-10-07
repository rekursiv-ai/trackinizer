import type { ChatMessage } from "../api/chats";

/** A message sent and not yet stored. */
export type PendingLine = {
  readonly key: string;
  readonly text: string;
  /** The conversation it was sent to; null for a new one. */
  readonly conversationId: string | null;
};

/** One line of the transcript: stored, or pending. */
export type Line = {
  readonly key: string;
  readonly role: "user" | "assistant";
  readonly text: string;
  /** The stored message's `seq`; null while pending. */
  readonly seq: number | null;
};

/** The transcript: the stored messages, then what was sent and is not stored yet. */
export function transcript(stored: readonly ChatMessage[], pending: readonly PendingLine[]): Line[] {
  return [
    ...stored.map((message): Line => ({ key: message.id, role: message.role, text: message.text, seq: message.seq })),
    ...pending.map((line): Line => ({ key: `pending:${line.key}`, role: "user", text: line.text, seq: null })),
  ];
}
