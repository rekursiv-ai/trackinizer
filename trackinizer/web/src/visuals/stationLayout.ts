import { skipToken, useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useSyncExternalStore } from "react";
import { setStationLayout } from "../api/stations";
import type { components } from "../api/generated/schema";
import type { WorkspaceState } from "../api/workspaces";
import { useCurrentStation } from "../stations/store";

/** The least time between two writes of a station's layout. */
export const LAYOUT_EVERY_MS = 500;

/** Where a floating window stands, as fractions of the stage's width and height. */
export type Place = { readonly left: number; readonly top: number };

/**
 * How a station's parts were left, resolution-free: widths, sizes and places
 * are fractions of the stage, Chat's width is in `ch`. A key the user never set
 * is absent. Each is replaced whole when it changes.
 */
export type Layout = {
  /** The columns left and right of the page, as fractions of the stage's width. */
  readonly columns?: { readonly left?: number; readonly side?: number };
  /** Chat's width, in `ch`. */
  readonly chat_ch?: number;
  /** Floating windows' sizes as fractions of the stage, by visual type. */
  readonly floating?: { readonly [type: string]: { readonly width: number; readonly height: number } };
  /** Floating windows' folds and places, by visual type. */
  readonly tiles?: { readonly [type: string]: { readonly collapsed?: boolean; readonly place?: Place } };
  /** Docked tiles' shares of their strips, by visual type (1 is an even share). */
  readonly share?: { readonly [type: string]: number };
  /** The id of the Console view that is open. */
  readonly console?: string;
};

/** What the server stores: the same, with a key it holds nothing for null. */
type Stored = components["schemas"]["StationLayout"];

/** A change to the layout: the keys to replace, or a function of the layout now that returns them. */
export type LayoutChange = Layout | ((layout: Layout) => Layout);

/** The layout as the server read it: the keys it holds, and none it does not. */
export function layoutOf(stored: Stored | null | undefined): Layout {
  return Object.fromEntries(Object.entries(stored ?? {}).filter(([, value]) => value !== null && value !== undefined)) as Layout;
}

/** Whether the layout holds a key of `keys`. */
export function holds(layout: Layout, ...keys: readonly (keyof Layout)[]): boolean {
  return keys.some((key) => layout[key] !== undefined);
}

const listeners = new Set<() => void>();
/** The layouts this tab changed, by station; a station not here shows what the server read. */
const changed = new Map<string, Layout>();
/** The keys not yet written, by station, and when they go. */
const waiting = new Map<string, { patch: Layout; timer: ReturnType<typeof setTimeout> }>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/**
 * The current station's layout, and `change`, which sets keys of it. A change
 * shows at once in every hook showing the layout and goes to the server at most
 * every `LAYOUT_EVERY_MS`, with the keys changed since the last write. Nothing
 * is written but what the user changes: a screen that fits the layout to its own
 * size writes nothing. A tab with no station yet keeps its changes only here.
 */
export function useLayout(): readonly [Layout, (change: LayoutChange, options?: { readonly now?: boolean }) => void] {
  // Only reads the station the tab shows, from the cache the shell's read fills;
  // asking for a station is the canvas's and Chat's business, not a layout's.
  const { id: current } = useCurrentStation();
  const station = useQuery<WorkspaceState>({ queryKey: ["workspace", current ?? "boot"], queryFn: skipToken }).data;
  const id = station?.id ?? "";
  const server = useMemo(() => layoutOf(station?.layout), [station?.layout]);
  const latest = useRef(server);
  latest.current = server;
  const mine = useSyncExternalStore(subscribe, () => changed.get(id));
  useEffect(() => {
    addEventListener("pagehide", flushLayouts);
    return () => removeEventListener("pagehide", flushLayouts);
  }, []);
  const change = useCallback((change: LayoutChange, { now = false } = {}) => {
    const patch = typeof change === "function" ? change(changed.get(id) ?? latest.current) : change;
    changed.set(id, { ...(changed.get(id) ?? latest.current), ...patch });
    listeners.forEach((listener) => listener());
    if (id) queueWrite(id, patch, now);
  }, [id]);
  return [mine ?? server, change] as const;
}

function queueWrite(id: string, patch: Layout, now: boolean): void {
  const pending = waiting.get(id);
  if (pending) clearTimeout(pending.timer);
  const merged = { ...pending?.patch, ...patch };
  if (now) {
    waiting.delete(id);
    send(id, merged);
    return;
  }
  waiting.set(id, { patch: merged, timer: setTimeout(() => flush(id), LAYOUT_EVERY_MS) });
}

function flush(id: string): void {
  const pending = waiting.get(id);
  if (!pending) return;
  clearTimeout(pending.timer);
  waiting.delete(id);
  send(id, pending.patch);
}

function send(id: string, patch: Layout): void {
  void setStationLayout(id, patch).catch(() => {});
}

/** Write every layout change still waiting, as the page goes. */
export function flushLayouts(): void {
  for (const id of [...waiting.keys()]) flush(id);
}

/** Forget this tab's changes and the writes waiting; for a test's start. */
export function forgetLayouts(): void {
  for (const { timer } of waiting.values()) clearTimeout(timer);
  waiting.clear();
  changed.clear();
  listeners.forEach((listener) => listener());
}
