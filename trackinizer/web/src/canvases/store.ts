import { useSyncExternalStore } from "react";
import { currentRoute } from "../router/addressBar";

/** The canvas this tab shows, and the page it was sent to when the tab switched to it. */
export type CurrentCanvas = {
  /** Null before the first canvas is known: the tab asks the server for the one changed last. */
  readonly id: string | null;
  /** The route a switch navigated to; the page is not written back until the address is there. */
  readonly landing: string | null;
};

const listeners = new Set<() => void>();

/** The tab's `sessionStorage` key for its canvas: a reload finds it, a new tab does not. */
const KEY = "trackinizer.canvas.tab";

/**
 * The canvas as this page has it. Its id is kept in the tab's `sessionStorage`,
 * so a reload keeps the canvas; a new tab has none and asks the server for the
 * one changed last. Held here too when storage refuses it, so the tab still
 * switches.
 */
let current: CurrentCanvas = { id: readTab(), landing: null };
let navigation = 0;

/** An asynchronous navigation may select its result only while this intent is current. */
export function beginCanvasNavigation(): () => boolean {
  const intent = ++navigation;
  const route = currentRoute();
  return () => intent === navigation && currentRoute() === route;
}

// The browser no longer keeps the last canvas opened for every tab: forget what an older build left.
try {
  localStorage.removeItem("trackinizer.station.current");
} catch { /* Storage can be unavailable; then nothing was kept. */ }

function readTab(): string | null {
  try {
    const id = sessionStorage.getItem(KEY);
    const legacyKey = "trackinizer.station.tab";
    const legacy = id === null ? sessionStorage.getItem(legacyKey) : null;
    try {
      if (legacy !== null) sessionStorage.setItem(KEY, legacy);
      sessionStorage.removeItem(legacyKey);
    } catch { /* A readable tab choice still works when storage refuses writes. */ }
    return id ?? legacy;
  } catch {
    return null;
  }
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/**
 * Show canvas `id` (null forgets it, and the tab asks the server again). A
 * switch that moves the page names it as `landing`, so the page being left is
 * not written to the canvas being entered.
 */
export function setCanvas(id: string | null, landing: string | null = null): void {
  try {
    if (id) sessionStorage.setItem(KEY, id);
    else sessionStorage.removeItem(KEY);
  } catch { /* Private storage can be unavailable. */ }
  if (id === current.id && landing === current.landing) return;
  if (id !== current.id) navigation += 1;
  current = { id, landing };
  listeners.forEach((listener) => listener());
}

/** The canvas now, for what is not a component. */
export function currentCanvas(): CurrentCanvas {
  return current;
}

/** The canvas this tab shows, kept current wherever it is switched. */
export function useCurrentCanvas(): CurrentCanvas {
  return useSyncExternalStore(subscribe, currentCanvas);
}
