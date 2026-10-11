import { useSyncExternalStore } from "react";

const KEY = "trackinizer.station.current";

/** The station this tab shows, and the page it was sent to when the tab switched to it. */
export type CurrentStation = {
  /** Null before the first station is known: the tab asks the server for the one changed last. */
  readonly id: string | null;
  /** The route a switch navigated to; the page is not written back until the address is there. */
  readonly landing: string | null;
};

const listeners = new Set<() => void>();

/**
 * The station as this page has it. Its id is kept in the browser as the last one
 * opened, and read back once; held here too when storage refuses it, so the tab
 * still switches.
 */
let current: CurrentStation = { id: readStored(), landing: null };

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function readStored(): string | null {
  try {
    return localStorage.getItem(KEY);
  } catch {
    return null;
  }
}

/**
 * Show station `id` (null forgets it, and the tab asks the server again). A
 * switch that moves the page names it as `landing`, so the page being left is
 * not written to the station being entered.
 */
export function setStation(id: string | null, landing: string | null = null): void {
  try {
    if (id) localStorage.setItem(KEY, id);
    else localStorage.removeItem(KEY);
  } catch { /* Private storage can be unavailable. */ }
  if (id === current.id && landing === current.landing) return;
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
