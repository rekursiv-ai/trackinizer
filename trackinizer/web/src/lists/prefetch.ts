import { type QueryClient, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { detailQueries } from "../detail/queries";

/**
 * Read a row's detail ahead of its opening (SP6): `intend(id)` once the
 * pointer rests on the row or the keyboard moves the focus to it, and
 * `intend(null)` when it leaves. A row held for `DWELL_MS` has its detail read
 * into the detail's own query, so an open renders at once; one passed over
 * sooner is not read. A median detail is 22.6 KB on production, so a detail
 * read in the last `FRESH_MS` is not read again, and at most `CAP` are read a
 * minute.
 */
export class DetailWarmer {
  readonly #client: QueryClient;
  #held: string | null = null;
  #timer: ReturnType<typeof setTimeout> | undefined;
  /** When each read of the last minute started. */
  #reads: number[] = [];

  constructor(client: QueryClient) {
    this.#client = client;
  }

  readonly intend = (id: string | null): void => {
    if (id === this.#held) return;
    this.#held = id;
    clearTimeout(this.#timer);
    if (id !== null) this.#timer = setTimeout(() => this.#warm(id), DWELL_MS);
  };

  #warm(id: string): void {
    const query = detailQueries.detail(id);
    const state = this.#client.getQueryState(query.queryKey);
    const now = Date.now();
    if (state?.fetchStatus === "fetching" || (state?.data !== undefined && now - state.dataUpdatedAt < FRESH_MS)) return;
    this.#reads = this.#reads.filter((at) => now - at < MINUTE_MS);
    if (this.#reads.length >= CAP) return;
    this.#reads.push(now);
    void this.#client.prefetchQuery(query);
  }
}

/** A list's warmer; whatever it holds is let go when the list unmounts. */
export function useDetailWarmer(): DetailWarmer["intend"] {
  const client = useQueryClient();
  const [warmer] = useState(() => new DetailWarmer(client));
  useEffect(() => () => warmer.intend(null), [warmer]);
  return warmer.intend;
}

/** How long the pointer or the focus rests on a row before its detail is read: Linear's hover prefetch. */
const DWELL_MS = 150;

/** How old a cached detail may be and still spare a read. */
const FRESH_MS = 30_000;

/** The most details read ahead a minute. */
const CAP = 10;

const MINUTE_MS = 60_000;
