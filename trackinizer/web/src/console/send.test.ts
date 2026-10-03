import { expect, test } from "vitest";
import type { FeedEvent } from "../api/sessions";
import { broadcastTargets, parseLine, receipt } from "./send";

/** A feed event from `actor`'s session, in `rooms`. */
function said(actor: string, rooms: string[] = []): FeedEvent {
  return {
    session_id: "00000000-0000-4000-8000-000000000001",
    actor,
    rooms,
    part: 0,
    seq: 0,
    kind: "UserMessage",
    created: "2026-10-01T10:00:00Z",
    message: {},
    text: "",
  };
}

test("a line names an agent, an agent in a room, or several, then its text (v1's grammar)", () => {
  expect(parseLine("@codex fix it", [])).toEqual({ targets: [{ actor: "codex", room: null }], text: "fix it" });
  expect(parseLine("  @codex:ops   fix it  ", [])).toEqual({ targets: [{ actor: "codex", room: "ops" }], text: "fix it" });
  // v1 sent `@b` here, the @ and all, to an agent no one is named.
  expect(parseLine("@a,@b:ops,c hi", [])).toEqual({
    targets: [
      { actor: "a", room: null },
      { actor: "b", room: "ops" },
      { actor: "c", room: null },
    ],
    text: "hi",
  });
  // A room may hold a colon; an agent's name cannot.
  expect(parseLine("@a:x:y hi", [])).toEqual({ targets: [{ actor: "a", room: "x:y" }], text: "hi" });
});

test("@* reaches each agent-and-room pair shown, once each, and an agent in no room once with none", () => {
  const shown = [said("codex", ["ops", "lab"]), said("codex", ["ops"]), said("claude"), said("claude")];
  expect(broadcastTargets(shown)).toEqual([
    { actor: "codex", room: "ops" },
    { actor: "codex", room: "lab" },
    { actor: "claude", room: null },
  ]);
  expect(parseLine("@* stop", shown)).toEqual({ targets: broadcastTargets(shown), text: "stop" });
});

test("a line with no target goes to the view's To targets; one that names a target goes there instead", () => {
  const to = [
    { actor: "codex", room: "ops" },
    { actor: "claude", room: null },
  ];
  expect(parseLine("  rerun the suite ", [], to)).toEqual({ targets: to, text: "rerun the suite" });
  expect(parseLine("@codex:lab rerun", [], to)).toEqual({ targets: [{ actor: "codex", room: "lab" }], text: "rerun" });
  expect(parseLine("fix it", [], [])).toEqual({ problem: "Pick agents for this view, or start with a target: @agent message" });
});

test("a line that cannot be sent says why", () => {
  expect(parseLine("@codex", [])).toEqual({ problem: "Write the message after the target" });
  expect(parseLine("@* stop", [])).toEqual({ problem: "No agents are shown to send to" });
  expect(parseLine("@:ops hi", [])).toEqual({ problem: "An agent's name is missing in ':ops'" });
  expect(parseLine("@codex: hi", [])).toEqual({ problem: "A room is missing in 'codex:'" });
});

test("a receipt counts the sessions reached and names the targets no live session matched; any failure fails the send", () => {
  const codex = { actor: "codex", room: "ops" };
  const nobody = { actor: "nobody", room: null };
  const reached = (ids: number) => ({ status: "fulfilled" as const, value: { delivered: Array.from({ length: ids }, (_, k) => `id-${k}`) } });
  expect(receipt([codex], [reached(1)])).toBe("Sent to 1 session");
  expect(receipt([codex, nobody], [reached(2), reached(0)])).toBe("Sent to 2 sessions; no live session for @nobody");
  expect(() => receipt([codex, nobody], [reached(1), { status: "rejected", reason: new Error("timed out") }])).toThrow(
    "Not sent to @nobody (timed out). Retry sends it again; no session gets a message twice.",
  );
});
