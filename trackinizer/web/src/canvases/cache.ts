import type { QueryClient } from "@tanstack/react-query";
import type { CanvasState } from "../api/canvases";

/** Keep a slower read or replay from replacing a newer canvas revision. */
export function newerCanvas(previous: CanvasState | undefined, incoming: CanvasState): CanvasState {
  if (!previous) return incoming;
  return incoming.revision >= previous.revision ? incoming : previous;
}

/** Put a canvas's state in the cache, unless the cache holds a newer one. */
export function acceptCanvas(client: QueryClient, state: CanvasState): void {
  client.setQueryData<CanvasState>(["canvas", state.id], (previous) => newerCanvas(previous, state));
}
