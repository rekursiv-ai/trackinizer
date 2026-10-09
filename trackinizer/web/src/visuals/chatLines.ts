import type { SessionRecord } from "../api/sessions";

/** What the key of a line not yet a record starts with, before the key of its send. */
export const PENDING = "pending:";

/** One part of a science chat's session: its file's number and the records read from it. */
export type ChatPart = { readonly part: number; readonly records: readonly SessionRecord[] };

/** Where a stored line is in its session: its part and its position in it. */
export type LineAt = { readonly part: number; readonly idx: number };

/** One line of the transcript: a person's, an answer, or one still being sent. */
export type Line = {
  readonly key: string;
  /** The record the line is, which a fork starts from; null for a line not yet stored. */
  readonly at: LineAt | null;
  readonly role: "user" | "assistant";
  /** The poster's attested email for a person's line; null for an answer. */
  readonly author: string | null;
  readonly text: string;
  /** Sent from this browser and not yet a record of the session. */
  readonly pending: boolean;
};

/** A line sent from this browser; it shows until its record arrives. */
export type PendingLine = {
  readonly key: string;
  readonly text: string;
  /** The conversation it was sent to; null for a new one. */
  readonly conversationId: string | null;
  /** How many lines by the sender showed, stored or sending, when it was sent. */
  readonly baseline: number;
};

/** What a chat's records say: its lines, and what the agent is doing about the last one. */
export type Transcript = {
  readonly lines: readonly Line[];
  /**
   * What the agent is doing when the last line is a person's and no answer has come: the
   * name of the last tool it called since, else `""` (it has not called one, or this
   * is the last record). Null when the last line is an answer, or there is none.
   */
  readonly working: string | null;
};

/**
 * The lines of a science chat, read from its session's records: a person's line is an
 * `AgentToAgentMessage` from its poster, an answer is an `AssistantMessage` with words.
 * Tool calls and results are not lines, but the last call since the last person's line
 * says what the agent is doing.
 */
export function readTranscript(parts: readonly ChatPart[]): Transcript {
  const lines: Line[] = [];
  let working: string | null = null;
  for (const { part, records } of parts) {
    for (const record of records) {
      const key = `${part}:${record.idx}`;
      const payload = record.payload ?? {};
      if (record.kind === "AgentToAgentMessage") {
        lines.push({ key, at: { part, idx: record.idx }, role: "user", author: text(payload.sender) || null, text: text(payload.content), pending: false });
        working = "";
      } else if (record.kind === "AssistantMessage" && text(payload.content)) {
        lines.push({ key, at: { part, idx: record.idx }, role: "assistant", author: null, text: text(payload.content), pending: false });
        working = null;
      } else if (record.kind === "ToolCall" && working !== null) {
        working = text(payload.name);
      }
    }
  }
  return { lines, working };
}

/**
 * The transcript's lines, then what this browser sent that is not a record yet. A
 * sent line is a record once the session holds more lines by `me` than it did when the
 * line was sent, in-flight ones counted. The words are not compared: the assistant may
 * have changed them (a stored key is replaced) before recording the line.
 */
export function withPending(
  transcript: Transcript,
  pending: readonly PendingLine[],
  { me }: { readonly me: string },
): Line[] {
  const stored = transcript.lines.filter((line) => line.role === "user" && line.author === me).length;
  const waiting = pending
    .filter((line) => stored < line.baseline + 1)
    .map((line): Line => ({ key: `${PENDING}${line.key}`, at: null, role: "user", author: me, text: line.text, pending: true }));
  return [...transcript.lines, ...waiting];
}

/** How many of `lines`, sent or still being sent, are `me`'s. */
export function sentBefore(lines: readonly Line[], { me }: { readonly me: string }): number {
  return lines.filter((line) => line.role === "user" && line.author === me).length;
}

/** The lines of a transcript up to and including the one stored at `at`: what a fork from it opens with. */
export function linesThrough(lines: readonly Line[], at: LineAt): Line[] {
  const end = lines.findIndex((line) => line.at?.part === at.part && line.at.idx === at.idx);
  return end < 0 ? [] : lines.slice(0, end + 1);
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}
