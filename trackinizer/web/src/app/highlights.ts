import { createContext, useContext, useSyncExternalStore } from "react";

/**
 * The inquiries an agent last pointed at on this tab, by id. A highlight is an
 * event on the canvas's stream, not canvas state: the newest frame's ids replace
 * the last, an empty list clears, and a reload starts with none. Views mark what
 * they draw from it as React state (`useIsHighlighted`, `useHighlighted`).
 */
export class HighlightStore {
  #ids: ReadonlySet<string> = NONE;
  readonly #listeners = new Set<() => void>();

  readonly subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => void this.#listeners.delete(listener);
  };

  readonly snapshot = (): ReadonlySet<string> => this.#ids;

  /** Point at `ids` and no others; the same set again tells no one. */
  set(ids: readonly string[]): void {
    const next = ids.length === 0 ? NONE : new Set(ids);
    if (next.size === this.#ids.size && [...next].every((id) => this.#ids.has(id))) return;
    this.#ids = next;
    for (const listener of [...this.#listeners]) listener();
  }
}

/** The canvas's store; null outside a canvas, where nothing is highlighted. */
export const HighlightContext = createContext<HighlightStore | null>(null);

/** Every highlighted id: the same set until the highlights change. */
export function useHighlighted(): ReadonlySet<string> {
  const store = useContext(HighlightContext);
  return useSyncExternalStore(store?.subscribe ?? unsubscribed, store?.snapshot ?? none);
}

/** Whether inquiry `id` is highlighted; its view draws again only when that changes. */
export function useIsHighlighted(id: string): boolean {
  const store = useContext(HighlightContext);
  return useSyncExternalStore(store?.subscribe ?? unsubscribed, () => store?.snapshot().has(id) ?? false);
}

const NONE: ReadonlySet<string> = new Set();
const none = () => NONE;
const unsubscribed = () => () => {};
