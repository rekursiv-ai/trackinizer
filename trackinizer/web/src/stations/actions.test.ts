import { QueryClient } from "@tanstack/react-query";
import { afterEach, expect, test, vi } from "vitest";
import { clearChat, hideStation } from "../api/stations";
import { getWorkspace } from "../api/workspaces";
import type { WorkspaceState } from "../api/workspaces";
import { clearChat as clearChatAction, STATIONS_KEY, hideStation as hideStationAction, openStation } from "./actions";
import { currentStation, setStation } from "./store";

vi.mock("../api/stations", () => ({ clearChat: vi.fn(), hideStation: vi.fn() }));
vi.mock("../api/workspaces", () => ({ getWorkspace: vi.fn() }));

afterEach(() => {
  vi.clearAllMocks();
  setStation(null);
});

const state = (id: string): WorkspaceState => ({ id, name: id, conversation_id: null, page: null, revision: 1, focused_instance: null, visuals: [] });

test("a cached chat opens before its network refresh finishes", async () => {
  const client = new QueryClient();
  setStation("w1");
  client.setQueryData(["workspace", "w2"], state("w2"));
  let finish!: (value: WorkspaceState) => void;
  vi.mocked(getWorkspace).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
  const opening = openStation(client, "w2");
  expect(currentStation().id).toBe("w2");
  finish({ ...state("w2"), revision: 2 });
  await opening;
  expect(client.getQueryData(["workspace", "w2"])).toMatchObject({ revision: 2 });
});

test("a slow earlier selection cannot replace the chat clicked last", async () => {
  const client = new QueryClient();
  setStation("w1");
  let first!: (value: WorkspaceState) => void;
  let second!: (value: WorkspaceState) => void;
  vi.mocked(getWorkspace)
    .mockReturnValueOnce(new Promise((resolve) => { first = resolve; }))
    .mockReturnValueOnce(new Promise((resolve) => { second = resolve; }));
  const earlier = openStation(client, "w2");
  const latest = openStation(client, "w3");
  second(state("w3"));
  await latest;
  first(state("w2"));
  await earlier;
  expect(currentStation().id).toBe("w3");
});

test("a workspace refresh keeps navigation made after the chat was selected", async () => {
  const client = new QueryClient();
  setStation("w1");
  client.setQueryData(["workspace", "w2"], { ...state("w2"), page: "#/graph" });
  let finish!: (value: WorkspaceState) => void;
  vi.mocked(getWorkspace).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
  const opening = openStation(client, "w2");
  window.location.hash = "#/list/Issue";
  finish({ ...state("w2"), page: "#/graph", revision: 2 });
  await opening;
  expect(window.location.hash).toBe("#/list/Issue");
});

test("hiding the current station opens its visible neighbour and marks it hidden", async () => {
  const client = new QueryClient();
  setStation("w1");
  client.setQueryData(STATIONS_KEY, [
    { id: "w1", name: "alpha", created_at: "2026-10-01T00:00:00Z", conversation_id: "c1" },
    { id: "w2", name: "beta", created_at: "2026-10-02T00:00:00Z", conversation_id: null },
  ]);
  vi.mocked(hideStation).mockResolvedValue({ ...state("w1"), hidden: true });
  vi.mocked(getWorkspace).mockResolvedValue(state("w2"));
  await hideStationAction(client, "w1");
  expect(currentStation().id).toBe("w2");
  expect(client.getQueryData<{ id: string; hidden?: boolean }[]>(STATIONS_KEY)?.[0]).toMatchObject({ id: "w1", hidden: true });
});

test("clear chat opens and caches the workspace returned by the server", async () => {
  const client = new QueryClient();
  const next = { ...state("w2"), conversation_id: null };
  setStation("w1");
  vi.mocked(clearChat).mockResolvedValue(next);
  await clearChatAction(client, "w1", "key-1");
  expect(currentStation().id).toBe("w2");
  expect(client.getQueryData(["workspace", "w2"])).toEqual(next);
});

test.each(["hide", "clear"] as const)("a delayed %s result keeps a newer station selection", async (action) => {
  const client = new QueryClient();
  setStation("w1");
  client.setQueryData(STATIONS_KEY, [
    { id: "w1", created_at: "1" }, { id: "w2", created_at: "2" },
  ]);
  let finish!: (value: WorkspaceState) => void;
  const reply = new Promise<WorkspaceState>((resolve) => { finish = resolve; });
  vi.mocked(getWorkspace).mockResolvedValue(state("w2"));
  vi.mocked(hideStation).mockReturnValue(reply);
  vi.mocked(clearChat).mockReturnValue(reply);
  const changing = action === "hide" ? hideStationAction(client, "w1") : clearChatAction(client, "w1", "key");
  setStation("w3");
  finish(state(action === "hide" ? "w1" : "new-chat"));
  await changing;
  expect(currentStation().id).toBe("w3");
});

test("selecting the current chat cancels delayed navigation away from it", async () => {
  const client = new QueryClient();
  setStation("w1");
  let finish!: (value: WorkspaceState) => void;
  vi.mocked(clearChat).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
  const clearing = clearChatAction(client, "w1", "key");
  await openStation(client, "w1");
  finish(state("new-chat"));
  await clearing;
  expect(currentStation().id).toBe("w1");
});
