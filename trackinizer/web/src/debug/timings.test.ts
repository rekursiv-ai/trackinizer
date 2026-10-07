import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { markAfterPaint, markPageDrawn, recentTimings, recordFrame, recordNavigation, resetTimings } from "./timings";

beforeEach(() => {
  resetTimings();
  vi.useFakeTimers({ toFake: ["setTimeout", "requestAnimationFrame", "performance"] });
});

afterEach(() => vi.useRealTimers());

/** Run the frame callback and the task after it, which is when a mark lands. */
function paint() {
  vi.advanceTimersToNextFrame();
  vi.advanceTimersByTime(1);
}

test("a frame is recorded on receipt with its revision, server time and the page's clocks", () => {
  recordFrame(7, 1_700_000_000_000);
  const [frame] = recentTimings();
  expect(frame).toMatchObject({ frame: "workspace", revision: 7, route: null, t: 1_700_000_000_000, marks: [] });
  expect(frame!.receivedWall - performance.timeOrigin).toBeCloseTo(frame!.received, 3);
});

test("a mark lands on the frame of its revision after the frame paints, and waits one task past the frame callback", () => {
  recordFrame(7, 1);
  recordFrame(8, 2);
  markAfterPaint(7, "trax.browse", "paint");
  expect(recentTimings()[0]!.marks).toEqual([]);
  vi.advanceTimersToNextFrame();
  expect(recentTimings()[0]!.marks).toEqual([]);
  vi.advanceTimersByTime(1);
  expect(recentTimings()[0]!.marks).toMatchObject([{ type: "trax.browse", kind: "paint" }]);
  expect(recentTimings()[1]!.marks).toEqual([]);
});

test("a revision no frame carried is not recorded, and only the newest 200 frames are kept", () => {
  markAfterPaint(99, "trax.chat", "paint");
  paint();
  expect(recentTimings()).toEqual([]);
  for (let revision = 0; revision < 205; revision++) recordFrame(revision, revision);
  expect(recentTimings()).toHaveLength(200);
  expect(recentTimings()[0]!.revision).toBe(5);
});

test("a navigation is marked once, when the page it opened draws its data, and no other draw counts", () => {
  recordNavigation("#/lookup/ABC", 5);
  expect(recentTimings()[0]).toMatchObject({ frame: "navigate", revision: null, route: "#/lookup/ABC", t: 5 });
  markPageDrawn("#/lookup/other");
  paint();
  expect(recentTimings()[0]!.marks).toEqual([]);
  markPageDrawn("#/lookup/abc");
  markPageDrawn("#/lookup/abc");
  paint();
  expect(recentTimings()[0]!.marks).toMatchObject([{ type: "page", kind: "page" }]);
  expect(recentTimings()[0]!.marks).toHaveLength(1);
});

test("a page that draws with no navigation pending marks nothing", () => {
  markPageDrawn("#/lookup/abc");
  paint();
  expect(recentTimings()).toEqual([]);
});
