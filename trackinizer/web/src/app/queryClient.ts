import { MutationCache, QueryCache, QueryClient } from "@tanstack/react-query";
import { ApiError, failureFields } from "../api/client";
import { log } from "../debug/log";

/**
 * The app's one cache of server data.
 *
 * A 401 from any read or write calls `onUnauthorized`: the session has ended.
 * A 403 is logged: the user's role cannot do what was asked. Reads retry as the
 * plan's failure table says (`retryRead`). Nothing refetches on window focus:
 * the live stream keeps data current, and a tab that was hidden catches up once
 * through the stream layer's gap recovery instead of refetching every query at
 * once.
 */
export function createQueryClient(onUnauthorized: () => void): QueryClient {
  const onError = (error: unknown) => {
    if (error instanceof ApiError && error.status === 401) onUnauthorized();
    if (error instanceof ApiError && error.status === 403) log("warn", "role.refused", failureFields(error));
  };
  return new QueryClient({
    queryCache: new QueryCache({ onError }),
    mutationCache: new MutationCache({ onError }),
    defaultOptions: {
      queries: {
        retry: retryRead,
        retryDelay: (failures) => READ_RETRY_DELAYS_MS[failures] ?? 0,
        refetchOnWindowFocus: false,
      },
    },
  });
}

/**
 * Whether a failed read tries again: twice, after 1 s and 3 s, and only when
 * no answer came (a timeout or a dropped network) or the server failed (5xx).
 * A 4xx is the server's answer, and asking again gets the same one.
 */
export function retryRead(failures: number, error: unknown): boolean {
  return (
    failures < READ_RETRY_DELAYS_MS.length &&
    error instanceof ApiError &&
    (error.status === 0 || error.status >= 500)
  );
}

const READ_RETRY_DELAYS_MS = [1000, 3000];
