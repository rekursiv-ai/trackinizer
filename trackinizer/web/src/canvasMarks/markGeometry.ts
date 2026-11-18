import type { components } from "../api/generated/schema";

export type MarkPoint = components["schemas"]["MarkPoint"];

/** A box in the stage's fractions. */
export type Bounds = { readonly left: number; readonly top: number; readonly right: number; readonly bottom: number };

const clamp = (value: number) => Math.min(1, Math.max(0, value));
const round = (value: number) => Math.round(value * 10_000) / 10_000;

/** Where a pointer at `(clientX, clientY)` is on a stage drawn at `rect`, as fractions of it. */
export function toFractions(clientX: number, clientY: number, rect: { left: number; top: number; width: number; height: number }): MarkPoint {
  if (rect.width === 0 || rect.height === 0) return { x: 0, y: 0 };
  return { x: round(clamp((clientX - rect.left) / rect.width)), y: round(clamp((clientY - rect.top) / rect.height)) };
}

/** The smallest box that holds every point. */
export function boundsOf(points: readonly MarkPoint[]): Bounds {
  const xs = points.map((p) => p.x);
  const ys = points.map((p) => p.y);
  return { left: Math.min(...xs), top: Math.min(...ys), right: Math.max(...xs), bottom: Math.max(...ys) };
}

/** Where something stands on screen, in pixels. */
export type Box = { readonly left: number; readonly top: number; readonly width: number; readonly height: number };

/** The topmost of `tiles`, which are in paint order, whose rect holds the point; edges count. */
export function topmostHolding<T extends { readonly rect: Box }>(tiles: readonly T[], x: number, y: number): T | undefined {
  return tiles.findLast(({ rect }) => x >= rect.left && x <= rect.left + rect.width && y >= rect.top && y <= rect.top + rect.height);
}

/**
 * A box's corners, which are fractions of the `stage`, as the store keeps them: the
 * tile whose body holds the box's center is its tile, and the corners are fractions of
 * that body (clamped to it). With no tile there, the corners stay the stage's.
 */
export function pinTo(
  corners: readonly MarkPoint[],
  stage: Box,
  tiles: readonly { readonly id: string; readonly rect: Box }[],
): { readonly tile: string | null; readonly points: readonly MarkPoint[] } {
  const onScreen = corners.map(({ x, y }) => ({ x: stage.left + x * stage.width, y: stage.top + y * stage.height }));
  const tile = topmostHolding(tiles, onScreen.reduce((sum, p) => sum + p.x, 0) / onScreen.length, onScreen.reduce((sum, p) => sum + p.y, 0) / onScreen.length);
  if (!tile) return { tile: null, points: corners };
  return { tile: tile.id, points: onScreen.map((p) => toFractions(p.x, p.y, tile.rect)) };
}
