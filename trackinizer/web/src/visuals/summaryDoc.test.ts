import { expect, test } from "vitest";
import {
  anchorOf, itemRange, parseCorpusDoc, parseSessionDoc, sessionsByStart, type SessionDoc, type SessionRow,
} from "./summaryDoc";

const step = { start: 0, end: 3, title: "Fix", text: "Fixed.", learned: "x" };
const session: SessionDoc = {
  kind: "session", session: "a", ref: "AgentSession#1", state: "running", as_of: "2026-10-12T00:00:00+00:00",
  title: "t", ask: "a", overview: "o", outcome: "success", items: 4, tool_calls: 0, errors: 0,
  learnings: 1, started: "", steps: [step], phases: [step],
  beliefs: [{ id: "b0", claim: "c", verdict: "supported", confidence: 0.5, evidence: [{ at: 1, valence: -1 }] }],
  anchors: [{ item: 1, part: 0, idx: 5 }],
};

test("a session document reads back as written", () => {
  expect(parseSessionDoc(JSON.stringify({ ...session, version: 1 }))).toEqual({ ...session, version: 1 });
});

test.each([
  ["no text", null],
  ["not json", "{"],
  ["a campaign", JSON.stringify({ kind: "campaign", version: 1 })],
  ["a later version", JSON.stringify({ ...session, version: 2 })],
  ["a bad evidence sign", JSON.stringify({ ...session, version: 1, beliefs: [{ ...session.beliefs[0], evidence: [{ at: 1, valence: 0 }] }] })],
  ["a missing field", JSON.stringify({ ...session, version: 1, anchors: undefined })],
])("%s is no session document", (_, text) => {
  expect(parseSessionDoc(text)).toBeNull();
});

const row = (id: string, started: string): SessionRow => ({
  session: id, ref: `AgentSession#${id}`, title: id, outcome: "success", started, items: 1,
  errors: 0, learnings: 0,
});

test("a campaign document reads back as written", () => {
  const doc = {
    kind: "campaign", version: 1, campaign: "c", issue: "Issue#1", state: "closed", as_of: "2026-10-12T00:00:00+00:00",
    sessions: [row("a", "")],
    efforts: [{ title: "e", text: "t", members: [{ session: "a", title: "p", text: "x" }] }],
    claims: [{ claim: "c", status: "contested", supporting: 1, refuting: 1, members: [{ session: "a", claim: "c", verdict: "open" }] }],
  };

  expect(parseCorpusDoc(JSON.stringify(doc))).toEqual(doc);
  expect(parseCorpusDoc(JSON.stringify({ ...doc, state: "paused" }))).toBeNull();
  expect(parseSessionDoc(JSON.stringify(doc))).toBeNull();
});

test("the sessions list by start, ties by id, with no tree", () => {
  const rows = [row("c2", "2026-10-12T02"), row("lead", "2026-10-12T00"), row("b", "2026-10-12T01"), row("a", "2026-10-12T01")];

  expect(sessionsByStart(rows).map((r) => r.session)).toEqual(["lead", "a", "b", "c2"]);
});

test("an item's anchor is found, and an item the summary did not anchor has none", () => {
  expect(anchorOf(session, 1)).toEqual({ item: 1, part: 0, idx: 5 });
  expect(anchorOf(session, 2)).toBeUndefined();
});

test("an item range names one item or a span", () => {
  expect([itemRange({ start: 7, end: 7 }), itemRange({ start: 7, end: 23 })]).toEqual(["#7", "#7–23"]);
});
