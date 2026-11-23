import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { sameTopic, setTopic, topicName, topicOf, useTopic } from "./topic";

/**
 * Every kind, in the server's order, as `inquiry_kind_all` gives it to the boot
 * (`GET /api/meta/enums` → `app/boot.ts`). All nine, not a handy four: the kinds whose
 * names run together are exactly the ones a short fixture would never have tested, and
 * `namedRows` builds its pattern out of this list.
 */
const KINDS = [
  "Issue", "Artifact", "Experiment", "Paper", "Belief",
  "CodeChange", "WebResult", "WebSearch", "AgentSession",
];

afterEach(() => setTopic(null));

test("a composition is about the first row it names, which is the one its title leads with", () => {
  const words = [
    "Necessary, or alongside? Belief#651 against its own series",
    "The clamp arm is an intervention, not an observation, which Experiment#1658 ran.",
  ].join("\n\n");
  expect(topicOf(words, KINDS)).toEqual({ kind: "Belief", seq: 651 });
});

test("a ref quoted as code names nothing: an answer showing a command is not about the row in it", () => {
  // `namedRows` reads what the page renders as a ref, and code is not one. An answer
  // whose first mention of a row is inside `trax belief 651` is about whatever it goes
  // on to discuss, not about the example in its snippet.
  const words = "How to read one\n\n`trax belief 651`\n\nIt bears on Issue#12.";
  expect(topicOf(words, KINDS)).toEqual({ kind: "Issue", seq: 12 });
});

test.each([
  ["Issue", "Issue#412 is the one"],
  ["Artifact", "Artifact#7 holds it"],
  ["Experiment", "Experiment#1658 ran the arm"],
  ["Paper", "Paper#34 says so"],
  ["Belief", "Belief#651 against its own series"],
  ["CodeChange", "CodeChange#19 landed it"],
  ["WebResult", "WebResult#5 was fetched"],
  ["WebSearch", "WebSearch#2 found them"],
  ["AgentSession", "AgentSession#88 did the work"],
])("a composition can be about a %s, so no kind is unreachable from the canvas", (kind, words) => {
  expect(topicOf(words, KINDS)).toEqual({ kind, seq: Number(/#(\d+)/.exec(words)![1]) });
});

test("a composition that names no row is about nothing, and the rail is left as it was", () => {
  expect(topicOf("What the three arms did, with no row named at all.", KINDS)).toBeNull();
  // Kinds arrive with the boot; until they do, a `Belief#651` is not yet a ref and the
  // pattern must not fall back to naming every bare `#651`.
  expect(topicOf("Belief#651 against its own series", [])).toBeNull();
});

test("a uuid names a row too, so a composition distilled from a record id has a topic", () => {
  const id = "5d315d5c-881e-4331-8b3c-144017cf772d";
  expect(topicOf(`Knowledge Graph · ${id}`, KINDS)).toEqual({ id });
  expect(topicName({ id })).toBe("5d315d5c");
  expect(topicName({ kind: "Belief", seq: 651 })).toBe("Belief#651");
});

test("a view reading the subject is told when it changes, and not when it is rewritten the same", () => {
  let drawn = 0;
  const { result, unmount } = renderHook(() => {
    drawn += 1;
    return useTopic();
  });
  expect(result.current).toBeNull();

  const was = drawn;
  act(() => setTopic({ kind: "Belief", seq: 651 }));
  expect(result.current).toEqual({ kind: "Belief", seq: 651 });
  expect(drawn).toBeGreaterThan(was);

  // The same row again is not a change: a composition that re-renders on every poll
  // must not drag the whole rail through a render with it.
  const settled = drawn;
  act(() => setTopic({ kind: "Belief", seq: 651 }));
  expect(drawn).toBe(settled);

  act(() => setTopic(null));
  expect(result.current).toBeNull();
  unmount();
});

test("two rows are the same subject only when they name the same row", () => {
  expect(sameTopic(null, null)).toBe(true);
  expect(sameTopic({ kind: "Belief", seq: 651 }, { kind: "Belief", seq: 651 })).toBe(true);
  expect(sameTopic({ kind: "Belief", seq: 651 }, { kind: "Issue", seq: 651 })).toBe(false);
  expect(sameTopic({ kind: "Belief", seq: 651 }, null)).toBe(false);
  expect(sameTopic({ id: "a" }, { id: "a" })).toBe(true);
  expect(sameTopic({ id: "a" }, { kind: "Belief", seq: 651 })).toBe(false);
});
