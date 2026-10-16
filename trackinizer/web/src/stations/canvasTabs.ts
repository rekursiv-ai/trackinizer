import { useSyncExternalStore } from "react";
import type { CanvasTab } from "../visuals/canvasTabs";

/** What the top bar may do to the open station's canvas tabs; the canvas does each. */
export type CanvasTabActions = {
  readonly open: (id: string) => void;
  readonly add: () => void;
  readonly close: (id: string) => void;
  readonly rename: (id: string, name: string) => void;
};

/**
 * The open station's canvas tabs as its canvas holds them: the top bar draws
 * them inside the station's group. Only the canvas switches tabs, since the
 * station's canvas is its open tab and the others keep what they show.
 */
export type CanvasTabsControl = {
  readonly station: string;
  readonly tabs: readonly CanvasTab[];
  readonly open: string;
  /** A step is on its way: the tabs wait. */
  readonly moving: boolean;
  readonly canAdd: boolean;
  readonly actions: CanvasTabActions;
};

/** A tab of another station to open once that station is on show. */
export type RequestedTab = { readonly station: string; readonly tab: string };

let control: CanvasTabsControl | null = null;
let requested: RequestedTab | null = null;
const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function changed(): void {
  listeners.forEach((listener) => listener());
}

/** The canvas says what its tabs are now, or null as it unmounts. */
export function publishCanvasTabs(next: CanvasTabsControl | null): void {
  if (next === control) return;
  control = next;
  changed();
}

/** The open station's canvas tabs, or null while no canvas is mounted. */
export function useCanvasTabs(): CanvasTabsControl | null {
  return useSyncExternalStore(subscribe, () => control);
}

/**
 * Ask for tab `tab` of station `station`, which the top bar is opening: the
 * canvas opens the tab once the station is the one it shows.
 */
export function requestTab(next: RequestedTab | null): void {
  if (next?.station === requested?.station && next?.tab === requested?.tab) return;
  requested = next;
  changed();
}

/** The tab asked for, until the canvas has opened it. */
export function useRequestedTab(): RequestedTab | null {
  return useSyncExternalStore(subscribe, () => requested);
}
