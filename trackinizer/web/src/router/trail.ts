import { useSyncExternalStore } from "react";

/** The most hashes the trail keeps: the current page and the 8 a message may carry besides it. */
const KEPT = 9;

/** The longest hash the server takes as a page. */
const MAX_HASH = 512;

/** A hash the server takes as a page: `#/`, then no whitespace or control character. */
const PAGE = /^#\/[^\s\u0000-\u001f\u007f]*$/;

let trail: readonly string[] = [];

/** Whether the server takes `hash` as a page. */
export function validHash(hash: string): boolean {
  return hash.length <= MAX_HASH && PAGE.test(hash);
}

/** The hashes the app visited, oldest first: the last 9, the page it is on last. */
export function visited(): readonly string[] {
  return trail;
}

/**
 * Record the hash the app is on and every one it moves to, so that Chat can say
 * where the user has been however late it opens. A hash the server would refuse
 * is not kept, nor is one the same as the last. Returns the function that stops
 * it and forgets what it recorded.
 */
export function startTrail(): () => void {
  const record = () => {
    const hash = location.hash;
    if (!validHash(hash) || trail.at(-1) === hash) return;
    trail = [...trail, hash].slice(-KEPT);
  };
  trail = [];
  record();
  addEventListener("hashchange", record);
  return () => {
    removeEventListener("hashchange", record);
    trail = [];
  };
}

/** The address bar's hash, live. */
export function useHash(): string {
  return useSyncExternalStore(subscribe, () => location.hash);
}

function subscribe(onChange: () => void): () => void {
  addEventListener("hashchange", onChange);
  return () => removeEventListener("hashchange", onChange);
}
