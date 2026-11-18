import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { setStationLayout } from "../api/stations";
import { setStation } from "../stations/store";
import { flushLayouts, forgetLayouts, holds, LAYOUT_EVERY_MS, layoutOf, useLayout } from "./stationLayout";

vi.mock("../api/stations", () => ({ setStationLayout: vi.fn().mockResolvedValue(undefined) }));

const client = new QueryClient();

/** The station the tab shows, as the shell's read left it in the cache; none for a null `id`. */
function station(layout: unknown = null, id: string | null = "s1") {
  setStation(id);
  if (id) client.setQueryData(["workspace", id], { id, layout });
}

function useIt() {
  return renderHook(() => useLayout(), { wrapper: ({ children }: { children: ReactNode }) => createElement(QueryClientProvider, { client }, children) });
}

beforeEach(() => {
  vi.useFakeTimers();
  station({ columns: { side: 0.3, left: null }, chat_ch: 80, console: null });
});

afterEach(() => {
  client.clear();
  setStation(null);
  cleanup();
  forgetLayouts();
  vi.useRealTimers();
  vi.clearAllMocks();
});

test("the layout is what the server read, without the keys it holds nothing for", () => {
  const { result } = useIt();
  expect(result.current[0]).toEqual({ columns: { side: 0.3, left: null }, chat_ch: 80 });
  expect(layoutOf(null)).toEqual({});
  expect(holds(result.current[0], "chat_ch")).toBe(true);
  expect(holds(result.current[0], "tiles", "share")).toBe(false);
});

test("a screen that only fits the layout writes nothing", () => {
  useIt();
  act(() => { vi.advanceTimersByTime(10 * LAYOUT_EVERY_MS); });
  expect(setStationLayout).not.toHaveBeenCalled();
});

test("a change shows at once and is written once, with every key changed since the last write", () => {
  const { result } = useIt();
  act(() => result.current[1]({ chat_ch: 60 }));
  act(() => result.current[1]((now) => ({ columns: { ...now.columns, left: 0.2 } })));
  act(() => result.current[1]({ chat_ch: 64 }));
  expect(result.current[0]).toEqual({ columns: { side: 0.3, left: 0.2 }, chat_ch: 64 });
  expect(setStationLayout).not.toHaveBeenCalled();
  act(() => { vi.advanceTimersByTime(LAYOUT_EVERY_MS); });
  expect(setStationLayout).toHaveBeenCalledTimes(1);
  expect(setStationLayout).toHaveBeenCalledWith("s1", { chat_ch: 64, columns: { side: 0.3, left: 0.2 } });
});

test("a change made now is written now, and one waiting goes with it", () => {
  const { result } = useIt();
  act(() => result.current[1]({ console: "view-1" }));
  act(() => result.current[1]({ chat_ch: 50 }, { now: true }));
  expect(setStationLayout).toHaveBeenCalledTimes(1);
  expect(setStationLayout).toHaveBeenCalledWith("s1", { console: "view-1", chat_ch: 50 });
  act(() => { vi.advanceTimersByTime(LAYOUT_EVERY_MS); });
  expect(setStationLayout).toHaveBeenCalledTimes(1);
});

test("a page that goes writes what waits", () => {
  const { result } = useIt();
  act(() => result.current[1]({ share: { "trax.browse": 1.2 } }));
  act(() => { dispatchEvent(new Event("pagehide")); });
  expect(setStationLayout).toHaveBeenCalledWith("s1", { share: { "trax.browse": 1.2 } });
  flushLayouts();
  expect(setStationLayout).toHaveBeenCalledTimes(1);
});

test("a tab with no station yet shows its changes and writes none", () => {
  station(null, null);
  const { result } = useIt();
  act(() => result.current[1]({ chat_ch: 70 }));
  expect(result.current[0]).toEqual({ chat_ch: 70 });
  act(() => { vi.advanceTimersByTime(LAYOUT_EVERY_MS); });
  expect(setStationLayout).not.toHaveBeenCalled();
});

test("another station keeps its own layout", () => {
  const { result, rerender } = useIt();
  act(() => result.current[1]({ chat_ch: 70 }));
  act(() => station({ chat_ch: 90 }, "s2"));
  rerender();
  expect(result.current[0]).toEqual({ chat_ch: 90 });
});

test("a refused write is not an error the page shows", async () => {
  vi.mocked(setStationLayout).mockRejectedValueOnce(new Error("offline"));
  const { result } = useIt();
  act(() => result.current[1]({ chat_ch: 70 }, { now: true }));
  await vi.advanceTimersByTimeAsync(1);
  expect(result.current[0].chat_ch).toBe(70);
});

test.each(["timer", "now", "pagehide"])("a held layout write coalesces newer changes and sends them after it (%s)", async (flush) => {
  let release: () => void = () => { throw new Error("No held write"); };
  let saved = 0;
  vi.mocked(setStationLayout).mockImplementationOnce(() => new Promise<void>((resolve) => {
    release = () => { saved = 0.3; resolve(); };
  })).mockImplementation(async (_id, patch) => { saved = patch.columns?.side ?? saved; });
  const { result } = useIt();
  act(() => result.current[1]({ columns: { side: 0.3 } }, { now: true }));
  act(() => result.current[1]({ columns: { side: 0.4 } }));
  act(() => result.current[1]({ columns: { side: 0.5 } }, { now: flush === "now" }));
  if (flush === "pagehide") act(() => { dispatchEvent(new Event("pagehide")); });
  await act(async () => { await vi.advanceTimersByTimeAsync(LAYOUT_EVERY_MS); });
  expect(setStationLayout).toHaveBeenCalledTimes(1);
  await act(async () => release());
  expect(setStationLayout).toHaveBeenCalledTimes(2);
  expect(vi.mocked(setStationLayout).mock.calls.at(-1)).toEqual(["s1", { columns: { side: 0.5 } }]);
  expect(saved).toBe(0.5);
});

test("another station writes independently while a refused write releases its own queue", async () => {
  let reject: (error: Error) => void = () => { throw new Error("No held write"); };
  vi.mocked(setStationLayout).mockImplementationOnce(() => new Promise<void>((_resolve, refuse) => { reject = refuse; }));
  const { result, rerender } = useIt();
  act(() => result.current[1]({ chat_ch: 70 }, { now: true }));
  act(() => result.current[1]({ chat_ch: 80 }, { now: true }));
  act(() => station({ chat_ch: 90 }, "s2"));
  rerender();
  act(() => result.current[1]({ chat_ch: 100 }, { now: true }));
  expect(vi.mocked(setStationLayout).mock.calls.map(([id]) => id)).toEqual(["s1", "s2"]);
  await act(async () => reject(new Error("offline")));
  expect(vi.mocked(setStationLayout).mock.calls.at(-1)).toEqual(["s1", { chat_ch: 80 }]);
});

test("forget discards changes waiting behind an outstanding write", async () => {
  let release: () => void = () => { throw new Error("No held write"); };
  vi.mocked(setStationLayout).mockImplementationOnce(() => new Promise<void>((resolve) => { release = resolve; }));
  const { result } = useIt();
  act(() => result.current[1]({ chat_ch: 70 }, { now: true }));
  act(() => result.current[1]({ chat_ch: 80 }, { now: true }));
  act(() => forgetLayouts());
  await act(async () => release());
  expect(setStationLayout).toHaveBeenCalledTimes(1);
});
