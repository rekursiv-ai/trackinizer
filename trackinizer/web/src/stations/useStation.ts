import { useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import { createDefaultWorkspace, getWorkspace, type WorkspaceState } from "../api/workspaces";
import { acceptWorkspace, newerWorkspace } from "./cache";
import { setStation, useCurrentStation } from "./store";

/** Where a station with no page of its own opens. */
const HOME = "#/graph";

/**
 * The station this tab shows, read from the query cache the live stream keeps.
 * A reload keeps the tab's station; a tab with none (a new one) asks the server
 * for the one the user changed last (made, if they have none). A station that is
 * gone, or is not the user's, is replaced the same way.
 */
export function useStation({ enabled = true }: { readonly enabled?: boolean } = {}) {
  const { id } = useCurrentStation();
  const queryClient = useQueryClient();
  return useQuery({
    queryKey: ["workspace", id ?? "boot"],
    queryFn: async ({ signal }): Promise<WorkspaceState> => {
      if (id) {
        try {
          // A write that landed while the read was out is kept: the cache is read again once it is back.
          const incoming = await getWorkspace(id, { signal });
          return newerWorkspace(queryClient.getQueryData<WorkspaceState>(["workspace", id]), incoming);
        } catch (error) {
          if (!(error instanceof ApiError && error.status === 404)) throw error;
        }
      }
      const state = await createDefaultWorkspace({ signal });
      acceptWorkspace(queryClient, state);
      setStation(state.id);
      return state;
    },
    enabled,
    staleTime: Infinity,
    retry: false,
  });
}

/**
 * Switch this tab to `state`, a station the server has just opened: its state
 * goes into the cache, the tab remembers it, and the page moves to the one the
 * station last showed (the graph when it showed none), or to `route` when the
 * caller keeps the page it is on, as a fork does.
 */
export function showStation(queryClient: QueryClient, state: WorkspaceState, route: string = state.page ?? HOME): void {
  acceptWorkspace(queryClient, state);
  setStation(state.id, route);
  if (window.location.hash !== route) window.location.hash = route;
}
