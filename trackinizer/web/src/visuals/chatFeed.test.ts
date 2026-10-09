import { afterEach, expect, test, vi } from "vitest";
import { ChatFeed } from "./chatFeed";

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

test("the open conversation of a canvas is held, kept in storage, and null for a new chat", () => {
  const feed = new ChatFeed();
  expect(feed.openId("w1")).toBeNull();
  expect(feed.openId(null)).toBeNull();
  feed.setOpen("w1", "c1");
  expect(feed.openId("w1")).toBe("c1");
  expect(localStorage.getItem("trackinizer.v2.chat.w1")).toBe("c1");
  expect(new ChatFeed().openId("w1")).toBe("c1");
  feed.setOpen("w1", null);
  expect(feed.openId("w1")).toBeNull();
  expect(localStorage.getItem("trackinizer.v2.chat.w1")).toBeNull();
});

test("storage that is refused loses nothing while the page lives", () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("denied"); });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("denied"); });
  const feed = new ChatFeed();
  expect(feed.openId("w1")).toBeNull();
  feed.setOpen("w1", "c1");
  expect(feed.openId("w1")).toBe("c1");
});

test("a change of the open conversation is told to whoever listens", () => {
  const feed = new ChatFeed();
  const heard = vi.fn();
  const stop = feed.subscribe(heard);
  feed.setOpen("w1", "c1");
  expect(heard).toHaveBeenCalledOnce();
  expect(feed.snapshot().openVersion).toBe(1);
  stop();
  feed.setOpen("w1", "c2");
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
