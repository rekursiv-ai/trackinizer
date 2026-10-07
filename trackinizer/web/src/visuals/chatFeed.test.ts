import { expect, test, vi } from "vitest";
import { ChatFeed } from "./chatFeed";

test("a message starts the conversation's status over, and a cleared status is kept as cleared", () => {
  const feed = new ChatFeed();
  const heard = vi.fn();
  feed.subscribe(heard);
  feed.setStatus("c", "working");
  expect(feed.snapshot().status).toEqual({ c: "working" });
  feed.setStatus("c", "");
  expect(feed.snapshot().status).toEqual({ c: "" });
  feed.messaged("c");
  expect(feed.snapshot().status).toEqual({});
  const calls = heard.mock.calls.length;
  feed.messaged("c");
  expect(heard).toHaveBeenCalledTimes(calls);
});

test("delivery only moves forward, and forgetting drops a conversation", () => {
  const feed = new ChatFeed();
  feed.drained("c", 4);
  feed.drained("c", 2);
  expect(feed.snapshot().delivered).toEqual({ c: 4 });
  feed.setStatus("c", "x");
  feed.forget("c");
  expect(feed.snapshot()).toMatchObject({ status: {}, delivered: {} });
});

test("each open is counted", () => {
  const feed = new ChatFeed();
  feed.opened();
  feed.opened();
  expect(feed.snapshot().opens).toBe(2);
});

test("the open conversation is held by the feed, restored from storage once, and survives refused storage", () => {
  localStorage.setItem("trackinizer.v2.chat.w", "c-stored");
  const feed = new ChatFeed();
  expect(feed.openId("w")).toBe("c-stored");
  expect(feed.openId(null)).toBeNull();
  feed.setOpen("w", "c2");
  expect(feed.openId("w")).toBe("c2");
  expect(localStorage.getItem("trackinizer.v2.chat.w")).toBe("c2");
  feed.setOpen("w", null);
  expect(localStorage.getItem("trackinizer.v2.chat.w")).toBeNull();

  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("denied"); });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("denied"); });
  const refused = new ChatFeed();
  expect(refused.openId("w2")).toBeNull();
  refused.setOpen("w2", "c3");
  expect(refused.openId("w2")).toBe("c3");
  vi.restoreAllMocks();
});
