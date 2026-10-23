import type { CanvasOperation, CanvasState } from "../api/canvases";
import { SHEET, validHash } from "../router/trail";
import type { Placement } from "./layout";

/** A visual as a snapshot keeps it: what it shows and where, never its instance or its size. */
export type SnapVisual = {
  readonly type: string;
  readonly placement: Placement;
  readonly record_id: string | null;
  readonly params: { readonly [name: string]: string | number | boolean };
};

/**
 * What a canvas shows, as its Back and Forward and its tabs restore it: the
 * page's route while the page is shown (null while it is not), and every visual.
 */
export type Snapshot = { readonly page: string | null; readonly visuals: readonly SnapVisual[] };

/** The page: the catalog's default visual, which the route moves. */
export const PAGE = "trax.browse";

/** Where Home goes when it goes to the graph. */
export const HOME_ROUTE = "#/graph";

/** A hash that lies over the page (a sheet, the create form) rather than moving it. */
const OVERLAY = /^#\/new(?:[/?]|$)/;

/**
 * The snapshot of a canvas holding `visuals` with the address at `hash`, or null
 * while the address is not a page of its own (a sheet or the create form over
 * the page, or no route yet): that is not a place to come back to.
 */
export function snapshotOf(visuals: CanvasState["visuals"], hash: string): Snapshot | null {
  const paged = visuals.some((visual) => visual.type === PAGE);
  if (paged && (!validHash(hash) || SHEET.test(hash) || OVERLAY.test(hash))) return null;
  return {
    page: paged ? hash : null,
    visuals: visuals.map((visual) => ({
      type: visual.type,
      placement: visual.placement ?? "main",
      record_id: visual.record_id ?? null,
      params: visual.params ?? {},
    })),
  };
}

/** What Home shows, and a new tab: the page on the graph, or nothing. */
export function homeSnapshot(home: "graph" | "empty"): Snapshot {
  return home === "empty"
    ? { page: null, visuals: [] }
    : { page: HOME_ROUTE, visuals: [{ type: PAGE, placement: "main", record_id: null, params: {} }] };
}

/** One text for each snapshot: two canvases that show the same have the same, in whatever order their visuals came. */
export function snapshotKey(snapshot: Snapshot): string {
  const visuals = snapshot.visuals.toSorted((a, b) => a.type.localeCompare(b.type))
    .map(({ type, placement, record_id, params }) => [type, placement, record_id, Object.entries(params).toSorted(([a], [b]) => a.localeCompare(b))]);
  return JSON.stringify([snapshot.page, visuals]);
}

/**
 * The operations that turn a canvas holding `live` into one showing `target`'s
 * visuals, applied in order: the visuals it lacks hidden, the ones it shows
 * elsewhere or on another record or with other params shown again or placed,
 * the ones missing shown. The page's route is the caller's to set.
 */
export function planRestore(live: CanvasState["visuals"], target: Snapshot): CanvasOperation[] {
  const wanted = new Set(target.visuals.map((visual) => visual.type));
  const operations: CanvasOperation[] = live
    .filter((visual) => !wanted.has(visual.type))
    .map((visual) => ({ kind: "hide", instance_id: visual.id }));
  for (const want of target.visuals) {
    const have = live.find((visual) => visual.type === want.type);
    const same = have && (have.record_id ?? null) === want.record_id && sameParams(have.params ?? {}, want.params);
    if (!have || !same) {
      operations.push({ kind: "show", visual_type: want.type, placement: want.placement, record_id: want.record_id, params: { ...want.params } });
    } else if ((have.placement ?? "main") !== want.placement) {
      operations.push({ kind: "place", instance_id: have.id, placement: want.placement });
    }
  }
  return operations;
}

function sameParams(a: SnapVisual["params"], b: SnapVisual["params"]): boolean {
  const names = Object.keys(a);
  return names.length === Object.keys(b).length && names.every((name) => a[name] === b[name]);
}
