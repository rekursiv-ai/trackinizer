import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { FakeEventSource } from "../live/testing";
import { stubFetch } from "./testing";
import { openWorkspaceEvents, sendWorkspaceMessage, type WorkspaceEventListener } from "./workspaces";

beforeEach(() => FakeEventSource.reset());
afterEach(() => vi.unstubAllGlobals());

const state = { id: "w", revision: 4, visuals: [{ id: "v", type: "trax.browse" }] };
const message = { id: "m1", seq: 1, role: "user", author: "a", text: "hi", created: "2026-10-03T10:00:00Z" };

function recorder(): { heard: unknown[][]; listener: WorkspaceEventListener } {
  const heard: unknown[][] = [];
  return {
    heard,
    listener: {
      workspace: (...args) => heard.push(["workspace", ...args]),
      navigate: (...args) => heard.push(["navigate", ...args]),
      highlight: (...args) => heard.push(["highlight", ...args]),
      message: (...args) => heard.push(["message", ...args]),
      status: (...args) => heard.push(["status", ...args]),
      delivered: (...args) => heard.push(["delivered", ...args]),
      deleted: (...args) => heard.push(["deleted", ...args]),
      changed: (...args) => heard.push(["changed", ...args]),
      open: () => heard.push(["open"]),
      drop: () => heard.push(["drop"]),
      refuse: () => heard.push(["refuse"]),
    },
  };
}

const connect = (url: string) => new FakeEventSource(url);

test("a message goes under its key with its conversation, and the receipt carries the stored message", async () => {
  const receipt = { session_id: "s", conversation_id: "c1", message };
  const sent = stubFetch(() => Response.json(receipt));
  await expect(sendWorkspaceMessage("w/1", { text: "hi", chatInstanceId: "chat", expectedRecordId: "rec", conversationId: null }, "key-1"))
    .resolves.toEqual(receipt);
  expect(sent).toMatchObject([{ method: "POST", path: "/api/workspaces/w%2F1/messages", headers: { "idempotency-key": "key-1" },
    body: { text: "hi", chat_instance_id: "chat", expected_record_id: "rec", conversation_id: null } }]);
});

test("the events stream opens on the workspace's route and passes on each kind of frame", () => {
  const { heard, listener } = recorder();
  openWorkspaceEvents("w", listener, { connect });
  const source = FakeEventSource.last;
  expect(new URL(source.url).pathname).toBe("/api/workspaces/w/events");
  source.open();
  const frames = [
    { type: "workspace", state, t: 1 },
    { type: "navigate", route: "#/lookup/x", t: 2 },
    { type: "highlight", ids: ["a", "b"], t: 21 },
    { type: "highlight", ids: [], t: 22 },
    { type: "message", conversation_id: "c", message, t: 3 },
    { type: "status", conversation_id: "c", text: "busy", t: 4 },
    { type: "status", conversation_id: "c", text: "", t: 5 },
    { type: "delivered", conversation_id: "c", seq: 7, t: 6 },
    { type: "changed", id: "row", t: 7 },
    { type: "deleted", conversation_id: "c", t: 8 },
  ];
  for (const frame of frames) source.send("", JSON.stringify(frame));
  expect(heard).toEqual([
    ["open"], ["workspace", state, 1], ["navigate", "#/lookup/x", 2], ["highlight", ["a", "b"], 21], ["highlight", [], 22],
    ["message", "c", message, 3], ["status", "c", "busy", 4],
    ["status", "c", "", 5], ["delivered", "c", 7, 6], ["changed", "row", 7], ["deleted", "c", 8],
  ]);
});

test("a frame of another shape is logged and skipped, and the stream goes on", () => {
  const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
  const { heard, listener } = recorder();
  openWorkspaceEvents("w", listener, { connect });
  const source = FakeEventSource.last;
  const bad = ["not json", "null", { type: "workspace", state: {}, t: 1 }, { type: "workspace", state }, { type: "status", conversation_id: "c", t: 1 },
    { type: "message", conversation_id: "c", message: {}, t: 1 }, { type: "other", t: 1 }, { type: "navigate", t: 1 },
    { type: "delivered", conversation_id: "c", t: 1 }, { type: "delivered", conversation_id: "c", seq: "7", t: 1 }, { type: "changed", t: 1 }, { type: "deleted", t: 1 },
    { type: "highlight", t: 1 }, { type: "highlight", ids: "a", t: 1 }, { type: "highlight", ids: [1], t: 1 }];
  for (const data of bad) source.send("", typeof data === "string" ? data : JSON.stringify(data));
  source.send("", JSON.stringify({ type: "delivered", conversation_id: "c", seq: 9, t: 9 }));
  expect(heard).toEqual([["delivered", "c", 9, 9]]);
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
