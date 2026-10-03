import type { FeedActorFacet, FeedFilters, MessageTarget } from "../api/sessions";
import { type Level, levelKinds } from "./levels";

/** What a view picks: agents and rooms by name or pattern (`atlas-*`), CLIs by name; none picks all. */
export type Selection = {
  readonly agents: readonly string[];
  readonly rooms: readonly string[];
  readonly clis: readonly string[];
};

/** How the agent facet groups its agents. */
export type Grouping = "none" | "room" | "cli" | "state" | "family";

/** One group of agents, by its name, and the pattern that picks it, for a family. */
export type AgentGroup = {
  readonly key: string;
  readonly agents: readonly FeedActorFacet[];
  readonly pattern: string | null;
};

/** Whether `entry` matches `name`: a pattern's `*`s match any run of characters; any other entry is the name itself. */
export function matches(entry: string, name: string): boolean {
  if (!entry.includes("*")) return entry === name;
  const each = entry.split("*").map((part) => part.replace(/[.+?^${}()|[\]\\]/g, "\\$&"));
  return new RegExp(`^${each.join(".*")}$`).test(name);
}

/** Whether any entry of `selection` matches `name`. */
export function isPicked(selection: readonly string[], name: string): boolean {
  return selection.some((entry) => matches(entry, name));
}

/**
 * The names `selection` picks: each of `known` an entry matches, then each exact
 * entry not known, since an agent quiet in the facets' window still has records.
 */
export function picked(selection: readonly string[], known: readonly string[]): string[] {
  const unknown = selection.filter((entry) => !entry.includes("*") && !known.includes(entry));
  return [...new Set(known)].filter((name) => isPicked(selection, name)).concat(unknown);
}

/**
 * The feed's filters for `view` against the agents seen: its agents, rooms and
 * CLIs by name, sorted, so that the same picks read the same feed whatever
 * order the facets list them in, and its level's kinds. Null when a choice
 * picks no name, so that the feed reads nothing rather than everything.
 */
export function feedFilter(view: Selection & { readonly level: Level }, agents: readonly FeedActorFacet[]): FeedFilters | null {
  const actor = picked(view.agents, agents.map(({ actor }) => actor));
  const room = picked(view.rooms, roomsOf(agents));
  if ((view.agents.length && !actor.length) || (view.rooms.length && !room.length)) return null;
  return { actor: actor.toSorted(), room: room.toSorted(), cli: view.clis.toSorted(), kind: levelKinds(view.level) };
}

/**
 * `seen` with `agents` in: one row per name, from its session heard from last.
 * A view picks among every agent seen, not only those the facets' window lists
 * now, so a narrower window drops none of its agents, records or To chips. Gives
 * back `seen` itself when `agents` change nothing, so it can be kept as state.
 */
export function remember(
  seen: ReadonlyMap<string, FeedActorFacet>,
  agents: readonly FeedActorFacet[],
): ReadonlyMap<string, FeedActorFacet> {
  const next = new Map(seen);
  for (const agent of agents) {
    const held = next.get(agent.actor);
    if (!held || Date.parse(agent.last) >= Date.parse(held.last)) next.set(agent.actor, agent);
  }
  return [...next].every(([actor, agent]) => seen.get(actor) === agent) ? seen : next;
}

/** The agents `view` shows, and whether that is every agent: it chose no agent, room or CLI. */
export function shown(view: Selection, agents: readonly FeedActorFacet[]): { agents: FeedActorFacet[]; everyone: boolean } {
  const everyone = !view.agents.length && !view.rooms.length && !view.clis.length;
  const rooms = picked(view.rooms, roomsOf(agents));
  return {
    everyone,
    agents: agents.filter(
      ({ actor, rooms: in_, cli }) =>
        (!view.agents.length || isPicked(view.agents, actor)) &&
        (!view.rooms.length || in_.some((room) => rooms.includes(room))) &&
        (!view.clis.length || view.clis.includes(cli ?? "")),
    ),
  };
}

