import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { canvasActions } from "../visuals/testing";
import { ChatFeed } from "../visuals/chatFeed";
import { detail, renderDetail, row, serveDetails, uuid } from "./testing";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const CHAT = { labels: ["science-chat", "poster:ada@example.com"], cli_session_id: "chat:3d0e9f1a-1b2c-4d5e-8f60-7a8b9c0d1e2f" };
const chatCanvas = () => canvasActions({ visualTypes: new Set(["trax.chat"]) });

test("a science chat's session page offers Continue in Chat, which opens it in Chat and shows Chat", async () => {
  serveDetails([detail(row("AgentSession", 7, CHAT))]);
  const workspace = chatCanvas();
  const feed = new ChatFeed();
  renderDetail({ kind: "AgentSession", seq: 7 }, undefined, { workspace, chat: feed });
  fireEvent.click(await screen.findByRole("button", { name: "Continue in Chat" }));
  expect(feed.snapshot().request).toMatchObject({ sessionId: uuid(7) });
  expect(workspace.operate).toHaveBeenCalledWith({ kind: "show", visual_type: "trax.chat" });
});

test("no other row offers it: not another session, another kind, a session without the label, or one outside a canvas that offers Chat", async () => {
  const feed = new ChatFeed();
  const cases = [
    [row("AgentSession", 8, { labels: ["slack-thread:x"], cli_session_id: "slack:C1:1" }), { workspace: chatCanvas(), chat: feed }],
    [row("AgentSession", 9, { labels: ["science-chat"], cli_session_id: "slack:C1:1" }), { workspace: chatCanvas(), chat: feed }],
    [row("AgentSession", 10, { labels: ["slack-thread:x"], cli_session_id: CHAT.cli_session_id }), { workspace: chatCanvas(), chat: feed }],
    [row("Issue", 11, { labels: ["science-chat"] }), { workspace: chatCanvas(), chat: feed }],
    [row("AgentSession", 12, CHAT), { chat: feed }],
    [row("AgentSession", 13, CHAT), { workspace: canvasActions(), chat: feed }],
    [row("AgentSession", 14, CHAT), { workspace: chatCanvas() }],
  ] as const;
  for (const [self, options] of cases) {
    serveDetails([detail(self)]);
    renderDetail({ kind: self.kind, seq: self.seq }, undefined, { ...options });
    await screen.findByRole("heading", { name: self.title });
    expect(screen.queryByRole("button", { name: "Continue in Chat" })).toBeNull();
    cleanup();
  }
});
