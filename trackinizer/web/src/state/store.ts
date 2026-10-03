import { useCallback, useMemo, useSyncExternalStore } from "react";
import { useProfile } from "../app/boot";
import { type BrowserState, EMPTY_STATE, parseState } from "./value";

/** A change to the state: the new state, given the one stored now. */
export type StateChange = (state: BrowserState) => BrowserState;

/**
 * The signed-in user's state in this browser, and `update`, which stores a
 * change to it. Every hook showing it, in this tab or another, updates at once.
 *
 * `update` applies the change to what is stored when it runs, not to what this
 * render shows, so a change made in another tab meanwhile is kept. It throws
 * when the browser refuses to store (storage off or full), and when the stored
 * value is one this build cannot read, rather than overwrite it.
 */
export function useBrowserState(): readonly [BrowserState, (change: StateChange) => void] {
  const { email } = useProfile();
  const key = storageKey(email);
  const stored = useSyncExternalStore(subscribe, () => readStored(key));
  const state = useMemo(() => readable(stored), [stored]);
  const update = useCallback(
    (change: StateChange) => {
      const now = localStorage.getItem(key);
      localStorage.setItem(key, JSON.stringify(change(now === null ? EMPTY_STATE : parseState(JSON.parse(now)))));
      // A `storage` event reaches other tabs only; this tells this one's hooks.
      dispatchEvent(new Event(CHANGED));
    },
    [key],
  );
  return [state, update] as const;
}

/**
 * The `localStorage` key of `email`'s state. Each origin has its own storage
 * already; the origin is in the key too, as the plan names it, so an export
 * read by hand says where it came from.
 */
export function storageKey(email: string): string {
  return `trackinizer.v2.${location.origin}.${email}`;
}

/** Fired on `window` when this tab stores a new state. */
const CHANGED = "trackinizer-state";

function subscribe(onChange: () => void): () => void {
  addEventListener("storage", onChange);
  addEventListener(CHANGED, onChange);
  return () => {
    removeEventListener("storage", onChange);
    removeEventListener(CHANGED, onChange);
  };
}

/** The stored text, or null when there is none or storage is off. */
function readStored(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

/**
 * The stored text as a state to show. Text this build cannot read (hand-edited,
 * or from a newer build in another tab) shows as empty rather than stopping the
 * app; `update` refuses to overwrite it.
 */
function readable(stored: string | null): BrowserState {
  if (stored === null) return EMPTY_STATE;
  try {
    return parseState(JSON.parse(stored));
  } catch {
    return EMPTY_STATE;
  }
}
