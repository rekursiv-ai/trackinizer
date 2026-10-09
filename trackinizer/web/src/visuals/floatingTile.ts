import type { BrowserState, TileMemory } from "../state/value";

/**
 * A press on a floating tile's top bar that never goes farther than this many
 * pixels from where it began is a click; one that does is a drag. A hand
 * resting on a mouse or a finger on glass moves a pixel or two on its own.
 */
export const CLICK_SLOP = 4;

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

/** What a release was: a click, or a drag that left the tile at `place`. */
export type Outcome =
  | { readonly kind: "click" }
  | { readonly kind: "drag"; readonly place: { readonly left: number; readonly top: number } };

export function finishGesture(gesture: Gesture): Outcome {
  if (gesture.peak <= CLICK_SLOP) return { kind: "click" };
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
