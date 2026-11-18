import { queryOptions, type QueryKey, useQuery, type UseQueryOptions } from "@tanstack/react-query";
import { readTrust } from "./summaries";

/** A read that runs once the trusted accounts are known; the hook owns its `enabled`. */
export type TrustedQuery<T, K extends QueryKey> = Omit<UseQueryOptions<T, Error, T, K>, "enabled">;

/** A trusted read's state: the document, the trust or document error, and a refetch of whichever failed. */
export type TrustedRead<T> = {
  readonly data: T | undefined;
  readonly error: Error | null;
  readonly refetch: () => void;
  readonly isSuccess: boolean;
};

/** The profile's trust, one read shared by every trusted reader. */
export const trustQuery = queryOptions({ queryKey: ["me", "trust"], queryFn: ({ signal }) => readTrust({ signal }), staleTime: 300_000 });

/**
 * Read a document through the accounts the profile trusts: the profile's trust
 * first (shared under `["me", "trust"]`), then `query(accounts)`. Neither runs
 * unless `enabled`. A trust failure is the read's error, and refetch retries it.
 */
export function useTrustedRead<T, K extends QueryKey>(enabled: boolean, query: (accounts: readonly string[]) => TrustedQuery<T, K>): TrustedRead<T> {
  const trust = useQuery({ ...trustQuery, enabled });
  const accounts = trust.data?.accounts;
  const read = useQuery({ ...query(accounts ?? []), enabled: enabled && accounts !== undefined });
  return { data: read.data, error: trust.error ?? read.error, refetch: () => void (trust.error ? trust.refetch() : read.refetch()), isSuccess: read.isSuccess };
}
