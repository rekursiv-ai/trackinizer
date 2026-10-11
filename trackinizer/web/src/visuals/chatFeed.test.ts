import { expect, test, vi } from "vitest";
import { ChatFeed } from "./chatFeed";

test("a request is told to whoever listens, until they stop", () => {
  const feed = new ChatFeed();
  const heard = vi.fn();
  const stop = feed.subscribe(heard);
  feed.continueIn("s1");
  expect(heard).toHaveBeenCalledOnce();
  stop();
  feed.continueIn("s2");
  expect(heard).toHaveBeenCalledOnce();
});

test("a request to continue a session waits for Chat, and only the newest is taken by number", () => {
  const feed = new ChatFeed();
  feed.continueIn("s1");
  const first = feed.snapshot().request!;
  expect(first).toMatchObject({ sessionId: "s1" });
  feed.continueIn("s2");
  const second = feed.snapshot().request!;
  expect(second.n).toBeGreaterThan(first.n);
  feed.taken(first.n);
  expect(feed.snapshot().request).toBe(second);
  feed.taken(second.n);
  expect(feed.snapshot().request).toBeNull();
});
