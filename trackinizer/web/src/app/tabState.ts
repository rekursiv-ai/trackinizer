import { useEffect, useState } from "react";

/**
 * A view's state kept in this browser tab's `sessionStorage` under `key`, so it
 * outlives the view: opening a row and coming Back finds the view as it was.
 *
 * What comes back is checked with `read`, which returns null for anything this
 * build cannot use. The key outlives a deploy, so an older build's shape, or a
 * hand edit, can be there; such a state starts the view afresh rather than
 * stopping it. Storage that is off or full only loses the state.
 */
export function useTabState<T>(
  key: string,
  read: (saved: unknown) => T | null,
  initial: () => T,
): [T, (change: (state: T) => T) => void] {
  const [state, setState] = useState<T>(() => {
    try {
      const saved = sessionStorage.getItem(key);
      if (saved !== null) return read(JSON.parse(saved)) ?? initial();
    } catch {
      // Unreadable storage or text: start afresh.
    }
    return initial();
  });
  useEffect(() => {
    try {
      sessionStorage.setItem(key, JSON.stringify(state));
    } catch {
      // Storage off or full: the view works, and forgets its state on the next visit.
    }
  }, [key, state]);
  return [state, setState];
}

/** Whether `value` is pages loaded per kind: each a whole number, at least one. */
export function isPageCounts(value: unknown): value is { readonly [kind: string]: number } {
  return (
    typeof value === "object" &&
    value !== null &&
    Object.values(value).every((pages) => Number.isSafeInteger(pages) && (pages as number) >= 1)
  );
}

/** Whether `value` is an array of strings. */
export function isStrings(value: unknown): value is readonly string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}
