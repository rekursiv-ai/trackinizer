import { expect, test } from "vitest";
import { EMPTY_STATE } from "../state/value";
import { CLICK_SLOP, finishGesture, moveGesture, rememberedTile, resizeRect, slideDivider, SNAP_SIDE, SNAP_TOP, snapZone, startGesture, TEAR_SLOP, withoutPlaces, withTile } from "./floatingTile";

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

test("a docked tile's press is a click until it has gone the tear slop", () => {
  const gesture = startGesture(START);
  moveGesture(gesture, 100 + TEAR_SLOP, 100);
  expect(finishGesture(gesture, TEAR_SLOP)).toEqual({ kind: "click" });
  moveGesture(gesture, 101 + TEAR_SLOP, 100);
  expect(finishGesture(gesture, TEAR_SLOP).kind).toBe("drag");
});

test("a tile snaps to the column at the side whose edge the pointer is near, to the main strip at the top edge, and nowhere else", () => {
  const width = 800;
  expect(snapZone(SNAP_SIDE, 300, width)).toBe("left");
  expect(snapZone(SNAP_SIDE + 1, 300, width)).toBeNull();
  expect(snapZone(width - SNAP_SIDE, 300, width)).toBe("side");
  expect(snapZone(width - SNAP_SIDE - 1, 300, width)).toBeNull();
  expect(snapZone(400, SNAP_TOP, width)).toBe("main");
  // A drag along a docked tile's bar, which lies under the top edge, expands nothing.
  expect(snapZone(400, SNAP_TOP + 1, width)).toBeNull();
  // A corner is its side's.
  expect([snapZone(0, 0, width), snapZone(width, 0, width)]).toEqual(["left", "side"]);
});

const WINDOW = { left: 200, top: 100, width: 400, height: 300 };
const MIN = { width: 280, height: 220 };
const STAGE = { width: 1000, height: 600 };

test("a window resized by an edge moves that side alone, and by a corner both of its sides", () => {
  expect(resizeRect(WINDOW, "e", 50, 99, MIN, STAGE)).toEqual({ ...WINDOW, width: 450 });
  expect(resizeRect(WINDOW, "s", 99, 40, MIN, STAGE)).toEqual({ ...WINDOW, height: 340 });
  // The left and top sides move the window's corner, and the opposite sides stay.
  expect(resizeRect(WINDOW, "w", -50, 99, MIN, STAGE)).toEqual({ ...WINDOW, left: 150, width: 450 });
  expect(resizeRect(WINDOW, "n", 99, 30, MIN, STAGE)).toEqual({ ...WINDOW, top: 130, height: 270 });
  expect(resizeRect(WINDOW, "nw", -20, -10, MIN, STAGE)).toEqual({ left: 180, top: 90, width: 420, height: 310 });
  expect(resizeRect(WINDOW, "se", 20, 10, MIN, STAGE)).toEqual({ ...WINDOW, width: 420, height: 310 });
  expect(resizeRect(WINDOW, "ne", 20, -10, MIN, STAGE)).toEqual({ left: 200, top: 90, width: 420, height: 310 });
  expect(resizeRect(WINDOW, "sw", -20, 10, MIN, STAGE)).toEqual({ left: 180, top: 100, width: 420, height: 310 });
});

test("a resized window never goes under its least size or out of the stage", () => {
  expect(resizeRect(WINDOW, "e", -500, 0, MIN, STAGE).width).toBe(MIN.width);
  expect(resizeRect(WINDOW, "s", 0, -500, MIN, STAGE).height).toBe(MIN.height);
  // Shrunk from the left or the top, the far side holds: the corner stops where the least size begins.
  expect(resizeRect(WINDOW, "w", 500, 0, MIN, STAGE)).toEqual({ ...WINDOW, left: 320, width: MIN.width });
  expect(resizeRect(WINDOW, "n", 0, 500, MIN, STAGE)).toEqual({ ...WINDOW, top: 180, height: MIN.height });
  expect(resizeRect(WINDOW, "se", 5000, 5000, MIN, STAGE)).toEqual({ ...WINDOW, width: 800, height: 500 });
  expect(resizeRect(WINDOW, "nw", -5000, -5000, MIN, STAGE)).toEqual({ left: 0, top: 0, width: 600, height: 400 });
});

test("a divider gives one neighbour what it takes from the other, down to each one's least size", () => {
  expect(slideDivider(400, 300, 50, 240, 240)).toEqual([450, 250]);
  expect(slideDivider(400, 300, -50, 240, 240)).toEqual([350, 350]);
  expect(slideDivider(400, 300, 500, 240, 240)).toEqual([460, 240]);
  expect(slideDivider(400, 300, -500, 300, 240)).toEqual([300, 400]);
});
