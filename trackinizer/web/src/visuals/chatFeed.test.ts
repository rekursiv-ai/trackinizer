import { afterEach, expect, test, vi } from "vitest";
import { ChatFeed } from "./chatFeed";

afterEach(() => sessionStorage.clear());

test("pending posts survive remounts and reloads and stay with their sender", () => {
  const feed = new ChatFeed();
  const line = { key: "k1", text: "continue", workspaceId: "w1", conversationId: "c1", baseline: 0, submittedAt: 1_000 };
  feed.updatePending("ada", () => [line]);
  feed.updatePending("bob", () => [{ ...line, key: "k2", text: "bob's post" }]);

  const restored = new ChatFeed();
  expect(restored.snapshot().pending.filter((post) => post.sender === "ada")).toEqual([{ ...line, sender: "ada" }]);
  feed.updatePending("ada", () => []);
  expect(new ChatFeed().snapshot().pending.map((post) => post.sender)).toEqual(["bob"]);
});

test("malformed saved posts are ignored", () => {
  sessionStorage.setItem("trackinizer.chat.pending", JSON.stringify([{ sender: "ada", key: "k", baseline: "zero" }]));
  expect(new ChatFeed().snapshot().pending).toEqual([]);
  sessionStorage.setItem("trackinizer.chat.pending", "broken JSON");
  expect(new ChatFeed().snapshot().pending).toEqual([]);
});

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
