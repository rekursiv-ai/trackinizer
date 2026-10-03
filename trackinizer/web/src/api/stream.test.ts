import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { FakeEventSource } from "../live/testing";
import { openStream, type StreamListener } from "./stream";

beforeEach(() => {
  FakeEventSource.reset();
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

/** A listener that records what it hears, in order. */
function recorder(): { heard: string[]; listener: StreamListener } {
  const heard: string[] = [];
  return {
    heard,
    listener: {
      change: (id) => heard.push(id),
      open: () => heard.push("open"),
      drop: () => heard.push("drop"),
      refuse: () => heard.push("refuse"),
    },
  };
}

const connect = (url: string) => new FakeEventSource(url);

test("the stream listens on /api/web/subscribe and passes on each frame's id", () => {
  const { heard, listener } = recorder();
  openStream(listener, { connect });
  const source = FakeEventSource.last;
  expect(new URL(source.url).pathname).toBe("/api/web/subscribe");
  source.open();
  source.send("a");
  source.send("a");
  source.send("b");
  expect(heard).toEqual(["open", "a", "a", "b"]);
});

test("a frame without a string id is logged and skipped, and the stream goes on", () => {
  const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
  const { heard, listener } = recorder();
  openStream(listener, { connect });
  const source = FakeEventSource.last;
  source.send("", "not json");
  source.send("", JSON.stringify({ id: 7 }));
  source.send("", JSON.stringify(null));
  source.send("c");
  expect(heard).toEqual(["c"]);
  expect(warn).toHaveBeenCalledTimes(3);
});

test("a dropped connection is left to reconnect by itself, never closed", () => {
  const { heard, listener } = recorder();
  openStream(listener, { connect });
  const source = FakeEventSource.last;
  source.open();
  source.drop();
  source.drop();
  source.open();
  expect(heard).toEqual(["open", "drop", "drop", "open"]);
  expect(source.readyState).toBe(1);
  expect(FakeEventSource.made).toHaveLength(1);
});

test("a refused stream opens again after 1 s, 3 s, 10 s, then every 30 s, until it connects", () => {
  const { heard, listener } = recorder();
  openStream(listener, { connect });
  const waits: number[] = [];
  for (let attempt = 0; attempt < 5; attempt++) {
    const made = FakeEventSource.made.length;
    FakeEventSource.last.refuse();
    const started = Date.now();
    while (FakeEventSource.made.length === made) vi.advanceTimersByTime(500);
    waits.push(Date.now() - started);
  }
  expect(waits).toEqual([1_000, 3_000, 10_000, 30_000, 30_000]);
  expect(heard.filter((event) => event === "refuse")).toHaveLength(5);
  // Connecting resets the wait.
  FakeEventSource.last.open();
  FakeEventSource.last.refuse();
  vi.advanceTimersByTime(1_000);
  expect(FakeEventSource.made).toHaveLength(7);
});

test("a refused connection that errs again is still one refusal and one new try", () => {
  const { heard, listener } = recorder();
  openStream(listener, { connect });
  const source = FakeEventSource.last;
  source.refuse();
  source.refuse();
  vi.advanceTimersByTime(1_000);
  expect(FakeEventSource.made).toHaveLength(2);
  expect(heard).toEqual(["drop", "refuse"]);
  // The next wait is the second one, 3 s, not a third.
  FakeEventSource.last.refuse();
  vi.advanceTimersByTime(2_999);
  expect(FakeEventSource.made).toHaveLength(2);
  vi.advanceTimersByTime(1);
  expect(FakeEventSource.made).toHaveLength(3);
});

test("closing stops the stream and any reopening", () => {
  const { heard, listener } = recorder();
  const close = openStream(listener, { connect });
  const source = FakeEventSource.last;
  source.refuse();
  close();
  vi.advanceTimersByTime(60_000);
  expect(FakeEventSource.made).toHaveLength(1);
  expect(source.readyState).toBe(2);
  source.drop();
  expect(heard).toEqual(["drop", "refuse"]);
});
