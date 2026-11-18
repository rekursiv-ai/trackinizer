import { infiniteQueryOptions } from "@tanstack/react-query";
import { client, send, TIMEOUT_MS, type CallOptions } from "./client";
import type { components } from "./generated/schema";

export type LineagePage = components["schemas"]["LineagePage"];
export type LineageEdge = components["schemas"]["LineageEdge"];
export type LineageFilters = {
  readonly direction: "in" | "out";
  readonly edgeKinds?: readonly LineageEdge["edge_kind"][];
  readonly peerKind?: components["schemas"]["InquiryKind"];
  readonly pageSize?: number;
};

/** Distinct peers in stable date/UUID order; the selected record stays outside the quota. */
export function lineageQuery(recordId: string, filters: LineageFilters) {
  return infiniteQueryOptions({
    queryKey: ["visual", "lineage", recordId, filters],
    queryFn: ({ pageParam, signal }) => getLineagePage(recordId, filters, { cursor: pageParam, signal }),
    initialPageParam: "",
    getNextPageParam: (page: LineagePage) => page.next_cursor ?? undefined,
    enabled: !!recordId,
    retry: false,
    refetchInterval: 30_000,
  });
}

async function getLineagePage(recordId: string, { direction, edgeKinds, peerKind, pageSize = 20 }: LineageFilters,
  { cursor, signal }: CallOptions & { readonly cursor: string }): Promise<LineagePage> {
  if (!Number.isInteger(pageSize) || pageSize < 1 || pageSize > 50) throw new Error("Invalid lineage page size");
  return send(TIMEOUT_MS.read, signal, (signal) => client.GET("/api/visuals/lineage/{record_id}", {
    params: { path: { record_id: recordId }, query: {
      direction, edge_kind: edgeKinds ? [...edgeKinds] : undefined, peer_kind: peerKind, page_size: pageSize, cursor,
    } }, signal,
  }));
}
