import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { COALESCE_MS, forget, forgetHistories, go, interrupt, KEPT_STEPS, observe, restoring, settle, step, takeInterrupted, useCanvasHistory } from "./canvasHistory";
import type { Snapshot } from "./canvasSnapshot";

afterEach(forgetHistories);

/** A canvas showing only the page, at `page`. */
function at(page: string): Snapshot {
  return { page, visuals: [{ type: "trax.browse", placement: "main", record_id: null, params: {} }] };
}

/** Step `key` and let the canvas arrive where the step went. */
function stepAndArrive(key: string, by: -1 | 1): Snapshot | null {
  const target = step(key, by);
  if (target) observe(key, target, 0);
  return target;
}

test("each change is a step: Back walks to the one before and Forward returns", () => {
  observe("s/1", at("#/a"), 0);
  observe("s/1", at("#/b"), 1_000);
  observe("s/1", at("#/c"), 2_000);
  expect(stepAndArrive("s/1", -1)).toEqual(at("#/b"));
  expect(stepAndArrive("s/1", -1)).toEqual(at("#/a"));
  expect(step("s/1", -1)).toBeNull();
  expect(stepAndArrive("s/1", 1)).toEqual(at("#/b"));
});

test("the same canvas again is no step, and changes close together are one", () => {
  observe("s/1", at("#/a"), 0);
  observe("s/1", at("#/a"), 1_000);
  observe("s/1", at("#/b"), 2_000);
  observe("s/1", at("#/c"), 2_000 + COALESCE_MS - 1);
  expect(stepAndArrive("s/1", -1)).toEqual(at("#/a"));
});

test("the first canvas a tab shows is its own step, even when a change follows at once", () => {
  observe("s/1", at("#/a"), 0);
  observe("s/1", at("#/b"), 1);
  expect(stepAndArrive("s/1", -1)).toEqual(at("#/a"));
});

test("a new step after Back drops the steps ahead, as a browser does", () => {
  observe("s/1", at("#/a"), 0);
  observe("s/1", at("#/b"), 1_000);
  stepAndArrive("s/1", -1);
  observe("s/1", at("#/c"), 1_001);
  expect(step("s/1", 1)).toBeNull();
  expect(stepAndArrive("s/1", -1)).toEqual(at("#/a"));
});

test("what the canvas shows on the way to a step is no step; arriving ends the wait", () => {
  observe("s/1", at("#/a"), 0);
  observe("s/1", at("#/b"), 1_000);
  step("s/1", -1);
  observe("s/1", { page: null, visuals: [] }, 1_001);
  observe("s/1", at("#/a"), 1_002);
  observe("s/1", at("#/c"), 1_003);
  expect(stepAndArrive("s/1", -1)).toEqual(at("#/a"));
});

test("a restore that fell short leaves the tab where the canvas got to", () => {
  observe("s/1", at("#/a"), 0);
  observe("s/1", at("#/b"), 1_000);
  const target = step("s/1", -1)!;
  settle("s/1", target, at("#/z"));
  expect(stepAndArrive("s/1", 1)).toEqual(at("#/b"));
  expect(stepAndArrive("s/1", -1)).toEqual(at("#/z"));
});

test("only the restore on its way is settled", () => {
  observe("s/1", at("#/a"), 0);
  observe("s/1", at("#/b"), 1_000);
  const target = step("s/1", -1)!;
  settle("s/1", at("#/a"), at("#/z"));
  expect(step("s/1", 1)).toBeNull();
  settle("s/1", target, null);
  expect(stepAndArrive("s/1", 1)).toEqual(at("#/b"));
});

test("go takes its target as a new step, and begins the steps of a tab that has none", () => {
  observe("s/1", at("#/a"), 0);
  go("s/1", at("#/graph"));
  observe("s/1", at("#/graph"), 1);
  expect(stepAndArrive("s/1", -1)).toEqual(at("#/a"));
  go("s/2", at("#/graph"));
  observe("s/2", at("#/graph"), 2);
  expect(step("s/2", -1)).toBeNull();
});

test("each tab keeps its own steps, and a closed tab forgets them", () => {
  observe("s/1", at("#/a"), 0);
  observe("s/1", at("#/b"), 1_000);
  observe("s/2", at("#/x"), 1_000);
  expect(step("s/2", -1)).toBeNull();
  forget("s/1");
  expect(step("s/1", -1)).toBeNull();
});

test(`a tab keeps its last ${KEPT_STEPS} steps`, () => {
  for (let n = 0; n <= KEPT_STEPS; n += 1) observe("s/1", at(`#/${n}`), n * 1_000);
  let back = 0;
  while (stepAndArrive("s/1", -1)) back += 1;
  expect(back).toBe(KEPT_STEPS - 1);
});

test("the hook says whether Back and Forward go anywhere, and whether a step is on its way", () => {
  const { result, rerender } = renderHook(({ snapshot }) => useCanvasHistory("s/1", snapshot), { initialProps: { snapshot: at("#/a") } });
  expect(result.current).toEqual({ back: false, forward: false, moving: false });
  rerender({ snapshot: at("#/b") });
  expect(result.current).toEqual({ back: true, forward: false, moving: false });
  act(() => void step("s/1", -1));
  expect(result.current).toEqual({ back: false, forward: true, moving: true });
  rerender({ snapshot: at("#/a") });
  expect(result.current).toEqual({ back: false, forward: true, moving: false });
});

test("a restore a canvas was left in the middle of is taken once, on coming back; closing its tab forgets it", () => {
  interrupt("s", "s/1", at("#/a"));
  expect(takeInterrupted("t")).toBeNull();
  expect(takeInterrupted("s")).toEqual({ key: "s/1", target: at("#/a") });
  expect(takeInterrupted("s")).toBeNull();
  interrupt("s", "s/2", at("#/b"));
  forget("s/2");
  expect(takeInterrupted("s")).toBeNull();
});

test("whether a restore is on its way reads the same outside a render as in one", () => {
  expect(restoring()).toBe(false);
  const target = at("#/b");
  go("s/1", target);
  expect(restoring()).toBe(true);
  settle("s/1", target, null);
  expect(restoring()).toBe(false);
});
