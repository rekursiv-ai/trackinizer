import type { StationSummary } from "../api/stations";
import type { Workstream } from "../api/workstreams";

/** How many unpinned chats a workstream lists, newest first, after its pinned ones. */
export const RECENT_CHATS = 5;

/** A chat as a workstream lists it: enough to show and open it. */
export type ListedChat = { readonly id: string; readonly name: string };

/** Whether `station` is one of `stream`'s chats. */
export function belongs(station: { readonly workstream_id?: string | null }, stream: Pick<Workstream, "id" | "scratch">): boolean {
  // A chat made in Scratch names it; one made before workstreams names none. Both are Scratch's.
  return station.workstream_id === stream.id || (stream.scratch && station.workstream_id == null);
}

/**
 * The chats `stream` lists, as the top bar and Chat's header both show them:
 * pinned ones, then the newest unpinned ones, hidden ones left out. The chat on
 * show is among them under its name now, even before the list has caught up.
 */
export function chatsIn(
  stations: readonly StationSummary[],
  stream: Pick<Workstream, "id" | "scratch">,
  current: { readonly id: string; readonly name: string; readonly workstream_id?: string | null } | null | undefined,
): readonly ListedChat[] {
  const visible = stations.filter((each) => belongs(each, stream) && !each.hidden).toSorted(byRecent);
  const rows: ListedChat[] = [
    ...visible.filter((each) => each.pinned),
    ...visible.filter((each) => !each.pinned).slice(0, RECENT_CHATS),
  ];
  if (current && belongs(current, stream) && !rows.some((each) => each.id === current.id)) return [{ id: current.id, name: current.name }, ...rows];
  return rows.map((each) => (each.id === current?.id ? { id: each.id, name: current.name } : each));
}

/** Newest first, ties by id, so a list never reshuffles between reads. */
export function byRecent(left: StationSummary, right: StationSummary): number {
  return right.modified_at.localeCompare(left.modified_at) || left.id.localeCompare(right.id);
}
