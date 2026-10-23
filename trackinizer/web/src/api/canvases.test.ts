import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { stubFetch } from "./testing";
import { FakeEventSource } from "../live/testing";
import { answerCapture, chooseProvider, clearMarks, drawMark, type NewMark, openCanvasEvents, removeMark, setSessionSettings, type CanvasEventListener } from "./canvases";

beforeEach(() => FakeEventSource.reset());
afterEach(() => vi.unstubAllGlobals());

const state = { id: "w", revision: 4, visuals: [{ id: "v", type: "trax.browse" }] };

function recorder(): { heard: unknown[][]; listener: CanvasEventListener } {
  const heard: unknown[][] = [];
  return {
    heard,
    listener: {
      canvas: (...args) => heard.push(["canvas", ...args]),
      navigate: (...args) => heard.push(["navigate", ...args]),
      highlight: (...args) => heard.push(["highlight", ...args]),
      chat: (...args) => heard.push(["chat", ...args]),
      capture: (...args) => heard.push(["capture", ...args]),
      preferences: (...args) => heard.push(["preferences", ...args]),
      changed: (...args) => heard.push(["changed", ...args]),
      open: () => heard.push(["open"]),
      drop: () => heard.push(["drop"]),
      refuse: () => heard.push(["refuse"]),
    },
  };
}

const connect = (url: string) => new FakeEventSource(url);

test("session settings use the owned canvas route and campaign body", async () => {
  const sent = stubFetch(() => Response.json({ campaign: { hours: 2 } }));
  await setSessionSettings("canvas-id", { hours: 2 });
  expect(sent).toMatchObject([{
    method: "PUT",
    path: "/api/canvases/canvas-id/session-settings",
    body: { campaign: { hours: 2 } },
  }]);
});

test("a box, its removal and the clearing of all go to the operations route with no revision to match", async () => {
  const sent = stubFetch(() => Response.json(state));
  const mark: NewMark = { points: [{ x: 0.1, y: 0.2 }, { x: 0.3, y: 0.4 }], page: "#/graph" };
  await drawMark("w", mark);
  await removeMark("w", "box-id");
  await clearMarks("w", "#/graph");
  expect(sent.map((each) => [each.method, each.path, each.body])).toEqual([
    ["POST", "/api/canvases/w/operations", { revision: 0, operation: { kind: "mark", mark } }],
    ["POST", "/api/canvases/w/operations", { revision: 0, operation: { kind: "remove_mark", id: "box-id" } }],
    ["POST", "/api/canvases/w/operations", { revision: 0, operation: { kind: "clear_marks", page: "#/graph" } }],
  ]);
  expect(new Set(sent.map((each) => each.headers["idempotency-key"])).size).toBe(3);
});

test("the events stream opens on the canvas's route and passes on each kind of frame", () => {
  const { heard, listener } = recorder();
  openCanvasEvents("w", listener, { connect });
  const source = FakeEventSource.last;
  expect(new URL(source.url).pathname).toBe("/api/canvases/w/events");
  source.open();
  const frames = [
    { type: "canvas", state, t: 1 },
    { type: "canvas", state, t: 3 },
    { type: "navigate", route: "#/lookup/x", t: 2 },
    { type: "highlight", ids: ["a", "b"], t: 21 },
    { type: "highlight", ids: [], t: 22 },
    { type: "chat", hidden: true, t: 8 },
    { type: "chat", hidden: false, t: 9 },
    { type: "capture", request: "r1", t: 30 },
    { type: "preferences", preferences: { theme: "light", chat_side: "left", highlight_mentions: true, terminal_font_size: "13px" }, t: 10 },
    { type: "changed", id: "row", t: 7 },
  ];
  for (const frame of frames) source.send("", JSON.stringify(frame));
  expect(heard).toEqual([
    ["open"], ["canvas", state, 1], ["canvas", state, 3],
    ["navigate", "#/lookup/x", 2], ["highlight", ["a", "b"], 21], ["highlight", [], 22],
    ["chat", true, 8], ["chat", false, 9], ["capture", "r1", 30],
    ["preferences", { theme: "light", chat_side: "left", highlight_mentions: true, terminal_font_size: "13px" }, 10],
    ["changed", "row", 7],
  ]);
});

test("a frame of another shape is logged and skipped, and the stream goes on", () => {
  const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
  const { heard, listener } = recorder();
  openCanvasEvents("w", listener, { connect });
  const source = FakeEventSource.last;
  // The frames Chat once had are no frames now: a chat is a session, and its lines arrive as changed ids.
  const bad = ["not json", "null", { type: "canvas", state: {}, t: 1 }, { type: "canvas", state }, { type: "status", conversation_id: "c", text: "busy", t: 1 },
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
  const close = openCanvasEvents("w", listener, { connect });
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

test("a capture frame without a request id is ignored with a warning", () => {
  const { heard, listener } = recorder();
  const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
  openCanvasEvents("w", listener, { connect });
  FakeEventSource.last.send("", JSON.stringify({ type: "capture", t: 1 }));
  expect(heard).toEqual([]);
  expect(warn).toHaveBeenCalledOnce();
  warn.mockRestore();
});

test("an answer to a request for a picture is put under its request id", async () => {
  const sent = stubFetch(() => new Response(null, { status: 204 }));
  await answerCapture("w", "req-1", { error: "No canvas is on screen." });
  expect(sent.map((each) => [each.method, each.path, each.body])).toEqual([
    ["PUT", "/api/canvases/w/captures/req-1", { error: "No canvas is on screen." }],
  ]);
});

test("choosing a provider posts its operation at the canvas's revision, under its key", async () => {
  const sent = stubFetch(() => Response.json({ ...state, provider: "claude" }));
  const next = await chooseProvider("w", 4, "claude", "key-1");
  expect(next.provider).toBe("claude");
  expect(sent).toMatchObject([{
    method: "POST",
    path: "/api/canvases/w/operations",
    headers: { "idempotency-key": "key-1" },
    body: { revision: 4, operation: { kind: "provider", provider: "claude" } },
  }]);
});

test("a choice the server refuses as unavailable carries the server's reason", async () => {
  const reason = "Connect a machine and install claude on it";
  stubFetch(() => Response.json({ detail: reason, code: "provider_unavailable" }, { status: 409 }));
  await expect(chooseProvider("w", 4, "claude", "key-2")).rejects.toMatchObject({ status: 409, detail: reason, code: "provider_unavailable" });
});
