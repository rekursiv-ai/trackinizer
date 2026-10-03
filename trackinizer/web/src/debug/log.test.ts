import { afterEach, expect, test, vi } from "vitest";
import { formatEvent, log, recentEvents, setDebug } from "./log";

afterEach(() => {
  setDebug(false);
  vi.restoreAllMocks();
});

/** Spy on the four console methods the logger writes to, silenced. */
function spyConsole() {
  return Object.fromEntries(
    (["debug", "info", "warn", "error"] as const).map((level) => [level, vi.spyOn(console, level).mockImplementation(() => {})]),
  );
}

test("warnings and errors always reach the console; info and debug only while debug is on", () => {
  const spies = spyConsole();
  const levels = ["debug", "info", "warn", "error"] as const;
  for (const level of levels) log(level, "probe", { level });
  expect(levels.map((level) => spies[level]!.mock.calls.length)).toEqual([0, 0, 1, 1]);
  expect(spies.warn!.mock.calls[0]).toEqual(["trackinizer probe level=warn"]);
  setDebug(true);
  for (const level of levels) log(level, "probe", { level });
  expect(levels.map((level) => spies[level]!.mock.calls.length)).toEqual([1, 1, 2, 2]);
});

test("every event reaches the ring whatever its level, and the ring keeps the latest 200", () => {
  spyConsole();
  for (let n = 0; n < 250; n++) log(n % 2 ? "debug" : "warn", "tick", { n });
  const events = recentEvents();
  expect(events).toHaveLength(200);
  expect(events.map(({ fields }) => fields.n)).toEqual(Array.from({ length: 200 }, (_, k) => k + 50));
  expect(events[0]).toEqual({ at: expect.stringMatching(/^\d{4}-\d\d-\d\dT.*Z$/), level: "warn", event: "tick", fields: { n: 50 } });
});

test("an event is one logfmt line: quoted where a value has spaces, quotes or is empty, unset fields left out", () => {
  expect(
    formatEvent("request.failed", { method: "GET", status: 0, detail: 'no "answer"', empty: "", code: null, attempt: undefined, ok: false }),
  ).toBe('request.failed method=GET status=0 detail="no \\"answer\\"" empty="" ok=false');
});
