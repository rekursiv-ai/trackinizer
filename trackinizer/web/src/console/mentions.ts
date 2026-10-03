import type { FeedActorFacet, FeedEvent } from "../api/sessions";

/**
 * The target the caret is in, in the line's address: from its `@` (`start`) to
 * the comma or space that ends it (`end`), the text from its `@` to the caret
 * (`query`), and whether it is the address's only target (`whole`), the one
 * place `@*` may go.
 */
export type Mention = { readonly start: number; readonly end: number; readonly query: string; readonly whole: boolean };

/**
 * What the console knows when `@` is typed: every agent the facets have listed
 * while it is open (`agents`), the lines the feed shows, oldest first, the
 * agents the view's picks show (`shown`, none when it picks no one) and the
 * rooms it picks.
 */
export type Context = {
  readonly agents: readonly FeedActorFacet[];
  readonly lines: readonly FeedEvent[];
  readonly shown: readonly string[];
  readonly rooms: readonly string[];
};

/**
 * The target at `caret` in `line`, or null when the caret is in none. A line
 * addresses agents only in its first word, and only when that starts with `@`
 * (`parseLine`), as `@a`, `@a:room` or a list `@a,@b`; a target counts once its
 * `@` is typed, so `@a,b` suggests nothing for `b`.
 */
export function mentionAt(line: string, caret: number): Mention | null {
  const lead = line.length - line.trimStart().length;
  if (line[lead] !== "@") return null;
  const space = line.slice(lead).search(/\s/);
  const address = space < 0 ? line.length : lead + space;
  if (caret <= lead || caret > address) return null;
  const start = Math.max(lead, line.lastIndexOf(",", caret - 1) + 1);
  const comma = line.indexOf(",", caret);
  const end = comma < 0 || comma > address ? address : comma;
  if (line[start] !== "@" || caret === start) return null;
  return { start, end, query: line.slice(start + 1, caret), whole: start === lead && end === address };
}

/**
 * What `@` may complete to, as targets without their `@`, most likely first.
 *
 * Names that start with the query (in any case) come before names that only
 * hold it. Within each, as GitHub, Zulip and Mattermost rank people in the
 * conversation first: the agents the view shows (its picks, which its To chips
 * are among, or, when it picks no one, the agents with lines in the feed),
 * those with lines by their latest; then agents in the rooms the view shows;
 * then every other agent, by when the facets last heard from it; ties by name.
 * An agent in several rooms is offered once per room, the view's rooms first,
 * and never bare, since the server refuses a bare name in several rooms. An
 * agent the facets list as ended is left out, as the To chips leave it out; one
 * known only from its lines, outside the facets' window, may have ended and is
 * offered all the same. Eight agents at most, then `*` (every agent shown)
 * when the target is the whole address and the feed shows more than one agent,
 * after the agents, as Mattermost lists `@channel` after people.
 */
export function suggest(context: Context, { query, whole }: Pick<Mention, "query" | "whole">): string[] {
  const needle = query.toLowerCase();
  const said = latestLines(context.lines);
  const agents = known(context.agents, said);
  const shown = new Set([...context.shown, ...said.keys()]);
  const rooms = new Set(context.rooms.length ? context.rooms : [...shown].flatMap((actor) => agents.get(actor)?.rooms ?? []));
  const offered = [...agents].flatMap(([actor, { rooms: in_, last }]) => {
    const tier = shown.has(actor) ? 0 : in_.some((room) => rooms.has(room)) ? 1 : 2;
    const line = said.get(actor);
    const order = [tier, line ? 0 : 1, -Date.parse(line?.created ?? last)];
    const names = in_.length > 1 ? in_.map((room) => [`${actor}:${room}`, rooms.has(room) ? 0 : 1] as const) : [[actor, 0] as const];
    return names.map(([name, elsewhere]) => ({ name, key: [match(name, needle), ...order, elsewhere] }));
  });
  const ranked = offered
    .filter(({ key }) => key[0]! < NO_MATCH)
    .toSorted((a, b) => compareKeys(a.key, b.key) || a.name.localeCompare(b.name))
    .slice(0, 8)
    .map(({ name }) => name);
  const every = whole && "*".startsWith(needle) && new Set(context.lines.map(({ actor }) => actor)).size > 1;
  return every ? [...ranked, "*"] : ranked;
}

/**
 * `line` with `mention` replaced by `@name`, and where the caret goes: past a
 * space after it, which is added at the end of the line so the message can
 * follow, or before the comma that goes on to the next target.
 */
export function complete(line: string, mention: Mention, name: string): { text: string; caret: number } {
  const after = line.slice(mention.end);
  const target = `@${name}${after ? "" : " "}`;
  const caret = mention.start + target.length + (/^\s/.test(after) ? 1 : 0);
  return { text: line.slice(0, mention.start) + target + after, caret };
}

/**
 * `line` addressed to `name` too, as a click on a line's agent asks: `@name`
 * before a line that has no address, or added to the list it opens with, unless
 * the list has it already.
 */
export function addressed(line: string, name: string): string {
  const target = `@${name}`;
  const lead = line.length - line.trimStart().length;
  if (line[lead] !== "@") return line.trim() ? `${target} ${line}` : `${target} `;
  const end = lead + line.slice(lead).search(/\s|$/);
  return line.slice(lead, end).split(",").includes(target) ? line : `${line.slice(0, end)},${target}${line.slice(end)}`;
}

/** `match` for a name that does not hold the query. */
const NO_MATCH = 2;

/** How well `name` answers `needle`: 0 starts with it, 1 holds it, `NO_MATCH` neither. */
function match(name: string, needle: string): number {
  const lower = name.toLowerCase();
  return lower.startsWith(needle) ? 0 : lower.includes(needle) ? 1 : NO_MATCH;
}

/** Each agent's latest line, by name. */
function latestLines(lines: readonly FeedEvent[]): Map<string, FeedEvent> {
  const said = new Map<string, FeedEvent>();
  for (const line of lines) {
    const held = said.get(line.actor);
    if (!held || Date.parse(line.created) > Date.parse(held.created)) said.set(line.actor, line);
  }
  return said;
}

/**
 * Every agent a message may reach, by name, with its rooms and when it was last
 * heard from: the facets' agents that have not ended, and the agents with lines
 * the facets have not listed, in their latest line's rooms.
 */
function known(agents: readonly FeedActorFacet[], said: ReadonlyMap<string, FeedEvent>): Map<string, { rooms: readonly string[]; last: string }> {
  const listed = new Set(agents.map(({ actor }) => actor));
  return new Map([
    ...agents.filter(({ ended }) => !ended).map(({ actor, rooms, last }) => [actor, { rooms, last }] as const),
    ...[...said.values()].filter(({ actor }) => !listed.has(actor)).map(({ actor, rooms, created }) => [actor, { rooms: rooms ?? [], last: created }] as const),
  ]);
}

function compareKeys(a: readonly number[], b: readonly number[]): number {
  for (const [k, value] of a.entries()) if (value !== b[k]) return value - b[k]!;
  return 0;
}
