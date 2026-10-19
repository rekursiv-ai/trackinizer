import { useSyncExternalStore } from "react";

/** The key this browser keeps each station's flip under. */
const KEY = (station: string) => `trackinizer.chat.flip.${station}`;
const BAR_KEY = "trackinizer.chat.terminalBar.dismissed";

const listeners = new Set<() => void>();
/** What this page knows when the browser's storage is unavailable. */
const held = new Map<string, string>();

function read(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return held.get(key) ?? null;
  }
}

function write(key: string, value: string | null): void {
  if (value === null) held.delete(key);
  else held.set(key, value);
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    // Storage is a convenience: the page keeps the value for its own life.
  }
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Whether `station` shows its terminal side, as this viewer left it. */
export function flipped(station: string): boolean {
  return read(KEY(station)) === "terminal";
}

/** Show `station`'s terminal side, or its Chat. */
export function setFlipped(station: string, terminal: boolean): void {
  write(KEY(station), terminal ? "terminal" : null);
}

/** `station`'s flip, kept current. */
export function useFlipped(station: string | undefined): boolean {
  return useSyncExternalStore(subscribe, () => station !== undefined && flipped(station));
}

/**
 * Whether this viewer dismissed the terminal side's bar, how to dismiss it, how to reopen it with its help
 * disclosure open, and whether it was reopened that way.
 */
export function useBarDismissed(): readonly [boolean, () => void, () => void, boolean] {
  const state = useSyncExternalStore(subscribe, () => read(BAR_KEY));
  return [state === "1", () => write(BAR_KEY, "1"), () => write(BAR_KEY, "open"), state === "open"];
}