/** The agents whose name or a room holds `query`, in any case; a query with `*` is a pattern over names. */
export function findAgents(agents: readonly FeedActorFacet[], query: string): FeedActorFacet[] {
  const needle = query.trim().toLowerCase();
  if (needle.includes("*")) return agents.filter(({ actor }) => matches(needle, actor.toLowerCase()));
  return agents.filter(({ actor, rooms }) => [actor, ...rooms].some((name) => name.toLowerCase().includes(needle)));
}

/**
 * `agents` in groups: by room (an agent in each of its rooms), CLI, state or
 * name family, the group heard from most lately first (state in its own
 * order), each group's agents newest first. Working is heard from in the last
 * five minutes and not ended; quiet is neither. A family is the name before its
 * last `-`, picked as a pattern so that it takes in new agents.
 */
export function groupAgents(agents: readonly FeedActorFacet[], by: Grouping, now: number): AgentGroup[] {
  const newest = agents.toSorted((a, b) => Date.parse(b.last) - Date.parse(a.last));
  if (by === "none") return [{ key: "", agents: newest, pattern: null }];
  const groups = new Map<string, FeedActorFacet[]>();
  for (const one of newest) {
    for (const key of keysOf(one, by, now)) groups.set(key, [...(groups.get(key) ?? []), one]);
  }
  const ordered = by === "state" ? STATES.filter((state) => groups.has(state)) : [...groups.keys()];
  return ordered.map((key) => {
    const members = groups.get(key)!;
    // A name with no `-` is a family of its own, which no pattern names.
    const pattern = by === "family" && members.every(({ actor }) => actor !== key) ? `${key}-*` : null;
    return { key, agents: members, pattern };
  });
}

/**
 * `selection` with `names` picked, or cleared when all of them are: `pattern`,
 * a family's, replaces the names it covers; without one, the names are added.
 * Clearing drops every entry that picks one of the names, a wider pattern too.
 */
export function toggle(selection: readonly string[], names: readonly string[], pattern: string | null): string[] {
  if (names.every((name) => isPicked(selection, name))) {
    return selection.filter((entry) => !names.some((name) => matches(entry, name)));
  }
  if (pattern) return [...selection.filter((entry) => !matches(pattern, entry)), pattern];
  return [...selection, ...names.filter((name) => !isPicked(selection, name))];
}

/**
 * Where a message to the view's agents goes: each of `agents` not ended, in each
 * of its rooms among `rooms` (all of them when `rooms` is empty), or with no room
 * when it has none, since the server refuses a bare name in several rooms.
 */
export function toTargets(agents: readonly FeedActorFacet[], rooms: readonly string[]): MessageTarget[] {
  return agents
    .filter(({ ended }) => !ended)
    .flatMap(({ actor, rooms: in_ }): MessageTarget[] => {
      if (!in_.length) return [{ actor, room: null }];
      return in_.filter((room) => !rooms.length || rooms.includes(room)).map((room) => ({ actor, room }));
    });
}

/** Every room `agents` are in, once each, in order. */
export function roomsOf(agents: readonly FeedActorFacet[]): string[] {
  return [...new Set(agents.flatMap(({ rooms }) => rooms))];
}

const STATES = ["Working", "Quiet", "Ended"];

/** How long an agent may be silent and still be working. */
const WORKING_MS = 5 * 60_000;

/** The groups `agent` belongs in under `by`. */
function keysOf(agent: FeedActorFacet, by: Exclude<Grouping, "none">, now: number): string[] {
  switch (by) {
    case "room":
      return agent.rooms.length ? [...agent.rooms] : ["(no room)"];
    case "cli":
      return [agent.cli ?? "(unknown)"];
    case "state":
      return [agent.ended ? "Ended" : now - Date.parse(agent.last) < WORKING_MS ? "Working" : "Quiet"];
    case "family": {
      const dash = agent.actor.lastIndexOf("-");
      return [dash > 0 ? agent.actor.slice(0, dash) : agent.actor];
    }
  }
}
