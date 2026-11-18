/**
 * Canvas timing marks: when each pushed frame arrived and when what it changed
 * painted, for `trackinizer.timings()`.
 *
 * A `workspace` frame is followed by the marks of the visuals it changed; a
 * `navigate` frame by the mark of the page it opened, drawn with its data.
 * Wall times are epoch milliseconds from the same clock as the frame's `t` (the
 * server's), so on one machine `wall - t` is the time from the server accepting
 * an operation to the pixels. `at` is `performance.now()`, the page's own
 * clock. Only ids, types, routes and numbers are kept.
 */

/** Which moment a mark records. */
export type MarkKind = "paint" | "data" | "page";

/** One mark: a visual's content (`paint`) or data (`data`), or a navigated page's data (`page`). */
export type Mark = {
  readonly type: string;
  readonly kind: MarkKind;
  readonly at: number;
  readonly wall: number;
};

/** One pushed frame and its marks. */
export type FrameTiming = {
  readonly frame: "workspace" | "navigate";
  /** The workspace revision of a `workspace` frame; null for a `navigate` frame. */
  readonly revision: number | null;
  /** The route a `navigate` frame opened; null for a `workspace` frame. */
  readonly route: string | null;
  /** The frame's `t`, the server's epoch milliseconds. */
  readonly t: number;
  readonly received: number;
  readonly receivedWall: number;
  readonly marks: readonly Mark[];
};

/** How many frames are kept. */
const KEPT = 200;

type Held = { -readonly [K in keyof FrameTiming]: K extends "marks" ? Mark[] : FrameTiming[K] };

const frames: Held[] = [];
/** The navigate frame whose page has not drawn yet. */
let navigating: Held | null = null;

function push(frame: "workspace" | "navigate", revision: number | null, route: string | null, t: number): Held {
  const received = performance.now();
  const held: Held = { frame, revision, route, t, received, receivedWall: performance.timeOrigin + received, marks: [] };
  frames.push(held);
  if (frames.length > KEPT) frames.shift();
  return held;
}

/** Record that the `workspace` frame for `revision` arrived. */
export function recordFrame(revision: number, t: number): void {
  push("workspace", revision, null, t);
}

/** Record that a `navigate` frame to `route` arrived; `markPageDrawn` closes it. */
export function recordNavigation(route: string, t: number): void {
  navigating = push("navigate", null, route, t);
}

/**
 * After the next frame paints, mark that a visual of `type` showed its content
 * (`paint`) or its data (`data`) at `revision`. A revision no frame carried (the
 * browser's own write, a first read) is not recorded.
 */
export function markAfterPaint(revision: number, type: string, kind: MarkKind): void {
  afterPaint(() => frames.findLast((entry) => entry.revision === revision), type, kind);
}

/**
 * The page at `hash` has drawn its data. Marks the pending `navigate` frame
 * when it opened that route, once; any other draw is none of its business.
 */
export function markPageDrawn(hash: string): void {
  const pending = navigating;
  if (!pending || pending.route?.toLowerCase() !== hash.toLowerCase()) return;
  navigating = null;
  afterPaint(() => pending, "page", "page");
}

function afterPaint(find: () => Held | undefined, type: string, kind: MarkKind): void {
  // A frame callback runs before its paint, so the mark waits one task more.
  requestAnimationFrame(() => setTimeout(() => {
    const frame = find();
    if (!frame) return;
    const at = performance.now();
    frame.marks.push({ type, kind, at, wall: performance.timeOrigin + at });
  }, 0));
}

/** The recorded frames, oldest first. */
export function recentTimings(): readonly FrameTiming[] {
  return frames.map((frame) => ({ ...frame, marks: [...frame.marks] }));
}

/** Forget every frame; for tests. */
export function resetTimings(): void {
  frames.length = 0;
  navigating = null;
}
