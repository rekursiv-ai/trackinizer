import { expect, test } from "vitest";
import type { FeedActorFacet, FeedEvent } from "../api/sessions";
import { addressed, complete, type Context, type Mention, mentionAt, suggest } from "./mentions";

/** `actor`'s session as the facets list it: heard from at `last` (10:MM), in `rooms`. */
function agent(actor: string, last: string, rooms: string[] = [], ended: string | null = null): FeedActorFacet {
  return {
    actor,
    session_id: "00000000-0000-4000-8000-000000000001",
    cli: "claude",
    rooms,
    count: 1,
    conversation: 1,
    last: `2026-10-03T10:${last}:00Z`,
    ended,
  };
}

/** A line of `actor`'s in the feed, written at 10:MM, in `rooms`. */
function line(actor: string, created: string, rooms: string[] = []): FeedEvent {
  return {
    session_id: "00000000-0000-4000-8000-000000000002",
    actor,
    rooms,
    part: 0,
    seq: 0,
    kind: "AssistantMessage",
    created: `2026-10-03T10:${created}:00Z`,
    message: {},
    text: "",
  };
}

/** A view that picks no one, its feed showing `lines`, among `agents`. */
function everyone(agents: FeedActorFacet[], lines: FeedEvent[] = []): Context {
  return { agents, lines, shown: [], rooms: [] };
}

test.each<[string, string, number, Mention | null]>([
  ["a lone @", "@", 1, { start: 0, end: 1, query: "", whole: true }],
  ["a name typed so far", "@ti", 3, { start: 0, end: 3, query: "ti", whole: true }],
  ["the caret inside a name: the query stops at it, the name does not", "@ti", 2, { start: 0, end: 3, query: "t", whole: true }],
  ["after leading space, which the line's parse trims", "  @ti", 5, { start: 2, end: 5, query: "ti", whole: true }],
  ["an agent in a room", "@a:ops", 6, { start: 0, end: 6, query: "a:ops", whole: true }],
  ["the second of a list", "@a,@b", 5, { start: 3, end: 5, query: "b", whole: false }],
  ["the first of a list, ending at its comma", "@a,@b hi", 2, { start: 0, end: 2, query: "a", whole: false }],
  ["a list item with no @ yet", "@a,b", 4, null],
  ["a comma just typed", "@a,", 3, null],
  ["the caret before the @", "@ti", 0, null],
  ["the caret in the message", "@ti hello", 9, null],
  ["an @ inside the message, which addresses no one", "hello @ti", 9, null],
  ["a line with no @", "hello", 5, null],
  ["an empty line", "", 0, null],
])("the @ the caret is in: %s", (_, text, caret, mention) => {
  expect(mentionAt(text, caret)).toEqual(mention);
});

