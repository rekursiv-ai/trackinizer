import { isChatHandle } from "../api/chats";
import type { FeedActorFacet, FeedEvent, MessageTarget } from "../api/sessions";

/** The text a line sends, and each target it names; or why it cannot be sent. */
export type Parsed =
  | { readonly targets: readonly MessageTarget[]; readonly text: string }
  | { readonly problem: string };

/**
 * `line` read as the old console read it: `@agent text`, `@agent:room text`,
 * `@a,@b text` (a list, each with or without its `@`), or `@* text` for every
 * agent-and-room pair among `shown`. A room is everything after an agent's
 * first `:`, since an agent's name holds none. A line that names no target goes
 * to `to`, the view's agents.
 */
export function parseLine(
  line: string,
  shown: readonly FeedEvent[],
  to: readonly MessageTarget[] = [],
): Parsed {
  const trimmed = line.trim();
  if (!trimmed.startsWith("@")) {
    return to.length
      ? { targets: to, text: trimmed }
      : {
          problem: "Pick agents for this view, or start with a target: @agent message",
        };
  }
  const space = trimmed.search(/\s/);
  const text = space < 0 ? "" : trimmed.slice(space).trim();
  if (!text) return { problem: "Write the message after the target" };
  const spec = trimmed.slice(1, space);
  if (spec === "*") {
    const targets = broadcastTargets(shown);
    return targets.length
      ? { targets, text }
      : { problem: "No agents are shown to send to" };
  }
  const targets: MessageTarget[] = [];
  for (const part of spec.split(",").map((name) => name.replace(/^@/, ""))) {
    const colon = part.indexOf(":");
    const actor = colon < 0 ? part : part.slice(0, colon);
    const room = colon < 0 ? null : part.slice(colon + 1);
    if (!actor) return { problem: `An agent's name is missing in '${part}'` };
    if (room === "") return { problem: `A room is missing in '${part}'` };
    // A science chat takes lines from its own Chat alone: the server refuses a browser's message to one.
    if (isChatHandle(actor))
      return { problem: `${actor} is a chat: only its own Chat posts to it` };
    targets.push({ actor, room });
  }
  return { targets, text };
}

/**
 * Where `@*` sends: each distinct agent-and-room pair among `shown`, in the order
 * first shown, a chat's not among them. An agent in several rooms is sent to in
 * each, since the server refuses a bare name for one; an agent in none is sent to
 * once, with no room.
 */
export function broadcastTargets(shown: readonly FeedEvent[]): MessageTarget[] {
  const targets = new Map<string, MessageTarget>();
  for (const { actor, rooms } of shown) {
    if (isChatHandle(actor)) continue;
    for (const room of rooms?.length ? rooms : [null]) {
      const target = { actor, room };
      targets.set(targetName(target), target);
    }
  }
  return [...targets.values()];
}

/** `@agent` or `@agent:room`. */
export function targetName({ actor, room }: MessageTarget): string {
  return room === null ? `@${actor}` : `@${actor}:${room}`;
}

/**
 * The session `target` names among `agents`, the facets' one row per agent: its own while it has not ended and is in
 * the room the target names, if it names one; null for none.
 */
export function sessionOf(
  { actor, room }: MessageTarget,
  agents: readonly FeedActorFacet[],
): string | null {
  const agent = agents.find(
    (each) =>
      each.actor === actor &&
      each.ended === null &&
      (room === null || each.rooms.includes(room)),
  );
  return agent?.session_id ?? null;
}

/**
 * How a line went to one target: the session that took it, null when no running agent did (none named, or its
 * session refused it as not live), or why its send failed.
 */
export type Outcome = {
  readonly target: MessageTarget;
  readonly reached: string | null | Error;
};

/**
 * What a line came to, from each target's outcome: how many sessions took it, each once, and the targets no running
 * agent took it for. A target whose send failed fails the whole line, naming it and what went: an input is sent once,
 * so the draft stays for the person to send again where it did not go.
 */
export function receipt(outcomes: readonly Outcome[]): string {
  const reached = new Set(
    outcomes.flatMap(({ reached }) => (typeof reached === "string" ? [reached] : [])),
  );
  const sent = `Sent to ${reached.size} ${reached.size === 1 ? "session" : "sessions"}`;
  const failed = outcomes.flatMap(({ target, reached }) =>
    reached instanceof Error ? [`${targetName(target)} (${reached.message})`] : [],
  );
  if (failed.length) throw new Error(`Not sent to ${failed.join(", ")}. ${sent}.`);
  const missed = outcomes.flatMap(({ target, reached }) =>
    reached === null ? [targetName(target)] : [],
  );
  return missed.length ? `${sent}; no live session for ${missed.join(", ")}` : sent;
}
