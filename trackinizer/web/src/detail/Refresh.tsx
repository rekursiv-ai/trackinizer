import { type QueryKey, useIsFetching, useQueryClient } from "@tanstack/react-query";
import { useCallback, useSyncExternalStore } from "react";
import { CopyDetails } from "../debug/CopyDetails";

/**
 * A detail section's Refresh, for reads the live stream does not carry: it reads
 * every query under `queryKey` again.
 *
 * A refresh that fails while the section still shows data says so here, in the
 * header line, with Retry: the data stays, and a message in the section's body
 * would push down everything below it.
 */
export function Refresh({ queryKey, title }: { queryKey: QueryKey; title: string }) {
  const queryClient = useQueryClient();
  const fetching = useIsFetching({ queryKey }) > 0;
  const failed = useRefreshFailure(queryKey);
  const refresh = () => void queryClient.invalidateQueries({ queryKey });
  if (failed && !fetching) {
    return (
      <span className="sec-error" role="alert">
        Could not refresh: {failed.message}
        <button type="button" className="btn ghost" onClick={refresh}>
          Retry
        </button>
        <CopyDetails message={`Could not refresh: ${failed.message}`} error={failed} />
      </span>
    );
  }
  return (
    <button type="button" className="btn ghost" onClick={refresh} disabled={fetching} title={title}>
      {fetching ? "Refreshing…" : "Refresh"}
    </button>
  );
}

/** The failure of the first read under `queryKey` that failed to refresh data it still shows, or null. */
function useRefreshFailure(queryKey: QueryKey): Error | null {
  const queryClient = useQueryClient();
  const subscribe = useCallback((changed: () => void) => queryClient.getQueryCache().subscribe(changed), [queryClient]);
  return useSyncExternalStore(subscribe, () => {
    const failed = queryClient
      .getQueryCache()
      .findAll({ queryKey })
      .find(({ state }) => state.status === "error" && state.data !== undefined);
    return failed?.state.error ?? null;
  });
}
