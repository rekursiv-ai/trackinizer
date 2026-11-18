import type { QueryClient } from "@tanstack/react-query";
import { useSyncExternalStore } from "react";
import { applyWorkspaceOperation, type WorkspaceState } from "../api/workspaces";
import { acceptWorkspace } from "../stations/cache";

const TERMINAL = "trax.terminal";

// The command palette's "New terminal" asks the Terminal tile for a tab. The tile may not be on the canvas yet
// (the command shows it first), so the request waits here until a tile takes it.
let pending = false;
const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function set(next: boolean): void {
  pending = next;
  listeners.forEach((listener) => listener());
}

/** Ask the Terminal tile for a new terminal. */
export function requestNewTerminal(): void {
  set(true);
}

/** Take the request, if there is one: true for exactly one caller. */
export function takeNewTerminalRequest(): boolean {
  const was = pending;
  if (was) set(false);
  return was;
}

/** Whether a request waits for a tile. */
export function useNewTerminalRequested(): boolean {
  return useSyncExternalStore(subscribe, () => pending);
}

/**
 * The palette's "New terminal": show the station's Terminal tile (focus it when it is on the canvas) and ask it
 * for a tab. A show names no params, so the tile's machine is left as it is.
 */
export async function showNewTerminal(queryClient: QueryClient, station: WorkspaceState | null): Promise<void> {
  if (!station) return;
  const existing = station.visuals.find((visual) => visual.type === TERMINAL);
  requestNewTerminal();
  const operation = existing ? { kind: "focus", instance_id: existing.id } as const : { kind: "show", visual_type: TERMINAL, placement: "main" } as const;
  try {
    acceptWorkspace(queryClient, await applyWorkspaceOperation(station.id, station.revision, operation, crypto.randomUUID()));
  } catch (error) {
    takeNewTerminalRequest();
    throw error;
  }
}
