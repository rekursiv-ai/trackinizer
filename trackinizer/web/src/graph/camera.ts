import { type DrawNode, isPlaced } from "./model";

export const MIN_ZOOM = 0.02;
export const MAX_ZOOM = 20;

export function clampZoom(
  zoom: number,
  minZoom = MIN_ZOOM,
  maxZoom = MAX_ZOOM,
): number {
  return Math.min(maxZoom, Math.max(minZoom, zoom));
}

export type Bounds = {
  readonly left: number;
  readonly right: number;
  readonly top: number;
  readonly bottom: number;
};

type Tween = {
  fromX: number;
  fromY: number;
  fromZoom: number;
  toX: number;
  toY: number;
  toZoom: number;
  startMs: number;
  durationMs: number;
};

/**
 * 2D pan/zoom camera in graph coordinates (`x, y` at canvas center, `zoom`
 * pixels per graph unit), with smooth eased transitions and `covered.right`
 * framing math reusable by any renderer backend.
 */
export class GraphCamera {
  x = 0;
  y = 0;
  zoom = 1;
  width = 800;
  height = 600;
  #tween: Tween | null = null;

  resize(width: number, height: number): void {
    if (width > 0) this.width = width;
    if (height > 0) this.height = height;
  }

  screenToGraph(sx: number, sy: number): { x: number; y: number } {
    return {
      x: this.x + (sx - this.width / 2) / this.zoom,
      y: this.y + (sy - this.height / 2) / this.zoom,
    };
  }

  graphToScreen(gx: number, gy: number): { x: number; y: number } {
    return {
      x: (gx - this.x) * this.zoom + this.width / 2,
      y: (gy - this.y) * this.zoom + this.height / 2,
    };
  }

  panByScreen(dx: number, dy: number): void {
    this.#tween = null;
    this.x -= dx / this.zoom;
    this.y -= dy / this.zoom;
  }

  zoomAtScreen(
    sx: number,
    sy: number,
    factor: number,
    minZoom = MIN_ZOOM,
    maxZoom = MAX_ZOOM,
  ): void {
    this.#tween = null;
    const before = this.screenToGraph(sx, sy);
    this.zoom = clampZoom(this.zoom * factor, minZoom, maxZoom);
    const after = this.screenToGraph(sx, sy);
    this.x += before.x - after.x;
    this.y += before.y - after.y;
  }

  animateTo(x: number, y: number, zoom: number, durationMs: number): void {
    const clampedZoom = clampZoom(zoom);
    if (durationMs <= 0) {
      this.#tween = null;
      this.x = x;
      this.y = y;
      this.zoom = clampedZoom;
      return;
    }
    this.#tween = {
      fromX: this.x,
      fromY: this.y,
      fromZoom: this.zoom,
      toX: x,
      toY: y,
      toZoom: clampedZoom,
      startMs: performance.now(),
      durationMs,
    };
  }

  /** Advance active transition; returns true if still animating. */
  step(nowMs = performance.now()): boolean {
    const t = this.#tween;
    if (!t) return false;
    const raw = Math.min(1, (nowMs - t.startMs) / t.durationMs);
    if (raw >= 1) {
      this.x = t.toX;
      this.y = t.toY;
      this.zoom = t.toZoom;
      this.#tween = null;
      return false;
    }
    const ease = 1 - (1 - raw) ** 3;
    this.x = t.fromX + (t.toX - t.fromX) * ease;
    this.y = t.fromY + (t.toY - t.fromY) * ease;
    this.zoom = t.fromZoom + (t.toZoom - t.fromZoom) * ease;
    return true;
  }

  get animating(): boolean {
    return this.#tween !== null;
  }
}

/** Bounding box of visible placed nodes passing `filter`, or null if none. */
export function boundsOf(
  nodes: readonly DrawNode[],
  filter: (node: DrawNode) => boolean,
): Bounds | null {
  let left = Infinity;
  let right = -Infinity;
  let top = Infinity;
  let bottom = -Infinity;
  for (const node of nodes) {
    if (node.hidden || !isPlaced(node) || !filter(node)) continue;
    if (node.x < left) left = node.x;
    if (node.x > right) right = node.x;
    if (node.y < top) top = node.y;
    if (node.y > bottom) bottom = node.y;
  }
  return left <= right ? { left, right, top, bottom } : null;
}

/**
 * Target `{ x, y, zoom }` that fits `bounds` inside the uncovered viewport
 * (`viewWidth - coveredRight` by `viewHeight`), accounting for `coveredRight`
 * panel offset.
 */
export function fitBounds(
  bounds: Bounds,
  viewWidth: number,
  viewHeight: number,
  coveredRight: number,
  maxZoom: number,
  paddingPx: number,
): { x: number; y: number; zoom: number } {
  const width = Math.max(1, viewWidth - coveredRight);
  const height = Math.max(1, viewHeight);
  // A padding to suit what is left: beside Peek and a list, a narrow canvas can be under 200 px wide.
  const padding = Math.min(paddingPx, width / 8, height / 8);
  const zoom = clampZoom(
    Math.min(
      maxZoom,
      (width - 2 * padding) / Math.max(bounds.right - bounds.left, 1),
      (height - 2 * padding) / Math.max(bounds.bottom - bounds.top, 1),
    ),
    MIN_ZOOM,
    maxZoom,
  );
  return {
    x: (bounds.left + bounds.right) / 2 + coveredRight / 2 / zoom,
    y: (bounds.top + bounds.bottom) / 2,
    zoom,
  };
}
