import { useSyncExternalStore } from "react";

/** The station this tab shows, and the page it was sent to when the tab switched to it. */
export type CurrentStation = {
  /** Null before the first station is known: the tab asks the server for the one changed last. */
  readonly id: string | null;
  /** The route a switch navigated to; the page is not written back until the address is there. */
  readonly landing: string | null;
};

const listeners = new Set<() => void>();

/** The tab's `sessionStorage` key for its station: a reload finds it, a new tab does not. */
const KEY = "trackinizer.station.tab";

/**
 * The station as this page has it. Its id is kept in the tab's `sessionStorage`,
 * so a reload keeps the station; a new tab has none and asks the server for the
 * one changed last. Held here too when storage refuses it, so the tab still
 * switches.
 */
let current: CurrentStation = { id: readTab(), landing: null };
let navigation = 0;

/** An asynchronous navigation may select its result only while this intent is current. */
export function beginStationNavigation(): () => boolean {
  const intent = ++navigation;
  const route = window.location.hash;
  return () => intent === navigation && window.location.hash === route;
}

// The browser no longer keeps the last station opened for every tab: forget what an older build left.
try {
  localStorage.removeItem("trackinizer.station.current");
} catch { /* Storage can be unavailable; then nothing was kept. */ }

function readTab(): string | null {
  try {
    return sessionStorage.getItem(KEY);
  } catch {
    return null;
  }
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/**
 * Show station `id` (null forgets it, and the tab asks the server again). A
 * switch that moves the page names it as `landing`, so the page being left is
 * not written to the station being entered.
 */
export function setStation(id: string | null, landing: string | null = null): void {
  try {
    if (id) sessionStorage.setItem(KEY, id);
    else sessionStorage.removeItem(KEY);
  } catch { /* Private storage can be unavailable. */ }
  if (id === current.id && landing === current.landing) return;
  if (id !== current.id) navigation += 1;
  current = { id, landing };
  listeners.forEach((listener) => listener());
}

/** The station now, for what is not a component. */
export function currentStation(): CurrentStation {
  return current;
}

/** The station this tab shows, kept current wherever it is switched. */
export function useCurrentStation(): CurrentStation {
  return useSyncExternalStore(subscribe, currentStation);
}
