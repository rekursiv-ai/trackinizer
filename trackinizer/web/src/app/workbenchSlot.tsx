import { useEffect, useSyncExternalStore } from "react";

/** The sidebar's Workbench section's body, which the canvas draws its controls into. */
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
 * Where the canvas's controls stand: the Workbench section of the sidebar,
 * outside the canvas, so that they stay one place whatever the sidebar's side
 * and state. The canvas draws into it by a portal and keeps its own state.
 */
export function WorkbenchSlot() {
  return <div className="workbench-slot" ref={takeSlot} />;
}

/** The slot's element, or null where no sidebar stands. */
export function useWorkbenchSlot(): HTMLElement | null {
  return useSyncExternalStore(subscribe, () => slot);
}

/** Whether a canvas is mounted, and so whether the sidebar shows its Workbench section. */
export function useWorkbenchPresent(): boolean {
  return useSyncExternalStore(subscribe, () => canvases > 0);
}

/** Tell the sidebar that a canvas is mounted, for as long as the caller is. */
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
