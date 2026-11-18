import { type CallOptions, client, send, TIMEOUT_MS } from "./client";
import type { components } from "./generated/schema";
import { type FeedFilters, filterQuery } from "./sessions";

/**
 * One histogram read: the feed's filters, a window within the last 7 days (ISO
 * times), and how many equal buckets to split it into (2 to 1,000, 120 by
 * default). No `since` starts at the first record of those 7 days, and an
 * earlier one at the 7-day mark; no `until` ends now, and one before the mark
 * answers 400.
 */
export type HistogramRead = FeedFilters & {
  readonly since?: string;
  readonly until?: string;
  readonly buckets?: number;
};

/** Records per bucket, oldest first, over the window from `start` to `end`. */
export type Histogram = components["schemas"]["FeedHistogramResponse"];

/** Fetch `GET /api/web/feed/histogram`: how many feed records fall in each bucket of a window, under the feed's filters. */
export async function readHistogram(
  { since, until, buckets, ...filters }: HistogramRead,
  { signal }: CallOptions = {},
): Promise<Histogram> {
  return send(TIMEOUT_MS.read, signal, (signal) =>
    client.GET("/api/web/feed/histogram", {
      params: {
        query: {
          ...filterQuery(filters),
          ...(since ? { since } : {}),
          ...(until ? { until } : {}),
          ...(buckets ? { buckets } : {}),
        },
      },
      signal,
    }),
  );
}
