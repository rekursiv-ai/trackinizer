import { afterEach, expect, onTestFinished, test, vi } from "vitest";
import { keepRefused } from "./details";
import { installDebug, logRenderError } from "./install";
import { log, recentEvents } from "./log";

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
  history.replaceState(null, "", "/");
});

function install(): void {
  onTestFinished(installDebug());
}

/** Whether an info event reaches the console, as it does only while debug is on. */
function debugOn(): boolean {
  const info = vi.spyOn(console, "info").mockImplementation(() => {});
  log("info", "probe");
  const seen = info.mock.calls.length > 0;
  info.mockRestore();
  return seen;
}

test("debug is on with ?debug=1 or the localStorage switch, and off otherwise", () => {
  const on = (setup: () => void) => {
    setup();
    const uninstall = installDebug();
    const seen = debugOn();
    uninstall();
    localStorage.clear();
    history.replaceState(null, "", "/");
    return seen;
  };
  expect(on(() => {})).toBe(false);
  expect(on(() => history.replaceState(null, "", "/app/?debug=1#/list/Issue"))).toBe(true);
  expect(on(() => localStorage.setItem("trackinizer.v2.debug", "1"))).toBe(true);
  expect(on(() => localStorage.setItem("trackinizer.v2.debug", "0"))).toBe(false);
});

test("an uncaught error and an unhandled rejection are logged as errors", () => {
  const error = vi.spyOn(console, "error").mockImplementation(() => {});
  install();
  dispatchEvent(new ErrorEvent("error", { error: new TypeError("x is undefined"), message: "x is undefined" }));
  dispatchEvent(Object.assign(new Event("unhandledrejection"), { reason: new RangeError("too deep") }));
  expect(recentEvents().slice(-2).map(({ level, event, fields }) => [level, event, fields.error, fields.message])).toEqual([
    ["error", "uncaught", "TypeError", "x is undefined"],
    ["error", "unhandled_rejection", "RangeError", "too deep"],
  ]);
  expect(error).toHaveBeenCalledTimes(2);
});

test("the devtools console can dump the ring and the details", () => {
  install();
  log("warn", "dumped", { n: 7 });
  const dump = (window as unknown as { trackinizer: { events: () => unknown[]; details: () => string } }).trackinizer;
  expect(dump.events().at(-1)).toMatchObject({ event: "dumped", fields: { n: 7 } });
  expect(dump.details()).toContain("warn dumped n=7");
  // Details the clipboard refused are the ones the console gives.
  keepRefused("Trackinizer web app: Not saved.");
  expect(dump.details()).toBe("Trackinizer web app: Not saved.");
});

test("a render crash is logged once, with the component stack", () => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  logRenderError(new TypeError("kinds.find is not a function"), { componentStack: "\n    at RouterProvider" });
  expect(recentEvents().at(-1)).toMatchObject({
    level: "error",
    event: "render.crash",
    fields: { error: "TypeError", message: "kinds.find is not a function", component_stack: "\n    at RouterProvider" },
  });
});
