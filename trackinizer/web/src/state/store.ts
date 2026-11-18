import { useCallback, useMemo, useSyncExternalStore } from "react";
import { useProfile } from "../app/boot";
import { choosePreferences, type Preferences, preferences, usePreferences } from "../preferences";
import { type BrowserState, EMPTY_STATE, parseState, sameJson, stateOf, storageKey } from "./value";

/** A change to the state: the new state, given the one shown now. */
export type StateChange = (state: BrowserState) => BrowserState;

/**
 * The signed-in user's state, and `update`, which stores a change to it. Every
 * hook showing it updates at once.
 *
 * All of it is the user's preferences, kept on the server (`src/preferences.ts`)
 * so it follows the user to any browser, except `ui.tiles`, which this browser
 * keeps in `localStorage`. `update` applies the change to what is shown when it
 * runs, and sends the keys it changed. It throws when it changes `ui.tiles` and
 * the browser refuses to store (storage off or full), or the stored value is one
 * this build cannot read, rather than overwrite it.
 */
export function useBrowserState(): readonly [BrowserState, (change: StateChange) => void] {
  const { email } = useProfile();
  const key = storageKey(email);
  const stored = useSyncExternalStore(subscribe, () => readStored(key));
  const tiles = useMemo(() => readable(stored).ui.tiles, [stored]);
  const kept = usePreferences();
  const state = useMemo(() => stateOf(kept, tiles), [kept, tiles]);
  const update = useCallback(
    (change: StateChange) => {
      const now = localStorage.getItem(key);
      // Unreadable local text blocks a change to the tiles only, below.
      const local = attempt(() => (now === null ? EMPTY_STATE : parseState(JSON.parse(now))));
      const before = stateOf(preferences(), local instanceof Error ? {} : local.ui.tiles);
      const after = change(before);
      const moved = changedPreferences(before, after);
      if (Object.keys(moved).length > 0) void choosePreferences(moved);
      if (after.ui.tiles === before.ui.tiles) return;
      if (local instanceof Error) throw local;
      localStorage.setItem(key, JSON.stringify({ ...local, ui: { ...local.ui, tiles: after.ui.tiles } }));
      // A `storage` event reaches other tabs only; this tells this one's hooks.
      dispatchEvent(new Event(CHANGED));
    },
    [key],
  );
  return [state, update] as const;
}

/** The preferences `after` changes from `before`; a value rebuilt equal to the old one is no change. */
function changedPreferences(before: BrowserState, after: BrowserState): Partial<Preferences> {
  return {
    ...(sameJson(after.stars, before.stars) ? {} : { stars: after.stars }),
    ...(sameJson(after.views, before.views) ? {} : { views: after.views }),
    ...(sameJson(after.aliases, before.aliases) ? {} : { aliases: after.aliases }),
    ...(sameJson(after.people, before.people) ? {} : { people: after.people }),
    ...(sameJson(after.read, before.read) ? {} : { read: after.read }),
    ...(sameJson(after.ui.collapsed, before.ui.collapsed) ? {} : { collapsed: after.ui.collapsed }),
    ...(after.ui.lens === before.ui.lens ? {} : { lens: after.ui.lens }),
  };
}

function attempt<Value>(read: () => Value): Value | Error {
  try {
    return read();
  } catch (error) {
    return error instanceof Error ? error : new Error(String(error));
  }
}

/** Fired on `window` when this tab stores new tiles. */
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
