import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { ApiError } from "../api/client";
import { createDefaultWorkspace, getWorkspace, type WorkspaceState } from "../api/workspaces";
import { currentStation, setStation } from "./store";
import { showStation, useStation } from "./useStation";

vi.mock("../api/workspaces", () => ({ createDefaultWorkspace: vi.fn(), getWorkspace: vi.fn() }));

const state = (id: string, extra: Partial<WorkspaceState> = {}): WorkspaceState => ({
  id, name: `name of ${id}`, conversation_id: null, page: null, revision: 1, focused_instance: null, visuals: [], ...extra,
});

let client: QueryClient;
const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  setStation(null);
  history.replaceState(null, "", "#/list/Issue");
});

afterEach(() => {
  cleanup();
  setStation(null);
  vi.clearAllMocks();
  history.replaceState(null, "", "#/");
});

test("a tab with no station asks for the one changed last, and keeps it", async () => {
  vi.mocked(createDefaultWorkspace).mockResolvedValue(state("w1"));
  const { result } = renderHook(() => useStation(), { wrapper });
  await waitFor(() => expect(result.current.data?.id).toBe("w1"));
  expect(currentStation().id).toBe("w1");
  expect(client.getQueryData(["workspace", "w1"])).toMatchObject({ id: "w1" });
  expect(getWorkspace).not.toHaveBeenCalled();
  // The page the tab opened on is left where it was.
  expect(location.hash).toBe("#/list/Issue");
});

test("a tab that kept a station reads that one, without asking for the last", async () => {
  setStation("w7");
  vi.mocked(getWorkspace).mockResolvedValue(state("w7"));
  const { result } = renderHook(() => useStation(), { wrapper });
  await waitFor(() => expect(result.current.data?.id).toBe("w7"));
  expect(getWorkspace).toHaveBeenCalledWith("w7", expect.anything());
  expect(createDefaultWorkspace).not.toHaveBeenCalled();
});

test("a kept station that is gone, or is not the user's, is replaced by the one changed last", async () => {
  setStation("gone");
  vi.mocked(getWorkspace).mockRejectedValue(new ApiError(404, "not found"));
  vi.mocked(createDefaultWorkspace).mockResolvedValue(state("w2"));
  const { result } = renderHook(() => useStation(), { wrapper });
  await waitFor(() => expect(result.current.data?.id).toBe("w2"));
  expect(currentStation().id).toBe("w2");
});

test("a station that fails to open for another reason is an error, and is not replaced", async () => {
  setStation("w7");
  vi.mocked(getWorkspace).mockRejectedValue(new ApiError(500, "boom"));
  const { result } = renderHook(() => useStation(), { wrapper });
  await waitFor(() => expect(result.current.isError).toBe(true));
  expect(currentStation().id).toBe("w7");
  expect(createDefaultWorkspace).not.toHaveBeenCalled();
});

test("a read after a refused write reads the station again, and a write that landed meanwhile is kept", async () => {
  setStation("w1");
  vi.mocked(getWorkspace).mockResolvedValue(state("w1"));
  const { result } = renderHook(() => useStation(), { wrapper });
  await waitFor(() => expect(result.current.data?.revision).toBe(1));
  let release: (value: WorkspaceState) => void = () => {};
  vi.mocked(getWorkspace).mockImplementation(() => new Promise((resolve) => { release = resolve; }));
  const again = result.current.refetch();
  client.setQueryData(["workspace", "w1"], state("w1", { revision: 3 }));
  await act(async () => { release(state("w1", { revision: 2 })); await again; });
  expect(client.getQueryData<WorkspaceState>(["workspace", "w1"])?.revision).toBe(3);
  expect(getWorkspace).toHaveBeenCalledTimes(2);
});

test("showing a station seeds the cache, remembers it, and moves the page to the one it showed last", () => {
  showStation(client, state("w5", { page: "#/list/Paper" }));
  expect(client.getQueryData(["workspace", "w5"])).toMatchObject({ id: "w5" });
  expect(currentStation()).toEqual({ id: "w5", landing: "#/list/Paper" });
  expect(location.hash).toBe("#/list/Paper");
});

test("a station that showed no page opens on the graph, and a caller can keep the page it is on", () => {
  showStation(client, state("w5"));
  expect(location.hash).toBe("#/graph");
  showStation(client, state("w6"), "#/list/Paper");
  expect(location.hash).toBe("#/list/Paper");
  expect(currentStation().landing).toBe("#/list/Paper");
});
