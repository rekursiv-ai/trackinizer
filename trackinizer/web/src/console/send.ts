import type { FeedEvent, MessageTarget } from "../api/sessions";

/** The text a line sends, and each target it names; or why it cannot be sent. */
export type Parsed = { readonly targets: readonly MessageTarget[]; readonly text: string } | { readonly problem: string };

/**
 * `line` read as the old console read it: `@agent text`, `@agent:room text`,
 * `@a,@b text` (a list, each with or without its `@`), or `@* text` for every
 * agent-and-room pair among `shown`. A room is everything after an agent's
 * first `:`, since an agent's name holds none. A line that names no target goes
 * to `to`, the view's agents.
 */
export function parseLine(line: string, shown: readonly FeedEvent[], to: readonly MessageTarget[] = []): Parsed {
  const trimmed = line.trim();
  if (!trimmed.startsWith("@")) {
    return to.length ? { targets: to, text: trimmed } : { problem: "Pick agents for this view, or start with a target: @agent message" };
  }
  const space = trimmed.search(/\s/);
  const text = space < 0 ? "" : trimmed.slice(space).trim();
  if (!text) return { problem: "Write the message after the target" };
  const spec = trimmed.slice(1, space);
  if (spec === "*") {
    const targets = broadcastTargets(shown);
    return targets.length ? { targets, text } : { problem: "No agents are shown to send to" };
  }
  const targets: MessageTarget[] = [];
  for (const part of spec.split(",").map((name) => name.replace(/^@/, ""))) {
    const colon = part.indexOf(":");
    const actor = colon < 0 ? part : part.slice(0, colon);
    const room = colon < 0 ? null : part.slice(colon + 1);
    if (!actor) return { problem: `An agent's name is missing in '${part}'` };
    if (room === "") return { problem: `A room is missing in '${part}'` };
    targets.push({ actor, room });
  }
  return { targets, text };
}

/**
 * Where `@*` sends: each distinct agent-and-room pair among `shown`, in the order
 * first shown. An agent in several rooms is sent to in each, since the server
 * refuses a bare name for one; an agent in none is sent to once, with no room.
 */
export function broadcastTargets(shown: readonly FeedEvent[]): MessageTarget[] {
  const targets = new Map<string, MessageTarget>();
  for (const { actor, rooms } of shown) {
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
 * What a send to `targets` came to, from each one's result: how many sessions it
 * reached, and the targets no live session matched. A target whose send failed
 * fails the whole send, which its Retry sends again under the same keys, so the
 * ones that went through are not sent twice.
 */
export function receipt(
  targets: readonly MessageTarget[],
  results: readonly PromiseSettledResult<{ readonly delivered: readonly string[] }>[],
): string {
  const failed = results.flatMap((result, k) =>
    result.status === "rejected" ? [`${targetName(targets[k]!)} (${messageOf(result.reason)})`] : [],
  );
  if (failed.length) {
    const them = failed.length === 1 ? "it" : "them";
    throw new Error(`Not sent to ${failed.join(", ")}. Retry sends ${them} again; no session gets a message twice.`);
  }
  const reached = results.reduce((sum, result) => sum + (result.status === "fulfilled" ? result.value.delivered.length : 0), 0);
  const missed = results.flatMap((result, k) =>
    result.status === "fulfilled" && result.value.delivered.length === 0 ? [targetName(targets[k]!)] : [],
  );
  const sent = `Sent to ${reached} ${reached === 1 ? "session" : "sessions"}`;
  return missed.length ? `${sent}; no live session for ${missed.join(", ")}` : sent;
}

function messageOf(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}
