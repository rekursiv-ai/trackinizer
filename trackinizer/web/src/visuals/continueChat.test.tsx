import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { ChatFeed, ChatFeedContext } from "./chatFeed";
import { useContinueInChat } from "./continueChat";
import { canvasActions } from "./testing";
import { WorkspaceActionsProvider } from "./workspaceActions";

afterEach(cleanup);

function Probe() {
  const continueIn = useContinueInChat();
  return <button type="button" disabled={!continueIn} onClick={() => continueIn?.("sess-1")}>go</button>;
}

function show(feed: ChatFeed | null, visualTypes: readonly string[] | null) {
  const actions = visualTypes === null ? null : canvasActions({ visualTypes: new Set(visualTypes) });
  render(<ChatFeedContext value={feed}><WorkspaceActionsProvider value={actions}><Probe /></WorkspaceActionsProvider></ChatFeedContext>);
  return actions;
}

test("a view in a canvas that offers Chat can continue a session there: it leaves the request and shows Chat", () => {
  const feed = new ChatFeed();
  const actions = show(feed, ["trax.chat"]);
  screen.getByRole("button", { name: "go" }).click();
  expect(feed.snapshot().request).toMatchObject({ sessionId: "sess-1" });
  expect(actions!.operate).toHaveBeenCalledWith({ kind: "show", visual_type: "trax.chat" });
});

test("it is offered nowhere else: outside a canvas, without the shell's feed, or where the catalog has no Chat", () => {
  for (const [feed, types] of [[new ChatFeed(), null], [null, ["trax.chat"]], [new ChatFeed(), ["trax.browse"]]] as const) {
    show(feed, types);
    expect(screen.getByRole("button", { name: "go" })).toHaveProperty("disabled", true);
    cleanup();
  }
  expect(vi.isMockFunction(canvasActions().operate)).toBe(true);
});
