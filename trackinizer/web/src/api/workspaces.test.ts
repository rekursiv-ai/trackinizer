import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { stubFetch } from "./testing";
import { FakeEventSource } from "../live/testing";
import { openWorkspaceEvents, setSessionSettings, type WorkspaceEventListener } from "./workspaces";

beforeEach(() => FakeEventSource.reset());
afterEach(() => vi.unstubAllGlobals());

const state = { id: "w", revision: 4, visuals: [{ id: "v", type: "trax.browse" }] };

function recorder(): { heard: unknown[][]; listener: WorkspaceEventListener } {
  const heard: unknown[][] = [];
  return {
    heard,
    listener: {
      workspace: (...args) => heard.push(["workspace", ...args]),
      navigate: (...args) => heard.push(["navigate", ...args]),
      highlight: (...args) => heard.push(["highlight", ...args]),
      chat: (...args) => heard.push(["chat", ...args]),
      preferences: (...args) => heard.push(["preferences", ...args]),
      changed: (...args) => heard.push(["changed", ...args]),
      open: () => heard.push(["open"]),
      drop: () => heard.push(["drop"]),
      refuse: () => heard.push(["refuse"]),
    },
  };
}

const connect = (url: string) => new FakeEventSource(url);

test("session settings use the owned workspace route and campaign body", async () => {
  const sent = stubFetch(() => Response.json({ campaign: { hours: 2 } }));
  await setSessionSettings("workspace-id", { hours: 2 });
  expect(sent).toMatchObject([{
    method: "PUT",
    path: "/api/workspaces/workspace-id/session-settings",
    body: { campaign: { hours: 2 } },
  }]);
});

test("the events stream opens on the workspace's route and passes on each kind of frame", () => {
  const { heard, listener } = recorder();
  openWorkspaceEvents("w", listener, { connect });
  const source = FakeEventSource.last;
  expect(new URL(source.url).pathname).toBe("/api/workspaces/w/events");
  source.open();
  const frames = [
    { type: "workspace", state, t: 1 },
    { type: "workspace", state, t: 3 },
    { type: "navigate", route: "#/lookup/x", t: 2 },
    { type: "highlight", ids: ["a", "b"], t: 21 },
    { type: "highlight", ids: [], t: 22 },
    { type: "chat", hidden: true, t: 8 },
    { type: "chat", hidden: false, t: 9 },
    { type: "preferences", preferences: { theme: "light", chat_style: "default", chat_side: "left", highlight_mentions: true, terminal_font_size: "13px" }, t: 10 },
    { type: "changed", id: "row", t: 7 },
  ];
  for (const frame of frames) source.send("", JSON.stringify(frame));
  expect(heard).toEqual([
    ["open"], ["workspace", state, 1], ["workspace", state, 3],
    ["navigate", "#/lookup/x", 2], ["highlight", ["a", "b"], 21], ["highlight", [], 22],
    ["chat", true, 8], ["chat", false, 9],
    ["preferences", { theme: "light", chat_style: "default", chat_side: "left", highlight_mentions: true, terminal_font_size: "13px" }, 10],
    ["changed", "row", 7],
  ]);
});

test("a frame of another shape is logged and skipped, and the stream goes on", () => {
  const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
  const { heard, listener } = recorder();
  openWorkspaceEvents("w", listener, { connect });
  const source = FakeEventSource.last;
  // The frames Chat once had are no frames now: a chat is a session, and its lines arrive as changed ids.
  const bad = ["not json", "null", { type: "workspace", state: {}, t: 1 }, { type: "workspace", state }, { type: "status", conversation_id: "c", text: "busy", t: 1 },
    { type: "message", conversation_id: "c", message: {}, t: 1 }, { type: "other", t: 1 }, { type: "navigate", t: 1 }, { type: "preferences", t: 1 }, { type: "preferences", preferences: null, t: 1 },
    { type: "delivered", conversation_id: "c", seq: 7, t: 1 }, { type: "deleted", conversation_id: "c", t: 1 }, { type: "changed", t: 1 },
    { type: "highlight", t: 1 }, { type: "highlight", ids: "a", t: 1 }, { type: "highlight", ids: [1], t: 1 },
    { type: "chat", t: 1 }, { type: "chat", hidden: "yes", t: 1 }, { type: "chat", hidden: 1, t: 1 }];
  for (const data of bad) source.send("", typeof data === "string" ? data : JSON.stringify(data));
  source.send("", JSON.stringify({ type: "changed", id: "row", t: 9 }));
  expect(heard).toEqual([["changed", "row", 9]]);
  expect(warn).toHaveBeenCalledTimes(bad.length);
  warn.mockRestore();
});

test("a dropped stream reconnects by itself, a refused one tries again, and each open and drop is reported", () => {
  vi.useFakeTimers();
  const { heard, listener } = recorder();
  const close = openWorkspaceEvents("w", listener, { connect });
  const source = FakeEventSource.last;
  source.open();
  source.drop();
  source.open();
  expect(heard).toEqual([["open"], ["drop"], ["open"]]);
  expect(FakeEventSource.made).toHaveLength(1);
  source.refuse();
  expect(heard.slice(-2)).toEqual([["drop"], ["refuse"]]);
  vi.advanceTimersByTime(1_000);
  expect(FakeEventSource.made).toHaveLength(2);
  close();
  expect(FakeEventSource.last.readyState).toBe(2);
  vi.useRealTimers();
});