test.each<[string, Context, string, boolean, string[]]>([
  [
    "with nothing typed: the agents with lines in the feed, latest line first; then those in their rooms; then the rest by activity; @* last",
    everyone(
      [agent("a", "01"), agent("b", "05", ["ops"]), agent("c", "10", ["ops"]), agent("d", "20", ["lab"]), agent("e", "30", [], "2026-10-03T10:31:00Z")],
      [line("a", "01"), line("b", "05", ["ops"]), line("a", "02")],
    ),
    "",
    true,
    ["b", "a", "c", "d", "*"],
  ],
  [
    "prefix matches before substring matches, whatever the view shows",
    everyone([agent("alpha", "01"), agent("tiles-a", "02")], [line("tiles-a", "02")]),
    "a",
    true,
    ["alpha", "tiles-a"],
  ],
  ["in any case", everyone([agent("Tiles-A", "01"), agent("web", "02")]), "tIL", true, ["Tiles-A"]],
  [
    "the agents the view picks first, those with lines by their latest, then those without; then their rooms' agents",
    { agents: [agent("p", "30", ["ops"]), agent("q", "20"), agent("r", "01"), agent("s", "02", ["ops"])], lines: [line("r", "01")], shown: ["p", "q", "r"], rooms: [] },
    "",
    true,
    ["r", "p", "q", "s"],
  ],
  [
    "an agent in several rooms once per room, the rooms the view picks first, never bare; and only those rooms rank their agents",
    {
      agents: [agent("x", "01", ["lab", "ops"]), agent("y", "02", ["ops"]), agent("z", "03", ["lab"]), agent("w", "04")],
      lines: [],
      shown: ["x", "y"],
      rooms: ["ops"],
    },
    "",
    true,
    ["y", "x:ops", "x:lab", "w", "z"],
  ],
  ["a room typed after the colon", everyone([agent("x", "01", ["ops", "lab"])]), "x:o", true, ["x:ops"]],
  [
    "an agent the facets have not listed, from its lines",
    everyone([agent("a", "01")], [line("new", "09", ["ops", "lab"])]),
    "",
    true,
    ["new:lab", "new:ops", "a"],
  ],
  ["ties by name", everyone([agent("b", "01"), agent("a", "01")]), "", true, ["a", "b"]],
  ["no @* in a list", everyone([agent("a", "01"), agent("b", "02")], [line("a", "01"), line("b", "02")]), "", false, ["b", "a"]],
  ["no @* for one agent shown", everyone([agent("a", "01"), agent("b", "02")], [line("a", "01")]), "", true, ["a", "b"]],
  ["@* for a typed *", everyone([agent("a", "01")], [line("a", "01"), line("b", "02")]), "*", true, ["*"]],
  ["nothing that matches", everyone([agent("a", "01")]), "zz", true, []],
  [
    "eight agents at most, then @*",
    everyone("abcdefghij".split("").map((name, k) => agent(name, `0${k}`)), [line("a", "00"), line("b", "01")]),
    "",
    true,
    ["b", "a", "j", "i", "h", "g", "f", "e", "*"],
  ],
])("suggest: %s", (_, context, query, whole, names) => {
  expect(suggest(context, { query, whole })).toEqual(names);
});

test.each<[string, string, number, string, { text: string; caret: number }]>([
  ["at the end of the line, with a space to type the message after", "@ti", 3, "tiles-a", { text: "@tiles-a ", caret: 9 }],
  ["before the message, the caret past its space", "@ti hello", 3, "tiles-a", { text: "@tiles-a hello", caret: 9 }],
  ["the whole name, from the caret in its middle", "@tixx hello", 3, "tiles-a", { text: "@tiles-a hello", caret: 9 }],
  ["a list item, the caret before its comma", "@a,@b hi", 2, "alpha", { text: "@alpha,@b hi", caret: 6 }],
  ["the last of a list", "@a,@b", 5, "beta:ops", { text: "@a,@beta:ops ", caret: 13 }],
  ["every agent shown", "@", 1, "*", { text: "@* ", caret: 3 }],
  ["after leading space", "  @t", 4, "tiles-a", { text: "  @tiles-a ", caret: 11 }],
])("complete: %s", (_, text, caret, name, completed) => {
  expect(complete(text, mentionAt(text, caret)!, name)).toEqual(completed);
});

test.each<[string, string, string, string]>([
  ["an empty box, with a space to type the message after", "", "tiles-a", "@tiles-a "],
  ["a box of spaces", "  ", "tiles-a", "@tiles-a "],
  ["a message with no address, before it", "stop now", "tiles-a", "@tiles-a stop now"],
  ["an address, added to its list", "@tiles-b stop", "tiles-a:ops", "@tiles-b,@tiles-a:ops stop"],
  ["an address alone, added to its list", "@tiles-b ", "tiles-a", "@tiles-b,@tiles-a "],
  ["an address that has it, as it is", "@tiles-b,@tiles-a stop", "tiles-a", "@tiles-b,@tiles-a stop"],
])("addressed: %s", (_, line, name, text) => {
  expect(addressed(line, name)).toBe(text);
});
