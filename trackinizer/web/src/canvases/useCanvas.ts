import { useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import { createDefaultCanvas, getCanvas, type CanvasState } from "../api/canvases";
import { currentRoute, goTo } from "../router/addressBar";
import { acceptCanvas, newerCanvas } from "./cache";
import { setCanvas, useCurrentCanvas } from "./store";

/** Where a canvas with no page of its own opens. */
const HOME = "#/graph";

/**
 * The canvas this tab shows, read from the query cache the live stream keeps.
 * A reload keeps the tab's canvas; a tab with none (a new one) asks the server
 * for the one the user changed last (made, if they have none). A canvas that is
 * gone, or is not the user's, is replaced the same way.
 */
export function useCanvas({ enabled = true }: { readonly enabled?: boolean } = {}) {
  const { id } = useCurrentCanvas();
  const queryClient = useQueryClient();
  return useQuery({
    queryKey: ["canvas", id ?? "boot"],
    queryFn: async ({ signal }): Promise<CanvasState> => {
      if (id) {
        try {
          // A write that landed while the read was out is kept: the cache is read again once it is back.
          const incoming = await getCanvas(id, { signal });
          return newerCanvas(queryClient.getQueryData<CanvasState>(["canvas", id]), incoming);
        } catch (error) {
          if (!(error instanceof ApiError && error.status === 404)) throw error;
        }
      }
      const state = await createDefaultCanvas({ signal });
      acceptCanvas(queryClient, state);
      setCanvas(state.id);
      return state;
    },
    enabled,
    staleTime: Infinity,
    retry: false,
  });
}

/**
 * Switch this tab to `state`, a canvas the server has just opened: its state
 * goes into the cache, the tab remembers it, and the page moves to the one the
 * canvas last showed (the graph when it showed none), or to `route` when the
 * caller keeps the page it is on, as a fork does.
 */
export function showCanvas(queryClient: QueryClient, state: CanvasState, route: string = state.page ?? HOME): void {
  acceptCanvas(queryClient, state);
  setCanvas(state.id, route);
  if (currentRoute() !== route) goTo(route);
}
