import type { FeedEvent } from "../api/sessions";
import { codexContext, taskNotification, unreadable } from "../detail/transcript/records";

/** How much of the feed a view shows: 1 Messages, 2 + Calls, 3 + Output, 4 All. */
export type Level = 1 | 2 | 3 | 4;

/** The levels, as their control names them. */
export const LEVELS: readonly { readonly level: Level; readonly name: string }[] = [
  { level: 1, name: "Messages" },
  { level: 2, name: "+ Calls" },
  { level: 3, name: "+ Output" },
  { level: 4, name: "All" },
];

/**
 * The least level that shows `event`; past All for a record with nothing to
 * read (`unreadable`), which no level shows.
 *
 * Messages is the conversation: what a person said (not what a harness wrote on
 * the user's turn: Claude's `isMeta`, codex's context, a background task's
 * notice), what the agent or another agent said, and a person's message queued
 * while the agent worked. + Calls adds tool calls; All adds the bookkeeping
 * kinds and the rest of the conversation kinds; + Output is everything else.
 * Each level is kinds the facets count, so `levelCounts` counts exactly what a
 * level shows.
 */
export function levelOf({ kind, message }: FeedEvent): number {
  const payload = isFields(message) ? message : {};
  const extra = isFields(payload.extra) ? payload.extra : {};
  if (unreadable({ kind, payload })) return Number.POSITIVE_INFINITY;
  const content = typeof payload.content === "string" ? payload.content : "";
  switch (kind) {
    case "UserMessage":
      return extra.isMeta === true || codexContext(content) || taskNotification(content) ? 4 : said(payload);
    case "AssistantMessage":
    case "AgentToAgentMessage":
      return said(payload);
    case "ContextState": {
      const attachment = isFields(extra.attachment) ? extra.attachment : {};
      const origin = isFields(attachment.origin) ? attachment.origin.kind : null;
      return payload.kind === "queued_command" && origin === "human" ? 1 : 4;
    }
    case "ToolCall":
      return 2;
    default:
      return BOOKKEEPING.includes(kind) ? 4 : 3;
  }
}

/**
 * The kinds of the records `level` shows, as the feed's filters and the minimap
 * ask for them: the conversation's, and then tool calls; every kind above. The
 * feed reads Messages as the conversation itself (`ConsoleFeed`).
 */
export function levelKinds(level: Level): readonly string[] {
  if (level > 2) return [];
  return level === 1 ? CONVERSATION : [...CONVERSATION, "ToolCall"];
}

/** How many records each level shows, from what is shown: its conversation, its record count, and its records by kind. */
export function levelCounts({
  conversation,
  count,
  kinds,
}: {
  conversation: number;
  count: number;
  kinds: { readonly [kind: string]: number };
}): readonly number[] {
  const sum = (names: readonly string[]) => names.reduce((total, name) => total + (kinds[name] ?? 0), 0);
  const rest = sum(BOOKKEEPING) + sum(CONVERSATION) - conversation;
  return [conversation, conversation + sum(["ToolCall"]), count - rest, count];
}

/** Kinds that hold conversation, and other records too (`levelOf`). */
const CONVERSATION: readonly string[] = ["AgentToAgentMessage", "AssistantMessage", "ContextState", "UserMessage"];

/** Kinds shown only at All. */
const BOOKKEEPING: readonly string[] = ["SystemMessage", "TokenUsage", "TurnContext", "UncategorizedRecord"];

type Fields = { readonly [field: string]: unknown };

/** A message is conversation when it says something: text, or an attachment (a codec tuple). */
function said(payload: Fields): Level {
  const text = typeof payload.content === "string" ? payload.content.trim() : "";
  return text || (Array.isArray(payload.attachments) && payload.attachments.length) ? 1 : 4;
}

function isFields(value: unknown): value is Fields {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
