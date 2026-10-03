import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { attachStream, openEarlyStream, type OpenListener } from "./earlyStream";
import { FakeEventSource } from "./testing";

beforeEach(() => {
  FakeEventSource.reset();
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

/** A listener that records what it hears, in order, an open with the time it gives. */
function recorder(): { heard: string[]; listener: OpenListener } {
  const heard: string[] = [];
  return {
    heard,
    listener: {
      change: (id) => heard.push(id),
      open: (at) => {
        heard.push(at === undefined ? "open" : `open at ${at}`);
      },
      drop: () => heard.push("drop"),
      refuse: () => heard.push("refuse"),
    },
  };
}

test("a stream opened early tells its listener when it opened, then passes everything on", () => {
  openEarlyStream();
  const source = FakeEventSource.last;
  vi.setSystemTime(1_000);
  source.open();
  // Before any view mounted: no view's read can predate it.
  source.send("early");
  vi.setSystemTime(1_200);
  const { heard, listener } = recorder();
  attachStream(listener);
  source.send("a");
  source.drop();
  source.open();
  expect(heard).toEqual(["open at 1000", "a", "drop", "open"]);
  expect(FakeEventSource.made).toHaveLength(1);
});

test("an early stream that dropped before its listener came says nothing of its open", () => {
  openEarlyStream();
  FakeEventSource.last.open();
  FakeEventSource.last.drop();
  const { heard, listener } = recorder();
  attachStream(listener);
  expect(heard).toEqual([]);
});

test("the early stream goes to the first listener; without one, attaching opens a stream of its own", () => {
  openEarlyStream();
  const first = recorder();
  const close = attachStream(first.listener);
  const second = recorder();
  attachStream(second.listener);
  expect(FakeEventSource.made).toHaveLength(2);
  FakeEventSource.made[1]!.open();
  expect(second.heard).toEqual(["open"]);
  close();
  expect(FakeEventSource.made[0]!.readyState).toBe(2);
});
