import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { currentChatColumn, setChatHidden } from "./chatColumnState";
import { ChatFeed, ChatFeedContext } from "./chatFeed";
import { useContinueInChat } from "./continueChat";

afterEach(cleanup);

function Probe() {
  const continueIn = useContinueInChat();
  return <button type="button" disabled={!continueIn} onClick={() => continueIn?.("sess-1")}>go</button>;
}

function show(feed: ChatFeed | null) {
  render(<ChatFeedContext value={feed}><Probe /></ChatFeedContext>);
}

test("a view under the shell's feed can continue a session in Chat: it leaves the request and shows a hidden Chat", () => {
  const feed = new ChatFeed();
  setChatHidden(true);
  show(feed);
  screen.getByRole("button", { name: "go" }).click();
  expect(feed.snapshot().request).toMatchObject({ sessionId: "sess-1" });
  expect(currentChatColumn().hidden).toBe(false);
});

test("it is offered nowhere else: a view without the shell's feed, which a user without the canvas has", () => {
  show(null);
  expect(screen.getByRole("button", { name: "go" })).toHaveProperty("disabled", true);
});
