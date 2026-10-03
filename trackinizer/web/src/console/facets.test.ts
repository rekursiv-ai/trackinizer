import { expect, test } from "vitest";
import type { FeedActorFacet } from "../api/sessions";
import {
  feedFilter,
  findAgents,
  groupAgents,
  isPicked,
  matches,
  picked,
  remember,
  shown,
  toggle,
  toTargets,
} from "./facets";

const NOW = Date.parse("2026-10-02T12:00:00Z");

/** Agent `actor`, last heard from `minutes` ago. */
function agent(actor: string, minutes: number, { rooms = [] as string[], cli = "codex", ended = null as string | null, count = 10 } = {}): FeedActorFacet {
  return {
    actor,
    session_id: `00000000-0000-4000-8000-${String(actor.length).padStart(12, "0")}`,
    cli,
    rooms,
    count,
    conversation: count / 10,
    last: new Date(NOW - minutes * 60_000).toISOString(),
    ended,
  };
}

const AGENTS = [
  agent("lead_atlas-a1", 1, { rooms: ["atlas-a1"] }),
  agent("child_coder", 3, { rooms: ["atlas-a1"], cli: "claude" }),
  agent("lead_atlas-b", 20, { rooms: ["atlas-b"] }),
  agent("Ada#8", 2, { cli: "slackbot" }),
  agent("critic-zo", 300, { rooms: ["critic"], ended: "2026-10-02T07:01:00Z" }),
];

test("a pattern's * matches any run of characters; any other entry matches only its own name", () => {
  expect(matches("lead_atlas-*", "lead_atlas-a1")).toBe(true);
  expect(matches("*atlas*", "lead_atlas-b")).toBe(true);
  expect(matches("lead_atlas-*", "lead_atlas")).toBe(false);
  expect(matches("a.b", "axb")).toBe(false);
  expect(matches("Ada#8", "Ada#8")).toBe(true);
  expect(matches("Ada", "Ada#8")).toBe(false);
});

test("a selection picks the known names its patterns match, and its exact names even when unknown", () => {
  const names = AGENTS.map(({ actor }) => actor);
  expect(picked(["lead_atlas-*", "gone-agent"], names)).toEqual(["lead_atlas-a1", "lead_atlas-b", "gone-agent"]);
  expect(picked([], names)).toEqual([]);
  expect(isPicked(["lead_atlas-*"], "lead_atlas-b")).toBe(true);
});

test("the feed reads the view's agents, rooms and CLIs by name, and its level's kinds; a choice that picks nothing reads nothing", () => {
  const view = { agents: ["lead_atlas-*"], rooms: [], clis: ["codex"], level: 1 as const };
  expect(feedFilter(view, AGENTS)).toEqual({
    actor: ["lead_atlas-a1", "lead_atlas-b"],
    room: [],
    cli: ["codex"],
    kind: ["AgentToAgentMessage", "AssistantMessage", "ContextState", "UserMessage"],
  });
  expect(feedFilter({ agents: [], rooms: ["atlas-*"], clis: [], level: 4 }, AGENTS)).toEqual({
    actor: [],
    room: ["atlas-a1", "atlas-b"],
    cli: [],
    kind: [],
  });
  expect(feedFilter({ agents: ["nobody-*"], rooms: [], clis: [], level: 4 }, AGENTS)).toBeNull();
});

test("the feed's filters are sorted, so the facets in another order, or picks made in another order, read the same feed", () => {
  const view = { agents: ["lead_atlas-*", "Ada#8"], rooms: ["atlas-*"], clis: ["slackbot", "codex"], level: 2 as const };
  const filter = feedFilter(view, AGENTS);
  expect(filter).toMatchObject({ actor: ["Ada#8", "lead_atlas-a1", "lead_atlas-b"], room: ["atlas-a1", "atlas-b"], cli: ["codex", "slackbot"] });
  expect(feedFilter(view, AGENTS.toReversed())).toEqual(filter);
  expect(feedFilter({ ...view, agents: view.agents.toReversed(), clis: view.clis.toReversed() }, AGENTS)).toEqual(filter);
});

test("the agents seen are remembered, one row per name from its session heard from last; a read that adds nothing gives back the same", () => {
  const seen = remember(new Map(), AGENTS);
  expect([...seen.keys()]).toEqual(AGENTS.map(({ actor }) => actor));
  // A narrower window lists fewer agents, and forgets none.
  expect(remember(seen, AGENTS.slice(0, 2))).toBe(seen);
  const restarted = agent("critic-zo", 0, { rooms: ["critic"] });
  const next = remember(seen, [restarted, AGENTS[4]!]);
  expect(next.get("critic-zo")).toBe(restarted);
  expect(remember(next, [AGENTS[4]!])).toBe(next);
});

