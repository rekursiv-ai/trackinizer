import { useEffect, useSyncExternalStore } from "react";

/** The top bar's Workbench menu's body, which the canvas draws its controls into. */
let slot: HTMLElement | null = null;

/** How many canvases are mounted: the section shows while there is one. */
let canvases = 0;

/** Whoever reads the slot or the count: told of every change. */
const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function changed(): void {
  listeners.forEach((listener) => listener());
}

function takeSlot(element: HTMLElement | null): void {
  slot = element;
  changed();
}

/**
 * Where the canvas's controls stand: the Workbench menu of the top bar,
 * outside the canvas, so that they stay one place whatever Chat's side
 * and state. The canvas draws into it by a portal and keeps its own state.
 */
export function WorkbenchSlot() {
  return <div className="workbench-slot" ref={takeSlot} />;
}

/** The slot's element, or null where no top bar stands. */
export function useWorkbenchSlot(): HTMLElement | null {
  return useSyncExternalStore(subscribe, () => slot);
}

/** Whether a canvas is mounted, and so whether the top bar shows its Workbench menu. */
export function useWorkbenchPresent(): boolean {
  return useSyncExternalStore(subscribe, () => canvases > 0);
}

/** Tell the top bar that a canvas is mounted, for as long as the caller is. */
export function useWorkbenchMounted(): void {
  useEffect(() => {
    canvases += 1;
    changed();
    return () => {
      canvases -= 1;
      changed();
    };
  }, []);
}
