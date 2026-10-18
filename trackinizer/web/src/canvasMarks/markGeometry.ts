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
