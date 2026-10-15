import { useEffect, useSyncExternalStore } from "react";
import { type Snapshot, snapshotKey } from "./canvasSnapshot";

/** The most steps one canvas tab keeps; the oldest go first. */
export const KEPT_STEPS = 50;

/**
 * Changes this close after a step are part of it: an agent's calls sent back to
 * back, an address the router rewrites at once. A person's next click comes later.
 */
export const COALESCE_MS = 150;

/**
 * One canvas tab's steps, as a browser tab's history: the snapshots it showed,
 * oldest first, the one it stands at, and when the last one was taken.
 */
type Steps = { entries: Snapshot[]; index: number; takenAt: number };

/**
 * A restore on its way: the tab it is for and the snapshot it goes to. What the
 * canvas shows meanwhile is the restore's own doing, so no step is taken for it.
 */
type Pending = { readonly key: string; readonly target: Snapshot };

/**
 * Back and Forward for every canvas tab this page has shown, by station and
 * tab: kept in memory, as a browser tab keeps its own history, and gone with
 * the page.
 */
const histories = new Map<string, Steps>();
let pending: Pending | null = null;
let version = 0;
const listeners = new Set<() => void>();

function changed(): void {
  version += 1;
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/**
 * Take what the canvas of tab `key` shows now. A snapshot like the step it
 * stands at is no step; one taken within `COALESCE_MS` of the last step is
 * that step; any other is a new step, and the steps ahead of the one it
 * stands at are gone, as a browser's are after a new page. While a restore is
 * on its way, only its target counts: the canvas has arrived.
 */
export function observe(key: string, snapshot: Snapshot, now: number): void {
  if (pending) {
    if (pending.key === key && snapshotKey(pending.target) === snapshotKey(snapshot)) arrive(key, snapshot);
    return;
  }
  const steps = histories.get(key);
  if (!steps) {
    histories.set(key, { entries: [snapshot], index: 0, takenAt: -Infinity });
    changed();
    return;
  }
  if (snapshotKey(steps.entries[steps.index]!) === snapshotKey(snapshot)) return;
  if (now - steps.takenAt < COALESCE_MS && steps.index > 0 && steps.index === steps.entries.length - 1) {
    steps.entries[steps.index] = snapshot;
  } else {
    steps.entries = [...steps.entries.slice(0, steps.index + 1), snapshot].slice(-KEPT_STEPS);
    steps.index = steps.entries.length - 1;
  }
  steps.takenAt = now;
  changed();
}

/** Step tab `key` back (-1) or forward (1): the snapshot to restore, or null where there is none. */
export function step(key: string, by: -1 | 1): Snapshot | null {
  const steps = histories.get(key);
  const target = steps?.entries[steps.index + by];
  if (!steps || !target || pending) return null;
  steps.index += by;
  steps.takenAt = -Infinity;
  pending = { key, target };
  changed();
  return target;
}

/** Go to `target` as a new step of tab `key`, as Home and a tab's opening do, and wait for the canvas to show it. */
export function go(key: string, target: Snapshot): void {
  const steps = histories.get(key);
  if (steps && snapshotKey(steps.entries[steps.index]!) !== snapshotKey(target)) {
    steps.entries = [...steps.entries.slice(0, steps.index + 1), target].slice(-KEPT_STEPS);
    steps.index = steps.entries.length - 1;
    steps.takenAt = -Infinity;
  }
  pending = { key, target };
  changed();
}

/**
 * A restore of tab `key` is done: what the canvas shows (`snapshot`, null when
 * it is under a sheet) is where the tab stands, whether or not the restore got
 * there. A restore that already arrived, or another since, is left be.
 */
export function settle(key: string, target: Snapshot, snapshot: Snapshot | null): void {
  if (pending?.key !== key || pending.target !== target) return;
  arrive(key, snapshot ?? target);
}

/** Forget tab `key`'s steps, as closing it does. */
export function forget(key: string): void {
  if (histories.delete(key)) changed();
}

/** Forget every tab's steps; for a test's start. */
export function forgetHistories(): void {
  histories.clear();
  pending = null;
  changed();
}

/** Whether tab `key` can step back or forward, and whether a restore is on its way, followed as they change. */
export function useCanvasHistory(key: string | null, snapshot: Snapshot | null): { readonly back: boolean; readonly forward: boolean; readonly moving: boolean } {
  useSyncExternalStore(subscribe, () => version);
  const text = snapshot ? snapshotKey(snapshot) : null;
  useEffect(() => {
    if (key && snapshot) observe(key, snapshot, Date.now());
    // The snapshot is a new object each render; its text says when it changed.
  }, [key, text]);
  const steps = key ? histories.get(key) : undefined;
  return {
    back: !!steps && steps.index > 0,
    forward: !!steps && steps.index < steps.entries.length - 1,
    moving: pending !== null,
  };
}

/** The restore has shown its target: tab `key` stands at `snapshot`, its steps begun with it if it had none. */
function arrive(key: string, snapshot: Snapshot): void {
  pending = null;
  const steps = histories.get(key);
  if (!steps) histories.set(key, { entries: [snapshot], index: 0, takenAt: -Infinity });
  else steps.entries[steps.index] = snapshot;
  changed();
}
