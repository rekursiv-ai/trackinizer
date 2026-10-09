import { expect, test } from "vitest";
import { EMPTY_STATE } from "../state/value";
import { CLICK_SLOP, finishGesture, moveGesture, rememberedTile, startGesture, withoutPlaces, withTile } from "./floatingTile";

const START = { x: 100, y: 100, left: 50, top: 60, maxLeft: 500, maxTop: 300 };

test("a press that never leaves the click slop is a click, and moves nothing", () => {
  const gesture = startGesture(START);
  moveGesture(gesture, 100 + CLICK_SLOP, 100);
  expect(finishGesture(gesture)).toEqual({ kind: "click" });
});

test("a press that goes past the slop is a drag, and the tile lands where the pointer took it", () => {
  const gesture = startGesture(START);
  moveGesture(gesture, 100 + CLICK_SLOP + 1, 100);
  expect(finishGesture(gesture)).toEqual({ kind: "drag", place: { left: 55, top: 60 } });
});

test("a press that goes out past the slop and back is still a drag: dragging never toggles", () => {
  const gesture = startGesture(START);
  moveGesture(gesture, 160, 130);
  moveGesture(gesture, 100, 100);
  expect(finishGesture(gesture)).toEqual({ kind: "drag", place: { left: 50, top: 60 } });
});

test("the tile follows the pointer and stops at the stage's edges", () => {
  const gesture = startGesture(START);
  moveGesture(gesture, 140, 120);
  expect([gesture.dx, gesture.dy]).toEqual([40, 20]);
  moveGesture(gesture, 900, 900);
  expect([gesture.dx, gesture.dy]).toEqual([450, 240]);
  moveGesture(gesture, -900, -900);
  expect([gesture.dx, gesture.dy]).toEqual([-50, -60]);
  expect(finishGesture(gesture)).toEqual({ kind: "drag", place: { left: 0, top: 0 } });
});

test("a stage smaller than the tile pins it to the corner", () => {
  const gesture = startGesture({ ...START, maxLeft: 0, maxTop: 0 });
  moveGesture(gesture, 300, 300);
  expect(finishGesture(gesture)).toEqual({ kind: "drag", place: { left: 0, top: 0 } });
});

test("a tile never touched is expanded and unplaced", () => {
  expect(rememberedTile(EMPTY_STATE, "trax.chat")).toEqual({ collapsed: false, place: null });
});

test("withTile changes one tile's memory and leaves the rest of the state alone", () => {
  const first = withTile(EMPTY_STATE, "trax.chat", { collapsed: true });
  expect(rememberedTile(first, "trax.chat")).toEqual({ collapsed: true, place: null });
  const second = withTile(first, "trax.chat", { place: { left: 8, top: 9 } });
  expect(rememberedTile(second, "trax.chat")).toEqual({ collapsed: true, place: { left: 8, top: 9 } });
  expect(second.stars).toBe(EMPTY_STATE.stars);
  expect(second.ui.collapsed).toBe(EMPTY_STATE.ui.collapsed);
  const other = withTile(second, "trax.timeline", { collapsed: true });
  expect(rememberedTile(other, "trax.chat")).toEqual(rememberedTile(second, "trax.chat"));
});

test("withoutPlaces forgets where tiles were dragged and keeps whether they are collapsed", () => {
  const placed = withTile(withTile(EMPTY_STATE, "trax.chat", { collapsed: true, place: { left: 1, top: 2 } }), "trax.timeline", { place: { left: 3, top: 4 } });
  const cleared = withoutPlaces(placed);
  expect(cleared.ui.tiles).toEqual({
    "trax.chat": { collapsed: true, place: null },
    "trax.timeline": { collapsed: false, place: null },
  });
});
