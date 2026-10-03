import { type QueryClient, queryOptions } from "@tanstack/react-query";
import { findRef, getDetail, getEvidenceConfidence } from "../api/detail";
import { getGraphFocus } from "../api/graph";
import { prefetched } from "../app/prefetch";

/**
 * The detail view's reads, as TanStack Query options, so the stream layer can
 * refetch them by key: a detail by id, evidence confidence by id, and the graph
 * preview's neighbourhood by id. A link's id and detail take the reads the entry
 * started for the page's first view (`prefetch`).
 */
export const detailQueries = {
  /** A `Kind#seq` link's id. A seq never moves to another row, so it never goes stale. */
  ref: (kind: string, seq: number) =>
    queryOptions({
      queryKey: ["ref", kind, seq],
      queryFn: ({ queryKey, signal }) => prefetched(queryKey, () => findRef(kind, seq, { signal })),
      staleTime: Infinity,
    }),
  detail: (id: string) =>
    queryOptions({
      queryKey: ["detail", id],
      queryFn: ({ queryKey, signal }) => prefetched(queryKey, () => getDetail(id, { signal })),
    }),
  confidence: (id: string) =>
    queryOptions({
      queryKey: ["confidence", id],
      queryFn: ({ signal }) => getEvidenceConfidence(id, { signal }),
    }),
  /** What lies within `hops` of `id`: the nearest 60 inquiries, as many as the graph preview has room for. */
  neighbourhood: (id: string, hops: 1 | 2 | 3) =>
    queryOptions({
      queryKey: ["graph", "focus", id, hops],
      queryFn: ({ signal }) => getGraphFocus({ focus: id, hops, limit: NEIGHBOURHOOD_LIMIT }, { signal }),
    }),
};

/** The most inquiries the graph preview reads: the focus and its nearest 59. */
export const NEIGHBOURHOOD_LIMIT = 60;

/**
 * When each detail was last opened, by id, per cache. A detail's read time will
 * not do: a live update refetches an open detail, which is not opening it again.
 */
const OPENED = new WeakMap<QueryClient, Map<string, number>>();

/** Note that the detail of `id` opened now: as a page or in a peek. */
export function markOpened(queryClient: QueryClient, id: string): void {
  const opened = OPENED.get(queryClient) ?? new Map<string, number>();
  OPENED.set(queryClient, opened);
  opened.set(id, Date.now());
}

/** When the detail of `id` was last opened, in ms since the epoch; 0 if never. */
export function openedAt(queryClient: QueryClient, id: string): number {
  return OPENED.get(queryClient)?.get(id) ?? 0;
}
