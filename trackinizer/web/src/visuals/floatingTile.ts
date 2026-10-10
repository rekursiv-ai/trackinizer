import type { BrowserState, TileMemory } from "../state/value";

/**
 * A press on a floating tile's top bar that never goes farther than this many
 * pixels from where it began is a click; one that does is a drag. A hand
 * resting on a mouse or a finger on glass moves a pixel or two on its own.
 */
export const CLICK_SLOP = 4;

/**
 * A press on a docked tile's bar must go this far before the tile leaves its
 * place: the bar is also where a click focuses it, and a click that slips a few
 * pixels must not tear the tile out of the page.
 */
export const TEAR_SLOP = 24;

/**
 * How near an edge of the stage, in pixels, the pointer must come for the tile it
 * drags to snap there. The top is narrow: a docked tile's bar lies along it, and a
 * drag that only slides along the bar must not expand the tile.
 */
export const SNAP_SIDE = 48;
export const SNAP_TOP = 12;

/** Where a dragged tile docks: the column at a side of the page, or the main strip beside the page. */
export type Zone = "left" | "side" | "main";

/**
 * Where a tile dropped with the pointer at (`x`, `y`) in a stage `width` wide
 * docks: the column at the side whose edge the pointer is near, the main strip
 * at the top edge, or nowhere, where it floats.
 */
export function snapZone(x: number, y: number, width: number): Zone | null {
  if (x <= SNAP_SIDE) return "left";
  if (x >= width - SNAP_SIDE) return "side";
  return y <= SNAP_TOP ? "main" : null;
}

/** A floating tile being pressed on: where, where the tile stands, and how far it may go. */
export type Gesture = {
  readonly x: number;
  readonly y: number;
  readonly left: number;
  readonly top: number;
  readonly maxLeft: number;
  readonly maxTop: number;
  /** The tile's offset from where it stood, already held inside the stage. */
  dx: number;
  dy: number;
  /** The farthest the pointer has been from where it began. */
  peak: number;
};

export function startGesture(start: Omit<Gesture, "dx" | "dy" | "peak">): Gesture {
  return { ...start, dx: 0, dy: 0, peak: 0 };
}

/** The pointer is at (`x`, `y`): the tile follows it, inside the stage. */
export function moveGesture(gesture: Gesture, x: number, y: number): void {
  gesture.peak = Math.max(gesture.peak, Math.hypot(x - gesture.x, y - gesture.y));
  gesture.dx = Math.min(gesture.maxLeft, Math.max(0, gesture.left + x - gesture.x)) - gesture.left;
  gesture.dy = Math.min(gesture.maxTop, Math.max(0, gesture.top + y - gesture.y)) - gesture.top;
}

/** The edges and corners a floating window is resized by, named as a compass names them. */
export const EDGES = ["n", "e", "s", "w", "ne", "se", "sw", "nw"] as const;
export type Edge = (typeof EDGES)[number];

/** Where a window stands in the stage and how large it is, in pixels. */
export type Rect = { readonly left: number; readonly top: number; readonly width: number; readonly height: number };

/**
 * `start` with the sides `edge` names moved by (`dx`, `dy`): the opposite
 * sides stay where they are, the window never gets smaller than `min`, and no
 * side leaves a stage `bounds` large.
 */
export function resizeRect(
  start: Rect, edge: Edge, dx: number, dy: number,
  min: { readonly width: number; readonly height: number }, bounds: { readonly width: number; readonly height: number },
): Rect {
  const held = (value: number, low: number, high: number) => Math.max(low, Math.min(value, high));
  const right = start.left + start.width;
  const bottom = start.top + start.height;
  const left = edge.includes("w") ? held(start.left + dx, 0, right - min.width) : start.left;
  const top = edge.includes("n") ? held(start.top + dy, 0, bottom - min.height) : start.top;
  return {
    left,
    top,
    width: edge.includes("e") ? held(start.width + dx, min.width, bounds.width - start.left) : right - left,
    height: edge.includes("s") ? held(start.height + dy, min.height, bounds.height - start.top) : bottom - top,
  };
}

/**
 * The sizes two docked neighbours take when the divider between them, `a` before
 * it and `b` after, moves by `delta`: what one gains the other gives, and
 * neither goes under its own least size.
 */
export function slideDivider(a: number, b: number, delta: number, minA: number, minB: number): readonly [number, number] {
  const moved = Math.max(minA - a, Math.min(delta, b - minB));
  return [a + moved, b - moved];
}

/** What a release was: a click, or a drag that left the tile at `place`. */
export type Outcome =
  | { readonly kind: "click" }
  | { readonly kind: "drag"; readonly place: { readonly left: number; readonly top: number } };

export function finishGesture(gesture: Gesture, slop = CLICK_SLOP): Outcome {
  if (gesture.peak <= slop) return { kind: "click" };
  return { kind: "drag", place: { left: gesture.left + gesture.dx, top: gesture.top + gesture.dy } };
}

const UNTOUCHED: TileMemory = { collapsed: false, place: null };

/** How the floating tile of visual type `type` was left in this browser. */
export function rememberedTile(state: BrowserState, type: string): TileMemory {
  return state.ui.tiles[type] ?? UNTOUCHED;
}

/** `state` with `change` applied to the memory of the tile of visual type `type`. */
export function withTile(state: BrowserState, type: string, change: Partial<TileMemory>): BrowserState {
  return { ...state, ui: { ...state.ui, tiles: { ...state.ui.tiles, [type]: { ...rememberedTile(state, type), ...change } } } };
}

/** `state` with every tile's dragged place forgotten, so a saved layout's places show. */
export function withoutPlaces(state: BrowserState): BrowserState {
  return {
    ...state,
    ui: { ...state.ui, tiles: Object.fromEntries(Object.entries(state.ui.tiles).map(([type, tile]) => [type, { ...tile, place: null }])) },
  };
}