test("a view shows the agents that pass its agent, room and CLI choices, or every agent when it chooses none", () => {
  const actors = (view: { agents: string[]; rooms: string[]; clis: string[] }) => {
    const { agents, everyone } = shown(view, AGENTS);
    return { actors: agents.map(({ actor }) => actor), everyone };
  };
  expect(actors({ agents: [], rooms: [], clis: [] })).toEqual({ actors: AGENTS.map(({ actor }) => actor), everyone: true });
  expect(actors({ agents: [], rooms: ["atlas-a1"], clis: [] })).toEqual({ actors: ["lead_atlas-a1", "child_coder"], everyone: false });
  expect(actors({ agents: ["*atlas*", "child_coder"], rooms: [], clis: ["claude"] })).toEqual({ actors: ["child_coder"], everyone: false });
});

test("agents are found by a fragment of their name or room, or by a pattern", () => {
  const found = (query: string) => findAgents(AGENTS, query).map(({ actor }) => actor);
  expect(found("ATLAS-A")).toEqual(["lead_atlas-a1", "child_coder"]);
  expect(found("lead_*-b")).toEqual(["lead_atlas-b"]);
  expect(found("")).toHaveLength(5);
});

test("agents group by room, CLI, state or name family, the busiest-lately group first, each newest first", () => {
  const groups = (by: Parameters<typeof groupAgents>[1]) =>
    groupAgents(AGENTS, by, NOW).map(({ key, agents, pattern }) => [key, agents.map(({ actor }) => actor), pattern]);
  expect(groups("room")).toEqual([
    ["atlas-a1", ["lead_atlas-a1", "child_coder"], null],
    ["(no room)", ["Ada#8"], null],
    ["atlas-b", ["lead_atlas-b"], null],
    ["critic", ["critic-zo"], null],
  ]);
  expect(groups("cli")).toEqual([
    ["codex", ["lead_atlas-a1", "lead_atlas-b", "critic-zo"], null],
    ["slackbot", ["Ada#8"], null],
    ["claude", ["child_coder"], null],
  ]);
  // Working: heard from in the last 5 minutes, and not ended.
  expect(groups("state")).toEqual([
    ["Working", ["lead_atlas-a1", "Ada#8", "child_coder"], null],
    ["Quiet", ["lead_atlas-b"], null],
    ["Ended", ["critic-zo"], null],
  ]);
  // A family is the name before its last `-`, and is picked as a pattern.
  expect(groups("family")).toEqual([
    ["lead_atlas", ["lead_atlas-a1", "lead_atlas-b"], "lead_atlas-*"],
    ["Ada#8", ["Ada#8"], null],
    ["child_coder", ["child_coder"], null],
    ["critic", ["critic-zo"], "critic-*"],
  ]);
  expect(groups("none")).toEqual([["", ["lead_atlas-a1", "Ada#8", "child_coder", "lead_atlas-b", "critic-zo"], null]]);
});

test("a group is picked in one step: a family as its pattern, any other group as its names; picking it again clears it", () => {
  const tiles = ["lead_atlas-a1", "lead_atlas-b"];
  expect(toggle(["lead_atlas-b", "Ada#8"], tiles, "lead_atlas-*")).toEqual(["Ada#8", "lead_atlas-*"]);
  expect(toggle(["Ada#8", "lead_atlas-*"], tiles, "lead_atlas-*")).toEqual(["Ada#8"]);
  const room = ["lead_atlas-a1", "child_coder"];
  expect(toggle(["child_coder"], room, null)).toEqual(["child_coder", "lead_atlas-a1"]);
  expect(toggle(["child_coder", "lead_atlas-a1"], room, null)).toEqual([]);
  // Clearing drops whatever picks one of the names, a wider pattern too.
  expect(toggle(["lead_*", "Ada#8"], ["lead_atlas-b"], null)).toEqual(["Ada#8"]);
});

test("To is each agent shown that has not ended, in each of its rooms the view shows, or with no room when it has none", () => {
  expect(toTargets(AGENTS, [])).toEqual([
    { actor: "lead_atlas-a1", room: "atlas-a1" },
    { actor: "child_coder", room: "atlas-a1" },
    { actor: "lead_atlas-b", room: "atlas-b" },
    { actor: "Ada#8", room: null },
  ]);
  const both = agent("lead", 1, { rooms: ["ops", "lab"] });
  expect(toTargets([both], [])).toEqual([
    { actor: "lead", room: "ops" },
    { actor: "lead", room: "lab" },
  ]);
  expect(toTargets([both], ["lab"])).toEqual([{ actor: "lead", room: "lab" }]);
});
