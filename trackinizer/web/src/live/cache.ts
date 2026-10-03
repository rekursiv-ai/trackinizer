// The stream layer's reads, made through the query cache so that a 401 ends
// the session as any read does.
import type { QueryClient, QueryFilters } from "@tanstack/react-query";

/**
 * Read once through the cache under `["live", ...key]`, with no retries of its
 * own (the caller's `Serial` retries). Nothing keeps the entry: it is gone once
 * the answer is taken.
 */
export function readOnce<T>(client: QueryClient, key: readonly unknown[], read: (signal: AbortSignal) => Promise<T>): Promise<T> {
  return client.fetchQuery({
    queryKey: ["live", ...key],
    queryFn: ({ signal }) => read(signal),
    staleTime: 0,
    gcTime: 0,
    retry: false,
  });
}

/**
 * Refetch the queries `filters` picks, reusing a fetch already under way
 * (`cancelRefetch: false`) rather than cancelling it.
 *
 * A fetch under way may have started before the change and answer without it,
 * and TanStack does not queue a second run, so each query that was fetching is
 * refetched once more after that fetch ends; the others only once. A failed
 * refetch rejects, as TanStack otherwise swallows it: the caller's `Serial`
 * then tries again, where a swallowed failure would leave the view stale.
 */
export async function refetchFresh(client: QueryClient, filters: QueryFilters): Promise<void> {
  const underWay = client
    .getQueryCache()
    .findAll(filters)
    .filter((query) => query.state.fetchStatus === "fetching");
  await client.refetchQueries(filters, { cancelRefetch: false, throwOnError: true });
  await Promise.all(
    underWay.map((query) => client.refetchQueries({ queryKey: query.queryKey, exact: true }, { cancelRefetch: false, throwOnError: true })),
  );
}
