import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { currentChatColumn, setChatHidden } from "../visuals/chatColumn";
import { ChatFeed } from "../visuals/chatFeed";
import { detail, renderDetail, row, serveDetails, uuid } from "./testing";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const CHAT = { labels: ["science-chat", "poster:ada@example.com"], cli_session_id: "chat:3d0e9f1a-1b2c-4d5e-8f60-7a8b9c0d1e2f" };

test("a science chat's session page offers Continue in Chat, which opens it in Chat and shows a hidden Chat", async () => {
  serveDetails([detail(row("AgentSession", 7, CHAT))]);
  const feed = new ChatFeed();
  setChatHidden(true);
  renderDetail({ kind: "AgentSession", seq: 7 }, undefined, { chat: feed });
  fireEvent.click(await screen.findByRole("button", { name: "Continue in Chat" }));
  expect(feed.snapshot().request).toMatchObject({ sessionId: uuid(7) });
  expect(currentChatColumn().hidden).toBe(false);
});

const feed = new ChatFeed();
const NOT_OFFERED = [
  ["another session", row("AgentSession", 8, { labels: ["slack-thread:x"], cli_session_id: "slack:C1:1" }), { chat: feed }],
  ["a session without the chat id", row("AgentSession", 9, { labels: ["science-chat"], cli_session_id: "slack:C1:1" }), { chat: feed }],
  ["a session without the label", row("AgentSession", 10, { labels: ["slack-thread:x"], cli_session_id: CHAT.cli_session_id }), { chat: feed }],
  ["another kind", row("Issue", 11, CHAT), { chat: feed }],
  ["a page outside the shell's canvas, which has no Chat feed", row("AgentSession", 12, CHAT), {}],
] as const;

test.each(NOT_OFFERED)("no other row offers it: %s", async (_case, self, options) => {
  serveDetails([detail(self)]);
  renderDetail({ kind: self.kind, seq: self.seq }, undefined, { ...options });
  await screen.findByRole("heading", { name: self.title });
  expect(screen.queryByRole("button", { name: "Continue in Chat" })).toBeNull();
});
