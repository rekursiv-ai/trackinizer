import { QueryClient } from "@tanstack/react-query";
import { afterEach, expect, test, vi } from "vitest";
import { clearChat, hideCanvas } from "../api/canvasManagement";
import { getCanvas } from "../api/canvases";
import type { CanvasState } from "../api/canvases";
import { clearChat as clearChatAction, CANVASES_KEY, hideCanvas as hideCanvasAction, openCanvas } from "./actions";
import { currentCanvas, setCanvas } from "./store";

vi.mock("../api/canvasManagement", () => ({ clearChat: vi.fn(), hideCanvas: vi.fn() }));
vi.mock("../api/canvases", () => ({ getCanvas: vi.fn() }));

afterEach(() => {
  vi.clearAllMocks();
  setCanvas(null);
});

const state = (id: string): CanvasState => ({ id, name: id, conversation_id: null, page: null, revision: 1, focused_instance: null, visuals: [] });

test("a cached chat opens before its network refresh finishes", async () => {
  const client = new QueryClient();
  setCanvas("w1");
  client.setQueryData(["canvas", "w2"], state("w2"));
  let finish!: (value: CanvasState) => void;
  vi.mocked(getCanvas).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
  const opening = openCanvas(client, "w2");
  expect(currentCanvas().id).toBe("w2");
  finish({ ...state("w2"), revision: 2 });
  await opening;
  expect(client.getQueryData(["canvas", "w2"])).toMatchObject({ revision: 2 });
});

test("a slow earlier selection cannot replace the chat clicked last", async () => {
  const client = new QueryClient();
  setCanvas("w1");
  let first!: (value: CanvasState) => void;
  let second!: (value: CanvasState) => void;
  vi.mocked(getCanvas)
    .mockReturnValueOnce(new Promise((resolve) => { first = resolve; }))
    .mockReturnValueOnce(new Promise((resolve) => { second = resolve; }));
  const earlier = openCanvas(client, "w2");
  const latest = openCanvas(client, "w3");
  second(state("w3"));
  await latest;
  first(state("w2"));
  await earlier;
  expect(currentCanvas().id).toBe("w3");
});

test("a canvas refresh keeps navigation made after the chat was selected", async () => {
  const client = new QueryClient();
  setCanvas("w1");
  client.setQueryData(["canvas", "w2"], { ...state("w2"), page: "#/graph" });
  let finish!: (value: CanvasState) => void;
  vi.mocked(getCanvas).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
  const opening = openCanvas(client, "w2");
  window.location.hash = "#/list/Issue";
  finish({ ...state("w2"), page: "#/graph", revision: 2 });
  await opening;
  expect(window.location.hash).toBe("#/list/Issue");
});

test("hiding the current canvas opens its visible neighbour and marks it hidden", async () => {
  const client = new QueryClient();
  setCanvas("w1");
  client.setQueryData(CANVASES_KEY, [
    { id: "w1", name: "alpha", created_at: "2026-10-01T00:00:00Z", conversation_id: "c1" },
    { id: "w2", name: "beta", created_at: "2026-10-02T00:00:00Z", conversation_id: null },
  ]);
  vi.mocked(hideCanvas).mockResolvedValue({ ...state("w1"), hidden: true });
  vi.mocked(getCanvas).mockResolvedValue(state("w2"));
  await hideCanvasAction(client, "w1");
  expect(currentCanvas().id).toBe("w2");
  expect(client.getQueryData<{ id: string; hidden?: boolean }[]>(CANVASES_KEY)?.[0]).toMatchObject({ id: "w1", hidden: true });
});

test("clear chat opens and caches the canvas returned by the server", async () => {
  const client = new QueryClient();
  const next = { ...state("w2"), conversation_id: null };
  setCanvas("w1");
  vi.mocked(clearChat).mockResolvedValue(next);
  await clearChatAction(client, "w1", "key-1");
  expect(currentCanvas().id).toBe("w2");
  expect(client.getQueryData(["canvas", "w2"])).toEqual(next);
});

test.each(["hide", "clear"] as const)("a delayed %s result keeps a newer canvas selection", async (action) => {
  const client = new QueryClient();
  setCanvas("w1");
  client.setQueryData(CANVASES_KEY, [
    { id: "w1", created_at: "1" }, { id: "w2", created_at: "2" },
  ]);
  let finish!: (value: CanvasState) => void;
  const reply = new Promise<CanvasState>((resolve) => { finish = resolve; });
  vi.mocked(getCanvas).mockResolvedValue(state("w2"));
  vi.mocked(hideCanvas).mockReturnValue(reply);
  vi.mocked(clearChat).mockReturnValue(reply);
  const changing = action === "hide" ? hideCanvasAction(client, "w1") : clearChatAction(client, "w1", "key");
  setCanvas("w3");
  finish(state(action === "hide" ? "w1" : "new-chat"));
  await changing;
  expect(currentCanvas().id).toBe("w3");
});

test("selecting the current chat cancels delayed navigation away from it", async () => {
  const client = new QueryClient();
  setCanvas("w1");
  let finish!: (value: CanvasState) => void;
  vi.mocked(clearChat).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
  const clearing = clearChatAction(client, "w1", "key");
  await openCanvas(client, "w1");
  finish(state("new-chat"));
  await clearing;
  expect(currentCanvas().id).toBe("w1");
});
