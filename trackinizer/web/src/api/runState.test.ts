import { expect, test } from "vitest";
import { runStateOf, takenOver, waitingOf } from "./runState";

test("the run's state is read as the server says it, and as none when it says none the web knows", () => {
  for (const state of ["idle", "running", "requires_action", "taken_over"] as const) expect(runStateOf({ state })).toBe(state);
  for (const silent of [{}, { state: "asleep" }, { state: 3 }, { state: null }, null, undefined, "idle"]) expect(runStateOf(silent)).toBeNull();
});

test("a chat waits on the person only in the state that says so, and no word is not a no", () => {
  expect(waitingOf({ state: "requires_action" })).toBe(true);
  for (const state of ["idle", "running", "taken_over"]) expect(waitingOf({ state })).toBe(false);
  for (const silent of [{}, { requires_action: true }, null, undefined]) expect(waitingOf(silent)).toBeNull();
});

test("a run is taken over only when the server says so", () => {
  expect(takenOver({ state: "taken_over" })).toBe(true);
  for (const other of [{ state: "running" }, {}, null, undefined]) expect(takenOver(other)).toBe(false);
});
