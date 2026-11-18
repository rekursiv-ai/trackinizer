import type { QueryClient } from "@tanstack/react-query";
import type { WorkspaceState } from "../api/workspaces";

/** Keep a slower read or replay from replacing a newer canvas revision. */
export function newerWorkspace(previous: WorkspaceState | undefined, incoming: WorkspaceState): WorkspaceState {
  if (!previous) return incoming;
  return incoming.revision >= previous.revision ? incoming : previous;
}

/** Put a station's state in the cache, unless the cache holds a newer one. */
export function acceptWorkspace(client: QueryClient, state: WorkspaceState): void {
  client.setQueryData<WorkspaceState>(["workspace", state.id], (previous) => newerWorkspace(previous, state));
}
