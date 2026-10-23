import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { VisualPane } from "./registry";

// Each test waits for the failed import inside act: outside it, React keeps a
// suspended tile's fallback on screen for 300 ms before the error replaces it.
vi.mock("./Timeline", () => { throw new Error("Timeline chunk unavailable"); });

afterEach(() => { cleanup(); });

test("timeline is a separate lazy renderer whose import failure stays in its tile", async () => {
  const shared = { canvas: null, focused: false, onCanvasChanged: vi.fn() };
  render(<>
    <VisualPane {...shared} instance={{ id: "browse", type: "trax.browse", version: 1,
      placement: "main", record_id: null, params: {} }}><button>Browse records</button></VisualPane>
    <VisualPane {...shared} instance={{ id: "timeline", type: "trax.timeline", version: 1,
      placement: "side", record_id: "record-id", params: {} }} />
  </>);
  await act(() => vi.dynamicImportSettled());
  expect(screen.getByRole("alert")).toHaveProperty("textContent", "Could not load trax.timeline. Reload to try again.");
  expect(screen.getByRole("button", { name: "Browse records" })).toBeTruthy();
});

test("a pane marks its paint for the revision that changed it, once, and not again for a later revision", async () => {
  const { recentTimings, recordFrame, resetTimings } = await import("../debug/timings");
  resetTimings();
  vi.useFakeTimers({ toFake: ["setTimeout", "requestAnimationFrame"] });
  recordFrame(4, 1);
  recordFrame(5, 2);
  const instance = { id: "browse", type: "trax.browse", version: 1, placement: "main" as const, record_id: null, params: {} };
  const show = (revision: number) => <VisualPane instance={instance} focused={false} onCanvasChanged={vi.fn()}
    canvas={{ id: "w", name: "unnamed-canvas#1", conversation_id: null, page: null, revision, visuals: [instance], focused_instance: null, partner: null }}>
    <p>Browse records</p></VisualPane>;
  const view = render(show(4));
  vi.advanceTimersToNextFrame();
  vi.advanceTimersByTime(1);
  view.rerender(show(5));
  vi.advanceTimersToNextFrame();
  vi.advanceTimersByTime(1);
  vi.useRealTimers();
  expect(recentTimings().map((frame) => frame.marks.map((mark) => `${mark.type}:${mark.kind}`))).toEqual([["trax.browse:paint"], []]);
});

test("trax.terminal has a renderer of its catalog version; another version shows the unsupported notice", () => {
  const instance = { id: "v1", type: "trax.terminal", version: 2, placement: "main", record_id: null, params: {} } as const;
  render(<VisualPane instance={instance} canvas={null} onCanvasChanged={vi.fn()} focused={false} />);
  expect(screen.getByRole("alert").textContent).toBe("No renderer for trax.terminal version 2.");
});
