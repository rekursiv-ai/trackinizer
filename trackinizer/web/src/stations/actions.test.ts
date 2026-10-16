import { QueryClient } from "@tanstack/react-query";
import { afterEach, expect, test, vi } from "vitest";
import { clearChat, hideStation } from "../api/stations";
import { getWorkspace } from "../api/workspaces";
import type { WorkspaceState } from "../api/workspaces";
import { clearChat as clearChatAction, STATIONS_KEY, hideStation as hideStationAction } from "./actions";
import { currentStation, setStation } from "./store";

vi.mock("../api/stations", () => ({ clearChat: vi.fn(), hideStation: vi.fn() }));
vi.mock("../api/workspaces", () => ({ getWorkspace: vi.fn() }));

afterEach(() => {
  vi.clearAllMocks();
  setStation(null);
});

const state = (id: string): WorkspaceState => ({ id, name: id, conversation_id: null, page: null, revision: 1, focused_instance: null, visuals: [] });

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
