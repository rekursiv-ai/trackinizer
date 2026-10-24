import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { ApiError } from "../api/client";
import { createDefaultCanvas, getCanvas, type CanvasState } from "../api/canvases";
import { currentRoute } from "../router/addressBar";
import { currentCanvas, setCanvas } from "./store";
import { showCanvas, useCanvas } from "./useCanvas";

vi.mock("../api/canvases", () => ({ createDefaultCanvas: vi.fn(), getCanvas: vi.fn() }));

const state = (id: string, extra: Partial<CanvasState> = {}): CanvasState => ({
  id, name: `name of ${id}`, conversation_id: null, page: null, revision: 1, focused_instance: null, visuals: [], ...extra,
});

let client: QueryClient;
const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  setCanvas(null);
  history.replaceState(null, "", "#/list/Issue");
});

afterEach(() => {
  cleanup();
  setCanvas(null);
  vi.clearAllMocks();
  history.replaceState(null, "", "#/");
});

test("a tab with no canvas asks for the one changed last, and keeps it", async () => {
  vi.mocked(createDefaultCanvas).mockResolvedValue(state("w1"));
  const { result } = renderHook(() => useCanvas(), { wrapper });
  await waitFor(() => expect(result.current.data?.id).toBe("w1"));
  expect(currentCanvas().id).toBe("w1");
  expect(client.getQueryData(["canvas", "w1"])).toMatchObject({ id: "w1" });
  expect(getCanvas).not.toHaveBeenCalled();
  // The page the tab opened on is left where it was.
  expect(currentRoute()).toBe("#/list/Issue");
});

test("a tab that kept a canvas reads that one, without asking for the last", async () => {
  setCanvas("w7");
  vi.mocked(getCanvas).mockResolvedValue(state("w7"));
  const { result } = renderHook(() => useCanvas(), { wrapper });
  await waitFor(() => expect(result.current.data?.id).toBe("w7"));
  expect(getCanvas).toHaveBeenCalledWith("w7", expect.anything());
  expect(createDefaultCanvas).not.toHaveBeenCalled();
});

test("a kept canvas that is gone, or is not the user's, is replaced by the one changed last", async () => {
  setCanvas("gone");
  vi.mocked(getCanvas).mockRejectedValue(new ApiError(404, "not found"));
  vi.mocked(createDefaultCanvas).mockResolvedValue(state("w2"));
  const { result } = renderHook(() => useCanvas(), { wrapper });
  await waitFor(() => expect(result.current.data?.id).toBe("w2"));
  expect(currentCanvas().id).toBe("w2");
});

test("a canvas that fails to open for another reason is an error, and is not replaced", async () => {
  setCanvas("w7");
  vi.mocked(getCanvas).mockRejectedValue(new ApiError(500, "boom"));
  const { result } = renderHook(() => useCanvas(), { wrapper });
  await waitFor(() => expect(result.current.isError).toBe(true));
  expect(currentCanvas().id).toBe("w7");
  expect(createDefaultCanvas).not.toHaveBeenCalled();
});

test("a read after a refused write reads the canvas again, and a write that landed meanwhile is kept", async () => {
  setCanvas("w1");
  vi.mocked(getCanvas).mockResolvedValue(state("w1"));
  const { result } = renderHook(() => useCanvas(), { wrapper });
  await waitFor(() => expect(result.current.data?.revision).toBe(1));
  let release: (value: CanvasState) => void = () => {};
  vi.mocked(getCanvas).mockImplementation(() => new Promise((resolve) => { release = resolve; }));
  const again = result.current.refetch();
  client.setQueryData(["canvas", "w1"], state("w1", { revision: 3 }));
  await act(async () => { release(state("w1", { revision: 2 })); await again; });
  expect(client.getQueryData<CanvasState>(["canvas", "w1"])?.revision).toBe(3);
  expect(getCanvas).toHaveBeenCalledTimes(2);
});

test("showing a canvas seeds the cache, remembers it, and moves the page to the one it showed last", () => {
  showCanvas(client, state("w5", { page: "#/list/Paper" }));
  expect(client.getQueryData(["canvas", "w5"])).toMatchObject({ id: "w5" });
  expect(currentCanvas()).toEqual({ id: "w5", landing: "#/list/Paper" });
  expect(currentRoute()).toBe("#/list/Paper");
});

test("a canvas that showed no page opens on the graph, and a caller can keep the page it is on", () => {
  showCanvas(client, state("w5"));
  expect(currentRoute()).toBe("#/graph");
  showCanvas(client, state("w6"), "#/list/Paper");
  expect(currentRoute()).toBe("#/list/Paper");
  expect(currentCanvas().landing).toBe("#/list/Paper");
});
