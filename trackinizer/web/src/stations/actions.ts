import type { QueryClient } from "@tanstack/react-query";
import { createStation, listStations, renameStation, type StationSummary } from "../api/stations";
import { getWorkspace } from "../api/workspaces";
import { useSyncExternalStore } from "react";
import { acceptWorkspace } from "./cache";
import { stepStation } from "./cycle";
import { currentStation } from "./store";
import { showStation } from "./useStation";

/** The query key of the user's station list. */
export const STATIONS_KEY = ["stations"] as const;

/** The list the menu and the cycle read: fetched again once it is a moment old. */
export const stationsQuery = { queryKey: STATIONS_KEY, queryFn: ({ signal }: { signal?: AbortSignal }) => listStations({ signal }), staleTime: 2_000 } as const;

/** Read station `id`, switch this tab to it, and refresh the list. */
export async function openStation(queryClient: QueryClient, id: string): Promise<void> {
  if (currentStation().id === id) return;
  showStation(queryClient, await getWorkspace(id));
  void queryClient.invalidateQueries({ queryKey: STATIONS_KEY });
}

/** Make a station, named `unnamed-workstream#N`, and switch to it. */
export async function newStation(queryClient: QueryClient): Promise<void> {
  showStation(queryClient, await createStation());
  void queryClient.invalidateQueries({ queryKey: STATIONS_KEY });
}

/** Rename a station; the cache and the list follow. A name already taken rejects with the server's 409. */
export async function nameStation(queryClient: QueryClient, id: string, name: string): Promise<void> {
  acceptWorkspace(queryClient, await renameStation(id, name));
  void queryClient.invalidateQueries({ queryKey: STATIONS_KEY });
}

/** Switch to the renamed station after (1) or before (-1) this one in the cycle, wrapping; nothing when none was renamed. */
export async function cycleStation(queryClient: QueryClient, step: 1 | -1): Promise<void> {
  const stations: StationSummary[] = await queryClient.fetchQuery(stationsQuery);
  const next = stepStation(stations, currentStation().id, step);
  if (next) await openStation(queryClient, next.id);
}

let menuOpen = false;
const listeners = new Set<() => void>();

/** Open or close the station menu in Chat's header, from the menu's button or `g s`. */
export function setStationMenu(open: boolean): void {
  if (open === menuOpen) return;
  menuOpen = open;
  listeners.forEach((listener) => listener());
}

/** Whether the station menu is open. */
export function useStationMenu(): boolean {
  return useSyncExternalStore((listener) => {
    listeners.add(listener);
    return () => listeners.delete(listener);
  }, () => menuOpen);
}
