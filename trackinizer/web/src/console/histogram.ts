import { type QueryClient, queryOptions } from "@tanstack/react-query";
import { type Histogram, type HistogramRead, readHistogram } from "../api/histogram";
import type { FeedFilters } from "../api/sessions";
import { readOnce, refetchFresh } from "../live/cache";
import type { Batch, Later } from "../live/serial";
import type { Bucketing } from "./timeline";

/**
 * The read of the feed's records under `filters` in `bucketing`'s bars, from
 * the first that starts at or after `earliest`, the 7-day mark. The server
 * counts nothing before the mark, so the bar it falls in would hold only part
 * of its records. The read asks for one bucket more than the bars: the server's
 * window holds its end, so an end on a bucket's edge opens that bucket too, and
 * with one too few buckets the server would widen them all.
 */
export function spanRead(filters: FeedFilters, { since, until, seconds }: Bucketing, earliest: number): HistogramRead {
  const ms = seconds * 1000;
  const first = Math.max(since, Math.ceil(earliest / ms) * ms);
  return { ...filters, since: iso(first), until: iso(until), buckets: (until - first) / ms + 1 };
}

/**
 * The histogram `read` asks for, as TanStack Query options. Kept, never stale: a
 * past bucket never changes, since the feed dates a record when the server
 * writes it, and the live band's newest are kept current by `HistogramLive`.
 */
export function histogramQuery(read: HistogramRead) {
  return queryOptions({
    queryKey: ["console", "histogram", read],
    queryFn: ({ signal }) => readHistogram(read, { signal }),
    staleTime: Infinity,
  });
}

/**
 * The live band's counts, kept current by the stream. A batch never says what
 * changed, and any may be a captured record, so it reads again the bucket now
 * falls in and the one before (a record written just before a bucket's edge
 * comes after it), on the band's own grid, at most once every 2 s. The buckets
 * before them are past. Buckets newer than the band's last join it, for the
 * moments before the band reads its next span. A gap reads the whole band
 * again. While the band is scrolled back, batches change nothing it shows.
 */
export class HistogramLive {
  readonly #client: QueryClient;
  readonly #now: () => number;
  #read: HistogramRead | null = null;
  #lastAskAt = -Infinity;

  constructor(client: QueryClient, now: () => number = Date.now) {
    this.#client = client;
    this.#now = now;
  }

  /** Keep the band `read` reads current: the band at now, or null while it is scrolled back. */
  follow(read: HistogramRead | null): void {
    this.#read = read;
  }

  async update({ gap }: Batch): Promise<Later | null> {
    const read = this.#read;
    if (!read) return null;
    const key = histogramQuery(read).queryKey;
    if (gap) {
      await refetchFresh(this.#client, { queryKey: key, exact: true });
      return null;
    }
    const held = this.#client.getQueryState(key);
    // The band's read under way may predate the change: ask once it has loaded.
    if (held?.data === undefined) return held?.fetchStatus === "fetching" ? { afterMs: FIRST_READ_WAIT_MS } : null;
    const wait = this.#lastAskAt + ASK_EVERY_MS - this.#now();
    if (wait > 0) return { afterMs: wait };
    this.#lastAskAt = this.#now();
    // The server aligns its buckets to the epoch, so the grid is the bucket width alone.
    const bucketMs = held.data.bucket_seconds * 1000;
    const current = Math.floor(this.#now() / bucketMs) * bucketMs;
    // Three buckets, for the one the window's end opens.
    const newest = { ...read, since: iso(current - bucketMs), until: iso(current + bucketMs), buckets: 3 };
    const fresh = await readOnce(this.#client, ["histogram", newest], (signal) => readHistogram(newest, { signal }));
    this.#client.setQueryData(key, (data) => data && merged(data, fresh));
    return null;
  }
}

/** `time` (ms) as an ISO time, as the server's reads take one. */
export function iso(time: number): string {
  return new Date(time).toISOString();
}

/** `held`'s buckets with `fresh`'s counts in place, and any newer bucket after them. */
function merged(held: Histogram, fresh: Histogram): Histogram {
  const byStart = new Map([...held.counts, ...fresh.counts].map((bucket) => [Date.parse(bucket.start), bucket]));
  return { ...held, counts: [...byStart].toSorted(([a], [b]) => a - b).map(([, bucket]) => bucket) };
}

/** The least time between two reads of the newest buckets, the live layer's "at most every 2 s". */
const ASK_EVERY_MS = 2_000;

/** How often a batch that came during the band's first read looks again. */
const FIRST_READ_WAIT_MS = 500;
