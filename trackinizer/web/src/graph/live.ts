import { type QueryClient, queryOptions, useQueryClient } from "@tanstack/react-query";
import { useContext, useEffect, useState } from "react";
import { type Graph, getGraph } from "../api/graph";
import { prefetched, takeReadAt } from "../app/prefetch";
import { LiveContext } from "../live";
import { refetchFresh } from "../live/cache";
import type { Batch, Failure, Later } from "../live/serial";

/**
 * The graph of at most `limit` inquiries, as TanStack Query options, so the
 * stream layer finds it by key. Its first fetch takes the read the entry
 * started for the home view (`prefetch`).
 */
export function graphQuery(limit: number) {
  return queryOptions({
    queryKey: ["graph", limit],
    queryFn: ({ queryKey, signal }) => prefetched(queryKey, () => getGraph(limit, { signal })),
  });
}

/**
 * The graph, kept current by the stream: any batch, or a gap, reads the whole
 * graph again, at most once every 2 ms for each node it holds and never
 * sooner than 2 s apart: 2 s up to 1,000 nodes, 10 s at 5,000, 40 s at 20,000.
 * The interval runs from the last read, its own or the view's (the first read,
 * or one on mounting); a batch sooner waits for the rest of it, and a gap does
 * not wait. The bigger the graph, the longer an answer that changes its shape
 * keeps the tab busy laying it out.
 *
 * Reading it all again is the only answer that is right whatever a frame's id
 * was: a new inquiry that belongs, an old one past the limit that does not
 * (v1's FR-06), an edge change on both ends, or a purge. TanStack keeps an
 * unchanged answer's `data` as it was, so the view redraws nothing for it.
 */
export class GraphLive {
  readonly #client: QueryClient;
  readonly #limit: number;
  /** When this last started a read: one under way has no answer to stamp yet. */
  #lastReadAt = -Infinity;

  constructor(client: QueryClient, limit: number) {
    this.#client = client;
    this.#limit = limit;
  }

  async update({ gap }: Batch): Promise<Later | null> {
    const { queryKey } = graphQuery(this.#limit);
    const state = this.#client.getQueryState<Graph>(queryKey);
    const held = state?.data?.nodes.length ?? 0;
    // TanStack stamps each answer with `Date.now()`, so the clock is that one.
    const readAt = Math.max(this.#lastReadAt, state?.dataUpdatedAt ?? -Infinity);
    const wait = readAt + Math.max(MIN_INTERVAL_MS, MS_PER_NODE * held) - Date.now();
    if (!gap && wait > 0) return { afterMs: wait };
    this.#lastReadAt = Date.now();
    await refetchFresh(this.#client, { type: "active", queryKey, exact: true });
    return null;
  }
}

/** Keep the graph of `limit` inquiries current while it is mounted; returns its live updates' failure, if they keep failing. */
export function useLiveGraph(limit: number): Failure | null {
  const hub = useContext(LiveContext);
  const client = useQueryClient();
  const [failure, setFailure] = useState<Failure | null>(null);
  useEffect(() => {
    if (!hub) return;
    const live = new GraphLive(client, limit);
    // Its read may be the one main.tsx started before the stream opened.
    const readAt = takeReadAt(([scope, read]) => scope === "graph" && read === limit);
    const registration = hub.register({ update: (batch) => live.update(batch), failing: setFailure }, { readAt });
    return () => {
      registration.dispose();
      setFailure(null);
    };
  }, [hub, client, limit]);
  return failure;
}

const MIN_INTERVAL_MS = 2_000;
/**
 * The interval per node held: 10 s at 5,000 nodes. Above 5,000, each frame of
 * a new layout was a long task, 50 to 90 ms at 7,500 to 10,000 nodes, for the
 * 9 to 12 s it ran (2026-10-02, this Mac, synthetic graphs).
 */
const MS_PER_NODE = 2;
