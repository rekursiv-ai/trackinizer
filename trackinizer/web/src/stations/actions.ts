import type { QueryClient } from "@tanstack/react-query";
import { clearChat as requestClearChat, createStation, hideStation as requestHideStation, listStations, renameStation, type StationSummary } from "../api/stations";
import { getWorkspace } from "../api/workspaces";
import { neighbourOf } from "../visuals/canvasTabs";
import { acceptWorkspace } from "./cache";
import { currentStation } from "./store";
import { showStation } from "./useStation";

/** The query key of the user's station list. */
export const STATIONS_KEY = ["stations"] as const;

/** The list workstream and chat pickers read: fetched again once it is a moment old. */
export const stationsQuery = { queryKey: STATIONS_KEY, queryFn: ({ signal }: { signal?: AbortSignal }) => listStations({ signal }), staleTime: 2_000 } as const;

/** Read station `id`, switch this tab to it, and refresh the list. */
export async function openStation(queryClient: QueryClient, id: string): Promise<void> {
  if (currentStation().id === id) return;
  showStation(queryClient, await getWorkspace(id));
  void queryClient.invalidateQueries({ queryKey: STATIONS_KEY });
}

/** Make a chat and switch to it. */
export async function newChat(queryClient: QueryClient): Promise<void> {
  showStation(queryClient, await createStation());
  void queryClient.invalidateQueries({ queryKey: STATIONS_KEY });
}

/** Rename a station; the cache and the list follow. A name already taken rejects with the server's 409. */
export async function nameStation(queryClient: QueryClient, id: string, name: string): Promise<void> {
  acceptWorkspace(queryClient, await renameStation(id, name));
  void queryClient.invalidateQueries({ queryKey: STATIONS_KEY });
}

/** Hide a station from the bar; when it is current, move to the next visible station. */
export async function hideStation(queryClient: QueryClient, id: string): Promise<void> {
  const onShow = currentStation().id === id;
  const visible = (queryClient.getQueryData<StationSummary[]>(STATIONS_KEY) ?? [])
    .toSorted((a, b) => a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id)).filter((each) => !each.hidden);
  const neighbour = neighbourOf(visible, id);
  const state = await requestHideStation(id);
  acceptWorkspace(queryClient, state);
  queryClient.setQueryData<StationSummary[]>(STATIONS_KEY, (held) => held?.map((each) => each.id === id ? { ...each, hidden: true } : each));
  if (onShow && neighbour) await openStation(queryClient, neighbour.id);
}

/** Clear a station's chat and open the new station returned by the server. */
export async function clearChat(queryClient: QueryClient, id: string, key: string): Promise<void> {
  const state = await requestClearChat(id, key);
  showStation(queryClient, state);
  void queryClient.invalidateQueries({ queryKey: STATIONS_KEY });
}
