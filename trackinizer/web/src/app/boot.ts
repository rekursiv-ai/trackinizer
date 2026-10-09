import { queryOptions, type UseQueryResult, useQueries } from "@tanstack/react-query";
import { createContext, useContext } from "react";
import { getEdgeTopology, getEnums, getFieldOwners, type EdgeTopology, type Enums, type FieldOwners } from "../api/meta";
import { getProfile, type Profile } from "../api/me";
import { useOnline } from "../ui/bars";
import { prefetched } from "./prefetch";

/** The server's vocabulary, read once at boot. Nothing in the UI hard-codes it. */
export type Meta = {
  readonly enums: Enums;
  readonly fieldOwners: FieldOwners;
  readonly edges: EdgeTopology;
  /** Every inquiry kind, PascalCase, in the server's order. */
  readonly kinds: readonly string[];
};

/**
 * The boot reads, as TanStack Query options, so other code can refetch them by
 * key. The first fetch of each takes the read the entry started (`prefetch`).
 */
export const bootQueries = {
  enums: queryOptions({
    queryKey: ["meta", "enums"],
    queryFn: ({ queryKey, signal }) => prefetched(queryKey, () => getEnums({ signal })),
    staleTime: Infinity,
  }),
  fieldOwners: queryOptions({
    queryKey: ["meta", "fields"],
    queryFn: ({ queryKey, signal }) => prefetched(queryKey, () => getFieldOwners({ signal })),
    staleTime: Infinity,
  }),
  edges: queryOptions({
    queryKey: ["meta", "edges"],
    queryFn: ({ queryKey, signal }) => prefetched(queryKey, () => getEdgeTopology({ signal })),
    staleTime: Infinity,
  }),
  /** Refetch it after a 403: the role may have changed. */
  profile: queryOptions({
    queryKey: ["me", "profile"],
    queryFn: ({ queryKey, signal }) => prefetched(queryKey, () => getProfile({ signal })),
  }),
};

/** Where boot is: loading, failed (with a retry), or ready with everything read. */
export type Boot =
  | { readonly state: "loading" }
  | { readonly state: "failed"; readonly error: unknown; readonly retry: () => void }
  | { readonly state: "ready"; readonly meta: Meta; readonly profile: Profile };

/**
 * Read the meta calls and the profile, all four in parallel.
 *
 * Ready once all four have answered, and stays ready through later refetches,
 * so a failed profile refetch never tears down the app.
 */
export function useBoot(): Boot {
  return useQueries({
    queries: [bootQueries.enums, bootQueries.fieldOwners, bootQueries.edges, bootQueries.profile],
    combine: combineBoot,
  });
}

// Module-level so its identity is stable: TanStack re-runs an inline `combine`
// on every render, which would hand every consumer a new `meta` each time.
function combineBoot([enums, fieldOwners, edges, profile]: [
  UseQueryResult<Enums>,
  UseQueryResult<FieldOwners>,
  UseQueryResult<EdgeTopology>,
  UseQueryResult<Profile>,
]): Boot {
  if (enums.data && fieldOwners.data && edges.data && profile.data) {
    return {
      state: "ready",
      meta: {
        enums: enums.data,
        fieldOwners: fieldOwners.data,
        edges: edges.data,
        kinds: enums.data.inquiry_kind_all ?? [],
      },
      profile: profile.data,
    };
  }
  const failed = [enums, fieldOwners, edges, profile].filter((result) => result.isError);
  if (failed.length === 0) return { state: "loading" };
  return {
    state: "failed",
    error: failed[0].error,
    retry: () => failed.forEach((result) => void result.refetch()),
  };
}

export const MetaContext = createContext<Meta | null>(null);
export const ProfileContext = createContext<Profile | null>(null);

/**
 * Whether the page shows a locked inquiry that the signed-in user may not change
 * (only an admin may), so its write controls do not show.
 */
export const LockedContext = createContext(false);

/** The server's vocabulary. */
export function useMeta(): Meta {
  const meta = useContext(MetaContext);
  if (!meta) throw new Error("useMeta needs a MetaContext above it.");
  return meta;
}

/** The signed-in user. */
export function useProfile(): Profile {
  const profile = useContext(ProfileContext);
  if (!profile) throw new Error("useProfile needs a ProfileContext above it.");
  return profile;
}

/**
 * How write controls show: `hidden` for a viewer, and on a locked inquiry for
 * anyone but an admin, `disabled` while offline, `enabled` otherwise. The server
 * refuses a viewer's writes, and a locked row's, with 403 anyway.
 */
export function useWriteMode(): "hidden" | "disabled" | "enabled" {
  const online = useOnline();
  const locked = useContext(LockedContext);
  if (useProfile().role === "viewer" || locked) return "hidden";
  return online ? "enabled" : "disabled";
}
